"""Event-time features. Every function takes `as_of` and only reads rows knowable at that time:
received_at <= as_of and (block_time IS NULL OR block_time <= as_of). See db.asof_clause.
Missing data is None, never 0, unless a count of nothing is genuinely zero.
"""
from __future__ import annotations

import sqlite3
from typing import Any

from ..chain.curve import CurveParams, CurveState, dev_buy_fraction_of_curve, graduation_progress
from ..db import asof_clause
from . import meta as meta_mod

ASOF = asof_clause()


def _event_time(alias: str = "") -> str:
    a = f"{alias}." if alias else ""
    return f"COALESCE({a}block_time, {a}received_at)"


def coin_row(conn: sqlite3.Connection, mint: str, as_of: float) -> sqlite3.Row | None:
    return conn.execute(f"SELECT * FROM coins WHERE mint=? AND {ASOF}", (mint, as_of, as_of)).fetchone()


def migration_row(conn: sqlite3.Connection, mint: str, as_of: float) -> sqlite3.Row | None:
    return conn.execute(f"SELECT * FROM migrations WHERE mint=? AND {ASOF}", (mint, as_of, as_of)).fetchone()


def _window_stats(conn: sqlite3.Connection, mint: str, as_of: float, window_s: float) -> dict[str, Any]:
    et = _event_time()
    row = conn.execute(
        f"SELECT COUNT(*) AS n, COUNT(DISTINCT CASE WHEN is_buy=1 THEN buyer END) AS buyers,"
        f" COALESCE(SUM(CASE WHEN is_buy=1 THEN sol END),0) AS buy_sol, COALESCE(SUM(CASE WHEN is_buy=0 THEN sol END),0) AS sell_sol,"
        f" SUM(is_buy=1) AS buys, SUM(is_buy=0) AS sells"
        f" FROM trades WHERE mint=? AND {ASOF} AND {et} > ? AND {et} <= ?",
        (mint, as_of, as_of, as_of - window_s, as_of),
    ).fetchone()
    n = int(row["n"])
    return {
        "trades": n,
        "unique_buyers": int(row["buyers"]) if n else 0,
        "buy_sol": float(row["buy_sol"]),
        "sell_sol": float(row["sell_sol"]),
        "net_sol": float(row["buy_sol"]) - float(row["sell_sol"]),
        "buys": int(row["buys"] or 0),
        "sells": int(row["sells"] or 0),
    }


def coin_features(conn: sqlite3.Connection, mint: str, as_of: float, params: CurveParams, cfg: dict[str, Any]) -> dict[str, Any] | None:
    """Momentum + bundling features for one mint as of `as_of`. None when the coin is unknown then."""
    coin = coin_row(conn, mint, as_of)
    if coin is None:
        return None
    created = coin["block_time"] if coin["block_time"] is not None else coin["received_at"]
    mig = migration_row(conn, mint, as_of)
    f: dict[str, Any] = {
        "mint": mint,
        "as_of": as_of,
        "name": coin["name"],
        "symbol": coin["symbol"],
        "creator": coin["creator"],
        "created_at": created,
        "created_time_source": "chain" if coin["block_time"] is not None else "received",
        "age_s": round(as_of - created, 1),
        "dev_buy_sol": coin["dev_buy_sol"],
        "dev_buy_frac_of_curve": dev_buy_fraction_of_curve(coin["dev_buy_sol"], params),
        "initial_mcap_sol": coin["initial_mcap_sol"],
        "graduated": mig is not None,
        "graduation_time": None if mig is None else (mig["block_time"] if mig["block_time"] is not None else mig["received_at"]),
    }
    for label, w in (("1m", 60), ("5m", 300), ("15m", 900)):
        st = _window_stats(conn, mint, as_of, w)
        f[f"unique_buyers_{label}"] = st["unique_buyers"]
        f[f"net_sol_{label}"] = round(st["net_sol"], 4)
        f[f"buy_sol_{label}"] = round(st["buy_sol"], 4)
        f[f"buys_{label}"] = st["buys"]
        f[f"sells_{label}"] = st["sells"]
        f[f"trades_{label}"] = st["trades"]
    f["buy_sell_ratio_15m"] = (f["buys_15m"] / f["sells_15m"]) if f["sells_15m"] else (float(f["buys_15m"]) if f["buys_15m"] else None)

    et = _event_time()
    # largest buyer share (of buy SOL in the last 15 min)
    row = conn.execute(
        f"SELECT buyer, SUM(sol) AS s FROM trades WHERE mint=? AND is_buy=1 AND {ASOF} AND {et} > ? AND {et} <= ?"
        f" GROUP BY buyer ORDER BY s DESC LIMIT 1",
        (mint, as_of, as_of, as_of - 900, as_of),
    ).fetchone()
    f["largest_buyer_share_15m"] = (float(row["s"]) / f["buy_sol_15m"]) if row is not None and f["buy_sol_15m"] > 0 else None
    f["largest_buyer_15m"] = row["buyer"] if row is not None else None

    # creator sold fraction (tokens sold / tokens bought by the creator, all history as of)
    if coin["creator"]:
        cr = conn.execute(
            f"SELECT COALESCE(SUM(CASE WHEN is_buy=1 THEN tokens END),0) AS bought, COALESCE(SUM(CASE WHEN is_buy=0 THEN tokens END),0) AS sold"
            f" FROM trades WHERE mint=? AND buyer=? AND {ASOF}",
            (mint, coin["creator"], as_of, as_of),
        ).fetchone()
        bought = float(cr["bought"]) + (0.0 if coin["dev_buy_sol"] is None else 0.0)
        f["creator_sold_frac"] = (float(cr["sold"]) / bought) if bought > 0 else (1.0 if float(cr["sold"]) > 0 else None)
    else:
        f["creator_sold_frac"] = None

    # latest curve state knowable as of
    last = conn.execute(
        f"SELECT virtual_sol, virtual_token, {et} AS t FROM trades WHERE mint=? AND {ASOF} AND virtual_sol IS NOT NULL"
        f" ORDER BY {et} DESC, received_at DESC LIMIT 1",
        (mint, as_of, as_of),
    ).fetchone()
    if last is not None:
        st = CurveState(int(last["virtual_sol"]), int(last["virtual_token"]))
        f["price_sol"] = st.price_sol(params)
        f["mcap_sol"] = round(st.mcap_sol(params), 4)
        f["graduation_progress"] = round(graduation_progress(st, params), 4)
        f["last_trade_age_s"] = round(as_of - float(last["t"]), 1)
    else:
        f["price_sol"] = None
        f["mcap_sol"] = None
        f["graduation_progress"] = None
        f["last_trade_age_s"] = None
    f["trades_total"] = conn.execute(f"SELECT COUNT(*) FROM trades WHERE mint=? AND {ASOF}", (mint, as_of, as_of)).fetchone()[0]

    # bundled: graduated within N s of create, or dev buy >= X of the curve
    bw = float(cfg.get("bundled_graduation_window_s", 60))
    bf = float(cfg.get("bundled_dev_buy_fraction", 0.5))
    fast_grad = f["graduated"] and f["graduation_time"] is not None and (f["graduation_time"] - created) <= bw
    big_dev = f["dev_buy_frac_of_curve"] is not None and f["dev_buy_frac_of_curve"] >= bf
    if fast_grad or big_dev:
        f["bundled"] = True
    elif f["dev_buy_frac_of_curve"] is None:
        f["bundled"] = None  # the dev-buy criterion cannot be ruled out without dev-buy information
    elif f["graduated"] or f["age_s"] > bw:
        f["bundled"] = False  # small dev buy and no (possible) instant graduation
    else:
        f["bundled"] = None  # too young to rule out an instant graduation
    f["bundled_reasons"] = [r for r, ok in (("fast_graduation", fast_grad), ("dev_buy_ge_half_curve", big_dev)) if ok]

    # creator history from our own data (creates by the same wallet before this coin)
    if coin["creator"]:
        f["creator_prior_coins_24h"] = conn.execute(
            f"SELECT COUNT(*) FROM coins WHERE creator=? AND mint<>? AND {ASOF} AND {et} > ? AND {et} <= ?",
            (coin["creator"], mint, as_of, as_of, created - 86400, created),
        ).fetchone()[0]
    else:
        f["creator_prior_coins_24h"] = None
    return f


def recent_coins(conn: sqlite3.Connection, as_of: float, window_s: float) -> list[dict[str, Any]]:
    et = _event_time()
    rows = conn.execute(
        f"SELECT mint, name, symbol, creator, {et} AS t FROM coins WHERE {ASOF} AND {et} > ? AND {et} <= ?",
        (as_of, as_of, as_of - window_s, as_of),
    ).fetchall()
    return [dict(r) for r in rows]


def meta_features(conn: sqlite3.Connection, term: str, members: list[dict[str, Any]], as_of: float, cfg: dict[str, Any],
                  stopwords: set[str]) -> dict[str, Any]:
    """Money and wallets behind a term over the window, versus the term's own earlier baseline."""
    window_s = float(cfg.get("window_s", 10800))
    mints = [m["mint"] for m in members]
    et = _event_time()
    marks = ",".join("?" for _ in mints)
    grads = conn.execute(
        f"SELECT COUNT(*) FROM migrations WHERE mint IN ({marks}) AND {ASOF}", (*mints, as_of, as_of)
    ).fetchone()[0] if mints else 0
    tr = conn.execute(
        f"SELECT COALESCE(SUM(CASE WHEN is_buy=1 THEN sol END),0) AS buy_sol, COALESCE(SUM(CASE WHEN is_buy=0 THEN sol END),0) AS sell_sol,"
        f" COUNT(DISTINCT CASE WHEN is_buy=1 THEN buyer END) AS buyers, COUNT(DISTINCT mint) AS mints_with_trades"
        f" FROM trades WHERE mint IN ({marks}) AND {ASOF} AND {et} > ? AND {et} <= ?",
        (*mints, as_of, as_of, as_of - window_s, as_of),
    ).fetchone() if mints else {"buy_sol": 0, "sell_sol": 0, "buyers": 0, "mints_with_trades": 0}
    # baseline: coins/hour with this term between baseline_from and baseline_to seconds ago
    b_from = float(cfg.get("baseline_from_s", 172800))
    b_to = float(cfg.get("baseline_to_s", 21600))
    base_rows = conn.execute(
        f"SELECT name, symbol FROM coins WHERE {ASOF} AND {et} > ? AND {et} <= ?",
        (as_of, as_of, as_of - b_from, as_of - b_to),
    ).fetchall()
    min_len = int(cfg.get("min_token_len", 4))
    base_n = sum(1 for r in base_rows if term in meta_mod.tokenize(r["name"], r["symbol"], stopwords, min_len))
    base_hours = max(1e-9, (b_from - b_to) / 3600.0)
    base_rate = base_n / base_hours
    cur_rate = len(mints) / (window_s / 3600.0)
    burst = (cur_rate / base_rate) if base_rate > 0 else (float("inf") if cur_rate > 0 else 0.0)
    return {
        "term": term,
        "as_of": as_of,
        "n_coins": len(mints),
        "distinct_creators": len({m.get("creator") for m in members if m.get("creator")}),
        "graduations": int(grads),
        "sol_inflow": round(float(tr["buy_sol"]), 4),
        "net_sol": round(float(tr["buy_sol"]) - float(tr["sell_sol"]), 4),
        "unique_buyers": int(tr["buyers"]),
        "mints_with_trades": int(tr["mints_with_trades"]),
        "baseline_coins_per_h": round(base_rate, 4),
        "current_coins_per_h": round(cur_rate, 4),
        "burst_ratio": None if burst == float("inf") else round(burst, 3),
        "burst_ratio_is_inf": burst == float("inf"),
        "first_seen": min((m["t"] for m in members), default=None),
        "sample_mints": mints[:10],
    }


def rank_metas(metas: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Money and distinct wallets first, coin count last."""
    return sorted(metas, key=lambda m: (m["sol_inflow"], m["unique_buyers"], m["distinct_creators"], m["n_coins"]), reverse=True)
