"""Daily recall report: `fart measure report --day YYYY-MM-DD`.

Universe = all graduations of the day from our own stream, with coverage. For every winner
(hit_100k at +24 h): organic vs bundled, and whether an `observe` decision existed before its
first trade / before graduation (lead time, negative allowed). Precision of the day's observe
decisions, paper results with and without bundled coins, coverage gaps, a `complete` flag,
rules_version and method_version. Output: out/recall/<day>.json and .md (Dutch), atomic.

Honesty rules (milestone 3): the coverage hours are recomputed live (never from stale rows); an
hour fart was not running is "not covered"; a day without a covered hour is NIET GEDEKT and every
percentage stays empty; the enrich limit (getTransaction budget), the queue backlog and the
failure counters of the day (engine ticks, ingest handlers, refused messages, trade-stream caps)
are part of the report so a quiet day is never mistaken for a complete one.
"""
from __future__ import annotations

import json
import sqlite3
import statistics
from typing import Any

from .. import db
from ..config import Settings
from ..engine.meta import load_stopwords, tokenize
from ..ingest import coverage as cov
from ..ingest.sweep import enrich_backlog, enrich_daily_limit
from ..log import get_logger
from ..util import atomic_write_json, atomic_write_text, day_bounds, hour_key, now as wall_now, utc_iso
from ..version import rules_version

log = get_logger("report")

ET = "COALESCE(block_time, received_at)"


def _latest_outcome(conn: sqlite3.Connection, subject: str, mint: str, horizon_s: int, method_version: str,
                    decision_id: int | None = None) -> sqlite3.Row | None:
    if subject == "decision":
        return conn.execute(
            "SELECT * FROM outcomes WHERE subject='decision' AND decision_id=? AND horizon_s=? AND method_version=? ORDER BY measured_at DESC LIMIT 1",
            (decision_id, horizon_s, method_version),
        ).fetchone()
    return conn.execute(
        "SELECT * FROM outcomes WHERE subject='graduation' AND mint=? AND horizon_s=? AND method_version=? ORDER BY measured_at DESC LIMIT 1",
        (mint, horizon_s, method_version),
    ).fetchone()


def _tri(v: Any) -> bool | None:
    return None if v is None else bool(v)


def _first_trade_time(conn: sqlite3.Connection, mint: str) -> float | None:
    r = conn.execute(f"SELECT MIN({ET}) FROM trades WHERE mint=?", (mint,)).fetchone()
    return None if r is None or r[0] is None else float(r[0])


def _earliest_decision(conn: sqlite3.Connection, mint: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT id, as_of, rules_version FROM decisions WHERE kind='observe' AND verdict='observe' AND mint=? ORDER BY as_of LIMIT 1", (mint,)
    ).fetchone()


def _earliest_meta_decision(conn: sqlite3.Connection, terms: set[str]) -> sqlite3.Row | None:
    if not terms:
        return None
    marks = ",".join("?" for _ in terms)
    return conn.execute(
        f"SELECT id, as_of, term FROM decisions WHERE kind='observe_meta' AND verdict='observe' AND term IN ({marks}) ORDER BY as_of LIMIT 1",
        tuple(terms),
    ).fetchone()


def _summ(values: list[float]) -> dict[str, Any]:
    if not values:
        return {"n": 0}
    return {"n": len(values), "median_s": round(statistics.median(values)), "min_s": round(min(values)), "max_s": round(max(values)),
            "positive": sum(1 for v in values if v > 0), "negative_or_zero": sum(1 for v in values if v <= 0)}


def build_report(conn: sqlite3.Connection, settings: Settings, day: str, now: float) -> dict[str, Any]:
    cfg = settings.config("measure")
    mv = str(cfg["method_version"])
    start, end = day_bounds(day)
    h24 = max(int(h) for h in cfg["horizons_s"])
    stopwords = load_stopwords(settings.config("stopwords"))

    # ----- coverage (always recomputed: stored rows may predate a downtime gap) ----------------------
    hours = [cov.compute_hour(conn, hour_key(start + i * 3600), now) for i in range(24)]
    gap_seconds: dict[str, float] = {}
    for h in hours:
        for g in h["gaps"]:
            gap_seconds[g["source"]] = gap_seconds.get(g["source"], 0.0) + float(g["seconds"])
    first_start = min((v for v in (cov.source_active_since(conn, s) for s in cov.CREATE_SOURCES) if v is not None), default=None)
    coverage_summary = {
        "hours_complete": sum(1 for h in hours if h["complete"]),
        "hours_covered": sum(1 for h in hours if h["covered"]),
        "first_source_start": utc_iso(first_start) if first_start is not None else None,
        "creates_pp": sum(h["creates_pp"] or 0 for h in hours),
        "creates_rpc": sum(h["creates_rpc"] or 0 for h in hours),
        "creates_union": sum(h["creates_union"] or 0 for h in hours),
        "grads": sum(h["grads"] or 0 for h in hours),
        "grads_without_create": sum(h["grads_without_create"] or 0 for h in hours),
        "gap_seconds_by_source": {k: round(v) for k, v in gap_seconds.items()},
        "hours": [{"hour": h["hour"], "creates_union": h["creates_union"], "grads": h["grads"], "complete": h["complete"],
                   "covered": h["covered"], "gap_s": round(sum(float(g["seconds"]) for g in h["gaps"]))} for h in hours],
    }
    covered = coverage_summary["hours_covered"] > 0

    # ----- enrichment limit and the day's failures ---------------------------------------------------
    icfg = settings.config("ingest")
    bcfg = dict(icfg.get("backfill", {}))
    bcfg.update(icfg.get("enrich", {}))
    rpc_src = settings.config("budget")["sources"].get("rpc_public" if settings.rpc_is_public else "rpc", {})
    tx_cost = float((rpc_src.get("cost_table") or {}).get("getTransaction", rpc_src.get("cost_default", 40)))
    cap = None if settings.rpc_is_public else float(settings.rpc_daily_budget_cu)
    limit_kv = db.kv_get(conn, f"enrich.limit.{day}")
    kvi = lambda key: int(db.kv_get(conn, key, "0") or 0)  # noqa: E731
    rejected = {r[0]: int(r[1]) for r in conn.execute(
        "SELECT reason, COUNT(*) FROM rejected_messages WHERE received_at >= ? AND received_at < ? GROUP BY reason ORDER BY 2 DESC", (start, end))}
    ops = {
        "enrich": {"used": kvi(f"enrich.tx.{day}"), "limit": int(limit_kv) if limit_kv else enrich_daily_limit(bcfg, cap, tx_cost),
                   "skipped_budget": kvi(f"enrich.skipped_budget.{day}"), "backlog": enrich_backlog(conn, int(bcfg.get("max_attempts", 3)))},
        "engine_tick_failed": kvi(f"engine.tick_failed.{day}"),
        "handle_failed": {s: kvi(f"ingest.handle_failed.{s}.{day}") for s in ("pumpportal", "rpc_logs", "rpc_trades")},
        "rejected_messages": rejected,
        "rpc_trades": {"subscription_capped": kvi(f"rpc_trades.subscription_capped.{day}"), "envelope_paused": kvi(f"rpc_trades.envelope_paused.{day}")},
        # graduations stored without a known create (milestone 4): part of the universe, never watched
        "migrations_coin_unknown": int(conn.execute(
            "SELECT COUNT(*) FROM migrations WHERE coin_unknown=1 AND COALESCE(block_time, received_at) >= ? AND COALESCE(block_time, received_at) < ?",
            (start, end)).fetchone()[0]),
    }

    # ----- universe: graduations of the day --------------------------------------------------------
    grads = conn.execute(
        "SELECT m.mint, COALESCE(m.block_time, m.received_at) AS t, c.name, c.symbol, COALESCE(c.block_time, c.received_at) AS created"
        " FROM migrations m LEFT JOIN coins c ON c.mint = m.mint"
        " WHERE COALESCE(m.block_time, m.received_at) >= ? AND COALESCE(m.block_time, m.received_at) < ? ORDER BY t",
        (start, end),
    ).fetchall()
    universe = []
    counts = {"graduations": len(grads), "measured": 0, "pending": 0, "missing": 0, "winners": 0, "winners_organic": 0,
              "winners_bundled": 0, "winners_bundled_unknown": 0, "not_winner": 0, "outcome_unknown": 0,
              "bundled_total": 0, "organic_total": 0, "bundled_unknown_total": 0, "without_create": 0}
    winners = []
    lead_first_trade: list[float] = []
    lead_grad: list[float] = []
    lead_theme: list[float] = []
    for g in grads:
        o = _latest_outcome(conn, "graduation", g["mint"], h24, mv)
        entry: dict[str, Any] = {"mint": g["mint"], "name": g["name"], "symbol": g["symbol"], "graduated_at": utc_iso(g["t"]),
                                 "created_at": utc_iso(g["created"]) if g["created"] is not None else None}
        if g["created"] is None:
            counts["without_create"] += 1
        if o is None:
            status = "pending" if g["t"] + h24 > now else "missing"
            counts[status] += 1
            entry.update({"status": status, "hit_100k": None, "bundled": None})
            universe.append(entry)
            continue
        counts["measured"] += 1
        hit = _tri(o["hit_100k"])
        bundled = _tri(o["bundled"])
        entry.update({"status": "measured", "hit_100k": hit, "bundled": bundled, "peak_mcap_usd": o["peak_mcap_usd"],
                      "peak_src": o["peak_src"], "volume_usd": o["volume_usd"]})
        if bundled is True:
            counts["bundled_total"] += 1
        elif bundled is False:
            counts["organic_total"] += 1
        else:
            counts["bundled_unknown_total"] += 1
        if hit is None:
            counts["outcome_unknown"] += 1
        elif hit is False:
            counts["not_winner"] += 1
        else:
            counts["winners"] += 1
            if bundled is True:
                counts["winners_bundled"] += 1
            elif bundled is False:
                counts["winners_organic"] += 1
            else:
                counts["winners_bundled_unknown"] += 1
            d = _earliest_decision(conn, g["mint"])
            ft = _first_trade_time(conn, g["mint"])
            terms = tokenize(g["name"], g["symbol"], stopwords, int(settings.config("engine")["meta"].get("min_token_len", 4)))
            md = _earliest_meta_decision(conn, terms)
            w = dict(entry)
            w["decision_id"] = None if d is None else d["id"]
            w["decision_as_of"] = None if d is None else utc_iso(d["as_of"])
            w["lead_s_vs_first_trade"] = None if d is None or ft is None else round(ft - d["as_of"])
            w["lead_s_vs_graduation"] = None if d is None else round(g["t"] - d["as_of"])
            w["theme_term"] = None if md is None else md["term"]
            w["theme_lead_s_vs_graduation"] = None if md is None else round(g["t"] - md["as_of"])
            if w["lead_s_vs_first_trade"] is not None:
                lead_first_trade.append(w["lead_s_vs_first_trade"])
            if w["lead_s_vs_graduation"] is not None:
                lead_grad.append(w["lead_s_vs_graduation"])
            if w["theme_lead_s_vs_graduation"] is not None:
                lead_theme.append(w["theme_lead_s_vs_graduation"])
            winners.append(w)
        universe.append(entry)

    recall = {
        "winners": counts["winners"],
        "with_decision": sum(1 for w in winners if w["decision_id"] is not None),
        "decision_before_first_trade": sum(1 for w in winners if (w["lead_s_vs_first_trade"] or 0) > 0),
        "decision_before_graduation": sum(1 for w in winners if (w["lead_s_vs_graduation"] or 0) > 0),
        "with_theme_decision": sum(1 for w in winners if w["theme_term"] is not None),
        "theme_before_graduation": sum(1 for w in winners if (w["theme_lead_s_vs_graduation"] or 0) > 0),
        "lead_vs_first_trade": _summ(lead_first_trade),
        "lead_vs_graduation": _summ(lead_grad),
        "theme_lead_vs_graduation": _summ(lead_theme),
    }
    recall["recall_before_graduation"] = (recall["decision_before_graduation"] / counts["winners"]) if counts["winners"] else None
    recall["recall_organic_before_graduation"] = None
    org_w = [w for w in winners if w["bundled"] is False]
    if org_w:
        recall["recall_organic_before_graduation"] = sum(1 for w in org_w if (w["lead_s_vs_graduation"] or 0) > 0) / len(org_w)

    # ----- precision of the day's observe decisions ------------------------------------------------
    decs = conn.execute(
        "SELECT id, mint, as_of, rules_version, features_json FROM decisions WHERE kind='observe' AND verdict='observe' AND as_of >= ? AND as_of < ? ORDER BY as_of",
        (start, end),
    ).fetchall()
    prec = {"decisions": len(decs), "known": 0, "hits": 0, "misses": 0, "unknown": 0, "pending": 0, "bundled": 0, "organic": 0,
            "bundled_unknown": 0, "hits_organic": 0, "known_organic": 0, "distinct_mints": len({d["mint"] for d in decs})}
    rules_versions = sorted({d["rules_version"] for d in decs})
    paper_rows = []
    for d in decs:
        o = _latest_outcome(conn, "decision", d["mint"], h24, mv, decision_id=d["id"])
        if o is None:
            prec["pending" if d["as_of"] + h24 > now else "unknown"] += 1
            continue
        hit, bundled = _tri(o["hit_100k"]), _tri(o["bundled"])
        if bundled is True:
            prec["bundled"] += 1
        elif bundled is False:
            prec["organic"] += 1
        else:
            prec["bundled_unknown"] += 1
        if hit is None:
            prec["unknown"] += 1
        else:
            prec["known"] += 1
            prec["hits" if hit else "misses"] += 1
            if bundled is False:
                prec["known_organic"] += 1
                prec["hits_organic"] += 1 if hit else 0
        p = conn.execute("SELECT * FROM paper WHERE decision_id=? AND method_version=? ORDER BY computed_at DESC LIMIT 1", (d["id"], mv)).fetchone()
        if p is not None:
            paper_rows.append({"decision_id": d["id"], "mint": d["mint"], "bundled": bundled, "pnl": p["pnl"], "exit_reason": p["exit_reason"],
                               "slippage_bps": p["slippage_bps"], "fees": p["fees"], "size_sol": p["size_sol"]})
    prec["precision"] = (prec["hits"] / prec["known"]) if prec["known"] else None
    prec["precision_organic"] = (prec["hits_organic"] / prec["known_organic"]) if prec["known_organic"] else None

    def paper_summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
        known = [r for r in rows if r["pnl"] is not None]
        pnls = [float(r["pnl"]) for r in known]
        return {
            "trades": len(rows), "with_exit": len(known), "no_exit": len(rows) - len(known),
            "pnl_sol_total": round(sum(pnls), 6) if pnls else None,
            "pnl_sol_mean": round(statistics.mean(pnls), 6) if pnls else None,
            "win_rate": (sum(1 for v in pnls if v > 0) / len(pnls)) if pnls else None,
            "exit_reasons": {k: sum(1 for r in rows if r["exit_reason"] == k) for k in sorted({r["exit_reason"] for r in rows})},
            "size_sol": rows[0]["size_sol"] if rows else cfg["paper"]["size_sol"],
        }

    paper = {"all": paper_summary(paper_rows), "without_bundled": paper_summary([r for r in paper_rows if r["bundled"] is not True]),
             "bundled_only": paper_summary([r for r in paper_rows if r["bundled"] is True])}

    complete = covered and coverage_summary["hours_complete"] == 24 and counts["pending"] == 0 and counts["missing"] == 0
    return {
        "day": day,
        "generated_at": utc_iso(now),
        "rules_version_now": rules_version(),
        "rules_versions_in_decisions": rules_versions,
        "method_version": mv,
        "complete": complete,
        "covered": covered,
        "coverage_status": "complete" if complete else ("incomplete" if covered else "not_covered"),
        "ops": ops,
        "winner_definition": cfg["winner"],
        "paper_cfg": cfg["paper"],
        "coverage": coverage_summary,
        "universe": counts,
        "recall": recall,
        "winners": winners,
        "precision": prec,
        "paper": paper,
        "unknowns": {
            "graduations_outcome_unknown": counts["outcome_unknown"],
            "graduations_pending": counts["pending"],
            "graduations_missing_outcome": counts["missing"],
            "graduations_bundled_unknown": counts["bundled_unknown_total"],
            "decisions_outcome_unknown": prec["unknown"],
            "decisions_pending": prec["pending"],
            "graduations_without_create": counts["without_create"],
            "hours_not_covered": 24 - coverage_summary["hours_covered"],
        },
        "graduations": universe,
    }


def _pct(x: float | None) -> str:
    return "onbekend" if x is None else f"{100 * x:.0f}%"


def _n(x: Any) -> str:
    return "onbekend" if x is None else (f"{x:,.0f}" if isinstance(x, float) else str(x))


STATUS_NL = {"complete": "COMPLEET", "incomplete": "NIET COMPLEET", "not_covered": "NIET GEDEKT"}


def render_markdown(r: dict[str, Any]) -> str:
    c, u, rc, p, pa, cvg = r["coverage"], r["universe"], r["recall"], r["precision"], r["paper"], r["coverage"]
    status = r.get("coverage_status") or ("complete" if r["complete"] else "incomplete")
    blank = status == "not_covered"
    pct = (lambda x: "–") if blank else _pct  # a day without coverage has no percentages, only unknowns
    ops = r.get("ops") or {}
    lines = [
        f"# Dagrapport recall {r['day']}",
        "",
        f"Gegenereerd {r['generated_at']} · rules_version {r['rules_version_now']} · method_version {r['method_version']} · "
        f"**{STATUS_NL.get(status, status)}**",
        "",
    ]
    if blank:
        lines += [
            f"**Geen enkel uur van deze dag was gedekt**: fart draaide niet (eerste start van een bron: "
            f"{cvg.get('first_source_start') or 'onbekend'}). Alle percentages hieronder zijn leeg; het universum is onbekend, niet leeg.",
            "",
        ]
    lines += [
        "## Samenvatting",
        "",
        f"- Graduaties in ons eigen universum: **{u['graduations']}** (gemeten {u['measured']}, nog bezig {u['pending']}, ontbrekend {u['missing']})"
        + (" · universum NIET GEDEKT" if blank else ""),
        f"- Winnaars (piek ≥ ${r['winner_definition']['peak_mcap_usd']:,} én volume ≥ ${r['winner_definition']['min_volume_usd']:,} binnen 24 u): "
        f"**{u['winners']}** waarvan organisch {u['winners_organic']}, gebundeld {u['winners_bundled']}, bundeling onbekend {u['winners_bundled_unknown']}",
        f"- Winnaars met een `observe`-beslissing vóór graduatie: **{rc['decision_before_graduation']}/{rc['winners']}** "
        f"({pct(rc['recall_before_graduation'])}); organisch: {pct(rc['recall_organic_before_graduation'])}",
        f"- Winnaars met een thema-beslissing vóór graduatie: {rc['theme_before_graduation']}/{rc['winners']}",
        f"- Precisie van de {p['decisions']} `observe`-beslissingen van deze dag: {pct(p['precision'])} "
        f"({p['hits']} raak van {p['known']} bekend; {p['unknown']} onbekend, {p['pending']} nog bezig)",
        "",
        "## Dekking (coverage)",
        "",
        f"- Gedekte uren: {cvg.get('hours_covered', 'onbekend')}/24 · complete uren: {cvg['hours_complete']}/24"
        + (f" · eerste start van een bron: {cvg['first_source_start']}" if cvg.get("first_source_start") else " · geen bron heeft ooit gedraaid"),
        f"- Creates: PumpPortal {cvg['creates_pp']}, RPC {cvg['creates_rpc']}, unie {cvg['creates_union']}",
        f"- Graduaties: {cvg['grads']}, waarvan zonder bekende create: {cvg['grads_without_create']}",
        f"- Gaten (seconden per bron): {json.dumps(cvg['gap_seconds_by_source']) if cvg['gap_seconds_by_source'] else 'geen'}",
        "",
        "| uur (UTC) | creates | grads | gat (s) | gedekt | compleet |",
        "|---|---:|---:|---:|---|---|",
    ]
    for h in cvg["hours"]:
        lines.append(f"| {h['hour']} | {_n(h['creates_union'])} | {_n(h['grads'])} | {h['gap_s']} | "
                     f"{'ja' if h.get('covered') else 'nee'} | {'ja' if h['complete'] else 'nee'} |")
    if ops:
        en = ops.get("enrich", {})
        bl = en.get("backlog", {})
        hf = ops.get("handle_failed", {})
        rt = ops.get("rpc_trades", {})
        rej = ops.get("rejected_messages", {})
        lines += [
            "",
            "## Verrijking en fouten",
            "",
            f"- Verrijking (getTransaction van gemiste creates/graduaties): {_n(en.get('used'))} van {_n(en.get('limit'))} vandaag gebruikt, "
            f"{_n(en.get('skipped_budget'))} keer overgeslagen door de limiet; wachtrij nu: {_n(bl.get('migrate_tx'))} migraties, "
            f"{_n(bl.get('create_tx'))} creates (migraties gaan voor)",
            f"- Mislukte engine-ticks: {_n(ops.get('engine_tick_failed'))}",
            f"- Mislukte ingest-handlers: " + ", ".join(f"{k} {_n(v)}" for k, v in hf.items()),
            f"- Geweigerde berichten: " + (", ".join(f"{k} {_n(v)}" for k, v in rej.items()) if rej else "geen"),
            f"- Trade-stroom: {_n(rt.get('subscription_capped'))} abonnementen afgekapt, {_n(rt.get('envelope_paused'))} keer gepauzeerd door de uurenvelop",
            f"- Graduaties zonder bekende create (coin_unknown, nooit gevolgd): {_n(ops.get('migrations_coin_unknown'))}",
        ]
    lines += [
        "",
        "## Winnaars",
        "",
        f"Organisch {u['winners_organic']} · gebundeld {u['winners_bundled']} · onbekend {u['winners_bundled_unknown']}. "
        f"Lead time = gebeurtenis minus beslissing; negatief = te laat.",
        "",
        "| mint | naam | gegradueerd | gebundeld | beslissing | lead t.o.v. 1e trade (s) | lead t.o.v. graduatie (s) | thema | thema-lead (s) |",
        "|---|---|---|---|---|---:|---:|---|---:|",
    ]
    for w in r["winners"]:
        b = "ja" if w["bundled"] is True else "nee" if w["bundled"] is False else "onbekend"
        lines.append(f"| `{w['mint'][:8]}…` | {w['name'] or ''} | {w['graduated_at']} | {b} | {w['decision_as_of'] or '–'} | "
                     f"{_n(w['lead_s_vs_first_trade']) if w['lead_s_vs_first_trade'] is not None else '–'} | "
                     f"{_n(w['lead_s_vs_graduation']) if w['lead_s_vs_graduation'] is not None else '–'} | {w['theme_term'] or '–'} | "
                     f"{_n(w['theme_lead_s_vs_graduation']) if w['theme_lead_s_vs_graduation'] is not None else '–'} |")
    if not r["winners"]:
        lines.append("| – | geen winnaars gemeten | | | | | | | |")
    lines += [
        "",
        "## Lead time",
        "",
        f"- t.o.v. eerste trade: {json.dumps(rc['lead_vs_first_trade'])}",
        f"- t.o.v. graduatie: {json.dumps(rc['lead_vs_graduation'])}",
        f"- thema t.o.v. graduatie: {json.dumps(rc['theme_lead_vs_graduation'])}",
        "",
        "## Precisie van observaties",
        "",
        f"- Beslissingen: {p['decisions']} ({p['distinct_mints']} munten); bekend {p['known']}, raak {p['hits']}, mis {p['misses']}, onbekend {p['unknown']}, nog bezig {p['pending']}",
        f"- Precisie: {pct(p['precision'])}; organisch: {pct(p['precision_organic'])} ({p['hits_organic']}/{p['known_organic']})",
        f"- Gebundeld {p['bundled']} · organisch {p['organic']} · onbekend {p['bundled_unknown']}",
        "",
        "## Paper trades",
        "",
        "| set | trades | met exit | zonder exit | PnL totaal (SOL) | PnL gem. (SOL) | winrate | exit-redenen |",
        "|---|---:|---:|---:|---:|---:|---:|---|",
    ]
    for k, label in (("all", "alles"), ("without_bundled", "zonder gebundeld"), ("bundled_only", "alleen gebundeld")):
        s = pa[k]
        tot = "onbekend" if s["pnl_sol_total"] is None else f"{s['pnl_sol_total']:.4f}"
        mean = "onbekend" if s["pnl_sol_mean"] is None else f"{s['pnl_sol_mean']:.4f}"
        lines.append(f"| {label} | {s['trades']} | {s['with_exit']} | {s['no_exit']} | {tot} | {mean} | {pct(s['win_rate'])} | "
                     f"{json.dumps(s['exit_reasons'])} |")
    lines += [
        "",
        f"Inzet per trade: {pa['all']['size_sol']} SOL, instap {r['paper_cfg']['entry_delay_s']} s na de beslissing, trailing stop "
        f"{round(100 * r['paper_cfg']['trailing_stop_frac'])}%, max. {round(r['paper_cfg']['max_hold_s'] / 3600)} u (config/measure.json).",
        "",
        "## Onbekenden",
        "",
    ]
    for k, v in r["unknowns"].items():
        lines.append(f"- {k}: {v}")
    lines += [
        "",
        "## Versies",
        "",
        f"- rules_version nu: `{r['rules_version_now']}`; in beslissingen van deze dag: {', '.join(f'`{v}`' for v in r['rules_versions_in_decisions']) or 'geen'}",
        f"- method_version: `{r['method_version']}`",
        "",
    ]
    return "\n".join(lines)


def run_report(settings: Settings, day: str) -> int:
    settings.ensure_dirs()
    conn = db.connect(settings.db_path)
    try:
        now = wall_now()
        rep = build_report(conn, settings, day, now)
        out_dir = settings.out_dir / "recall"
        atomic_write_json(out_dir / f"{day}.json", rep)
        atomic_write_text(out_dir / f"{day}.md", render_markdown(rep))
        db.kv_set(conn, "measure.report.last_run", str(now), now)
        log.info("report.written", day=day, status=rep["coverage_status"], winners=rep["universe"]["winners"], graduations=rep["universe"]["graduations"])
    finally:
        conn.close()
    return 0
