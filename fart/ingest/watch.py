"""Watchlist: which mints we hold a PumpPortal trade subscription for (capped, with expiry).

Reasons, in eviction order (first evicted first): probation (every new coin for its first
minutes), early_buyers, meta_burst, graduation, observe.
"""
from __future__ import annotations

import sqlite3

PRIORITY = {"probation": 0, "early_buyers": 1, "meta_burst": 2, "graduation": 3, "observe": 4}


def add(conn: sqlite3.Connection, mint: str, reason: str, now: float, ttl_s: float | None, cap: int) -> bool:
    """Add or upgrade a mint. Evicts the lowest-priority, oldest entry when the cap is reached.
    Returns False when nothing could be evicted for a lower-priority request."""
    until = None if ttl_s is None else now + ttl_s
    row = conn.execute("SELECT reason, active, until FROM watchlist WHERE mint=?", (mint,)).fetchone()
    if row is not None and row["active"]:
        if PRIORITY.get(reason, 0) >= PRIORITY.get(row["reason"], 0):
            # upgrade: later expiry wins; None means no expiry
            new_until = None if (until is None or row["until"] is None) else max(until, row["until"])
            conn.execute("UPDATE watchlist SET reason=?, until=? WHERE mint=?", (reason, new_until, mint))
        return True
    active = conn.execute("SELECT COUNT(*) FROM watchlist WHERE active=1").fetchone()[0]
    if active >= cap:
        victim = conn.execute(
            "SELECT mint, reason FROM watchlist WHERE active=1 ORDER BY "
            " CASE reason WHEN 'probation' THEN 0 WHEN 'early_buyers' THEN 1 WHEN 'meta_burst' THEN 2"
            " WHEN 'graduation' THEN 3 WHEN 'observe' THEN 4 ELSE 0 END ASC, since ASC LIMIT 1"
        ).fetchone()
        if victim is None or PRIORITY.get(victim["reason"], 0) > PRIORITY.get(reason, 0):
            return False
        conn.execute("UPDATE watchlist SET active=0, until=? WHERE mint=?", (now, victim["mint"]))
    if row is None:
        conn.execute(
            "INSERT INTO watchlist(mint, since, reason, until, active) VALUES(?,?,?,?,1)", (mint, now, reason, until)
        )
    else:
        conn.execute(
            "UPDATE watchlist SET since=?, reason=?, until=?, active=1 WHERE mint=?", (now, reason, until, mint)
        )
    return True


def expire(conn: sqlite3.Connection, now: float, dead_after_idle_s: float | None = None) -> int:
    """Deactivate entries past `until`, and (optionally) entries with no trade for dead_after_idle_s."""
    cur = conn.execute("UPDATE watchlist SET active=0 WHERE active=1 AND until IS NOT NULL AND until<=?", (now,))
    n = cur.rowcount
    if dead_after_idle_s is not None:
        cur = conn.execute(
            "UPDATE watchlist SET active=0, until=? WHERE active=1 AND reason<>'probation'"
            " AND COALESCE(last_trade_at, since) <= ?",
            (now, now - dead_after_idle_s),
        )
        n += cur.rowcount
    return n


def active_mints(conn: sqlite3.Connection) -> list[str]:
    return [r[0] for r in conn.execute("SELECT mint FROM watchlist WHERE active=1 ORDER BY since")]


def is_watched(conn: sqlite3.Connection, mint: str) -> bool:
    return conn.execute("SELECT 1 FROM watchlist WHERE mint=? AND active=1", (mint,)).fetchone() is not None
