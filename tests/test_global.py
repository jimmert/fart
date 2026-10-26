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


def build(fee=95, creator=5, tail=True, disc_ok=True, extra=b"", idl_tail=False):
    """Synthetic Global account with the VPS values (26 Oct 2026): creator_fee_basis_points 5 at offset 154."""
    data = (ga.ACCOUNT_DISCRIMINATOR if disc_ok else b"\x00" * 8) + b"\x01" + pk("authority") + pk("fee-recipient")
    data += struct.pack("<QQQQQ", 1_073_000_000_000_000, 30_000_000_000, 793_100_000_000_000, 1_000_000_000_000_000, fee)
    assert len(data) == ga.MINIMUM_END_OFFSET == 113
    if tail:
        data += pk("withdraw") + b"\x01" + struct.pack("<QQ", 150_000_000, creator) + b"".join(pk(f"fr{i}") for i in range(7))
        data += pk("set-creator") + pk("admin-set-creator")
        assert len(data) == ga.VERIFIED_END_OFFSET == 450
    if idl_tail:  # the newer fields of idl/pump.json (26 Oct 2026), offsets 450-1088
        data += b"\x01" + pk("whitelist") + pk("reserved-fr") + b"\x00" + b"".join(pk(f"rfr{i}") for i in range(7)) + b"\x01"
        data += b"".join(pk(f"bb{i}") for i in range(8)) + struct.pack("<QQ", 10, 30_000_000_000) + pk("usdc") + b"\x01"
        data += struct.pack("<Q", 100) + pk("holder-claim") + b"\x00" + b"\x03"
        assert len(data) == ga.IDL_END_OFFSET == 1088
    return data + extra


def test_decode_verified_and_idl_fields():
    g = ga.decode_global(build())
    assert g.discriminator_ok and g.error is None and not g.suspect
    assert g.verified["fee_basis_points"] == 95 and g.verified["initial_virtual_sol_reserves"] == 30_000_000_000
    assert g.verified["authority"] == b58encode(pk("authority"))
    # the fields past offset 113 are verified now (IDL layout = the VPS read: creator fee 5 bps at offset 154)
    assert g.verified["creator_fee_basis_points"] == 5 and g.verified["enable_migrate"] == 1 and g.verified["pool_migration_fee"] == 150_000_000
    assert len(g.verified["fee_recipients"]) == 7 and g.extra_bytes == 0 and g.to_verify == {} and g.consumed == 450
    # an older, shorter account keeps what it carries and is not an error
    g2 = ga.decode_global(build(tail=False))
    assert g2.verified["fee_basis_points"] == 95 and "creator_fee_basis_points" not in g2.verified and g2.consumed == ga.MINIMUM_END_OFFSET
    assert g2.error is None
    # the newer IDL fields decode tolerantly into to_verify
    g5 = ga.decode_global(build(idl_tail=True))
    assert g5.to_verify["create_v2_enabled"] == 1 and g5.to_verify["buyback_basis_points"] == 10 and g5.to_verify["max_curve_depth"] == 3
    assert len(g5.to_verify["buyback_fee_recipients"]) == 8 and g5.to_verify["max_configurable_creator_fee_bps"] == 100
    assert g5.consumed == ga.IDL_END_OFFSET and g5.extra_bytes == 0 and not g5.suspect
    g3 = ga.decode_global(build(extra=b"\x07" * 40))
    assert g3.consumed == 483 and g3.extra_bytes == 7  # create_v2_enabled + whitelist_pda fit; the next pubkey does not
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
    assert cfg["fee_bps"] == 95 and cfg["creator_fee_bps"] == 5 and cfg["total_curve_fee_bps"] == 100  # 95 + 5 = the 1% curve fee
    assert ga.compare_with_config(ga.decode_global(build()), cfg) == []
    m = ga.compare_with_config(ga.decode_global(build()), dict(cfg, fee_bps=100))
    assert [x["config_key"] for x in m] == ["fee_bps"] and m[0]["chain"] == 95 and m[0]["verified_layout"] is True
    # the milestone-2 config (30 / 125) is now a mismatch on a verified field
    m2 = ga.compare_with_config(ga.decode_global(build()), dict(cfg, creator_fee_bps=30, total_curve_fee_bps=125))
    keys = {x["config_key"] for x in m2}
    assert keys == {"creator_fee_bps", "total_curve_fee_bps"} and all(x["verified_layout"] is True for x in m2)
    # creator fee absent from the chain data (old, short account) is not a mismatch
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
