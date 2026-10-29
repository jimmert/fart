"""Milestone 5, points 1, 2 and 4: the week report (`fart measure week`) on SYNTHETIC days, the frozen go/no-go
test, the tracker-fidelity rule (sparse samples -> uitkomst onbekend) and the tracker's skipped counter.

Every day built by `syn_day()` is marked `"synthetic": True`; the report prints SYNTHETISCH in its header."""
from __future__ import annotations

import json

import pytest

from fart import db
from fart.measure import week as wk
from fart.measure.report import build_report
from fart.util import day_bounds
from fart.version import rules_fingerprint

RV = "rv-frozen"
FP = "sha256:frozen-fingerprint"
DAYS = ["2026-10-27", "2026-10-28", "2026-10-29", "2026-10-30", "2026-10-31", "2026-11-01", "2026-11-02"]


def syn_day(day, *, covered=True, peak_not_covered=0, hours_covered=24, hours_complete=24, winners=(), hit=None, obs=(5, 2), rv=RV,
            creates=(100, 300, 320), grads=20, coin_unknown=1, not_winner=10, unknown=3, sparse=1, cost=None, pauses=None, tracker_share=0.9):
    """A synthetic daily report with only the fields the week aggregation reads.

    winners: (coin_lead_s | None, theme_lead_s | None) per winner, lead = graduation minus decision; the first trade is
    always 300 s before graduation. hit: dict(decisions, hits, misses, unknown, pending) of the day's coin observations.
    obs: (observe, observe_meta) counts. A day with covered=False is NIET GEDEKT: nothing of its universe is known."""
    t0 = day_bounds(day)[0] + 15 * 3600
    ws = []
    for i, (lc, lt) in enumerate(winners):
        grad_t = t0 + 60 * i
        earliest = {}
        if lc is not None or lt is not None:
            earliest[rv] = {"coin": None if lc is None else {"id": 10 * i + 1, "as_of": grad_t - lc},
                            "theme": None if lt is None else {"id": 10 * i + 2, "term": "thema", "as_of": grad_t - lt}}
        ws.append({"mint": f"W{i}", "graduated_t": grad_t, "first_trade_t": grad_t - 300.0, "bundled": False, "earliest_by_rules_version": earliest})
    hit = hit or {"decisions": 0, "hits": 0, "misses": 0, "unknown": 0, "pending": 0}
    if not covered:
        return {"synthetic": True, "day": day, "covered": False, "coverage_status": "not_covered",
                "coverage": {"hours_covered": 0, "hours_complete": 0, "peak_hours_not_covered": 14},
                "universe": {"graduations": 0, "winners": 0}, "ops": {}, "winners": [], "precision_by_rules_version": {},
                "observations_by_rules_version": {}}
    return {
        "synthetic": True, "day": day, "covered": True, "coverage_status": "complete" if hours_complete == 24 else "incomplete",
        "coverage": {"hours_covered": hours_covered, "hours_complete": hours_complete, "peak_hours_not_covered": peak_not_covered,
                     "creates_pp": creates[0], "creates_rpc": creates[1], "creates_union": creates[2]},
        "universe": {"graduations": grads, "measured": grads, "pending": 0, "missing": 0, "winners": len(ws), "winners_organic": len(ws),
                     "winners_bundled": 0, "winners_bundled_unknown": 0, "not_winner": not_winner, "outcome_unknown": unknown,
                     "outcome_unknown_sparse_samples": sparse, "tracker_coverage_share": tracker_share},
        "ops": {"migrations_coin_unknown": coin_unknown, "tracker_skipped": 0,
                "budget_pauses": pauses or {"rpc_cap_blocked_calls": 0, "x_cap_blocked_calls": 0, "budget_cap_gap_s": 0, "trade_stream_envelope_paused": 0},
                "cost": cost or {"rpc_cu_estimate": 1_200_000, "rpc_cu_cap": 3_000_000, "x_usd": None, "x_usd_cap": None, "dexscreener_requests": 288}},
        "winners": ws,
        "precision_by_rules_version": {rv: {**hit, "known": hit["hits"] + hit["misses"], "elapsed": hit["hits"] + hit["misses"] + hit["unknown"],
                                            "distinct_mints": hit["decisions"]}} if hit["decisions"] else {},
        "observations_by_rules_version": {rv: {"observe": obs[0], "observe_meta": obs[1]}},
    }


def run_aggregate(days, cfg, fp_map=None, rv_now=RV, mv="m1.0"):
    return wk.aggregate({mv: days}, fp_map or {RV: FP}, cfg, days[-1]["day"], len(days), day_bounds(days[-1]["day"])[1] + 6 * 3600 + 1800,
                        rv_now, fp_map.get(rv_now) if fp_map else FP, synthetic=True)


@pytest.fixture
def cfg(settings):
    c = json.loads(json.dumps(settings.config("measure")))
    c["community_gate"]["frozen_rules"] = {"rules_version": RV, "rules_fingerprint": FP, "since": "2026-10-26T12:18:00Z", "first_full_day": "2026-10-27"}
    return c


# ----- window, groups, bins ------------------------------------------------------------------------------------
def test_window_days_calendar_week_and_rules_groups():
    assert wk.window_days("2026-11-01", 7) == ["2026-10-26", "2026-10-27", "2026-10-28", "2026-10-29", "2026-10-30", "2026-10-31", "2026-11-01"]
    assert wk.window_days("2026-10-27", 1) == ["2026-10-27"]
    assert wk.is_calendar_week("2026-11-01", 7) is True   # a Sunday
    assert wk.is_calendar_week("2026-10-31", 7) is False and wk.is_calendar_week("2026-11-01", 6) is False
    groups = wk.rules_groups(["b", "a", "c", "d"], {"a": "sha256:x", "b": "sha256:x", "c": "sha256:y"})
    assert groups == {"sha256:x": ["a", "b"], "sha256:y": ["c"], "unknown:d": ["d"]}
    bins = wk.lead_bins([-7200, -3600, -601, -600, -1, 0, 599, 600, 3599, 3600, 99999], [-3600, -600, 0, 600, 3600])
    assert [b["n"] for b in bins] == [1, 2, 2, 2, 2, 2]
    assert bins[0] == {"from_s": None, "to_s": -3600.0, "n": 1} and bins[-1]["from_s"] == 3600.0 and bins[-1]["to_s"] is None


# ----- bootstrap -----------------------------------------------------------------------------------------------
def test_bootstrap_is_reproducible_with_a_fixed_seed():
    blocks = [(3, 10), (5, 12), (1, 9), (4, 11), (2, 8), (6, 10), (0, 7)]
    a = wk.bootstrap_ratio(blocks, 9999, 20261026, 0.05)
    b = wk.bootstrap_ratio(blocks, 9999, 20261026, 0.05)
    assert a == b and a["draws"] == 9999 and a["defined_draws"] == 9999 and a["blocks"] == 7
    assert a["point"] == pytest.approx(21 / 67)
    assert 0.0 <= a["low"] <= a["point"] <= a["high"] <= 1.0 and a["low"] < a["high"]
    c = wk.bootstrap_ratio(blocks, 9999, 1, 0.05)
    assert (c["low"], c["high"]) != (a["low"], a["high"])  # another seed is another sample of resamples
    # a wider alpha gives a narrower interval
    d = wk.bootstrap_ratio(blocks, 9999, 20261026, 0.5)
    assert d["low"] >= a["low"] and d["high"] <= a["high"]
    # a block with denominator 0 is skipped only in the draws where every pick is empty; all empty -> None
    assert wk.bootstrap_ratio([(0, 0), (0, 0)], 100, 1, 0.05) is None
    assert wk.bootstrap_ratio([], 100, 1, 0.05) is None
    e = wk.bootstrap_ratio([(2, 4), (0, 0)], 999, 7, 0.05)
    assert e["defined_draws"] < 999 and e["low"] == 0.5 == e["high"] == e["point"]


# ----- denominator rules --------------------------------------------------------------------------------------
def test_hit_rate_denominator_keeps_unknown_and_lists_pending_apart(cfg):
    days = [syn_day(d, winners=[(900, None)] * 5, hit={"decisions": 10, "hits": 4, "misses": 3, "unknown": 2, "pending": 1}) for d in DAYS]
    rep = run_aggregate(days, cfg)
    assert rep["synthetic"] is True and len(rep["segments"]) == 1
    seg = rep["segments"][0]
    h = seg["hit_rate"]
    assert (h["decisions"], h["elapsed"], h["hits"], h["misses"], h["unknown"], h["pending"]) == (70, 63, 28, 21, 14, 7)
    assert h["rate"] == pytest.approx(28 / 63) and h["rate_known"] == pytest.approx(28 / 49)
    assert h["ci"]["status"] == "ok" and h["ci"]["interval"]["point"] == pytest.approx(28 / 63)
    # every day has the same ratio: the resampled pooled ratio is always 4/9
    assert h["ci"]["interval"]["low"] == pytest.approx(4 / 9) and h["ci"]["interval"]["high"] == pytest.approx(4 / 9)
    md = wk.render_markdown(rep)
    assert "onbekend 14 in de noemer; nog bezig 7 apart" in md and "SYNTHETISCH" in md
    assert "Trefkans munt-observaties: **44%** = 28 raak van 63 met verstreken 24 u" in md


def test_recall_and_lead_time_use_the_earliest_coin_or_theme_decision(cfg):
    # per day 5 winners: coin 15 min early, theme 2 min early, theme only 30 min early, coin late (-5 min), none
    pattern = [(900, 120), (None, 1800), (-300, None), (60, 7200), (None, None)]
    days = [syn_day(d, winners=pattern, hit={"decisions": 4, "hits": 2, "misses": 2, "unknown": 0, "pending": 0}) for d in DAYS]
    seg = run_aggregate(days, cfg)["segments"][0]
    rec = seg["recall"]
    assert rec["winners"] == 35 and rec["with_first_trade"] == 35
    # any before graduation: 4 of 5 per day (the late coin and the undetected one fail... the late coin has no theme) -> 3 of 5
    assert rec["before_graduation"]["any"]["n"] == 21 and rec["before_graduation"]["coin"]["n"] == 14 and rec["before_graduation"]["theme"]["n"] == 21
    assert rec["before_graduation"]["any"]["rate"] == pytest.approx(21 / 35)
    # before the first trade (300 s before graduation): coin 900 (yes), theme 1800 (yes), theme 7200 (yes); 120 and 60 are too late
    assert rec["before_first_trade"]["any"]["n"] == 21 and rec["before_first_trade"]["coin"]["n"] == 7 and rec["before_first_trade"]["theme"]["n"] == 14
    lt = seg["lead_time"]["any_vs_graduation"]
    # earliest decision per winner: 900, 1800, -300, 7200 -> per day; over 7 days the median is the middle of {900, 1800, -300, 7200}
    assert lt["n"] == 28 and lt["median_s"] == 1350 and lt["p25_s"] == 600 and lt["p75_s"] == 3150
    assert lt["positive"] == 21 and lt["negative_or_zero"] == 7
    assert [b["n"] for b in lt["bins"]] == [0, 0, 7, 0, 14, 7]
    assert seg["lead_time"]["coin_vs_graduation"]["n"] == 21 and seg["lead_time"]["theme_vs_graduation"]["n"] == 21
    assert seg["lead_time"]["any_vs_first_trade"]["median_s"] == 1050


# ----- NIET GEDEKT days are not zero -------------------------------------------------------------------------------
def test_not_covered_days_are_no_blocks_and_never_count_as_zero_winners(cfg):
    good = {"decisions": 6, "hits": 2, "misses": 3, "unknown": 1, "pending": 0}
    days = [syn_day(d, winners=[(900, None)] * 6, hit=good) if i not in (1, 4) else syn_day(d, covered=False) for i, d in enumerate(DAYS)]
    rep = run_aggregate(days, cfg)
    seg = rep["segments"][0]
    u = seg["universe"]
    assert u["covered_days"] == 5 and u["not_covered_days"] == ["2026-10-28", "2026-10-31"] and u["complete_days"] == 5
    assert u["winners"] == 30 and u["graduations"] == 100 and u["hours_not_covered"] == 48
    # the bootstrap has 5 blocks, exactly the covered days: identical to a direct bootstrap over those 5 days
    iv = seg["hit_rate"]["ci"]["interval"]
    assert iv["blocks"] == 5
    direct = wk.bootstrap_ratio([(2, 6)] * 5, 9999, 20261026, 0.05)
    assert (iv["low"], iv["high"], iv["point"]) == (direct["low"], direct["high"], direct["point"])
    # had the NIET GEDEKT days been counted as days with zero winners, the recall interval would reach down to 0
    rec_iv = seg["recall"]["before_graduation"]["any"]["ci"]["interval"]
    assert rec_iv["low"] == 1.0 == rec_iv["high"]
    wrong = wk.bootstrap_ratio([(6, 6)] * 5 + [(0, 0)] * 2, 9999, 20261026, 0.05)
    assert wrong["defined_draws"] < 9999 and wrong["low"] == 1.0  # (with (0,0) blocks the draw is undefined, not zero: never a false zero)
    rows = [b for b in seg["days"] if not b["covered"]]
    assert [b["day"] for b in rows] == ["2026-10-28", "2026-10-31"] and all(b["graduations"] in (0, None) and b["hours_not_covered"] == 24 for b in rows)
    md = wk.render_markdown(rep)
    assert "| 2026-10-28 | **NIET GEDEKT** | – | onbekend |" in md and "NIET GEDEKT: 2026-10-28, 2026-10-31" in md
    assert "Gedekte dagen: **5** van 7" in md


def test_too_little_data_below_three_days_or_twenty_winners(cfg):
    two = [syn_day(d, winners=[(900, None)] * 15, hit={"decisions": 5, "hits": 2, "misses": 3, "unknown": 0, "pending": 0}) for d in DAYS[:2]]
    seg = run_aggregate(two, cfg)["segments"][0]
    assert seg["bootstrap"]["enough_data"] is False and "2 gedekte dagen (< 3)" in seg["bootstrap"]["reason"]
    assert seg["hit_rate"]["ci"]["status"] == "te weinig data" and seg["recall"]["before_graduation"]["any"]["ci"]["interval"] is None
    few = [syn_day(d, winners=[(900, None)] * 2, hit={"decisions": 5, "hits": 2, "misses": 3, "unknown": 0, "pending": 0}) for d in DAYS]
    seg2 = run_aggregate(few, cfg)["segments"][0]
    assert seg2["universe"]["winners"] == 14 and seg2["bootstrap"]["reason"] == "14 winnaars (< 20)"
    g = {c["key"]: c["status"] for c in seg2["gate"]["criteria"]}
    assert g["hit_rate_ci_low"] == "te weinig data" and g["recall_before_graduation_ci_low"] == "te weinig data"
    assert g["median_lead_s_before_graduation"] == "te weinig data" and g["winners"] == "niet voldaan"
    assert seg2["gate"]["verdict"].startswith("NO-GO")
    md = wk.render_markdown(run_aggregate(few, cfg))
    assert "te weinig data (14 winnaars (< 20))" in md and "| **te weinig data** |" in md


# ----- the frozen go/no-go test -------------------------------------------------------------------------------------
def test_gate_prints_go_when_every_criterion_is_met_and_decides_nothing(cfg):
    hit = {"decisions": 10, "hits": 4, "misses": 4, "unknown": 2, "pending": 0}
    days = [syn_day(d, winners=[(900, 1200)] * 5, hit=hit, obs=(8, 3)) for d in DAYS]
    rep = run_aggregate(days, cfg)
    g = rep["segments"][0]["gate"]
    by = {c["key"]: c for c in g["criteria"]}
    assert by["complete_days"]["value"] == 7 and by["complete_days"]["status"] == "voldaan"
    assert by["winners"]["value"] == 35 and by["winners"]["status"] == "voldaan"
    assert by["hit_rate_ci_low"]["value"] == pytest.approx(0.4) and by["hit_rate_ci_low"]["status"] == "voldaan"
    assert by["recall_before_graduation_ci_low"]["value"] == 1.0 and by["recall_before_graduation_ci_low"]["status"] == "voldaan"
    assert by["median_lead_s_before_graduation"]["value"] == 1200 and by["median_lead_s_before_graduation"]["status"] == "voldaan"  # the theme came first
    assert by["observations_per_day"]["value"] == 11.0 and by["observations_per_day"]["status"] == "voldaan"
    assert g["verdict"] == "GO: alle criteria voldaan" and "beslist niets en verstuurt niets" in g["note"]
    assert rep["gate_config"]["min_winners"] == 30 and rep["gate_config"]["frozen_rules"]["rules_version"] == RV
    md = wk.render_markdown(rep)
    assert "### Go/no-go community-alerts (bevroren criteria)" in md and "**GO: alle criteria voldaan**" in md
    assert md.count("| **voldaan** |") == 6 and "nooit erna" in md
    # one criterion fails: too many observations and a peak hour not covered on three days
    days2 = [syn_day(d, winners=[(900, 1200)] * 5, hit=hit, obs=(12, 3), peak_not_covered=1 if i < 3 else 0) for i, d in enumerate(DAYS)]
    g2 = run_aggregate(days2, cfg)["segments"][0]["gate"]
    by2 = {c["key"]: c for c in g2["criteria"]}
    assert by2["complete_days"]["value"] == 4 and by2["complete_days"]["status"] == "niet voldaan"
    assert by2["observations_per_day"]["value"] == 15.0 and by2["observations_per_day"]["status"] == "niet voldaan"
    assert g2["verdict"] == "NO-GO: niet alle criteria voldaan"


def test_gate_thresholds_in_config_match_the_frozen_decisions(settings):
    g = settings.config("measure")["community_gate"]
    assert g["min_complete_days"] == 5 and g["min_winners"] == 30 and g["min_hit_rate_ci_low"] == 0.15
    assert g["min_recall_before_graduation_ci_low"] == 0.25 and g["min_median_lead_s_before_graduation"] == 600 and g["max_observations_per_day"] == 12
    assert set(g["peak_hours_utc"]) == {12, 13, 14, 15, 16, 17, 18, 19, 20, 21, 22, 23, 0, 1}
    assert g["frozen_rules"]["rules_version"] == "150ca73cb354" and g["frozen_rules"]["first_full_day"] == "2026-10-27"


FROZEN_FP = "sha256:e3bf4320c45c7cd3c159bc045a66164ae1754637979ddacc759dc6bd59bbbd46"   # 150ca73cb354, the measurement week
M11_FP = "sha256:147aa831c90d545ca8cd73f45c3772f854a1ec399cee961b7513b0c6ab253ace"      # milestone 11: the same rules, computed once per tick


def test_the_working_tree_still_runs_the_frozen_rules():
    """Changing a rule-defining file (config/engine.json, decide.py, features.py, meta.py, curve.py, pumpfun.json,
    stopwords.json) is a rules change: describe it in docs/DECISIONS.md first and then update
    community_gate.frozen_rules in config/measure.json on purpose. Never silently.

    Milestone 11 changed `features.py` without changing a rule (the meta baseline once per tick instead of once per term):
    the working tree carries that fingerprint, `community_gate.frozen_rules_equivalents` lists it with the date, the PR and
    the test that proves byte-equal outcomes against the frozen implementation, and the frozen entry itself is untouched."""
    from fart.util import load_json
    from fart.version import REPO_ROOT, canonical_fingerprint, frozen_rules_equivalents, runs_frozen_rules

    gate = load_json(REPO_ROOT / "config" / "measure.json")["community_gate"]
    assert gate["frozen_rules"]["rules_fingerprint"] == FROZEN_FP and gate["frozen_rules"]["rules_version"] == "150ca73cb354"
    assert rules_fingerprint() == M11_FP, "a rule-defining file changed: this is a new rules_version, not a silent edit (or a new, unproven equivalent)"
    equivalents = frozen_rules_equivalents(gate)
    assert list(equivalents) == [M11_FP] and gate["frozen_rules_equivalents"][0]["pr"] == 12 and gate["frozen_rules_equivalents"][0]["date"] == "2026-10-29"
    for e in equivalents.values():
        assert e["rules_fingerprint"].startswith("sha256:") and len(e["rules_fingerprint"]) == 71 and e["date"] and int(e["pr"]) > 0
        path, _, func = str(e["test"]).partition("::")
        assert (REPO_ROOT / path).exists() and f"def {func}(" in (REPO_ROOT / path).read_text(), e["test"]  # the proof exists
        assert e.get("changed_files") == ["fart/engine/features.py"]
    assert canonical_fingerprint(M11_FP, gate) == FROZEN_FP and runs_frozen_rules(gate) is True
    assert canonical_fingerprint("sha256:other", gate) == "sha256:other" and runs_frozen_rules(gate, "sha256:other") is False


# ----- segments per rules group and method ----------------------------------------------------------------------
def test_segments_pool_identical_fingerprints_and_separate_different_ones(cfg):
    hit_a = {"decisions": 4, "hits": 2, "misses": 2, "unknown": 0, "pending": 0}
    hit_b = {"decisions": 6, "hits": 1, "misses": 5, "unknown": 0, "pending": 0}
    days = []
    for i, d in enumerate(DAYS):
        if i < 3:
            days.append(syn_day(d, winners=[(900, None)] * 5, hit=hit_a, rv="rv-old"))      # same rules, older commit
        elif i < 5:
            days.append(syn_day(d, winners=[(900, None)] * 5, hit=hit_a, rv="rv-new"))      # the M5 deploy: same rules
        else:
            days.append(syn_day(d, winners=[(None, 600)] * 5, hit=hit_b, rv="rv-other"))    # a real rules change
    fp_map = {"rv-old": FP, "rv-new": FP, "rv-other": "sha256:other"}
    rep = run_aggregate(days, cfg, fp_map=fp_map, rv_now="rv-new")
    assert [s["rules_group"] for s in rep["segments"]] == [FP, "sha256:other"]
    frozen_seg, other = rep["segments"]
    assert frozen_seg["frozen"] is True and frozen_seg["rules_versions"] == ["rv-new", "rv-old"]
    assert frozen_seg["hit_rate"]["decisions"] == 20 and frozen_seg["hit_rate"]["hits"] == 10
    assert frozen_seg["recall"]["before_graduation"]["coin"]["n"] == 25 and frozen_seg["recall"]["before_graduation"]["theme"]["n"] == 0
    assert other["frozen"] is False and other["hit_rate"]["decisions"] == 12 and other["recall"]["before_graduation"]["theme"]["n"] == 10
    # the universe (winners) is the same for both segments: 35 winners in the window; the decisions differ
    assert frozen_seg["universe"]["winners"] == other["universe"]["winners"] == 35
    md = wk.render_markdown(rep)
    assert "(bevroren meetweek)" in md and "`rv-new`, `rv-old`" in md and "sha256:other" in md
    # two method versions -> two segments per rules group
    rep2 = wk.aggregate({"m1.0": days[:5], "m1.1": days[:5]}, fp_map, cfg, DAYS[4], 5, day_bounds(DAYS[4])[1] + 3600, "rv-new", FP, synthetic=True)
    assert [(s["method_version"], s["rules_group"]) for s in rep2["segments"]] == [("m1.0", FP), ("m1.1", FP)]
    assert rep2["method_versions"] == ["m1.0", "m1.1"]


def test_costs_and_budget_pauses_per_day_with_the_calibration_caveat(cfg):
    pauses = {"rpc_cap_blocked_calls": 3, "x_cap_blocked_calls": 0, "budget_cap_gap_s": 1200, "trade_stream_envelope_paused": 2}
    days = [syn_day(d, winners=[(900, None)] * 5, cost={"rpc_cu_estimate": 2_000_000 + i, "rpc_cu_cap": 3_000_000, "x_usd": 0.5, "x_usd_cap": 3.0, "dexscreener_requests": 10},
                    pauses=pauses) for i, d in enumerate(DAYS)]
    rep = run_aggregate(days, cfg)
    assert rep["costs"]["rpc_cu_estimate_total"] == 7 * 2_000_000 + 21 and rep["costs"]["x_usd_total"] == 3.5 and rep["costs"]["cu_calibrated"] is False
    assert rep["segments"][0]["universe"]["budget_pauses"] == {"rpc_cap_blocked_calls": 21, "x_cap_blocked_calls": 0, "budget_cap_gap_s": 8400, "trade_stream_envelope_paused": 14}
    md = wk.render_markdown(rep)
    assert "## Kosten per dag" in md and "ijking met het Alchemy-dashboard staat open" in md and "| 2026-10-27 | ja | 2,000,000 | 3,000,000 | 0.50 | 3.00 | 10 | 3 | 2 |" in md
    assert "rpc 3, gat 1200 s, envelop 2, x 0" in md


# ----- end to end on a database -------------------------------------------------------------------------------------
def test_week_cli_end_to_end_on_a_synthetic_world(settings, conn, capsys):
    from fart.cli import main
    from fart.util import day_key
    from tests.test_measure import T0, build_world

    from fart.measure import outcomes as oc
    from fart.net.budget import Budget, load_source_configs
    from tests.test_measure import Clock

    build_world(conn)
    budget = Budget(conn, load_source_configs(settings.config("budget"), {}), settings.lock_path, clock=Clock(T0))
    oc.Tracker(conn, settings, budget, clock=Clock(T0 + 95000)).measure_due(T0 + 95000)  # every 24 h horizon is due
    conn.execute("INSERT INTO kv(key, value, updated_at) VALUES(?, ?, 0)", ("engine.rules_fingerprint.r1", rules_fingerprint()))
    conn.close()
    end = day_key(T0)
    assert main(["--root", str(settings.root), "--quiet", "measure", "week", "--end", end, "--days", "2"]) == 0
    j = settings.out_dir / "recall" / f"week-{end}.json"
    m = settings.out_dir / "recall" / f"week-{end}.md"
    assert j.exists() and m.exists() and not list((settings.out_dir / "recall").glob("*.tmp"))
    rep = json.loads(j.read_text())
    assert rep["kind"] == "week" and rep["days"] == 2 and rep["end_day"] == end and rep["synthetic"] is False
    assert rep["method_versions"] == ["m1.0"] and len(rep["segments"]) == 1
    seg = rep["segments"][0]
    # r1 ran the working tree's rules: since milestone 11 a proven-equivalent fingerprint, pooled into the frozen segment and named there
    assert seg["rules_versions"] == ["r1"] and seg["rules_group"] == FROZEN_FP and seg["frozen"] is True
    assert seg["rules_fingerprints"] == [rules_fingerprint()] == [M11_FP] and seg["equivalents"][0]["pr"] == 12
    # build_world marks the PumpPortal source as started at epoch 1: both days are covered, the first one has no graduations
    assert seg["universe"]["covered_days"] == 2 and seg["universe"]["not_covered_days"] == []
    assert seg["universe"]["graduations"] == 5 and seg["hit_rate"]["decisions"] == 3 and seg["universe"]["winners"] == 2
    assert [b["graduations"] for b in seg["days"]] == [0, 5]
    assert seg["bootstrap"]["enough_data"] is False and seg["gate"]["verdict"].startswith("NO-GO")
    md = m.read_text()
    assert md.startswith(f"# Weekrapport recall {wk.window_days(end, 2)[0]} t/m {end} (2 dagen, rollend venster)")
    for heading in ("### Samenvatting", "### Universum per dag", "### Trefkans van munt-observaties", "### Recall", "### Lead time",
                    "### Betrouwbaarheidsintervallen", "### Go/no-go community-alerts (bevroren criteria)", "## Kosten per dag", "## Versies"):
        assert heading in md, heading
    assert "## Regels `sha256:e3bf4320c45c` (bevroren meetweek)" in md and "bewezen gelijkwaardige berekening** `sha256:147aa831c90d` (PR #12, 2026-10-29" in md
    assert main(["--root", str(settings.root), "--quiet", "status"]) == 0
    out = capsys.readouterr().out
    assert f"week report: last end day {end}" in out and "rules fingerprints (engine): r1=" in out


# ----- point 4: tracker fidelity --------------------------------------------------------------------------------------
def _grad_with_outcome(conn, mint, t, hit, peak, samples_every_s, n_samples=None):
    conn.execute("INSERT INTO coins(mint, name, symbol, block_time, received_at, dev_buy_sol, source, sources) VALUES(?, 'n', 'S', ?, ?, 1.0, 'pumpportal', '[]')", (mint, t - 600, t - 599))
    conn.execute("INSERT INTO migrations(mint, block_time, received_at, source, sources) VALUES(?, ?, ?, 'rpc_logs', '[]')", (mint, t, t + 1))
    conn.execute("INSERT INTO outcomes(decision_id, subject, mint, anchor_time, horizon_s, measured_at, peak_mcap_usd, peak_src, volume_usd, hit_100k, bundled, method_version)"
                 " VALUES(NULL, 'graduation', ?, ?, 86400, ?, ?, 'dexscreener', 1000, ?, 0, 'm1.0')", (mint, t, t + 90000, peak, hit))
    if samples_every_s:
        k = 0
        while t + k <= t + 86400 and (n_samples is None or k // samples_every_s < n_samples):
            conn.execute("INSERT INTO mcap_samples(mint, ts, src, mcap_usd, sol_usd) VALUES(?, ?, 'dexscreener', ?, 200)", (mint, t + k + 3, peak))
            k += samples_every_s


def test_graduation_without_samples_every_10_minutes_is_unknown_never_not_winner(settings, conn):
    day = "2026-10-27"
    t0 = day_bounds(day)[0] + 10 * 3600
    _grad_with_outcome(conn, "DENSE_LOSER", t0, 0, 40000.0, 300)            # a sample every 5 min: a known miss
    _grad_with_outcome(conn, "SPARSE_LOSER", t0 + 60, 0, 40000.0, 300, 3)   # three samples, then dark: unknown
    _grad_with_outcome(conn, "GAP_LOSER", t0 + 120, 0, 40000.0, 1200)       # a sample every 20 min: unknown
    _grad_with_outcome(conn, "SPARSE_WINNER", t0 + 180, 1, 250000.0, 3600)  # a winner stays a winner
    _grad_with_outcome(conn, "NO_SAMPLES", t0 + 240, None, None, 0)         # nothing measured: unknown
    db.kv_set(conn, "ingest.pumpportal.first_started", str(t0 - 86400), t0)
    db.counter_bump_daily(conn, "measure.track.skipped", 12, t0 + 3600)
    rep = build_report(conn, settings, day, t0 + 200000)
    by = {g["mint"]: g for g in rep["graduations"]}
    assert by["DENSE_LOSER"]["outcome"] == "not_winner" and by["DENSE_LOSER"]["tracker_covered"] is True
    assert by["SPARSE_LOSER"]["outcome"] == "unknown" and by["SPARSE_LOSER"]["unknown_reason"] == "sparse_samples" and by["SPARSE_LOSER"]["tracker_covered"] is False
    assert by["GAP_LOSER"]["outcome"] == "unknown" and by["GAP_LOSER"]["samples_max_gap_s"] == 1200
    assert by["SPARSE_WINNER"]["outcome"] == "winner" and by["SPARSE_WINNER"]["tracker_covered"] is False
    assert by["NO_SAMPLES"]["outcome"] == "unknown" and "unknown_reason" not in by["NO_SAMPLES"]
    u = rep["universe"]
    assert u["winners"] == 1 and u["not_winner"] == 1 and u["outcome_unknown"] == 3 and u["outcome_unknown_sparse_samples"] == 2
    assert u["tracker_covered"] == 1 and u["tracker_not_covered"] == 4 and u["tracker_coverage_share"] == 0.2
    assert rep["tracker"] == {"max_sample_gap_s": 600.0, "measured": 5, "covered": 1, "not_covered": 4, "share": 0.2,
                              "unknown_due_to_sparse_samples": 2, "skipped_mints_today": 12}
    assert rep["unknowns"]["graduations_outcome_unknown_sparse_samples"] == 2
    # the outcomes rows are untouched: the stored method and the stored "0" stay what they were
    assert conn.execute("SELECT COUNT(*) FROM outcomes WHERE method_version='m1.0' AND hit_100k=0").fetchone()[0] == 3
    assert rep["method_version"] == "m1.0"
    from fart.measure.report import render_markdown

    md = render_markdown(rep)
    assert "1 van 5 gemeten graduaties had minstens elke 10 min een sample (20%); 2 graduaties zonder voldoende samples tellen als uitkomst onbekend" in md
    assert "de tracker sloeg vandaag 12 munten over" in md and "Tracker: 12 munten overgeslagen" in md


def test_tracker_counts_skipped_mints_and_status_shows_them(settings, conn, monkeypatch, capsys):
    from fart.cli import main
    from fart.measure import outcomes as oc
    from fart.measure import prices as pr
    from fart.net.budget import Blocked, Budget, load_source_configs
    from tests.test_measure import Clock, T0, add_coin, add_grad

    for i in range(7):
        add_coin(conn, f"G{i}", T0 - 100)
        add_grad(conn, f"G{i}", T0 - 50 + i)
    settings.configs["measure"]["sampling"]["max_mints_per_run"] = 4
    settings.configs["measure"]["sampling"]["dexscreener_batch"] = 2
    batches = {"n": 0}

    def fake_request(budget, source, method, url, **kw):
        if source != "dexscreener":
            return {"data": {"attributes": {"price_usd": "200"}}}
        if "," in url:  # a sampling batch (the SOL/USD lookup asks for one mint)
            batches["n"] += 1
            if batches["n"] >= 2:
                raise Blocked("dexscreener", "tokens", "rate_limit", 1.0)  # the gate refuses the second batch
        return []  # no pairs: nothing stored, nothing skipped

    monkeypatch.setattr(pr, "request_json", fake_request)
    budget = Budget(conn, load_source_configs(settings.config("budget"), {}), settings.lock_path, clock=Clock(T0))
    tr = oc.Tracker(conn, settings, budget, clock=Clock(T0))
    stats = tr.run()
    # 7 subjects, 4 fit the run (3 over the cap); of the 4, the first batch of 2 got no pair and the second batch was blocked
    assert stats["skipped"] == {"over_cap": 3, "dexscreener": 2} and stats["sampled"]["blocked"] == 1 and stats["sampled"]["skipped"] == 2
    assert db.kv_get(conn, "measure.track.skipped") == "5" and db.kv_get(conn, f"measure.track.skipped.{db.day_of(T0)}") == "5"
    assert json.loads(db.kv_get(conn, "measure.track.skipped.last")) == {"at": T0, "over_cap": 3, "dexscreener": 2}
    conn.close()
    assert main(["--root", str(settings.root), "--quiet", "status"]) == 0
    out = capsys.readouterr().out
    assert "tracker: skipped mints total=5 today=0 (measure.track.skipped; max_mints_per_run 600)" in out  # today = the real clock's day
    assert main(["--root", str(settings.root), "--quiet", "--json", "status"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["tracker"]["skipped_total"] == 5 and data["tracker"]["skipped_last"]["over_cap"] == 3
