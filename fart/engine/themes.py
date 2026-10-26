"""The engine's ranked themes as a snapshot for the website (milestone 10).

Every tick the engine clusters the coins of the last three hours, computes the money and the wallets behind every term
and ranks the terms (`features.recent_coins`, `meta.cluster`, `features.meta_features`, `features.rank_metas`: rule
files, imported here, never changed). Milestone 9 let the website repeat that work on every cache miss: minutes per
visit on the VPS database (7,486 coins in the window, one baseline scan per term). Milestone 10 makes the engine write
the ranked metas it already has as JSON in kv (`engine.metas_latest`, inside the same short decision transaction as
`engine.meta_burst_sets` and the heartbeat), and the website's themes card reads only that value and computes nothing.

The snapshot carries `as_of` (the tick), the window, the number of coins in it, and per term the fields the card shows:
the counts, the money, the burst ratio, whether the engine calls it bursting, whether the term is in the engine's frozen
meta_burst choice, whether it had an observation today and three example names. A snapshot older than `MAX_AGE_S` is
never shown as current: the card says the themes are not being computed and how old the last snapshot is.

The daily report and `fart engine explain` keep their own path (`engine_candidates`, the decisions).
"""
from __future__ import annotations

import json
import sqlite3
from typing import Any

from .. import db
from ..util import day_start
from . import decide, features
from .meta import cluster, load_stopwords

KV_KEY = "engine.metas_latest"
MAX_AGE_S = 300          # older than this the website says the themes are not being computed
EXAMPLES = 3             # example coin names per term (the youngest first)
SNAPSHOT_VERSION = 1


def compute_metas(conn: sqlite3.Connection, as_of: float, mcfg: dict[str, Any], stopwords: set[str]) -> tuple[list[dict[str, Any]], dict[str, list[dict]], list[dict[str, Any]]]:
    """The engine's step 5: the recent coins, their clusters per term and the ranked metas (top `max_terms`)."""
    recent = features.recent_coins(conn, as_of, float(mcfg.get("window_s", 10800)))
    clusters = cluster(recent, stopwords, int(mcfg.get("min_token_len", 4)))
    metas = []
    for term, members in clusters.items():
        if len(members) < int(mcfg.get("min_coins", 3)):
            continue
        metas.append(features.meta_features(conn, term, members, as_of, mcfg, stopwords))
    metas = features.rank_metas(metas)[: int(mcfg.get("max_terms", 50))]
    return recent, clusters, metas


def observed_terms_today(conn: sqlite3.Connection, as_of: float) -> set[str]:
    """The terms with a theme observation on the UTC day of `as_of` (the `decisions_as_of` index serves it)."""
    return {str(r[0]) for r in conn.execute("SELECT DISTINCT term FROM decisions WHERE kind='observe_meta' AND as_of >= ? AND term IS NOT NULL", (day_start(as_of),))}


def snapshot(metas: list[dict[str, Any]], clusters: dict[str, list[dict]], as_of: float, mcfg: dict[str, Any], coins_in_window: int,
             watched_terms: set[str], observed_today: set[str], rules_version: str) -> dict[str, Any]:
    """The JSON document the engine writes: pure, no database."""
    rows = []
    for m in metas:
        members = sorted(clusters.get(m["term"], []), key=lambda x: float(x.get("t") or 0.0), reverse=True)
        examples = [(c.get("name") or "?") + (f" (${c['symbol']})" if c.get("symbol") else "") for c in members[:EXAMPLES]]
        rows.append({
            "term": m["term"], "n_coins": int(m["n_coins"]), "distinct_creators": int(m["distinct_creators"]), "graduations": int(m["graduations"]),
            "sol_inflow": m["sol_inflow"], "net_sol": m["net_sol"], "unique_buyers": int(m["unique_buyers"]), "mints_with_trades": int(m["mints_with_trades"]),
            "burst_ratio": m["burst_ratio"], "burst_ratio_is_inf": bool(m["burst_ratio_is_inf"]), "first_seen": m.get("first_seen"),
            "bursting": bool(decide.is_bursting(m, mcfg)), "watched": m["term"] in watched_terms, "observed_today": m["term"] in observed_today,
            "examples": examples,
        })
    return {"version": SNAPSHOT_VERSION, "as_of": float(as_of), "window_s": float(mcfg.get("window_s", 10800)), "coins_in_window": int(coins_in_window),
            "clusters_total": len(metas), "rules_version": rules_version, "metas": rows}


def build(conn: sqlite3.Connection, settings: Any, as_of: float, watched_terms: set[str] | None = None, rules_version: str | None = None) -> dict[str, Any]:
    """Compute and snapshot in one go (tests, fixtures, a one-off): the engine itself calls `compute_metas` in its read
    snapshot and `snapshot` in its write, with the sets it holds in memory."""
    from ..version import rules_version as rv

    mcfg = settings.config("engine").get("meta") or {}
    stopwords = load_stopwords(settings.config("stopwords"))
    recent, clusters, metas = compute_metas(conn, as_of, mcfg, stopwords)
    if watched_terms is None:
        watched_terms = burst_set_terms(conn)
    return snapshot(metas, clusters, as_of, mcfg, len(recent), watched_terms, observed_terms_today(conn, as_of), rules_version or rv())


def burst_set_terms(conn: sqlite3.Connection) -> set[str]:
    raw = db.kv_get(conn, "engine.meta_burst_sets")
    if not raw:
        return set()
    try:
        d = json.loads(raw)
    except json.JSONDecodeError:
        return set()
    return {str(k) for k in d} if isinstance(d, dict) else set()


def write(conn: sqlite3.Connection, snap: dict[str, Any], now: float) -> None:
    """Inside the engine's write transaction."""
    db.kv_set(conn, KV_KEY, json.dumps(snap, separators=(",", ":"), default=str), now)


def read(conn: sqlite3.Connection) -> dict[str, Any] | None:
    """The last snapshot, or None when there is none or it is unreadable (the website then says so)."""
    raw = db.kv_get(conn, KV_KEY)
    if not raw:
        return None
    try:
        d = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(d, dict) or not isinstance(d.get("metas"), list):
        return None
    try:
        d["as_of"] = float(d["as_of"])
    except (KeyError, TypeError, ValueError):
        return None
    return d
