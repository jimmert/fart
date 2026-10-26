"""pump.fun Global account decoding and the config comparison (synthetic bytes with the VPS values)."""
import base64
import hashlib
import struct

from fart.chain import global_account as ga
from fart.chain.base58 import b58decode, b58encode
from fart.net.budget import Blocked

PUMP = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"


def pk(seed: str) -> bytes:
    return hashlib.sha256(seed.encode()).digest()


def build(fee=95, creator=30, tail=True, disc_ok=True, extra=b""):
    data = (ga.ACCOUNT_DISCRIMINATOR if disc_ok else b"\x00" * 8) + b"\x01" + pk("authority") + pk("fee-recipient")
    data += struct.pack("<QQQQQ", 1_073_000_000_000_000, 30_000_000_000, 793_100_000_000_000, 1_000_000_000_000_000, fee)
    assert len(data) == ga.VERIFIED_END_OFFSET
    if tail:
        data += pk("withdraw") + b"\x01" + struct.pack("<QQ", 150_000_000, creator) + b"".join(pk(f"fr{i}") for i in range(7))
        data += pk("set-creator") + pk("admin-set-creator")
    return data + extra


def test_decode_verified_and_to_verify():
    g = ga.decode_global(build())
    assert g.discriminator_ok and g.error is None and not g.suspect
    assert g.verified["fee_basis_points"] == 95 and g.verified["initial_virtual_sol_reserves"] == 30_000_000_000
    assert g.verified["authority"] == b58encode(pk("authority"))
    assert g.to_verify["creator_fee_basis_points"] == 30 and g.to_verify["enable_migrate"] == 1
    assert len(g.to_verify["fee_recipients"]) == 7 and g.extra_bytes == 0
    g2 = ga.decode_global(build(tail=False))
    assert g2.verified["fee_basis_points"] == 95 and g2.to_verify == {} and g2.consumed == ga.VERIFIED_END_OFFSET
    g3 = ga.decode_global(build(extra=b"\x07" * 40))
    assert g3.extra_bytes == 40
    g4 = ga.decode_global(build(fee=50_000))
    assert g4.suspect and "fee_basis_points" in g4.suspect[0]


def test_truncated_and_garbage_never_raise():
    assert ga.decode_global(b"").error == "too_short"
    t = ga.decode_global(build()[:60])
    assert t.error and t.error.startswith("truncated") and "fee_basis_points" not in t.verified
    assert t.verified["initialized"] == 1  # what could be read is kept; fetch_and_check reports it as undecodable
    g = ga.decode_global(build(disc_ok=False))
    assert g.discriminator_ok is False and g.verified["fee_basis_points"] == 95


def test_compare_with_config(settings):
    cfg = dict(settings.config("pumpfun"))
    assert cfg["fee_bps"] == 95 and cfg["creator_fee_bps"] == 30 and cfg["total_curve_fee_bps"] == 125
    assert ga.compare_with_config(ga.decode_global(build()), cfg) == []
    m = ga.compare_with_config(ga.decode_global(build()), dict(cfg, fee_bps=100))
    assert [x["config_key"] for x in m] == ["fee_bps"] and m[0]["chain"] == 95 and m[0]["verified_layout"] is True
    m2 = ga.compare_with_config(ga.decode_global(build(creator=50)), cfg)
    keys = {x["config_key"] for x in m2}
    assert keys == {"creator_fee_bps", "total_curve_fee_bps"} and all(x["verified_layout"] is False for x in m2)
    # creator fee absent from the chain data (old layout) is not a mismatch
    assert ga.compare_with_config(ga.decode_global(build(tail=False)), cfg) == []


class FakeRpc:
    def __init__(self, value=None, exc=None):
        self.value = value
        self.exc = exc

    def get_account_info_base64(self, address):
        if self.exc:
            raise self.exc
        return self.value


def test_fetch_and_check(settings):
    cfg = settings.config("pumpfun")
    value = {"data": [base64.b64encode(build()).decode(), "base64"], "owner": PUMP, "lamports": 1}
    res = ga.fetch_and_check(FakeRpc(value), cfg)
    assert res["status"] == "ok" and res["decoded"]["verified"]["fee_basis_points"] == 95 and res["address"] == cfg["global_account"]
    res = ga.fetch_and_check(FakeRpc(value), dict(cfg, fee_bps=100))
    assert res["status"] == "mismatch" and res["mismatches"][0]["config_key"] == "fee_bps"
    res = ga.fetch_and_check(FakeRpc(dict(value, owner="11111111111111111111111111111111")), cfg)
    assert res["status"] == "mismatch" and res["mismatches"][0]["field"] == "owner"
    res = ga.fetch_and_check(FakeRpc(exc=Blocked("rpc", "getAccountInfo", "daily_cost_cap", 10)), cfg)
    assert res["status"] == "unavailable" and "Blocked" in res["error"]
    res = ga.fetch_and_check(FakeRpc({"data": ["!!!", "base64"], "owner": PUMP}), cfg)
    assert res["status"] == "undecodable"
    res = ga.fetch_and_check(FakeRpc(None), cfg)
    assert res["status"] == "undecodable"
