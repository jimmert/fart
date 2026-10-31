"""Milestone 12, part B: the edge factory. The shared execution model (fills after the delay at the curve's quote, the exits
in their order, the costs, unknown and no fill counted apart), the statistics (a block bootstrap that is reproducible for a
seed, the definition of "bewezen"), the walk-forward split, the pre-registered hypotheses, the bundle and dev-sell features,
the harness on a synthetic world with known outcomes (reproducible expected values and intervals, unknown counted apart),
the Dutch report, the promotion gate and the autopsy."""
from __future__ import annotations

import json
import math
from pathlib import Path

import pytest

from fart import db
from fart.chain.curve import CurveParams, CurveState, buy_quote
from fart.config import load_settings
from fart.measure.paper import Costs, ExitRules, PricePoint, Position, capacity_sol, pool_buy, pool_sell
from fart.research import autopsy, features as rf, harness as rh, hypotheses as hyps, report as rr
from fart.util import day_bounds

DAYS = ["2026-10-20", "2026-10-21", "2026-10-22", "2026-10-23"]
K = 30_000_000_000 * 1_073_000_000_000_000           # the constant product of a fresh curve (virtual SOL x virtual tokens)
KINDS = ["pump", "dump", "pump", "flat", "pump", "stall", "pump", "dump", "pump", "flat", "pump", "stall", "pump", "dump", "pump", "flat", "pump", "stall", "pump", "dump"]
PUMPS_PER_DAY = KINDS.count("pump")
COIN_SQL = "INSERT INTO coins(mint, name, symbol, creator, slot, block_time, received_at, dev_buy_sol, source, sources) VALUES(?,?,?,?,?,?,?,?,'pumpportal','[]')"
TRADE_SQL = ("INSERT INTO trades(mint, slot, block_time, received_at, signature, buyer, is_buy, sol, tokens, virtual_sol, virtual_token, source)"
             " VALUES(?,?,?,?,?,?,?,?,?,?,?,'rpc_trades')")


def _trade(conn, mint, t, buyer, sol, is_buy, vsol, slot, n):
    conn.execute(TRADE_SQL, (mint, slot, t, t + 0.4, f"{mint[:7]}-{n}-{int(t * 10)}", buyer, 1 if is_buy else 0, sol, sol * 1e6, vsol, K // vsol))


def build_lab(root: Path) -> dict:
    """Four days, twenty coins a day with known fates: pumps (accelerating buys, cross 75 SOL, keep rising), dumps (a 6 SOL dev
    buy, sells), flats, stalls (trades stop: unknown). Two pumps a day graduate; one of them is a winner."""
    settings = load_settings(root=root, env={"FART_RULES_VERSION": "test-rules-v1"})
    settings.ensure_dirs()
    conn = db.connect(settings.db_path)
    n = 0
    grads = []
    conn.execute("BEGIN")
    for day in DAYS:
        d0 = day_bounds(day)[0]
        for i, kind in enumerate(KINDS):
            n += 1
            mint = f"Lab{n:04d}" + "m" * 36
            t0 = d0 + 3600 * (8 + (i % 12)) + i * 37
            creator = f"Maker{i % 7}"
            dev = 6.0 if kind == "dump" else 0.3
            slot = 1000 + n
            conn.execute(COIN_SQL, (mint, f"Lab coin {n}", f"L{n}", creator, slot, t0, t0 + 0.5, dev))
            vsol = 30_000_000_000 + int(dev * 1e9)
            k = 0
            _trade(conn, mint, t0 + 1, creator, dev, True, vsol, slot, k)
            for j in range(40):                                  # 2.5 minutes of buys by distinct wallets
                k += 1
                vsol += int(0.5e9)
                _trade(conn, mint, t0 + 10 + j * 4, f"W{n}-{j}", 0.5, True, vsol, slot + 1 + j, k)
            if kind == "pump":
                for j in range(200):                             # 20 more minutes of buying: the curve crosses 75 SOL and keeps rising
                    k += 1
                    vsol += int(0.4e9)
                    _trade(conn, mint, t0 + 180 + j * 6, f"P{n}-{j}", 0.4, True, vsol, 2000 + j, k)
            elif kind == "dump":
                for j in range(120):
                    k += 1
                    vsol = max(31_000_000_000, vsol - int(0.3e9))
                    _trade(conn, mint, t0 + 180 + j * 6, f"D{n}-{j}", 0.3, False, vsol, 3000 + j, k)
            elif kind == "flat":
                for j in range(200):
                    k += 1
                    vsol += int(0.05e9) if j % 2 else -int(0.05e9)
                    _trade(conn, mint, t0 + 180 + j * 6, f"F{n}-{j}", 0.05, bool(j % 2), vsol, 4000 + j, k)
            else:
                for j in range(3):
                    k += 1
                    vsol += int(0.1e9)
                    _trade(conn, mint, t0 + 180 + j * 6, f"S{n}-{j}", 0.1, True, vsol, 5000 + j, k)
            if kind == "pump" and i in (0, 2):
                gt = t0 + 1500
                conn.execute("INSERT INTO migrations(mint, block_time, received_at, source, sources, pool_address) VALUES(?,?,?,?,?,?)", (mint, gt, gt + 1, "rpc_logs", "[]", f"Pool{n}"))
                conn.execute("INSERT INTO outcomes(decision_id, subject, mint, anchor_time, horizon_s, measured_at, peak_mcap_usd, hit_100k, bundled, method_version)"
                             " VALUES(NULL, 'graduation', ?, ?, 86400, ?, ?, ?, 0, 'm1.0')", (mint, gt, gt + 86400, 150000.0 if i == 0 else 60000.0, 1 if i == 0 else 0))
                conn.execute("INSERT INTO mcap_samples(mint, ts, src, mcap_usd, volume_h24_usd, sol_usd) VALUES(?,?,?,?,?,?)", (mint, gt + 1800, "dexscreener", 120000.0, 50000.0, 180.0))
                grads.append((mint, gt, i == 0))
    conn.execute("COMMIT")
    return {"settings": settings, "conn": conn, "grads": grads, "now": day_bounds(DAYS[-1])[1] + 3600, "coins": n}


@pytest.fixture(scope="module")
def lab(tmp_path_factory):
    from fart import version

    mp = pytest.MonkeyPatch()
    mp.setenv("FART_RULES_VERSION", "test-rules-v1")
    version._CACHE.pop("v", None)
    world = build_lab(tmp_path_factory.mktemp("lab"))
    yield world
    world["conn"].close()
    mp.undo()


@pytest.fixture(scope="module")
def h2(lab):
    """H2 on the lab, three variants, the proven threshold lowered to the size of the lab; the report written once."""
    res = rh.run_hypothesis(lab["conn"], lab["settings"], "H2", DAYS, min_cases=5, min_select_cases=3, variants=["m5_ts5", "m5_ts10", "m10_ts15"])
    md, js = rr.write_report(lab["settings"], res, lab["now"])
    return {"result": res, "md": md, "js": js}


@pytest.fixture
def params(lab) -> CurveParams:
    return CurveParams.from_config(lab["settings"].config("pumpfun"))


# ----- helpers for the execution model ---------------------------------------------------------------------------------
def point(t: float, mult: float, **kw) -> PricePoint:
    """A curve point whose spot is `mult` times the fresh curve's spot (the constant product kept)."""
    vsol = int(30_000_000_000 * math.sqrt(mult))
    return PricePoint(t=t, kind="curve", virtual_sol=vsol, virtual_token=K // vsol, **kw)


# ----- the execution model -----------------------------------------------------------------------------------------------------
def test_position_fills_after_the_delay_takes_the_ladder_and_trails_the_rest(params):
    rules = ExitRules(target_pct=0.6, target_fraction=0.5, trailing_frac=0.3, hard_stop_frac=0.35)
    pos = Position(signal_t=0.0, size_sol=0.5, params=params, rules=rules, costs=Costs(), entry_delay_s=20.0)
    assert pos.feed(point(5.0, 1.0)) == [] and pos.status == "signal"      # before the delay: nothing fills
    assert "filled" in pos.feed(point(25.0, 1.0)) and pos.status == "open"
    e = pos.entry
    assert e.t == 25.0 and e.kind == "buy" and e.reason == "entry" and e.venue == "curve" and e.tokens > 0
    assert 0.004 < e.fee_sol < 0.006 and e.fill_price > e.spot and 0.02 < e.impact < 0.05   # 1% curve fee; 0.5 SOL moves a 30 SOL curve's spot by about 3.3%
    assert pos.feed(point(60.0, 1.3)) == [] and pos.pnl_sol is None       # open: no result yet
    assert pos.mark(point(60.0, 1.3)) > 0
    assert pos.feed(point(100.0, 1.7)) == ["exit_pending"]                # the target is hit; the sell fills after the exit delay
    assert pos.feed(point(110.0, 1.72)) == []
    assert "partial_exit" in pos.feed(point(130.0, 1.75)) and pos.status == "open" and pos.target_taken is True
    assert pos.feed(point(200.0, 1.9)) == [] and pos.peak == pytest.approx(point(200.0, 1.9).spot(params))
    assert pos.feed(point(260.0, 1.3)) == ["exit_pending"]                # 31.6% under the peak: the trailing stop
    assert "closed" in pos.feed(point(290.0, 1.25)) and pos.status == "closed" and pos.done
    assert pos.exit_reason == "trailing_stop" and pos.exit_t == 290.0 and pos.held == 0
    assert [f.kind for f in pos.fills] == ["buy", "sell", "sell"] and [f.reason for f in pos.fills] == ["entry", "target", "trailing_stop"]
    assert pos.pnl_sol == pytest.approx(pos.sold_sol - 0.5 - 3 * 0.0005) and 0.15 < pos.pnl_sol < 0.3 and pos.pnl_pct == pytest.approx(pos.pnl_sol / 0.5)
    assert pos.fees_sol > 0.01 and pos.feed(point(300.0, 1.0)) == []    # closed stays closed
    # the state survives JSON (the plays layer stores it per tick) and continues to the same result
    pos2 = Position(0.0, 0.5, params, rules, Costs(), 20.0)
    pts = [point(25.0, 1.0), point(100.0, 1.7), point(130.0, 1.75), point(200.0, 1.9)]
    pos2.feed_all(pts)
    restored = Position.from_json(json.loads(json.dumps(pos2.to_json())), params)
    assert restored.status == "open" and restored.held == pos2.held and restored.peak == pos2.peak and restored.target_taken
    restored.feed_all([point(260.0, 1.3), point(290.0, 1.25)])
    assert restored.status == "closed" and restored.pnl_sol == pytest.approx(pos.pnl_sol)


def test_position_hard_stop_time_stop_no_fill_and_unknown(params):
    hard = Position(0.0, 0.5, params, ExitRules(trailing_frac=None, hard_stop_frac=0.35), entry_delay_s=20.0)
    hard.feed_all([point(25.0, 1.0), point(60.0, 0.6)])
    assert hard.status == "open" and hard.pending_exit is not None and hard.pending_exit[0] == "hard_stop"
    hard.feed(point(90.0, 0.55))
    assert hard.status == "closed" and hard.exit_reason == "hard_stop" and -0.3 < hard.pnl_sol < -0.2
    ts = Position(0.0, 0.5, params, ExitRules(trailing_frac=None, hard_stop_frac=None, time_stop_s=300.0), entry_delay_s=20.0)
    assert ts.feed_all([point(25.0, 1.0), point(300.0, 1.1), point(324.0, 1.1)]) == ["filled"]
    assert ts.feed(point(330.0, 1.1)) == ["exit_pending"] and ts.feed(point(355.0, 1.1)) == ["closed"] and ts.exit_reason == "time_stop"
    assert ts.pnl_sol > 0.0 and ts.exit_t - ts.entry.t == 330.0
    nofill = Position(0.0, 0.5, params, ExitRules(), entry_delay_s=20.0, max_entry_wait_s=600.0)
    assert nofill.feed_all([point(5.0, 1.0), point(700.0, 1.0)]) == ["no_fill"] and nofill.status == "no_fill" and nofill.pnl_sol is None and nofill.done
    unknown = Position(0.0, 0.5, params, ExitRules(), entry_delay_s=20.0)
    unknown.feed_all([point(25.0, 1.0), point(100.0, 1.2)])
    assert unknown.status == "open" and unknown.pnl_sol is None and not unknown.done     # the path ended: no result, never a number
    # the other delays: 5 seconds fills at the first point after 5 s
    quick = Position(0.0, 0.5, params, ExitRules(), entry_delay_s=5.0)
    assert quick.feed_all([point(3.0, 1.0), point(6.0, 1.0)]) == ["filled"] and quick.entry.t == 6.0


def test_position_graduation_and_dev_sell_exits_use_the_pool_after_graduation(params):
    g = Position(0.0, 0.5, params, ExitRules(trailing_frac=None, hard_stop_frac=None, sell_at_graduation=True), entry_delay_s=20.0)
    g.feed_all([point(25.0, 1.0), point(60.0, 1.2, graduation=True)])
    assert g.status == "open" and g.pending_exit[0] == "graduation"
    spot = point(60.0, 1.2).spot(params)
    g.feed(PricePoint(t=90.0, kind="sample", price_sol=spot * 1.1, liquidity_sol=100.0))
    assert g.status == "closed" and g.exit_reason == "graduation" and g.fills[-1].venue == "pool" and g.fills[-1].impact_src == "liquidity"
    assert g.pnl_sol > 0
    d = Position(0.0, 0.5, params, ExitRules(trailing_frac=None, hard_stop_frac=None, sell_at_dev_sell=True), entry_delay_s=20.0)
    d.feed_all([point(25.0, 1.0), point(40.0, 1.1, dev_sell=True), point(70.0, 1.05)])
    assert d.status == "closed" and d.exit_reason == "dev_sell"
    # without liquidity the pool impact assumes an 85 SOL side and says so
    net, fee, impact, src = pool_sell(5_000_000_000, 1e-7, None, 25)
    assert src == "assumed_85_sol" and 0 < impact < 0.01 and fee == pytest.approx((net + fee) * 0.0025) and net < 5_000_000_000 / 1e6 * 1e-7
    net2, fee2, impact2, src2 = pool_sell(5_000_000_000, 1e-7, 1000.0, 25)
    assert src2 == "liquidity" and impact2 < impact and net2 > net
    tokens, fee3, impact3, src3 = pool_buy(0.5, 1e-7, 100.0, 25)
    assert tokens > 0 and fee3 == pytest.approx(0.5 * 25 / 10025) and 0 < impact3 < 0.01 and src3 == "liquidity"
    fresh = CurveState(30_000_000_000, K // 30_000_000_000)
    cap = capacity_sol(fresh, params, max_impact=0.05)
    q = buy_quote(fresh, params, int(cap * 1e9))
    assert 0.5 < cap < 1.0 and q.new_spot_price / q.spot_price - 1 <= 0.0501 and buy_quote(fresh, params, int(cap * 1e9) + 10_000_000).new_spot_price / q.spot_price - 1 > 0.05


def test_exit_rules_are_described_in_dutch_and_round_trip():
    assert hyps.EXITS["ladder60_tr30"].describe_nl() == "harde stop op −35%; verkoop 50% bij +60%; trailing stop 30% onder de piek voor de rest; uiterlijk na 24 uur"
    assert "tijdstop na 5 minuten" in hyps.EXITS["ts5"].describe_nl() and "verkoop op het moment van graduatie" in hyps.EXITS["grad_tr30"].describe_nl()
    assert "verkoop zodra de maker verkoopt" in hyps.EXITS["devsell_tr30"].describe_nl()
    r = ExitRules(target_pct=1.0, target_fraction=1.0, trailing_frac=None, time_stop_s=3600.0)
    assert ExitRules.from_dict(r.to_dict()) == r and "verkoop alles bij +100%" in r.describe_nl() and "tijdstop na 1 uur" in r.describe_nl()
    assert ExitRules.from_dict({"target_pct": 0.4, "nonsense": 1}).target_pct == 0.4
    assert Costs.from_config({"priority_fee_sol": 0.001, "pumpswap_fee_bps": 30}) == Costs(0.001, 30) and Costs.from_config(None) == Costs()


# ----- statistics ----------------------------------------------------------------------------------------------------------
def test_block_bootstrap_is_reproducible_and_brackets_the_mean():
    blocks = [(3.0, 10), (1.0, 8), (4.0, 12), (-0.5, 9), (2.0, 11)]       # (sum of pnl, n) per day
    a = rh.bootstrap_mean(blocks, 999, 7, 0.05)
    b = rh.bootstrap_mean(blocks, 999, 7, 0.05)
    assert a == b and a["draws"] == 999 and a["low"] < a["high"]
    mean = sum(s for s, _ in blocks) / sum(n for _, n in blocks)
    assert a["low"] <= mean <= a["high"]
    assert rh.bootstrap_mean(blocks, 999, 8, 0.05)["low"] != a["low"]       # another seed, another draw
    assert rh.bootstrap_mean([(5.0, 10), (6.0, 10), (4.0, 10)], 999, 1, 0.05)["low"] > 0
    assert rh.bootstrap_mean([], 999, 1, 0.05) is None
    assert rh.max_drawdown([1.0, -2.0, -1.0, 3.0, -0.5]) == 3.0 and rh.max_drawdown([1.0, 1.0]) == 0.0
    assert rh._quantile([1.0, 2.0, 3.0, 4.0], 0.5) == 2.5 and rh._quantile([], 0.5) is None


def _case(variant, day, status, pnl=None, lead=300.0, signal_t=0.0, group="g") -> rh.Case:
    return rh.Case(variant, group, f"M{signal_t}", day, signal_t, 14, status, pnl, None if pnl is None else pnl / 0.5, "time_stop" if status == "closed" else None,
                   0.01, 1e-7, 1.02e-7, 0.015, "curve", lead if status != "no_fill" else None, 0.5, 2.0, signal_t + 400 if status == "closed" else None,
                   380.0 if status == "closed" else None, "up", 10)


def test_metrics_count_unknown_apart_and_the_definition_of_proven(lab):
    cfg = rh.RunConfig.from_settings(lab["settings"], "H2", DAYS)
    assert cfg.min_cases == 100 and cfg.max_unknown_share == 0.2 and cfg.min_lead_s == 120 and cfg.draws == 9999 and cfg.seed == 20261029 and cfg.size_sol == 0.5
    assert cfg.entry_delays_s == (5.0, 20.0, 60.0, 120.0) and cfg.sizes_sol == (0.1, 0.5, 1.0) and cfg.costs == Costs(0.0005, 25) and 13 in cfg.peak_hours_utc and 1 in cfg.peak_hours_utc
    cases = [_case("v", DAYS[2], "closed", 0.05, signal_t=i) for i in range(60)] + [_case("v", DAYS[3], "closed", 0.04, signal_t=100 + i) for i in range(60)]
    m = rh.metrics(cases, cfg)
    assert m["signals"] == 120 and m["cases"] == 120 and m["unknown"] == 0 and m["no_fill"] == 0 and m["unknown_share"] == 0.0
    assert m["ev_sol"] == pytest.approx(0.045) and m["hit_rate"] == 1.0 and m["ev_pct"] == pytest.approx(0.09) and m["median_pct"] == pytest.approx(0.09)
    assert m["lead_median_s"] == 300.0 and m["ci"]["low"] > 0 and m["ci"]["high"] >= m["ev_sol"] >= m["ci"]["low"] and m["days"] == DAYS[2:]
    assert rh.proven_verdict(m, cfg)["proven"] is True
    few = rh.metrics(cases[:50], cfg)
    v = rh.proven_verdict(few, cfg)
    assert v["proven"] is False and [c["key"] for c in v["criteria"] if not c["ok"]] == ["min_cases"]
    with_unknown = rh.metrics(cases + [_case("v", DAYS[3], "unknown", signal_t=1000 + i) for i in range(30)], cfg)
    assert with_unknown["unknown"] == 30 and with_unknown["unknown_share"] == pytest.approx(30 / 150) and with_unknown["cases"] == 120
    assert [c["key"] for c in rh.proven_verdict(with_unknown, cfg)["criteria"] if not c["ok"]] == ["max_unknown"]    # 20% is not under 20%
    short_lead = rh.metrics([_case("v", DAYS[2 + i % 2], "closed", 0.05, lead=60.0, signal_t=i) for i in range(120)], cfg)
    assert [c["key"] for c in rh.proven_verdict(short_lead, cfg)["criteria"] if not c["ok"]] == ["min_lead"]
    losing = rh.metrics([_case("v", DAYS[2 + i % 2], "closed", 0.05 if i % 3 else -0.2, signal_t=i) for i in range(120)], cfg)
    assert losing["ev_sol"] < 0 and losing["hit_rate"] == pytest.approx(80 / 120) and "ci_low_positive" in [c["key"] for c in rh.proven_verdict(losing, cfg)["criteria"] if not c["ok"]]
    nofill = rh.metrics([_case("v", DAYS[2], "no_fill", signal_t=i) for i in range(5)], cfg)
    assert nofill["no_fill"] == 5 and nofill["cases"] == 0 and nofill["ev_sol"] is None and nofill["unknown_share"] is None and nofill["ci"] is None


def test_walk_forward_split_and_the_pre_registered_catalogue(lab):
    h = rh.Harness(lab["conn"], lab["settings"], rh.RunConfig.from_settings(lab["settings"], "H2", [f"2026-11-{d:02d}" for d in range(1, 8)]))
    assert h.split_days() == ([f"2026-11-{d:02d}" for d in range(1, 5)], [f"2026-11-{d:02d}" for d in range(5, 8)])
    assert rh.Harness(lab["conn"], lab["settings"], rh.RunConfig.from_settings(lab["settings"], "H2", DAYS)).split_days() == (DAYS[:2], DAYS[2:])
    assert rh.Harness(lab["conn"], lab["settings"], rh.RunConfig.from_settings(lab["settings"], "H2", DAYS[:1])).split_days() == (DAYS[:1], [])
    assert rh.window_days("2026-10-23", 4) == DAYS
    assert hyps.ORDER[:7] == ["H4", "H5", "H1", "H3", "H7", "H2", "H6"] and hyps.ORDER[7:] == ["H8", "H9", "H10"] and len(hyps.HYPOTHESES) == 10   # milestone 13 adds the second round after the seven
    counts = {hid: len(hyps.get(hid).variants) for hid in hyps.ORDER}
    assert {k: v for k, v in counts.items() if k in hyps.ORDER[:7]} == {"H4": 12, "H5": 18, "H1": 81, "H3": 12, "H7": 7, "H2": 9, "H6": 6} and sum(counts[k] for k in hyps.ORDER[:7]) == 145
    for hid in hyps.ORDER:
        hyp = hyps.get(hid)
        assert hyp.title_nl and hyp.statement_nl and hyp.scope_nl and hyp.expectation_nl, hid
        assert len({v.id for v in hyp.variants}) == len(hyp.variants)
        for v in hyp.variants:
            assert v.exit in hyps.EXITS and v.label_nl and v.rules == hyps.EXITS[v.exit], (hid, v.id)
    cat = hyps.catalogue()
    assert [c["id"] for c in cat] == hyps.ORDER and all(c["variants"] == counts[c["id"]] for c in cat)
    assert hyps.get("H4").needs_creator and hyps.get("H3").needs_theme and hyps.get("H6").post_graduation
    with pytest.raises(KeyError):
        hyps.get("H99")
    # H2 fires on the crossing itself (the previous tick under 75 SOL, this one at or above), once, never after graduation
    v = hyps.get("H2").variant("m5_ts5")
    ctx = hyps.Ctx("m", 0.0, 600.0, {"curve_sol": 76.0, "net_sol_5m": 8.0}, {"curve_sol": 70.0}, {}, {}, False)
    assert hyps.get("H2").fires(v, ctx) is True
    assert hyps.get("H2").fires(v, hyps.Ctx("m", 0.0, 600.0, {"curve_sol": 76.0, "net_sol_5m": 8.0}, None, {}, {}, False)) is False
    assert hyps.get("H2").fires(v, hyps.Ctx("m", 0.0, 600.0, {"curve_sol": 77.0, "net_sol_5m": 8.0}, {"curve_sol": 76.0}, {}, {}, False)) is False
    assert hyps.get("H2").fires(v, hyps.Ctx("m", 0.0, 600.0, {"curve_sol": 76.0, "net_sol_5m": 3.0}, {"curve_sol": 70.0}, {}, {}, False)) is False
    assert hyps.get("H2").fires(v, hyps.Ctx("m", 0.0, 600.0, {"curve_sol": 76.0, "net_sol_5m": 8.0}, {"curve_sol": 70.0}, {}, {}, True)) is False


# ----- features: bundles and the creator's sells -----------------------------------------------------------------------------
def _t(t, buyer, is_buy, sol, vsol=30_000_000_000, slot=None):
    return rf.Trade(t, buyer, is_buy, sol, sol * 1e6, vsol, K // vsol, slot, t + 0.3)


def test_bundle_flags_and_dev_sell_stay_unknown_until_they_can_be_judged(params):
    created = 1000.0
    spread = [_t(created + 1 + i, f"W{i}", True, 1.0, slot=5 + i) for i in range(10)]
    b = rf.bundle_flags(spread, created + 60, created, 0.3, 4)
    assert b == {**b, "bundled_dev": False, "bundled_slot": False, "bundled_top2": False, "bundled_any": False, "bundle_reasons": []} and b["top2_share"] == 0.2
    assert b["dev_buy_frac_of_curve"] == round(0.3 / 85, 4)
    dev = rf.bundle_flags(spread, created + 60, created, 6.0, 4)
    assert dev["bundled_dev"] is True and dev["bundled_any"] is True and dev["bundle_reasons"] == ["dev_buy_gt_5pct"]
    same_slot = [_t(created + 1, "Cr", True, 0.3, slot=4)] + [_t(created + 1, f"S{i}", True, 5.0, slot=4) for i in range(3)] + [_t(created + 10 + i, f"W{i}", True, 0.5, slot=9) for i in range(6)]
    s = rf.bundle_flags(same_slot, created + 60, created, 0.3, 4, creator="Cr")
    assert s["bundled_slot"] is True and s["slot_buyers"] == 3 and s["slot_sol"] == 15.0 and "same_slot_buyers" in s["bundle_reasons"]
    top2 = [_t(created + 1 + i, "A" if i < 3 else ("B" if i < 6 else f"W{i}"), True, 2.0 if i < 6 else 0.5, slot=5 + i) for i in range(10)]
    t2 = rf.bundle_flags(top2, created + 60, created, 0.3, 4)
    assert t2["bundled_top2"] is True and t2["top2_share"] == pytest.approx(12.0 / 14.0, abs=1e-4) and t2["bundle_reasons"] == ["top2_gt_40pct"]
    # too few buys: two wallets always hold most of two buys, so the flag stays unknown, young or old
    few = [_t(created + 1 + i, f"W{i}", True, 1.0, slot=5 + i) for i in range(3)]
    assert rf.bundle_flags(few, created + 30, created, 0.3, 4)["bundled_top2"] is None and rf.bundle_flags(few, created + 900, created, 0.3, 4)["bundled_any"] is None
    assert rf.bundle_flags(few, created + 900, created, 0.3, 4)["bundled_dev"] is False     # what can be judged, is
    # no slot known for the create: the slot flag cannot exist, the verdict rests on the dev buy and the top-2 share
    assert rf.bundle_flags(spread, created + 60, created, 0.3, None)["bundled_any"] is False and rf.bundle_flags(spread, created + 60, created, None, None)["bundled_any"] is None
    # only what was knowable at as_of counts
    later = rf.bundle_flags(top2, created + 2, created, 0.3, 4)
    assert later["bundled_top2"] is None
    # the creator's sells
    trades = [_t(created + 1, "Cr", True, 1.0), _t(created + 100, "W", True, 0.5), _t(created + 400, "Cr", False, 0.4), _t(created + 2000, "Cr", False, 0.6)]
    d = rf.dev_sell(trades, created + 500, created, "Cr", 600.0)
    assert d["dev_first_sell_t"] == created + 400 and d["dev_sold_within_window"] is True and d["dev_sold_frac"] == pytest.approx(0.4)
    assert rf.dev_sell(trades, created + 300, created, "Cr", 600.0)["dev_first_sell_t"] is None and rf.dev_sell(trades, created + 300, created, "Cr", 600.0)["dev_sold_within_window"] is False
    assert rf.dev_sell(trades, created + 3000, created, "Cr", 600.0)["dev_sold_frac"] == pytest.approx(1.0)
    assert rf.dev_sell(trades, created + 500, created, None, 600.0)["dev_sold_within_window"] is None
    # the replayed engine features: the momentum definitions in memory, as of a moment
    f = rf.replay_features(spread, created + 60, created, params)
    assert f["unique_buyers_5m"] == 10 and f["buys_5m"] == 10 and f["sells_5m"] == 0 and f["net_sol_5m"] == pytest.approx(10.0) and f["largest_buyer_share_15m"] == pytest.approx(0.1)
    assert f["trades_total"] == 10 and f["curve_sol"] >= 0 and f["price_sol"] > 0 and f["last_trade_age_s"] == pytest.approx(50.0)
    assert rf.tick_times(created, created + 60, 15.0)[:3] == [created + 15.0, created + 30.0, created + 45.0]


# ----- the harness on the lab ----------------------------------------------------------------------------------------------------
def test_the_harness_runs_h2_on_the_lab_with_known_outcomes_reproducibly(lab, h2):
    res = h2["result"]
    n_pumps = PUMPS_PER_DAY * len(DAYS)
    assert res["stats"]["signals"] > 0 and res["stats"]["coins_evaluated"] == n_pumps and res["stats"]["coins_prefiltered"] == 0   # H2's candidates: the coins that passed 75 SOL
    assert res["config"]["selection_days"] == DAYS[:2] and res["config"]["test_days"] == DAYS[2:]
    assert res["config"]["variants_tested"] == 3 and res["config"]["variants_in_grid"] == 9 and res["config"]["costs"] == {"priority_fee_sol": 0.0005, "pumpswap_fee_bps": 25}
    assert res["config"]["curve_fee_bps"] == 100 and res["config"]["bootstrap"] == {"draws": 9999, "seed": 20261029, "alpha": 0.05}
    rows = {r["variant"]: r for r in res["variants"]}
    assert set(rows) == {"m5_ts5", "m5_ts10", "m10_ts15"}
    for r in rows.values():
        for half in ("full", "selection", "test"):
            m = r[half]
            assert m["signals"] == m["cases"] + m["unknown"] + m["no_fill"], (r["variant"], half)   # unknown and no fill are counted, never scored
    ts5 = rows["m5_ts5"]
    assert ts5["full"]["signals"] == n_pumps and ts5["full"]["unknown"] == 0 and ts5["full"]["no_fill"] == 0 and ts5["full"]["cases"] == n_pumps
    assert ts5["full"]["hit_rate"] == 1.0 and ts5["full"]["ev_sol"] > 0.05 and ts5["full"]["ci"]["low"] > 0 and ts5["full"]["exit_reasons"] == {"time_stop": n_pumps}
    # the curve crosses 75 SOL about 1000 s after the create (20 SOL from the first buys, then 0.4 SOL per 6 s); the path ends 1380 s after it: the peak is the last trade
    assert 300 < ts5["full"]["lead_median_s"] < 450 and ts5["full"]["capacity_median_sol"] > 1 and ts5["full"]["fees_mean_sol"] > 0.01 and ts5["full"]["hold_median_s"] == pytest.approx(330.0, abs=20)
    assert ts5["test"]["cases"] == n_pumps // 2 and ts5["test"]["days"] == DAYS[2:] and ts5["verdict"]["proven"] is True
    assert ts5["exit_text"] == hyps.EXITS["ts5"].describe_nl() and ts5["entry"] == {"min_net_sol": 5} and ts5["exit_rules"]["time_stop_s"] == 300.0
    late = rows["m10_ts15"]                 # the exit lies beyond the end of every price path: unknown, counted apart, never a number
    assert late["full"]["unknown"] == n_pumps and late["full"]["cases"] == 0 and late["full"]["ev_sol"] is None and late["full"]["unknown_share"] == 1.0
    assert late["verdict"]["proven"] is False and "max_unknown" in [c["key"] for c in late["verdict"]["criteria"] if not c["ok"]]
    assert rows["m5_ts10"]["full"]["unknown"] == n_pumps                   # ten minutes after the crossing the path has ended too
    assert res["selected"] == "m5_ts5" and res["selected_proven"] is True and res["proven_variants"] == ["m5_ts5"]
    assert "3 varianten getoetst" in res["multiplicity_note"]
    assert res["cases_total"] == 3 * n_pumps and res["cases_truncated"] is False and {c["status"] for c in res["cases"]} == {"closed", "unknown"}
    assert all(13 <= c["hour_utc"] <= 19 or 8 <= c["hour_utc"] <= 12 for c in res["cases"])
    assert [g["group"] for g in res["groups"]] == sorted({r["group"] for r in rows.values()}) and res["exit_reasons"]["time_stop"] >= n_pumps
    sens = res["sensitivity"]
    assert [d["entry_delay_s"] for d in sens["entry_delay"]] == [5.0, 20.0, 60.0, 120.0] and all(d["cases"] + d["unknown"] + d["no_fill"] == n_pumps for d in sens["entry_delay"])
    assert [d["size_sol"] for d in sens["size"]] == [0.1, 0.5, 1.0] and sens["size"][0]["ev_pct"] > sens["size"][2]["ev_pct"]    # a bigger size pays more impact
    cond = res["conditioning"]
    assert {r["split"] for r in cond["hour"]["rows"]} <= {"peak", "off"} and sum(r["signals"] for r in cond["hour"]["rows"]) == n_pumps
    assert {r["split"] for r in cond["sol_trend"]["rows"]} <= {"up", "down", "unknown"} and cond["coins_per_hour"]["rows"]   # the lab has few SOL price samples: mostly unknown
    assert cond["hour"]["label"] == "tijd van de dag" and all(r["label"] for r in cond["coins_per_hour"]["rows"])
    # the same run again: the same figures and the same intervals (fixed seed, blocks = days)
    again = rh.run_hypothesis(lab["conn"], lab["settings"], "H2", DAYS, min_cases=5, min_select_cases=3, variants=["m5_ts5", "m5_ts10", "m10_ts15"])
    for key in ("variants", "groups", "exit_reasons", "selected", "selected_proven", "proven_variants", "sensitivity", "conditioning", "cases", "cases_total"):
        assert json.dumps(again[key], sort_keys=True, default=str) == json.dumps(res[key], sort_keys=True, default=str), key
    # another entry delay changes the fills, not the signals
    slow = rh.run_hypothesis(lab["conn"], lab["settings"], "H2", DAYS, min_cases=5, min_select_cases=3, variants=["m5_ts5"], entry_delay_s=120.0)
    assert slow["variants"][0]["full"]["signals"] == n_pumps and slow["variants"][0]["full"]["ev_sol"] != ts5["full"]["ev_sol"]


def test_the_other_hypotheses_run_on_the_lab(lab):
    now = lab["now"]
    h5 = rh.run_hypothesis(lab["conn"], lab["settings"], "H5", DAYS, min_cases=5, min_select_cases=3, variants=["none_ts15", "excl_dev_ts15", "only_bundled_ts15"])
    rows = {r["variant"]: r for r in h5["variants"]}
    assert rows["none_ts15"]["full"]["signals"] == lab["coins"]                       # every coin reaches ten buyers in its first three minutes
    assert rows["excl_dev_ts15"]["full"]["signals"] == lab["coins"] - KINDS.count("dump") * len(DAYS)      # the 6 SOL dev buys are excluded
    assert rows["only_bundled_ts15"]["full"]["signals"] == KINDS.count("dump") * len(DAYS)
    assert rows["none_ts15"]["full"]["unknown"] == KINDS.count("stall") * len(DAYS)   # the stalls: the path ends, unknown
    assert rows["excl_dev_ts15"]["full"]["ev_sol"] > rows["only_bundled_ts15"]["full"]["ev_sol"]
    h4 = rh.run_hypothesis(lab["conn"], lab["settings"], "H4", DAYS, min_cases=5, min_select_cases=3, variants=["good_ts15", "none_ts15", "bad_ts15"])
    g = {r["variant"]: r["full"]["signals"] for r in h4["variants"]}
    assert sum(g.values()) <= lab["coins"] and g["bad_ts15"] > 0 and g["none_ts15"] > 0   # Maker1 bundles (the dumps): bad; the first coin of a maker: none
    h7 = rh.run_hypothesis(lab["conn"], lab["settings"], "H7", DAYS, min_cases=5, min_select_cases=3, variants=["baseline_tr30", "exclude_dev_sold_tr30"])
    assert {r["variant"] for r in h7["variants"]} == {"baseline_tr30", "exclude_dev_sold_tr30"} and all(r["full"]["signals"] > 0 for r in h7["variants"])
    h6 = rh.run_hypothesis(lab["conn"], lab["settings"], "H6", DAYS, min_cases=5, min_select_cases=3)
    assert h6["stats"]["signals"] >= 0 and h6["config"]["variants_tested"] == 6      # eight graduations with one sample each: a few signals at most
    h3 = rh.run_hypothesis(lab["conn"], lab["settings"], "H3", DAYS, min_cases=5, min_select_cases=3)
    assert h3["stats"]["signals"] == 0 and h3["selected"] is None                    # no theme decisions in the lab: nothing fires, nothing is claimed
    md, js = rr.write_report(lab["settings"], h3, now)
    text = md.read_text(encoding="utf-8")
    assert "Geen variant gekozen" in text and "Niets is bewezen; er wordt niets gepromoveerd." in text


def test_the_report_is_dutch_honest_and_listed(lab, h2):
    res, md, js = h2["result"], h2["md"], h2["js"]
    assert md.exists() and js.exists() and md.parent == lab["settings"].out_dir / "research" and md.name.endswith("-H2.md") and js.name == md.name[:-3] + ".json"
    text = md.read_text(encoding="utf-8")
    for needle in ("Vooraf vastgelegde hypothese", "Data en uitvoering", "Oordeel", "Alle varianten", "Conditionering", "Gevoeligheid", "Onbekend en geen vulling", "Eerlijkheid",
                   "Graduatiemoment", res["selected"], "bewezen", "onbekend", "keuzehelft", "toetshelft", "3 varianten getoetst", "0,5 SOL", "95%"):
        assert needle in text, needle
    assert "Bewezen" in text
    for forbidden in ("winstclaim", "garantie", "voorspelling", "gegarandeerd"):
        assert forbidden not in text, forbidden
    doc = json.loads(js.read_text(encoding="utf-8"))
    assert doc["selected"] == res["selected"] and doc["generated_at"] and doc["hypothesis"]["id"] == "H2" and len(doc["cases"]) == res["cases_total"]
    listed = rr.list_reports(lab["settings"])
    mine = next(r for r in listed if r["name"] == md.stem)
    assert mine["kind"] == "research" and mine["hypothesis"] == "H2" and mine["selected"] == res["selected"] and mine["selected_proven"] is True
    assert mine["variants_tested"] == 3 and mine["days"] == DAYS and mine["proven_variants"] == ["m5_ts5"] and mine["generated_at"] == doc["generated_at"]
    assert rr.load_report(lab["settings"], md.stem)["selected"] == res["selected"] and rr.load_report(lab["settings"], "../etc/passwd") is None and rr.load_report(lab["settings"], "nope") is None


def test_promote_writes_a_rule_and_refuses_the_unproven_or_unchosen(lab, h2, tmp_path):
    res = h2["result"]
    plays_path = tmp_path / "plays.json"
    plays_path.write_text(json.dumps({"version": 1, "size_sol": 0.5, "entry_delay_s": 20, "alerts": {"max_per_hour": 6}, "rules": []}), encoding="utf-8")
    now = lab["now"] + 60
    rule = rr.promote(lab["settings"], "H2", res["selected"], now, plays_path=plays_path)
    assert rule["id"] == "H2-v1" and rule["version"] == 1 and rule["active"] is True and rule["variant"] == res["selected"] and rule["report"] == h2["md"].stem
    sel = next(r for r in res["variants"] if r["variant"] == res["selected"])
    assert rule["figures"]["cases"] == sel["test"]["cases"] and rule["figures"]["ev_sol"] == sel["test"]["ev_sol"] and rule["figures"]["ci_low_sol"] == sel["test"]["ci"]["low"] > 0
    assert rule["figures"]["test_days"] == DAYS[2:] and rule["exit_text"] == sel["exit_text"] and rule["entry"] == sel["entry"] and rule["name"] == "Graduatiemoment"
    written = json.loads(plays_path.read_text(encoding="utf-8"))
    assert [r["id"] for r in written["rules"]] == ["H2-v1"] and written["size_sol"] == 0.5
    # a second promotion of the same hypothesis supersedes the first
    rule2 = rr.promote(lab["settings"], "H2", res["selected"], now + 1, plays_path=plays_path)
    written = json.loads(plays_path.read_text(encoding="utf-8"))
    assert rule2["id"] == "H2-v2" and [r["active"] for r in written["rules"]] == [False, True] and written["rules"][0]["superseded_by"] == "H2-v2"
    # refusals: an unknown variant, a variant that was not the pre-chosen one, a hypothesis without a report
    with pytest.raises(rr.PromoteError, match="onbekende variant"):
        rr.promote(lab["settings"], "H2", "m99_ts1", now, plays_path=plays_path)
    other = next(v for v in ("m5_ts5", "m5_ts10") if v != res["selected"])
    with pytest.raises(rr.PromoteError, match="niet de vooraf gekozen"):
        rr.promote(lab["settings"], "H2", other, now, plays_path=plays_path)
    with pytest.raises(rr.PromoteError, match="geen onderzoeksrapport"):
        rr.promote(lab["settings"], "H1", "n15_m3_tr30", now, plays_path=plays_path)
    # an unproven pre-chosen variant (the real threshold of 100 cases on the test half) is refused, with the failed criteria named
    strict = rh.run_hypothesis(lab["conn"], lab["settings"], "H2", DAYS, min_select_cases=3, variants=["m5_ts5", "m5_ts10", "m10_ts15"])
    assert strict["selected"] == res["selected"] and strict["selected_proven"] is False
    md2, _ = rr.write_report(lab["settings"], strict, lab["now"] + 86400)
    assert md2.stem != h2["md"].stem
    with pytest.raises(rr.PromoteError, match="niet bewezen") as e:
        rr.promote(lab["settings"], "H2", res["selected"], now + 2, plays_path=plays_path)
    assert "minstens 100 gevallen" in str(e.value)
    assert json.loads(plays_path.read_text(encoding="utf-8"))["rules"][-1]["id"] == "H2-v2"   # nothing was written
    # the promoted rule loads in the plays layer and fires on the same predicate
    from fart.plays import rules as plays_rules

    s = lab["settings"]
    old = s.configs.get("plays")
    s.configs["plays"] = json.loads(plays_path.read_text(encoding="utf-8"))
    try:
        active = plays_rules.load_rules(s)
        assert [r.id for r in active] == ["H2-v2"] and len(plays_rules.load_rules(s, include_inactive=True)) == 2
        r = active[0]
        assert r.hypothesis == "H2" and r.exit_rules == hyps.EXITS[r.exit_key] and r.fires(hyps.Ctx("m", 0.0, 600.0, {"curve_sol": 76.0, "net_sol_5m": 8.0}, {"curve_sol": 70.0}, {}, {}, False))
        pub = r.to_public()
        assert pub["id"] == "H2-v2" and pub["hit_rate"] == rule2["figures"]["hit_rate"] and "figures" not in pub
        got = plays_rules.settings_of(s)
        assert {k: got[k] for k in ("size_sol", "entry_delay_s", "alerts_per_hour", "loop_interval_s")} == {"size_sol": 0.5, "entry_delay_s": 20.0, "alerts_per_hour": 6, "loop_interval_s": 15.0}
    finally:
        if old is None:
            s.configs.pop("plays", None)
        else:
            s.configs["plays"] = old


# ----- the autopsy ------------------------------------------------------------------------------------------------------------
def test_autopsy_of_the_graduations_groups_organic_winners(lab):
    res = autopsy.build_autopsy(lab["conn"], lab["settings"], DAYS, lab["now"])
    assert res["counts"]["graduations"] == len(lab["grads"]) == 2 * len(DAYS) and res["kind"] == "autopsy" and res["days"] == DAYS
    g = res["groups"]
    assert g["organic_winner"] == len(DAYS) and g["bundled_winner"] == 0
    assert g["not_winner"] + res["counts"]["sparse"] == len(DAYS)        # a 'not winner' needs dense samples; one sample is sparse and left out, honestly
    cov = res["coverage"]["organic_winner"]
    assert cov["n"] == len(DAYS) and cov["radar_had_trades_5m"] == len(DAYS) and cov["bundled_dev"] == 0 and cov["bundled_top2"] == 0 and cov["age_at_graduation_median_s"] == 1500.0
    assert cov["observed"] == 0 and cov["lead_median_s"] is None          # no decision rows in the lab: unknown, not zero lead
    feats = res["features"]["organic_winner"]
    assert feats["unique_buyers_5m@180s"]["n"] == len(DAYS) and feats["unique_buyers_5m@180s"]["p50"] >= 30 and feats["dev_buy_frac_of_curve"]["p50"] == pytest.approx(0.3 / 85, abs=1e-3)
    assert feats["net_sol_5m@60s"]["n"] == len(DAYS) and feats["graduation_progress@300s"]["p50"] > 0
    text = autopsy.render_autopsy(res)
    for needle in ("Autopsie van de organische winnaars", "organische winnaars **4**", "onbekend", "unieke kopers (5 min)", "Dit rapport vergelijkt"):
        assert needle in text, needle
    md, js = autopsy.write_autopsy(lab["settings"], res)
    assert Path(md).exists() and Path(js).exists() and Path(md).name == f"{DAYS[-1][:4]}-10-24-autopsy.md" or Path(md).name.endswith("-autopsy.md")
    listed = rr.list_reports(lab["settings"])
    assert any(r["kind"] == "autopsy" for r in listed)
