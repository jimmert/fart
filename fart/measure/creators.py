"""`creator_stats` (milestone 12, B.4): per creator wallet what our own data says about its coins, recomputed by a step of
`fart measure track`, never by the engine.

Per creator: the number of coins, graduations, winners and non-winners (the 24 h outcome of the graduations), bundled
coins (the `any` flag of fart/research/features.py on the trades we have), early dev sells (the creator sold within
`dev_sell_window_s` of the create), the first and last coin. The step is incremental and bounded: it walks the coins
received since its cursor (kv `creator_stats.coins_cursor`), the migrations received since its cursor and the graduation
outcomes measured since its cursor, recomputes the creators they belong to, at most `max_creators_per_run` per run, and
moves the cursors only past what it did; the rest follows in the next run and the backlog is visible (`fart status`). The
bundle and dev-sell scan of a creator is bounded to its last 50 coins (`SCAN_CAP`, flagged in the row's json). The
terminal reads the table; the research harness never does (it computes the same figures as-of the entry time).
"""
from __future__ import annotations

import json
import sqlite3
import time
from typing import Any

from .. import db
from ..log import get_logger
from ..research import features as rf

log = get_logger("creators")

ET = "COALESCE(block_time, received_at)"
SCAN_CAP = 50
KV_COINS = "creator_stats.coins_cursor"
KV_MIGRATIONS = "creator_stats.migrations_cursor"
KV_OUTCOMES = "creator_stats.outcomes_cursor"
KV_LAST_RUN = "creator_stats.last_run"
KV_BACKLOG = "creator_stats.backlog"
KV_UPDATED = "creator_stats.updated"
WRITE_BATCH = 200


def compute_creator(conn: sqlite3.Connection, creator: str, now: float, method_version: str, h24: int, dev_sell_window_s: float, graduation_sol: float,
                    scan_cap: int = SCAN_CAP) -> dict[str, Any]:
    rows = conn.execute(f"SELECT mint, {ET} AS t, dev_buy_sol, slot FROM coins WHERE creator=? ORDER BY t", (creator,)).fetchall()
    mints = [str(r["mint"]) for r in rows]
    grads = winners = not_winners = 0
    for chunk in rf.iter_chunks(mints, 400):
        marks = ",".join("?" for _ in chunk)
        grads += int(conn.execute(f"SELECT COUNT(*) FROM migrations WHERE mint IN ({marks})", chunk).fetchone()[0])
        for r in conn.execute(f"SELECT hit_100k FROM outcomes WHERE subject='graduation' AND horizon_s=? AND method_version=? AND mint IN ({marks})", (int(h24), method_version, *chunk)):
            if r["hit_100k"] == 1:
                winners += 1
            elif r["hit_100k"] == 0:
                not_winners += 1
    scan = rows[-scan_cap:] if scan_cap and len(rows) > scan_cap else rows
    bundled = dumped = 0
    for r in scan:
        tr = rf.load_trades(conn, str(r["mint"]))
        if rf.bundle_flags(tr, now, float(r["t"]), r["dev_buy_sol"], r["slot"], graduation_sol=graduation_sol, creator=creator)["bundled_any"] is True:
            bundled += 1
        if rf.dev_sell(tr, now, float(r["t"]), creator, dev_sell_window_s)["dev_sold_within_window"]:
            dumped += 1
    return {"creator": creator, "coins": len(rows), "graduations": grads, "winners": winners, "not_winners": not_winners, "bundled": bundled, "dev_sold_early": dumped,
            "first_coin_at": float(rows[0]["t"]) if rows else None, "last_coin_at": float(rows[-1]["t"]) if rows else None, "computed_at": now,
            "json": json.dumps({"scan_capped": len(scan) < len(rows), "scanned": len(scan),
                                "track_record": ("good" if (grads > 0 or winners > 0) and bundled == 0 and dumped == 0 else
                                                 ("bad" if (bundled > 0 or dumped > 0) else ("none" if not rows else "neutral")))})}


def write_rows(conn: sqlite3.Connection, rows: list[dict[str, Any]]) -> None:
    for r in rows:
        conn.execute("INSERT INTO creator_stats(creator, coins, graduations, winners, not_winners, bundled, dev_sold_early, first_coin_at, last_coin_at, computed_at, json)"
                     " VALUES(?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(creator) DO UPDATE SET coins=excluded.coins, graduations=excluded.graduations, winners=excluded.winners,"
                     " not_winners=excluded.not_winners, bundled=excluded.bundled, dev_sold_early=excluded.dev_sold_early, first_coin_at=excluded.first_coin_at,"
                     " last_coin_at=excluded.last_coin_at, computed_at=excluded.computed_at, json=excluded.json",
                     (r["creator"], r["coins"], r["graduations"], r["winners"], r["not_winners"], r["bundled"], r["dev_sold_early"], r["first_coin_at"], r["last_coin_at"],
                      r["computed_at"], r["json"]))


def pending_creators(conn: sqlite3.Connection, limit: int) -> tuple[list[str], dict[str, float], int]:
    """The creators to recompute (newest event first is NOT the order: oldest events first so the cursors move monotonically),
    the new cursor per source once they are done, and the backlog left behind."""
    cur_c = float(db.kv_get(conn, KV_COINS, "0") or 0)
    cur_m = float(db.kv_get(conn, KV_MIGRATIONS, "0") or 0)
    cur_o = float(db.kv_get(conn, KV_OUTCOMES, "0") or 0)
    seen: dict[str, None] = {}
    cursors = {KV_COINS: cur_c, KV_MIGRATIONS: cur_m, KV_OUTCOMES: cur_o}
    backlog = 0
    # coins received since the cursor
    rows = conn.execute("SELECT creator, received_at FROM coins WHERE received_at > ? AND creator IS NOT NULL ORDER BY received_at LIMIT ?", (cur_c, limit + 1)).fetchall()
    if len(rows) > limit:
        backlog += 1
        rows = rows[:limit]
    for r in rows:
        seen.setdefault(str(r["creator"]), None)
        cursors[KV_COINS] = max(cursors[KV_COINS], float(r["received_at"]))
    room = max(0, limit - len(seen))
    rows = conn.execute("SELECT c.creator, m.received_at FROM migrations m JOIN coins c ON c.mint = m.mint WHERE m.received_at > ? AND c.creator IS NOT NULL"
                        " ORDER BY m.received_at LIMIT ?", (cur_m, room + 1)).fetchall()
    if len(rows) > room:
        backlog += 1
        rows = rows[:room]
    for r in rows:
        seen.setdefault(str(r["creator"]), None)
        cursors[KV_MIGRATIONS] = max(cursors[KV_MIGRATIONS], float(r["received_at"]))
    room = max(0, limit - len(seen))
    rows = conn.execute("SELECT c.creator, o.measured_at FROM outcomes o JOIN coins c ON c.mint = o.mint WHERE o.subject='graduation' AND o.measured_at > ?"
                        " AND c.creator IS NOT NULL ORDER BY o.measured_at LIMIT ?", (cur_o, room + 1)).fetchall()
    if len(rows) > room:
        backlog += 1
        rows = rows[:room]
    for r in rows:
        seen.setdefault(str(r["creator"]), None)
        cursors[KV_OUTCOMES] = max(cursors[KV_OUTCOMES], float(r["measured_at"]))
    return list(seen), cursors, backlog


def update(conn: sqlite3.Connection, settings: Any, now: float, max_creators: int | None = None, time_budget_s: float = 60.0) -> dict[str, Any]:
    """One bounded step: recompute the pending creators, move the cursors, record the backlog. Returns the summary."""
    mcfg = settings.config("measure") or {}
    ccfg = mcfg.get("creator_stats") or {}
    limit = int(max_creators if max_creators is not None else ccfg.get("max_creators_per_run", 2000))
    window = float(ccfg.get("dev_sell_window_s", rf.DEV_SELL_WINDOW_S))
    mv = str(mcfg.get("method_version", "m1.0"))
    h24 = max(int(h) for h in mcfg.get("horizons_s") or [86400])
    pf = settings.config("pumpfun")
    grad_sol = float(pf["graduation_real_sol"]) / float(10 ** int(pf.get("sol_decimals", 9)))
    t0 = time.perf_counter()
    creators, cursors, backlog_sources = pending_creators(conn, limit)
    done = 0
    rows: list[dict[str, Any]] = []
    stopped_early = False
    for i, cr in enumerate(creators):
        if time.perf_counter() - t0 > time_budget_s:
            stopped_early = True
            break
        rows.append(compute_creator(conn, cr, now, mv, h24, window, grad_sol))
        done += 1
        if len(rows) >= WRITE_BATCH:
            db.write_tx(conn, lambda c, rs=list(rows): write_rows(c, rs), what="creator_stats")
            rows = []
    if rows:
        db.write_tx(conn, lambda c, rs=list(rows): write_rows(c, rs), what="creator_stats")

    def _kv(c: sqlite3.Connection) -> None:
        if not stopped_early:
            for k, v in cursors.items():
                db.kv_set(c, k, str(v), now)
        db.kv_set(c, KV_LAST_RUN, str(now), now)
        db.kv_set(c, KV_BACKLOG, str(backlog_sources + (1 if stopped_early else 0)), now)
        db.counter_bump_daily(c, KV_UPDATED, done, now)

    db.write_tx(conn, _kv, what="creator_stats.kv")
    out = {"creators_pending": len(creators), "creators_updated": done, "stopped_early": stopped_early, "backlog_sources": backlog_sources,
           "seconds": round(time.perf_counter() - t0, 2), "limit": limit}
    log.info("creator_stats.done", **out)
    return out


def status(conn: sqlite3.Connection, now: float) -> dict[str, Any]:
    """For `fart status` and the owner page: rows, the last run, the backlog flag, updated today."""
    from ..util import day_key

    if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='creator_stats'").fetchone() is None:
        return {"exists": False}
    last = db.kv_get(conn, KV_LAST_RUN)
    return {"exists": True, "rows": int(conn.execute("SELECT COUNT(*) FROM creator_stats").fetchone()[0]),
            "last_run": None if last is None else float(last), "last_run_age_s": None if last is None else round(max(0.0, now - float(last))),
            "backlog": int(db.kv_get(conn, KV_BACKLOG, "0") or 0), "updated_today": int(db.kv_get(conn, f"{KV_UPDATED}.{day_key(now)}", "0") or 0),
            "updated_total": int(db.kv_get(conn, KV_UPDATED, "0") or 0),
            "with_track_record": int(conn.execute("SELECT COUNT(*) FROM creator_stats WHERE graduations > 0 OR winners > 0").fetchone()[0])}


def lookup(conn: sqlite3.Connection, creators: list[str]) -> dict[str, dict[str, Any]]:
    """creator -> row (for the terminal feed); missing creators are absent (unknown, never zero)."""
    out: dict[str, dict[str, Any]] = {}
    uniq = sorted({c for c in creators if c})
    for chunk in rf.iter_chunks(uniq, 400):
        marks = ",".join("?" for _ in chunk)
        for r in conn.execute(f"SELECT * FROM creator_stats WHERE creator IN ({marks})", chunk):
            d = dict(r)
            try:
                d["track_record"] = (json.loads(d.get("json") or "{}") or {}).get("track_record")
            except ValueError:
                d["track_record"] = None
            out[str(d["creator"])] = d
    return out
