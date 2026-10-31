"""pump.fun BondingCurve account: decode the per-coin curve state the follow step polls (milestone 13).

Layout (Anchor account, 8-byte discriminator sha256("account:BondingCurve")[:8] = 17 b7 f8 37 60 d8 ac 60 first), read
on 31 Oct 2026 from the official IDL (github.com/pump-fun/pump-public-docs, idl/pump.json, account `BondingCurve`; the
IDL's own `discriminator` list is the same eight bytes) and checked against a live account and a completed account fetched
through the public RPC (tests/fixtures/bonding_curve_accounts.json, tests/test_m13_follow.py):

    offset  field                              type     note
    0       discriminator                      8 bytes
    8       virtual_token_reserves             u64      the TradeEvent's virtual_token_reserves after the last trade
    16      virtual_quote_reserves             u64      the TradeEvent's virtual_sol_reserves (SOL-quoted coins; see quote_mint)
    24      real_token_reserves                u64
    32      real_quote_reserves                u64      the TradeEvent's real_sol_reserves
    40      token_total_supply                 u64
    48      complete                           bool     true once the curve graduated (the migration may still be pending)
    49      creator                            pubkey
    81      is_mayhem_mode                     bool
    82      is_cashback_coin                   bool
    83      quote_mint                         pubkey   the zero pubkey (default = SOL) on the live accounts of 31 Oct 2026; WSOL is SOL too; another mint = not priced in SOL
    115     creator_fee_bps                    u64
    123     can_edit_creator_fee               bool
    124     is_holder_reward                   bool
    125     creator_fee                        u64
    133     protocol_fees                      u64
    141     depth                              u8
    142     initial_virtual_quote_reserves     u64
    150     post_complete_base_out             u64
    158     post_complete_quote_in             u64
    166     (anything further)                 raw      reported as extra bytes

The IDL names the SOL side "quote" (newer program versions allow other quote mints); the radar stores the quote side as
`virtual_sol` / `real_sol` only when the quote mint is the zero pubkey or WSOL, or the account is too short to carry one (an
older layout, which was SOL-only). Everything is decoded tolerantly in order until the bytes run out: an account shorter than the six fields
through `complete` is undecodable, a wrong discriminator is reported, nothing here ever raises on odd data.
"""
from __future__ import annotations

import hashlib
import struct
from dataclasses import dataclass, field
from typing import Any

from .base58 import b58encode
from .curve import CurveState

ACCOUNT_DISCRIMINATOR = hashlib.sha256(b"account:BondingCurve").digest()[:8]
WSOL_MINT = "So11111111111111111111111111111111111111112"
ZERO_PUBKEY = "11111111111111111111111111111111"   # 32 zero bytes: the quote_mint of every SOL-quoted curve read on 31 Oct 2026 (the default, not WSOL)
SOL_QUOTE_MINTS = (WSOL_MINT, ZERO_PUBKEY)

# (field, type) in the IDL's order; types: u8 | u64 | bool | pubkey
LAYOUT: list[tuple[str, str]] = [
    ("virtual_token_reserves", "u64"), ("virtual_quote_reserves", "u64"), ("real_token_reserves", "u64"), ("real_quote_reserves", "u64"),
    ("token_total_supply", "u64"), ("complete", "bool"),
    ("creator", "pubkey"), ("is_mayhem_mode", "bool"), ("is_cashback_coin", "bool"), ("quote_mint", "pubkey"), ("creator_fee_bps", "u64"),
    ("can_edit_creator_fee", "bool"), ("is_holder_reward", "bool"), ("creator_fee", "u64"), ("protocol_fees", "u64"), ("depth", "u8"),
    ("initial_virtual_quote_reserves", "u64"), ("post_complete_base_out", "u64"), ("post_complete_quote_in", "u64"),
]
REQUIRED_FIELDS = 6          # through `complete`: the part every program version carried
MINIMUM_LEN = 8 + 5 * 8 + 1  # 49 bytes


@dataclass
class BondingCurveAccount:
    fields: dict[str, Any] = field(default_factory=dict)
    discriminator_ok: bool = False
    raw_len: int = 0
    consumed: int = 0
    suspect: list[str] = field(default_factory=list)
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None and self.discriminator_ok and len(self.fields) >= REQUIRED_FIELDS

    @property
    def extra_bytes(self) -> int:
        return max(0, self.raw_len - self.consumed)

    @property
    def quote_is_sol(self) -> bool:
        """True when the account prices the coin in SOL: the quote mint is the zero pubkey (the default the live accounts carry,
        tests/fixtures/bonding_curve_accounts.json) or WSOL, or the layout predates quote mints. Another mint means another
        quote currency and the reserves are not SOL."""
        q = self.fields.get("quote_mint")
        return q is None or q in SOL_QUOTE_MINTS

    @property
    def complete(self) -> bool | None:
        v = self.fields.get("complete")
        return None if v is None else bool(v)

    @property
    def virtual_sol(self) -> int | None:
        return self.fields.get("virtual_quote_reserves") if self.quote_is_sol else None

    @property
    def virtual_token(self) -> int | None:
        return self.fields.get("virtual_token_reserves")

    @property
    def real_sol(self) -> int | None:
        return self.fields.get("real_quote_reserves") if self.quote_is_sol else None

    @property
    def real_token(self) -> int | None:
        return self.fields.get("real_token_reserves")

    def state(self) -> CurveState | None:
        """The curve state (virtual and real reserves) when the account is SOL-quoted and decodable, else None."""
        if not self.ok or not self.quote_is_sol or not self.virtual_sol or not self.virtual_token:
            return None
        return CurveState(int(self.virtual_sol), int(self.virtual_token), self.real_sol, self.real_token)

    def to_json(self) -> dict[str, Any]:
        return {"discriminator_ok": self.discriminator_ok, "raw_len": self.raw_len, "consumed": self.consumed, "extra_bytes": self.extra_bytes,
                "fields": dict(self.fields), "suspect": list(self.suspect), "error": self.error, "quote_is_sol": self.quote_is_sol}


def _read(data: bytes, pos: int, ftype: str) -> tuple[Any, int]:
    if ftype == "u8":
        if pos + 1 > len(data):
            raise EOFError
        return data[pos], pos + 1
    if ftype == "bool":
        if pos + 1 > len(data):
            raise EOFError
        v = data[pos]
        if v > 1:
            raise ValueError(f"bool byte {v}")
        return v == 1, pos + 1
    if ftype == "u64":
        if pos + 8 > len(data):
            raise EOFError
        return struct.unpack_from("<Q", data, pos)[0], pos + 8
    if ftype == "pubkey":
        if pos + 32 > len(data):
            raise EOFError
        return b58encode(data[pos:pos + 32]), pos + 32
    raise ValueError(ftype)


def decode_bonding_curve(data: bytes) -> BondingCurveAccount:
    """Decode the raw account bytes. Never raises: `error` says why a decode is unusable, `suspect` what looks odd."""
    a = BondingCurveAccount(raw_len=len(data))
    if len(data) < 8:
        a.error = "too_short"
        return a
    a.discriminator_ok = bytes(data[:8]) == ACCOUNT_DISCRIMINATOR
    if not a.discriminator_ok:
        a.error = f"wrong discriminator {bytes(data[:8]).hex()}"
        return a
    pos = 8
    for name, ftype in LAYOUT:
        try:
            a.fields[name], pos = _read(data, pos, ftype)
            a.consumed = pos
        except EOFError:
            break
        except ValueError as e:
            a.suspect.append(f"{name}: {e}")
            break
    if len(a.fields) < REQUIRED_FIELDS:
        a.error = f"truncated: {len(a.fields)} of the {REQUIRED_FIELDS} required fields in {len(data)} bytes"
        return a
    for name in ("virtual_token_reserves", "virtual_quote_reserves"):
        if int(a.fields.get(name) or 0) <= 0 and not a.fields.get("complete"):
            a.suspect.append(f"{name}={a.fields.get(name)} on an open curve")
    bps = a.fields.get("creator_fee_bps")
    if isinstance(bps, int) and bps > 10_000:
        a.suspect.append(f"creator_fee_bps={bps} > 10000")
    return a


def decode_account_value(value: Any) -> tuple[BondingCurveAccount | None, dict[str, Any]]:
    """A `getMultipleAccounts` / `getAccountInfo` value -> (decoded account or None, info with owner, lamports, error)."""
    from .global_account import account_data_from_value

    raw, info = account_data_from_value(value)
    if raw is None:
        return None, info
    return decode_bonding_curve(raw), info


__all__ = ["ACCOUNT_DISCRIMINATOR", "LAYOUT", "BondingCurveAccount", "decode_bonding_curve", "decode_account_value", "WSOL_MINT", "ZERO_PUBKEY", "SOL_QUOTE_MINTS"]
