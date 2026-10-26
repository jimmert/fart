"""Signature sweep and transaction backfill through the RPC (budget-bounded).

Every few minutes `getSignaturesForAddress` on the mint authority (creates) and the migration
account (graduations) lists what happened on-chain since the last sweep. Signatures we already
know (from PumpPortal or the log stream) get their chain time (slot, blockTime) filled in for
~50 CU per 1000 signatures; signatures we have never seen are missed events and go to the
enrich queue, where a bounded number of `getTransaction` calls recover them. This is the
completeness path; the websocket streams are the real-time path.
"""
from __future__ import annotations

import sqlite3
from typing import Any

from ..chain.rpc import RpcClient, RpcError
from ..chain.tx import create_from_tx, events_from_transaction, migration_from_tx
from ..db import kv_get, kv_set
from ..log import get_logger
from ..net.budget import Blocked
from ..net.http import HttpError
from ..util import day_key
from . import store

log = get_logger("sweep")

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
    kv_set(conn, "ingest.rpc_sigs.started", str(now), now)
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
            continue
        filled = missed = failed = 0
        conn.execute("BEGIN")
        try:
            for s in collected:
                sig = s.get("signature")
                if not sig:
                    continue
                if s.get("err"):
                    failed += 1
                    continue
                row = conn.execute(f"SELECT mint, slot, block_time FROM {table} WHERE signature=?", (sig,)).fetchone()
                if row is not None:
                    conn.execute(
                        f"UPDATE {table} SET slot=COALESCE(slot,?), block_time=COALESCE(block_time,?) WHERE mint=?",
                        (s.get("slot"), s.get("blockTime"), row["mint"]),
                    )
                    _add_source(conn, table, row["mint"], "rpc_sigs")
                    filled += 1
                else:
                    conn.execute(
                        "INSERT OR IGNORE INTO enrich_queue(key, kind, requested_at) VALUES(?,?,?)", (sig, enrich_kind, now)
                    )
                    missed += 1
            if collected:
                kv_set(conn, last_key, collected[0]["signature"], now)
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        out[kind] = {"signatures": len(collected), "pages": pages, "filled": filled, "missed": missed, "failed_tx": failed,
                     "first_run": last_sig is None}
        log.info("sweep.done", kind=kind, **out[kind])
    return out


def process_enrich_queue(conn: sqlite3.Connection, rpc: RpcClient, cfg: dict[str, Any], now: float) -> dict[str, int]:
    """Fetch missed create/migration transactions, bounded per run and per day."""
    max_run = int(cfg.get("max_transactions_per_gap", 100))
    max_day = int(cfg.get("max_transactions_per_day", 1500))
    max_attempts = int(cfg.get("max_attempts", 3))
    day_counter = f"enrich.tx.{day_key(now)}"
    used_today = int(kv_get(conn, day_counter, "0") or 0)
    stats = {"fetched": 0, "creates": 0, "migrations": 0, "empty": 0, "errors": 0, "skipped_budget": 0}
    rows = conn.execute(
        "SELECT key, kind FROM enrich_queue WHERE done=0 AND attempts<? ORDER BY requested_at LIMIT ?", (max_attempts, max_run)
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
        conn.execute("BEGIN")
        try:
            if r["kind"] == "create_tx":
                rec = create_from_tx(te)
                if rec and not te.err:
                    store.upsert_coin(conn, rec, "rpc_backfill", now)
                    stats["creates"] += 1
                else:
                    stats["empty"] += 1
            else:
                rec = migration_from_tx(te)
                if rec and not te.err:
                    store.upsert_migration(conn, rec, "rpc_backfill", now)
                    stats["migrations"] += 1
                else:
                    stats["empty"] += 1
            if te.undecoded:
                store.store_undecoded(conn, te.undecoded, "rpc_backfill", te.signature, te.slot, now)
            conn.execute("UPDATE enrich_queue SET done=1, attempts=attempts+1, last_attempt=? WHERE key=? AND kind=?",
                         (now, r["key"], r["kind"]))
            kv_set(conn, day_counter, str(used_today), now)
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
    if rows:
        log.info("enrich.done", **stats)
    return stats
