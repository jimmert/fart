"""Observations ('observe' decisions) and watchlist promotions from features. No delivery."""
from __future__ import annotations

import json
import sqlite3
from typing import Any

from ..ingest import watch


def _cooldown_ok(conn: sqlite3.Connection, kind: str, key_col: str, key: str, as_of: float, cooldown_s: float) -> bool:
    row = conn.execute(
        f"SELECT MAX(as_of) FROM decisions WHERE kind=? AND {key_col}=? AND verdict='observe'", (kind, key)
    ).fetchone()
    return row[0] is None or as_of - float(row[0]) >= cooldown_s


def write_features(conn: sqlite3.Connection, kind: str, key: str, mint: str | None, as_of: float, computed_at: float,
                   rules_version: str, features: dict[str, Any]) -> int:
    cur = conn.execute(
        "INSERT INTO features_asof(kind, key, mint, as_of, computed_at, rules_version, json) VALUES(?,?,?,?,?,?,?)",
        (kind, key, mint, as_of, computed_at, rules_version, json.dumps(features, default=str)),
    )
    return int(cur.lastrowid)


def write_decision(conn: sqlite3.Connection, as_of: float, created_at: float, kind: str, mint: str | None, term: str | None,
                   features: dict[str, Any], rules_version: str, verdict: str, reasons: list[str]) -> int:
    cur = conn.execute(
        "INSERT INTO decisions(as_of, created_at, kind, mint, term, features_json, rules_version, verdict, reasons_json)"
        " VALUES(?,?,?,?,?,?,?,?,?)",
        (as_of, created_at, kind, mint, term, json.dumps(features, default=str), rules_version, verdict, json.dumps(reasons)),
    )
    return int(cur.lastrowid)


def coin_verdict(f: dict[str, Any], cfg: dict[str, Any]) -> tuple[bool, list[str]]:
    """Threshold check for a coin observation. Returns (passes, reasons)."""
    reasons: list[str] = []
    ok = True
    if f["unique_buyers_5m"] >= int(cfg["observe_min_unique_buyers_5m"]):
        reasons.append(f"unique_buyers_5m={f['unique_buyers_5m']}>={cfg['observe_min_unique_buyers_5m']}")
    else:
        ok = False
    if f["net_sol_5m"] >= float(cfg["observe_min_net_sol_5m"]):
        reasons.append(f"net_sol_5m={f['net_sol_5m']}>={cfg['observe_min_net_sol_5m']}")
    else:
        ok = False
    share = f.get("largest_buyer_share_15m")
    if share is not None and share > float(cfg["observe_max_largest_buyer_share"]):
        ok = False
        reasons.append(f"largest_buyer_share={share:.2f}>{cfg['observe_max_largest_buyer_share']}")
    if cfg.get("observe_require_not_bundled", True) and f.get("bundled") is True:
        ok = False
        reasons.append("bundled")
    return ok, reasons


def meta_verdict(m: dict[str, Any], cfg: dict[str, Any]) -> tuple[bool, list[str]]:
    reasons: list[str] = []
    ok = m["n_coins"] >= int(cfg["min_coins"]) and m["distinct_creators"] >= int(cfg["min_distinct_creators"])
    if not ok:
        return False, ["below_min_coins_or_creators"]
    bursting = m["burst_ratio_is_inf"] or (m["burst_ratio"] is not None and m["burst_ratio"] >= float(cfg["burst_ratio"]))
    if bursting:
        reasons.append(f"burst_ratio={'inf' if m['burst_ratio_is_inf'] else m['burst_ratio']}")
    money = m["sol_inflow"] >= float(cfg["observe_min_sol_inflow"]) and m["unique_buyers"] >= int(cfg["observe_min_unique_buyers"])
    if money:
        reasons.append(f"sol_inflow={m['sol_inflow']} unique_buyers={m['unique_buyers']}")
    return bool(bursting and money), reasons


def is_bursting(m: dict[str, Any], cfg: dict[str, Any]) -> bool:
    return (m["n_coins"] >= int(cfg["min_coins"]) and m["distinct_creators"] >= int(cfg["min_distinct_creators"])
            and (m["burst_ratio_is_inf"] or (m["burst_ratio"] is not None and m["burst_ratio"] >= float(cfg["burst_ratio"]))))


def observe_coin(conn: sqlite3.Connection, f: dict[str, Any], as_of: float, now: float, cfg: dict[str, Any],
                 cooldown_s: float, rules_version: str) -> int | None:
    ok, reasons = coin_verdict(f, cfg)
    if not ok or not _cooldown_ok(conn, "observe", "mint", f["mint"], as_of, cooldown_s):
        return None
    write_features(conn, "coin", f["mint"], f["mint"], as_of, now, rules_version, f)
    return write_decision(conn, as_of, now, "observe", f["mint"], None, f, rules_version, "observe", reasons)


def observe_meta(conn: sqlite3.Connection, m: dict[str, Any], as_of: float, now: float, cfg: dict[str, Any],
                 cooldown_s: float, rules_version: str) -> int | None:
    ok, reasons = meta_verdict(m, cfg)
    if not ok or not _cooldown_ok(conn, "observe_meta", "term", m["term"], as_of, cooldown_s):
        return None
    write_features(conn, "meta", m["term"], None, as_of, now, rules_version, m)
    return write_decision(conn, as_of, now, "observe_meta", None, m["term"], m, rules_version, "observe", reasons)


def wants_promotion(f: dict[str, Any], wcfg: dict[str, Any]) -> bool:
    """Pure threshold check (no database): enough early buyers for 'early_buyers'."""
    return f["unique_buyers_5m"] >= int(wcfg.get("early_buyers_threshold", 10))


def promote_watchlist(conn: sqlite3.Connection, f: dict[str, Any], now: float, wcfg: dict[str, Any]) -> str | None:
    """Probation coin with enough early buyers -> 'early_buyers' (24 h). Call inside a write transaction."""
    row = conn.execute("SELECT reason FROM watchlist WHERE mint=? AND active=1", (f["mint"],)).fetchone()
    if row is None or row["reason"] != "probation":
        return None
    if wants_promotion(f, wcfg):
        watch.add(conn, f["mint"], "early_buyers", now, float(wcfg.get("ttl_s", 86400)), int(wcfg.get("cap", 200)))
        return "early_buyers"
    return None
