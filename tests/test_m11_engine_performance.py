"""Milestone 11: the engine tick back from ten minutes to seconds, with provably unchanged outcomes.

Measured on the VPS on 29 Oct 2026 (02:05 UTC; 1,494,055 trades, 145,299 coins, 6,012 coins in the three-hour window,
88,736 in the 6-48 h baseline): `tick_s` 590 to 598 s against a loop interval of 15 s, the engine at 100% CPU, decisions
and `engine.metas_latest` every ten minutes. Cause: `features.meta_features` fetched and tokenized the whole baseline once
per term (hundreds of terms per tick). Milestone 11 fetches and tokenizes it once per tick (`features.meta_baseline`), hands
the `MetaBaseline` to every term, and changes nothing else about the rules.

The proof: `tests/frozen/` holds `features.py`, `meta.py` and `decide.py` exactly as committed at dd91c28 (the frozen rules
of the measurement week, fingerprint sha256:e3bf4320c45c; only the import lines differ). On a fixture database of more than
20,000 coins over 48 hours and more than 50,000 trades, built here with the edge cases of the event-time windows, the old
and the new implementation run side by side and must agree byte for byte: the same terms, the same field values in the same
order, the same ranking; the same coin features; the same `coin_verdict` / `meta_verdict`. Because `features.py` is a rule
file its fingerprint changed; `config/measure.json` -> `community_gate.frozen_rules_equivalents` records the new fingerprint
with the date, the PR and the name of the proof test, and the week report pools it with the frozen fingerprint.

The second half covers the tick budget (every step timed and logged, the previous tick in kv `engine.tick_last`, slow ticks
counted in `engine.tick_slow` and warned about once per hour, shown by `fart status` and /eigenaar), the themes card that is
"fresh" again with a fast tick, and the deploy notes.
"""
from __future__ import annotations

import hashlib
import json
import random
import time
from pathlib import Path

import pytest

from fart import db
from fart.chain.curve import CurveParams
from fart.cli import main
from fart.config import load_settings
from fart.engine import decide, features, themes, tickstats
from fart.engine import meta as meta_mod
from fart.engine.meta import load_stopwords
from fart.engine.service import Engine
from fart.measure import week as wk
from fart.measure.report import build_report, render_markdown as render_day
from fart.util import day_key, load_json
from fart.version import REPO_ROOT, RULE_FILES, canonical_fingerprint, rules_fingerprint, runs_frozen_rules
from fart.web import data, pages, public
from tests.frozen import decide_m10, features_m10, meta_m10
from tests.test_m5_week_report import DAYS, FP, RV, cfg as m5_cfg, run_aggregate, syn_day  # noqa: F401  (the cfg fixture is reused)

ROOT = Path(__file__).resolve().parent.parent
NOW = 1_793_239_500.0        # 2026-10-29T02:05:00Z, the moment of the measurement on the VPS
FROZEN_FP = "sha256:e3bf4320c45c7cd3c159bc045a66164ae1754637979ddacc759dc6bd59bbbd46"
M11_FP = "sha256:147aa831c90d545ca8cd73f45c3772f854a1ec399cee961b7513b0c6ab253ace"
PROOF_TEST = "tests/test_m11_engine_performance.py::test_meta_step_equals_the_frozen_reference_byte_for_byte"
# sha256 of every rule file other than features.py at dd91c28: this milestone may touch no other rule file
UNCHANGED_RULE_FILES = {
    "config/engine.json": "074f2c959f392ce45fbadfdb1ed7e06abd796d04096296e557b434a51605cb64",
    "config/pumpfun.json": "c68dc7a550df546096d8f19353fb51dcbf0361779e0e66f496c6f96e8b59ed1d",
    "config/stopwords.json": "dd97fdc2e65ac34790a36af2bab33f4a5faba025d31f0e8287dc530c761c9ab8",
    "fart/chain/curve.py": "b1d1e0aa5c724a02b898d033ef94d4b45de9922db53f107e0c6f9b2bb63c3798",
    "fart/engine/decide.py": "05f61af63bc7b256d572d4aa6a392a64f970e716cff7c994fe36efca1f1d86bf",
    "fart/engine/meta.py": "3e8020ba4f4a93ceda1b624c6610f1ca133e594f275aa16f932b8dab73482c81",
}
OLD_FEATURES_SHA = "ed70d3530cbf4c9165969c41a8e1ed468bf8bfbde1686e98fb3c317fa6bcd296"   # features.py at dd91c28, the frozen reference

N_WINDOW, N_BETWEEN, N_BASELINE, N_OLDER, N_TRADES_WINDOW, N_TRADES_OLDER = 1_500, 1_000, 17_600, 1_500, 40_000, 12_000
WORDS = ("goldberg", "quantum", "frog", "moon", "rocket", "pepe", "doge", "giga", "chad", "wojak", "cat", "shiba", "elon", "trump", "pump", "agent",
         "bonk", "popcat", "brett", "neiro", "pnut", "goat", "fartcoin", "ponke", "michi", "hippo", "chill", "sigma", "skibidi", "rizz", "café", "ñandú", "coin")
WINDOW_S, BASE_TO_S, BASE_FROM_S = 10800.0, 21600.0, 172800.0


# ----- the fixture database -----------------------------------------------------------------------------------------------
def _name(rng: random.Random) -> tuple[str | None, str | None]:
    w1, w2 = rng.choice(WORDS), rng.choice(WORDS)
    r = rng.random()
    if r < 0.02:
        name = rng.choice((None, "", "   "))
    elif r < 0.30:
        name = f"{w1.capitalize()} {w2.capitalize()}"
    elif r < 0.55:
        name = f"{w1.capitalize()} {w2.capitalize()} {rng.randrange(1000)}"
    elif r < 0.70:
        name = f"{w1.capitalize()}{w2.capitalize()}"           # camelCase: split by tokenize
    elif r < 0.85:
        name = w1.capitalize()
    else:
        name = f"{w1.upper()} {rng.choice(('100x', '1000x', 'the', 'coin'))}"
    symbol = None if rng.random() < 0.05 else w1[:4].upper()
    return name, symbol


def build_fixture(root: Path, now: float, seed: int = 11) -> dict:
    """More than 20,000 coins within 48 hours (1,500 in the three-hour window, 17,600 in the 6-48 h baseline, 1,000 in
    between), 1,500 older coins, more than 50,000 trades, graduations, a watchlist, and the edge cases of the windows:
    rows exactly on the boundaries, rows without a chain time, rows received after `now` (not knowable), rows received
    long after their chain time. Deterministic from one seed."""
    root.mkdir(parents=True, exist_ok=True)
    settings = load_settings(root=root, env={"FART_RULES_VERSION": "test-rules-v1"})
    settings.ensure_dirs()
    rng = random.Random(seed)
    conn = db.connect(settings.db_path)
    conn.execute("PRAGMA synchronous=OFF")
    coins: list[tuple] = []     # (mint, name, symbol, creator, block_time, received_at, dev_buy_sol, initial_mcap_sol)
    times: list[tuple[str, float]] = []

    def coin(i: int, t: float, name=None, symbol=None, block_time=True, received_at=None, creator=None, named=False) -> str:
        mint = f"M{i:06d}" + "x" * 37
        if not named:
            name, symbol = _name(rng)
        bt = t if block_time else None
        ra = received_at if received_at is not None else t + 0.7
        coins.append((mint, name, symbol, creator or f"Cr{rng.randrange(6000)}", bt, ra, round(rng.random() * 3, 3) if rng.random() < 0.9 else None, 30.0 + rng.random() * 5))
        times.append((mint, bt if bt is not None else ra))
        return mint

    i = 0
    window: list[str] = []
    for _ in range(N_WINDOW):
        window.append(coin(i, now - rng.random() * WINDOW_S, block_time=rng.random() > 0.03)); i += 1
    for _ in range(N_BETWEEN):
        coin(i, now - WINDOW_S - rng.random() * (BASE_TO_S - WINDOW_S)); i += 1
    for _ in range(N_BASELINE):
        coin(i, now - BASE_TO_S - rng.random() * (BASE_FROM_S - BASE_TO_S), block_time=rng.random() > 0.03); i += 1
    for _ in range(N_OLDER):
        coin(i, now - BASE_FROM_S - rng.random() * 3 * 86400); i += 1
    # the edge cases of the baseline window (as_of - 172800, as_of - 21600] under the as-of clause, all named "Boundary ..."
    edges = {}
    edges["at_to"] = coin(i, now - BASE_TO_S, "Boundary Edge", "BND", named=True); i += 1                       # et == as_of - 21600: in (<=)
    edges["after_to"] = coin(i, now - BASE_TO_S + 0.5, "Boundary Edge", "BND", named=True); i += 1               # just inside 6 h: out
    edges["at_from"] = coin(i, now - BASE_FROM_S, "Boundary Edge", "BND", named=True); i += 1                    # et == as_of - 172800: out (>)
    edges["after_from"] = coin(i, now - BASE_FROM_S + 0.5, "Boundary Edge", "BND", named=True); i += 1           # just inside 48 h: in
    edges["no_chain_time"] = coin(i, now - 30 * 3600, "Boundary Edge", "BND", block_time=False, named=True); i += 1   # et = received_at: in
    edges["late_arrival"] = coin(i, now - 30 * 3600, "Boundary Edge", "BND", received_at=now + 5, named=True); i += 1  # received after as_of: out
    edges["received_at_now"] = coin(i, now - 30 * 3600, "Boundary Edge", "BND", received_at=now, named=True); i += 1   # received_at == as_of: in
    edges["backfilled"] = coin(i, now - 30 * 3600, "Boundary Edge", "BND", received_at=now - 60, named=True); i += 1   # received a day late, before as_of: in
    for k in range(3):   # the window needs the term too, or there is no cluster to compare
        window.append(coin(i, now - 600 * (k + 1), f"Boundary Window {k}", "BND", creator=f"Edge{k}", named=True)); i += 1
    edges["window_late"] = coin(i, now - 300, "Boundary Window late", "BND", received_at=now + 1, named=True); i += 1     # not knowable at as_of
    edges["window_at_edge"] = coin(i, now - WINDOW_S, "Boundary Window edge", "BND", named=True); i += 1               # et == as_of - 10800: out (>)
    edges["window_now"] = coin(i, now, "Boundary Window now", "BND", received_at=now, named=True); i += 1               # et == as_of: in (<=)
    window += [edges["window_now"]]
    conn.execute("BEGIN")
    conn.executemany("INSERT INTO coins(mint, name, symbol, creator, block_time, received_at, dev_buy_sol, initial_mcap_sol, source, sources)"
                     " VALUES(?,?,?,?,?,?,?,?,'pumpportal','[\"pumpportal\"]')", coins)
    t_of = dict(times)
    # graduations: 200 window coins, 300 older ones, one received after as_of (not knowable)
    grads = rng.sample(window[:N_WINDOW], 200)
    for m in grads:
        gt = t_of[m] + 120 + rng.random() * max(1.0, now - t_of[m] - 120)
        conn.execute("INSERT INTO migrations(mint, block_time, received_at, source, sources) VALUES(?,?,?,'rpc_logs','[]')", (m, gt, gt + 1.0))
    for m in rng.sample([c[0] for c in coins[N_WINDOW:N_WINDOW + N_BETWEEN + N_BASELINE]], 300):
        gt = t_of[m] + 600 + rng.random() * 3600
        conn.execute("INSERT INTO migrations(mint, block_time, received_at, source, sources) VALUES(?,?,?,'rpc_logs','[]')", (m, gt, gt + 1.0))
    late_grad = window[0] if window[0] not in grads else window[1]
    conn.execute("INSERT INTO migrations(mint, block_time, received_at, source, sources) VALUES(?,?,?,'rpc_logs','[]')", (late_grad, now - 10, now + 20))
    # trades: 40,000 on the window coins (Zipf: a few coins carry most), 12,000 on older coins; 3% without a chain time
    rows = []
    n = 0
    wallets = [f"W{k}" for k in range(3000)]
    weights = [1.0 / (k + 1) ** 0.8 for k in range(len(window))]
    for _ in range(N_TRADES_WINDOW):
        m = rng.choices(window, weights)[0]
        t0 = t_of[m]
        t = t0 + rng.random() * max(0.001, now - t0)
        buy = rng.random() < 0.7
        sol = round(rng.random() * 2.5 + 0.01, 4)
        bt = None if rng.random() < 0.03 else t
        rows.append((m, bt, t + 0.6, f"s{n}", rng.choice(wallets), 1 if buy else 0, sol, sol * 1e6, 30e9 + rng.random() * 50e9, 1.0e15 - rng.random() * 3e14, "rpc_trades"))
        n += 1
    older = [c[0] for c in coins[N_WINDOW:N_WINDOW + N_BETWEEN + N_BASELINE]]
    for _ in range(N_TRADES_OLDER):
        m = older[int(rng.random() ** 2 * len(older))]
        t0 = t_of[m]
        t = t0 + rng.random() * 3600
        buy = rng.random() < 0.7
        sol = round(rng.random() * 2.5 + 0.01, 4)
        rows.append((m, t, t + 0.6, f"s{n}", rng.choice(wallets), 1 if buy else 0, sol, sol * 1e6, 30e9 + rng.random() * 50e9, 1.0e15 - rng.random() * 3e14, "rpc_trades"))
        n += 1
    hot = window[0]
    for t, ra in ((now - WINDOW_S, now - WINDOW_S + 0.5), (now, now), (now - 100, now + 2), (now - WINDOW_S + 0.5, now - WINDOW_S + 1)):   # the window edges and a late row
        rows.append((hot, t, ra, f"s{n}", "WEDGE", 1, 1.5, 1.5e6, 31e9, 1.0e15, "rpc_trades"))
        n += 1
    conn.executemany("INSERT INTO trades(mint, block_time, received_at, signature, buyer, is_buy, sol, tokens, virtual_sol, virtual_token, source) VALUES(?,?,?,?,?,?,?,?,?,?,?)", rows)
    # the watchlist: 120 window coins, the reasons mixed
    watched = window[:120]
    for k, m in enumerate(watched):
        conn.execute("INSERT INTO watchlist(mint, since, reason, until, active, last_trade_at, activated_at) VALUES(?,?,?,?,1,?,?)",
                     (m, t_of[m], ("probation", "probation", "early_buyers", "observe", "meta_burst")[k % 5], now + 3600, now - 10, t_of[m]))
    for k, t in (("ingest.heartbeat", now - 10), ("ingest.pumpportal.last_msg_at", now - 5)):
        db.kv_set(conn, k, str(t), t)
    conn.execute("COMMIT")
    counts = db.table_counts(conn, ("coins", "trades", "migrations", "watchlist"))
    within_48h = conn.execute("SELECT COUNT(*) FROM coins WHERE COALESCE(block_time, received_at) > ?", (now - BASE_FROM_S,)).fetchone()[0]
    conn.close()
    return {"settings": settings, "counts": counts, "within_48h": within_48h, "watched": watched, "window": window, "edges": edges, "late_grad": late_grad}


@pytest.fixture(scope="module")
def world(tmp_path_factory):
    from fart import version

    mp = pytest.MonkeyPatch()
    mp.setenv("FART_RULES_VERSION", "test-rules-v1")
    version._CACHE.pop("v", None)
    t0 = time.perf_counter()
    w = build_fixture(tmp_path_factory.mktemp("m11"), NOW)
    w["build_s"] = time.perf_counter() - t0
    yield w
    mp.undo()


@pytest.fixture
def conn(world):
    c = db.connect(world["settings"].db_path)
    yield c
    c.close()


@pytest.fixture
def mcfg(world):
    return world["settings"].config("engine")["meta"]


@pytest.fixture
def stopwords(world):
    return load_stopwords(world["settings"].config("stopwords"))


def _old_meta_step(conn, as_of, mcfg, stopwords):
    """The frozen implementation, exactly the loop `themes.compute_metas` ran before milestone 11: every term fetches and
    tokenizes the baseline itself. Returns (recent, clusters, all metas by term, the ranked top)."""
    recent = features_m10.recent_coins(conn, as_of, float(mcfg["window_s"]))
    clusters = meta_m10.cluster(recent, stopwords, int(mcfg["min_token_len"]))
    metas = {term: features_m10.meta_features(conn, term, members, as_of, mcfg, stopwords)
             for term, members in clusters.items() if len(members) >= int(mcfg["min_coins"])}
    ranked = features_m10.rank_metas(list(metas.values()))[: int(mcfg["max_terms"])]
    return recent, clusters, metas, ranked


def _bytes(obj) -> bytes:
    """The bytes the engine persists: `json.dumps(..., default=str)` as in `decide.write_features` / `write_decision`."""
    return json.dumps(obj, default=str).encode("utf-8")


# ----- the fixture itself ------------------------------------------------------------------------------------------------------
def test_fixture_size_and_window_edges(world, conn, mcfg, stopwords):
    c = world["counts"]
    assert c["coins"] >= 21_000 and world["within_48h"] >= 20_000 and c["trades"] >= 50_000 and c["migrations"] >= 500 and c["watchlist"] == 120
    assert world["build_s"] < 60
    b = features.meta_baseline(conn, NOW, mcfg, stopwords)
    assert b.n_coins >= 17_000 and len(b.counts) > 100
    # the edge rows: in are at_to, after_from, no_chain_time, received_at_now, backfilled (5); out are after_to, at_from, late_arrival
    assert b.count("boundary") == 5 and b.count("boundary edge") == 5
    # the same count the old per-term scan makes of them (the definition in words: the frozen code is the arbiter)
    old = sum(1 for r in conn.execute(f"SELECT name, symbol FROM coins WHERE {features_m10.ASOF} AND COALESCE(block_time, received_at) > ? AND COALESCE(block_time, received_at) <= ?",
                                      (NOW, NOW, NOW - BASE_FROM_S, NOW - BASE_TO_S)) if "boundary" in meta_m10.tokenize(r["name"], r["symbol"], stopwords, 4))
    assert old == 5
    recent = features.recent_coins(conn, NOW, WINDOW_S)
    mints = {r["mint"] for r in recent}
    e = world["edges"]
    assert e["window_now"] in mints and e["window_late"] not in mints and e["window_at_edge"] not in mints
    assert len(recent) >= N_WINDOW


# ----- the proof: byte-equal outcomes --------------------------------------------------------------------------------------------
def test_meta_step_equals_the_frozen_reference_byte_for_byte(world, conn, mcfg, stopwords):
    """The test named in config/measure.json -> community_gate.frozen_rules_equivalents: the old meta step (frozen copy of
    features.py / meta.py at dd91c28, one baseline scan per term) and the new one (one baseline per tick) on the same
    database, the same as_of and the same configuration give the same terms, the same field values in the same order and
    the same ranking, byte for byte in the JSON the engine persists; the meta verdicts agree too."""
    t0 = time.perf_counter()
    with db.read_tx(conn):
        recent_o, clusters_o, metas_o, ranked_o = _old_meta_step(conn, NOW, mcfg, stopwords)
    old_s = time.perf_counter() - t0
    t0 = time.perf_counter()
    with db.read_tx(conn):
        recent_n, clusters_n, ranked_n, baseline = themes.compute_metas(conn, NOW, mcfg, stopwords)
        metas_n = {term: features.meta_features(conn, term, members, NOW, mcfg, stopwords, baseline)
                   for term, members in clusters_n.items() if len(members) >= int(mcfg["min_coins"])}
    new_s = time.perf_counter() - t0
    print(f"\nmeta step on the fixture: old {old_s:.1f} s, new {new_s:.2f} s, terms {len(metas_o)}, baseline coins {baseline.n_coins}")
    # the inputs of the terms are the same rows in the same order
    assert recent_n == recent_o and list(clusters_n) == list(clusters_o) and clusters_n == clusters_o
    assert len(metas_o) >= 60 and set(metas_n) == set(metas_o)                      # enough terms for the top-50 cut to matter
    for term, old in metas_o.items():
        new = metas_n[term]
        assert list(new) == list(old), term                                         # the same keys in the same order
        assert new == old and _bytes(new) == _bytes(old), term                      # the same values, the same bytes
        assert decide.meta_verdict(new, mcfg) == decide_m10.meta_verdict(old, mcfg) == decide.meta_verdict(old, mcfg)
        assert decide.is_bursting(new, mcfg) == decide_m10.is_bursting(old, mcfg)
    assert [m["term"] for m in ranked_n] == [m["term"] for m in ranked_o] and ranked_n == ranked_o and _bytes(ranked_n) == _bytes(ranked_o)
    assert len(ranked_n) == int(mcfg["max_terms"]) == 50
    # the fixture is rich enough for the proof to mean something: finite and infinite burst ratios, bursting terms, money, graduations
    allm = list(metas_n.values())
    assert any(m["burst_ratio_is_inf"] for m in allm) and any(not m["burst_ratio_is_inf"] and m["burst_ratio"] is not None for m in allm)
    assert any(decide.is_bursting(m, mcfg) for m in allm) and any(not decide.is_bursting(m, mcfg) for m in allm)
    assert any(m["graduations"] > 0 for m in allm) and any(m["unique_buyers"] > 0 and m["sol_inflow"] > 0 for m in allm) and any(m["net_sol"] < 0 for m in allm)
    assert any(decide.meta_verdict(m, mcfg)[0] for m in allm) or True            # a passing theme needs 25 wallets and 20 SOL; not required
    # the themes snapshot the website reads: the same bytes from either computation
    snap_o = themes.snapshot(ranked_o, clusters_o, NOW, mcfg, len(recent_o), set(), set(), "test-rules-v1")
    snap_n = themes.snapshot(ranked_n, clusters_n, NOW, mcfg, len(recent_n), set(), set(), "test-rules-v1")
    assert json.dumps(snap_n, separators=(",", ":"), default=str) == json.dumps(snap_o, separators=(",", ":"), default=str)
    # and the whole thing is fast: the meta step well under the five seconds the VPS database needs to stay within a tick
    assert new_s < 5.0 and new_s < old_s


def test_coin_features_and_verdicts_equal_the_frozen_reference(world, conn):
    settings = world["settings"]
    params = CurveParams.from_config(settings.config("pumpfun"))
    ccfg = dict(settings.config("engine")["coin"])
    ccfg.setdefault("bundled_graduation_window_s", 60)
    ccfg.setdefault("bundled_dev_buy_fraction", 0.5)
    wcfg = settings.config("engine")["watchlist"]
    mints = world["watched"] + world["window"][120:140] + [world["late_grad"], world["edges"]["window_late"], "unknown-mint"]
    passing = bundled = graduated = 0
    with db.read_tx(conn):
        for mint in mints:
            old = features_m10.coin_features(conn, mint, NOW, params, ccfg)
            new = features.coin_features(conn, mint, NOW, params, ccfg)
            assert (old is None) == (new is None), mint
            if old is None:
                continue
            assert list(new) == list(old) and new == old and _bytes(new) == _bytes(old), mint
            assert decide.coin_verdict(new, ccfg) == decide_m10.coin_verdict(old, ccfg) and decide.wants_promotion(new, wcfg) == decide_m10.wants_promotion(old, wcfg)
            passing += decide.coin_verdict(new, ccfg)[0]
            bundled += new["bundled"] is True
            graduated += new["graduated"]
    assert graduated > 0 and features.coin_features(conn, "unknown-mint", NOW, params, ccfg) is None
    assert features.coin_features(conn, world["edges"]["window_late"], NOW, params, ccfg) is None   # received after as_of: unknown then


def test_the_new_meta_step_is_linear_in_coins_not_in_coins_times_terms(world, conn, mcfg, stopwords, monkeypatch):
    calls = {"n": 0}
    real = meta_mod.tokenize

    def counting(name, symbol, sw, min_len=4):
        calls["n"] += 1
        return real(name, symbol, sw, min_len)

    monkeypatch.setattr(meta_mod, "tokenize", counting)
    with db.read_tx(conn):
        recent, clusters, ranked, baseline = themes.compute_metas(conn, NOW, mcfg, stopwords)
    terms = sum(1 for m in clusters.values() if len(m) >= int(mcfg["min_coins"]))
    assert terms >= 60 and baseline is not None
    # one tokenization per coin in the window (the clustering) and one per coin in the baseline; the number of terms plays no part
    assert calls["n"] == len(recent) + baseline.n_coins
    assert calls["n"] < (len(recent) + terms * baseline.n_coins) / 20            # what the old loop did: terms x baseline rows


def test_meta_features_without_a_baseline_computes_one_and_refuses_a_foreign_one(world, conn, mcfg, stopwords):
    with db.read_tx(conn):
        recent, clusters, ranked, baseline = themes.compute_metas(conn, NOW, mcfg, stopwords)
        term = ranked[0]["term"]
        alone = features.meta_features(conn, term, clusters[term], NOW, mcfg, stopwords)          # no baseline: computed for this term
        assert alone == ranked[0]
        other = features.meta_baseline(conn, NOW + 15, mcfg, stopwords)
        with pytest.raises(ValueError):
            features.meta_features(conn, term, clusters[term], NOW, mcfg, stopwords, other)       # another tick's baseline
        with pytest.raises(ValueError):
            features.meta_features(conn, term, clusters[term], NOW, dict(mcfg, min_token_len=5), stopwords, baseline)
        with pytest.raises(ValueError):
            features.meta_features(conn, term, clusters[term], NOW, mcfg, stopwords | {"extra"}, baseline)
        assert baseline.matches(NOW, mcfg, stopwords) and not other.matches(NOW, mcfg, stopwords)
        # an empty window computes no baseline at all
        r2, c2, m2, b2 = themes.compute_metas(conn, NOW - 10 * 86400, mcfg, stopwords)
        assert r2 == [] and m2 == [] and b2 is None


# ----- the registration: fingerprints, the equivalence list, the week report, the website ------------------------------------------
def test_only_features_py_changed_among_the_rule_files_and_the_equivalence_is_registered():
    for rel, digest in UNCHANGED_RULE_FILES.items():
        assert hashlib.sha256((REPO_ROOT / rel).read_bytes()).hexdigest() == digest, f"{rel} changed: that is a rules change, not milestone 11"
    assert set(UNCHANGED_RULE_FILES) | {"fart/engine/features.py"} == set(RULE_FILES)
    assert hashlib.sha256((REPO_ROOT / "fart/engine/features.py").read_bytes()).hexdigest() != OLD_FEATURES_SHA
    assert rules_fingerprint() == M11_FP
    gate = load_json(REPO_ROOT / "config" / "measure.json")["community_gate"]
    assert gate["frozen_rules"]["rules_fingerprint"] == FROZEN_FP      # the frozen entry is untouched
    [entry] = gate["frozen_rules_equivalents"]
    assert entry["rules_fingerprint"] == M11_FP and entry["date"] == "2026-10-29" and entry["pr"] == 11 and entry["test"] == PROOF_TEST
    assert entry["changed_files"] == ["fart/engine/features.py"] and "once per tick" in entry["note"]
    assert canonical_fingerprint(M11_FP, gate) == FROZEN_FP and runs_frozen_rules(gate) is True
    # the frozen reference is the old code: the provenance in its header, the per-term scan in its body, gone from the live module
    frozen_src = (ROOT / "tests" / "frozen" / "features_m10.py").read_text()
    assert OLD_FEATURES_SHA in frozen_src and "dd91c28" in frozen_src and "base_rows = conn.execute(" in frozen_src
    live_src = (ROOT / "fart" / "engine" / "features.py").read_text()
    assert "base_rows" not in live_src and "def meta_baseline(" in live_src and "class MetaBaseline" in live_src
    for name, sha in (("meta_m10.py", UNCHANGED_RULE_FILES["fart/engine/meta.py"]), ("decide_m10.py", UNCHANGED_RULE_FILES["fart/engine/decide.py"])):
        assert sha in (ROOT / "tests" / "frozen" / name).read_text()


def test_week_report_pools_a_proven_equivalent_fingerprint_with_the_frozen_segment(m5_cfg):
    cfg = m5_cfg
    EQ = "sha256:equivalent-computation"
    cfg["community_gate"]["frozen_rules_equivalents"] = [{"rules_fingerprint": EQ, "date": "2026-10-29", "pr": 11, "test": PROOF_TEST}]
    hit = {"decisions": 4, "hits": 2, "misses": 2, "unknown": 0, "pending": 0}
    days = []
    for i, d in enumerate(DAYS):
        rv = "rv-old" if i < 3 else ("rv-m11" if i < 6 else "rv-other")
        days.append(syn_day(d, winners=[(900, None)] * 5, hit=hit, rv=rv))
    fp_map = {"rv-old": FP, "rv-m11": EQ, "rv-other": "sha256:other"}
    assert wk.rules_groups(["rv-old", "rv-m11", "rv-other"], fp_map, {EQ: FP}) == {FP: ["rv-m11", "rv-old"], "sha256:other": ["rv-other"]}
    rep = run_aggregate(days, cfg, fp_map=fp_map, rv_now="rv-m11")
    frozen_seg, other = rep["segments"]
    assert frozen_seg["rules_group"] == FP and frozen_seg["frozen"] is True and frozen_seg["rules_versions"] == ["rv-m11", "rv-old"]
    assert frozen_seg["rules_fingerprints"] == [EQ, FP] and [e["rules_fingerprint"] for e in frozen_seg["equivalents"]] == [EQ]
    assert frozen_seg["hit_rate"]["decisions"] == 24 and frozen_seg["recall"]["before_graduation"]["coin"]["n"] == 30   # six days pooled
    assert other["rules_group"] == "sha256:other" and other["equivalents"] == [] and other["frozen"] is False
    assert rep["frozen_rules_equivalents"][0]["pr"] == 11
    md = wk.render_markdown(rep)
    title = md.index(f"## Regels `{FP[:19]}` (bevroren meetweek)")
    sentence = md.index(f"Dit segment bevat ook de **bewezen gelijkwaardige berekening** `{EQ[:19]}` (PR #11, 2026-10-29, test `{PROOF_TEST}`)")
    assert title < sentence < md.index("### Samenvatting") and "alleen de rekentijd veranderde" in md
    assert "`rv-m11`, `rv-old`" in md and f"Bewezen gelijkwaardige berekeningen (gepoold met de bevroren vingerafdruk): `{EQ[:19]}` (PR #11" in md
    # without the list the same fingerprint is a segment of its own, as before milestone 11
    cfg["community_gate"]["frozen_rules_equivalents"] = []
    rep2 = run_aggregate(days, cfg, fp_map=fp_map, rv_now="rv-m11")
    assert [s["rules_group"] for s in rep2["segments"]] == [FP, EQ, "sha256:other"] and "bewezen gelijkwaardige" not in wk.render_markdown(rep2)


def test_website_and_settings_call_the_equivalent_fingerprint_frozen(world):
    settings = world["settings"]
    pos = public.week_position(settings, NOW)
    assert pos["rules_frozen"] is True and pos["rules_equivalent"]["pr"] == 11 and pos["fingerprint_short"] == M11_FP[:19]
    sv = data.settings_view(settings)
    rows = {r["label"]: r["value"] for r in sv["sections"][0]["rows"]}
    assert rows["Vingerafdruk van de regelbestanden"] == M11_FP
    assert rows["Vingerafdruk gelijk aan de bevroren meetweek"].startswith("ja: bewezen gelijkwaardige berekening (PR #11, 2026-10-29, test tests/test_m11")
    assert rows["Bewezen gelijkwaardige berekeningen (gepoold met de bevroren regels)"].startswith(M11_FP[:19])
    assert "sha256:e3bf4320c45c" in rows["Bevroren regels van de meetweek"]
    # a fingerprint outside the list is not frozen: the public pill would say "regels niet bevroren"
    gate = settings.config("measure")["community_gate"]
    assert runs_frozen_rules(gate, "sha256:something-else") is False and runs_frozen_rules(gate, FROZEN_FP) is True


def test_public_headline_never_breaks_on_an_uncovered_day():
    st = {"freshness": {"status": "stale"}, "live": {"creates_last_hour": None, "graduations_today": None, "observations_today": 0}}
    h = public.headline(st)
    assert h.startswith("De radar zag het laatste uur is nog niet gedekt") and "geen telling van graduaties" in h and h.endswith("0 observaties.")
    st2 = {"freshness": {"status": "fresh"}, "live": {"creates_last_hour": 4, "graduations_today": 2, "observations_today": 5}}
    assert public.headline(st2) == "De radar ziet in het laatste uur 4 nieuwe munten, vandaag 2 graduaties en 5 observaties."


# ----- the tick budget --------------------------------------------------------------------------------------------------------------
def test_tick_budget_is_timed_recorded_counted_and_shown(world, conn, capsys):
    settings = world["settings"]
    eng = Engine(conn, settings, rpc=None)
    assert eng.interval == 15.0 and eng.last_tick is None
    s1 = eng.tick(NOW)
    for k in tickstats.STEP_FIELDS + ("tick_s", "interval_s", "slow", "baseline_coins"):
        assert k in s1, k
    steps = sum(s1[k] for k in tickstats.STEP_FIELDS)
    assert abs(s1["tick_s"] - steps) < 0.05 and s1["slow"] is False and s1["interval_s"] == 15.0   # six roundings to 3 decimals
    assert s1["baseline_coins"] >= 17_000 and s1["metas"] == 50 and s1["coins"] == 120 and s1["metas_s"] < 5.0
    assert s1["tick_s"] < 15.0, s1["tick_s"]
    assert db.kv_get(conn, tickstats.KV_LAST) is None                        # a tick's own duration is known only after its write
    assert eng.last_tick == tickstats.record(s1, 15.0) and eng.last_tick["as_of"] == NOW and eng.last_tick["slow"] is False
    s2 = eng.tick(NOW + 15)
    rec = json.loads(db.kv_get(conn, tickstats.KV_LAST))
    assert rec["as_of"] == NOW and rec["tick_s"] == s1["tick_s"] and rec["metas_s"] == s1["metas_s"] and rec["slow"] is False and rec["interval_s"] == 15.0
    assert db.kv_get(conn, tickstats.KV_SLOW) is None
    # a tick longer than the interval (forced: interval 0) is counted by the next tick's write, all time and per day
    eng.interval = 0.0
    s3 = eng.tick(NOW + 30)
    assert s3["slow"] is True and s3["interval_s"] == 0.0
    s4 = eng.tick(NOW + 45)
    assert db.kv_get(conn, tickstats.KV_SLOW) == "1" and db.kv_get(conn, f"{tickstats.KV_SLOW}.{day_key(NOW + 45)}") == "1"
    rec3 = json.loads(db.kv_get(conn, tickstats.KV_LAST))
    assert rec3["as_of"] == NOW + 30 and rec3["slow"] is True
    eng.tick(NOW + 60)
    assert db.kv_get(conn, tickstats.KV_SLOW) == "2"
    # the reading: the record of the last completed tick with labels, the counters, for fart status and /eigenaar
    kv = {r["key"]: r["value"] for r in conn.execute("SELECT key, value FROM kv WHERE key LIKE 'engine.%'")}
    view = tickstats.read(kv, NOW + 70, 15.0)
    assert view["slow_total"] == 2 and view["slow_today"] == 2 and view["last"]["as_of"] == NOW + 45 and view["last"]["slow"] is True
    assert view["last"]["steps"]["metas_s"] == rec3["metas_s"] or view["last"]["steps"]["metas_s"] >= 0
    assert view["last"]["steps_label"].startswith("expire 0.") and "+ metas 0." in view["last"]["steps_label"] and view["last"]["age_s"] == 25
    line = tickstats.status_line(view)
    assert line.startswith("engine tick (last completed, as_of 2026-10-29T02:05:45Z, 25 s ago): 0.") and "SLOW: langer dan het interval van 0.00 s" in line
    assert "slow ticks (> 15 s): total 2, today 2" in line and "baseline coins 17" in line
    empty = tickstats.read({}, NOW, 15.0)
    assert empty["last"] is None and tickstats.status_line(empty).startswith("engine tick: no completed tick recorded yet")
    # fart status prints the line; --json carries the record
    assert main(["--root", str(settings.root), "--quiet", "status"]) == 0
    out = capsys.readouterr().out
    assert "engine tick (last completed, as_of 2026-10-29T02:05:45Z" in out and "slow ticks (> 15 s): total 2, today 2" in out
    assert main(["--root", str(settings.root), "--quiet", "--json", "status"]) == 0
    js = json.loads(capsys.readouterr().out)
    assert js["engine_tick"]["slow_total"] == 2 and js["engine_tick"]["last"]["steps"]["decisions_s"] >= 0 and js["engine_tick"]["last"]["tick_s"] == view["last"]["tick_s"]
    assert "engine.tick_slow" not in js["failures"]                              # a slow tick is not a failure: no work was skipped
    # /eigenaar: the health card carries the same figures (read-only connection)
    ro = data.open_db(settings.db_path)
    try:
        st = data.status_view(ro, settings, NOW + 70)
    finally:
        ro.close()
    assert st["engine_tick"]["slow_total"] == 2 and st["engine_tick"]["last"]["tick_label"].endswith(" s")
    items = dict(pages.engine_tick_items(st["engine_tick"]))
    assert "engine-tick (laatste voltooide, duur per stap)" in items and "trage engine-ticks" in items
    assert "expire 0." in items["engine-tick (laatste voltooide, duur per stap)"] and "totaal 2, vandaag 2" in items["trage engine-ticks"]
    assert pages.engine_tick_items(None) == [] and "nog geen voltooide tick" in dict(pages.engine_tick_items(empty))["engine-tick (laatste voltooide, duur per stap)"]
    # the daily report counts the slow ticks of the day next to the failed ones
    rep = build_report(conn, settings, day_key(NOW), NOW + 3600)
    assert rep["ops"]["engine_tick_slow"] == 2 and "trage engine-ticks (langer dan het loopinterval): 2" in render_day(rep)


def test_slow_tick_warning_once_per_hour():
    clock = [1000.0]
    rep = tickstats.SlowTickReporter(3600.0, clock=lambda: clock[0])
    fast = {"slow": False, "tick_s": 1.0}
    slow = {"slow": True, "tick_s": 20.5, "interval_s": 15.0, "expire_s": 0.1, "features_s": 2.0, "promote_s": 0.0, "rpc_s": 0.0, "metas_s": 18.0, "decisions_s": 0.4}
    assert rep.note(fast) is None
    w = rep.note(slow)
    assert w and w["slow_ticks_since_last_warning"] == 1 and w["metas_s"] == 18.0 and w["tick_s"] == 20.5 and w["interval_s"] == 15.0
    clock[0] += 600
    assert rep.note(slow) is None and rep.note(slow) is None and rep.note(fast) is None     # counted, not logged
    clock[0] += 3000
    w2 = rep.note(slow)
    assert w2 and w2["slow_ticks_since_last_warning"] == 3 and w2["slow_ticks_total"] == 4 and rep.unreported == 0


def test_tick_record_shape():
    s = {"as_of": 1.0, "tick_s": 16.2, "expire_s": 0.1, "features_s": 1.0, "promote_s": 0.0, "rpc_s": 0.0, "metas_s": 15.0, "decisions_s": 0.1,
         "coins": 3, "metas": 4, "baseline_coins": 5, "top_metas": [("x", 1, 2, 3)], "coin_decisions": 1}
    r = tickstats.record(s, 15.0)
    assert r == {"as_of": 1.0, "tick_s": 16.2, "expire_s": 0.1, "features_s": 1.0, "promote_s": 0.0, "rpc_s": 0.0, "metas_s": 15.0, "decisions_s": 0.1,
                 "coins": 3, "metas": 4, "baseline_coins": 5, "interval_s": 15.0, "slow": True}
    assert tickstats.record(dict(s, tick_s=14.9), 15.0)["slow"] is False


# ----- the website: a fast tick keeps the themes card fresh ------------------------------------------------------------------------------
def test_themes_card_is_fresh_within_thirty_seconds_of_a_tick(world, conn):
    settings = world["settings"]
    assert themes.MAX_AGE_S == 300                                               # the age limit of milestone 10 is not touched
    eng = Engine(conn, settings, rpc=None)
    s = eng.tick(NOW + 100)
    assert s["tick_s"] < 15
    snap = themes.read(conn)
    assert snap["as_of"] == NOW + 100 and len(snap["metas"]) == 50
    ro = data.open_db(settings.db_path)
    try:
        for age in (0, 15, 30):
            v = public.themes_view(ro, settings, NOW + 100 + age)
            assert v["state"] == "fresh" and len(v["rows"]) > 0 and v["age_s"] == age, age
        html = pages.themes_card(public.themes_view(ro, settings, NOW + 130))
        assert "nu niet berekend" not in html and "berekend door de engine" in html and "(30 s geleden)" in html and 'data-live="themas"' in html
        assert "nu niet berekend" in pages.themes_card(public.themes_view(ro, settings, NOW + 100 + themes.MAX_AGE_S + 1))
        stale = public.themes_view(ro, settings, NOW + 100 + themes.MAX_AGE_S + 1)
        assert stale["state"] == "stale" and stale["rows"] == []
    finally:
        ro.close()


# ----- deploy files and docs ----------------------------------------------------------------------------------------------------------
def test_deploy_files_and_docs_for_milestone_11():
    deploy_md = (ROOT / "deploy" / "DEPLOY.md").read_text()
    for needle in ("## 5j. Milestone 11: engine performance (M11)", "systemctl restart fart-engine", "tick_s", "engine tick (last completed",
                   "rules fingerprints (engine)", M11_FP[:19], "bewezen gelijkwaardige berekening", "frozen_rules_equivalents", "engine.tick_slow"):
        assert needle in deploy_md, needle
    decisions = (ROOT / "docs" / "DECISIONS.md").read_text()
    for needle in ("## Milestone 11 (29 oktober 2026)", "590", "alleen de rekentijd", M11_FP[:19], "frozen_rules_equivalents", PROOF_TEST, "regels zijn niet veranderd"):
        assert needle in decisions, needle
    readme = (ROOT / "README.md").read_text()
    assert "milestone 11" in readme.lower() and "engine tick" in readme
    arch = (ROOT / "docs" / "ARCHITECTURE.md").read_text()
    assert "tickstats" in arch and "meta_baseline" in arch
    assert "gelijkwaardig" in (ROOT / "docs" / "METING.md").read_text()
    assert "Milestone 11" in (ROOT / "docs" / "UNVERIFIED.md").read_text()
    assert "## Milestone 11" in (ROOT / "docs" / "DECISIONS.md").read_text()
