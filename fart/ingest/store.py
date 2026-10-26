"""Normalize PumpPortal messages and RPC events into the event-time tables.

Chain time (slot, block_time) and our clock (received_at) are always separate columns. Rows
seen by several sources are merged: details are filled where missing, `sources` is the union.
"""
from __future__ import annotations

import json
import sqlite3
from typing import Any

from ..chain.events import DecodedEvent, Undecoded
from ..util import fnum

RPC_SOURCES = ("rpc_logs", "rpc_sigs", "rpc_backfill", "rpc_tx", "rpc_trades")
TRADE_SOURCES = ("pumpportal", "rpc_logs", "rpc_trades", "pump_amm")


def _sources_union(existing: str | None, new: str) -> str:
    try:
        cur = json.loads(existing) if existing else []
    except json.JSONDecodeError:
        cur = []
    if new not in cur:
        cur.append(new)
    return json.dumps(cur)


def upsert_coin(conn: sqlite3.Connection, rec: dict[str, Any], source: str, received_at: float, raw_ref: str | None = None) -> bool:
    """Insert or merge a create record. Returns True when the mint was new."""
    mint = rec.get("mint")
    if not mint:
        return False
    row = conn.execute("SELECT * FROM coins WHERE mint=?", (mint,)).fetchone()
    if row is None:
        conn.execute(
            "INSERT INTO coins(mint, chain, platform, name, symbol, uri, creator, bonding_curve, signature, slot,"
            " block_time, received_at, dev_buy_sol, initial_mcap_sol, source, sources, raw_ref)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                mint, rec.get("chain", "solana"), rec.get("platform", "pump"), rec.get("name"), rec.get("symbol"),
                rec.get("uri"), rec.get("creator"), rec.get("bonding_curve"), rec.get("signature"), rec.get("slot"),
                rec.get("block_time"), received_at, rec.get("dev_buy_sol"), rec.get("initial_mcap_sol"), source,
                json.dumps([source]), raw_ref,
            ),
        )
        return True
    # merge: keep the earliest received_at, fill blanks, union sources
    conn.execute(
        "UPDATE coins SET name=COALESCE(name,?), symbol=COALESCE(symbol,?), uri=COALESCE(uri,?),"
        " creator=COALESCE(creator,?), bonding_curve=COALESCE(bonding_curve,?), signature=COALESCE(signature,?),"
        " slot=COALESCE(slot,?), block_time=COALESCE(block_time,?), dev_buy_sol=COALESCE(dev_buy_sol,?),"
        " initial_mcap_sol=COALESCE(initial_mcap_sol,?), received_at=MIN(received_at,?), sources=? WHERE mint=?",
        (
            rec.get("name"), rec.get("symbol"), rec.get("uri"), rec.get("creator"), rec.get("bonding_curve"),
            rec.get("signature"), rec.get("slot"), rec.get("block_time"), rec.get("dev_buy_sol"),
            rec.get("initial_mcap_sol"), received_at, _sources_union(row["sources"], source), mint,
        ),
    )
    return False


def upsert_migration(conn: sqlite3.Connection, rec: dict[str, Any], source: str, received_at: float) -> bool:
    mint = rec.get("mint")
    if not mint:
        return False
    row = conn.execute("SELECT * FROM migrations WHERE mint=?", (mint,)).fetchone()
    if row is None:
        conn.execute(
            "INSERT INTO migrations(mint, slot, block_time, received_at, signature, pool_address, dex, source, sources)"
            " VALUES(?,?,?,?,?,?,?,?,?)",
            (mint, rec.get("slot"), rec.get("block_time"), received_at, rec.get("signature"), rec.get("pool_address"),
             rec.get("dex"), source, json.dumps([source])),
        )
        return True
    conn.execute(
        "UPDATE migrations SET slot=COALESCE(slot,?), block_time=COALESCE(block_time,?), signature=COALESCE(signature,?),"
        " pool_address=COALESCE(pool_address,?), dex=COALESCE(dex,?), received_at=MIN(received_at,?), sources=? WHERE mint=?",
        (rec.get("slot"), rec.get("block_time"), rec.get("signature"), rec.get("pool_address"), rec.get("dex"),
         received_at, _sources_union(row["sources"], source), mint),
    )
    return False


def insert_trade(conn: sqlite3.Connection, rec: dict[str, Any], source: str, received_at: float) -> bool:
    """Insert a trade for a watched mint. A duplicate (same signature+mint, e.g. seen by PumpPortal and
    by the RPC stream) is not inserted again, but fills in chain time, reserves and buyer where the
    first row lacked them (PumpPortal rows carry no slot/block_time). Returns True only for a new row."""
    if not rec.get("mint") or not rec.get("signature"):
        return False
    row = conn.execute("SELECT 1 FROM trades WHERE signature=? AND mint=?", (rec["signature"], rec["mint"])).fetchone()
    if row is not None:
        conn.execute(
            "UPDATE trades SET slot=COALESCE(slot,?), block_time=COALESCE(block_time,?), virtual_sol=COALESCE(virtual_sol,?),"
            " virtual_token=COALESCE(virtual_token,?), buyer=COALESCE(buyer,?) WHERE signature=? AND mint=?",
            (rec.get("slot"), rec.get("block_time"), rec.get("virtual_sol"), rec.get("virtual_token"), rec.get("buyer"),
             rec["signature"], rec["mint"]),
        )
        return False
    conn.execute(
        "INSERT INTO trades(mint, slot, block_time, received_at, signature, buyer, is_buy, sol, tokens,"
        " virtual_sol, virtual_token, source) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
        (rec["mint"], rec.get("slot"), rec.get("block_time"), received_at, rec["signature"], rec.get("buyer"),
         1 if rec.get("is_buy") else 0, float(rec.get("sol") or 0.0), float(rec.get("tokens") or 0.0),
         rec.get("virtual_sol"), rec.get("virtual_token"), source),
    )
    conn.execute("UPDATE watchlist SET last_trade_at=MAX(COALESCE(last_trade_at,0),?) WHERE mint=?", (received_at, rec["mint"]))
    return True


# ----- PumpPortal -----------------------------------------------------------------------------
def classify_pumpportal(msg: Any) -> str:
    """'create' | 'trade' | 'migrate' | 'info' | 'unknown'."""
    if not isinstance(msg, dict):
        return "unknown"
    t = msg.get("txType")
    if t == "create":
        return "create"
    if t in ("buy", "sell"):
        return "trade"
    if t == "migrate" or msg.get("pool") == "pump-amm" and "mint" in msg and t is None:
        return "migrate"
    if "message" in msg or "errors" in msg:
        return "info"
    return "unknown"


def pumpportal_create_record(msg: dict[str, Any]) -> dict[str, Any]:
    return {
        "mint": msg.get("mint"),
        "platform": msg.get("pool") or "pump",
        "name": msg.get("name"),
        "symbol": msg.get("symbol"),
        "uri": msg.get("uri"),
        "creator": msg.get("traderPublicKey"),
        "bonding_curve": msg.get("bondingCurveKey"),
        "signature": msg.get("signature"),
        "slot": None,
        "block_time": None,  # PumpPortal carries no chain time; filled by the RPC signature sweep
        "dev_buy_sol": fnum(msg.get("solAmount")),
        "initial_mcap_sol": fnum(msg.get("marketCapSol")),
        "virtual_sol_after": fnum(msg.get("vSolInBondingCurve")),
        "virtual_token_after": fnum(msg.get("vTokensInBondingCurve")),
    }


def pumpportal_trade_record(msg: dict[str, Any]) -> dict[str, Any]:
    vsol = fnum(msg.get("vSolInBondingCurve"))
    vtok = fnum(msg.get("vTokensInBondingCurve"))
    return {
        "mint": msg.get("mint"),
        "signature": msg.get("signature"),
        "buyer": msg.get("traderPublicKey"),
        "is_buy": msg.get("txType") == "buy",
        "sol": fnum(msg.get("solAmount")),
        "tokens": fnum(msg.get("tokenAmount")),
        "virtual_sol": vsol * 1e9 if vsol is not None else None,       # lamports
        "virtual_token": vtok * 1e6 if vtok is not None else None,     # base units
        "slot": None,
        "block_time": None,
        "pool": msg.get("pool"),
    }


def pumpportal_migration_record(msg: dict[str, Any]) -> dict[str, Any]:
    return {
        "mint": msg.get("mint"),
        "signature": msg.get("signature"),
        "pool_address": None,
        "dex": msg.get("pool") or "pump-amm",
        "slot": None,
        "block_time": None,
    }


# ----- RPC log events ---------------------------------------------------------------------------
def records_from_events(events: list[DecodedEvent], signature: str | None, slot: int | None) -> dict[str, Any]:
    """Group decoded events of one transaction into create/trades/migration records.

    block_time comes from the event's own timestamp when present and plausible (chain time),
    else stays None until the signature sweep fills it from getSignaturesForAddress.
    """
    out: dict[str, Any] = {"create": None, "trades": [], "migration": None}
    creates = [e for e in events if e.name == "CreateEvent"]
    trades = [e for e in events if e.name == "TradeEvent"]
    migs = [e for e in events if e.name == "CompletePumpAmmMigrationEvent"]

    def ts_of(e: DecodedEvent) -> int | None:
        ts = e.fields.get("timestamp")
        return ts if isinstance(ts, int) and not any("timestamp" in s for s in e.suspect) else None

    if creates:
        ce = creates[0]
        f = ce.fields
        dev = None
        for te in trades:
            if te.fields.get("mint") == f.get("mint") and te.fields.get("is_buy"):
                dev = (dev or 0.0) + (te.fields.get("sol_amount") or 0) / 1e9
        out["create"] = {
            "mint": f.get("mint"), "name": f.get("name"), "symbol": f.get("symbol"), "uri": f.get("uri"),
            "creator": f.get("creator") or f.get("user"), "bonding_curve": f.get("bonding_curve"),
            "signature": signature, "slot": slot, "block_time": ts_of(ce), "dev_buy_sol": dev,
            "initial_mcap_sol": None, "layout": ce.layout,
        }
    for i, te in enumerate(trades):
        f = te.fields
        out["trades"].append({
            "mint": f.get("mint"), "signature": f"{signature}:{i}" if len(trades) > 1 and signature else signature,
            "buyer": f.get("user"), "is_buy": bool(f.get("is_buy")),
            "sol": (f.get("sol_amount") or 0) / 1e9, "tokens": (f.get("token_amount") or 0) / 1e6,
            "virtual_sol": f.get("virtual_sol_reserves"), "virtual_token": f.get("virtual_token_reserves"),
            "slot": slot, "block_time": ts_of(te),
        })
    if migs:
        f = migs[0].fields
        out["migration"] = {
            "mint": f.get("mint"), "pool_address": f.get("pool"), "dex": "pump-amm", "signature": signature,
            "slot": slot, "block_time": ts_of(migs[0]), "layout": migs[0].layout,
        }
    return out


def pump_amm_trade_records(events: list[DecodedEvent], signature: str | None, slot: int | None, mint: str,
                           expected_pool: str | None) -> tuple[list[dict[str, Any]], list[str]]:
    """pump-amm BuyEvent/SellEvent -> trade records for `mint` (the subscription tells us the mint;
    the event only carries the pool). Returns (records, rejected_reasons). An event whose `pool` differs
    from the pool we subscribed to is rejected: that is the layout self-check (docs/PUMPFUN-EVENTS.md).

    Amounts: quote = WSOL lamports, base = token base units (6 decimals) - TO VERIFY. The pool reserves
    in the event are stored as virtual_sol/virtual_token so the price/mcap math works unchanged.
    """
    out: list[dict[str, Any]] = []
    rejected: list[str] = []
    swaps = [e for e in events if e.name in ("BuyEvent", "SellEvent")]
    for i, e in enumerate(swaps):
        f = e.fields
        if expected_pool and f.get("pool") and f["pool"] != expected_pool:
            rejected.append("pool_mismatch")
            continue
        if any("implausible" in s for s in e.suspect):
            rejected.append("suspect_layout")
            continue
        is_buy = e.name == "BuyEvent"
        sol_lamports = f.get("quote_amount_in") if is_buy else f.get("quote_amount_out")
        tokens_base = f.get("base_amount_out") if is_buy else f.get("base_amount_in")
        if sol_lamports is None or tokens_base is None:
            rejected.append("missing_amounts")
            continue
        ts = f.get("timestamp")
        out.append({
            "mint": mint, "signature": f"{signature}:{i}" if len(swaps) > 1 and signature else signature,
            "buyer": f.get("user"), "is_buy": is_buy,
            "sol": sol_lamports / 1e9, "tokens": tokens_base / 1e6,
            "virtual_sol": f.get("pool_quote_token_reserves"), "virtual_token": f.get("pool_base_token_reserves"),
            "slot": slot, "block_time": ts if isinstance(ts, int) and not any("timestamp" in s for s in e.suspect) else None,
            "pool": f.get("pool"),
        })
    return out, rejected


def pool_from_create_pool_event(events: list[DecodedEvent]) -> dict[str, Any] | None:
    """pump-amm CreatePoolEvent (in the migration transaction) -> {mint, pool_address} when both decoded."""
    for e in events:
        if e.name == "CreatePoolEvent" and e.fields.get("base_mint") and e.fields.get("pool"):
            return {"mint": e.fields["base_mint"], "pool_address": e.fields["pool"], "dex": "pump-amm"}
    return None


def store_undecoded(conn: sqlite3.Connection, items: list[Undecoded], source: str, signature: str | None,
                    slot: int | None, received_at: float, max_per_hour: int = 500) -> int:
    """Keep undecodable payloads (not ignored/known events) for later layout work. Bounded per hour.
    The emitting program (from the log nesting) is stored so `fart events classify` can tell pump.fun
    events from Jupiter/Orca/Raydium events in the same transaction."""
    n = 0
    for u in items:
        if u.reason == "ignored_event":
            continue
        cnt = conn.execute("SELECT COUNT(*) FROM raw_undecoded WHERE received_at>?", (received_at - 3600,)).fetchone()[0]
        if cnt >= max_per_hour:
            break
        conn.execute(
            "INSERT INTO raw_undecoded(received_at, source, signature, slot, discriminator, payload_b64, reason, program)"
            " VALUES(?,?,?,?,?,?,?,?)",
            (received_at, source, signature, slot, u.discriminator_hex, u.payload_b64[:4000], u.reason[:200], u.program),
        )
        n += 1
    return n


def counter_bump(conn: sqlite3.Connection, key: str, n: int = 1, ts: float = 0.0) -> None:
    conn.execute(
        "INSERT INTO kv(key, value, updated_at) VALUES(?, ?, ?)"
        " ON CONFLICT(key) DO UPDATE SET value=CAST(CAST(value AS INTEGER)+? AS TEXT), updated_at=?",
        (key, str(n), ts, n, ts),
    )
