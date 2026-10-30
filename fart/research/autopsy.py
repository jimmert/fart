"""The autopsy of the organic winners (milestone 12, B.3): their first five minutes next to the non-winners on every feature,
and what the radar saw of them (coverage and lead). Feeds the parameter grids of the hypotheses; replaces no test.

Universe: the graduations of the window with a measured 24 h outcome. Winner = hit_100k, organic = not bundled by the
outcome's own flag AND not bundled by the three research definitions on the trades knowable five minutes after the create
(the stricter of the two; a coin whose bundle flags cannot be evaluated is "bundeling onbekend" and left out of both
groups). Non-winner = a measured, tracker-covered 'no winner'. For every coin: the replay features at 60, 120, 180 and
300 s after the create, the bundle flags, the creator's sells and history as-of 300 s, and whether the radar had its
trades in those minutes, observed it, and how early. Distributions as n, p10, p50, p90 and mean per feature per group.
"""
from __future__ import annotations

import sqlite3
import time
from typing import Any

from ..chain.curve import CurveParams
from ..config import Settings
from ..measure.report import tracker_coverage
from ..util import atomic_write_json, atomic_write_text, day_bounds, day_key, utc_iso
from ..version import rules_version
from . import features as rf
from .harness import _quantile, creator_history_capped, load_coin
from .report import report_dir

ET = "COALESCE(block_time, received_at)"
MARKS_S = (60.0, 120.0, 180.0, 300.0)
FEATURES = ("unique_buyers_5m", "net_sol_5m", "buy_sol_5m", "sells_5m", "trades_total", "largest_buyer_share_15m", "mcap_sol", "graduation_progress")
FEATURE_NL = {"unique_buyers_5m": "unieke kopers (5 min)", "net_sol_5m": "netto SOL (5 min)", "buy_sol_5m": "gekocht SOL (5 min)", "sells_5m": "verkopen (5 min)",
              "trades_total": "trades totaal", "largest_buyer_share_15m": "aandeel grootste koper", "mcap_sol": "marktwaarde (SOL)", "graduation_progress": "graduatievoortgang",
              "dev_buy_frac_of_curve": "dev-buy (aandeel van de curve)", "top2_share": "aandeel top-2 wallets in de eerste 10 trades", "creator_coins": "eerdere munten van de maker",
              "creator_graduations": "eerdere graduaties van de maker", "creator_winners": "eerdere winnaars van de maker"}


def _dist(values: list[float]) -> dict[str, Any]:
    vs = sorted(float(v) for v in values if v is not None)
    if not vs:
        return {"n": 0, "p10": None, "p50": None, "p90": None, "mean": None}
    return {"n": len(vs), "p10": _quantile(vs, 0.1), "p50": _quantile(vs, 0.5), "p90": _quantile(vs, 0.9), "mean": sum(vs) / len(vs)}


def build_autopsy(conn: sqlite3.Connection, settings: Settings, days: list[str], now: float, limit: int | None = None) -> dict[str, Any]:
    params = CurveParams.from_config(settings.config("pumpfun"))
    mcfg = settings.config("measure")
    mv = str(mcfg["method_version"])
    h24 = max(int(h) for h in mcfg["horizons_s"])
    max_gap = float((mcfg.get("tracker") or {}).get("max_sample_gap_s", 600))
    dev_window = float((mcfg.get("creator_stats") or {}).get("dev_sell_window_s", rf.DEV_SELL_WINDOW_S))
    start, end = day_bounds(days[0])[0], day_bounds(days[-1])[1]
    grads = conn.execute(f"SELECT m.mint, {ET.replace('block_time', 'm.block_time').replace('received_at', 'm.received_at')} AS t, m.pool_address FROM migrations m"
                         f" WHERE COALESCE(m.block_time, m.received_at) >= ? AND COALESCE(m.block_time, m.received_at) < ? ORDER BY t", (start, end)).fetchall()
    if limit:
        grads = grads[: int(limit)]
    groups: dict[str, list[dict[str, Any]]] = {"organic_winner": [], "bundled_winner": [], "not_winner": [], "unknown": []}
    counts = {"graduations": len(grads), "no_coin_row": 0, "no_outcome": 0, "outcome_unknown": 0, "sparse": 0, "bundle_unknown": 0}
    t0 = time.perf_counter()
    for g in grads:
        mint, grad_t = str(g["mint"]), float(g["t"])
        o = conn.execute("SELECT hit_100k, bundled, peak_mcap_usd FROM outcomes WHERE subject='graduation' AND mint=? AND horizon_s=? AND method_version=? ORDER BY measured_at DESC LIMIT 1",
                         (mint, h24, mv)).fetchone()
        if o is None:
            counts["no_outcome"] += 1
            continue
        coin = load_coin(conn, mint, with_path=False)
        if coin is None:
            counts["no_coin_row"] += 1
            continue
        hit = o["hit_100k"]
        if hit is None:
            counts["outcome_unknown"] += 1
            continue
        if hit == 0 and not tracker_coverage(conn, mint, grad_t, grad_t + h24, max_gap)["covered"]:
            counts["sparse"] += 1
            continue
        t5 = coin.created_t + 300.0
        kn = rf.knowable(coin.trades, t5)
        bundle = rf.bundle_flags(kn, t5, coin.created_t, coin.dev_buy_sol, coin.slot, graduation_sol=params.graduation_real_sol / params.sol_unit, creator=coin.creator)
        dev = rf.dev_sell(kn, t5, coin.created_t, coin.creator, dev_window)
        outcome_bundled = o["bundled"]
        bundled_any = bundle["bundled_any"]
        if hit == 1:
            if outcome_bundled == 1 or bundled_any is True:
                group = "bundled_winner"
            elif outcome_bundled == 0 and bundled_any is False:
                group = "organic_winner"
            else:
                counts["bundle_unknown"] += 1
                group = "unknown"
        else:
            group = "not_winner"
        marks = {}
        for s in MARKS_S:
            t = coin.created_t + s
            kt = rf.knowable(coin.trades, t)
            marks[str(int(s))] = rf.replay_features(kt, t, coin.created_t, params) if kt else None
        first_trade = coin.trades[0].t if coin.trades else None
        first_received = min((x.received_at for x in coin.trades if x.received_at is not None), default=None)
        dec = conn.execute("SELECT MIN(as_of) FROM decisions WHERE kind='observe' AND mint=?", (mint,)).fetchone()[0]
        cr = creator_history_capped(conn, coin, t5, mv, h24, dev_window, 50) if coin.creator else {}
        groups[group].append({
            "mint": mint, "name": coin.name, "symbol": coin.symbol, "created_t": coin.created_t, "graduated_t": grad_t, "age_at_graduation_s": grad_t - coin.created_t,
            "peak_mcap_usd": o["peak_mcap_usd"], "marks": marks, "bundle": bundle, "dev": dev, "creator": {k: cr.get(k) for k in ("creator_coins", "creator_graduations", "creator_winners", "creator_bundled", "creator_dev_sold_early", "creator_track_record")},
            "trades_first_5m": len(kn), "radar_had_trades_5m": bool(kn), "first_trade_delay_s": None if first_trade is None else first_trade - coin.created_t,
            "first_received_delay_s": None if first_received is None else first_received - coin.created_t,
            "observed": dec is not None, "observed_before_graduation": dec is not None and float(dec) < grad_t,
            "lead_s": None if dec is None else grad_t - float(dec), "observe_age_s": None if dec is None else float(dec) - coin.created_t,
        })
    out: dict[str, Any] = {"kind": "autopsy", "name": f"{day_key(now)}-autopsy", "generated_at": utc_iso(now), "rules_version": rules_version(), "days": days,
                           "counts": counts, "groups": {k: len(v) for k, v in groups.items()}, "seconds": round(time.perf_counter() - t0, 2), "features": {}, "coverage": {},
                           "coins": {k: v for k, v in groups.items() if k != "unknown"}}
    for gname, coins in groups.items():
        if gname == "unknown":
            continue
        feats: dict[str, Any] = {}
        for s in MARKS_S:
            key = str(int(s))
            for fkey in FEATURES:
                feats[f"{fkey}@{key}s"] = _dist([(c["marks"][key] or {}).get(fkey) for c in coins if c["marks"].get(key)])
        feats["dev_buy_frac_of_curve"] = _dist([c["bundle"].get("dev_buy_frac_of_curve") for c in coins])
        feats["top2_share"] = _dist([c["bundle"].get("top2_share") for c in coins])
        for ck in ("creator_coins", "creator_graduations", "creator_winners"):
            feats[ck] = _dist([c["creator"].get(ck) for c in coins])
        n = len(coins)
        out["features"][gname] = feats
        out["coverage"][gname] = {
            "n": n,
            "radar_had_trades_5m": sum(1 for c in coins if c["radar_had_trades_5m"]), "observed": sum(1 for c in coins if c["observed"]),
            "observed_before_graduation": sum(1 for c in coins if c["observed_before_graduation"]),
            "dev_sold_within_10m": sum(1 for c in coins if c["dev"].get("dev_sold_within_window")),
            "bundled_dev": sum(1 for c in coins if c["bundle"].get("bundled_dev") is True), "bundled_slot": sum(1 for c in coins if c["bundle"].get("bundled_slot") is True),
            "bundled_top2": sum(1 for c in coins if c["bundle"].get("bundled_top2") is True),
            "lead_median_s": _quantile(sorted(c["lead_s"] for c in coins if c["lead_s"] is not None), 0.5),
            "first_trade_delay_median_s": _quantile(sorted(c["first_trade_delay_s"] for c in coins if c["first_trade_delay_s"] is not None), 0.5),
            "age_at_graduation_median_s": _quantile(sorted(c["age_at_graduation_s"] for c in coins), 0.5),
            "creator_track": _tally(c["creator"].get("creator_track_record") for c in coins),
        }
    return out


def _tally(items: Any) -> dict[str, int]:
    out: dict[str, int] = {}
    for it in items:
        k = str(it)
        out[k] = out.get(k, 0) + 1
    return dict(sorted(out.items()))


def _f(v: Any, pct: bool = False) -> str:
    if v is None:
        return "onbekend"
    if pct:
        return f"{100 * float(v):.1f}%".replace(".", ",")
    return f"{float(v):.2f}".replace(".", ",") if abs(float(v)) < 100 else f"{float(v):,.0f}".replace(",", ".")


def render_autopsy(r: dict[str, Any]) -> str:
    c = r["counts"]
    g = r["groups"]
    lines = [
        f"# Autopsie van de organische winnaars · {r['days'][0]} t/m {r['days'][-1]}",
        "",
        f"Gegenereerd {r['generated_at']} · rules_version van de code {r['rules_version']} · rekentijd {r['seconds']} s",
        "",
        "Winnaar = graduatie met piek ≥ $100.000 én volume ≥ $20.000 binnen 24 u (de winnaarsdefinitie van de meting). Organisch = niet gebundeld volgens de uitkomst "
        "én volgens de drie bundeldefinities van B.4 op de trades van de eerste 5 minuten; 'geen winnaar' alleen met dichte samples (de trackerregel). Dit rapport vergelijkt "
        "verdelingen; het toetst niets en vervangt de hypothesetoets niet.",
        "",
        f"- Graduaties in het venster: {c['graduations']}; zonder uitkomst {c['no_outcome']}; uitkomst onbekend {c['outcome_unknown']}; 'geen winnaar' met te weinig samples (weggelaten) {c['sparse']}; "
        f"winnaar met onbekende bundeling (weggelaten) {c['bundle_unknown']}; zonder muntrij {c['no_coin_row']}.",
        f"- Groepen: organische winnaars **{g['organic_winner']}**, gebundelde winnaars {g['bundled_winner']}, geen winnaar {g['not_winner']}.",
        "",
        "## Wat de radar zag",
        "",
        "| groep | n | trades in de eerste 5 min | observatie | observatie vóór graduatie | mediane lead | dev verkocht < 10 min | bundel dev / slot / top2 | mediane leeftijd bij graduatie | eerste trade na create (mediaan) | maker: track record |",
        "|---|---:|---:|---:|---:|---:|---:|---|---:|---:|---|",
    ]
    for name, label in (("organic_winner", "organische winnaars"), ("bundled_winner", "gebundelde winnaars"), ("not_winner", "geen winnaar")):
        cv = (r.get("coverage") or {}).get(name)
        if not cv:
            continue
        lines.append(f"| {label} | {cv['n']} | {cv['radar_had_trades_5m']} | {cv['observed']} | {cv['observed_before_graduation']} | {_f(None if cv['lead_median_s'] is None else cv['lead_median_s'] / 60)} min | "
                     f"{cv['dev_sold_within_10m']} | {cv['bundled_dev']} / {cv['bundled_slot']} / {cv['bundled_top2']} | {_f(None if cv['age_at_graduation_median_s'] is None else cv['age_at_graduation_median_s'] / 60)} min | "
                     f"{_f(cv['first_trade_delay_median_s'])} s | {', '.join(f'{k} {v}' for k, v in cv['creator_track'].items()) or '–'} |")
    lines += ["", "## Verdelingen in de eerste 5 minuten (p10 / p50 / p90, n)", ""]
    feats = r.get("features") or {}
    keys = sorted({k for f in feats.values() for k in f}, key=lambda k: (k.split("@")[0], int(k.split("@")[1][:-1]) if "@" in k else 0))
    lines += ["| kenmerk | organische winnaars | gebundelde winnaars | geen winnaar |", "|---|---|---|---|"]
    for k in keys:
        base, _, at = k.partition("@")
        label = FEATURE_NL.get(base, base) + (f" na {at}" if at else "")
        cells = []
        for gname in ("organic_winner", "bundled_winner", "not_winner"):
            d = (feats.get(gname) or {}).get(k)
            pct = base in ("largest_buyer_share_15m", "graduation_progress", "dev_buy_frac_of_curve", "top2_share")
            cells.append("–" if not d or not d["n"] else f"{_f(d['p10'], pct)} / {_f(d['p50'], pct)} / {_f(d['p90'], pct)} (n {d['n']})")
        lines.append(f"| {label} | " + " | ".join(cells) + " |")
    lines += ["", "## Lezing", "",
              "- De kolom 'trades in de eerste 5 min' zegt of de radar de munt überhaupt op tijd volgde; zonder die trades kan geen regel in de eerste minuten vuren, wat de winnaar ook deed.",
              "- Een kenmerk dat bij de organische winnaars duidelijk hoger ligt dan bij 'geen winnaar' is een kandidaat voor een drempel in B.2; de toets blijft de walk-forward van het harnas.",
              "- Dit rapport is geen advies en bewijst niets: het beschrijft."]
    return "\n".join(lines)


def write_autopsy(settings: Settings, result: dict[str, Any]) -> tuple[Any, Any]:
    d = report_dir(settings)
    d.mkdir(parents=True, exist_ok=True)
    js = d / f"{result['name']}.json"
    md = d / f"{result['name']}.md"
    atomic_write_json(js, result)
    atomic_write_text(md, render_autopsy(result))
    return md, js
