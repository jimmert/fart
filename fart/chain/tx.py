"""Pull pump.fun events and chain metadata out of a getTransaction (jsonParsed) response.

Events are collected from meta.logMessages and from self-CPI inner instructions whose data is
the anchor event-CPI payload. Duplicates (same event seen both ways) are collapsed.

Nothing here depends on the transaction version: only `meta` (logMessages, innerInstructions, err,
fee), the first signature, `slot` and `blockTime` are read, and a jsonParsed response carries them
the same way for legacy, version-0 and version-1 transactions (mainnet served version 1 on 31 Oct
2026; the client asks for it, see chain/rpc.py). Accounts loaded through lookup tables arrive in
`meta.loadedAddresses`, which is not needed. The node's `version` field is kept on TxEvents.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .base58 import b58decode
from .events import ANCHOR_EVENT_CPI_DISC, DecodedEvent, Undecoded, decode_event_bytes, decode_logs


@dataclass
class TxEvents:
    signature: str | None
    slot: int | None
    block_time: int | None
    err: Any
    events: list[DecodedEvent] = field(default_factory=list)
    undecoded: list[Undecoded] = field(default_factory=list)
    fee_lamports: int | None = None
    version: Any = None  # what the node reports: "legacy", 0 or 1 (absent on older responses and fixtures)

    def first(self, name: str) -> DecodedEvent | None:
        for e in self.events:
            if e.name == name:
                return e
        return None

    def all(self, name: str) -> list[DecodedEvent]:
        return [e for e in self.events if e.name == name]


def _event_key(e: DecodedEvent) -> tuple:
    f = e.fields
    return (e.name, f.get("mint"), f.get("user"), f.get("sol_amount"), f.get("token_amount"), f.get("timestamp"))


def events_from_transaction(tx: dict[str, Any] | None) -> TxEvents:
    """Never raises on odd shapes; missing pieces become None/empty."""
    if not isinstance(tx, dict):
        return TxEvents(None, None, None, "no_transaction")
    meta = tx.get("meta") or {}
    txn = tx.get("transaction") or {}
    sigs = txn.get("signatures") or []
    out = TxEvents(
        signature=sigs[0] if sigs else None,
        slot=tx.get("slot"),
        block_time=tx.get("blockTime"),
        err=meta.get("err"),
        fee_lamports=meta.get("fee"),
        version=tx.get("version"),
    )
    events, undecoded = decode_logs(meta.get("logMessages"))
    seen = {_event_key(e) for e in events}
    out.events.extend(events)
    out.undecoded.extend(undecoded)
    for inner in meta.get("innerInstructions") or []:
        for ix in inner.get("instructions") or []:
            data = ix.get("data")
            if not isinstance(data, str):
                continue
            try:
                raw = b58decode(data)
            except ValueError:
                continue
            if raw[:8] != ANCHOR_EVENT_CPI_DISC:
                continue
            res = decode_event_bytes(raw)
            if isinstance(res, DecodedEvent):
                k = _event_key(res)
                if k not in seen:
                    seen.add(k)
                    out.events.append(res)
            elif res.reason != "ignored_event":
                out.undecoded.append(res)
    return out


def create_from_tx(tx_events: TxEvents) -> dict[str, Any] | None:
    """Normalized create record (or None): mint, name, symbol, uri, creator, bonding_curve,
    dev_buy_sol (from a TradeEvent in the same tx), slot, block_time."""
    ce = tx_events.first("CreateEvent")
    if ce is None or not ce.fields.get("mint"):
        return None
    f = ce.fields
    dev_buy_sol = None
    initial_vsol = initial_vtok = None
    for te in tx_events.all("TradeEvent"):
        if te.fields.get("mint") == f.get("mint") and te.fields.get("is_buy"):
            dev_buy_sol = (dev_buy_sol or 0.0) + te.fields.get("sol_amount", 0) / 1e9
            initial_vsol = te.fields.get("virtual_sol_reserves")
            initial_vtok = te.fields.get("virtual_token_reserves")
    block_time = tx_events.block_time
    if block_time is None and isinstance(f.get("timestamp"), int) and not ce.suspect:
        block_time = f["timestamp"]
    return {
        "mint": f.get("mint"),
        "name": f.get("name"),
        "symbol": f.get("symbol"),
        "uri": f.get("uri"),
        "creator": f.get("creator") or f.get("user"),
        "user": f.get("user"),
        "bonding_curve": f.get("bonding_curve"),
        "signature": tx_events.signature,
        "slot": tx_events.slot,
        "block_time": block_time,
        "dev_buy_sol": dev_buy_sol,
        "virtual_sol_after": initial_vsol,
        "virtual_token_after": initial_vtok,
        "layout": ce.layout,
    }


def migration_from_tx(tx_events: TxEvents) -> dict[str, Any] | None:
    me = tx_events.first("CompletePumpAmmMigrationEvent")
    if me is None or not me.fields.get("mint"):
        return None
    f = me.fields
    block_time = tx_events.block_time
    if block_time is None and isinstance(f.get("timestamp"), int) and not me.suspect:
        block_time = f["timestamp"]
    return {
        "mint": f.get("mint"),
        "pool_address": f.get("pool"),
        "dex": "pump-amm",
        "signature": tx_events.signature,
        "slot": tx_events.slot,
        "block_time": block_time,
        "layout": me.layout,
    }
