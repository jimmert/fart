"""Minute candles after graduation without RPC cost (milestone 12, B.5): once per graduation, an hour after it, the
1-minute OHLCV candles of the PumpSwap pool from GeckoTerminal, through the shared `geckoterminal` budget source.

Queue: `candle_jobs` has one row per graduation (`enqueue`: every migration without a job; a migration without a pool
address is marked done with `pool_unknown` and never fetched). A job is due `after_graduation_s` after the graduation
(the candles then cover the first hour on the pool: `before_timestamp` = graduation + after + 60 s, `limit` candles back).
`fetch_due` takes at most `max_fetches_per_run` due jobs per tracker run, oldest first, asks the budget gate first (a
blocked gate ends the run and is counted, never retried in a loop), stores the candles (`pool_candles`, one row per
minute; nothing is interpolated), and marks the job done; an HTTP error counts an attempt, after `max_attempts` the job is
done with its last error. Everything is visible: `status()` gives the backlog, the fetched and failed jobs for `fart status`.

Endpoint shape (verified 29 Oct 2026 from the sandbox): GET
https://api.geckoterminal.com/api/v2/networks/solana/pools/<pool>/ohlcv/minute?aggregate=1&limit=N&before_timestamp=T
-> {"data": {"attributes": {"ohlcv_list": [[unix_seconds, open, high, low, close, volume_usd], ...]}}, "meta": {...}},
newest first, prices in USD; docs/UNVERIFIED.md says what still needs the VPS (pool address = our migrations.pool_address).
"""
from __future__ import annotations

import sqlite3
import time
from typing import Any, Callable

from .. import db
from ..log import get_logger
from ..net.budget import Blocked, Budget
from ..net.http import HttpError, request_json
from ..util import day_key

log = get_logger("candles")

GECKO_OHLCV = "https://api.geckoterminal.com/api/v2/networks/solana/pools/{pool}/ohlcv/minute"
ET = "COALESCE(block_time, received_at)"
ENQUEUE_LIMIT = 2000
KV_FETCHED = "candles.fetched"
KV_FAILED = "candles.failed"
KV_LAST_RUN = "candles.last_run"
KV_BLOCKED = "candles.blocked"


def enqueue(conn: sqlite3.Connection, now: float, after_s: float, limit: int = ENQUEUE_LIMIT) -> int:
    """Add a job for every graduation that has none yet (inside a write transaction). Returns the number added."""
    rows = conn.execute(f"SELECT m.mint, {ET.replace('block_time', 'm.block_time').replace('received_at', 'm.received_at')} AS t, m.pool_address FROM migrations m"
                        f" LEFT JOIN candle_jobs j ON j.mint = m.mint WHERE j.mint IS NULL ORDER BY t LIMIT ?", (int(limit),)).fetchall()
    n = 0
    for r in rows:
        pool = r["pool_address"]
        conn.execute("INSERT OR IGNORE INTO candle_jobs(mint, pool, graduated_at, due_at, attempts, done, candles, last_error, created_at) VALUES(?,?,?,?,0,?,0,?,?)",
                     (str(r["mint"]), pool, float(r["t"]), float(r["t"]) + float(after_s), 0 if pool else 1, None if pool else "pool_unknown", now))
        n += 1
    return n


def parse_ohlcv(payload: Any) -> list[dict[str, Any]]:
    """The candles of a GeckoTerminal answer, oldest first; a malformed entry is skipped (never a zero)."""
    try:
        lst = payload["data"]["attributes"]["ohlcv_list"]
    except (KeyError, TypeError):
        return []
    out = []
    for e in lst if isinstance(lst, list) else []:
        if not isinstance(e, (list, tuple)) or len(e) < 6:
            continue
        try:
            ts = int(e[0])
            vals = [None if v is None else float(v) for v in e[1:6]]
        except (TypeError, ValueError):
            continue
        out.append({"minute": ts - ts % 60, "open": vals[0], "high": vals[1], "low": vals[2], "close": vals[3], "volume_usd": vals[4]})
    out.sort(key=lambda c: c["minute"])
    return out


def store(conn: sqlite3.Connection, pool: str, candles: list[dict[str, Any]], now: float) -> int:
    n = 0
    for c in candles:
        conn.execute("INSERT OR REPLACE INTO pool_candles(pool, minute, open, high, low, close, volume_usd, fetched_at) VALUES(?,?,?,?,?,?,?,?)",
                     (pool, int(c["minute"]), c["open"], c["high"], c["low"], c["close"], c["volume_usd"], now))
        n += 1
    return n


def fetch_due(conn: sqlite3.Connection, budget: Budget, cfg: dict[str, Any], now: float, request: Callable[..., Any] = request_json,
              deadline: float | None = None) -> dict[str, Any]:
    """One bounded run: at most `max_fetches_per_run` due jobs, the gate asked first, the result written per job. Milestone 13:
    `deadline` (a `time.perf_counter()` value) ends the run early; the jobs left wait for the next run and are counted."""
    max_n = int(cfg.get("max_fetches_per_run", 20))
    limit = int(cfg.get("limit", 90))
    after_s = float(cfg.get("after_graduation_s", 3600))
    max_attempts = int(cfg.get("max_attempts", 3))
    timeout_s = float(cfg.get("timeout_s", 10))
    stats = {"due": 0, "fetched": 0, "candles": 0, "errors": 0, "blocked": 0, "gave_up": 0, "empty": 0, "time_budget": 0}
    jobs = conn.execute("SELECT mint, pool, graduated_at, attempts FROM candle_jobs WHERE done=0 AND due_at <= ? ORDER BY due_at LIMIT ?", (now, max_n)).fetchall()
    stats["due"] = int(conn.execute("SELECT COUNT(*) FROM candle_jobs WHERE done=0 AND due_at <= ?", (now,)).fetchone()[0])
    for k, j in enumerate(jobs):
        if deadline is not None and time.perf_counter() > deadline:
            stats["time_budget"] = len(jobs) - k
            break
        if not budget.peek("geckoterminal", "ohlcv").allowed:
            stats["blocked"] += 1
            db.write_tx(conn, lambda c: db.counter_bump_daily(c, KV_BLOCKED, 1, now), what="candles.blocked")
            break
        pool = str(j["pool"])
        before = int(float(j["graduated_at"]) + after_s + 60)
        url = f"{GECKO_OHLCV.format(pool=pool)}?aggregate=1&limit={limit}&before_timestamp={before}"
        try:
            payload = request(budget, "geckoterminal", "ohlcv", url, timeout_s=timeout_s)
        except Blocked:
            stats["blocked"] += 1
            db.write_tx(conn, lambda c: db.counter_bump_daily(c, KV_BLOCKED, 1, now), what="candles.blocked")
            break
        except HttpError as e:
            stats["errors"] += 1
            attempts = int(j["attempts"]) + 1
            gave_up = attempts >= max_attempts
            if gave_up:
                stats["gave_up"] += 1

            def _err(c: sqlite3.Connection, mint=str(j["mint"]), attempts=attempts, gave_up=gave_up, err=f"HTTP {e.status}: {str(e)[:120]}") -> None:
                c.execute("UPDATE candle_jobs SET attempts=?, last_attempt=?, done=?, last_error=? WHERE mint=?", (attempts, now, 1 if gave_up else 0, err, mint))
                if gave_up:
                    db.counter_bump_daily(c, KV_FAILED, 1, now)

            db.write_tx(conn, _err, what="candles.error")
            continue
        candles = parse_ohlcv(payload)
        if not candles:
            stats["empty"] += 1

        def _ok(c: sqlite3.Connection, mint=str(j["mint"]), pool=pool, candles=candles, attempts=int(j["attempts"]) + 1) -> None:
            n = store(c, pool, candles, now)
            c.execute("UPDATE candle_jobs SET attempts=?, last_attempt=?, done=1, candles=?, last_error=? WHERE mint=?",
                      (attempts, now, n, None if n else "no_candles", mint))
            db.counter_bump_daily(c, KV_FETCHED, 1, now)

        db.write_tx(conn, _ok, what="candles.store")
        stats["fetched"] += 1
        stats["candles"] += len(candles)
    return stats


def run(conn: sqlite3.Connection, budget: Budget, settings: Any, now: float, request: Callable[..., Any] = request_json, deadline: float | None = None) -> dict[str, Any]:
    cfg = (settings.config("measure") or {}).get("candles") or {}
    if not cfg.get("enabled", True):
        return {"enabled": False}
    t0 = time.perf_counter()
    added = db.write_tx(conn, lambda c: enqueue(c, now, float(cfg.get("after_graduation_s", 3600))), what="candles.enqueue")
    stats = fetch_due(conn, budget, cfg, now, request, deadline=deadline)
    db.write_tx(conn, lambda c: db.kv_set(c, KV_LAST_RUN, str(now), now), what="candles.kv")
    out = {"enabled": True, "enqueued": added, **stats, "seconds": round(time.perf_counter() - t0, 2)}
    log.info("candles.done", **out)
    return out


def status(conn: sqlite3.Connection, now: float) -> dict[str, Any]:
    if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='candle_jobs'").fetchone() is None:
        return {"exists": False}
    day = day_key(now)
    pending = int(conn.execute("SELECT COUNT(*) FROM candle_jobs WHERE done=0").fetchone()[0])
    due = int(conn.execute("SELECT COUNT(*) FROM candle_jobs WHERE done=0 AND due_at <= ?", (now,)).fetchone()[0])
    oldest = conn.execute("SELECT MIN(due_at) FROM candle_jobs WHERE done=0 AND due_at <= ?", (now,)).fetchone()[0]
    last = db.kv_get(conn, KV_LAST_RUN)
    return {"exists": True, "pending": pending, "due": due, "oldest_due_age_s": None if oldest is None else round(max(0.0, now - float(oldest))),
            "done_ok": int(conn.execute("SELECT COUNT(*) FROM candle_jobs WHERE done=1 AND candles > 0").fetchone()[0]),
            "done_empty": int(conn.execute("SELECT COUNT(*) FROM candle_jobs WHERE done=1 AND candles = 0 AND last_error = 'no_candles'").fetchone()[0]),
            "pool_unknown": int(conn.execute("SELECT COUNT(*) FROM candle_jobs WHERE last_error = 'pool_unknown'").fetchone()[0]),
            "failed": int(conn.execute("SELECT COUNT(*) FROM candle_jobs WHERE done=1 AND last_error IS NOT NULL AND last_error NOT IN ('pool_unknown', 'no_candles')").fetchone()[0]),
            "candles": int(conn.execute("SELECT COUNT(*) FROM pool_candles").fetchone()[0]),
            "fetched_today": int(db.kv_get(conn, f"{KV_FETCHED}.{day}", "0") or 0), "failed_today": int(db.kv_get(conn, f"{KV_FAILED}.{day}", "0") or 0),
            "blocked_today": int(db.kv_get(conn, f"{KV_BLOCKED}.{day}", "0") or 0),
            "last_run": None if last is None else float(last), "last_run_age_s": None if last is None else round(max(0.0, now - float(last)))}
