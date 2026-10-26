"""Tolerant decoding of pump.fun Anchor events.

Events arrive as `Program data: <base64>` log lines (logsSubscribe / getTransaction logMessages)
or as self-CPI inner-instruction payloads prefixed with the 8-byte anchor event-CPI
discriminator. Payload = 8-byte event discriminator (sha256('event:<Name>')[:8]) + Borsh fields.

Layouts below are from the published pump.fun IDL history and are to be VERIFIED against real
transactions (docs/PUMPFUN-EVENTS.md, docs/UNVERIFIED.md). Decoding never raises on drift:
fields are parsed in order until bytes run out; a payload with fewer than the required fields
is returned as Undecoded with a reason, and plausibility checks mark suspect layouts.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import struct
from dataclasses import dataclass, field
from typing import Any

from .base58 import b58encode

PROGRAM_DATA_PREFIX = "Program data: "
# Anchor's EVENT_IX_TAG (u64 0x1d9acb512ea545e4 = sha256("anchor:event")[:8] read big-endian),
# serialized little-endian on the wire -> these bytes prefix every self-CPI event payload.
ANCHOR_EVENT_CPI_DISC = bytes.fromhex("e445a52e51cb9a1d")

MAX_STRING_LEN = 2048
TS_MIN = 1_577_836_800  # 2020-01-01
TS_MAX = 4_102_444_800  # 2100-01-01

# (field name, type). Types: string | pubkey | u64 | i64 | bool
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
}

# Known pump.fun events we recognise by name but do not need; counted, payload not stored.
IGNORED_EVENT_NAMES = (
    "SetParamsEvent", "CollectCreatorFeeEvent", "ExtendAccountEvent", "UpdateGlobalAuthorityEvent",
    "SetCreatorEvent", "SetMetaplexCreatorEvent", "ClaimTokenIncentivesEvent", "InitUserVolumeAccumulatorEvent",
    "SyncUserVolumeAccumulatorEvent", "AdminSetCreatorEvent", "AdminUpdateTokenIncentivesEvent",
    "CloseUserVolumeAccumulatorEvent", "ReservesSyncedEvent",
)


def event_discriminator(name: str) -> bytes:
    return hashlib.sha256(f"event:{name}".encode("ascii")).digest()[:8]


DISCRIMINATORS: dict[bytes, str] = {event_discriminator(n): n for n in list(LAYOUTS) + list(IGNORED_EVENT_NAMES)}


@dataclass
class DecodedEvent:
    name: str
    fields: dict[str, Any]
    parsed_fields: int
    total_fields: int
    consumed: int
    size: int
    suspect: list[str] = field(default_factory=list)

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


def decode_event_bytes(data: bytes) -> DecodedEvent | Undecoded:
    """Decode one event payload (discriminator + fields). Never raises."""
    if data[:8] == ANCHOR_EVENT_CPI_DISC:
        data = data[8:]
    if len(data) < 8:
        return Undecoded(data.hex(), base64.b64encode(data).decode(), "too_short")
    disc = bytes(data[:8])
    name = DISCRIMINATORS.get(disc)
    if name is None:
        return Undecoded(disc.hex(), base64.b64encode(data).decode(), "unknown_discriminator")
    if name not in LAYOUTS:
        return Undecoded(disc.hex(), "", "ignored_event", name=name)
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
        return Undecoded(disc.hex(), base64.b64encode(data).decode(), reason, name=name)
    ev = DecodedEvent(name, out, parsed, len(fields_spec), r.pos, len(data))
    if err:
        ev.suspect.append(f"stopped at {err}")
    ts = out.get("timestamp")
    if isinstance(ts, int) and not (TS_MIN <= ts <= TS_MAX):
        ev.suspect.append(f"timestamp {ts} implausible")
    return ev


def extract_program_data(logs: list[str] | None) -> list[bytes]:
    """All `Program data:` payloads in a log list, base64-decoded. Garbage is skipped silently here;
    callers see it as fewer payloads (the raw logs are kept by the caller when needed)."""
    out: list[bytes] = []
    for line in logs or []:
        if not isinstance(line, str) or not line.startswith(PROGRAM_DATA_PREFIX):
            continue
        b64 = line[len(PROGRAM_DATA_PREFIX):].strip()
        try:
            out.append(base64.b64decode(b64, validate=True))
        except (binascii.Error, ValueError):
            out.append(b"")  # recorded as too_short -> counted, never crashes
    return out


def decode_logs(logs: list[str] | None) -> tuple[list[DecodedEvent], list[Undecoded]]:
    events: list[DecodedEvent] = []
    undecoded: list[Undecoded] = []
    for payload in extract_program_data(logs):
        res = decode_event_bytes(payload)
        (events if isinstance(res, DecodedEvent) else undecoded).append(res)
    return events, undecoded


def summarize_undecoded(items: list[Undecoded]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for u in items:
        key = u.reason.split(":")[0]
        counts[key] = counts.get(key, 0) + 1
    return counts
