"""Retention: raw gz 30 days, trades only for watched mints and only for N days, samples and
api_ledger pruned, weekly archive of pruned rows to data/archive/*.jsonl.gz. The append-only
ledgers (decisions, outcomes) are never touched.
"""
from __future__ import annotations

import gzip
import json
import sqlite3
from pathlib import Path
from typing import Any

from ..db import kv_get, kv_set
from ..log import get_logger
from ..util import day_key
from .raw import prune_raw

log = get_logger("retention")

WEEK_S = 7 * 86400


def _archive_rows(archive_dir: Path, name: str, rows: list[sqlite3.Row], now: float) -> Path | None:
    if not rows:
        return None
    archive_dir.mkdir(parents=True, exist_ok=True)
    path = archive_dir / f"{name}-{day_key(now)}.jsonl.gz"
    with gzip.open(path, "at", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(dict(r), separators=(",", ":"), default=str) + "\n")
    return path


def run_retention(conn: sqlite3.Connection, raw_dir: Path, archive_dir: Path, cfg: dict[str, Any], now: float) -> dict[str, Any]:
    out: dict[str, Any] = {}
    out["raw_files_removed"] = prune_raw(raw_dir, int(cfg.get("raw_days", 30)), now)

    cutoff_trades = now - int(cfg.get("trades_days", 14)) * 86400
    rows = conn.execute(
        "SELECT * FROM trades WHERE received_at < ? AND mint NOT IN (SELECT mint FROM watchlist WHERE active=1)", (cutoff_trades,)
    ).fetchall()
    _archive_rows(archive_dir, "trades", rows, now)
    conn.execute("DELETE FROM trades WHERE received_at < ? AND mint NOT IN (SELECT mint FROM watchlist WHERE active=1)", (cutoff_trades,))
    out["trades_archived"] = len(rows)

    cutoff_samples = now - int(cfg.get("samples_days", 30)) * 86400
    rows = conn.execute("SELECT * FROM mcap_samples WHERE ts < ?", (cutoff_samples,)).fetchall()
    _archive_rows(archive_dir, "mcap_samples", rows, now)
    conn.execute("DELETE FROM mcap_samples WHERE ts < ?", (cutoff_samples,))
    out["samples_archived"] = len(rows)

    cutoff_ledger = now - int(cfg.get("api_ledger_days", 30)) * 86400
    rows = conn.execute("SELECT * FROM api_ledger WHERE ts < ?", (cutoff_ledger,)).fetchall()
    _archive_rows(archive_dir, "api_ledger", rows, now)
    conn.execute("DELETE FROM api_ledger WHERE ts < ?", (cutoff_ledger,))
    out["ledger_archived"] = len(rows)

    cur = conn.execute("DELETE FROM raw_undecoded WHERE received_at < ?", (now - 30 * 86400,))
    out["undecoded_deleted"] = cur.rowcount
    cur = conn.execute("DELETE FROM enrich_queue WHERE done=1 AND last_attempt < ?", (now - 7 * 86400,))
    out["enrich_queue_deleted"] = cur.rowcount
    cur = conn.execute("DELETE FROM watchlist WHERE active=0 AND COALESCE(until, since) < ?", (now - 30 * 86400,))
    out["watchlist_deleted"] = cur.rowcount
    rows = conn.execute("SELECT * FROM features_asof WHERE computed_at < ?", (now - 30 * 86400,)).fetchall()
    _archive_rows(archive_dir, "features_asof", rows, now)
    conn.execute("DELETE FROM features_asof WHERE computed_at < ?", (now - 30 * 86400,))
    out["features_archived"] = len(rows)
    kv_set(conn, "retention.last_run", str(now), now)
    log.info("retention.done", **out)
    return out


def maybe_run_retention(conn: sqlite3.Connection, raw_dir: Path, archive_dir: Path, cfg: dict[str, Any], now: float) -> dict[str, Any] | None:
    last = float(kv_get(conn, "retention.last_run", "0") or 0)
    if now - last < WEEK_S:
        return None
    return run_retention(conn, raw_dir, archive_dir, cfg, now)
