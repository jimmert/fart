"""pump.fun Global account: fetch, decode, compare with config/pumpfun.json.

(The brief names this module `global.py`; `global` is a Python keyword, so the file is
`global_account.py`.)

Layout (Anchor account, 8-byte discriminator sha256("account:Global")[:8] first):

    offset  field                            type      status
    0       discriminator                    8 bytes
    8       initialized                      u8        verified on the VPS (26 Oct 2026)
    9       authority                        pubkey    verified
    41      fee_recipient                    pubkey    verified
    73      initial_virtual_token_reserves   u64       verified (1073000000000000)
    81      initial_virtual_sol_reserves     u64       verified (30000000000)
    89      initial_real_token_reserves      u64       verified (793100000000000)
    97      token_total_supply               u64       verified (1000000000000000)
    105     fee_basis_points                 u64       verified (95)
    113     withdraw_authority               pubkey    TO VERIFY (newer program versions)
    145     enable_migrate                   u8        TO VERIFY
    146     pool_migration_fee               u64       TO VERIFY
    154     creator_fee_basis_points         u64       TO VERIFY
    162     fee_recipients                   7 pubkeys TO VERIFY
    386     set_creator_authority            pubkey    TO VERIFY
    418     admin_set_creator_authority      pubkey    TO VERIFY
    450     (anything further)               raw       reported as extra bytes

Everything past offset 113 is decoded tolerantly and reported as to-verify; a basis-point field
above 10,000 marks the decode suspect. Nothing here ever raises on odd data.
"""
from __future__ import annotations

import base64
import hashlib
import struct
from dataclasses import dataclass, field
from typing import Any

from .base58 import b58encode

GLOBAL_ADDRESS = "4wTV1YmiEkRvAtNtsSGPtUrqRYQMe5SKy2uB4Jjaxnjf"
ACCOUNT_DISCRIMINATOR = hashlib.sha256(b"account:Global").digest()[:8]

# (field, type); types: u8 | u64 | pubkey | pubkey7
VERIFIED_LAYOUT: list[tuple[str, str]] = [
    ("initialized", "u8"), ("authority", "pubkey"), ("fee_recipient", "pubkey"),
    ("initial_virtual_token_reserves", "u64"), ("initial_virtual_sol_reserves", "u64"),
    ("initial_real_token_reserves", "u64"), ("token_total_supply", "u64"), ("fee_basis_points", "u64"),
]
TO_VERIFY_LAYOUT: list[tuple[str, str]] = [
    ("withdraw_authority", "pubkey"), ("enable_migrate", "u8"), ("pool_migration_fee", "u64"),
    ("creator_fee_basis_points", "u64"), ("fee_recipients", "pubkey7"), ("set_creator_authority", "pubkey"),
    ("admin_set_creator_authority", "pubkey"),
]
VERIFIED_END_OFFSET = 113

# config key -> account field
CONFIG_MAP = {
    "initial_virtual_token_reserves": "initial_virtual_token_reserves",
    "initial_virtual_sol_reserves": "initial_virtual_sol_reserves",
    "initial_real_token_reserves": "initial_real_token_reserves",
    "token_total_supply": "token_total_supply",
    "fee_bps": "fee_basis_points",
    "creator_fee_bps": "creator_fee_basis_points",
}


@dataclass
class GlobalAccount:
    verified: dict[str, Any] = field(default_factory=dict)
    to_verify: dict[str, Any] = field(default_factory=dict)
    discriminator_ok: bool = False
    raw_len: int = 0
    consumed: int = 0
    suspect: list[str] = field(default_factory=list)
    error: str | None = None

    @property
    def extra_bytes(self) -> int:
        return max(0, self.raw_len - self.consumed)

    @property
    def fields(self) -> dict[str, Any]:
        out = dict(self.verified)
        out.update(self.to_verify)
        return out

    def to_json(self) -> dict[str, Any]:
        return {
            "discriminator_ok": self.discriminator_ok, "raw_len": self.raw_len, "consumed": self.consumed,
            "extra_bytes": self.extra_bytes, "verified": self.verified, "to_verify": self.to_verify,
            "suspect": self.suspect, "error": self.error,
        }


def _read(data: bytes, pos: int, ftype: str) -> tuple[Any, int]:
    if ftype == "u8":
        if pos + 1 > len(data):
            raise EOFError
        return data[pos], pos + 1
    if ftype == "u64":
        if pos + 8 > len(data):
            raise EOFError
        return struct.unpack_from("<Q", data, pos)[0], pos + 8
    if ftype == "pubkey":
        if pos + 32 > len(data):
            raise EOFError
        return b58encode(data[pos:pos + 32]), pos + 32
    if ftype == "pubkey7":
        if pos + 224 > len(data):
            raise EOFError
        return [b58encode(data[pos + 32 * i:pos + 32 * (i + 1)]) for i in range(7)], pos + 224
    raise ValueError(ftype)


def decode_global(data: bytes) -> GlobalAccount:
    g = GlobalAccount(raw_len=len(data))
    if len(data) < 8:
        g.error = "too_short"
        return g
    g.discriminator_ok = bytes(data[:8]) == ACCOUNT_DISCRIMINATOR
    pos = 8
    try:
        for name, ftype in VERIFIED_LAYOUT:
            g.verified[name], pos = _read(data, pos, ftype)
    except EOFError:
        g.error = f"truncated in verified layout at {pos}"
        g.consumed = pos
        return g
    g.consumed = pos
    for name, ftype in TO_VERIFY_LAYOUT:
        try:
            g.to_verify[name], pos = _read(data, pos, ftype)
            g.consumed = pos
        except EOFError:
            break
    for name, val in g.fields.items():
        if name.endswith("basis_points") and isinstance(val, int) and val > 10_000:
            g.suspect.append(f"{name}={val} > 10000")
    if g.verified.get("initialized") not in (0, 1):
        g.suspect.append(f"initialized={g.verified.get('initialized')}")
    return g


def compare_with_config(g: GlobalAccount, cfg: dict[str, Any]) -> list[dict[str, Any]]:
    """Mismatches between the chain and config/pumpfun.json. Each entry says whether the field is
    in the verified part of the layout; to-verify fields that are absent are not mismatches."""
    out: list[dict[str, Any]] = []
    for cfg_key, acc_field in CONFIG_MAP.items():
        if cfg_key not in cfg:
            continue
        verified = acc_field in g.verified
        if acc_field in g.verified:
            chain_val = g.verified[acc_field]
        elif acc_field in g.to_verify:
            chain_val = g.to_verify[acc_field]
        else:
            continue
        if int(chain_val) != int(cfg[cfg_key]):
            out.append({"field": acc_field, "config_key": cfg_key, "chain": int(chain_val), "config": int(cfg[cfg_key]), "verified_layout": verified})
    total = cfg.get("total_curve_fee_bps")
    fee = g.verified.get("fee_basis_points")
    creator = g.to_verify.get("creator_fee_basis_points")
    if total is not None and fee is not None and creator is not None and int(fee) + int(creator) != int(total):
        out.append({"field": "fee_basis_points+creator_fee_basis_points", "config_key": "total_curve_fee_bps",
                    "chain": int(fee) + int(creator), "config": int(total), "verified_layout": False})
    return out


def account_data_from_value(value: Any) -> tuple[bytes | None, dict[str, Any]]:
    """getAccountInfo value -> (raw bytes, info). Tolerates every odd shape."""
    info: dict[str, Any] = {}
    if not isinstance(value, dict):
        return None, {"error": "no account"}
    info["owner"] = value.get("owner")
    info["lamports"] = value.get("lamports")
    data = value.get("data")
    try:
        if isinstance(data, list) and data and isinstance(data[0], str):
            return base64.b64decode(data[0], validate=True), info
        if isinstance(data, str):
            return base64.b64decode(data, validate=True), info
    except (ValueError, TypeError) as e:
        info["error"] = f"bad base64: {e}"
        return None, info
    info["error"] = "unexpected data encoding"
    return None, info


def fetch_and_check(rpc, pumpfun_cfg: dict[str, Any]) -> dict[str, Any]:
    """One RPC call (budget-gated), decode, compare. Never raises: errors land in the result.
    status: 'ok' | 'mismatch' | 'unavailable' | 'undecodable'."""
    from ..chain.rpc import RpcError
    from ..net.budget import Blocked
    from ..net.http import HttpError

    address = pumpfun_cfg.get("global_account", GLOBAL_ADDRESS)
    res: dict[str, Any] = {"address": address, "status": "unavailable", "mismatches": [], "decoded": None}
    try:
        value = rpc.get_account_info_base64(address)
    except (Blocked, HttpError, RpcError) as e:
        res["error"] = f"{type(e).__name__}: {str(e)[:200]}"
        return res
    raw, info = account_data_from_value(value)
    res["owner"] = info.get("owner")
    if raw is None:
        res["status"] = "undecodable"
        res["error"] = info.get("error", "no data")
        return res
    g = decode_global(raw)
    res["decoded"] = g.to_json()
    if g.error or not g.verified:
        res["status"] = "undecodable"
        res["error"] = g.error or "no fields"
        return res
    if info.get("owner") and pumpfun_cfg.get("program_id") and info["owner"] != pumpfun_cfg["program_id"]:
        res["mismatches"].append({"field": "owner", "config_key": "program_id", "chain": info["owner"], "config": pumpfun_cfg["program_id"], "verified_layout": True})
    if not g.discriminator_ok:
        res["mismatches"].append({"field": "discriminator", "config_key": None, "chain": raw[:8].hex(), "config": ACCOUNT_DISCRIMINATOR.hex(), "verified_layout": True})
    res["mismatches"].extend(compare_with_config(g, pumpfun_cfg))
    res["status"] = "mismatch" if res["mismatches"] else "ok"
    return res
