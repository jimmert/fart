"""Normalize PumpPortal messages and RPC events into the event-time tables.

Chain time (slot, block_time) and our clock (received_at) are always separate columns. Rows
seen by several sources are merged: details are filled where missing, `sources` is the union.

Milestone 3 guards: a mint on the configurable system-mint list (USDC, USDT, WSOL, PUMP;
config/pumpfun.json -> system_mints) is never a coin, a migration or a watchlist candidate, and a
PumpPortal migration is only accepted for a coin we know (`coins`). A refused message is kept in
`rejected_messages` with its reason. pump-amm swaps get their mint exclusively through the pool ->
base-mint mapping (CreatePoolEvent in the same transaction or `migrations.pool_address`).
"""
from __future__ import annotations

import json
import sqlite3
from typing import Any, Callable

from ..chain.events import DecodedEvent, Undecoded
from ..db import counter_bump as _db_counter_bump
from ..util import fnum

RPC_SOURCES = ("rpc_logs", "rpc_sigs", "rpc_backfill", "rpc_tx", "rpc_trades")
TRADE_SOURCES = ("pumpportal", "rpc_logs", "rpc_trades", "pump_amm")

# Fallback when config/pumpfun.json carries no list (the schema-v3 cleanup uses the same addresses).
DEFAULT_SYSTEM_MINTS = {
    "USDC": "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
    "USDT": "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB",
    "WSOL": "So11111111111111111111111111111111111111112",
    "PUMP": "pumpCmXqMfrsAkQ5r49WcJnRayYRqmXz6ae8H7H9Dfn",
}
MAX_REJECTED_JSON = 4000


def system_mints(pumpfun_cfg: dict[str, Any] | None) -> dict[str, str]:
    """label -> mint. The list is configurable; the defaults cover what the VPS saw on 26 Oct 2026."""
    raw = (pumpfun_cfg or {}).get("system_mints")
    if isinstance(raw, dict) and raw:
        return {str(k): str(v) for k, v in raw.items() if not str(k).startswith("_")}
    if isinstance(raw, list) and raw:
        return {str(m): str(m) for m in raw}
    return dict(DEFAULT_SYSTEM_MINTS)


def system_mint_label(mint: str | None, pumpfun_cfg: dict[str, Any] | None) -> str | None:
    """The label (USDC, PUMP, ...) when `mint` is a system mint, else None."""
    if not mint:
        return None
    for label, m in system_mints(pumpfun_cfg).items():
        if m == mint:
            return label
    return None


def coin_known(conn: sqlite3.Connection, mint: str | None) -> bool:
    """A create was confirmed by any source (PumpPortal, the RPC stream or the backfill)."""
    if not mint:
        return False
    return conn.execute("SELECT 1 FROM coins WHERE mint=?", (mint,)).fetchone() is not None


def reject_message(conn: sqlite3.Connection, source: str, kind: str | None, reason: str, mint: str | None, msg: Any,
                   received_at: float, raw_ref: str | None = None) -> int:
    """Keep a refused message (bounded JSON) and count it: kv `ingest.rejected.<source>.<reason>`."""
    try:
        raw = json.dumps(msg, separators=(",", ":"), ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        raw = str(msg)
    cur = conn.execute(
        "INSERT INTO rejected_messages(received_at, source, kind, reason, mint, raw_json, raw_ref) VALUES(?,?,?,?,?,?,?)",
        (received_at, source, kind, reason, mint, raw[:MAX_REJECTED_JSON], raw_ref),
    )
    counter_bump(conn, f"ingest.rejected.{source}.{reason}", 1, received_at)
    return int(cur.lastrowid)


def migration_rejection(conn: sqlite3.Connection, mint: str | None, pumpfun_cfg: dict[str, Any] | None,
                        require_known_coin: bool) -> str | None:
    """Why a migration / watchlist candidate must be refused: 'no_mint', 'system_mint:<label>' or
    'unknown_coin' (only when `require_known_coin`); None when it is acceptable."""
    if not mint:
        return "no_mint"
    label = system_mint_label(mint, pumpfun_cfg)
    if label is not None:
        return f"system_mint:{label}"
    if require_known_coin and not coin_known(conn, mint):
        return "unknown_coin"
    return None


def _sources_union(existing: str | None, new: str) -> str:
    try:
        cur = json.loads(existing) if existing else []
    except json.JSONDecodeError:
        cur = []
    if new not in cur:
        cur.append(new)
    return json.dumps(cur)


def upsert_coin(conn: sqlite3.Connection, rec: dict[str, Any], source: str, received_at: float, raw_ref: str | None = None,
                pumpfun_cfg: dict[str, Any] | None = None) -> bool:
    """Insert or merge a create record. Returns True when the mint was new. A system mint (USDC, WSOL,
    PUMP, ...) is never a coin and is ignored."""
    mint = rec.get("mint")
    if not mint or system_mint_label(mint, pumpfun_cfg) is not None:
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


def pool_base_mints(events: list[DecodedEvent], wsol_mint: str | None = None) -> dict[str, str | None]:
    """pool -> base mint from the CreatePoolEvents of one transaction. A pool whose quote mint is not
    WSOL maps to None (its amounts are not lamports; such a pool is never a trade source for us)."""
    out: dict[str, str | None] = {}
    for e in events:
        if e.name != "CreatePoolEvent":
            continue
        f = e.fields
        pool, base, quote = f.get("pool"), f.get("base_mint"), f.get("quote_mint")
        if not pool or not base:
            continue
        out[pool] = base if (wsol_mint is None or quote is None or quote == wsol_mint) else None
    return out


def pump_amm_trade_records(events: list[DecodedEvent], signature: str | None, slot: int | None,
                           lookup_pool: Callable[[str], str | None] | None = None,
                           wsol_mint: str | None = None) -> tuple[list[dict[str, Any]], list[str]]:
    """pump-amm BuyEvent/SellEvent -> trade records. The event only carries the pool; the mint comes
    EXCLUSIVELY from the pool -> base-mint mapping: a CreatePoolEvent in the same transaction, else
    `lookup_pool(pool)` (migrations.pool_address). Returns (records, rejected_reasons):
    `pool_unknown` (no mapping: never guess the mint from the subscription), `quote_not_wsol` (pool
    quoted in another mint: amounts would not be lamports), `suspect_layout`, `missing_amounts`.

    Amounts: quote = WSOL lamports, base = token base units (6 decimals); the pool reserves in the
    event are stored as virtual_sol/virtual_token so the price/mcap math works unchanged.
    """
    out: list[dict[str, Any]] = []
    rejected: list[str] = []
    known = pool_base_mints(events, wsol_mint)
    swaps = [e for e in events if e.name in ("BuyEvent", "SellEvent")]
    for i, e in enumerate(swaps):
        f = e.fields
        pool = f.get("pool")
        if not pool:
            rejected.append("pool_unknown")
            continue
        if pool in known:
            mint = known[pool]
            if mint is None:
                rejected.append("quote_not_wsol")
                continue
        else:
            mint = lookup_pool(pool) if lookup_pool is not None else None
            if not mint:
                rejected.append("pool_unknown")
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
            "pool": pool,
        })
    return out, rejected


def mint_of_pool(conn: sqlite3.Connection, pool: str) -> str | None:
    """migrations.pool_address -> mint (filled by CompletePumpAmmMigrationEvent / CreatePoolEvent / backfill)."""
    row = conn.execute("SELECT mint FROM migrations WHERE pool_address=? LIMIT 1", (pool,)).fetchone()
    return row["mint"] if row is not None else None


def pool_from_create_pool_event(events: list[DecodedEvent]) -> dict[str, Any] | None:
    """pump-amm CreatePoolEvent (in the migration transaction) -> {mint, pool_address, quote_mint} when decoded."""
    for e in events:
        if e.name == "CreatePoolEvent" and e.fields.get("base_mint") and e.fields.get("pool"):
            return {"mint": e.fields["base_mint"], "pool_address": e.fields["pool"], "dex": "pump-amm",
                    "quote_mint": e.fields.get("quote_mint")}
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
    _db_counter_bump(conn, key, n, ts)
