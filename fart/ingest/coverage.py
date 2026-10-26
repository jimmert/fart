"""Coverage: how complete is our view of pump.fun creates and graduations, per UTC hour.

Gaps are intervals during which a source was down (disconnect, budget cap, process restart).
A gap left open by the previous process is closed at the next start with reason `restart`.
The signature sweep (`rpc_sigs`) lists every create/graduation signature since its previous run,
so a complete, successful sweep *recovers* every stream gap that ended before it started
(`recovered_at`/`recovered_by='sweep'`); a gap caused by the CU budget cap is never recovered by
the sweep because the sweep is paused by the same cap. The sweep itself opens an `rpc_sigs` gap
when it fails.

Covered versus complete (milestone 3, after a report called a day fart never ran "complete"):

* a source only counts from the moment it first ran (`ingest.<source>.first_started`, else the
  earliest gap row it ever wrote); hours before that are not covered by it;
* a period without any running process is a `down` gap for every stream source (recorded at the
  next start from the previous process's last heartbeat);
* an hour is **covered** when at least one create source was active during some part of it with an
  unrecovered gap that does not span that whole part; a day without a covered hour is "NIET GEDEKT";
* an hour is **complete** when it is over and at least one create source was active for the whole
  hour without an unrecovered gap. Not covered is never complete.

Event hour = chain time when known, else our received_at (documented: docs/METING.md).
"""
from __future__ import annotations

import json
import sqlite3
from typing import Any

from ..db import kv_get
from ..util import hour_bounds, hour_key

CREATE_SOURCES = ("pumpportal", "rpc_logs", "rpc_sigs")
STREAM_SOURCES = ("pumpportal", "rpc_logs", "rpc_trades")
DOWNTIME_SOURCES = STREAM_SOURCES + ("rpc_sigs",)
UNRECOVERABLE_REASONS = ("budget_cap",)
DOWNTIME_MIN_S = 60.0


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


def record_downtime(conn: sqlite3.Connection, last_heartbeat: float | None, now: float,
                    sources: tuple[str, ...] = DOWNTIME_SOURCES, min_gap_s: float = DOWNTIME_MIN_S) -> int:
    """At startup, before `close_dangling_gaps`: the previous process's last heartbeat is older than
    `min_gap_s` and a source has no open gap (crash, kill, host down: nothing wrote a shutdown gap) ->
    one closed `down` gap per source from that heartbeat until now. Recoverable by the sweep like a
    restart gap. Returns the number of gaps written."""
    if last_heartbeat is None or now - float(last_heartbeat) < min_gap_s:
        return 0
    n = 0
    for source in sources:
        if open_gap_id(conn, source) is not None:
            continue
        if source_active_since(conn, source) is None:
            continue  # never ran: nothing was "down"
        conn.execute("INSERT INTO coverage_gaps(start, end, source, reason) VALUES(?,?,?,?)", (float(last_heartbeat), now, source, "down"))
        n += 1
    return n


def source_active_since(conn: sqlite3.Connection, source: str) -> float | None:
    """Earliest evidence that `source` ever ran: `ingest.<source>.first_started` (milestone 3), the
    older `.started` key, or the earliest gap row it wrote (every run starts with a `startup` gap).
    None = the source never ran: it covers nothing and makes no hour complete."""
    candidates: list[float] = []
    for key in (f"ingest.{source}.first_started", f"ingest.{source}.started"):
        v = kv_get(conn, key)
        if v is not None:
            try:
                candidates.append(float(v))
            except ValueError:
                pass
    row = conn.execute("SELECT MIN(start) FROM coverage_gaps WHERE source=?", (source,)).fetchone()
    if row is not None and row[0] is not None:
        candidates.append(float(row[0]))
    return min(candidates) if candidates else None


def _spans_whole(gaps: list[dict[str, Any]], source: str, start: float, end: float) -> bool:
    """Do the unrecovered gaps of `source` (already clipped to the hour) cover [start, end) entirely?"""
    cur = start
    for g in sorted((g for g in gaps if g["source"] == source and not g.get("recovered")), key=lambda g: g["start"]):
        if g["start"] > cur:
            return False
        cur = max(cur, g["end"])
        if cur >= end:
            return True
    return cur >= end


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
    active = {s: source_active_since(conn, s) for s in CREATE_SOURCES}
    clean = [s for s in CREATE_SOURCES if active[s] is not None and active[s] <= start and s not in sources_with_gap]
    covering = [s for s in CREATE_SOURCES if active[s] is not None and active[s] < min(end, now)
                and not _spans_whole(gaps, s, max(start, active[s]), min(end, now))]
    row["covered"] = bool(covering)
    row["covered_by"] = covering
    row["complete"] = bool(hour_over and clean)
    return row


def write_hour(conn: sqlite3.Connection, hour: str, now: float) -> dict[str, Any]:
    row = compute_hour(conn, hour, now)
    conn.execute(
        "INSERT INTO coverage(hour, creates_pp, creates_rpc, creates_union, grads, grads_without_create, gaps_json, complete, covered, computed_at)"
        " VALUES(?,?,?,?,?,?,?,?,?,?) ON CONFLICT(hour) DO UPDATE SET creates_pp=excluded.creates_pp, creates_rpc=excluded.creates_rpc,"
        " creates_union=excluded.creates_union, grads=excluded.grads, grads_without_create=excluded.grads_without_create,"
        " gaps_json=excluded.gaps_json, complete=excluded.complete, covered=excluded.covered, computed_at=excluded.computed_at",
        (hour, row["creates_pp"], row["creates_rpc"], row["creates_union"], row["grads"], row["grads_without_create"],
         json.dumps(row["gaps"]), 1 if row["complete"] else 0, 1 if row["covered"] else 0, now),
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
