"""Per-day candidate maxima (milestone 6): what the engine saw of every watched coin and every ranked meta,
so `fart engine explain --day` and the daily report can show why there were (no) observations.

One row per UTC day per coin (key = mint) and per meta term (key = term) in `engine_candidates`, updated by
an UPSERT in the engine's decision transaction every tick: the highest `unique_buyers_5m`, `net_sol_5m`
and `trades_total`, the lowest and highest `largest_buyer_share_15m`, the last and the "ever" value of
`bundled`, whether both momentum thresholds were met in one tick (`passed_momentum`) and whether the whole
coin verdict passed in one tick (`passed_all`, i.e. an observation modulo the cooldown); for metas the
highest `burst_ratio` (and whether it was ever infinite), `sol_inflow`, `unique_buyers`, `n_coins` and
`distinct_creators`, and `passed_all` = the meta verdict passed. `reason` is the watchlist reason at first
sight (the entry path: probation, meta_burst, ...). Nothing here changes a decision: the engine writes these
rows after the verdicts and never reads them. This module is not a rule file.
"""
from __future__ import annotations

import sqlite3
from typing import Any

from ..util import day_key


def _keep_max(col: str) -> str:
    return f"{col} = CASE WHEN excluded.{col} IS NULL THEN {col} WHEN {col} IS NULL THEN excluded.{col} ELSE MAX({col}, excluded.{col}) END"


def _keep_min(col: str) -> str:
    return f"{col} = CASE WHEN excluded.{col} IS NULL THEN {col} WHEN {col} IS NULL THEN excluded.{col} ELSE MIN({col}, excluded.{col}) END"


def _tri(v: Any) -> int | None:
    return None if v is None else (1 if v else 0)


COIN_UPSERT = (
    "INSERT INTO engine_candidates(day, kind, key, first_as_of, last_as_of, ticks, reason, rules_version, max_unique_buyers_5m, max_net_sol_5m,"
    " min_largest_buyer_share_15m, max_largest_buyer_share_15m, bundled_last, bundled_ever, max_trades_total, passed_momentum, passed_all)"
    " VALUES(?, 'coin', ?, ?, ?, 1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
    " ON CONFLICT(day, kind, key) DO UPDATE SET last_as_of = excluded.last_as_of, ticks = ticks + 1, rules_version = excluded.rules_version,"
    " reason = COALESCE(reason, excluded.reason), "
    + ", ".join([_keep_max("max_unique_buyers_5m"), _keep_max("max_net_sol_5m"), _keep_min("min_largest_buyer_share_15m"),
                 _keep_max("max_largest_buyer_share_15m"), _keep_max("max_trades_total")])
    + ", bundled_last = excluded.bundled_last, bundled_ever = MAX(bundled_ever, excluded.bundled_ever),"
    " passed_momentum = MAX(passed_momentum, excluded.passed_momentum), passed_all = MAX(passed_all, excluded.passed_all)"
)

META_UPSERT = (
    "INSERT INTO engine_candidates(day, kind, key, first_as_of, last_as_of, ticks, rules_version, max_burst_ratio, burst_inf, max_sol_inflow,"
    " max_unique_buyers, max_n_coins, max_distinct_creators, passed_momentum, passed_all)"
    " VALUES(?, 'meta', ?, ?, ?, 1, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
    " ON CONFLICT(day, kind, key) DO UPDATE SET last_as_of = excluded.last_as_of, ticks = ticks + 1, rules_version = excluded.rules_version, "
    + ", ".join([_keep_max("max_burst_ratio"), _keep_max("max_sol_inflow"), _keep_max("max_unique_buyers"), _keep_max("max_n_coins"),
                 _keep_max("max_distinct_creators")])
    + ", burst_inf = MAX(burst_inf, excluded.burst_inf), passed_momentum = MAX(passed_momentum, excluded.passed_momentum),"
    " passed_all = MAX(passed_all, excluded.passed_all)"
)


def coin_row(f: dict[str, Any], reason: str | None, ccfg: dict[str, Any], passed_all: bool) -> dict[str, Any]:
    """The compact per-tick record of one coin's features (computed in the read phase, written later)."""
    buyers = int(f.get("unique_buyers_5m") or 0)
    net_sol = float(f.get("net_sol_5m") or 0.0)
    momentum = buyers >= int(ccfg["observe_min_unique_buyers_5m"]) and net_sol >= float(ccfg["observe_min_net_sol_5m"])
    return {"mint": f["mint"], "reason": reason, "unique_buyers_5m": buyers, "net_sol_5m": net_sol,
            "largest_buyer_share_15m": f.get("largest_buyer_share_15m"), "bundled": f.get("bundled"),
            "trades_total": int(f.get("trades_total") or 0), "passed_momentum": bool(momentum), "passed_all": bool(passed_all)}


def meta_row(m: dict[str, Any], bursting: bool, passed_all: bool) -> dict[str, Any]:
    """`bursting` = `decide.is_bursting()` (coins, creators and burst ratio in one tick), `passed_all` = the meta verdict."""
    return {"term": m["term"], "burst_ratio": m.get("burst_ratio"), "burst_inf": bool(m.get("burst_ratio_is_inf")),
            "sol_inflow": float(m.get("sol_inflow") or 0.0), "unique_buyers": int(m.get("unique_buyers") or 0),
            "n_coins": int(m.get("n_coins") or 0), "distinct_creators": int(m.get("distinct_creators") or 0),
            "passed_momentum": bool(bursting), "passed_all": bool(passed_all)}


def write_coins(conn: sqlite3.Connection, rows: list[dict[str, Any]], as_of: float, rules_version: str) -> int:
    day = day_key(as_of)
    n = 0
    for r in rows:
        conn.execute(COIN_UPSERT, (day, r["mint"], as_of, as_of, r.get("reason"), rules_version, r["unique_buyers_5m"], r["net_sol_5m"],
                                   r.get("largest_buyer_share_15m"), r.get("largest_buyer_share_15m"), _tri(r.get("bundled")),
                                   1 if r.get("bundled") is True else 0, r["trades_total"], 1 if r["passed_momentum"] else 0,
                                   1 if r["passed_all"] else 0))
        n += 1
    return n


def write_metas(conn: sqlite3.Connection, rows: list[dict[str, Any]], as_of: float, rules_version: str) -> int:
    day = day_key(as_of)
    n = 0
    for r in rows:
        conn.execute(META_UPSERT, (day, r["term"], as_of, as_of, rules_version, r.get("burst_ratio"), 1 if r["burst_inf"] else 0,
                                   r["sol_inflow"], r["unique_buyers"], r["n_coins"], r["distinct_creators"],
                                   1 if r["passed_momentum"] else 0, 1 if r["passed_all"] else 0))
        n += 1
    return n
