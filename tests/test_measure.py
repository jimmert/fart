import json

import pytest

from fart.chain.curve import CurveParams, CurveState, buy_quote
from fart.measure import outcomes as oc
from fart.measure import prices as pr
from fart.measure.paper import TradePoint, simulate
from fart.measure.report import build_report, render_markdown
from fart.net.budget import Budget, load_source_configs

T0 = 1_761_480_000.0  # anchor (synthetic)
SOL_USD = 200.0


class Clock:
    def __init__(self, t):
        self.t = t

    def __call__(self):
        return self.t


@pytest.fixture
def params(settings):
    return CurveParams.from_config(settings.config("pumpfun"))


@pytest.fixture
def dex(fixtures_dir):
    return json.loads((fixtures_dir / "dexscreener_tokens.json").read_text())["pairs"]


def test_dexscreener_parsing(dex):
    best = pr.best_pair_per_token(dex)
    assert best["MINTA"]["pairAddress"] == "PAIRA1"  # deepest liquidity wins
    s = pr.pair_to_sample(best["MINTA"])
    assert s["mcap_usd"] == 150000 and s["volume_h24_usd"] == 250000.5 and s["volume_h1_usd"] == 20000
    assert s["txns_h24"] == 2100 and s["dex_id"] == "pumpswap"
    sb = pr.pair_to_sample(best["MINTB"])
    assert sb["mcap_usd"] is None and sb["fdv_usd"] == 50000  # marketCap missing stays None
    assert "MINTC" not in best
    assert pr.sol_usd_from_dexscreener(dex, "So11111111111111111111111111111111111111112") == 200.5  # USD-quoted pair, not BONK
    assert pr.best_pair_per_token("garbage") == {}
    assert pr.sol_usd_from_dexscreener(None, "x") is None


def test_sol_usd_cache_and_fallbacks(settings, conn, dex, monkeypatch):
    clock = Clock(T0)
    calls = []

    def fake_request(budget, source, method, url, **kw):
        calls.append((source, url))
        if source == "dexscreener":
            return dex
        return {"data": {"attributes": {"price_usd": "199.0"}}}

    monkeypatch.setattr(pr, "request_json", fake_request)
    budget = Budget(conn, load_source_configs(settings.config("budget"), {}), settings.lock_path, clock=clock)
    feed = pr.PriceFeed(conn, budget, settings.config("measure"), clock)
    assert feed.sol_usd() == (200.5, "dexscreener")
    assert feed.sol_usd()[1] == "cache:dexscreener"
    assert len(calls) == 1
    clock.t += 61
    # dexscreener fails -> geckoterminal
    def failing(budget, source, method, url, **kw):
        if source == "dexscreener":
            raise pr.HttpError(500, "down")
        return {"data": {"attributes": {"price_usd": "199.0"}}}

    monkeypatch.setattr(pr, "request_json", failing)
    assert feed.sol_usd() == (199.0, "geckoterminal")
    clock.t += 61
    # everything fails -> stale cache, flagged
    monkeypatch.setattr(pr, "request_json", lambda *a, **k: (_ for _ in ()).throw(pr.HttpError(503, "down")))
    price, src = feed.sol_usd()
    assert price == 199.0 and src.startswith("stale:geckoterminal")


def test_sample_mints_stores_known_and_counts_unknown(settings, conn, dex, monkeypatch):
    clock = Clock(T0)
    monkeypatch.setattr(pr, "request_json", lambda *a, **k: dex)
    budget = Budget(conn, load_source_configs(settings.config("budget"), {}), settings.lock_path, clock=clock)
    feed = pr.PriceFeed(conn, budget, settings.config("measure"), clock)
    stats = feed.sample_mints(["MINTA", "MINTB", "MINTC"], SOL_USD)
    assert stats["stored"] == 2 and stats["no_pair"] == 1 and stats["calls"] == 1
    rows = {r["mint"]: r for r in conn.execute("SELECT * FROM mcap_samples")}
    assert rows["MINTA"]["mcap_usd"] == 150000 and rows["MINTA"]["sol_usd"] == SOL_USD
    assert rows["MINTB"]["mcap_usd"] is None and rows["MINTB"]["fdv_usd"] == 50000
    assert "MINTC" not in rows


# ----- synthetic world -------------------------------------------------------------------------------
def add_coin(conn, mint, t, name="x", symbol="X", dev_buy=1.0, creator="C"):
    conn.execute("INSERT INTO coins(mint, name, symbol, creator, block_time, received_at, dev_buy_sol, source, sources) VALUES(?,?,?,?,?,?,?,'pumpportal','[]')",
                 (mint, name, symbol, creator, t, t + 0.5, dev_buy))


def add_grad(conn, mint, t):
    conn.execute("INSERT INTO migrations(mint, block_time, received_at, source, sources) VALUES(?,?,?,'rpc_logs','[]')", (mint, t, t + 0.5))


def add_trade(conn, mint, t, buyer, sol, vsol_sol, vtok_tokens, is_buy=True, sig=None):
    conn.execute("INSERT INTO trades(mint, block_time, received_at, signature, buyer, is_buy, sol, tokens, virtual_sol, virtual_token, source)"
                 " VALUES(?,?,?,?,?,?,?,?,?,?,'pumpportal')",
                 (mint, t, t + 0.5, sig or f"{mint}-{t}-{buyer}", buyer, 1 if is_buy else 0, sol, sol * 1e6, vsol_sol * 1e9, vtok_tokens * 1e6))


def add_sample(conn, mint, t, mcap, vol24, sol_usd=SOL_USD, vol1=None, vol6=None):
    js = {"volume_h24_usd": vol24, "volume_h1_usd": vol1, "volume_h6_usd": vol6}
    conn.execute("INSERT INTO mcap_samples(mint, ts, src, mcap_usd, volume_h24_usd, sol_usd, json) VALUES(?,?,?,?,?,?,?)",
                 (mint, t, "dexscreener", mcap, vol24, sol_usd, json.dumps(js)))


def add_decision(conn, mint, as_of, rules="r1"):
    cur = conn.execute("INSERT INTO decisions(as_of, created_at, kind, mint, features_json, rules_version, verdict, reasons_json)"
                       " VALUES(?,?,?,?,'{}',?,'observe','[]')", (as_of, as_of + 0.1, "observe", mint, rules))
    return cur.lastrowid


def build_world(conn):
    """Known answers:
    WIN   watched, graduated T0+600, trades peak mcap 900 SOL (=180k USD), volume 150 SOL (=30k USD) -> winner, organic
    BUND  graduated 5 s after create, dev buy 1 SOL; samples peak 300k, vol 500k -> winner, bundled
    LOSE  samples peak 40k -> not a winner
    UNK   graduated, no trades, no samples -> unknown
    NOCR  graduation without a known create -> unknown bundled
    Decisions: WIN at T0+100 (before graduation and before first trade at T0+120), LOSE at T0+50, UNK at T0.
    """
    add_coin(conn, "WIN", T0, name="Goldberg Winner", symbol="GOLD")
    add_coin(conn, "BUND", T0 + 1000, name="Bundle Boy", symbol="BB", dev_buy=1.0)
    add_coin(conn, "LOSE", T0 + 2000, name="Loser", symbol="LOSE")
    add_coin(conn, "UNK", T0 + 3000, name="Mystery", symbol="MYST", dev_buy=None)
    add_grad(conn, "WIN", T0 + 600)
    add_grad(conn, "BUND", T0 + 1005)
    add_grad(conn, "LOSE", T0 + 2600)
    add_grad(conn, "UNK", T0 + 3600)
    add_grad(conn, "NOCR", T0 + 4000)
    # WIN trades: mcap_sol = vsol/vtok*1e9 -> 900 SOL at peak (vsol 90, vtok 100M)
    add_trade(conn, "WIN", T0 + 120, "A", 50.0, 80.0, 0.3e9)
    add_trade(conn, "WIN", T0 + 300, "B", 60.0, 90.0, 0.1e9)   # peak: 90/0.1e9*1e9 = 900 SOL
    add_trade(conn, "WIN", T0 + 500, "C", 40.0, 85.0, 0.2e9, is_buy=False)
    add_sample(conn, "WIN", T0 + 100, None, None)  # provides sol_usd near the anchor, no mcap
    add_sample(conn, "WIN", T0 + 600 + 1800, 180000.0, 30000.0, vol1=25000.0, vol6=28000.0)  # post-graduation (AMM) sample
    add_sample(conn, "BUND", T0 + 1005 + 3600, 300000.0, 500000.0, vol1=100000.0, vol6=300000.0)
    add_sample(conn, "LOSE", T0 + 2600 + 7200, 40000.0, 9000.0, vol1=2000.0, vol6=5000.0)
    d_win = add_decision(conn, "WIN", T0 + 100)
    d_lose = add_decision(conn, "LOSE", T0 + 2050)
    d_unk = add_decision(conn, "UNK", T0 + 3000)
    conn.execute("INSERT INTO kv(key, value, updated_at) VALUES('ingest.pumpportal.started','1',0)")
    return d_win, d_lose, d_unk


def test_measure_window_known_answers(conn, params):
    build_world(conn)
    m = oc.measure_window(conn, "WIN", T0 + 600, T0 + 600 + 86400, params)
    # trades inside [grad, grad+24h]: none (all trades were before graduation) -> peak from the AMM sample only
    assert m["trades_n"] == 0 and m["peak_mcap_usd"] == 180000.0 and m["peak_src"] == "dexscreener"
    assert m["volume_src"] == "dexscreener:volume_h24_usd" and m["volume_usd"] == 30000.0
    m2 = oc.measure_window(conn, "WIN", T0 + 100, T0 + 100 + 3600, params)
    assert m2["trades_n"] == 3 and m2["peak_mcap_sol"] == pytest.approx(900.0)
    assert m2["peak_mcap_usd"] == pytest.approx(180000.0) and m2["peak_src"] == "both"  # trades and the AMM sample agree
    assert m2["volume_usd"] == pytest.approx(150 * SOL_USD) and m2["volume_src"] == "trades"
    assert oc.hit_100k(m2["peak_mcap_usd"], m2["volume_usd"], {"peak_mcap_usd": 100000, "min_volume_usd": 20000}) is True
    mb = oc.measure_window(conn, "BUND", T0 + 1005, T0 + 1005 + 3600, params)
    assert mb["peak_mcap_usd"] == 300000.0 and mb["peak_src"] == "dexscreener" and mb["volume_src"] == "dexscreener:volume_h1_usd"
    assert mb["volume_usd"] == 100000.0
    mu = oc.measure_window(conn, "UNK", T0 + 3600, T0 + 3600 + 86400, params)
    assert mu["peak_mcap_usd"] is None and mu["volume_usd"] is None
    assert oc.hit_100k(None, None, {"peak_mcap_usd": 100000, "min_volume_usd": 20000}) is None
    assert oc.hit_100k(40000.0, None, {"peak_mcap_usd": 100000, "min_volume_usd": 20000}) is False  # low peak is a known miss
    assert oc.hit_100k(150000.0, None, {"peak_mcap_usd": 100000, "min_volume_usd": 20000}) is None  # volume unknown


def test_tracker_measures_only_elapsed_horizons_and_is_append_only(settings, conn):
    d_win, d_lose, d_unk = build_world(conn)
    budget = Budget(conn, load_source_configs(settings.config("budget"), {}), settings.lock_path, clock=Clock(T0))
    tr = oc.Tracker(conn, settings, budget, clock=Clock(T0 + 2 * 3600))
    n_out, n_paper = tr.measure_due(T0 + 2 * 3600)
    # at T0+2h: WIN decision (T0+100) and WIN grad (T0+600) have +1h due; BUND grad +1h due; LOSE decision/grad: not yet
    rows = conn.execute("SELECT subject, mint, horizon_s, hit_100k, bundled FROM outcomes ORDER BY id").fetchall()
    got = {(r["subject"], r["mint"], r["horizon_s"]): (r["hit_100k"], r["bundled"]) for r in rows}
    assert got[("decision", "WIN", 3600)] == (1, 0)
    assert got[("graduation", "WIN", 3600)] == (1, 0)      # post-graduation sample: 180k peak, 25k volume in the first hour
    assert got[("graduation", "BUND", 3600)] == (1, 1)
    assert got[("decision", "LOSE", 3600)] == (None, 0)  # its only sample lies outside the first hour -> unknown
    assert got[("graduation", "UNK", 3600)] == (None, None)  # due exactly at T0+7200; no data, dev buy unknown
    assert ("graduation", "NOCR", 3600) not in got            # T0+4000+3600 > now
    assert n_paper == 0
    # re-run at the same time: nothing new (no duplicates)
    assert tr.measure_due(T0 + 2 * 3600) == (0, 0)
    n_before = conn.execute("SELECT COUNT(*) FROM outcomes").fetchone()[0]
    # a day later everything is due, paper rows are written for the 24 h horizon of decisions
    n_out2, n_paper2 = tr.measure_due(T0 + 95000)
    assert n_paper2 == 3
    rows = {(r["subject"], r["mint"], r["horizon_s"]): r for r in conn.execute("SELECT * FROM outcomes")}
    assert rows[("graduation", "LOSE", 86400)]["hit_100k"] == 0
    assert rows[("graduation", "UNK", 86400)]["hit_100k"] is None and rows[("graduation", "UNK", 86400)]["bundled"] is None
    assert rows[("graduation", "NOCR", 86400)]["bundled"] is None and json.loads(rows[("graduation", "NOCR", 86400)]["json"])["coin_known"] is False
    assert rows[("decision", "WIN", 86400)]["peak_mcap_usd"] == pytest.approx(180000.0)
    # a new method_version recomputes into new rows; old rows stay
    tr.method_version = "m1.1"
    n3, _ = tr.measure_due(T0 + 95000)
    assert n3 == len({k for k in rows})
    assert conn.execute("SELECT COUNT(DISTINCT method_version) FROM outcomes").fetchone()[0] == 2
    assert conn.execute("SELECT COUNT(*) FROM outcomes").fetchone()[0] == 2 * len(rows)
    assert conn.execute("SELECT COUNT(*) FROM outcomes WHERE measured_at < ?", (T0 + 95000,)).fetchone()[0] == n_before


def test_paper_simulation_known_inputs(params, settings):
    cfg = dict(settings.config("measure")["paper"], size_sol=1.0, entry_delay_s=20, trailing_stop_frac=0.30, network_fee_sol=0.0)
    s0 = CurveState.initial(params)
    # the path the market takes (reserves after each trade); we enter on the first point >= as_of+20
    pts = [TradePoint(T0 + 5, s0.virtual_sol, s0.virtual_token)]
    st = s0
    for i, t in enumerate((T0 + 25, T0 + 40, T0 + 60, T0 + 90)):
        st = buy_quote(st, params, 5 * 10**9).new_state  # price rises
        pts.append(TradePoint(t, st.virtual_sol, st.virtual_token))
    peak_state = st
    # crash: back to near the entry state
    pts.append(TradePoint(T0 + 120, pts[1].virtual_sol, pts[1].virtual_token))
    res = simulate(pts, params, cfg, T0)
    assert res.entry_time == T0 + 25 and res.exit_reason == "trailing_stop" and res.exit_time == T0 + 120
    assert res.fill_price > res.entry_price and res.slippage_bps > 0
    assert res.peak_price == pytest.approx(peak_state.price_sol(params))
    # we bought at state[1] and sold back into the same reserves: expected loss = both fees + both impacts
    from fart.chain.curve import sell_quote

    s1 = CurveState(pts[1].virtual_sol, pts[1].virtual_token)
    q = buy_quote(s1, params, 10**9)
    sq = sell_quote(s1, params, q.tokens_out)
    assert res.pnl_sol == pytest.approx(sq.sol_out_net / 1e9 - 1.0)
    assert -0.10 < res.pnl_sol < -0.02
    assert res.fees_sol == pytest.approx((q.fee + sq.fee) / 1e9)
    # no trade after entry
    res2 = simulate(pts[:1], params, cfg, T0)
    assert res2.exit_reason == "no_trade_after_entry" and res2.pnl_sol is None and res2.entry_time is None
    # entry but never an exit signal and not past max hold -> no exit data
    res3 = simulate(pts[:3], params, cfg, T0)
    assert res3.exit_reason == "no_exit_data" and res3.pnl_sol is None and res3.entry_time == T0 + 25
    # max hold: a later point beyond max_hold_s exits there with a profit (price kept rising)
    pts4 = pts[:5] + [TradePoint(T0 + 25 + 86401, peak_state.virtual_sol, peak_state.virtual_token)]
    res4 = simulate(pts4, params, cfg, T0)
    assert res4.exit_reason == "max_hold" and res4.pnl_sol > 0


def test_daily_report_synthetic_day(settings, conn):
    from fart.util import day_key

    d_win, d_lose, d_unk = build_world(conn)
    # milestone 5: a "no winner" only counts when the tracker sampled the whole window at least every 10 min;
    # LOSE gets a sample every 5 min (its single +2 h sample from build_world stays), the others keep sparse samples
    for k in range(0, 86400, 300):
        add_sample(conn, "LOSE", T0 + 2600 + k + 7, 40000.0, 9000.0, vol1=2000.0, vol6=5000.0)
    budget = Budget(conn, load_source_configs(settings.config("budget"), {}), settings.lock_path, clock=Clock(T0))
    now = T0 + 95000  # every +24 h horizon of the day is due
    tr = oc.Tracker(conn, settings, budget, clock=Clock(now))
    tr.measure_due(now)
    # a meta decision on 'goldberg' before WIN graduated
    conn.execute("INSERT INTO decisions(as_of, created_at, kind, term, features_json, rules_version, verdict, reasons_json)"
                 " VALUES(?,?,?,?,'{}','r1','observe','[]')", (T0 + 50, T0 + 50, "observe_meta", "goldberg"))
    # one PumpPortal gap of 5 minutes during the day -> that hour is not complete
    from fart.ingest import coverage as cov

    g = cov.gap_open(conn, "pumpportal", T0 + 20000, "disconnect")
    cov.gap_close(conn, g, T0 + 20300)
    day = day_key(T0)
    rep = build_report(conn, settings, day, now)
    u, rc, p = rep["universe"], rep["recall"], rep["precision"]
    assert u["graduations"] == 5 and u["measured"] == 5 and u["pending"] == 0
    assert u["winners"] == 2 and u["winners_organic"] == 1 and u["winners_bundled"] == 1
    assert u["not_winner"] == 1 and u["outcome_unknown"] == 2  # UNK and NOCR
    assert u["outcome_unknown_sparse_samples"] == 0 and u["tracker_covered"] == 1 and u["tracker_not_covered"] == 4  # only LOSE is dense
    assert u["tracker_coverage_share"] == 0.2 and rep["tracker"]["covered"] == 1 and rep["tracker"]["max_sample_gap_s"] == 600
    assert [g["outcome"] for g in rep["graduations"]] == ["winner", "winner", "not_winner", "unknown", "unknown"]
    assert u["without_create"] == 1 and u["bundled_unknown_total"] == 2
    assert rc["with_decision"] == 1 and rc["decision_before_graduation"] == 1 and rc["decision_before_first_trade"] == 1
    assert rc["recall_before_graduation"] == 0.5 and rc["recall_organic_before_graduation"] == 1.0
    win = [w for w in rep["winners"] if w["mint"] == "WIN"][0]
    assert win["lead_s_vs_first_trade"] == 20 and win["lead_s_vs_graduation"] == 500
    assert win["theme_term"] == "goldberg" and win["theme_lead_s_vs_graduation"] == 550
    assert p["decisions"] == 3 and p["known"] == 2 and p["hits"] == 1 and p["misses"] == 1 and p["unknown"] == 1
    assert p["precision"] == 0.5 and p["precision_organic"] == 0.5
    assert rep["paper"]["all"]["trades"] == 3 and rep["paper"]["all"]["no_exit"] >= 1
    assert rep["coverage"]["hours_complete"] == 23 and rep["coverage"]["gap_seconds_by_source"] == {"pumpportal": 300}
    assert rep["complete"] is False  # one hour with a gap
    assert rep["unknowns"]["graduations_outcome_unknown"] == 2 and rep["unknowns"]["graduations_without_create"] == 1
    assert rep["method_version"] == "m1.0" and rep["rules_versions_in_decisions"] == ["r1"]
    # milestone 5: the per-rules_version blocks the week report aggregates
    assert rep["precision_by_rules_version"] == {"r1": {"decisions": 3, "known": 2, "hits": 1, "misses": 1, "unknown": 1, "pending": 0, "distinct_mints": 3, "elapsed": 3}}
    assert rep["observations_by_rules_version"] == {"r1": {"observe": 3, "observe_meta": 1}}
    assert win["earliest_by_rules_version"] == {"r1": {"coin": {"id": d_win, "as_of": T0 + 100}, "theme": {"id": win["decision_id"] + 3, "term": "goldberg", "as_of": T0 + 50}}}
    assert win["theme_lead_s_vs_first_trade"] == 70 and win["first_trade_at"] is not None and win["decision_rules_version"] == "r1"
    assert rc["any_before_graduation"] == 1 and rc["any_before_first_trade"] == 1 and rc["theme_before_first_trade"] == 1
    assert rep["coverage"]["peak_hours_not_covered"] == 0 and len(rep["coverage"]["peak_hours_utc"]) == 14
    assert rep["ops"]["cost"]["rpc_cu_estimate"] == 0 and rep["ops"]["cost"]["cu_calibrated"] is False and rep["ops"]["cost"]["x_usd"] is None
    assert rep["ops"]["budget_pauses"] == {"rpc_cap_blocked_calls": 0, "x_cap_blocked_calls": 0, "budget_cap_gap_s": 0, "trade_stream_envelope_paused": 0}
    md = render_markdown(rep)
    assert md.startswith(f"# Dagrapport recall {day}")
    for heading in ("## Samenvatting", "## Dekking (coverage)", "## Winnaars", "## Lead time", "## Precisie van observaties", "## Paper trades", "## Onbekenden", "## Versies"):
        assert heading in md
    assert "NIET COMPLEET" in md and "Goldberg Winner" in md
    assert "Dekking van de tracker: 1 van 5 gemeten graduaties had minstens elke 10 min een sample (20%)" in md
    assert "## Budget en kosten" in md and "ijking met het Alchemy-dashboard staat open" in md


def test_report_cli_writes_atomic_outputs(settings, conn, capsys):
    from fart.cli import main
    from fart.util import day_key

    build_world(conn)
    conn.close()
    day = day_key(T0)
    assert main(["--root", str(settings.root), "--quiet", "measure", "report", "--day", day]) == 0
    j = settings.out_dir / "recall" / f"{day}.json"
    m = settings.out_dir / "recall" / f"{day}.md"
    assert j.exists() and m.exists()
    data = json.loads(j.read_text())
    assert data["day"] == day and data["universe"]["graduations"] == 5
    assert not list((settings.out_dir / "recall").glob("*.tmp"))
    # track command runs without network: blocked/unavailable sources leave unknowns, no crash
    assert main(["--root", str(settings.root), "--quiet", "measure", "track"]) == 0
