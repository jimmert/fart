"""Milestone 11: the engine tick back from minutes to seconds without changing one outcome, provably.

Measured on the VPS on 26 Oct 2026 (22:58 to 23:19 UTC; 199,000 trades, 37,500 coins, 7,100 coins in the three-hour window):
the tick took 166 to 337 s, all of it in step 5 (the metas): `meta_features` fetched the baseline coins (6 to 48 hours before
`as_of`, tens of thousands of rows) and tokenized them again for every term, hundreds of times per tick. Milestone 11 counts the
baseline once per tick (`features.meta_baseline`), which changes `fart/engine/features.py`, a rule file, and therefore the rules
fingerprint. This module is the proof that the rules did not change, only the computation time:

1. the milestone-10 implementation is kept byte for byte as `tests/reference/features_m10.py` (its sha256 is pinned here) and runs
   next to the new one on a fixture database of 20,000+ coins over 48 hours and 50,000+ trades: the metas (every term, every field,
   the rounding, the order), the coin features and the verdicts of `decide` are byte-equal as JSON;
2. the other rule files are byte-identical to the frozen week (their partial fingerprint is pinned);
3. the new meta step runs the baseline query once per tick, as an index range (no scan of `coins`), in seconds, and the whole
   tick carries the duration of every step; a tick longer than the loop interval is counted and logged, never hidden;
4. the fingerprint of the working tree is registered in `config/measure.json` -> `community_gate.frozen_rules_equivalents` with
   the date, the PR and the name of the test of (1); the week report pools it with the frozen fingerprint and says so under the
   segment title; the website keeps calling the rules frozen and names the equivalence;
5. with a fast tick `engine.metas_latest` is fresh again (at most 30 s old): the themes card says "fresh"; `MAX_AGE_S` stays 300.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import logging
import random
import sqlite3
import time
from pathlib import Path

import pytest

from fart import db, version
from fart.cli import main
from fart.config import load_settings
from fart.engine import decide, features, themes as engine_themes
from fart.engine.meta import cluster, load_stopwords
from fart.engine.service import STEPS, Engine
from fart.measure import week as wk
from fart.util import day_bounds, day_key, load_json
from fart.version import RULE_FILES, rules_fingerprint
from fart.web import data, public, server
from tests.test_m10_performance import _RecordingConnection, _plans, _serve
from tests.test_m8_web import NOW as WEB_NOW, Client, build_world

ROOT = Path(__file__).resolve().parent.parent
REFERENCE = ROOT / "tests" / "reference" / "features_m10.py"
REFERENCE_SHA256 = "ed70d3530cbf4c9165969c41a8e1ed468bf8bfbde1686e98fb3c317fa6bcd296"   # fart/engine/features.py at dd91c28 (PR #10), byte for byte
OTHER_RULE_FILES_FINGERPRINT = "sha256:c91564ebf3f3b811f8cf708cbb27dee7df88c1d429c7bba4a812dc11be9ddec5"   # the fingerprint algorithm over RULE_FILES minus features.py, at dd91c28
FROZEN = "sha256:e3bf4320c45c7cd3c159bc045a66164ae1754637979ddacc759dc6bd59bbbd46"
PROOF_TEST = "tests/test_m11_engine_performance.py::test_meta_step_old_and_new_implementation_are_byte_equal"
PR = 11
NOW = 1_793_030_000.0   # 2026-10-26T15:53:20Z
N_COINS, N_TRADES, N_WATCHED = 20_000, 50_000, 200
META_STEP_LIMIT_S = 5.0
WORDS = ("goldberg", "quantum", "frog", "moon", "rocket", "pepe", "doge", "giga", "chad", "wojak", "cat", "shiba", "elon", "trump", "pump", "ai", "agent",
         "bonk", "wif", "popcat", "mew", "brett", "neiro", "pnut", "goat", "fartcoin", "ponke", "retardio", "michi", "hippo", "chill", "guy", "sigma",
         "skibidi", "rizz", "gyat", "ohio", "baby", "king", "degen", "radar", "pixel", "meme", "lord", "based", "alpha", "beta", "omega", "zeta", "nova")
HYPE = ("tungsten", "moodeng", "chillguy", "fartboy", "pengu", "zorblax")   # words of the last three hours: bursting terms, some without any baseline


# ----- the frozen reference ------------------------------------------------------------------------------------------------
def load_reference():
    """`tests/reference/features_m10.py`, loaded as a module of the `fart.engine` package so its relative imports resolve."""
    assert hashlib.sha256(REFERENCE.read_bytes()).hexdigest() == REFERENCE_SHA256, "the reference is not the milestone-10 rule file any more"
    spec = importlib.util.spec_from_file_location("fart.engine.features_m10_reference", REFERENCE)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def partial_fingerprint(files) -> str:
    """The algorithm of `fart.version.rules_fingerprint` over a subset of the rule files."""
    h = hashlib.sha256()
    for rel in sorted(files):
        h.update(rel.encode("utf-8"))
        h.update(b"\n")
        p = ROOT / rel
        h.update(p.read_bytes() if p.exists() else b"<absent>")
        h.update(b"\n")
    return "sha256:" + h.hexdigest()


# ----- a fixture database of realistic size -------------------------------------------------------------------------------
def build_fixture(root: Path, now: float, seed: int = 11) -> dict:
    """20,000+ coins over 48 hours (names from a vocabulary, camelCase, stopwords, unicode, digits, no name; 5% without chain time,
    some with clock skew, rows at every window boundary, late backfills), 50,000+ trades (on the coins of the themes window and, in the
    last fifteen minutes, on 200 watched coins: momentum, whales, creator sells, boundary rows, late rows), graduations (slow, fast,
    late), the watchlist and a few X rows. Deterministic from one seed."""
    root.mkdir(parents=True, exist_ok=True)
    settings = load_settings(root=root, env={"FART_RULES_VERSION": "test-rules-v1"})
    settings.ensure_dirs()
    rng = random.Random(seed)
    conn = db.connect(settings.db_path)
    conn.execute("PRAGMA synchronous=OFF")
    conn.execute("BEGIN")
    coins: list[tuple[str, float]] = []

    def add_coin(i, t, name, symbol, creator, block_time, received_at, dev_buy):
        mint = f"M{i:06d}" + "x" * 37
        conn.execute("INSERT INTO coins(mint, name, symbol, creator, block_time, received_at, dev_buy_sol, initial_mcap_sol, source, sources) VALUES(?,?,?,?,?,?,?,?,?,?)",
                     (mint, name, symbol, creator, block_time, received_at, dev_buy, 30.0 + rng.random() * 5, "pumpportal", '["pumpportal","rpc_logs"]'))
        coins.append((mint, t))
        return mint

    def a_name(w1, w2):
        r = rng.random()
        if r < 0.55:
            return f"{w1.capitalize()} {w2.capitalize()} {rng.randrange(1000)}"
        if r < 0.70:
            return f"{w1.capitalize()}{w2.capitalize()}"                       # camelCase: split before lowering
        if r < 0.80:
            return f"{w1} the {w2}"                                              # a stopword in the middle: mixed bigrams
        if r < 0.85:
            return rng.choice(("Pépé Frög", "Dōge Kïng", "Ünicode Quantum", "naïve goldberg", "Straße Frog"))
        if r < 0.90:
            return rng.choice(("COIN", "the pump fun", "meme coin token", "Dev Test"))    # only stopwords: no term
        if r < 0.95:
            return rng.choice(("2024 1000x", "42 69 420", "100x"))                        # only digits
        return None                                                                       # no name at all

    for i in range(N_COINS):
        t = now - rng.random() * 48 * 3600
        hype = rng.random() < (0.3 if t > now - 3 * 3600 else 0.01)
        w1, w2 = (rng.choice(HYPE), rng.choice(WORDS)) if hype else (rng.choice(WORDS), rng.choice(WORDS))
        r = rng.random()
        symbol = w1[:4].upper() if r < 0.8 else (None if r < 0.85 else (f"${w2.upper()}" if r < 0.9 else w2))
        if rng.random() < 0.05:
            block_time, received_at = None, t                                      # received-only: the event time is the receipt
        elif rng.random() < 0.02:
            block_time, received_at = t + 1.5, t + 0.7                             # clock skew: the chain time a moment after the receipt
        else:
            block_time, received_at = t, t + 0.7
        add_coin(i, t, a_name(w1, w2), symbol, f"Cr{rng.randrange(5000)}", block_time, received_at, round(rng.random() * 3, 3))
    i = N_COINS
    # rows at the exact boundaries of the windows of as_of = now (chain time and received-only): `>` and `<=` decide
    for k, (t, chain) in enumerate([(now - 21600.0, True), (now - 21600.0, False), (now - 21600.0 + 0.001, True), (now - 172800.0, True), (now - 172800.0, False),
                                    (now - 172800.0 + 0.001, True), (now - 10800.0, True), (now - 10800.0, False), (now - 10800.0 + 0.001, True),
                                    (now - 10800.0 + 0.001, False), (now, True), (now, False), (now + 0.001, True), (now + 0.001, False)]):
        add_coin(i, t, f"Edgeword Boundary {k}", "EDGE", f"CrEdge{k}", t if chain else None, t, 1.0)
        i += 1
    for k in range(5):   # chain time inside the themes window, received after as_of: unknowable at as_of
        t = now - 7200 - k
        add_coin(i, t, f"Latecomer Backfill {k}", "LATE", "CrLate", t, now + 100 + k, 1.0)
        i += 1
    for k in range(5):   # the same inside the baseline window
        t = now - 30000 - k
        add_coin(i, t, f"Latecomer Baseline {k}", "LATE", "CrLate", t, now + 100 + k, 1.0)
        i += 1
    recent = [c for c in coins if now - 10800 < c[1] <= now]
    watched = sorted(coins, key=lambda c: c[1])[-N_WATCHED:]
    # trades
    rows: list[tuple] = []
    n = 0

    def trade(mint, t, buyer, buy, sol, received=None, vsol=0.0, vtok=0.0, block=True):
        nonlocal n
        rows.append((mint, t if block else None, (t + 0.6) if received is None else received, f"s{n}", buyer, 1 if buy else 0, sol, sol * 1e6,
                     None if vsol is None else (30e9 + vsol * 50e9), None if vsol is None else (1.0e15 - vtok * 3e14), "rpc_trades"))
        n += 1

    pool = recent[: int(len(recent) * 0.8)]   # a fifth of the window's coins never trades: mints_with_trades < n_coins for many terms
    for _ in range(N_TRADES - 10_000):   # on the coins of the themes window, from their creation to now; buyers overlap across coins
        mint, t0 = pool[int(rng.random() ** 2 * len(pool))]
        t = t0 + rng.random() * max(0.0, now - t0)
        trade(mint, t, f"W{rng.randrange(15000)}", rng.random() < 0.7, round(rng.random() * 2.5 + 0.01, 4),
              vsol=None if rng.random() < 0.03 else rng.random(), vtok=rng.random(), block=rng.random() > 0.03)
    for k, (mint, t0) in enumerate(watched):   # the watched coins in the last fifteen minutes: the coin features' windows
        for j in range(50):
            if k < 40:   # forty coins with thirty distinct buyers of 0.5 SOL in the last four minutes: they pass the momentum thresholds
                if j >= 30:
                    break
                trade(mint, now - 240 + j * 7, f"B{k}-{j}", True, 0.5, vsol=rng.random(), vtok=rng.random())
            else:
                trade(mint, now - rng.random() * 900, f"B{rng.randrange(300)}", rng.random() < 0.8, round(rng.random() * 3 + 0.01, 4), vsol=rng.random(), vtok=rng.random())
    for k in range(40, 50):   # whales: one buyer with 80 SOL in the last quarter: a share above 0.35 of the coin's buys
        trade(watched[k][0], now - 400, "Whale", True, 80.0, vsol=rng.random(), vtok=rng.random())
    for k in range(50, 70):   # the creator buys and sells half (creator_sold_frac 0.5)
        mint, t0 = watched[k]
        creator = conn.execute("SELECT creator FROM coins WHERE mint=?", (mint,)).fetchone()[0]
        trade(mint, t0 + 1, creator, True, 1.0, vsol=rng.random(), vtok=rng.random())
        trade(mint, t0 + 50, creator, False, 0.5, vsol=rng.random(), vtok=rng.random())
    for k in range(5):   # rows at the boundaries of the 1, 5 and 15 minute windows (received exactly at the event time)
        mint = watched[k][0]
        for off in (60.0, 60.0 - 0.001, 300.0, 300.0 - 0.001, 900.0, 900.0 - 0.001, 0.0, -0.001):
            trade(mint, now - off, f"Edge{k}", True, 0.25, received=now - off, vsol=rng.random(), vtok=rng.random())
    for j in range(1000):   # late rows: chain time inside the windows, received after as_of
        trade(watched[j % N_WATCHED][0], now - 100 - j * 0.3, f"Late{j}", True, 1.0, received=now + 50 + j)
    conn.executemany("INSERT INTO trades(mint, block_time, received_at, signature, buyer, is_buy, sol, tokens, virtual_sol, virtual_token, source) VALUES(?,?,?,?,?,?,?,?,?,?,?)", rows)
    # graduations: slow ones among the recent coins, fast ones (bundled) among watched coins, some in the baseline, some late
    watched_mints = {m for m, _ in watched}
    for mint, t0 in rng.sample([c for c in recent if c[0] not in watched_mints], 150):
        gt = min(now - 30, t0 + 600 + rng.random() * 3600)
        conn.execute("INSERT INTO migrations(mint, block_time, received_at, source, sources) VALUES(?,?,?,?,?)", (mint, gt, gt + 1.0, "rpc_logs", "[]"))
    for k in range(70, 80):
        mint, t0 = watched[k]
        conn.execute("INSERT INTO migrations(mint, block_time, received_at, source, sources) VALUES(?,?,?,?,?)", (mint, t0 + 20, t0 + 21, "rpc_logs", "[]"))
    for mint, t0 in rng.sample([c for c in coins if c[1] < now - 21600], 20):
        conn.execute("INSERT INTO migrations(mint, block_time, received_at, source, sources) VALUES(?,?,?,?,?)", (mint, t0 + 900, t0 + 901, "rpc_logs", "[]"))
    for k in range(80, 85):
        mint, t0 = watched[k]
        conn.execute("INSERT INTO migrations(mint, block_time, received_at, source, sources) VALUES(?,?,?,?,?)", (mint, now - 200, now + 100, "rpc_logs", "[]"))
    # the watchlist: the 200 newest coins active (every reason), ten inactive older rows
    reasons = ("probation", "probation", "early_buyers", "observe", "meta_burst")
    for k, (mint, t0) in enumerate(watched):
        conn.execute("INSERT INTO watchlist(mint, since, reason, until, active, last_trade_at, activated_at) VALUES(?,?,?,?,1,?,?)", (mint, t0, reasons[k % 5], now + 3600, now - 10, t0))
    for mint, t0 in coins[:10]:
        conn.execute("INSERT INTO watchlist(mint, since, reason, until, active, last_trade_at, activated_at) VALUES(?,?,?,?,0,?,?)", (mint, t0, "probation", t0 + 300, t0 + 100, t0))
    # X: confirmation posts and counts for five watched coins (one post without a creation time, one received after as_of), a fast mega post
    for k in range(5):
        mint = watched[k][0]
        for j in range(3):
            created = None if j == 2 else now - 600 - 100 * j
            received = now + 30 if j == 1 else now - 500 - 100 * j
            conn.execute("INSERT INTO social_posts(id, author, author_id, text, created_at, received_at, likes, kind, followers, query_key) VALUES(?,?,?,?,?,?,?,?,?,?)",
                         (f"p{k}-{j}", f"acct{j}", f"a{j}", f"ca {mint}", created, received, 10 * j, "ca", (1000 * (j + 1)) if j < 2 else None, mint))
        conn.execute("INSERT INTO social_counts(key, kind, ts, n_last_hour, n_24h, query) VALUES(?,?,?,?,?,?)", (mint, "ca", now - 120, 4 + k, 40 + k, mint))
    conn.execute("INSERT INTO social_posts(id, author, author_id, text, created_at, received_at, likes, kind, fast, fast_at, likes_per_min, quotes_per_min)"
                 " VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", ("mega1", "bigaccount", "big", "something", now - 300, now - 290, 5000, "mega", 1, now - 100, 50.0, 2.0))
    conn.execute("COMMIT")
    counts = db.table_counts(conn, ("coins", "trades", "migrations", "watchlist", "social_posts"))
    in_window = len(features.recent_coins(conn, now, 10800.0))   # knowable at `now`: the late backfills of the window are not
    assert len(recent) - 5 == in_window
    return {"settings": settings, "conn": conn, "counts": counts, "watched": [m for m, _ in watched], "recent": in_window, "root": root}


@pytest.fixture(scope="module")
def fx(tmp_path_factory):
    mp = pytest.MonkeyPatch()
    mp.setenv("FART_RULES_VERSION", "test-rules-v1")
    version._CACHE.pop("v", None)
    w = build_fixture(tmp_path_factory.mktemp("m11"), NOW)
    w["ref"] = load_reference()
    w["sw"] = load_stopwords(w["settings"].config("stopwords"))
    w["mcfg"] = w["settings"].config("engine")["meta"]
    ccfg = dict(w["settings"].config("engine")["coin"])
    ccfg.setdefault("bundled_graduation_window_s", 60)
    ccfg.setdefault("bundled_dev_buy_fraction", 0.5)
    w["ccfg"] = ccfg
    from fart.chain.curve import CurveParams

    w["params"] = CurveParams.from_config(w["settings"].config("pumpfun"))
    yield w
    w["conn"].close()
    mp.undo()


def dumps(obj) -> str:
    """Byte-equality: values, key order and list order (no sort_keys), `1` versus `1.0` included."""
    return json.dumps(obj, default=str, ensure_ascii=False, allow_nan=True)


def old_meta_step(ref, conn, as_of, mcfg, sw, terms=None):
    """The milestone-10 step 5, exactly as `themes.compute_metas` ran it then: every term with enough coins, in cluster order."""
    recent = ref.recent_coins(conn, as_of, float(mcfg["window_s"]))
    clusters = cluster(recent, sw, int(mcfg["min_token_len"]))
    metas = [ref.meta_features(conn, term, members, as_of, mcfg, sw)
             for term, members in clusters.items() if len(members) >= int(mcfg["min_coins"]) and (terms is None or term in terms)]
    return recent, clusters, metas


def new_meta_step(conn, as_of, mcfg, sw, terms=None):
    recent = features.recent_coins(conn, as_of, float(mcfg["window_s"]))
    clusters = cluster(recent, sw, int(mcfg["min_token_len"]))
    baseline = features.meta_baseline(conn, as_of, mcfg, sw)
    metas = [features.meta_features(conn, term, members, as_of, mcfg, sw, baseline)
             for term, members in clusters.items() if len(members) >= int(mcfg["min_coins"]) and (terms is None or term in terms)]
    return recent, clusters, metas, baseline


# ----- 1. the proof: old and new, byte for byte -------------------------------------------------------------------------------
def test_the_fixture_has_the_required_size_and_the_reference_is_the_m10_rule_file(fx):
    c = fx["counts"]
    assert c["coins"] >= N_COINS and c["trades"] >= N_TRADES and c["migrations"] >= 180 and c["watchlist"] == N_WATCHED + 10
    span = fx["conn"].execute("SELECT MIN(COALESCE(block_time, received_at)), MAX(COALESCE(block_time, received_at)) FROM coins").fetchone()
    assert span[1] - span[0] >= 47.9 * 3600
    assert hashlib.sha256(REFERENCE.read_bytes()).hexdigest() == REFERENCE_SHA256
    assert REFERENCE.read_bytes() != (ROOT / "fart" / "engine" / "features.py").read_bytes()   # the proof compares two different implementations
    assert "meta_baseline" not in REFERENCE.read_text(encoding="utf-8") and "meta_baseline" in (ROOT / "fart" / "engine" / "features.py").read_text(encoding="utf-8")
    assert fx["ref"].meta_features is not features.meta_features and fx["ref"].ASOF == features.ASOF


def test_meta_step_old_and_new_implementation_are_byte_equal(fx):
    """The proof named in config/measure.json -> frozen_rules_equivalents: the milestone-10 rule file and the milestone-11 one compute
    the same metas on the same data: the same terms, the same field values and rounding, the same field order, the same ranking."""
    ref, conn, mcfg, sw = fx["ref"], fx["conn"], fx["mcfg"], fx["sw"]
    # one full tick at `NOW`: every term, the old implementation first (it is the slow one: the baseline tokenized once per term)
    t0 = time.perf_counter()
    with db.read_tx(conn):
        recent_old, clusters_old, metas_old = old_meta_step(ref, conn, NOW, mcfg, sw)
    fx["old_s"] = time.perf_counter() - t0
    t0 = time.perf_counter()
    with db.read_tx(conn):
        recent_new, clusters_new, metas_new, baseline = new_meta_step(conn, NOW, mcfg, sw)
    fx["new_s"] = time.perf_counter() - t0
    assert dumps(recent_old) == dumps(recent_new)                                      # the same coins in the same order
    assert dumps({k: [m["mint"] for m in v] for k, v in clusters_old.items()}) == dumps({k: [m["mint"] for m in v] for k, v in clusters_new.items()})
    assert len(metas_old) == len(metas_new) >= 100
    assert dumps(metas_old) == dumps(metas_new)                                        # every term, every field, its rounding and its order
    assert all(list(a.keys()) == list(b.keys()) for a, b in zip(metas_old, metas_new))
    assert dumps(ref.rank_metas(metas_old)) == dumps(features.rank_metas(metas_new))   # the same ranking, ties included
    with db.read_tx(conn):
        _, _, ranked, _ = engine_themes.compute_metas(conn, NOW, mcfg, sw)
    assert dumps(ranked) == dumps(ref.rank_metas(metas_old)[: int(mcfg["max_terms"])])   # what the engine's step 5 produces
    # the fixture exercises every branch: terms with a baseline, new words without one, bursting and quiet terms, money and none
    assert sum(1 for m in metas_new if m["burst_ratio_is_inf"]) >= 1 and sum(1 for m in metas_new if m["burst_ratio"] is not None and m["baseline_coins_per_h"] > 0) >= 50
    assert sum(1 for m in metas_new if decide.is_bursting(m, mcfg)) >= 3 and sum(1 for m in metas_new if m["mints_with_trades"] < m["n_coins"]) >= 1
    verdicts = [decide.meta_verdict(m, mcfg) for m in metas_new]
    assert any(ok for ok, _ in verdicts) and any(not ok for ok, _ in verdicts)
    # the boundary rows count exactly as before: both rows at as_of - 6 h (chain time and received-only) and the one just inside as_of - 48 h; the late backfills never
    assert baseline.n_coins > 15_000 and baseline.count("edgeword") == 3 and baseline.count("latecomer") == 0
    # the same at a tick four hours earlier (a sample of terms against the slow reference) and at a tick where the baseline is empty
    for as_of, sample in ((NOW - 4 * 3600, 25), (NOW - 44 * 3600, None)):
        with db.read_tx(conn):
            _, clusters_n, metas_n, _ = new_meta_step(conn, as_of, mcfg, sw)
            terms = None if sample is None else set(random.Random(7).sample([m["term"] for m in metas_n], sample))
            _, _, metas_o = old_meta_step(ref, conn, as_of, mcfg, sw, terms)
        chosen = [m for m in metas_n if terms is None or m["term"] in terms]
        assert len(metas_o) == len(chosen) >= 25 and dumps(metas_o) == dumps(chosen), as_of
        if sample is None:
            assert all(m["burst_ratio_is_inf"] for m in metas_n)   # no coin older than 48 hours: every term is new there


def test_coin_features_and_verdicts_old_and_new_are_byte_equal(fx):
    ref, conn, ccfg, params, mcfg = fx["ref"], fx["conn"], fx["ccfg"], fx["params"], fx["mcfg"]
    wcfg = fx["settings"].config("engine")["watchlist"]
    others = [r[0] for r in conn.execute("SELECT mint FROM coins WHERE mint NOT IN (SELECT mint FROM watchlist) ORDER BY mint LIMIT 100")]
    mints = fx["watched"] + others + ["nope"]
    passes = promotions = total = unknown = 0
    for as_of in (NOW, NOW - 600.0):
        with db.read_tx(conn):
            for mint in mints:
                old = ref.coin_features(conn, mint, as_of, params, ccfg)
                new = features.coin_features(conn, mint, as_of, params, ccfg)
                assert dumps(old) == dumps(new), mint
                if old is None:   # unknown then: "nope", the rows received after as_of, the rows with a chain time after as_of
                    unknown += 1
                    continue
                assert list(old.keys()) == list(new.keys())
                assert decide.coin_verdict(old, ccfg) == decide.coin_verdict(new, ccfg) and decide.wants_promotion(old, wcfg) == decide.wants_promotion(new, wcfg)
                total += 1
                passes += decide.coin_verdict(new, ccfg)[0]
                promotions += decide.wants_promotion(new, wcfg)
            assert dumps(ref.mega_post_feature(conn, as_of)) == dumps(features.mega_post_feature(conn, as_of))
            for mint in fx["watched"][:10]:
                assert dumps(ref.x_features(conn, mint, as_of)) == dumps(features.x_features(conn, mint, as_of))
    # unknown: "nope" twice, the late rows, and at NOW - 600 the watched coins that did not exist yet; the thresholds are exercised in both directions
    assert total + unknown == 2 * (N_WATCHED + 101) and 10 <= unknown <= 150 and 30 <= passes < total // 2 and promotions > passes
    # the whales, the bundled fast graduations and the creator sells are in the data (the same for both implementations, by construction)
    with db.read_tx(conn):
        f_whale = features.coin_features(conn, fx["watched"][45], NOW, params, ccfg)
        f_fast = features.coin_features(conn, fx["watched"][75], NOW, params, ccfg)
        f_creator = features.coin_features(conn, fx["watched"][55], NOW, params, ccfg)
    assert f_whale["largest_buyer_share_15m"] > 0.35 and decide.coin_verdict(f_whale, ccfg)[0] is False
    assert f_fast["bundled"] is True and f_fast["bundled_reasons"] == ["fast_graduation"]
    assert f_creator["creator_sold_frac"] == pytest.approx(0.5)
    # the metas' verdicts, once more on the ranked list the engine uses
    with db.read_tx(conn):
        _, _, metas_old = old_meta_step(ref, conn, NOW - 44 * 3600, mcfg, fx["sw"])
        _, _, metas_new, _ = new_meta_step(conn, NOW - 44 * 3600, mcfg, fx["sw"])
    assert [decide.meta_verdict(m, mcfg) for m in metas_old] == [decide.meta_verdict(m, mcfg) for m in metas_new]
    assert [decide.is_bursting(m, mcfg) for m in metas_old] == [decide.is_bursting(m, mcfg) for m in metas_new]


def test_meta_features_without_a_baseline_computes_the_same_value_at_the_old_cost(fx):
    ref, conn, mcfg, sw = fx["ref"], fx["conn"], fx["mcfg"], fx["sw"]
    with db.read_tx(conn):
        recent = features.recent_coins(conn, NOW, float(mcfg["window_s"]))
        clusters = cluster(recent, sw, int(mcfg["min_token_len"]))
        baseline = features.meta_baseline(conn, NOW, mcfg, sw)
        terms = [t for t, m in clusters.items() if len(m) >= int(mcfg["min_coins"])][:5] + [t for t, m in clusters.items() if len(m) < int(mcfg["min_coins"])][:3]
        for term in terms:
            old = ref.meta_features(conn, term, clusters[term], NOW, mcfg, sw)
            assert dumps(old) == dumps(features.meta_features(conn, term, clusters[term], NOW, mcfg, sw))             # no baseline given: computed for the call
            assert dumps(old) == dumps(features.meta_features(conn, term, clusters[term], NOW, mcfg, sw, baseline))   # the tick's baseline
        # a baseline of another tick or configuration is refused, never silently used
        with pytest.raises(ValueError):
            features.meta_features(conn, terms[0], clusters[terms[0]], NOW + 15, mcfg, sw, baseline)
        with pytest.raises(ValueError):
            features.meta_features(conn, terms[0], clusters[terms[0]], NOW, dict(mcfg, min_token_len=3), sw, baseline)


def test_meta_baseline_counts_each_coin_once_per_term_on_the_exact_window(conn, settings):
    sw = load_stopwords(settings.config("stopwords"))
    mcfg = settings.config("engine")["meta"]
    as_of = NOW

    def coin(mint, t, name, symbol="S", block=True, received=None):
        conn.execute("INSERT INTO coins(mint, name, symbol, creator, block_time, received_at, source, sources) VALUES(?,?,?,?,?,?,'pumpportal','[]')",
                     (mint, name, symbol, "C", t if block else None, t if received is None else received))

    coin("A", as_of - 21600, "Goldberg Goldberg", "GOLD")            # at baseline_to: in; the term once per coin, the bigram and the symbol too
    coin("B", as_of - 21600, "Goldberg Frog", block=False)            # received-only at the boundary: in
    coin("C", as_of - 21600 + 1, "Goldberg too late")                 # a second after baseline_to: not in the baseline
    coin("D", as_of - 172800, "Goldberg at from")                     # at baseline_from: out (>)
    coin("E", as_of - 172800 + 1, "Goldberg just inside", block=False)
    coin("F", as_of - 30000, "Goldberg backfill", received=as_of + 1) # chain time inside, received after as_of: unknowable
    coin("G", as_of - 30000, None, None)                               # nothing to tokenize
    coin("H", as_of - 30000, "the coin", "COIN")                       # only stopwords
    b = features.meta_baseline(conn, as_of, mcfg, sw)
    assert b.n_coins == 5 and b.count("goldberg") == 3 and b.count("goldberg goldberg") == 1 and b.count("gold") == 1 and b.count("frog") == 1   # A, B and E
    assert b.count("goldberg frog") == 1 and b.count("late") == 0 and b.count("inside") == 1 and b.count("backfill") == 0 and b.count("nothing") == 0
    assert b.count("goldberg just") == 1 and b.count("just") == 0   # a mixed bigram survives, a stopword alone does not
    assert b.matches(as_of, 172800.0, 21600.0, 4) and not b.matches(as_of + 1, 172800.0, 21600.0, 4)
    # exactly the rows the milestone-10 query selected
    old = conn.execute(f"SELECT name, symbol FROM coins WHERE {features.ASOF} AND COALESCE(block_time, received_at) > ? AND COALESCE(block_time, received_at) <= ?",
                       (as_of, as_of, as_of - 172800, as_of - 21600)).fetchall()
    new = conn.execute(features.BASELINE_SQL, (as_of, as_of, as_of - 172800, as_of - 21600, as_of - 172800, as_of - 21600)).fetchall()
    assert sorted(repr((r[0], r[1])) for r in old) == sorted(repr((r[0], r[1])) for r in new) and len(new) == 5


# ----- 2. the other rule files are the frozen week's ---------------------------------------------------------------------------
def test_only_features_py_differs_from_the_frozen_rule_files():
    assert "fart/engine/features.py" in RULE_FILES
    assert partial_fingerprint([f for f in RULE_FILES if f != "fart/engine/features.py"]) == OTHER_RULE_FILES_FINGERPRINT
    assert rules_fingerprint() != FROZEN


# ----- 3. seconds, one baseline query per tick, an index range; the tick budget -----------------------------------------------
def test_the_baseline_is_one_index_range_query_per_tick_and_the_step_is_seconds(fx):
    s, mcfg, sw = fx["settings"], fx["mcfg"], fx["sw"]
    conn = sqlite3.connect(f"file:{s.db_path.as_posix()}?mode=ro", uri=True, factory=_RecordingConnection, isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    try:
        t0 = time.perf_counter()
        recent, clusters, metas, baseline = engine_themes.compute_metas(conn, NOW, mcfg, sw)
        new_s = time.perf_counter() - t0
        n_terms = sum(1 for m in clusters.values() if len(m) >= int(mcfg["min_coins"]))
        statements = [(sql, p) for sql, p in conn.statements if not sql.lstrip().upper().startswith(("PRAGMA", "EXPLAIN"))]
        coin_queries = [(sql, p) for sql, p in statements if "FROM coins" in sql]
        assert len(coin_queries) == 2 and n_terms >= 100                               # the window and the baseline: once per tick, not once per term
        base = [(sql, p) for sql, p in coin_queries if sql == features.BASELINE_SQL]
        assert len(base) == 1
        plan = _plans(conn, *base[0])
        assert not any(ln.startswith("SCAN coins") for ln in plan) and any("coins_block_time" in ln for ln in plan), plan   # an index range, both cases of the event time
        assert sum(1 for sql, _ in statements if "FROM trades" in sql) == n_terms and sum(1 for sql, _ in statements if "FROM migrations" in sql) == n_terms
        assert baseline.n_coins == conn.execute(f"SELECT COUNT(*) FROM coins WHERE {features.ASOF} AND COALESCE(block_time, received_at) > ? AND COALESCE(block_time, received_at) <= ?",
                                                (NOW, NOW, NOW - 172800, NOW - 21600)).fetchone()[0]
    finally:
        conn.close()
    assert new_s < META_STEP_LIMIT_S, new_s
    old_s = fx.get("old_s")
    if old_s is None:   # the proof test did not run in this session: measure the old implementation here
        with db.read_tx(fx["conn"]):
            t0 = time.perf_counter()
            old_meta_step(fx["ref"], fx["conn"], NOW, mcfg, sw)
            old_s = time.perf_counter() - t0
    assert new_s * 5 < old_s, (new_s, old_s)   # linear in the coins, not in coins times terms (about 50 times faster on this fixture)


def copy_fixture(fx: dict, root: Path):
    """A copy of the fixture database under a new root (the engine writes)."""
    settings = load_settings(root=root, env={"FART_RULES_VERSION": "test-rules-v1"})
    settings.ensure_dirs()
    dst = sqlite3.connect(str(settings.db_path))
    fx["conn"].backup(dst)
    dst.close()
    return settings


def test_engine_tick_is_seconds_counts_slow_ticks_and_keeps_the_themes_card_fresh(fx, tmp_path, caplog, capsys):
    caplog.set_level(logging.INFO)
    settings = copy_fixture(fx, tmp_path / "engine")
    conn = db.connect(settings.db_path)
    try:
        eng = Engine(conn, settings, rpc=None)
        assert eng.interval_s == 15.0
        s = eng.tick(NOW)
        assert all(isinstance(s[k], float) for k in STEPS) and abs(sum(s[k] for k in STEPS) - s["tick_s"]) < 0.5
        assert s["metas_s"] < META_STEP_LIMIT_S and s["slow"] is False and s["coins"] == N_WATCHED and s["recent_coins"] == fx["recent"] and s["baseline_coins"] > 15_000
        assert s["coin_decisions"] >= 30 and s["metas"] == int(fx["mcfg"]["max_terms"])
        steps_lines = [r for r in caplog.records if r.getMessage() == "engine.tick_steps"]
        assert len(steps_lines) == 1 and set(STEPS) <= set(steps_lines[0].fields) and steps_lines[0].fields["ok"] is True and steps_lines[0].fields["slow"] is False
        assert db.kv_get(conn, "engine.tick_last") is None   # written in the next tick's decision transaction, never outside one
        # the themes card: the snapshot the engine wrote is fresh for the website at most 30 s later; the age limit stays five minutes
        snap = engine_themes.read(conn)
        assert snap is not None and snap["as_of"] == NOW and len(snap["metas"]) == int(fx["mcfg"]["max_terms"])
        ro = data.open_db(settings.db_path)
        try:
            v = public.themes_view(ro, settings, NOW + 30)
            assert v["state"] == "fresh" and v["age_s"] == 30 and len(v["rows"]) == public.THEME_LIMIT and v["bursting"] >= 1
            assert engine_themes.MAX_AGE_S == 300 and public.themes_view(ro, settings, NOW + 331)["state"] == "stale"
        finally:
            ro.close()
        # the second tick writes the first tick's durations to kv, inside its decision write
        s2 = eng.tick(NOW + 15)
        last = json.loads(db.kv_get(conn, "engine.tick_last"))
        assert last["as_of"] == NOW and last["ok"] is True and last["slow"] is False and last["tick_s"] == s["tick_s"] and all(last[k] == s[k] for k in STEPS)
        assert last["coins"] == N_WATCHED and last["metas"] == s["metas"] and last["interval_s"] == 15.0 and db.kv_get(conn, "engine.tick_slow") is None
        assert s2["coin_decisions"] == 0   # the cooldown: the same data, no new observation
        # slow ticks: with a loop interval of zero every tick is slow; counted in kv at the next write, logged at most once per hour
        eng.interval_s = 0.0
        s3 = eng.tick(NOW + 30)
        assert s3["slow"] is True and eng.slow_pending == 1 and db.kv_get(conn, "engine.tick_slow") is None
        eng.tick(NOW + 45)
        assert db.kv_get(conn, "engine.tick_slow") == "1" and db.kv_get(conn, f"engine.tick_slow.{day_key(NOW + 45)}") == "1" and eng.slow_pending == 1
        assert json.loads(db.kv_get(conn, "engine.tick_slow.last"))["as_of"] == NOW + 30 and db.kv_get(conn, "engine.tick_slow.last_at") == str(NOW + 30)
        warn = [r for r in caplog.records if r.getMessage() == "engine.tick_slow"]
        assert len(warn) == 1 and warn[0].levelname == "WARNING" and warn[0].fields["slow_since_last_log"] == 1 and warn[0].fields["as_of"] == NOW + 30
        eng.tick(NOW + 60)
        assert db.kv_get(conn, "engine.tick_slow") == "2" and json.loads(db.kv_get(conn, "engine.tick_slow.last"))["as_of"] == NOW + 45
        assert len([r for r in caplog.records if r.getMessage() == "engine.tick_slow"]) == 1 and eng.slow_since_log == 2   # suppressed, counted
        assert len([r for r in caplog.records if r.getMessage() == "engine.tick_steps"]) == 5
        # a failed tick is recorded with the steps it completed and counted as slow when it was
        eng.steps = {"expire_s": 0.01, "features_s": 19.0}
        rec = eng.record_tick(NOW + 75, 20.0, False)
        assert rec["ok"] is False and rec["slow"] is True and rec["metas_s"] == 0.0 and eng.slow_pending == 2 and eng.last_tick is rec
        eng.tick(NOW + 90)
        assert db.kv_get(conn, "engine.tick_slow") == "4" and json.loads(db.kv_get(conn, "engine.tick_last"))["ok"] is False
        eng.tick(NOW + 105)   # writes the slow tick of NOW + 90 as the last completed one
        assert db.kv_get(conn, "engine.tick_slow") == "5" and json.loads(db.kv_get(conn, "engine.tick_last"))["as_of"] == NOW + 90
    finally:
        conn.close()
    # fart status prints the last completed tick with every step, and the slow ticks
    assert main(["--root", str(settings.root), "--quiet", "status"]) == 0
    out = capsys.readouterr().out
    line = [ln for ln in out.splitlines() if "engine tick:" in ln][0]
    assert "engine tick: last" in line and "SLOW" in line and all(f"{st} " in line for st in ("expire", "features", "promote", "rpc", "metas", "decisions"))
    # the interval printed is the configured one (15 s); the test lowered the engine's own copy to make every tick slow
    assert f"{N_WATCHED} watched coins" in line and "baseline coins" in line and "slow ticks (> 15 s): total=5" in line and "s ago" in line and "FAILED" not in line
    assert main(["--root", str(settings.root), "--quiet", "--json", "status"]) == 0
    js = json.loads(capsys.readouterr().out)["engine_tick"]
    assert js["slow_total"] == 5 and js["last"]["as_of"] == NOW + 90 and js["last"]["ok"] is True and set(STEPS) <= set(js["last"]) and js["slow_last"]["as_of"] == NOW + 90
    from fart.cli import engine_tick_line

    assert engine_tick_line({"interval_s": 15.0, "slow_total": 0, "slow_today": 0}).startswith("engine tick: no completed tick recorded yet")
    failed = engine_tick_line({"interval_s": 15.0, "slow_total": 1, "slow_today": 1, "last_age_s": 3, "last": {"tick_s": 20.0, "ok": False, "slow": True, "expire_s": 0.01, "features_s": 19.0}})
    assert failed.startswith("engine tick: last 20 s FAILED SLOW (expire 0.01 s, features 19 s, promote ?, rpc ?, metas ?, decisions ?; ? watched coins")
    assert engine_tick_line({"interval_s": 15.0, "slow_total": 0, "slow_today": 0, "last_age_s": 12, "last": {"tick_s": 1.84, "ok": True, "slow": False, "expire_s": 0.012, "features_s": 0.52,
                             "promote_s": 0.0, "rpc_s": 0.0, "metas_s": 1.21, "decisions_s": 0.1, "coins": 200, "recent_coins": 1205, "baseline_coins": 48112}}) == \
        "engine tick: last 1.8 s (expire 0.012 s, features 0.52 s, promote 0.00 s, rpc 0.00 s, metas 1.2 s, decisions 0.1 s; 200 watched coins, 1205 in the themes window, 48112 baseline coins; as_of 12 s ago); slow ticks (> 15 s): total=0 today=0"


def test_owner_page_shows_the_tick_steps(tmp_path):
    w = build_world(tmp_path / "w", WEB_NOW, public=True)
    conn = db.connect(w["settings"].db_path)
    rec = {"as_of": WEB_NOW - 20, "tick_s": 1.84, "ok": True, "interval_s": 15.0, "slow": False, "expire_s": 0.012, "features_s": 0.52, "promote_s": 0.0, "rpc_s": 0.0,
           "metas_s": 1.21, "decisions_s": 0.1, "coins": 200, "metas": 50, "recent_coins": 1205, "baseline_coins": 48112, "coin_decisions": 0, "meta_decisions": 1}
    db.kv_set(conn, "engine.tick_last", json.dumps(rec), WEB_NOW - 5)
    db.kv_set(conn, "engine.tick_slow", "3", WEB_NOW - 5)
    db.kv_set(conn, f"engine.tick_slow.{day_key(WEB_NOW)}", "1", WEB_NOW - 5)
    db.kv_set(conn, "engine.tick_slow.last", json.dumps({**rec, "as_of": WEB_NOW - 3600, "tick_s": 184.0, "slow": True}), WEB_NOW - 5)
    conn.close()
    app = server.App(w["settings"], clock=lambda: WEB_NOW)
    c = Client(_serve(app)[1])
    token = c.session()
    st, h, body = c.req("GET", "/eigenaar", {"Cookie": token})
    html = body.decode()
    assert st == 200 and "Engine-tick (stappen en budget)" in html and 'data-live="eigenaar-engine"' in html and 'id="engine"' in html
    for needle in ("1. verlopen en trimmen van de watchlist", "5. thema’s: clustering, basislijn, metas", "6. beslissingen, thema’s, hartslag", "1,2 s", "0,52 s",
                   "binnen het loopinterval van 15 s", "3 / 1", "184 s", "48.112 basislijnmunten", "engine.tick_slow", "nooit werk overgeslagen"):
        assert needle in html, needle
    st, h, body = c.req("GET", "/api/status", {"Cookie": token})
    et = json.loads(body)["engine_tick"]
    assert et["found"] is True and [r["step"][:2] for r in et["rows"]] == ["1.", "2.", "3.", "4.", "5.", "6."] and et["rows"][4]["seconds"] == 1.21
    assert et["state"] == "ok" and et["slow_total"] == 3 and et["slow_today"] == 1 and et["age_s"] == 20 and et["rows"][4]["share_label"] == "66%"
    # without a record the card says so instead of showing zeros
    view = data.engine_tick_view({}, w["settings"], WEB_NOW)
    assert view["found"] is False and view["rows"] == [] and view["state"] == "none" and view["tick_label"] == "nog niet" and view["slow_total"] == 0


# ----- 4. the registration: config, week report, website -----------------------------------------------------------------------
def test_the_new_fingerprint_is_registered_as_a_proven_equivalent():
    gate = load_json(ROOT / "config" / "measure.json")["community_gate"]
    assert gate["frozen_rules"]["rules_fingerprint"] == FROZEN and gate["frozen_rules"]["rules_version"] == "150ca73cb354"   # the frozen week itself is untouched
    fp = rules_fingerprint()
    assert fp != FROZEN
    eq = version.rules_equivalents(gate)
    assert list(eq) == [fp], "the working tree's fingerprint must be the registered equivalent (recompute it after a change of a rule file)"
    e = eq[fp]
    assert e["date"] == "2026-10-29" and e["pr"] == PR and e["proof_test"] == PROOF_TEST and "alleen" not in e["note"] and "only the computation time" in e["note"]
    path, func = e["proof_test"].split("::")
    assert (ROOT / path).resolve() == Path(__file__).resolve() and func in globals() and callable(globals()[func])
    assert version.pooled_fingerprint(fp, gate) == FROZEN and version.pooled_fingerprint(FROZEN, gate) == FROZEN and version.pooled_fingerprint("sha256:other", gate) == "sha256:other"
    assert version.runs_frozen_rules(gate) is True and version.runs_frozen_rules(gate, "sha256:other") is False and version.runs_frozen_rules({}) is False
    assert version.pooled_fingerprint(fp, {}) == fp and version.rules_equivalents(None) == {}


def test_the_week_report_pools_a_proven_equivalent_with_the_frozen_rules(settings):
    from tests.test_m5_week_report import DAYS, syn_day

    cfg = json.loads(json.dumps(settings.config("measure")))   # the real config: frozen_rules and frozen_rules_equivalents
    new_fp = rules_fingerprint()
    hit = {"decisions": 4, "hits": 2, "misses": 2, "unknown": 0, "pending": 0}
    days = [syn_day(d, winners=[(900, None)] * 5, hit=hit, rv="rv-m10" if i < 3 else ("rv-m11" if i < 6 else "rv-other")) for i, d in enumerate(DAYS)]
    fp_map = {"rv-m10": FROZEN, "rv-m11": new_fp, "rv-other": "sha256:other"}
    rep = wk.aggregate({"m1.0": days}, fp_map, cfg, DAYS[-1], 7, day_bounds(DAYS[-1])[1] + 3600, "rv-m11", new_fp, synthetic=True)
    assert [s["rules_group"] for s in rep["segments"]] == [FROZEN, "sha256:other"]
    seg, other = rep["segments"]
    assert seg["frozen"] is True and seg["rules_versions"] == ["rv-m10", "rv-m11"] and seg["hit_rate"]["decisions"] == 24 and seg["hit_rate"]["hits"] == 12
    assert seg["rules_fingerprints"] == {"rv-m10": FROZEN, "rv-m11": new_fp}
    assert len(seg["equivalents"]) == 1 and seg["equivalents"][0]["rules_fingerprint"] == new_fp and seg["equivalents"][0]["rules_versions"] == ["rv-m11"]
    assert seg["equivalents"][0]["pr"] == PR and seg["equivalents"][0]["proof_test"] == PROOF_TEST
    assert other["frozen"] is False and other["equivalents"] == [] and other["rules_fingerprints"] == {"rv-other": "sha256:other"}
    assert [e["rules_fingerprint"] for e in rep["frozen_rules_equivalents"]] == [new_fp]
    md = wk.render_markdown(rep)
    title = md.index(f"## Regels `{FROZEN[:19]}` (bevroren meetweek)")
    note = md.index("Bewezen gelijkwaardige berekening in dit segment")
    assert title < note < md.index("### Samenvatting")   # under the segment title, before the figures
    assert f"rules_version(s) `rv-m11` met vingerafdruk `{new_fp[:19]}`" in md and f"PR #{PR} van 2026-10-29" in md and "alleen de rekentijd" in md and PROOF_TEST in md
    assert md.count("Bewezen gelijkwaardige berekening in dit segment") == 1   # not under the other segment
    assert "- Bewezen gelijkwaardige vingerafdrukken (gepoold met de bevroren regels" in md
    # the same rules_versions without the registration would be two segments: the pooling is declared, not implicit
    bare = json.loads(json.dumps(cfg))
    bare["community_gate"].pop("frozen_rules_equivalents")
    rep2 = wk.aggregate({"m1.0": days}, fp_map, bare, DAYS[-1], 7, day_bounds(DAYS[-1])[1] + 3600, "rv-m11", new_fp, synthetic=True)
    assert [s["rules_group"] for s in rep2["segments"]] == [FROZEN, new_fp, "sha256:other"] and rep2["segments"][1]["frozen"] is False
    assert wk.rules_groups(["a", "b"], {"a": FROZEN, "b": new_fp}) == {FROZEN: ["a"], new_fp: ["b"]}   # without a gate config: no pooling
    assert wk.rules_groups(["a", "b"], {"a": FROZEN, "b": new_fp}, cfg["community_gate"]) == {FROZEN: ["a", "b"]}


def test_the_website_calls_the_equivalent_fingerprint_the_frozen_rules(settings, tmp_path):
    pos = public.week_position(settings, NOW)
    assert pos["rules_frozen"] is True and pos["rules_equivalent"] is True and pos["fingerprint_short"] == rules_fingerprint()[:19]
    fr = public.method_view(settings)["frozen"]
    assert fr["same"] is False and fr["equivalent"] is True and fr["equivalent_entry"] == {"date": "2026-10-29", "pr": PR, "proof_test": PROOF_TEST}
    rows = {r["label"]: r["value"] for s in data.settings_view(settings)["sections"] for r in s["rows"]}
    assert rows["Vingerafdruk gelijk aan de bevroren meetweek"].startswith("gelijkwaardig: bewezen dezelfde uitkomsten") and f"PR #{PR}" in rows["Vingerafdruk gelijk aan de bevroren meetweek"]
    assert rules_fingerprint() in rows["Bewezen gelijkwaardige vingerafdrukken (milestone 11)"] and rows["Vingerafdruk van de regelbestanden"] == rules_fingerprint()
    w = build_world(tmp_path / "w", WEB_NOW, public=True)
    app = server.App(w["settings"], clock=lambda: WEB_NOW)
    app.refresh_views()
    html = app.handle(server.Request("GET", "/methode", {}, {}, b"", "127.0.0.1")).body.decode()
    assert "een bewezen gelijkwaardige berekening van dezelfde regels, alleen de rekentijd veranderde" in html and f"PR #{PR} van 2026-10-29" in html and "ANDERE" not in html
    html = app.handle(server.Request("GET", "/meting", {}, {}, b"", "127.0.0.1")).body.decode()
    assert 'class="pill pill-frozen">regels bevroren<' in html and "regels niet bevroren" not in html
    # a different fingerprint is still "een ANDERE vingerafdruk", never pooled
    from fart.web import pages

    assert "een ANDERE vingerafdruk" in pages.fingerprint_now_sentence({"same": False, "equivalent": False, "fingerprint_now_short": "sha256:x"})
    assert "dezelfde vingerafdruk" in pages.fingerprint_now_sentence({"same": True, "fingerprint_now_short": "sha256:x"})


# ----- 5. deploy files and docs --------------------------------------------------------------------------------------------------
def test_deploy_files_and_docs_for_milestone_11():
    deploy_md = (ROOT / "deploy" / "DEPLOY.md").read_text(encoding="utf-8")
    for needle in ("## 5j. Milestone 11: engine performance (M11)", "systemctl restart fart-engine", "engine.tick_steps", "tick_s", "engine tick: last",
                   "rules fingerprints (engine)", rules_fingerprint()[:19], "frozen_rules_equivalents", "Bewezen gelijkwaardige berekening", "engine.tick_slow",
                   "/eigenaar", "Engine-tick"):
        assert needle in deploy_md, needle
    decisions = (ROOT / "docs" / "DECISIONS.md").read_text(encoding="utf-8")
    for needle in ("## Milestone 11 (29 oktober 2026)", "166 tot 337 s", "alleen de rekentijd", "frozen_rules_equivalents", "tests/reference/features_m10.py",
                   "De regels zijn niet veranderd", rules_fingerprint()[:19]):
        assert needle in decisions, needle
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "## Voor Jim: wat milestone 11 toevoegt" in readme and "engine tick:" in readme
    arch = (ROOT / "docs" / "ARCHITECTURE.md").read_text(encoding="utf-8")
    assert "meta_baseline" in arch and "frozen_rules_equivalents" in arch
    meting = (ROOT / "docs" / "METING.md").read_text(encoding="utf-8")
    assert "gelijkwaardig" in meting and "frozen_rules_equivalents" in meting
    assert (ROOT / "tests" / "reference" / "README.md").exists()
