import json

import pytest

from fart.chain.curve import CurveParams
from fart.engine import decide, features
from fart.engine.meta import cluster, load_stopwords, tokenize
from fart.engine.service import Engine
from fart.ingest import watch

T0 = 1_761_480_000.0


def add_coin(conn, mint, t, name="x", symbol="X", creator="C", dev_buy=None, block_time=True, received_delay=0.5):
    conn.execute(
        "INSERT INTO coins(mint, name, symbol, creator, block_time, received_at, dev_buy_sol, source, sources)"
        " VALUES(?,?,?,?,?,?,?,'pumpportal','[\"pumpportal\"]')",
        (mint, name, symbol, creator, t if block_time else None, t + received_delay, dev_buy),
    )


def add_trade(conn, mint, t, buyer, sol, is_buy=True, sig=None, block_time=True, received_delay=0.5, vsol=None, vtok=None):
    conn.execute(
        "INSERT INTO trades(mint, block_time, received_at, signature, buyer, is_buy, sol, tokens, virtual_sol, virtual_token, source)"
        " VALUES(?,?,?,?,?,?,?,?,?,?,'pumpportal')",
        (mint, t if block_time else None, t + received_delay, sig or f"{mint}-{buyer}-{t}", buyer, 1 if is_buy else 0, sol,
         sol * 1e6, vsol, vtok),
    )


@pytest.fixture
def params(settings):
    return CurveParams.from_config(settings.config("pumpfun"))


@pytest.fixture
def ccfg(settings):
    c = dict(settings.config("engine")["coin"])
    c["bundled_graduation_window_s"] = 60
    c["bundled_dev_buy_fraction"] = 0.5
    return c


def test_tokenize_and_cluster(settings):
    sw = load_stopwords(settings.config("stopwords"))
    # 'cat' and 'coin' are stopwords: no unigram and no all-stopword bigram, but the mixed bigram survives
    assert tokenize("Goldberg Cat Coin", "GOLDCAT", sw) == {"goldberg", "goldberg cat", "goldcat"}
    assert tokenize("PepeTrump 2024", "PT", sw) == {"trump 2024"}  # camelCase split; pepe/trump are stopwords
    assert tokenize("GoldbergFrog", "GF", sw) == {"goldberg", "frog", "goldberg frog"}
    assert "the" not in tokenize("the coin", "X", sw)
    coins = [{"mint": "a", "name": "Goldberg Dog"}, {"mint": "b", "name": "goldberg frog"}, {"mint": "c", "name": "Unrelated"}]
    cl = cluster(coins, sw)
    assert [c["mint"] for c in cl["goldberg"]] == ["a", "b"]
    assert "unrelated" in cl


def test_coin_features_as_of_is_immutable_under_later_inserts(conn, params, ccfg):
    add_coin(conn, "M", T0, dev_buy=1.0)
    add_trade(conn, "M", T0 + 10, "A", 1.0, vsol=31.0e9, vtok=1.04e15)
    add_trade(conn, "M", T0 + 20, "B", 2.0, vsol=33.0e9, vtok=0.98e15)
    add_trade(conn, "M", T0 + 30, "A", 0.5, is_buy=False, vsol=32.5e9, vtok=0.99e15)
    as_of = T0 + 60
    before = features.coin_features(conn, "M", as_of, params, ccfg)
    assert before["unique_buyers_5m"] == 2 and before["net_sol_5m"] == 2.5
    assert before["mcap_sol"] == pytest.approx(32.5 / 0.99, rel=1e-6)
    # later rows: after as_of in chain time, and a late-arriving row whose chain time is before as_of
    add_trade(conn, "M", T0 + 61, "C", 5.0, vsol=40e9, vtok=0.8e15)
    add_trade(conn, "M", T0 + 3600, "D", 9.0)
    add_trade(conn, "M", T0 + 15, "E", 7.0, received_delay=120.0, sig="late-backfill")  # received after as_of
    conn.execute("INSERT INTO migrations(mint, block_time, received_at, source, sources) VALUES('M', ?, ?, 'pumpportal', '[]')", (T0 + 100, T0 + 100.5))
    after = features.coin_features(conn, "M", as_of, params, ccfg)
    assert after == before
    # and as_of later does see them
    later = features.coin_features(conn, "M", T0 + 200, params, ccfg)
    assert later["graduated"] is True and later["unique_buyers_5m"] == 4  # A, B, C and the late E; D is outside 5 min


def test_coin_features_unknown_coin_and_unknown_bundled(conn, params, ccfg):
    assert features.coin_features(conn, "nope", T0, params, ccfg) is None
    add_coin(conn, "M", T0, dev_buy=None)
    f = features.coin_features(conn, "M", T0 + 5, params, ccfg)
    assert f["bundled"] is None and f["mcap_sol"] is None and f["largest_buyer_share_15m"] is None
    assert f["unique_buyers_1m"] == 0  # a real count of nothing


def test_bundled_detection(conn, params, ccfg):
    add_coin(conn, "FAST", T0, dev_buy=1.0)
    conn.execute("INSERT INTO migrations(mint, block_time, received_at, source, sources) VALUES('FAST', ?, ?, 'rpc_logs', '[]')", (T0 + 3, T0 + 3.5))
    add_coin(conn, "BIG", T0, dev_buy=50.0)
    add_coin(conn, "ORG", T0, dev_buy=2.0)
    conn.execute("INSERT INTO migrations(mint, block_time, received_at, source, sources) VALUES('ORG', ?, ?, 'rpc_logs', '[]')", (T0 + 1800, T0 + 1800.5))
    fast = features.coin_features(conn, "FAST", T0 + 10, params, ccfg)
    big = features.coin_features(conn, "BIG", T0 + 10, params, ccfg)
    org = features.coin_features(conn, "ORG", T0 + 2000, params, ccfg)
    assert fast["bundled"] is True and fast["bundled_reasons"] == ["fast_graduation"]
    assert big["bundled"] is True and big["bundled_reasons"] == ["dev_buy_ge_half_curve"]
    assert org["bundled"] is False and org["graduated"] is True


def test_creator_prior_coins_and_sold_fraction(conn, params, ccfg):
    add_coin(conn, "OLD1", T0 - 3600, creator="DEV")
    add_coin(conn, "OLD2", T0 - 90000, creator="DEV")  # outside 24 h
    add_coin(conn, "NEW", T0, creator="DEV", dev_buy=1.0)
    add_trade(conn, "NEW", T0 + 1, "DEV", 1.0, sig="devbuy")
    add_trade(conn, "NEW", T0 + 50, "DEV", 0.5, is_buy=False, sig="devsell")
    f = features.coin_features(conn, "NEW", T0 + 60, params, ccfg)
    assert f["creator_prior_coins_24h"] == 1
    assert f["creator_sold_frac"] == pytest.approx(0.5)


def test_meta_features_burst_and_rank(conn, settings):
    sw = load_stopwords(settings.config("stopwords"))
    mcfg = settings.config("engine")["meta"]
    # baseline: one 'goldberg' coin 30 h ago; now: 4 in the last hour from 3 creators with trades
    add_coin(conn, "B0", T0 - 30 * 3600, name="goldberg old", creator="Z")
    for i in range(4):
        add_coin(conn, f"G{i}", T0 - 600 * (i + 1), name=f"Goldberg thing {i}", creator=f"C{min(i, 2)}")
    for i in range(3):
        add_trade(conn, "G0", T0 - 100 - i, f"W{i}", 10.0, sig=f"g0-{i}")
    add_trade(conn, "G1", T0 - 50, "W0", 5.0, sig="g1")
    add_coin(conn, "Q0", T0 - 100, name="quiet meta", creator="Q")
    recent = features.recent_coins(conn, T0, mcfg["window_s"])
    cl = cluster(recent, sw)
    m = features.meta_features(conn, "goldberg", cl["goldberg"], T0, mcfg, sw)
    assert m["n_coins"] == 4 and m["distinct_creators"] == 3
    assert m["sol_inflow"] == 35.0 and m["unique_buyers"] == 3 and m["mints_with_trades"] == 2
    assert m["baseline_coins_per_h"] == pytest.approx(1 / 42, rel=1e-3)
    assert m["burst_ratio"] == pytest.approx((4 / 3) / (1 / 42), rel=1e-3)
    q = features.meta_features(conn, "quiet", cl["quiet"], T0, mcfg, sw)
    assert q["burst_ratio_is_inf"] is True and q["sol_inflow"] == 0
    ranked = features.rank_metas([q, m])
    assert ranked[0]["term"] == "goldberg"
    assert decide.is_bursting(m, mcfg) is True
    ok, reasons = decide.meta_verdict(m, mcfg)
    assert ok is False and any(r.startswith("burst_ratio") for r in reasons)  # bursting, but only 3 wallets
    ok2, reasons2 = decide.meta_verdict(m, dict(mcfg, observe_min_unique_buyers=3))
    assert ok2 is True and len(reasons2) == 2
    assert decide.meta_verdict(q, mcfg) == (False, ["below_min_coins_or_creators"])


def test_decisions_with_cooldown_and_promotion(conn, settings, params, ccfg):
    wcfg = settings.config("engine")["watchlist"]
    add_coin(conn, "M", T0, dev_buy=1.0)
    watch.add(conn, "M", "probation", T0, 300, 200)
    for i in range(30):
        add_trade(conn, "M", T0 + 1 + i, f"B{i}", 0.5, sig=f"b{i}", vsol=(31 + i * 0.5) * 1e9, vtok=1.0e15)
    f = features.coin_features(conn, "M", T0 + 40, params, ccfg)
    assert f["unique_buyers_5m"] == 30 and f["net_sol_5m"] == 15.0
    assert decide.promote_watchlist(conn, f, T0 + 40, wcfg) == "early_buyers"
    assert conn.execute("SELECT reason FROM watchlist WHERE mint='M'").fetchone()[0] == "early_buyers"
    ok, reasons = decide.coin_verdict(f, ccfg)
    assert ok and len(reasons) == 2
    d1 = decide.observe_coin(conn, f, T0 + 40, T0 + 40.2, ccfg, 1800, "r1")
    assert d1 is not None
    # cooldown blocks a repeat, a later as_of past the cooldown emits again (a new row, never an update)
    assert decide.observe_coin(conn, f, T0 + 100, T0 + 100.2, ccfg, 1800, "r1") is None
    f2 = features.coin_features(conn, "M", T0 + 2000, params, ccfg)
    f2["unique_buyers_5m"], f2["net_sol_5m"] = 30, 15.0  # keep passing for the test
    assert decide.observe_coin(conn, f2, T0 + 2000, T0 + 2000.2, ccfg, 1800, "r2") is not None
    rows = conn.execute("SELECT * FROM decisions ORDER BY id").fetchall()
    assert [r["rules_version"] for r in rows] == ["r1", "r2"]
    assert json.loads(rows[0]["features_json"])["unique_buyers_5m"] == 30
    assert json.loads(rows[0]["reasons_json"])[0].startswith("unique_buyers_5m=30")
    assert conn.execute("SELECT COUNT(*) FROM features_asof WHERE kind='coin'").fetchone()[0] == 2
    # a whale-dominated or bundled coin does not pass
    f["largest_buyer_share_15m"] = 0.9
    assert decide.coin_verdict(f, ccfg)[0] is False
    f["largest_buyer_share_15m"] = 0.1
    f["bundled"] = True
    assert decide.coin_verdict(f, ccfg)[0] is False


def test_engine_tick_end_to_end_without_rpc(conn, settings):
    add_coin(conn, "M", T0, name="Goldberg One", dev_buy=1.0)
    watch.add(conn, "M", "probation", T0, 300, 200)
    for i in range(30):
        add_trade(conn, "M", T0 + 1 + i, f"B{i}", 0.5, sig=f"b{i}", vsol=(31 + i * 0.5) * 1e9, vtok=1.0e15)
    for i in range(3):
        add_coin(conn, f"G{i}", T0 - 100 * (i + 1), name=f"Goldberg {i}", creator=f"C{i}")
    eng = Engine(conn, settings, rpc=None)
    s = eng.tick(T0 + 40)
    assert s["coins"] == 1 and s["promoted"] == 1 and s["coin_decisions"] == 1
    assert s["metas"] >= 1 and s["top_metas"][0][0] == "goldberg"
    assert conn.execute("SELECT value FROM kv WHERE key='engine.heartbeat'").fetchone()[0] == str(T0 + 40)
    # bursting meta (no baseline -> inf) adds its members to the watchlist
    assert conn.execute("SELECT COUNT(*) FROM watchlist WHERE reason='meta_burst'").fetchone()[0] >= 3
    s2 = eng.tick(T0 + 55)
    assert s2["coin_decisions"] == 0  # cooldown
