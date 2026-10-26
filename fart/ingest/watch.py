"""Watchlist: which mints the trade streams follow (capped, with expiry, with a reserve for probation).

Reasons, in priority order (lowest first): probation (every new coin for its first minutes),
early_buyers, meta_burst, graduation, observe.

Milestone 4 (after the subscription churn of 26 Oct 2026, when `add()` evicted entries of equal
priority and re-added them one engine tick later with a fresh `since`):

* `add()` evicts only an entry of *strictly* lower priority; two entries of the same reason never
  displace each other, so a full list stays as it is until something expires.
* `since` is written once, at the first insert. A repeated add of an active mint and a
  reactivation of an expired one keep it, so every ordering by `since` (eviction order, the trade
  stream's cap) is stable. `activated_at` is the last (re)activation and is what the idle expiry
  counts from (a coin that comes back after a stream pause gets a full idle window again).
* `probation_reserve` slots of the cap can only be used by probation: the other reasons share
  `cap - probation_reserve` slots among themselves. Probation may use any free slot beyond its
  reserve; such an overflow slot is the first thing a higher-priority add reclaims.
"""
from __future__ import annotations

import sqlite3
from typing import Any, Iterable

PRIORITY = {"probation": 0, "early_buyers": 1, "meta_burst": 2, "graduation": 3, "observe": 4}
_PRIORITY_SQL = ("CASE COALESCE(reason, 'probation') WHEN 'probation' THEN 0 WHEN 'early_buyers' THEN 1"
                 " WHEN 'meta_burst' THEN 2 WHEN 'graduation' THEN 3 WHEN 'observe' THEN 4 ELSE 0 END")


def priority(reason: str | None) -> int:
    return PRIORITY.get(reason or "probation", 0)


def limits(wcfg: dict[str, Any]) -> tuple[int, int]:
    """(cap, probation_reserve) from config/engine.json -> watchlist; the reserve never exceeds the cap."""
    cap = max(0, int(wcfg.get("cap", 200)))
    reserve = max(0, min(cap, int(wcfg.get("probation_reserve", 0))))
    return cap, reserve


def _deactivate(conn: sqlite3.Connection, mint: str, now: float) -> None:
    conn.execute("UPDATE watchlist SET active=0, until=? WHERE mint=?", (now, mint))


def add(conn: sqlite3.Connection, mint: str, reason: str, now: float, ttl_s: float | None, cap: int,
        probation_reserve: int = 0) -> bool:
    """Add, upgrade or reactivate a mint. Returns False when the list is full and nothing of strictly
    lower priority can be evicted (the caller simply tries again later; nothing is written then).

    An active mint is upgraded in place when the new reason ranks at least as high (later expiry wins,
    None means no expiry) and keeps its `since`; its slot is never re-checked against the caps."""
    until = None if ttl_s is None else now + ttl_s
    row = conn.execute("SELECT reason, active, until FROM watchlist WHERE mint=?", (mint,)).fetchone()
    if row is not None and row["active"]:
        if priority(reason) >= priority(row["reason"]):
            new_until = None if (until is None or row["until"] is None) else max(until, row["until"])
            conn.execute("UPDATE watchlist SET reason=?, until=? WHERE mint=?", (reason, new_until, mint))
        return True
    counts = conn.execute(
        "SELECT COUNT(*) AS total, COALESCE(SUM(CASE WHEN COALESCE(reason, 'probation') <> 'probation' THEN 1 ELSE 0 END), 0) AS others"
        " FROM watchlist WHERE active=1"
    ).fetchone()
    total, others = int(counts["total"]), int(counts["others"])
    shared = max(0, int(cap) - max(0, int(probation_reserve)))
    if reason == "probation":
        if total >= cap:
            return False  # nothing ranks below probation
    elif others >= shared:
        victim = conn.execute(
            f"SELECT mint FROM watchlist WHERE active=1 AND COALESCE(reason, 'probation') <> 'probation' AND {_PRIORITY_SQL} < ?"
            f" ORDER BY {_PRIORITY_SQL} ASC, since ASC LIMIT 1", (priority(reason),)
        ).fetchone()
        if victim is None:
            return False  # the shared slots hold only equal or higher priority: wait for an expiry
        _deactivate(conn, victim["mint"], now)
    elif total >= cap:
        # probation overflowed its reserve into the shared slots: reclaim the oldest probation entry
        victim = conn.execute(
            "SELECT mint FROM watchlist WHERE active=1 AND COALESCE(reason, 'probation') = 'probation' ORDER BY since ASC LIMIT 1"
        ).fetchone()
        if victim is None:
            return False
        _deactivate(conn, victim["mint"], now)
    if row is None:
        conn.execute(
            "INSERT INTO watchlist(mint, since, reason, until, active, activated_at) VALUES(?,?,?,?,1,?)", (mint, now, reason, until, now)
        )
    else:
        # reactivation: `since` stays the original time (stable ordering), `activated_at` moves
        conn.execute(
            "UPDATE watchlist SET reason=?, until=?, active=1, activated_at=? WHERE mint=?", (reason, until, now, mint)
        )
    return True


def release(conn: sqlite3.Connection, mints: Iterable[str], reason: str, now: float) -> int:
    """Deactivate the given mints while their reason is still `reason` (an upgraded entry stays)."""
    n = 0
    for mint in mints:
        cur = conn.execute("UPDATE watchlist SET active=0, until=? WHERE mint=? AND active=1 AND reason=?", (now, mint, reason))
        n += cur.rowcount
    return n


def expire(conn: sqlite3.Connection, now: float, dead_after_idle_s: float | None = None) -> int:
    """Deactivate entries past `until`, and (optionally) non-probation entries without a trade for
    `dead_after_idle_s` since their last (re)activation."""
    cur = conn.execute("UPDATE watchlist SET active=0 WHERE active=1 AND until IS NOT NULL AND until<=?", (now,))
    n = cur.rowcount
    if dead_after_idle_s is not None:
        cur = conn.execute(
            "UPDATE watchlist SET active=0, until=? WHERE active=1 AND COALESCE(reason, 'probation') <> 'probation'"
            " AND MAX(COALESCE(last_trade_at, 0), COALESCE(activated_at, since)) <= ?",
            (now, now - dead_after_idle_s),
        )
        n += cur.rowcount
    return n


def active_mints(conn: sqlite3.Connection) -> list[str]:
    return [r[0] for r in conn.execute("SELECT mint FROM watchlist WHERE active=1 ORDER BY since")]


def active_counts(conn: sqlite3.Connection) -> dict[str, int]:
    """Active entries per reason (for `fart status` and the engine summary)."""
    return {str(r[0] or "probation"): int(r[1]) for r in conn.execute("SELECT reason, COUNT(*) FROM watchlist WHERE active=1 GROUP BY reason")}


def is_watched(conn: sqlite3.Connection, mint: str) -> bool:
    return conn.execute("SELECT 1 FROM watchlist WHERE mint=? AND active=1", (mint,)).fetchone() is not None
