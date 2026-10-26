"""Tolerant decoding of pump.fun and pump-amm (PumpSwap) Anchor events.

Events arrive as `Program data: <base64>` log lines (logsSubscribe / getTransaction logMessages)
or as self-CPI inner-instruction payloads prefixed with the 8-byte anchor event-CPI
discriminator. Payload = 8-byte event discriminator (sha256('event:<Name>')[:8]) + Borsh fields.

The pump.fun layouts come from the published IDL history; CreateEvent / TradeEvent /
CompletePumpAmmMigrationEvent decode on mainnet (VPS, 26 Oct 2026). The pump-amm layouts
(BuyEvent, SellEvent, CreatePoolEvent) are from the PumpSwap IDL as remembered and are TO VERIFY
(docs/PUMPFUN-EVENTS.md): decoding never raises on drift, fields are parsed in order until bytes run
out, a payload with fewer than the required fields is returned as Undecoded with a reason, and
plausibility checks (timestamps, basis points) mark suspect layouts.

`attribute_program_data()` walks the `Program X invoke [n]` / `Program X success` nesting so every
payload is attributed to the program that emitted it: unknown discriminators from Jupiter, Orca or
Raydium in the same transaction are then counted per program instead of being mistaken for new
pump.fun events.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import re
import struct
from dataclasses import dataclass, field
from typing import Any

from .base58 import b58encode

PROGRAM_DATA_PREFIX = "Program data: "
# Anchor's EVENT_IX_TAG (u64 0x1d9acb512ea545e4 = sha256("anchor:event")[:8] read big-endian),
# serialized little-endian on the wire -> these bytes prefix every self-CPI event payload.
ANCHOR_EVENT_CPI_DISC = bytes.fromhex("e445a52e51cb9a1d")

PUMP_PROGRAM = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
PUMP_AMM_PROGRAM = "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA"

MAX_STRING_LEN = 2048
TS_MIN = 1_577_836_800  # 2020-01-01
TS_MAX = 4_102_444_800  # 2100-01-01

# (field name, type). Types: string | pubkey | u64 | i64 | bool | u8 | u16
LAYOUTS: dict[str, tuple[list[tuple[str, str]], int]] = {
    "CreateEvent": (
        [
            ("name", "string"), ("symbol", "string"), ("uri", "string"),
            ("mint", "pubkey"), ("bonding_curve", "pubkey"), ("user", "pubkey"),
            # added in later program versions
            ("creator", "pubkey"), ("timestamp", "i64"),
            ("virtual_token_reserves", "u64"), ("virtual_sol_reserves", "u64"),
            ("real_token_reserves", "u64"), ("token_total_supply", "u64"),
        ],
        6,
    ),
    "TradeEvent": (
        [
            ("mint", "pubkey"), ("sol_amount", "u64"), ("token_amount", "u64"), ("is_buy", "bool"),
            ("user", "pubkey"), ("timestamp", "i64"),
            ("virtual_sol_reserves", "u64"), ("virtual_token_reserves", "u64"),
            ("real_sol_reserves", "u64"), ("real_token_reserves", "u64"),
            # added in later program versions
            ("fee_recipient", "pubkey"), ("fee_basis_points", "u64"), ("fee", "u64"),
            ("creator", "pubkey"), ("creator_fee_basis_points", "u64"), ("creator_fee", "u64"),
            ("track_volume", "bool"), ("total_unclaimed_tokens", "u64"), ("total_claimed_tokens", "u64"),
            ("current_sol_volume", "u64"), ("last_update_timestamp", "i64"),
        ],
        10,
    ),
    "CompletePumpAmmMigrationEvent": (
        [
            ("user", "pubkey"), ("mint", "pubkey"), ("mint_amount", "u64"), ("sol_amount", "u64"),
            ("pool_migration_fee", "u64"), ("bonding_curve", "pubkey"), ("timestamp", "i64"), ("pool", "pubkey"),
        ],
        2,
    ),
    "CompleteEvent": (
        [("user", "pubkey"), ("mint", "pubkey"), ("bonding_curve", "pubkey"), ("timestamp", "i64")],
        2,
    ),
    # ----- pump-amm (PumpSwap) events: TO VERIFY -------------------------------------------------
    "BuyEvent": (
        [
            ("timestamp", "i64"), ("base_amount_out", "u64"), ("max_quote_amount_in", "u64"),
            ("user_base_token_reserves", "u64"), ("user_quote_token_reserves", "u64"),
            ("pool_base_token_reserves", "u64"), ("pool_quote_token_reserves", "u64"),
            ("quote_amount_in", "u64"), ("lp_fee_basis_points", "u64"), ("lp_fee", "u64"),
            ("protocol_fee_basis_points", "u64"), ("protocol_fee", "u64"),
            ("quote_amount_in_with_lp_fee", "u64"), ("user_quote_amount_in", "u64"),
            ("pool", "pubkey"), ("user", "pubkey"), ("user_base_token_account", "pubkey"),
            ("user_quote_token_account", "pubkey"), ("protocol_fee_recipient", "pubkey"),
            ("protocol_fee_recipient_token_account", "pubkey"),
            ("coin_creator", "pubkey"), ("coin_creator_fee_basis_points", "u64"), ("coin_creator_fee", "u64"),
            ("track_volume", "bool"), ("total_unclaimed_tokens", "u64"), ("total_claimed_tokens", "u64"),
            ("current_sol_volume", "u64"), ("last_update_timestamp", "i64"),
        ],
        16,
    ),
    "SellEvent": (
        [
            ("timestamp", "i64"), ("base_amount_in", "u64"), ("min_quote_amount_out", "u64"),
            ("user_base_token_reserves", "u64"), ("user_quote_token_reserves", "u64"),
            ("pool_base_token_reserves", "u64"), ("pool_quote_token_reserves", "u64"),
            ("quote_amount_out", "u64"), ("lp_fee_basis_points", "u64"), ("lp_fee", "u64"),
            ("protocol_fee_basis_points", "u64"), ("protocol_fee", "u64"),
            ("quote_amount_out_without_lp_fee", "u64"), ("user_quote_amount_out", "u64"),
            ("pool", "pubkey"), ("user", "pubkey"), ("user_base_token_account", "pubkey"),
            ("user_quote_token_account", "pubkey"), ("protocol_fee_recipient", "pubkey"),
            ("protocol_fee_recipient_token_account", "pubkey"),
            ("coin_creator", "pubkey"), ("coin_creator_fee_basis_points", "u64"), ("coin_creator_fee", "u64"),
        ],
        16,
    ),
    "CreatePoolEvent": (
        [
            ("timestamp", "i64"), ("index", "u16"), ("creator", "pubkey"), ("base_mint", "pubkey"), ("quote_mint", "pubkey"),
            ("base_mint_decimals", "u8"), ("quote_mint_decimals", "u8"), ("base_amount_in", "u64"), ("quote_amount_in", "u64"),
            ("pool_base_amount", "u64"), ("pool_quote_amount", "u64"), ("minimum_liquidity", "u64"), ("initial_liquidity", "u64"),
            ("lp_token_amount_out", "u64"), ("pool_bump", "u8"), ("pool", "pubkey"), ("lp_mint", "pubkey"),
            ("user_base_token_account", "pubkey"), ("user_quote_token_account", "pubkey"), ("coin_creator", "pubkey"),
        ],
        5,
    ),
}

# Known events we recognise by name but do not need; counted, payload not stored.
IGNORED_EVENT_NAMES = (
    # pump.fun
    "SetParamsEvent", "CollectCreatorFeeEvent", "ExtendAccountEvent", "UpdateGlobalAuthorityEvent",
    "SetCreatorEvent", "SetMetaplexCreatorEvent", "ClaimTokenIncentivesEvent", "InitUserVolumeAccumulatorEvent",
    "SyncUserVolumeAccumulatorEvent", "AdminSetCreatorEvent", "AdminUpdateTokenIncentivesEvent",
    "CloseUserVolumeAccumulatorEvent", "ReservesSyncedEvent", "AdminSetIdlAuthorityEvent", "ReserveEvent",
    "CreateEventV2", "TradeEventV2",
    # pump-amm
    "DepositEvent", "WithdrawEvent", "DisableEvent", "UpdateAdminEvent", "UpdateFeeConfigEvent",
    "CollectCoinCreatorFeeEvent", "SetCoinCreatorEvent", "AdminSetCoinCreatorEvent", "CreateConfigEvent",
    "UpdateConfigEvent", "BuyExactQuoteInEvent", "SellExactQuoteOutEvent",
)

# name -> programs whose IDL carries an event of that name. Names shared by pump.fun and
# pump-amm have the same discriminator (sha256 of the name only), so the program is ambiguous.
EVENT_PROGRAMS: dict[str, tuple[str, ...]] = {
    "CreateEvent": ("pump",), "TradeEvent": ("pump",), "CompleteEvent": ("pump",),
    "CompletePumpAmmMigrationEvent": ("pump",), "SetParamsEvent": ("pump",), "CollectCreatorFeeEvent": ("pump",),
    "UpdateGlobalAuthorityEvent": ("pump",), "SetCreatorEvent": ("pump",), "SetMetaplexCreatorEvent": ("pump",),
    "AdminSetCreatorEvent": ("pump",), "ReservesSyncedEvent": ("pump",), "AdminSetIdlAuthorityEvent": ("pump",),
    "ReserveEvent": ("pump",), "CreateEventV2": ("pump",), "TradeEventV2": ("pump",),
    "ExtendAccountEvent": ("pump", "pump_amm"), "ClaimTokenIncentivesEvent": ("pump", "pump_amm"),
    "InitUserVolumeAccumulatorEvent": ("pump", "pump_amm"), "SyncUserVolumeAccumulatorEvent": ("pump", "pump_amm"),
    "CloseUserVolumeAccumulatorEvent": ("pump", "pump_amm"), "AdminUpdateTokenIncentivesEvent": ("pump", "pump_amm"),
    "BuyEvent": ("pump_amm",), "SellEvent": ("pump_amm",), "CreatePoolEvent": ("pump_amm",), "DepositEvent": ("pump_amm",),
    "WithdrawEvent": ("pump_amm",), "DisableEvent": ("pump_amm",), "UpdateAdminEvent": ("pump_amm",),
    "UpdateFeeConfigEvent": ("pump_amm",), "CollectCoinCreatorFeeEvent": ("pump_amm",), "SetCoinCreatorEvent": ("pump_amm",),
    "AdminSetCoinCreatorEvent": ("pump_amm",), "CreateConfigEvent": ("pump_amm",), "UpdateConfigEvent": ("pump_amm",),
    "BuyExactQuoteInEvent": ("pump_amm",), "SellExactQuoteOutEvent": ("pump_amm",),
}

# Event names of other Anchor programs commonly found in the same transactions (best guesses for
# classification only; never decoded). program label -> names.
OTHER_PROGRAM_EVENT_NAMES: dict[str, tuple[str, ...]] = {
    "jupiter_v6": ("SwapEvent", "FeeEvent"),
    "raydium": ("SwapEvent", "LpChangeEvent", "PoolCreateEvent", "TradeEvent", "ClaimVestedEvent", "CreateVestingEvent",
                "SwapBaseInEvent", "SwapBaseOutEvent", "IncreaseLiquidityEvent", "DecreaseLiquidityEvent",
                "CollectPersonalFeeEvent", "CreatePersonalPositionEvent", "LiquidityChangeEvent", "LiquidityCalculateEvent",
                "ConfigChangeEvent", "CollectProtocolFeeEvent", "MigrateEvent"),
    "orca_whirlpool": ("Traded", "LiquidityIncreased", "LiquidityDecreased", "PoolInitialized"),
    "meteora": ("Swap", "AddLiquidity", "RemoveLiquidity", "PositionCreate", "PositionClose", "ClaimFee", "ClaimReward",
                "LbPairCreate", "CompositionFee", "EvtSwap", "EvtSwap2", "EvtCreatePool", "EvtAddLiquidity", "EvtRemoveLiquidity",
                "EvtClaimPositionFee", "EvtInitializePool", "EvtCurveComplete", "EvtClaimTradingFee", "EvtClaimProtocolFee",
                "EvtCreatorWithdrawSurplus", "EvtCreateDammV2MigrationMetadata", "EvtClaimCreatorTradingFee"),
}


def event_discriminator(name: str) -> bytes:
    return hashlib.sha256(f"event:{name}".encode("ascii")).digest()[:8]


DISCRIMINATORS: dict[bytes, str] = {event_discriminator(n): n for n in list(LAYOUTS) + list(IGNORED_EVENT_NAMES)}

_OTHER_DISCRIMINATORS: dict[bytes, list[tuple[str, str]]] = {}
for _prog, _names in OTHER_PROGRAM_EVENT_NAMES.items():
    for _n in _names:
        _OTHER_DISCRIMINATORS.setdefault(event_discriminator(_n), []).append((_prog, _n))


def classify_discriminator(disc_hex: str) -> dict[str, Any]:
    """Best-guess name(s) for an 8-byte event discriminator.

    status: 'decoded' (we have a layout), 'ignored' (known pump/pump-amm event we skip),
    'other_program' (name matches an event of another Anchor program; the program is a guess),
    'emit_cpi_tag' (the anchor event-CPI tag itself), 'unknown'.
    """
    try:
        disc = bytes.fromhex(disc_hex)
    except ValueError:
        return {"discriminator": disc_hex, "name": None, "programs": [], "status": "unknown"}
    if disc == ANCHOR_EVENT_CPI_DISC:
        return {"discriminator": disc_hex, "name": "anchor event-CPI tag (next 8 bytes are the event)", "programs": ["anchor"], "status": "emit_cpi_tag"}
    name = DISCRIMINATORS.get(disc)
    if name is not None:
        return {"discriminator": disc_hex, "name": name, "programs": list(EVENT_PROGRAMS.get(name, ())),
                "status": "decoded" if name in LAYOUTS else "ignored"}
    others = _OTHER_DISCRIMINATORS.get(disc)
    if others:
        return {"discriminator": disc_hex, "name": "/".join(sorted({n for _, n in others})),
                "programs": sorted({p for p, _ in others}), "status": "other_program"}
    return {"discriminator": disc_hex, "name": None, "programs": [], "status": "unknown"}


def discriminator_table() -> list[dict[str, Any]]:
    """Every discriminator we know about, for docs and `fart events classify`."""
    rows = []
    for name in list(LAYOUTS) + list(IGNORED_EVENT_NAMES):
        rows.append({"name": name, "discriminator": event_discriminator(name).hex(), "programs": list(EVENT_PROGRAMS.get(name, ())),
                     "status": "layout" if name in LAYOUTS else "ignored"})
    for prog, names in OTHER_PROGRAM_EVENT_NAMES.items():
        for n in names:
            rows.append({"name": n, "discriminator": event_discriminator(n).hex(), "programs": [prog], "status": "other_program"})
    return rows


@dataclass
class DecodedEvent:
    name: str
    fields: dict[str, Any]
    parsed_fields: int
    total_fields: int
    consumed: int
    size: int
    suspect: list[str] = field(default_factory=list)
    program: str | None = None

    @property
    def layout(self) -> str:
        tag = "full" if self.parsed_fields == self.total_fields and self.consumed == self.size else "partial"
        if self.suspect:
            tag = "suspect"
        return f"{self.name}/{tag}/{self.parsed_fields}of{self.total_fields}"

    @property
    def extra_bytes(self) -> int:
        return self.size - self.consumed


@dataclass
class Undecoded:
    discriminator_hex: str
    payload_b64: str
    reason: str
    name: str | None = None
    program: str | None = None


class _Reader:
    def __init__(self, data: bytes, pos: int = 0):
        self.data = data
        self.pos = pos

    def remaining(self) -> int:
        return len(self.data) - self.pos

    def take(self, n: int) -> bytes:
        if n < 0 or self.remaining() < n:
            raise EOFError
        b = self.data[self.pos:self.pos + n]
        self.pos += n
        return b

    def u8(self) -> int:
        return self.take(1)[0]

    def u16(self) -> int:
        return struct.unpack("<H", self.take(2))[0]

    def u64(self) -> int:
        return struct.unpack("<Q", self.take(8))[0]

    def i64(self) -> int:
        return struct.unpack("<q", self.take(8))[0]

    def bool(self) -> bool:
        v = self.take(1)[0]
        if v > 1:
            raise ValueError(f"bool byte {v}")
        return v == 1

    def pubkey(self) -> str:
        return b58encode(self.take(32))

    def string(self) -> str:
        n = struct.unpack("<I", self.take(4))[0]
        if n > MAX_STRING_LEN or n > self.remaining():
            raise ValueError(f"string length {n}")
        return self.take(n).decode("utf-8", errors="replace")


def decode_event_bytes(data: bytes, program: str | None = None) -> DecodedEvent | Undecoded:
    """Decode one event payload (discriminator + fields). Never raises."""
    if data[:8] == ANCHOR_EVENT_CPI_DISC:
        data = data[8:]
    if len(data) < 8:
        return Undecoded(data.hex(), base64.b64encode(data).decode(), "too_short", program=program)
    disc = bytes(data[:8])
    name = DISCRIMINATORS.get(disc)
    if name is None:
        return Undecoded(disc.hex(), base64.b64encode(data).decode(), "unknown_discriminator", program=program)
    if name not in LAYOUTS:
        return Undecoded(disc.hex(), "", "ignored_event", name=name, program=program)
    fields_spec, required = LAYOUTS[name]
    r = _Reader(data, 8)
    out: dict[str, Any] = {}
    parsed = 0
    err: str | None = None
    for fname, ftype in fields_spec:
        if r.remaining() == 0:
            break
        try:
            out[fname] = getattr(r, ftype)()
        except (EOFError, ValueError, struct.error) as e:
            err = f"{fname}: {type(e).__name__} {e}"
            break
        parsed += 1
    if parsed < required:
        reason = f"truncated_or_drift: parsed {parsed}/{required} required" + (f" ({err})" if err else "")
        return Undecoded(disc.hex(), base64.b64encode(data).decode(), reason, name=name, program=program)
    ev = DecodedEvent(name, out, parsed, len(fields_spec), r.pos, len(data), program=program)
    if err:
        ev.suspect.append(f"stopped at {err}")
    ts = out.get("timestamp")
    if isinstance(ts, int) and not (TS_MIN <= ts <= TS_MAX):
        ev.suspect.append(f"timestamp {ts} implausible")
    for fname, val in out.items():
        if fname.endswith("basis_points") and isinstance(val, int) and val > 10_000:
            ev.suspect.append(f"{fname} {val} implausible")
    return ev


_INVOKE = re.compile(r"^Program (\S+) invoke \[(\d+)\]$")
_END = re.compile(r"^Program (\S+) (success|failed.*)$")


def attribute_program_data(logs: list[str] | None) -> list[tuple[str | None, bytes]]:
    """All `Program data:` payloads with the program that emitted them (from the invoke nesting).

    Garbage base64 becomes b'' (recorded as too_short by the decoder, never a crash). A payload
    outside any invoke frame (should not happen) gets program None.
    """
    out: list[tuple[str | None, bytes]] = []
    stack: list[str] = []
    for line in logs or []:
        if not isinstance(line, str):
            continue
        if line.startswith(PROGRAM_DATA_PREFIX):
            b64 = line[len(PROGRAM_DATA_PREFIX):].strip()
            try:
                payload = base64.b64decode(b64, validate=True)
            except (binascii.Error, ValueError):
                payload = b""
            out.append((stack[-1] if stack else None, payload))
            continue
        m = _INVOKE.match(line)
        if m:
            stack.append(m.group(1))
            continue
        m = _END.match(line)
        if m and stack:
            # pop to the matching program (tolerate missing frames)
            prog = m.group(1)
            if prog in stack:
                while stack and stack[-1] != prog:
                    stack.pop()
                if stack:
                    stack.pop()
    return out


def extract_program_data(logs: list[str] | None) -> list[bytes]:
    """All `Program data:` payloads in a log list, base64-decoded (program attribution dropped)."""
    return [payload for _, payload in attribute_program_data(logs)]


def decode_logs(logs: list[str] | None) -> tuple[list[DecodedEvent], list[Undecoded]]:
    events: list[DecodedEvent] = []
    undecoded: list[Undecoded] = []
    for program, payload in attribute_program_data(logs):
        res = decode_event_bytes(payload, program=program)
        (events if isinstance(res, DecodedEvent) else undecoded).append(res)
    return events, undecoded


def summarize_undecoded(items: list[Undecoded]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for u in items:
        key = u.reason.split(":")[0]
        counts[key] = counts.get(key, 0) + 1
    return counts
