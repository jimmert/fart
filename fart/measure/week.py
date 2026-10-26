"""Week report: `fart measure week --end YYYY-MM-DD --days 7` (milestone 5).

A rolling window of `days` UTC days ending at `--end` (default: yesterday), written atomically to
`out/recall/week-<end>.json` and `.md` (Dutch, the style of the daily report). The systemd timer runs it
every day at 06:30 UTC; the Monday run covers the whole Monday-Sunday calendar week.

Everything is reported per segment = (rules group, method_version):

* a rules group is the set of `rules_version`s that ran identical rules: every deploy is a new commit and
  therefore a new `rules_version` even when no threshold changed, so decisions are pooled by the
  fingerprint of the rule-defining files (`fart/version.py`: kv `engine.rules_fingerprint.<rv>`, the
  frozen entry in `config/measure.json` -> `community_gate.frozen_rules`, the running tree). A
  rules_version without a known fingerprint is a group of its own. The rules_versions of a group are
  always listed; nothing is hidden by the pooling;
* a method_version is one measurement method (`outcomes.method_version`); every method present in the
  window gets its own day reports (`report.build_report(..., method_version=mv)`).

Per segment: (a) the universe per day (creates per source, graduations, `coin_unknown` graduations,
winners organic / bundled / unknown, hours NIET GEDEKT, budget pauses), (b) the hit rate of coin
observations (winner within 24 h over the decisions whose 24 h have elapsed: an unknown outcome stays in
the denominator because an unmeasurable observation is not a demonstrated hit, a pending one is listed
apart and joins the denominator once its 24 h have passed; the rate over known outcomes is printed next
to it), (c) recall (winners with an observe or theme decision before the first trade / before
graduation), (d) lead time (median, p25, p75, the bins of `config/measure.json`), (e) block-bootstrap
confidence intervals with days as blocks (9,999 draws, fixed seed, alpha 0.05; a NIET GEDEKT day is not a
block and never a day with zero winners; fewer than 3 covered days or fewer than 20 winners prints "te
weinig data"), (f) the estimated costs per day (CU from the cost table, uncalibrated until the Alchemy
dashboard comparison in docs/UNVERIFIED.md; X in USD) and (g) the frozen go/no-go test for community
alerts (`community_gate`), printed as voldaan / niet voldaan / te weinig data per criterion. The report
decides nothing and sends nothing.

Honesty rules: a day without a covered hour is NIET GEDEKT: its universe is unknown (not zero), it is no
block in the bootstrap and no "complete day"; the tracker-coverage share is shown per day and a graduation
without samples at least every 10 minutes is "uitkomst onbekend" (never "geen winnaar"); every number comes
with its unknown and pending counts.
"""
from __future__ import annotations

import datetime as _dt
import json
import math
import random
import sqlite3
import statistics
from typing import Any, Iterable

from .. import db
from ..config import Settings
from ..log import get_logger
from ..util import atomic_write_json, atomic_write_text, day_bounds, day_key, now as wall_now, utc_iso
from ..version import rules_fingerprint, rules_version
from .report import CU_NOTE, build_report

log = get_logger("week")

TOO_LITTLE = "te weinig data"
MET = "voldaan"
NOT_MET = "niet voldaan"
FP_PREFIX = "engine.rules_fingerprint."


# ----- window ------------------------------------------------------------------------------------------
def window_days(end_day: str, days: int) -> list[str]:
    """The `days` UTC days ending at `end_day` (inclusive), oldest first."""
    end = _dt.datetime.strptime(end_day, "%Y-%m-%d").replace(tzinfo=_dt.timezone.utc)
    n = max(1, int(days))
    return [(end - _dt.timedelta(days=i)).strftime("%Y-%m-%d") for i in range(n - 1, -1, -1)]


def is_calendar_week(end_day: str, days: int) -> bool:
    """A Monday-Sunday week: 7 days ending on a Sunday (the Monday 06:30 UTC run)."""
    end = _dt.datetime.strptime(end_day, "%Y-%m-%d")
    return int(days) == 7 and end.isoweekday() == 7


def method_versions_in_window(conn: sqlite3.Connection, start: float, end: float, current: str) -> list[str]:
    rows = conn.execute("SELECT DISTINCT method_version FROM outcomes WHERE anchor_time >= ? AND anchor_time < ?", (start, end)).fetchall()
    return sorted({str(current)} | {str(r[0]) for r in rows})


def rules_fingerprint_map(conn: sqlite3.Connection, gate_cfg: dict[str, Any], rv_now: str, fp_now: str) -> dict[str, str]:
    """rules_version -> fingerprint: what the engine recorded (kv), the frozen entry of the config, the running tree."""
    out: dict[str, str] = {}
    for r in conn.execute("SELECT key, value FROM kv WHERE key LIKE ?", (FP_PREFIX + "%",)):
        if r["value"]:
            out[str(r["key"])[len(FP_PREFIX):]] = str(r["value"])
    frozen = gate_cfg.get("frozen_rules") or {}
    if frozen.get("rules_version") and frozen.get("rules_fingerprint"):
        out.setdefault(str(frozen["rules_version"]), str(frozen["rules_fingerprint"]))
    out.setdefault(str(rv_now), str(fp_now))
    return out


def rules_groups(rules_versions: Iterable[str], fp_map: dict[str, str]) -> dict[str, list[str]]:
    """group key (the fingerprint, or `unknown:<rv>`) -> sorted rules_versions."""
    groups: dict[str, list[str]] = {}
    for rv in sorted({str(v) for v in rules_versions}):
        groups.setdefault(fp_map.get(rv) or f"unknown:{rv}", []).append(rv)
    return groups


# ----- statistics --------------------------------------------------------------------------------------
def bootstrap_ratio(blocks: list[tuple[int, int]], draws: int, seed: int, alpha: float) -> dict[str, Any] | None:
    """Block bootstrap of a pooled ratio sum(num)/sum(den) with the blocks (days) resampled with
    replacement. Deterministic for a given seed. A draw whose pooled denominator is 0 is undefined and
    skipped (`defined_draws`). None when nothing is defined."""
    n = len(blocks)
    if n == 0:
        return None
    rng = random.Random(int(seed))
    vals: list[float] = []
    for _ in range(int(draws)):
        num = den = 0
        for _ in range(n):
            b = blocks[rng.randrange(n)]
            num += int(b[0])
            den += int(b[1])
        if den > 0:
            vals.append(num / den)
    if not vals:
        return None
    vals.sort()
    k = len(vals) - 1
    lo = vals[int(math.floor(alpha / 2.0 * k))]
    hi = vals[int(math.ceil((1.0 - alpha / 2.0) * k))]
    tot_num = sum(int(b[0]) for b in blocks)
    tot_den = sum(int(b[1]) for b in blocks)
    return {"low": lo, "high": hi, "point": (tot_num / tot_den) if tot_den else None, "draws": int(draws), "defined_draws": len(vals),
            "seed": int(seed), "alpha": float(alpha), "blocks": n, "method": "block bootstrap, dagen als blokken, percentielen"}


def _quantiles(values: list[float]) -> dict[str, Any]:
    if not values:
        return {"n": 0, "median_s": None, "p25_s": None, "p75_s": None, "min_s": None, "max_s": None, "positive": 0, "negative_or_zero": 0}
    vs = sorted(values)
    if len(vs) == 1:
        p25 = p75 = vs[0]
    else:
        q = statistics.quantiles(vs, n=4, method="inclusive")
        p25, p75 = q[0], q[2]
    return {"n": len(vs), "median_s": round(statistics.median(vs)), "p25_s": round(p25), "p75_s": round(p75), "min_s": round(vs[0]),
            "max_s": round(vs[-1]), "positive": sum(1 for v in vs if v > 0), "negative_or_zero": sum(1 for v in vs if v <= 0)}


def lead_bins(values: list[float], edges: list[float]) -> list[dict[str, Any]]:
    """Histogram over the configured edges: (-inf, e0), [e0, e1), ..., [e_last, inf)."""
    es = sorted(float(e) for e in edges)
    bounds: list[tuple[float | None, float | None]] = [(None, es[0])] if es else [(None, None)]
    for a, b in zip(es, es[1:]):
        bounds.append((a, b))
    if es:
        bounds.append((es[-1], None))
    out = []
    for lo, hi in bounds:
        n = sum(1 for v in values if (lo is None or v >= lo) and (hi is None or v < hi))
        out.append({"from_s": lo, "to_s": hi, "n": n})
    return out


def _ratio(num: int, den: int) -> float | None:
    return (num / den) if den else None


# ----- the per-day block of a segment ------------------------------------------------------------------
def day_block(rep: dict[str, Any], rvs: list[str]) -> dict[str, Any]:
    """Everything the week report needs from one daily report, restricted to the rules_versions `rvs`.
    Reads defensively so a synthetic day (tests) needs only the fields it wants to exercise."""
    cov = rep.get("coverage") or {}
    u = rep.get("universe") or {}
    ops = rep.get("ops") or {}
    covered = bool(rep.get("covered", (cov.get("hours_covered") or 0) > 0))
    hours_not_covered = 24 - int(cov.get("hours_covered") or 0)
    peak_nc = cov.get("peak_hours_not_covered")
    gate_complete = covered and peak_nc is not None and int(peak_nc) == 0
    obs = {"observe": 0, "observe_meta": 0}
    for rv in rvs:
        o = (rep.get("observations_by_rules_version") or {}).get(rv) or {}
        obs["observe"] += int(o.get("observe", 0))
        obs["observe_meta"] += int(o.get("observe_meta", 0))
    hit = {"decisions": 0, "hits": 0, "misses": 0, "unknown": 0, "pending": 0, "elapsed": 0}
    for rv in rvs:
        pr = (rep.get("precision_by_rules_version") or {}).get(rv) or {}
        for k in ("decisions", "hits", "misses", "unknown", "pending"):
            hit[k] += int(pr.get(k, 0))
        hit["elapsed"] += int(pr.get("elapsed", int(pr.get("known", 0)) + int(pr.get("unknown", 0))))
    rec = {"winners": 0, "any_before_graduation": 0, "coin_before_graduation": 0, "theme_before_graduation": 0,
           "any_before_first_trade": 0, "coin_before_first_trade": 0, "theme_before_first_trade": 0, "with_first_trade": 0}
    leads: dict[str, list[float]] = {"any_vs_graduation": [], "coin_vs_graduation": [], "theme_vs_graduation": [], "any_vs_first_trade": []}
    for w in rep.get("winners") or []:
        rec["winners"] += 1
        grad_t = w.get("graduated_t")
        first_t = w.get("first_trade_t")
        if first_t is not None:
            rec["with_first_trade"] += 1
        coin_as_of = theme_as_of = None
        for rv in rvs:
            e = (w.get("earliest_by_rules_version") or {}).get(rv) or {}
            if e.get("coin") is not None:
                coin_as_of = e["coin"]["as_of"] if coin_as_of is None else min(coin_as_of, e["coin"]["as_of"])
            if e.get("theme") is not None:
                theme_as_of = e["theme"]["as_of"] if theme_as_of is None else min(theme_as_of, e["theme"]["as_of"])
        any_as_of = None if coin_as_of is None and theme_as_of is None else min(v for v in (coin_as_of, theme_as_of) if v is not None)
        if grad_t is not None:
            for key, as_of in (("any", any_as_of), ("coin", coin_as_of), ("theme", theme_as_of)):
                if as_of is None:
                    continue
                lead = float(grad_t) - float(as_of)
                leads[f"{key}_vs_graduation"].append(lead)
                if lead > 0:
                    rec[f"{key}_before_graduation"] += 1
        if first_t is not None:
            for key, as_of in (("any", any_as_of), ("coin", coin_as_of), ("theme", theme_as_of)):
                if as_of is None:
                    continue
                lead = float(first_t) - float(as_of)
                if key == "any":
                    leads["any_vs_first_trade"].append(lead)
                if lead > 0:
                    rec[f"{key}_before_first_trade"] += 1
    bp = ops.get("budget_pauses") or {}
    cost = ops.get("cost") or {}
    xs = rep.get("x_shadow") or {}
    xo = xs.get("outcomes") or {}
    xc = xs.get("confirmations") or {}
    return {
        "day": rep.get("day"),
        "x_shadow": {"active": bool(xs.get("active")), "decisions": int((xs.get("decisions") or {}).get("total", 0)),
                     "confirmed_1h": int(xc.get("confirmed_1h", 0)), "confirmed_24h": int(xc.get("confirmed_24h", 0)),
                     "counts_taken": int(xc.get("counts_taken", 0)), "observations": int(xc.get("observations_of_day", 0)),
                     "elapsed": int(xo.get("elapsed", 0)), "winner": int(xo.get("winner", 0)), "not_winner": int(xo.get("not_winner", 0)),
                     "unknown": int(xo.get("unknown", 0)), "pending": int(xo.get("pending", 0))},
        "synthetic": bool(rep.get("synthetic", False)),
        "covered": covered,
        "coverage_status": rep.get("coverage_status") or ("incomplete" if covered else "not_covered"),
        "hours_not_covered": hours_not_covered,
        "hours_complete": int(cov.get("hours_complete") or 0),
        "peak_hours_not_covered": None if peak_nc is None else int(peak_nc),
        "gate_complete": gate_complete,
        "creates_pp": cov.get("creates_pp"), "creates_rpc": cov.get("creates_rpc"), "creates_union": cov.get("creates_union"),
        "graduations": u.get("graduations"),
        "grads_coin_unknown": ops.get("migrations_coin_unknown"),
        "measured": u.get("measured", 0), "pending": u.get("pending", 0), "missing": u.get("missing", 0),
        "winners": int(u.get("winners", 0)), "winners_organic": int(u.get("winners_organic", 0)),
        "winners_bundled": int(u.get("winners_bundled", 0)), "winners_bundled_unknown": int(u.get("winners_bundled_unknown", 0)),
        "not_winner": int(u.get("not_winner", 0)), "outcome_unknown": int(u.get("outcome_unknown", 0)),
        "outcome_unknown_sparse_samples": int(u.get("outcome_unknown_sparse_samples", 0)),
        "tracker_coverage_share": u.get("tracker_coverage_share"),
        "observations": obs,
        "hit": hit,
        "recall": rec,
        "leads": leads,
        "budget_pauses": {"rpc_cap_blocked_calls": int(bp.get("rpc_cap_blocked_calls", 0)), "x_cap_blocked_calls": int(bp.get("x_cap_blocked_calls", 0)),
                          "budget_cap_gap_s": int(bp.get("budget_cap_gap_s", 0)), "trade_stream_envelope_paused": int(bp.get("trade_stream_envelope_paused", 0))},
        "cost": {"rpc_cu_estimate": cost.get("rpc_cu_estimate"), "rpc_cu_cap": cost.get("rpc_cu_cap"), "x_usd": cost.get("x_usd"),
                 "x_usd_cap": cost.get("x_usd_cap"), "dexscreener_requests": cost.get("dexscreener_requests")},
        "tracker_skipped": ops.get("tracker_skipped"),
    }


# ----- segment aggregation -----------------------------------------------------------------------------
def _sum(blocks: list[dict[str, Any]], *path: str) -> int:
    tot = 0
    for b in blocks:
        v: Any = b
        for k in path:
            v = (v or {}).get(k)
        tot += int(v or 0)
    return tot


def build_segment(reps: list[dict[str, Any]], rvs: list[str], group_key: str, mv: str, cfg: dict[str, Any], frozen_fp: str | None) -> dict[str, Any]:
    wcfg = cfg.get("week") or {}
    bcfg = wcfg.get("bootstrap") or {}
    draws, seed, alpha = int(bcfg.get("draws", 9999)), int(bcfg.get("seed", 20261026)), float(bcfg.get("alpha", 0.05))
    min_days, min_winners = int(bcfg.get("min_days", 3)), int(bcfg.get("min_winners", 20))
    edges = list((cfg.get("report") or {}).get("lead_time_bins_s", [-3600, -600, 0, 600, 3600]))
    blocks = [day_block(r, rvs) for r in reps]
    covered = [b for b in blocks if b["covered"]]
    not_covered = [b["day"] for b in blocks if not b["covered"]]
    winners = _sum(covered, "winners")
    enough = len(covered) >= min_days and winners >= min_winners
    reason = None
    if not enough:
        parts = []
        if len(covered) < min_days:
            parts.append(f"{len(covered)} gedekte dagen (< {min_days})")
        if winners < min_winners:
            parts.append(f"{winners} winnaars (< {min_winners})")
        reason = ", ".join(parts)

    def ci_for(num_key: tuple[str, ...], den_key: tuple[str, ...]) -> dict[str, Any]:
        bl = [(_sum([b], *num_key), _sum([b], *den_key)) for b in covered]
        if not enough:
            return {"status": TOO_LITTLE, "reason": reason, "interval": None}
        iv = bootstrap_ratio(bl, draws, seed, alpha)
        if iv is None:
            return {"status": TOO_LITTLE, "reason": "geen enkele trekking had een noemer > 0", "interval": None}
        return {"status": "ok", "reason": None, "interval": iv}

    hit = {k: _sum(covered, "hit", k) for k in ("decisions", "hits", "misses", "unknown", "pending", "elapsed")}
    hit["rate"] = _ratio(hit["hits"], hit["elapsed"])
    hit["rate_known"] = _ratio(hit["hits"], hit["hits"] + hit["misses"])
    hit["ci"] = ci_for(("hit", "hits"), ("hit", "elapsed"))
    hit["definition"] = ("raak = winnaar binnen 24 u; noemer = beslissingen waarvan de 24 u verstreken zijn, onbekende uitkomst meegeteld "
                         "(een niet-meetbare observatie is geen aangetoonde treffer); nog bezig apart, niet in de noemer")

    rec: dict[str, Any] = {"winners": winners, "with_first_trade": _sum(covered, "recall", "with_first_trade")}
    for ev in ("graduation", "first_trade"):
        den = winners if ev == "graduation" else rec["with_first_trade"]
        ev_out: dict[str, Any] = {"denominator": den}
        for kind in ("any", "coin", "theme"):
            n = _sum(covered, "recall", f"{kind}_before_{ev}")
            ev_out[kind] = {"n": n, "rate": _ratio(n, den)}
        ev_out["any"]["ci"] = ci_for(("recall", f"any_before_{ev}"), ("winners",) if ev == "graduation" else ("recall", "with_first_trade"))
        rec[f"before_{ev}"] = ev_out

    leads: dict[str, Any] = {}
    for key in ("any_vs_graduation", "coin_vs_graduation", "theme_vs_graduation", "any_vs_first_trade"):
        vals = [v for b in covered for v in b["leads"].get(key, [])]
        leads[key] = _quantiles(vals)
        leads[key]["bins"] = lead_bins(vals, edges)
    leads["bins_edges_s"] = edges
    leads["definition"] = "lead time = gebeurtenis minus de vroegste observe-/thema-beslissing van deze regels; positief = op tijd"

    obs_total = _sum(covered, "observations", "observe") + _sum(covered, "observations", "observe_meta")
    observations = {"observe": _sum(covered, "observations", "observe"), "observe_meta": _sum(covered, "observations", "observe_meta"),
                    "covered_days": len(covered), "mean_per_day": (obs_total / len(covered)) if covered else None}

    universe = {
        "covered_days": len(covered), "not_covered_days": not_covered,
        "complete_days": sum(1 for b in covered if b["gate_complete"]),
        "creates_pp": _sum(covered, "creates_pp"), "creates_rpc": _sum(covered, "creates_rpc"), "creates_union": _sum(covered, "creates_union"),
        "graduations": _sum(covered, "graduations"), "grads_coin_unknown": _sum(covered, "grads_coin_unknown"),
        "measured": _sum(covered, "measured"), "pending": _sum(covered, "pending"), "missing": _sum(covered, "missing"),
        "winners": winners, "winners_organic": _sum(covered, "winners_organic"), "winners_bundled": _sum(covered, "winners_bundled"),
        "winners_bundled_unknown": _sum(covered, "winners_bundled_unknown"),
        "not_winner": _sum(covered, "not_winner"), "outcome_unknown": _sum(covered, "outcome_unknown"),
        "outcome_unknown_sparse_samples": _sum(covered, "outcome_unknown_sparse_samples"),
        "hours_not_covered": _sum(blocks, "hours_not_covered"),
        "budget_pauses": {k: _sum(covered, "budget_pauses", k) for k in ("rpc_cap_blocked_calls", "x_cap_blocked_calls", "budget_cap_gap_s", "trade_stream_envelope_paused")},
        "tracker_skipped": _sum(covered, "tracker_skipped"),
    }
    measured = universe["measured"]
    tc_cov = sum(round(float(b["tracker_coverage_share"]) * int(b["measured"] or 0)) for b in covered if b["tracker_coverage_share"] is not None)
    universe["tracker_coverage_share"] = (tc_cov / measured) if measured else None

    seg = {
        "rules_group": group_key,
        "rules_versions": list(rvs),
        "frozen": frozen_fp is not None and group_key == frozen_fp,
        "method_version": mv,
        "days": blocks,
        "universe": universe,
        "hit_rate": hit,
        "recall": rec,
        "lead_time": leads,
        "observations": observations,
        "bootstrap": {"draws": draws, "seed": seed, "alpha": alpha, "min_days": min_days, "min_winners": min_winners,
                      "enough_data": enough, "reason": reason},
    }
    seg["gate"] = evaluate_gate(seg, cfg.get("community_gate") or {})
    return seg


def evaluate_gate(seg: dict[str, Any], gate: dict[str, Any]) -> dict[str, Any]:
    """The frozen go/no-go test, per criterion voldaan / niet voldaan / te weinig data. Shows, never decides."""
    crit: list[dict[str, Any]] = []

    def add(key: str, label: str, value: Any, threshold: Any, op: str) -> None:
        if value is None:
            status = TOO_LITTLE
        elif op == ">=":
            status = MET if float(value) >= float(threshold) else NOT_MET
        else:
            status = MET if float(value) <= float(threshold) else NOT_MET
        crit.append({"key": key, "label": label, "value": value, "threshold": threshold, "op": op, "status": status})

    u = seg["universe"]
    add("complete_days", "minimaal {t} complete dagen (piekuren 12:00 tot 02:00 UTC gedekt)".format(t=gate.get("min_complete_days", 5)),
        u["complete_days"], int(gate.get("min_complete_days", 5)), ">=")
    add("winners", "minimaal {t} winnaars in het universum".format(t=gate.get("min_winners", 30)), u["winners"], int(gate.get("min_winners", 30)), ">=")
    hit_ci = seg["hit_rate"]["ci"]
    add("hit_rate_ci_low", "ondergrens bootstrapinterval trefkans munt-observaties ≥ {t}".format(t=gate.get("min_hit_rate_ci_low", 0.15)),
        None if hit_ci.get("interval") is None else hit_ci["interval"]["low"], float(gate.get("min_hit_rate_ci_low", 0.15)), ">=")
    rec_ci = seg["recall"]["before_graduation"]["any"]["ci"]
    add("recall_before_graduation_ci_low", "ondergrens bootstrapinterval recall vóór graduatie ≥ {t}".format(t=gate.get("min_recall_before_graduation_ci_low", 0.25)),
        None if rec_ci.get("interval") is None else rec_ci["interval"]["low"], float(gate.get("min_recall_before_graduation_ci_low", 0.25)), ">=")
    med = seg["lead_time"]["any_vs_graduation"]["median_s"]
    add("median_lead_s_before_graduation", "mediane lead time vóór graduatie ≥ {t} minuten".format(t=round(float(gate.get("min_median_lead_s_before_graduation", 600)) / 60)),
        med if seg["bootstrap"]["enough_data"] else None, float(gate.get("min_median_lead_s_before_graduation", 600)), ">=")
    add("observations_per_day", "gemiddeld hoogstens {t} observaties per dag".format(t=gate.get("max_observations_per_day", 12)),
        seg["observations"]["mean_per_day"], float(gate.get("max_observations_per_day", 12)), "<=")
    statuses = [c["status"] for c in crit]
    if all(s == MET for s in statuses):
        verdict = "GO: alle criteria voldaan"
    elif any(s == NOT_MET for s in statuses):
        verdict = "NO-GO: niet alle criteria voldaan"
    else:
        verdict = "TE WEINIG DATA: nog geen oordeel mogelijk"
    return {"criteria": crit, "verdict": verdict,
            "note": ("Dit rapport beslist niets en verstuurt niets; het toont alleen de toets. De drempels zijn startwaarden van de bouwer "
                     "(config/measure.json → community_gate, docs/DECISIONS.md, 26 oktober 2026); Jim kan ze bijstellen vóór het einde van "
                     "de meetweek, nooit erna.")}


# ----- the report --------------------------------------------------------------------------------------
def aggregate(day_reports_by_mv: dict[str, list[dict[str, Any]]], fp_map: dict[str, str], cfg: dict[str, Any], end_day: str, days: int,
              now: float, rv_now: str, fp_now: str, synthetic: bool = False) -> dict[str, Any]:
    gate_cfg = cfg.get("community_gate") or {}
    frozen = gate_cfg.get("frozen_rules") or {}
    frozen_fp = frozen.get("rules_fingerprint")
    segments: list[dict[str, Any]] = []
    for mv in sorted(day_reports_by_mv):
        reps = day_reports_by_mv[mv]
        rvs: set[str] = set()
        for r in reps:
            rvs |= set((r.get("observations_by_rules_version") or {}).keys())
            rvs |= set((r.get("precision_by_rules_version") or {}).keys())
            for w in r.get("winners") or []:
                rvs |= set((w.get("earliest_by_rules_version") or {}).keys())
        if not rvs:
            rvs = {rv_now}
        for key, group in rules_groups(rvs, fp_map).items():
            segments.append(build_segment(reps, group, key, mv, cfg, frozen_fp))
    # the frozen rules group first, then the rest
    segments.sort(key=lambda s: (not s["frozen"], s["rules_group"], s["method_version"]))
    any_reps = next(iter(day_reports_by_mv.values()), [])
    costs = []
    for r in any_reps:
        b = day_block(r, [])
        costs.append({"day": b["day"], "covered": b["covered"], **b["cost"], "budget_pauses": b["budget_pauses"]})
    shadow_days = [{"day": b["day"], "covered": b["covered"], **b["x_shadow"]} for b in (day_block(r, []) for r in any_reps)]
    x_shadow = {"per_day": shadow_days, "active_days": sum(1 for d in shadow_days if d["active"]),
                "decisions": sum(d["decisions"] for d in shadow_days), "confirmed_1h": sum(d["confirmed_1h"] for d in shadow_days),
                "confirmed_24h": sum(d["confirmed_24h"] for d in shadow_days), "observations": sum(d["observations"] for d in shadow_days),
                "elapsed": sum(d["elapsed"] for d in shadow_days), "winner": sum(d["winner"] for d in shadow_days),
                "not_winner": sum(d["not_winner"] for d in shadow_days), "unknown": sum(d["unknown"] for d in shadow_days),
                "pending": sum(d["pending"] for d in shadow_days)}
    x_shadow["rate"] = _ratio(x_shadow["winner"], x_shadow["elapsed"])
    x_shadow["rate_known"] = _ratio(x_shadow["winner"], x_shadow["winner"] + x_shadow["not_winner"])
    day_list = window_days(end_day, days)
    return {
        "kind": "week",
        "x_shadow": x_shadow,
        "synthetic": synthetic,
        "start_day": day_list[0],
        "end_day": end_day,
        "days": int(days),
        "day_list": day_list,
        "calendar_week": is_calendar_week(end_day, days),
        "generated_at": utc_iso(now),
        "rules_version_now": rv_now,
        "rules_fingerprint_now": fp_now,
        "frozen_rules": frozen,
        "method_versions": sorted(day_reports_by_mv),
        "rules_fingerprints": fp_map,
        "costs": {"per_day": costs, "rpc_cu_estimate_total": sum(int(c.get("rpc_cu_estimate") or 0) for c in costs),
                  "x_usd_total": round(sum(float(c.get("x_usd") or 0.0) for c in costs), 4), "cu_calibrated": False, "cu_note": CU_NOTE},
        "winner_definition": cfg.get("winner"),
        "gate_config": {k: v for k, v in gate_cfg.items() if not k.startswith("_")},
        "segments": segments,
    }


def build_week_report(conn: sqlite3.Connection, settings: Settings, end_day: str, days: int, now: float) -> dict[str, Any]:
    cfg = settings.config("measure")
    day_list = window_days(end_day, days)
    start, _ = day_bounds(day_list[0])
    _, end = day_bounds(day_list[-1])
    rv_now, fp_now = rules_version(), rules_fingerprint()
    mvs = method_versions_in_window(conn, start, end, str(cfg["method_version"]))
    reps = {mv: [build_report(conn, settings, d, now, method_version=mv) for d in day_list] for mv in mvs}
    fp_map = rules_fingerprint_map(conn, cfg.get("community_gate") or {}, rv_now, fp_now)
    return aggregate(reps, fp_map, cfg, end_day, days, now, rv_now, fp_now)


# ----- markdown ----------------------------------------------------------------------------------------
def _pct(x: Any) -> str:
    return "onbekend" if x is None else f"{100 * float(x):.0f}%"


def _n(x: Any) -> str:
    if x is None:
        return "onbekend"
    if isinstance(x, bool):
        return "ja" if x else "nee"
    if isinstance(x, (int, float)):
        return f"{x:,.0f}"
    return str(x)


def _min(seconds: Any) -> str:
    return "onbekend" if seconds is None else f"{float(seconds) / 60:.1f} min"


def _f1(x: Any) -> str:
    return "onbekend" if x is None else f"{float(x):.1f}"


def _usd(x: Any) -> str:
    return "niet geconfigureerd" if x is None else f"{float(x):.2f}"


def _ci(ci: dict[str, Any]) -> str:
    if ci.get("interval") is None:
        return f"{TOO_LITTLE}" + (f" ({ci.get('reason')})" if ci.get("reason") else "")
    iv = ci["interval"]
    return f"[{_pct(iv['low'])} – {_pct(iv['high'])}] (punt {_pct(iv['point'])}; {iv['draws']} trekkingen, seed {iv['seed']}, alpha {iv['alpha']}, {iv['blocks']} dagblokken)"


def _bin_label(b: dict[str, Any]) -> str:
    lo, hi = b.get("from_s"), b.get("to_s")
    if lo is None:
        return f"< {hi / 60:.0f} min"
    if hi is None:
        return f"≥ {lo / 60:.0f} min"
    return f"{lo / 60:.0f} tot {hi / 60:.0f} min"


def render_markdown(r: dict[str, Any]) -> str:
    kind = "hele kalenderweek (maandag t/m zondag)" if r.get("calendar_week") else "rollend venster"
    lines = [
        f"# Weekrapport recall {r['start_day']} t/m {r['end_day']} ({r['days']} dagen, {kind})",
        "",
        f"Gegenereerd {r['generated_at']} · rules_version nu {r['rules_version_now']} · regels-vingerafdruk nu `{str(r.get('rules_fingerprint_now'))[:19]}` · "
        f"method_version(s) {', '.join(r.get('method_versions') or [])}" + (" · **SYNTHETISCH (testdata)**" if r.get("synthetic") else ""),
        "",
        "Alles hieronder staat per segment: één set regels (rules_versions met dezelfde vingerafdruk van de regelbestanden) × één meetmethode. "
        "Een dag zonder gedekt uur is NIET GEDEKT: zijn universum is onbekend (niet nul), hij telt niet als dagblok en niet als complete dag.",
    ]
    fr = r.get("frozen_rules") or {}
    if fr:
        lines.append(f"Bevroren regels van de meetweek: rules_version `{fr.get('rules_version')}` sinds {fr.get('since')} "
                     f"(vingerafdruk `{str(fr.get('rules_fingerprint'))[:19]}`, eerste volle dag {fr.get('first_full_day')}).")
    for seg in r.get("segments") or []:
        u, hit, rec, lt, obs, g = seg["universe"], seg["hit_rate"], seg["recall"], seg["lead_time"], seg["observations"], seg["gate"]
        title = f"## Regels `{seg['rules_group'][:19]}`" + (" (bevroren meetweek)" if seg.get("frozen") else "") + f" · method_version `{seg['method_version']}`"
        lines += [
            "",
            title,
            "",
            f"rules_versions in dit segment: {', '.join(f'`{v}`' for v in seg['rules_versions'])}",
            "",
            "### Samenvatting",
            "",
            f"- Gedekte dagen: **{u['covered_days']}** van {len(seg['days'])}" + (f" · NIET GEDEKT: {', '.join(u['not_covered_days'])}" if u["not_covered_days"] else "")
            + f" · complete dagen (piekuren gedekt): {u['complete_days']} · uren NIET GEDEKT in totaal: {u['hours_not_covered']}",
            f"- Graduaties (gedekte dagen): **{u['graduations']}**, waarvan coin_unknown {u['grads_coin_unknown']}; gemeten {u['measured']}, nog bezig {u['pending']}, ontbrekend {u['missing']}",
            f"- Winnaars: **{u['winners']}** (organisch {u['winners_organic']}, gebundeld {u['winners_bundled']}, bundeling onbekend {u['winners_bundled_unknown']}); "
            f"geen winnaar {u['not_winner']}; uitkomst onbekend {u['outcome_unknown']} (waarvan {u['outcome_unknown_sparse_samples']} door te weinig samples)",
            f"- Trefkans munt-observaties: **{_pct(hit['rate'])}** = {hit['hits']} raak van {hit['elapsed']} met verstreken 24 u "
            f"(mis {hit['misses']}, onbekend {hit['unknown']} in de noemer; nog bezig {hit['pending']} apart); over bekende uitkomsten {_pct(hit['rate_known'])}",
            f"- Recall vóór graduatie (munt- of thema-beslissing): **{_pct(rec['before_graduation']['any']['rate'])}** "
            f"({rec['before_graduation']['any']['n']}/{rec['winners']}); vóór eerste trade: {_pct(rec['before_first_trade']['any']['rate'])} "
            f"({rec['before_first_trade']['any']['n']}/{rec['with_first_trade']} winnaars met bekende eerste trade)",
            f"- Lead time vóór graduatie: mediaan {_min(lt['any_vs_graduation']['median_s'])} (p25 {_min(lt['any_vs_graduation']['p25_s'])}, p75 {_min(lt['any_vs_graduation']['p75_s'])}, n {lt['any_vs_graduation']['n']})",
            f"- Observaties per gedekte dag: {_f1(obs['mean_per_day'])} (munt {obs['observe']}, thema {obs['observe_meta']})",
            f"- Dekking van de tracker: {_pct(u['tracker_coverage_share'])} van de gemeten graduaties had minstens elke 10 min een sample; "
            f"de tracker sloeg {u['tracker_skipped']} munten over",
            "",
            "### Universum per dag",
            "",
            "| dag | status | creates PP / RPC / unie | graduaties | coin_unknown | winnaars (org / geb / onb) | geen winnaar | onbekend | uren niet gedekt | piekuren niet gedekt | budgetpauzes | tracker |",
            "|---|---|---|---:|---:|---|---:|---:|---:|---:|---|---:|",
        ]
        for b in seg["days"]:
            if not b["covered"]:
                lines.append(f"| {b['day']} | **NIET GEDEKT** | – | onbekend | – | – | – | – | 24 | – | – | – |")
                continue
            bp = b["budget_pauses"]
            pauses = (f"rpc {bp['rpc_cap_blocked_calls']}, gat {bp['budget_cap_gap_s']} s, envelop {bp['trade_stream_envelope_paused']}, x {bp['x_cap_blocked_calls']}")
            lines.append(f"| {b['day']} | {'compleet' if b['coverage_status'] == 'complete' else 'niet compleet'} | {_n(b['creates_pp'])} / {_n(b['creates_rpc'])} / {_n(b['creates_union'])} | "
                         f"{_n(b['graduations'])} | {_n(b['grads_coin_unknown'])} | {b['winners']} ({b['winners_organic']} / {b['winners_bundled']} / {b['winners_bundled_unknown']}) | "
                         f"{b['not_winner']} | {b['outcome_unknown']} | {b['hours_not_covered']} | {_n(b['peak_hours_not_covered'])} | {pauses} | {_pct(b['tracker_coverage_share'])} |")
        lines += [
            "",
            "### Trefkans van munt-observaties",
            "",
            f"- {hit['definition']}.",
            f"- Beslissingen: {hit['decisions']}; verstreken 24 u: {hit['elapsed']} (raak {hit['hits']}, mis {hit['misses']}, onbekend {hit['unknown']}); nog bezig: {hit['pending']}",
            f"- Trefkans: {_pct(hit['rate'])}; over bekende uitkomsten: {_pct(hit['rate_known'])}",
            f"- Bootstrapinterval (95%): {_ci(hit['ci'])}",
            "",
            "| dag | beslissingen | verstreken | raak | mis | onbekend | bezig | trefkans |",
            "|---|---:|---:|---:|---:|---:|---:|---:|",
        ]
        for b in seg["days"]:
            if not b["covered"]:
                lines.append(f"| {b['day']} | NIET GEDEKT | – | – | – | – | – | – |")
                continue
            h = b["hit"]
            lines.append(f"| {b['day']} | {h['decisions']} | {h['elapsed']} | {h['hits']} | {h['misses']} | {h['unknown']} | {h['pending']} | {_pct(_ratio(h['hits'], h['elapsed']))} |")
        lines += [
            "",
            "### Recall",
            "",
            f"- Winnaars: {rec['winners']}; met bekende eerste trade: {rec['with_first_trade']}",
            f"- Vóór graduatie: munt- of thema-beslissing {rec['before_graduation']['any']['n']}/{rec['winners']} ({_pct(rec['before_graduation']['any']['rate'])}); "
            f"munt {rec['before_graduation']['coin']['n']}, thema {rec['before_graduation']['theme']['n']}",
            f"- Vóór eerste trade: munt- of thema-beslissing {rec['before_first_trade']['any']['n']}/{rec['with_first_trade']} ({_pct(rec['before_first_trade']['any']['rate'])}); "
            f"munt {rec['before_first_trade']['coin']['n']}, thema {rec['before_first_trade']['theme']['n']}",
            f"- Bootstrapinterval recall vóór graduatie (95%): {_ci(rec['before_graduation']['any']['ci'])}",
            f"- Bootstrapinterval recall vóór eerste trade (95%): {_ci(rec['before_first_trade']['any']['ci'])}",
            "",
            "### Lead time",
            "",
            f"- {lt['definition']}.",
        ]
        for key, label in (("any_vs_graduation", "munt of thema t.o.v. graduatie"), ("coin_vs_graduation", "munt t.o.v. graduatie"),
                           ("theme_vs_graduation", "thema t.o.v. graduatie"), ("any_vs_first_trade", "munt of thema t.o.v. eerste trade")):
            q = lt[key]
            lines.append(f"- {label}: n {q['n']}, mediaan {_min(q['median_s'])}, p25 {_min(q['p25_s'])}, p75 {_min(q['p75_s'])}; op tijd {q['positive']}, te laat {q['negative_or_zero']}")
        lines += ["", "| bin | munt of thema t.o.v. graduatie | munt | thema | t.o.v. eerste trade |", "|---|---:|---:|---:|---:|"]
        for i, b in enumerate(lt["any_vs_graduation"]["bins"]):
            lines.append(f"| {_bin_label(b)} | {b['n']} | {lt['coin_vs_graduation']['bins'][i]['n']} | {lt['theme_vs_graduation']['bins'][i]['n']} | {lt['any_vs_first_trade']['bins'][i]['n']} |")
        bs = seg["bootstrap"]
        lines += [
            "",
            "### Betrouwbaarheidsintervallen",
            "",
            f"- Blokbootstrap met dagen als blokken: {bs['draws']} trekkingen, vaste seed {bs['seed']}, alpha {bs['alpha']}; alleen gedekte dagen zijn blokken; "
            f"een NIET GEDEKT-dag telt nooit als dag met nul winnaars.",
            f"- Minimaal {bs['min_days']} gedekte dagen en {bs['min_winners']} winnaars, anders '{TOO_LITTLE}': "
            + ("voldoende data" if bs["enough_data"] else f"{TOO_LITTLE} ({bs['reason']})"),
            "",
            "### Go/no-go community-alerts (bevroren criteria)",
            "",
            f"**{g['verdict']}**",
            "",
            "| criterium | waarde | drempel | toets |",
            "|---|---:|---:|---|",
        ]
        for c in g["criteria"]:
            v = c["value"]
            if v is None:
                vs = "–"
            elif c["key"] in ("hit_rate_ci_low", "recall_before_graduation_ci_low"):
                vs = _pct(v)
            elif c["key"] == "median_lead_s_before_graduation":
                vs = _min(v)
            elif isinstance(v, float):
                vs = f"{v:.1f}"
            else:
                vs = str(v)
            t = c["threshold"]
            ts = _pct(t) if c["key"] in ("hit_rate_ci_low", "recall_before_graduation_ci_low") else (_min(t) if c["key"] == "median_lead_s_before_graduation" else str(t))
            lines.append(f"| {c['label']} | {vs} | {c['op']} {ts} | **{c['status']}** |")
        lines += ["", g["note"]]
    costs = r.get("costs") or {}
    lines += [
        "",
        "## Kosten per dag",
        "",
        f"- Geschatte CU in het venster: {_n(costs.get('rpc_cu_estimate_total'))} ({costs.get('cu_note') or CU_NOTE}) · X: ${float(costs.get('x_usd_total') or 0):.2f}",
        "",
        "| dag | gedekt | CU (schatting) | CU-plafond | X (USD) | X-plafond | DexScreener-verzoeken | rpc-plafond geblokkeerd | envelop gepauzeerd |",
        "|---|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for c in costs.get("per_day") or []:
        bp = c.get("budget_pauses") or {}
        lines.append(f"| {c['day']} | {'ja' if c.get('covered') else 'NEE'} | {_n(c.get('rpc_cu_estimate'))} | {_n(c.get('rpc_cu_cap'))} | "
                     f"{_usd(c.get('x_usd'))} | "
                     f"{_usd(c.get('x_usd_cap'))} | {_n(c.get('dexscreener_requests'))} | {_n(bp.get('rpc_cap_blocked_calls'))} | {_n(bp.get('trade_stream_envelope_paused'))} |")
    xs = r.get("x_shadow") or {}
    lines += ["", "## X-schaduw", ""]
    if not xs.get("active_days"):
        lines.append("Geen schaduwgegevens in dit venster (X uit, of nog geen schaduwbeslissing of -telling).")
    else:
        lines += [
            f"- Beslissingen die X zou hebben toegevoegd: **{xs['decisions']}** op {xs['active_days']} dagen; bestaande observaties bevestigd binnen 1 u: "
            f"**{xs['confirmed_1h']}**, binnen 24 u: **{xs['confirmed_24h']}** (observaties in het venster: {xs['observations']})",
            f"- Uitkomst schaduwbeslissingen: verstreken {xs['elapsed']} (winnaar {xs['winner']}, geen winnaar {xs['not_winner']}, onbekend {xs['unknown']}), "
            f"nog bezig {xs['pending']}; trefkans {_pct(xs['rate'])}, over bekende uitkomsten {_pct(xs['rate_known'])}: dezelfde noemerregels als de echte trefkans",
            "",
            "| dag | schaduwbeslissingen | tellingen | bevestigd 1 u | bevestigd 24 u | observaties | verstreken | winnaar | geen winnaar | onbekend | bezig |",
            "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
        ]
        for d in xs.get("per_day") or []:
            lines.append(f"| {d['day']} | {d['decisions']} | {d['counts_taken']} | {d['confirmed_1h']} | {d['confirmed_24h']} | {d['observations']} | "
                         f"{d['elapsed']} | {d['winner']} | {d['not_winner']} | {d['unknown']} | {d['pending']} |")
    lines += [
        "",
        "## Versies",
        "",
        f"- rules_version nu: `{r['rules_version_now']}` (vingerafdruk `{r.get('rules_fingerprint_now')}`)",
        f"- method_version(s): {', '.join(f'`{m}`' for m in (r.get('method_versions') or []))}",
        "- Vingerafdrukken per rules_version: " + ("; ".join(f"`{rv}` → `{str(fp)[:19]}`" for rv, fp in sorted((r.get("rules_fingerprints") or {}).items())) or "geen"),
        "",
    ]
    return "\n".join(lines)


def run_week(settings: Settings, end_day: str | None, days: int | None) -> int:
    settings.ensure_dirs()
    cfg = settings.config("measure")
    n_days = int(days or (cfg.get("week") or {}).get("days", 7))
    conn = db.connect(settings.db_path)
    try:
        now = wall_now()
        end = end_day or day_key(now - 86400)
        rep = build_week_report(conn, settings, end, n_days, now)
        out_dir = settings.out_dir / "recall"
        atomic_write_json(out_dir / f"week-{end}.json", rep)
        atomic_write_text(out_dir / f"week-{end}.md", render_markdown(rep))
        db.kv_set(conn, "measure.week.last_run", str(now), now)
        db.kv_set(conn, "measure.week.last_end_day", end, now)
        seg0 = rep["segments"][0] if rep["segments"] else {}
        log.info("week.written", end_day=end, days=n_days, segments=len(rep["segments"]),
                 covered_days=(seg0.get("universe") or {}).get("covered_days"), winners=(seg0.get("universe") or {}).get("winners"),
                 verdict=(seg0.get("gate") or {}).get("verdict"))
    finally:
        conn.close()
    return 0
