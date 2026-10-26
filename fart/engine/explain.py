"""`fart engine explain --day` (milestone 6): why the coin branch gave (no) observations on a day. Read-only.

Source: `engine_candidates` (one row per watched coin and per ranked meta per UTC day with the highest value
of every threshold feature the engine computed in any tick, `fart/engine/candidates.py`), the day's decisions
and `features_asof` rows (a cross-check: one `coin` snapshot per coin decision), the `rpc_trades` coverage gaps
of the day and the stream's counters (envelope pauses, capped subscriptions).

It prints, per threshold of config/engine.json -> coin, how many candidates met it at their best moment, the
distribution (p50, p90, max) of every feature over the candidates' maxima, the share of probation coins that
had a trade in our data at all (per hour, next to the trade-stream gap seconds of that hour), and the same for
the metas (burst_ratio, sol_inflow, unique_buyers). It changes nothing, the thresholds stay what they are, and
it gives no advice on new thresholds: it only shows the distribution. The daily report carries the same table
under "Waarom wel of geen observaties".
"""
from __future__ import annotations

import datetime as _dt
import math
import sqlite3
from typing import Any

from .. import db
from ..config import Settings
from ..ingest import coverage as cov
from ..util import day_bounds, utc_iso

STREAM_SOURCE = "rpc_trades"


def percentiles(values: list[float]) -> dict[str, Any]:
    """Nearest-rank p50 / p90 and the max; n=0 gives None everywhere."""
    vs = sorted(float(v) for v in values if v is not None)
    if not vs:
        return {"n": 0, "p50": None, "p90": None, "max": None}

    def rank(q: float) -> float:
        return vs[min(len(vs) - 1, max(0, int(math.ceil(q * len(vs))) - 1))]

    return {"n": len(vs), "p50": rank(0.5), "p90": rank(0.9), "max": vs[-1]}


def _hour_of(ts: float) -> int:
    return _dt.datetime.fromtimestamp(ts, tz=_dt.timezone.utc).hour


def build_explain(conn: sqlite3.Connection, settings: Settings, day: str, now: float) -> dict[str, Any]:
    ecfg = settings.config("engine")
    ccfg, mcfg = ecfg["coin"], ecfg["meta"]
    start, end = day_bounds(day)
    coins = [dict(r) for r in conn.execute("SELECT * FROM engine_candidates WHERE day=? AND kind='coin' ORDER BY first_as_of, key", (day,))]
    metas = [dict(r) for r in conn.execute("SELECT * FROM engine_candidates WHERE day=? AND kind='meta' ORDER BY first_as_of, key", (day,))]
    kvi = lambda key: int(db.kv_get(conn, key, "0") or 0)  # noqa: E731

    # ----- coins: thresholds, distributions -----------------------------------------------------------
    min_buyers = int(ccfg["observe_min_unique_buyers_5m"])
    min_sol = float(ccfg["observe_min_net_sol_5m"])
    max_share = float(ccfg["observe_max_largest_buyer_share"])
    require_not_bundled = bool(ccfg.get("observe_require_not_bundled", True))
    by_reason: dict[str, int] = {}
    for r in coins:
        by_reason[str(r.get("reason") or "probation")] = by_reason.get(str(r.get("reason") or "probation"), 0) + 1
    with_trades = [r for r in coins if (r.get("max_trades_total") or 0) > 0]
    share_ok = [r for r in coins if r.get("min_largest_buyer_share_15m") is None or float(r["min_largest_buyer_share_15m"]) <= max_share]
    coin_thresholds = [
        {"key": "unique_buyers_5m", "label": "unieke kopers in 5 min", "op": ">=", "threshold": min_buyers,
         "met": sum(1 for r in coins if (r.get("max_unique_buyers_5m") or 0) >= min_buyers)},
        {"key": "net_sol_5m", "label": "netto SOL-instroom in 5 min", "op": ">=", "threshold": min_sol,
         "met": sum(1 for r in coins if (r.get("max_net_sol_5m") or 0.0) >= min_sol)},
        {"key": "momentum_both", "label": "beide momentumdrempels in één tick", "op": "and", "threshold": f"{min_buyers} kopers én {min_sol:g} SOL",
         "met": sum(1 for r in coins if r.get("passed_momentum"))},
        {"key": "largest_buyer_share", "label": "grootste koper (aandeel van de instroom, laagste waarde)", "op": "<=", "threshold": max_share,
         "met": len(share_ok), "note": "onbekend aandeel telt als voldaan (de regel faalt alleen op een bekend te hoog aandeel)"},
        {"key": "not_bundled", "label": "niet gebundeld", "op": "never True", "threshold": "bundled != true" if require_not_bundled else "n.v.t.",
         "met": sum(1 for r in coins if not r.get("bundled_ever")) if require_not_bundled else len(coins)},
        {"key": "all", "label": "alle drempels tegelijk in één tick (observatie op cooldown na)", "op": "all", "threshold": "-",
         "met": sum(1 for r in coins if r.get("passed_all"))},
    ]
    coin_dist = {
        "unique_buyers_5m": percentiles([r.get("max_unique_buyers_5m") for r in coins]),
        "net_sol_5m": percentiles([r.get("max_net_sol_5m") for r in coins]),
        "largest_buyer_share_15m_max": percentiles([r.get("max_largest_buyer_share_15m") for r in coins]),
        "largest_buyer_share_15m_min": percentiles([r.get("min_largest_buyer_share_15m") for r in coins]),
        "trades_total": percentiles([r.get("max_trades_total") for r in coins]),
        "ticks": percentiles([r.get("ticks") for r in coins]),
    }
    bundled = {"ever_true": sum(1 for r in coins if r.get("bundled_ever")),
               "known_false": sum(1 for r in coins if not r.get("bundled_ever") and r.get("bundled_last") == 0),
               "unknown": sum(1 for r in coins if not r.get("bundled_ever") and r.get("bundled_last") is None)}
    decisions_coin = conn.execute(
        "SELECT COUNT(*), COUNT(DISTINCT mint) FROM decisions WHERE kind='observe' AND verdict='observe' AND as_of >= ? AND as_of < ?", (start, end)).fetchone()
    decisions_meta = conn.execute(
        "SELECT COUNT(*), COUNT(DISTINCT term) FROM decisions WHERE kind='observe_meta' AND verdict='observe' AND as_of >= ? AND as_of < ?", (start, end)).fetchone()
    snapshots = {r[0]: int(r[1]) for r in conn.execute(
        "SELECT kind, COUNT(*) FROM features_asof WHERE as_of >= ? AND as_of < ? GROUP BY kind", (start, end))}

    # ----- data availability per hour, next to the trade stream's gaps -------------------------------------
    gaps = [g for g in cov.gaps_overlapping(conn, start, end, now) if g["source"] == STREAM_SOURCE]
    hours = []
    for h in range(24):
        hs, he = start + h * 3600, start + (h + 1) * 3600
        seen = [r for r in coins if hs <= float(r["first_as_of"]) < he]
        prob = [r for r in seen if str(r.get("reason") or "probation") == "probation"]
        prob_traded = [r for r in prob if (r.get("max_trades_total") or 0) > 0]
        gap_s = 0.0
        reasons: set[str] = set()
        for g in gaps:
            a, b = max(float(g["start"]), hs), min(float(g["end"]), he)
            if b > a:
                gap_s += b - a
                reasons.add(str(g.get("reason") or "?"))
        hours.append({"hour": h, "candidates_first_seen": len(seen), "probation_first_seen": len(prob), "probation_with_trades": len(prob_traded),
                      "probation_trade_share": (len(prob_traded) / len(prob)) if prob else None, "stream_gap_s": round(gap_s),
                      "stream_gap_reasons": sorted(reasons)})
    probation = [r for r in coins if str(r.get("reason") or "probation") == "probation"]
    probation_traded = [r for r in probation if (r.get("max_trades_total") or 0) > 0]
    stream = {
        "gap_seconds": round(sum(float(g["end"]) - float(g["start"]) for g in gaps)),
        "gap_reasons": sorted({str(g.get("reason") or "?") for g in gaps}),
        "envelope_paused": kvi(f"rpc_trades.envelope_paused.{day}"),
        "subscription_capped": kvi(f"rpc_trades.subscription_capped.{day}"),
        "engine_tick_failed": kvi(f"engine.tick_failed.{day}"),
        "first_started": utc_iso(cov.source_active_since(conn, STREAM_SOURCE)) if cov.source_active_since(conn, STREAM_SOURCE) is not None else None,
    }

    # ----- metas --------------------------------------------------------------------------------------------
    burst_min = float(mcfg["burst_ratio"])
    min_inflow = float(mcfg["observe_min_sol_inflow"])
    min_mbuyers = int(mcfg["observe_min_unique_buyers"])
    min_coins, min_creators = int(mcfg["min_coins"]), int(mcfg["min_distinct_creators"])
    meta_thresholds = [
        {"key": "coins_and_creators", "label": "munten én makers", "op": ">=", "threshold": f"{min_coins} munten, {min_creators} makers",
         "met": sum(1 for r in metas if (r.get("max_n_coins") or 0) >= min_coins and (r.get("max_distinct_creators") or 0) >= min_creators)},
        {"key": "burst_ratio", "label": "burst t.o.v. de eigen basislijn", "op": ">=", "threshold": burst_min,
         "met": sum(1 for r in metas if r.get("burst_inf") or (r.get("max_burst_ratio") is not None and float(r["max_burst_ratio"]) >= burst_min))},
        {"key": "sol_inflow", "label": "SOL-instroom in het venster", "op": ">=", "threshold": min_inflow,
         "met": sum(1 for r in metas if (r.get("max_sol_inflow") or 0.0) >= min_inflow)},
        {"key": "unique_buyers", "label": "unieke kopers in het venster", "op": ">=", "threshold": min_mbuyers,
         "met": sum(1 for r in metas if (r.get("max_unique_buyers") or 0) >= min_mbuyers)},
        {"key": "bursting", "label": "burstend (munten, makers én burst in één tick)", "op": "all", "threshold": "-",
         "met": sum(1 for r in metas if r.get("passed_momentum"))},
        {"key": "all", "label": "alle drempels tegelijk in één tick (thema-observatie op cooldown na)", "op": "all", "threshold": "-",
         "met": sum(1 for r in metas if r.get("passed_all"))},
    ]
    meta_dist = {
        "burst_ratio": percentiles([r.get("max_burst_ratio") for r in metas]),
        "burst_ratio_inf": sum(1 for r in metas if r.get("burst_inf")),
        "sol_inflow": percentiles([r.get("max_sol_inflow") for r in metas]),
        "unique_buyers": percentiles([r.get("max_unique_buyers") for r in metas]),
        "n_coins": percentiles([r.get("max_n_coins") for r in metas]),
    }
    top_coins = sorted(coins, key=lambda r: ((r.get("max_unique_buyers_5m") or 0), (r.get("max_net_sol_5m") or 0.0)), reverse=True)[:10]
    top_metas = sorted(metas, key=lambda r: ((r.get("max_sol_inflow") or 0.0), (r.get("max_unique_buyers") or 0)), reverse=True)[:10]
    return {
        "day": day,
        "generated_at": utc_iso(now),
        "available": bool(coins or metas),
        "note": None if (coins or metas) else "geen kandidaatgegevens voor deze dag: de engine schrijft engine_candidates sinds mijlpaal 6",
        "thresholds": {"coin": {k: v for k, v in ccfg.items() if not k.startswith("_")}, "meta": {k: v for k, v in mcfg.items() if not k.startswith("_")}},
        "coins": {
            "candidates": len(coins), "by_reason": dict(sorted(by_reason.items())),
            "with_trades": len(with_trades), "with_trades_share": (len(with_trades) / len(coins)) if coins else None,
            "probation": len(probation), "probation_with_trades": len(probation_traded),
            "probation_trade_share": (len(probation_traded) / len(probation)) if probation else None,
            "thresholds": coin_thresholds, "distribution": coin_dist, "bundled": bundled,
            "decisions": int(decisions_coin[0]), "decisions_distinct_mints": int(decisions_coin[1]),
            "features_asof_snapshots": int(snapshots.get("coin", 0)),
            "top": [{"mint": r["key"], "reason": r.get("reason"), "max_unique_buyers_5m": r.get("max_unique_buyers_5m"),
                     "max_net_sol_5m": r.get("max_net_sol_5m"), "min_largest_buyer_share_15m": r.get("min_largest_buyer_share_15m"),
                     "bundled_ever": bool(r.get("bundled_ever")), "max_trades_total": r.get("max_trades_total"), "ticks": r.get("ticks"),
                     "passed_all": bool(r.get("passed_all"))} for r in top_coins],
        },
        "hours": hours,
        "stream": stream,
        "metas": {
            "candidates": len(metas), "thresholds": meta_thresholds, "distribution": meta_dist,
            "decisions": int(decisions_meta[0]), "decisions_distinct_terms": int(decisions_meta[1]),
            "features_asof_snapshots": int(snapshots.get("meta", 0)),
            "top": [{"term": r["key"], "max_burst_ratio": r.get("max_burst_ratio"), "burst_inf": bool(r.get("burst_inf")),
                     "max_sol_inflow": r.get("max_sol_inflow"), "max_unique_buyers": r.get("max_unique_buyers"), "max_n_coins": r.get("max_n_coins"),
                     "max_distinct_creators": r.get("max_distinct_creators"), "ticks": r.get("ticks"), "passed_all": bool(r.get("passed_all"))}
                    for r in top_metas],
        },
        "disclaimer": "Alleen de verdeling; de drempels blijven wat ze zijn (bevroren regels). Geen advies over nieuwe drempels.",
    }


# ----- rendering --------------------------------------------------------------------------------------------
def _n(x: Any) -> str:
    if x is None:
        return "–"
    if isinstance(x, bool):
        return "ja" if x else "nee"
    if isinstance(x, float):
        return f"{x:,.2f}" if abs(x) < 100 else f"{x:,.0f}"
    return str(x)


def _pct(x: Any) -> str:
    return "–" if x is None else f"{100 * float(x):.0f}%"


def _dist_row(label: str, d: dict[str, Any]) -> str:
    return f"| {label} | {d['n']} | {_n(d['p50'])} | {_n(d['p90'])} | {_n(d['max'])} |"


def markdown_lines(e: dict[str, Any]) -> list[str]:
    """The daily report section (Dutch)."""
    c, m, st = e["coins"], e["metas"], e["stream"]
    lines = ["## Waarom wel of geen observaties", ""]
    if not e["available"]:
        lines += [f"- {e['note']}.", ""]
        return lines
    lines += [
        f"- Munt-kandidaten die de engine deze dag zag: **{c['candidates']}** ("
        + ", ".join(f"{k} {v}" for k, v in c["by_reason"].items()) + f"); met minstens één trade in onze data: {c['with_trades']} ({_pct(c['with_trades_share'])}); "
        f"probation-munten met trades: {c['probation_with_trades']} van {c['probation']} ({_pct(c['probation_trade_share'])})",
        f"- Munt-observaties (`observe`): {c['decisions']} op {c['decisions_distinct_mints']} munten; `features_asof`-snapshots (munt): {c['features_asof_snapshots']}",
        f"- Trade-stroom (rpc_trades) deze dag: {_n(st['gap_seconds'])} s gat" + (f" ({', '.join(st['gap_reasons'])})" if st["gap_reasons"] else "")
        + f", {st['envelope_paused']} keer gepauzeerd door de uurenvelop, {st['subscription_capped']} abonnementen afgekapt, {st['engine_tick_failed']} mislukte engine-ticks",
        "",
        "| drempel (config/engine.json → coin) | eis | kandidaten die eraan voldeden (beste moment) |",
        "|---|---|---:|",
    ]
    for t in c["thresholds"]:
        lines.append(f"| {t['label']} | {t['op']} {t['threshold']} | {t['met']} |")
    d = c["distribution"]
    lines += [
        "",
        "| feature (hoogste waarde per munt) | n | p50 | p90 | max |",
        "|---|---:|---:|---:|---:|",
        _dist_row("unique_buyers_5m", d["unique_buyers_5m"]),
        _dist_row("net_sol_5m", d["net_sol_5m"]),
        _dist_row("largest_buyer_share_15m (hoogste)", d["largest_buyer_share_15m_max"]),
        _dist_row("largest_buyer_share_15m (laagste)", d["largest_buyer_share_15m_min"]),
        _dist_row("trades in onze data", d["trades_total"]),
        _dist_row("ticks gezien", d["ticks"]),
        "",
        f"- Gebundeld: ooit gezien {c['bundled']['ever_true']}, bekend niet gebundeld {c['bundled']['known_false']}, onbekend {c['bundled']['unknown']}",
        "",
        "| uur (UTC) | kandidaten nieuw | probation nieuw | met trades | aandeel | trade-stroom gat (s) |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for h in e["hours"]:
        if not h["candidates_first_seen"] and not h["stream_gap_s"]:
            continue
        lines.append(f"| {h['hour']:02d} | {h['candidates_first_seen']} | {h['probation_first_seen']} | {h['probation_with_trades']} | "
                     f"{_pct(h['probation_trade_share'])} | {h['stream_gap_s']}" + (f" ({', '.join(h['stream_gap_reasons'])})" if h["stream_gap_reasons"] else "") + " |")
    lines += [
        "",
        f"- Thema-kandidaten (gerangschikte termen): **{m['candidates']}**; thema-observaties (`observe_meta`): {m['decisions']} op {m['decisions_distinct_terms']} termen",
        "",
        "| drempel (config/engine.json → meta) | eis | termen die eraan voldeden (beste moment) |",
        "|---|---|---:|",
    ]
    for t in m["thresholds"]:
        lines.append(f"| {t['label']} | {t['op']} {t['threshold']} | {t['met']} |")
    md = m["distribution"]
    lines += [
        "",
        "| feature (hoogste waarde per term) | n | p50 | p90 | max |",
        "|---|---:|---:|---:|---:|",
        _dist_row(f"burst_ratio (eindig; oneindig: {md['burst_ratio_inf']} termen)", md["burst_ratio"]),
        _dist_row("sol_inflow", md["sol_inflow"]),
        _dist_row("unique_buyers", md["unique_buyers"]),
        _dist_row("n_coins", md["n_coins"]),
        "",
        f"{e['disclaimer']}",
        "",
    ]
    return lines


def render_text(e: dict[str, Any]) -> str:
    """The CLI view: the same numbers as plain text tables."""
    c, m, st = e["coins"], e["metas"], e["stream"]
    out = [f"fart engine explain --day {e['day']}  (generated {e['generated_at']}; read-only, thresholds unchanged)"]
    if not e["available"]:
        out.append(f"  {e['note']}")
        return "\n".join(out)
    out += [
        f"  coin candidates: {c['candidates']} (" + ", ".join(f"{k} {v}" for k, v in c["by_reason"].items()) + f"); with trades {c['with_trades']} ({_pct(c['with_trades_share'])});"
        f" probation with trades {c['probation_with_trades']}/{c['probation']} ({_pct(c['probation_trade_share'])})",
        f"  coin observations: {c['decisions']} on {c['decisions_distinct_mints']} mints; features_asof coin snapshots: {c['features_asof_snapshots']}",
        f"  trade stream: gap {_n(st['gap_seconds'])} s" + (f" ({', '.join(st['gap_reasons'])})" if st["gap_reasons"] else "")
        + f", envelope paused {st['envelope_paused']}x, subscriptions capped {st['subscription_capped']}, engine ticks failed {st['engine_tick_failed']}",
        "",
        f"  {'coin threshold':<64}{'requirement':<34}{'met':>6}",
    ]
    for t in c["thresholds"]:
        out.append(f"  {t['label']:<64}{(t['op'] + ' ' + str(t['threshold'])):<34}{t['met']:>6}")
    d = c["distribution"]
    out += ["", f"  {'feature (max per coin)':<40}{'n':>6}{'p50':>12}{'p90':>12}{'max':>12}"]
    for label, key in (("unique_buyers_5m", "unique_buyers_5m"), ("net_sol_5m", "net_sol_5m"), ("largest_buyer_share_15m (max)", "largest_buyer_share_15m_max"),
                       ("largest_buyer_share_15m (min)", "largest_buyer_share_15m_min"), ("trades_total", "trades_total"), ("ticks", "ticks")):
        x = d[key]
        out.append(f"  {label:<40}{x['n']:>6}{_n(x['p50']):>12}{_n(x['p90']):>12}{_n(x['max']):>12}")
    out += [f"  bundled: ever true {c['bundled']['ever_true']}, known false {c['bundled']['known_false']}, unknown {c['bundled']['unknown']}",
            "", f"  {'hour':<6}{'cand':>6}{'prob':>6}{'traded':>8}{'share':>8}{'stream gap (s)':>16}  reasons"]
    for h in e["hours"]:
        if not h["candidates_first_seen"] and not h["stream_gap_s"]:
            continue
        out.append(f"  {h['hour']:02d}    {h['candidates_first_seen']:>6}{h['probation_first_seen']:>6}{h['probation_with_trades']:>8}{_pct(h['probation_trade_share']):>8}"
                   f"{h['stream_gap_s']:>16}  {', '.join(h['stream_gap_reasons'])}")
    if c["top"]:
        out += ["", "  strongest coins (max unique_buyers_5m, max net_sol_5m, min share, bundled ever, trades, ticks, passed all):"]
        for r in c["top"]:
            out.append(f"    {r['mint'][:12]:<13}{str(r['reason']):<13}{_n(r['max_unique_buyers_5m']):>6}{_n(r['max_net_sol_5m']):>10}"
                       f"{_n(r['min_largest_buyer_share_15m']):>8}{_n(r['bundled_ever']):>6}{_n(r['max_trades_total']):>8}{_n(r['ticks']):>7}{_n(r['passed_all']):>6}")
    out += ["", f"  meta candidates: {m['candidates']}; theme observations: {m['decisions']} on {m['decisions_distinct_terms']} terms",
            f"  {'meta threshold':<64}{'requirement':<34}{'met':>6}"]
    for t in m["thresholds"]:
        out.append(f"  {t['label']:<64}{(t['op'] + ' ' + str(t['threshold'])):<34}{t['met']:>6}")
    md = m["distribution"]
    out += ["", f"  {'feature (max per term)':<40}{'n':>6}{'p50':>12}{'p90':>12}{'max':>12}"]
    for label, key in ((f"burst_ratio (finite; inf: {md['burst_ratio_inf']})", "burst_ratio"), ("sol_inflow", "sol_inflow"), ("unique_buyers", "unique_buyers"), ("n_coins", "n_coins")):
        x = md[key]
        out.append(f"  {label:<40}{x['n']:>6}{_n(x['p50']):>12}{_n(x['p90']):>12}{_n(x['max']):>12}")
    if m["top"]:
        out += ["", "  strongest metas (max sol_inflow, max unique_buyers, max burst_ratio, coins, creators, ticks, passed all):"]
        for r in m["top"]:
            burst = "inf" if r["burst_inf"] else _n(r["max_burst_ratio"])
            out.append(f"    {r['term'][:24]:<25}{_n(r['max_sol_inflow']):>10}{_n(r['max_unique_buyers']):>8}{burst:>10}{_n(r['max_n_coins']):>7}"
                       f"{_n(r['max_distinct_creators']):>9}{_n(r['ticks']):>7}{_n(r['passed_all']):>6}")
    out += ["", f"  {e['disclaimer']}"]
    return "\n".join(out)
