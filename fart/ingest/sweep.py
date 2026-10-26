"""Signature sweep and transaction backfill through the RPC (budget-bounded).

Every few minutes `getSignaturesForAddress` on the mint authority (creates) and the migration
account (graduations) lists what happened on-chain since the last sweep. Signatures we already
know (from PumpPortal or the log stream) get their chain time (slot, blockTime) filled in for
~50 CU per 1000 signatures; signatures we have never seen are missed events and go to the
enrich queue, where a bounded number of `getTransaction` calls recover them. This is the
completeness path; the websocket streams are the real-time path.

Milestone 3: the enrich queue serves migrations before creates (a graduation without block_time
and pool address hurts the measurement more than a missed create), its daily limit is a fraction of
the daily CU cap (`backfill.max_cu_fraction_per_day`; the fixed `max_transactions_per_day` is the
fallback without a cap) and the day's used/limit/skipped counts go to kv for the daily report.
Writes are short IMMEDIATE transactions with retries.
"""
from __future__ import annotations

import sqlite3
from typing import Any

from .. import db
from ..chain.rpc import RpcClient, RpcError
from ..chain.tx import create_from_tx, events_from_transaction, migration_from_tx
from ..db import kv_get, kv_set
from ..log import get_logger
from ..net.budget import Blocked
from ..net.http import HttpError
from ..util import day_key
from . import coverage, store

log = get_logger("sweep")

SIGS_SOURCE = "rpc_sigs"

KINDS = {"create": ("coins", "ingest.sweep.create.last_sig", "create_tx"),
         "migrate": ("migrations", "ingest.sweep.migrate.last_sig", "migrate_tx")}


def _add_source(conn: sqlite3.Connection, table: str, mint: str, source: str) -> None:
    row = conn.execute(f"SELECT sources FROM {table} WHERE mint=?", (mint,)).fetchone()
    if row is not None:
        conn.execute(f"UPDATE {table} SET sources=? WHERE mint=?", (store._sources_union(row["sources"], source), mint))


def sweep_signatures(conn: sqlite3.Connection, rpc: RpcClient, addresses: dict[str, str], cfg: dict[str, Any], now: float) -> dict[str, Any]:
    """One sweep over both addresses. Returns counters. Never raises on RPC trouble."""
    out: dict[str, Any] = {}
    max_sigs = int(cfg.get("max_signatures_per_gap", 1000))
    db.kv_setdefault(conn, "ingest.rpc_sigs.first_started", str(now), now)
    kv_set(conn, "ingest.rpc_sigs.started", str(now), now)
    complete = True   # every kind listed without error, below the bound, not a first run
    failed_reason: str | None = None
    for kind, address in addresses.items():
        table, last_key, enrich_kind = KINDS[kind]
        last_sig = kv_get(conn, last_key)
        collected: list[dict[str, Any]] = []
        before: str | None = None
        pages = 0
        try:
            while len(collected) < max_sigs:
                page = rpc.get_signatures_for_address(address, limit=min(1000, max_sigs - len(collected)), before=before, until=last_sig)
                pages += 1
                collected.extend(page)
                if len(page) < 1000 or last_sig is None:  # first run: one page only, bounded
                    break
                before = page[-1]["signature"]
        except (Blocked, HttpError, RpcError) as e:
            log.warning("sweep.rpc_unavailable", kind=kind, error=str(e)[:200])
            out[kind] = {"error": str(e)[:200]}
            complete = False
            failed_reason = "budget_cap" if isinstance(e, Blocked) and e.reason in ("daily_cost_cap", "daily_requests_cap") else "sweep_error"
            continue
        def _apply(c: sqlite3.Connection) -> tuple[int, int, int]:
            filled = missed = failed = 0
            for s in collected:
                sig = s.get("signature")
                if not sig:
                    continue
                if s.get("err"):
                    failed += 1
                    continue
                row = c.execute(f"SELECT mint, slot, block_time FROM {table} WHERE signature=?", (sig,)).fetchone()
                if row is not None:
                    c.execute(
                        f"UPDATE {table} SET slot=COALESCE(slot,?), block_time=COALESCE(block_time,?) WHERE mint=?",
                        (s.get("slot"), s.get("blockTime"), row["mint"]),
                    )
                    _add_source(c, table, row["mint"], "rpc_sigs")
                    filled += 1
                else:
                    c.execute("INSERT OR IGNORE INTO enrich_queue(key, kind, requested_at) VALUES(?,?,?)", (sig, enrich_kind, now))
                    missed += 1
            if collected:
                kv_set(c, last_key, collected[0]["signature"], now)
            return filled, missed, failed

        filled, missed, failed = db.write_tx(conn, _apply, what=f"sweep.{kind}")
        out[kind] = {"signatures": len(collected), "pages": pages, "filled": filled, "missed": missed, "failed_tx": failed,
                     "first_run": last_sig is None, "truncated": len(collected) >= max_sigs}
        if last_sig is None or len(collected) >= max_sigs:
            complete = False
        log.info("sweep.done", kind=kind, **out[kind])
    # the sweep is itself a create source: it has a gap while it fails, and a complete run recovers stream gaps
    gap_id = coverage.open_gap_id(conn, SIGS_SOURCE)
    if failed_reason is not None:
        if gap_id is None:
            coverage.gap_open(conn, SIGS_SOURCE, now, failed_reason)
        out["gap"] = failed_reason
    else:
        if gap_id is not None:
            coverage.gap_close(conn, gap_id, now)
        if complete:
            out["recovered_gaps"] = coverage.mark_recovered_by_sweep(conn, now, now)
    return out


def enrich_daily_limit(cfg: dict[str, Any], daily_cap: float | None, tx_cost: float | None) -> int:
    """Transactions per UTC day: max_cu_fraction_per_day x the daily CU cap / the getTransaction cost
    (CU-based, so it scales with RPC_DAILY_BUDGET_CU); the fixed max_transactions_per_day without a cap."""
    frac = cfg.get("max_cu_fraction_per_day")
    if frac is not None and daily_cap and tx_cost and float(tx_cost) > 0:
        return max(0, int(float(frac) * float(daily_cap) / float(tx_cost)))
    return int(cfg.get("max_transactions_per_day", 1500))


def rpc_cap_and_tx_cost(rpc: Any) -> tuple[float | None, float | None]:
    """(daily CU cap, getTransaction cost) of the client's budget source; (None, None) for a client without one."""
    budget = getattr(rpc, "budget", None)
    source = getattr(rpc, "source", None)
    if budget is None or source is None:
        return None, None
    try:
        sc = budget.source(source)
    except KeyError:
        return None, None
    return sc.daily_cost, sc.cost_of("getTransaction")


def enrich_backlog(conn: sqlite3.Connection, max_attempts: int = 3) -> dict[str, int]:
    """Pending enrich_queue rows by kind (what the limit holds back)."""
    out = {"migrate_tx": 0, "create_tx": 0}
    for r in conn.execute("SELECT kind, COUNT(*) FROM enrich_queue WHERE done=0 AND attempts<? GROUP BY kind", (max_attempts,)):
        out[r[0]] = int(r[1])
    return out


def process_enrich_queue(conn: sqlite3.Connection, rpc: RpcClient, cfg: dict[str, Any], now: float) -> dict[str, int]:
    """Fetch missed migration and create transactions (migrations first), bounded per run and per day."""
    max_run = int(cfg.get("max_transactions_per_gap", 100))
    cap, tx_cost = rpc_cap_and_tx_cost(rpc)
    max_day = enrich_daily_limit(cfg, cap, tx_cost)
    max_attempts = int(cfg.get("max_attempts", 3))
    day = day_key(now)
    day_counter = f"enrich.tx.{day}"
    used_today = int(kv_get(conn, day_counter, "0") or 0)
    stats = {"fetched": 0, "creates": 0, "migrations": 0, "empty": 0, "errors": 0, "skipped_budget": 0, "limit": max_day}
    rows = conn.execute(
        "SELECT key, kind FROM enrich_queue WHERE done=0 AND attempts<?"
        " ORDER BY CASE kind WHEN 'migrate_tx' THEN 0 ELSE 1 END, requested_at LIMIT ?", (max_attempts, max_run)
    ).fetchall()
    for r in rows:
        if used_today >= max_day:
            stats["skipped_budget"] += 1
            continue
        if not rpc.available("getTransaction"):
            stats["skipped_budget"] += 1
            continue
        try:
            tx = rpc.get_transaction(r["key"])
        except Blocked:
            stats["skipped_budget"] += 1
            continue
        except (HttpError, RpcError) as e:
            stats["errors"] += 1
            conn.execute("UPDATE enrich_queue SET attempts=attempts+1, last_attempt=?, last_error=? WHERE key=? AND kind=?",
                         (now, str(e)[:200], r["key"], r["kind"]))
            continue
        used_today += 1
        stats["fetched"] += 1
        te = events_from_transaction(tx)
        used_now = used_today

        def _apply(c: sqlite3.Connection) -> str:
            res = "empty"
            if r["kind"] == "create_tx":
                rec = create_from_tx(te)
                if rec and not te.err:
                    store.upsert_coin(c, rec, "rpc_backfill", now)
                    res = "creates"
            else:
                rec = migration_from_tx(te)
                if rec and not te.err:
                    store.upsert_migration(c, rec, "rpc_backfill", now)
                    res = "migrations"
            if te.undecoded:
                store.store_undecoded(c, te.undecoded, "rpc_backfill", te.signature, te.slot, now)
            c.execute("UPDATE enrich_queue SET done=1, attempts=attempts+1, last_attempt=? WHERE key=? AND kind=?",
                      (now, r["key"], r["kind"]))
            kv_set(c, day_counter, str(used_now), now)
            return res

        stats[db.write_tx(conn, _apply, what="enrich")] += 1
    if stats["skipped_budget"] or rows:
        db.write_tx(conn, lambda c: (kv_set(c, f"enrich.limit.{day}", str(max_day), now),
                                     store.counter_bump(c, f"enrich.skipped_budget.{day}", stats["skipped_budget"], now)
                                     if stats["skipped_budget"] else None), what="enrich.stats")
    if rows:
        log.info("enrich.done", **stats, used_today=used_today)
    return stats
