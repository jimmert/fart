"""Coverage: how complete is our view of pump.fun creates and graduations, per UTC hour.

Gaps are intervals during which a source was down (disconnect, budget cap, process restart).
A gap left open by the previous process is closed at the next start with reason `restart`.
The signature sweep (`rpc_sigs`) lists every create/graduation signature since its previous run,
so a complete, successful sweep *recovers* every stream gap that ended before it started
(`recovered_at`/`recovered_by='sweep'`); a gap caused by the CU budget cap is never recovered by
the sweep because the sweep is paused by the same cap. The sweep itself opens an `rpc_sigs` gap
when it fails. An hour is `complete` when it is over and at least one configured create source had
no unrecovered gap inside it. Event hour = chain time when known, else our received_at
(documented: docs/METING.md).
"""
from __future__ import annotations

import json
import sqlite3
from typing import Any

from ..util import hour_bounds, hour_key

CREATE_SOURCES = ("pumpportal", "rpc_logs", "rpc_sigs")
STREAM_SOURCES = ("pumpportal", "rpc_logs", "rpc_trades")
UNRECOVERABLE_REASONS = ("budget_cap",)


def gap_open(conn: sqlite3.Connection, source: str, start: float, reason: str) -> int:
    cur = conn.execute("INSERT INTO coverage_gaps(start, end, source, reason) VALUES(?,?,?,?)", (start, None, source, reason))
    return int(cur.lastrowid)


def gap_close(conn: sqlite3.Connection, gap_id: int, end: float) -> None:
    conn.execute("UPDATE coverage_gaps SET end=? WHERE id=? AND end IS NULL", (end, gap_id))


def close_dangling_gaps(conn: sqlite3.Connection, end: float, source: str | None = None, relabel: str | None = None) -> int:
    """At startup: gaps left open by the previous process (shutdown or crash) end now. With
    `relabel` (ingest passes 'restart') the reason is replaced so the report shows restart gaps as
    such; the sweep recovers them (`mark_recovered_by_sweep`)."""
    set_clause = "end=?" + (", reason=?" if relabel else "")
    args: list[Any] = [end] + ([relabel] if relabel else [])
    if source is None:
        cur = conn.execute(f"UPDATE coverage_gaps SET {set_clause} WHERE end IS NULL", args)
    else:
        cur = conn.execute(f"UPDATE coverage_gaps SET {set_clause} WHERE end IS NULL AND source=?", args + [source])
    return cur.rowcount


def mark_recovered_by_sweep(conn: sqlite3.Connection, sweep_started_at: float, now: float) -> int:
    """A complete, successful sweep saw every create/graduation signature since the previous sweep:
    every closed stream gap that ended before this sweep started is recovered. Budget-cap gaps are
    not (the sweep shares the cap)."""
    marks = ",".join("?" for _ in STREAM_SOURCES)
    unrec = ",".join("?" for _ in UNRECOVERABLE_REASONS)
    cur = conn.execute(
        f"UPDATE coverage_gaps SET recovered_at=?, recovered_by='sweep' WHERE recovered_at IS NULL AND end IS NOT NULL"
        f" AND end <= ? AND source IN ({marks}) AND COALESCE(reason, '') NOT IN ({unrec})",
        (now, sweep_started_at, *STREAM_SOURCES, *UNRECOVERABLE_REASONS),
    )
    return cur.rowcount


def open_gap_id(conn: sqlite3.Connection, source: str) -> int | None:
    row = conn.execute("SELECT id FROM coverage_gaps WHERE source=? AND end IS NULL ORDER BY id DESC LIMIT 1", (source,)).fetchone()
    return None if row is None else int(row["id"])


def gaps_overlapping(conn: sqlite3.Connection, start: float, end: float, now: float) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT id, start, end, source, reason, recovered_at, recovered_by FROM coverage_gaps WHERE start < ? AND COALESCE(end, ?) > ? ORDER BY start",
        (end, now, start),
    ).fetchall()
    out = []
    for r in rows:
        s = max(r["start"], start)
        e = min(r["end"] if r["end"] is not None else now, end)
        out.append({"source": r["source"], "reason": r["reason"], "start": s, "end": e, "seconds": round(e - s, 1),
                    "recovered": r["recovered_at"] is not None, "recovered_by": r["recovered_by"]})
    return out


def _count_coins(conn: sqlite3.Connection, start: float, end: float, source: str | None) -> int:
    if source is None:
        return conn.execute(
            "SELECT COUNT(*) FROM coins WHERE COALESCE(block_time, received_at) >= ? AND COALESCE(block_time, received_at) < ?",
            (start, end),
        ).fetchone()[0]
    return conn.execute(
        "SELECT COUNT(*) FROM coins c WHERE COALESCE(c.block_time, c.received_at) >= ? AND COALESCE(c.block_time, c.received_at) < ?"
        " AND EXISTS (SELECT 1 FROM json_each(c.sources) WHERE value = ?)",
        (start, end, source),
    ).fetchone()[0]


def _count_coins_any(conn: sqlite3.Connection, start: float, end: float, sources: tuple[str, ...]) -> int:
    marks = ",".join("?" for _ in sources)
    return conn.execute(
        f"SELECT COUNT(*) FROM coins c WHERE COALESCE(c.block_time, c.received_at) >= ? AND COALESCE(c.block_time, c.received_at) < ?"
        f" AND EXISTS (SELECT 1 FROM json_each(c.sources) WHERE value IN ({marks}))",
        (start, end, *sources),
    ).fetchone()[0]


def compute_hour(conn: sqlite3.Connection, hour: str, now: float) -> dict[str, Any]:
    start, end = hour_bounds(hour)
    gaps = gaps_overlapping(conn, start, end, now)
    row = {
        "hour": hour,
        "creates_pp": _count_coins(conn, start, end, "pumpportal"),
        "creates_rpc": _count_coins_any(conn, start, end, ("rpc_logs", "rpc_sigs", "rpc_backfill", "rpc_tx")),
        "creates_union": _count_coins(conn, start, end, None),
        "grads": conn.execute(
            "SELECT COUNT(*) FROM migrations WHERE COALESCE(block_time, received_at) >= ? AND COALESCE(block_time, received_at) < ?",
            (start, end),
        ).fetchone()[0],
        "grads_without_create": conn.execute(
            "SELECT COUNT(*) FROM migrations m WHERE COALESCE(m.block_time, m.received_at) >= ? AND COALESCE(m.block_time, m.received_at) < ?"
            " AND NOT EXISTS (SELECT 1 FROM coins c WHERE c.mint = m.mint)",
            (start, end),
        ).fetchone()[0],
        "gaps": gaps,
    }
    sources_with_gap = {g["source"] for g in gaps if not g.get("recovered")}
    hour_over = now >= end
    some_create_source_clean = any(s not in sources_with_gap for s in CREATE_SOURCES if _source_was_configured(conn, s))
    row["complete"] = bool(hour_over and some_create_source_clean)
    return row


def _source_was_configured(conn: sqlite3.Connection, source: str) -> bool:
    """A source counts as configured if it ever produced a row or a gap; avoids calling an hour
    'complete' on the strength of a source that was never running."""
    if source == "pumpportal":
        return conn.execute("SELECT 1 FROM kv WHERE key='ingest.pumpportal.started'").fetchone() is not None
    if source in ("rpc_logs", "rpc_sigs"):
        return conn.execute("SELECT 1 FROM kv WHERE key=?", (f"ingest.{source}.started",)).fetchone() is not None
    return False


def write_hour(conn: sqlite3.Connection, hour: str, now: float) -> dict[str, Any]:
    row = compute_hour(conn, hour, now)
    conn.execute(
        "INSERT INTO coverage(hour, creates_pp, creates_rpc, creates_union, grads, grads_without_create, gaps_json, complete, computed_at)"
        " VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(hour) DO UPDATE SET creates_pp=excluded.creates_pp, creates_rpc=excluded.creates_rpc,"
        " creates_union=excluded.creates_union, grads=excluded.grads, grads_without_create=excluded.grads_without_create,"
        " gaps_json=excluded.gaps_json, complete=excluded.complete, computed_at=excluded.computed_at",
        (hour, row["creates_pp"], row["creates_rpc"], row["creates_union"], row["grads"], row["grads_without_create"],
         json.dumps(row["gaps"]), 1 if row["complete"] else 0, now),
    )
    return row


def recompute_recent(conn: sqlite3.Connection, now: float, hours: int = 3) -> list[dict[str, Any]]:
    """Recompute the current hour and the previous `hours` hours (late rows keep arriving)."""
    out = []
    for i in range(hours, -1, -1):
        out.append(write_hour(conn, hour_key(now - i * 3600), now))
    return out


def report(conn: sqlite3.Connection, hours: int, now: float) -> list[dict[str, Any]]:
    """Live view for `fart coverage --hours N` (computed, not from the table)."""
    return [compute_hour(conn, hour_key(now - i * 3600), now) for i in range(hours - 1, -1, -1)]
