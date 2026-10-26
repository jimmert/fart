"""X shadow mode (milestone 6): what the X layer would have done, written next to the real tables.

With a bearer token and `X_MODE` unset or `shadow`, `fart-social` runs completely (mega watch, KOL watch,
velocity refresh, confirmation counts, fast-post matching) within its USD budget, but nothing of it reaches
`decisions`:

* a fast post that matches a burst of coin names becomes a row in `shadow_decisions` (the columns of
  `decisions` plus `would_kind`, the kind the row would have had: `fast_post`; `kind` is `shadow`), never a
  `decisions` row, so the notifier never sends it and the tracker never measures it;
* a confirmation count (counts/recent per key) becomes a row in `shadow_confirmations`, linked to the
  observe / observe_meta decision it would have confirmed, with the threshold `confirm.search_when_count_1h`
  of the moment and whether the last hour / the last 24 h reached it; `social_counts` stays empty, so
  `features.x_features()` sees nothing;
* the engine passes no mega-post feature and neutral X features to the decisions (`fart/engine/service.py`),
  whatever the social tables hold, so the decisions of a shadow day are the decisions of a day without X.

Both tables are append-only (triggers). `live` (the milestone-2 behaviour) needs `X_MODE=live` AND
`"x_live": true` in config/engine.json, which changes the rules fingerprint: X going live is a rules change
and opens a new segment in the week report (docs/DECISIONS.md, milestone 6).
"""
from __future__ import annotations

import json
import sqlite3
from typing import Any

from ..util import day_key, day_start

SHADOW_KIND = "shadow"


def write_shadow_decision(conn: sqlite3.Connection, as_of: float, created_at: float, would_kind: str, mint: str | None,
                          term: str | None, features: dict[str, Any], rules_version: str, verdict: str, reasons: list[str]) -> int:
    cur = conn.execute(
        "INSERT INTO shadow_decisions(as_of, created_at, kind, mint, term, features_json, rules_version, verdict, reasons_json, would_kind)"
        " VALUES(?,?,?,?,?,?,?,?,?,?)",
        (as_of, created_at, SHADOW_KIND, mint, term, json.dumps(features, default=str), rules_version, verdict, json.dumps(reasons), would_kind),
    )
    return int(cur.lastrowid)


def write_shadow_confirmation(conn: sqlite3.Connection, ts: float, key: dict[str, Any], n_last_hour: int | None, n_24h: int | None,
                              threshold: int, rules_version: str) -> int:
    """`key` is an entry of `x.confirmation_candidates()`: key, kind, query and the decision it belongs to."""
    c1 = 1 if n_last_hour is not None and int(n_last_hour) >= int(threshold) else 0
    c24 = 1 if n_24h is not None and int(n_24h) >= int(threshold) else 0
    cur = conn.execute(
        "INSERT INTO shadow_confirmations(ts, decision_id, decision_kind, decision_as_of, key, kind, n_last_hour, n_24h, query, threshold,"
        " confirmed_1h, confirmed_24h, rules_version) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (ts, key.get("decision_id"), key.get("decision_kind"), key.get("decision_as_of"), key["key"], key["kind"], n_last_hour, n_24h,
         key.get("query"), int(threshold), c1, c24, rules_version),
    )
    return int(cur.lastrowid)


def status_counters(conn: sqlite3.Connection, now: float) -> dict[str, Any]:
    """The shadow counters of `fart status`: totals and today's rows."""
    t0 = day_start(now)
    out: dict[str, Any] = {
        "decisions_total": int(conn.execute("SELECT COUNT(*) FROM shadow_decisions").fetchone()[0]),
        "decisions_today": int(conn.execute("SELECT COUNT(*) FROM shadow_decisions WHERE as_of >= ?", (t0,)).fetchone()[0]),
        "confirmations_today": int(conn.execute("SELECT COUNT(*) FROM shadow_confirmations WHERE ts >= ?", (t0,)).fetchone()[0]),
        "confirmed_1h_today": int(conn.execute(
            "SELECT COUNT(DISTINCT COALESCE(decision_id, key)) FROM shadow_confirmations WHERE ts >= ? AND confirmed_1h=1", (t0,)).fetchone()[0]),
        "confirmed_24h_today": int(conn.execute(
            "SELECT COUNT(DISTINCT COALESCE(decision_id, key)) FROM shadow_confirmations WHERE ts >= ? AND confirmed_24h=1", (t0,)).fetchone()[0]),
        "day": day_key(now),
    }
    last = conn.execute("SELECT MAX(as_of) FROM shadow_decisions").fetchone()[0]
    out["last_decision_at"] = None if last is None else float(last)
    return out
