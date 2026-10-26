"""Milestone 10: the public dashboard fast and robust, without a change of design or content.

Three causes of the time-outs on the VPS (26 Oct 2026, 174,305 trades, 34,635 coins) and their tests:

1. the themes card repeated the engine's clustering on every cache miss -> the engine writes its ranked metas in kv
   every tick (`fart/engine/themes.py`) and the card reads only that value (stale after 5 minutes: it says so);
2. time windows over `trades` and `coins` scanned the tables -> schema v8 adds `trades(received_at)` and
   `trades(block_time)`, every window is bounded on the indexed `received_at` first, and this module proves with
   `EXPLAIN QUERY PLAN` that no time-window query of a public view scans a table;
3. the cache made every request wait for the computation -> one background thread (`server.ViewStore`) computes the
   views on their schedule, a request only reads: a view that hangs for 30 s does not delay `/`, and SIGTERM stops the
   thread. A fixture database of realistic size (200,000 trades, 40,000 coins, 1,000 decisions) keeps every public view
   under 300 ms.
"""
from __future__ import annotations

import json
import os
import random
import re
import signal
import socket
import sqlite3
import threading
import time
from pathlib import Path

import pytest

from fart import db
from fart.cli import main
from fart.config import load_settings
from fart.engine import themes as engine_themes
from fart.engine.service import Engine
from fart.ingest import coverage
from fart.util import day_key, hour_key
from fart.web import auth, data, pages, public, server
from tests.test_m8_web import NOW, PASSWORD, Client, build_world

ROOT = Path(__file__).resolve().parent.parent
N_TRADES, N_COINS, N_DECISIONS, N_MIGRATIONS = 200_000, 40_000, 1_000, 2_000
VIEW_LIMIT_S = 0.3
VIEWS = ("status", "coverage", "observations", "reports", "week", "live", "hot", "new", "themes", "graduations", "social", "today", "method")
# the tables that grow without bound and carry a time column: a window over them must be an index range, never a scan
BIG_TABLES = ("trades", "coins", "migrations", "decisions", "outcomes", "mcap_samples", "coverage", "social_posts")
WORDS = ("goldberg", "quantum", "frog", "moon", "rocket", "pepe", "doge", "giga", "chad", "wojak", "cat", "shiba", "elon", "trump", "pump", "ai", "agent",
         "bonk", "wif", "popcat", "mew", "brett", "neiro", "pnut", "goat", "fartcoin", "ponke", "retardio", "michi", "hippo", "chill", "guy", "sigma",
         "skibidi", "rizz", "gyat", "ohio", "baby", "king", "degen", "radar", "pixel", "meme", "lord", "based", "alpha", "beta", "omega", "zeta", "nova")


# ----- a database of realistic size -------------------------------------------------------------------------------------
def build_big_world(root: Path, now: float, seed: int = 10) -> dict:
    """200,000 trades, 40,000 coins, 2,000 graduations, 1,000 decisions, tracker samples, 200 watched coins with trades
    in the last five minutes, the coverage rows of the last 24 hours and the engine's themes snapshot: what the VPS
    database looks like after two weeks, in one deterministic build."""
    root.mkdir(parents=True, exist_ok=True)
    (root / ".env").write_text(f"FART_WEB_PASSWORD_HASH={auth.hash_password(PASSWORD)}\nFART_WEB_SECRET={auth.new_secret()}\nFART_WEB_BIND=127.0.0.1:0\nFART_WEB_PUBLIC=1\n")
    settings = load_settings(root=root, env={"FART_RULES_VERSION": "test-rules-v1"})
    settings.ensure_dirs()
    rng = random.Random(seed)
    conn = db.connect(settings.db_path)
    conn.execute("PRAGMA synchronous=OFF")
    first = now - 7 * 86400
    for src in ("pumpportal", "rpc_logs"):
        db.kv_set(conn, f"ingest.{src}.first_started", str(first), first)
        g = coverage.gap_open(conn, src, first, "startup")
        coverage.gap_close(conn, g, first + 2)
    for k, t in (("ingest.heartbeat", now - 10), ("engine.heartbeat", now - 20), ("ingest.pumpportal.last_msg_at", now - 5), ("ingest.rpc_logs.last_msg_at", now - 7),
                 ("measure.track.last_run", now - 100)):
        db.kv_set(conn, k, str(t), t)
    db.kv_set(conn, "sol_usd.cache", json.dumps({"ts": now - 60, "price": 180.0, "src": "dexscreener"}), now - 60)
    # coins: 40,000 over seven days, denser in the last hours, names from a vocabulary so themes exist
    coins = []
    conn.execute("BEGIN")
    for i in range(N_COINS):
        if i < 1500:
            t = now - rng.random() * 3 * 3600              # the last three hours: the themes window
        else:
            t = first + rng.random() * (7 * 86400 - 3 * 3600)
        w1, w2 = rng.choice(WORDS), rng.choice(WORDS)
        name = f"{w1.capitalize()} {w2.capitalize()} {rng.randrange(1000)}" if rng.random() < 0.6 else f"{w1.capitalize()}{rng.randrange(100)}"
        mint = f"M{i:06d}" + "x" * 37
        creator = f"Cr{rng.randrange(8000)}"
        conn.execute("INSERT INTO coins(mint, name, symbol, creator, block_time, received_at, dev_buy_sol, initial_mcap_sol, source, sources) VALUES(?,?,?,?,?,?,?,?,?,?)",
                     (mint, name, w1[:4].upper(), creator, t, t + 0.7, round(rng.random() * 3, 3), 30.0 + rng.random() * 5, "pumpportal", '["pumpportal","rpc_logs"]'))
        coins.append((mint, t))
    coins.sort(key=lambda c: c[1])
    # graduations: 2,000 coins, mostly older; the newest ten within the last hour; outcomes for most of them
    grads = []
    for j, idx in enumerate(rng.sample(range(0, N_COINS - 1500), N_MIGRATIONS - 10) + list(range(N_COINS - 10, N_COINS))):
        mint, t = coins[idx]
        gt = min(now - 60, t + 600 + rng.random() * 5 * 3600)
        conn.execute("INSERT INTO migrations(mint, block_time, received_at, source, sources) VALUES(?,?,?,?,?)", (mint, gt, gt + 1.0, "rpc_logs", "[]"))
        grads.append((mint, gt))
        if gt + 86400 < now and rng.random() < 0.8:
            hit = rng.choice((1, 0, 0, 0, None))
            conn.execute("INSERT INTO outcomes(decision_id, subject, mint, anchor_time, horizon_s, measured_at, peak_mcap_usd, hit_100k, bundled, method_version)"
                         " VALUES(?,?,?,?,?,?,?,?,?,?)", (None, "graduation", mint, gt, 86400, gt + 86400, None if hit is None else (150000 if hit else 40000), hit, 0, "m1.0"))
    # tracker samples for the 100 newest graduations: every five minutes for a day (up to now)
    for mint, gt in sorted(grads, key=lambda g: g[1])[-100:]:
        ts = gt
        while ts <= min(now, gt + 86400):
            conn.execute("INSERT INTO mcap_samples(mint, ts, src, mcap_usd, volume_h24_usd, sol_usd) VALUES(?,?,?,?,?,?)", (mint, ts, "dexscreener", 60000.0, 20000.0, 180.0))
            ts += 300
    # the watchlist: the 200 newest coins (probation, early_buyers, observe, meta_burst) with trades in the last five minutes
    watched = [c for c in coins[-200:]]
    reasons = ("probation", "probation", "early_buyers", "observe", "meta_burst")
    for k, (mint, t) in enumerate(watched):
        conn.execute("INSERT INTO watchlist(mint, since, reason, until, active, last_trade_at, activated_at) VALUES(?,?,?,?,1,?,?)", (mint, t, reasons[k % 5], now + 3600, now - 10, t))
    # trades: 180,000 over the last two days on 5,000 coins, 20,000 in the last fifteen minutes on the watched coins
    recent = [c for c in coins if c[1] >= now - 2 * 86400]
    pool = rng.sample(recent, min(5000, len(recent)))
    rows = []
    n = 0
    for i in range(N_TRADES - 20_000):
        mint, t0 = pool[int(rng.random() ** 2 * len(pool))]
        t = t0 + rng.random() * min(2 * 86400, now - t0)
        buy = rng.random() < 0.7
        sol = round(rng.random() * 2.5 + 0.01, 4)
        rows.append((mint, t, t + 0.6, f"s{n}", f"W{rng.randrange(60000)}", 1 if buy else 0, sol, sol * 1e6, 30e9 + rng.random() * 50e9, 1.0e15 - rng.random() * 3e14, "rpc_trades"))
        n += 1
    for i in range(20_000):
        mint, t0 = watched[i % 200]
        t = now - rng.random() * 900
        buy = rng.random() < 0.8
        sol = round(rng.random() * 3 + 0.01, 4)
        rows.append((mint, t, t + 0.6, f"s{n}", f"W{rng.randrange(400)}", 1 if buy else 0, sol, sol * 1e6, 30e9 + rng.random() * 50e9, 1.0e15 - rng.random() * 3e14, "rpc_trades"))
        n += 1
    conn.executemany("INSERT INTO trades(mint, block_time, received_at, signature, buyer, is_buy, sol, tokens, virtual_sol, virtual_token, source) VALUES(?,?,?,?,?,?,?,?,?,?,?)", rows)
    # decisions: 1,000 over the week, outcomes for most coin observations
    feats = {"unique_buyers_5m": 30, "net_sol_5m": 12.5, "largest_buyer_share_15m": 0.2, "bundled": False, "trades_total": 44, "x_mentions_1h": 0, "age_s": 400, "mcap_sol": 45.2}
    for i in range(N_DECISIONS):
        as_of = first + rng.random() * (7 * 86400 - 60)
        if i % 7 == 0:
            term = rng.choice(WORDS)
            conn.execute("INSERT INTO decisions(as_of, created_at, kind, mint, term, features_json, rules_version, verdict, reasons_json) VALUES(?,?,?,?,?,?,?,?,?)",
                         (as_of, as_of + 1, "observe_meta", None, term, json.dumps({"term": term, "n_coins": 5, "distinct_creators": 4, "burst_ratio": 4.2, "sol_inflow": 31.0, "unique_buyers": 40}),
                          "test-rules-v1", "observe", '["burst_ratio=4.2"]'))
            continue
        mint, t = rng.choice(coins)
        cur = conn.execute("INSERT INTO decisions(as_of, created_at, kind, mint, term, features_json, rules_version, verdict, reasons_json) VALUES(?,?,?,?,?,?,?,?,?)",
                           (as_of, as_of + 1, "observe", mint, None, json.dumps({**feats, "mint": mint}), "test-rules-v1", "observe", '["unique_buyers_5m=30>=25"]'))
        if as_of + 86400 < now and rng.random() < 0.7:
            hit = rng.choice((1, 0, 0, None))
            conn.execute("INSERT INTO outcomes(decision_id, subject, mint, anchor_time, horizon_s, measured_at, peak_mcap_usd, hit_100k, bundled, method_version)"
                         " VALUES(?,?,?,?,?,?,?,?,?,?)", (cur.lastrowid, "decision", mint, as_of, 86400, as_of + 86400, None if hit is None else (150000 if hit else 40000), hit, 0, "m1.0"))
    conn.execute("COMMIT")
    # what the daemons leave behind for the website: the coverage rows (the ingest, every minute) and the themes snapshot (the engine, every tick)
    conn.execute("BEGIN")
    for i in range(23, -1, -1):
        coverage.write_hour(conn, hour_key(now - i * 3600), now)
    engine_themes.write(conn, engine_themes.build(conn, settings, now, rules_version="test-rules-v1"), now)
    conn.execute("COMMIT")
    counts = db.table_counts(conn, ("trades", "coins", "migrations", "decisions", "outcomes", "mcap_samples", "watchlist", "coverage"))
    conn.close()
    return {"settings": settings, "counts": counts, "watched": watched, "grads": grads}


@pytest.fixture(scope="module")
def big(tmp_path_factory):
    from fart import version

    mp = pytest.MonkeyPatch()
    mp.setenv("FART_RULES_VERSION", "test-rules-v1")
    for k in ("FART_WEB_PASSWORD_HASH", "FART_WEB_SECRET", "FART_WEB_BIND", "FART_WEB_PUBLIC"):
        mp.delenv(k, raising=False)
    version._CACHE.pop("v", None)
    t0 = time.perf_counter()
    w = build_big_world(tmp_path_factory.mktemp("big"), NOW)
    w["build_s"] = time.perf_counter() - t0
    yield w
    mp.undo()


@pytest.fixture(scope="module")
def world(tmp_path_factory):
    """The milestone-9 fixture world in public mode, for the server tests of this module."""
    from fart import version

    mp = pytest.MonkeyPatch()
    mp.setenv("FART_RULES_VERSION", "test-rules-v1")
    for k in ("FART_WEB_PASSWORD_HASH", "FART_WEB_SECRET", "FART_WEB_BIND", "FART_WEB_PUBLIC"):
        mp.delenv(k, raising=False)
    version._CACHE.pop("v", None)
    yield build_world(tmp_path_factory.mktemp("m10-world"), NOW, public=True)
    mp.undo()


def _views(conn, settings, now):
    return {"status": lambda: public.status_view(conn, settings, now), "coverage": lambda: public.coverage_view(conn, now),
            "observations": lambda: public.observations_view(conn, settings, now), "reports": lambda: public.reports_view(settings, now),
            "week": lambda: public.week_view(settings, now), "live": lambda: public.live_view(conn, settings, now), "hot": lambda: public.hot_coins_view(conn, settings, now),
            "new": lambda: public.new_coins_view(conn, settings, now), "themes": lambda: public.themes_view(conn, settings, now),
            "graduations": lambda: public.graduations_view(conn, settings, now), "social": lambda: public.social_view(conn, settings, now),
            "today": lambda: public.today_view(conn, settings, now), "method": lambda: public.method_view(settings)}


# ----- the schema and the engine ---------------------------------------------------------------------------------------------
def test_schema_v8_adds_the_trade_indexes_idempotently(conn, settings):
    assert db.schema_version(conn) == db.SCHEMA_VERSION == 8
    idx = {r[1] for r in conn.execute("PRAGMA index_list(trades)")}
    assert {"trades_received_at", "trades_block_time", "trades_mint_received"} <= idx
    assert db.migrate(conn) == 8 and {r[1] for r in conn.execute("PRAGMA index_list(trades)")} == idx
    # a v7 database (the VPS before the upgrade) gets the two indexes and keeps every row
    path = settings.data_dir / "v7.db"
    c = sqlite3.connect(str(path), isolation_level=None)
    c.row_factory = sqlite3.Row
    c.executescript(f"BEGIN;\n{db.MIGRATIONS[1]}\nPRAGMA user_version=1;\nCOMMIT;")
    for v in range(2, 8):
        c.execute("BEGIN")
        db.MIGRATIONS[v](c)
        c.execute(f"PRAGMA user_version={v}")
        c.execute("COMMIT")
    c.executemany("INSERT INTO trades(mint, block_time, received_at, signature, is_buy, sol, tokens, source) VALUES(?,?,?,?,?,?,?,?)",
                  [("M", 1.0 * i, 1.0 * i + 0.5, f"s{i}", 1, 1.0, 1.0, "pumpportal") for i in range(50)])
    assert c.execute("PRAGMA user_version").fetchone()[0] == 7 and "trades_received_at" not in {r[1] for r in c.execute("PRAGMA index_list(trades)")}
    c.close()
    c2 = db.connect(path)
    try:
        assert db.schema_version(c2) == 8 and {"trades_received_at", "trades_block_time"} <= {r[1] for r in c2.execute("PRAGMA index_list(trades)")}
        assert c2.execute("SELECT COUNT(*) FROM trades").fetchone()[0] == 50
        plan = " ".join(str(r[3]) for r in c2.execute("EXPLAIN QUERY PLAN SELECT COUNT(*) FROM trades WHERE received_at >= ? AND COALESCE(block_time, received_at) >= ?", (40.0, 41.0)))
        assert "SEARCH trades USING INDEX trades_received_at" in plan and "SCAN trades" not in plan
    finally:
        c2.close()


def test_engine_writes_the_ranked_metas_every_tick(conn, settings):
    from tests.test_engine import T0, add_coin, add_trade

    for i in range(4):
        add_coin(conn, f"G{i}", T0 - 600 * (i + 1), name=f"Goldberg thing {i}", symbol="GOLD", creator=f"C{min(i, 2)}")
    for i in range(3):
        add_trade(conn, "G0", T0 - 100 - i, f"W{i}", 10.0, sig=f"g0-{i}")
    add_trade(conn, "G1", T0 - 50, "W0", 5.0, sig="g1")
    for m in ("G0", "G1", "G2", "G3"):
        conn.execute("INSERT INTO watchlist(mint, since, reason, until, active, activated_at) VALUES(?,?,?,?,1,?)", (m, T0 - 2000, "probation", T0 + 3600, T0 - 2000))
    eng = Engine(conn, settings, rpc=None)
    s1 = eng.tick(T0)
    snap = engine_themes.read(conn)
    assert snap is not None and snap["as_of"] == T0 and snap["rules_version"] == "test-rules-v1" and snap["coins_in_window"] == 4 and snap["clusters_total"] == s1["metas"]
    by_term = {m["term"]: m for m in snap["metas"]}
    g = by_term["goldberg"]
    assert g["n_coins"] == 4 and g["distinct_creators"] == 3 and g["sol_inflow"] == 35.0 and g["unique_buyers"] == 3 and g["burst_ratio_is_inf"] is True
    assert g["bursting"] is True and g["observed_today"] is False and g["examples"][0] == "Goldberg thing 0 ($GOLD)"
    # the four terms share the four coins: the engine's frozen meta_burst choice takes them for the first term it meets, so exactly one term is watched
    assert sum(1 for m in snap["metas"] if m["watched"]) == 1 and {m["term"] for m in snap["metas"]} == {"goldberg", "goldberg thing", "thing", "gold"}
    assert set(g) == {"term", "n_coins", "distinct_creators", "graduations", "sol_inflow", "net_sol", "unique_buyers", "mints_with_trades", "burst_ratio",
                      "burst_ratio_is_inf", "first_seen", "bursting", "watched", "observed_today", "examples"}
    assert len(json.dumps(snap)) < 60_000   # bounded: at most max_terms rows with the card's fields, no mints
    # the next tick overwrites it with the new as_of (one kv row, never a history)
    eng.tick(T0 + 15)
    assert engine_themes.read(conn)["as_of"] == T0 + 15 and conn.execute("SELECT COUNT(*) FROM kv WHERE key LIKE 'engine.metas%'").fetchone()[0] == 1
    # the website reads only the snapshot: fresh within five minutes, otherwise it says so and shows no figure
    ro = data.open_db(settings.db_path)
    try:
        v = public.themes_view(ro, settings, T0 + 25)
        assert v["state"] == "fresh" and v["age_s"] == 10 and sum(1 for r in v["rows"] if r["watched"]) == 1
        assert v["bursting"] == len(v["rows"]) == 4 and {r["term"] for r in v["rows"]} == {"goldberg", "goldberg thing", "thing", "gold"}   # every term of the four names bursts
        assert all(r["burst_label"] == "nieuw woord" and r["sol_inflow_label"] == "35,0 SOL" for r in v["rows"]) and v["computed_label"].endswith("UTC)")
        html = pages.themes_card(v)
        assert '<span class="theme-term">goldberg</span>' in html and "4 opkomend" in html and "berekend door de engine" in html
        stale = public.themes_view(ro, settings, T0 + 15 + engine_themes.MAX_AGE_S + 60)
        assert stale["state"] == "stale" and stale["rows"] == [] and stale["bursting"] == 0 and "niet berekend" in stale["note"] and "6 min oud" in stale["note"]
        html = pages.themes_card(stale)
        assert "nu niet berekend" in html and "6 min oud" in html and "theme-term" not in html and "opkomend<" not in html
    finally:
        ro.close()
    conn.execute("DELETE FROM kv WHERE key=?", (engine_themes.KV_KEY,))
    ro = data.open_db(settings.db_path)
    try:
        missing = public.themes_view(ro, settings, T0 + 30)
        assert missing["state"] == "missing" and missing["rows"] == [] and "nog geen thema" in missing["note"]
    finally:
        ro.close()


# ----- speed on a database of realistic size ---------------------------------------------------------------------------------
def test_every_public_view_stays_under_300_ms_on_a_big_database(big):
    s = big["settings"]
    c = big["counts"]
    assert c["trades"] == N_TRADES and c["coins"] == N_COINS and c["decisions"] == N_DECISIONS and c["migrations"] == N_MIGRATIONS and c["coverage"] == 24
    timings = {}
    for name in VIEWS:
        conn = data.open_db(s.db_path)       # a fresh connection per view: a cold page cache, as a cycle of the thread has it
        try:
            t0 = time.perf_counter()
            v = _views(conn, s, NOW)[name]()
            timings[name] = time.perf_counter() - t0
        finally:
            conn.close()
        assert isinstance(v, dict) and "generated_at" in v or name == "method", name
        if name == "hot":
            assert len(v["rows"]) == 10 and v["watched"] == 200 and v["with_trades"] == 200 and all(r["buyers_5m"] > 0 and r["mcap_sol"] is not None for r in v["rows"])
        if name == "themes":
            assert v["state"] == "fresh" and len(v["rows"]) == 8 and v["coins_in_window"] >= 1400
        if name == "coverage":
            assert v["summary"]["hours_covered"] == 24 and v["summary"]["hours_not_covered"] == 0 and v["summary"]["creates_covered"] > 1000
        if name == "status":
            assert v["live"]["creates_last_hour"] is not None and v["live"]["hour_covered"] and v["totals"]["coins"] == N_COINS
        if name == "live":
            assert v["watched"]["active"] == 200 and v["watched"]["trades_5m"] > 5000 and v["creates"]["last_hour"] is not None
        if name == "observations":
            assert len(v["rows"]) == 500 and v["total_all_time"] == N_DECISIONS and {r["outcome"] for r in v["rows"]} >= {"winner", "not_winner", "unknown"}
        if name == "graduations":
            assert len(v["rows"]) == 10 and all(r["mcap_usd"] is not None for r in v["rows"]) and v["today"] >= 10
        if name == "new":
            assert len(v["rows"]) == 12 and v["rate"]["per_min"] is not None
    slow = {k: round(t, 3) for k, t in timings.items() if t > VIEW_LIMIT_S}
    assert not slow, f"views over {VIEW_LIMIT_S} s on {N_TRADES} trades / {N_COINS} coins: {slow} (all: { {k: round(t, 3) for k, t in timings.items()} })"
    # and the whole first cycle of the background thread, every view in a row on one connection
    app = server.App(s, clock=lambda: NOW)
    t0 = time.perf_counter()
    done = app.refresh_views(force=True)
    total = time.perf_counter() - t0
    assert set(done) >= set(server.App.LIVE_VIEWS) | set(server.App.REST_VIEWS) and total < 10 * VIEW_LIMIT_S, total
    r = app.handle(server.Request("GET", "/", {}, {}, b"", "127.0.0.1"))
    assert r.status == 200 and "wordt berekend" not in r.body.decode() and len(r.body) > 20_000


class _RecordingConnection(sqlite3.Connection):
    """A read-only connection that remembers every statement it ran (the query plans are checked afterwards)."""

    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.statements: list[tuple[str, tuple]] = []

    def execute(self, sql, params=()):  # noqa: ANN001
        self.statements.append((sql, tuple(params) if not isinstance(params, dict) else ()))
        return super().execute(sql, params)


def _plans(conn: sqlite3.Connection, sql: str, params: tuple) -> list[str]:
    return [str(r[3]) for r in conn.execute(f"EXPLAIN QUERY PLAN {sql}", params)]


def test_every_time_window_query_of_the_public_views_uses_an_index(big):
    s = big["settings"]
    conn = sqlite3.connect(f"file:{s.db_path.as_posix()}?mode=ro", uri=True, factory=_RecordingConnection, isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=1")
    try:
        for name in VIEWS:
            _views(conn, s, NOW)[name]()
        statements = [(sql, p) for sql, p in conn.statements if not sql.lstrip().upper().startswith(("PRAGMA", "EXPLAIN"))]
        big_table = lambda sql: any(f"FROM {t}" in sql or f" {t} " in sql or f" {t}(" in sql for t in BIG_TABLES)  # noqa: E731
        # a window: a comparison on a time column; a walk: "the newest first" with a LIMIT (ORDER BY a time column)
        window_re = re.compile(r"\b(received_at|block_time|as_of|measured_at|anchor_time|ts|computed_at|hour|fast_at|created_at)\s*(>=|<=|>|<)")
        walk_re = re.compile(r"ORDER BY .*(received_at|as_of|ts)\b.* DESC LIMIT", re.S)
        windows = [(sql, p) for sql, p in statements if window_re.search(sql) and big_table(sql)]
        walks = [(sql, p) for sql, p in statements if walk_re.search(sql) and big_table(sql) and not window_re.search(sql)]
        assert len(windows) >= 15 and len(walks) >= 3, (len(windows), len(walks))
        offenders = []
        for sql, p in windows:
            for line in _plans(conn, sql, p):
                if line.startswith("SCAN") and any(line.startswith(f"SCAN {t}") for t in BIG_TABLES):
                    offenders.append((sql[:140], line))
        assert not offenders, "\n".join(f"{line}  <-  {sql}" for sql, line in offenders)
        for sql, p in walks:   # an index walk that stops at the LIMIT: never a bare scan, never a temporary sort
            plans = _plans(conn, sql, p)
            assert any("USING INDEX" in ln or "USING COVERING INDEX" in ln or "USING PRIMARY KEY" in ln for ln in plans) and not any("TEMP B-TREE" in ln for ln in plans), (sql[:140], plans)
        # a window over all trades (the shape that scanned the whole table on the VPS: 720 ms per call through trades_mint_received) is an index range now;
        # the views themselves always add the watched mints (`mint IN (...)`), which the planner serves through (mint, received_at) per mint
        plan = _plans(conn, "SELECT COUNT(*), SUM(sol) FROM trades WHERE received_at >= ? AND COALESCE(block_time, received_at) >= ?", (NOW - 960, NOW - 900))
        assert any("SEARCH trades USING INDEX trades_received_at" in ln for ln in plan) and not any(ln.startswith("SCAN trades") for ln in plan), plan
        # the count over the trades of the watched coins is an index range too
        trade_windows = [(sql, p) for sql, p in windows if "FROM trades" in sql]
        assert trade_windows and all(all("USING INDEX" in ln or "USING COVERING INDEX" in ln or "USING PRIMARY KEY" in ln or not ln.startswith(("SCAN", "SEARCH"))
                                         for ln in _plans(conn, sql, p) if "trades" in ln) for sql, p in trade_windows)
    finally:
        conn.close()


# ----- the request thread never waits ----------------------------------------------------------------------------------------
def _serve(app: server.App):
    srv = server.make_server(app, "127.0.0.1", 0)
    t = threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.1}, daemon=True)
    t.start()
    return srv, srv.server_address[1]


def test_the_dashboard_answers_within_a_second_while_a_view_hangs(tmp_path, monkeypatch):
    """A view that takes 30 s (here: blocked on an event) never delays a request: `/` shows "wordt berekend" for that
    card the first time, and the last value afterwards, while the thread is still inside the slow computation."""
    world = build_world(tmp_path / "w", time.time(), public=True)   # a world of this moment: the thread runs on the wall clock
    app = server.App(world["settings"])
    gate = threading.Event()
    hang = {"on": True, "entered": threading.Event()}
    real = public.themes_view

    def slow(conn, s, now, limit=public.THEME_LIMIT):
        if hang["on"]:
            hang["entered"].set()
            gate.wait(30)
        return real(conn, s, now, limit)

    monkeypatch.setattr(public, "themes_view", slow)
    srv, port = _serve(app)
    client = Client(port)
    try:
        app.start_views()
        assert hang["entered"].wait(10)       # the thread is inside the 30 s computation of the themes
        t0 = time.monotonic()
        st, h, body = client.req("GET", "/")
        dt = time.monotonic() - t0
        html = body.decode()
        hd = {k.lower(): v for k, v in h}
        assert st == 200 and dt < 1.0, dt
        assert "wordt berekend" in html and 'data-live="themas"' in html and hd["cache-control"] == "public, max-age=2"
        names = set(__import__("re").findall(r'data-live="([^"]+)"', html))
        assert {"hero", "hot", "nieuw", "themas", "graduaties", "sociaal", "vandaag", "meting-strip"} <= names   # every region exists: the page script keeps swapping
        assert app.store.get("live") is not None and app.store.get("hot").value is not None      # the views before the slow one are there already
        st, h, body = client.req("GET", "/api/public/themes")
        assert st == 200 and json.loads(body) == {**json.loads(body), "state": "computing"} and {k.lower(): v for k, v in h}["cache-control"] == "public, max-age=2"
        # the slow computation ends: the card fills itself at the next request
        hang["on"] = False
        gate.set()
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and (app.store.get("themes") is None or app.store.get("themes").value is None):
            time.sleep(0.05)
        e = app.store.get("themes")
        assert e is not None and e.value["state"] == "fresh" and e.seconds >= 0
        st, h, body = client.req("GET", "/")
        html = body.decode()
        assert st == 200 and "wordt berekend" not in html and '<span class="theme-term">quantum</span>' in html and {k.lower(): v for k, v in h}["cache-control"] == "public, max-age=15"
        first_generated = e.value["generated_at"]
        # the next computation hangs again: the request gets the last value at once, with its own timestamp, never a wait
        hang["entered"].clear()
        gate.clear()
        hang["on"] = True
        with app.store._lock:
            app.store._entries["themes"].next_at = 0.0     # due now
        app.store._wake.set()
        assert hang["entered"].wait(10)
        t0 = time.monotonic()
        st, h, body = client.req("GET", "/")
        dt = time.monotonic() - t0
        html = body.decode()
        assert st == 200 and dt < 1.0 and '<span class="theme-term">quantum</span>' in html and "wordt berekend" not in html
        st, h, body = client.req("GET", "/api/public/themes")
        assert json.loads(body)["generated_at"] == first_generated
        assert any(r["view"] == "themes" and r["state"] == "computed" for r in app.store.stats())
    finally:
        hang["on"] = False
        gate.set()
        stopped = app.stop_views(timeout=10)
        srv.shutdown()
        srv.server_close()
    assert stopped


def test_a_fresh_server_says_computing_until_the_first_cycle(world):
    app = server.App(world["settings"], clock=lambda: NOW)
    r = app.handle(server.Request("GET", "/api/public/live", {}, {}, b"", "127.0.0.1"))
    hd = {k: v for k, v in r.headers}
    assert r.status == 200 and json.loads(r.body) == {"state": "computing", "view": "live", "generated_at": None, "generated_label": "wordt berekend",
                                                      "note": "Dit onderdeel wordt nu berekend; binnen een halve minuut staat het hier."} and hd["Cache-Control"] == "public, max-age=2"
    for path in ("/", "/meting", "/observaties", "/rapporten", "/methode"):
        r = app.handle(server.Request("GET", path, {}, {}, b"", "127.0.0.1"))
        html = r.body.decode()
        assert r.status == 200 and "wordt berekend" in html and 'class="pill pill-computing"' in html and {k: v for k, v in r.headers}["Cache-Control"] == "public, max-age=2", path
        assert "0 SOL" not in html and "0%" not in html, path     # a placeholder is never a number
    assert app.handle(server.Request("GET", f"/rapporten/{world['yesterday']}", {}, {}, b"", "127.0.0.1")).status == 200   # the file exists: computing, not 404
    assert app.handle(server.Request("GET", "/rapporten/2026-01-01", {}, {}, b"", "127.0.0.1")).status == 404
    assert app.handle(server.Request("GET", "/api/public/reports/2026-01-01", {}, {}, b"", "127.0.0.1")).status == 404
    app.refresh_views()
    r = app.handle(server.Request("GET", "/", {}, {}, b"", "127.0.0.1"))
    assert r.status == 200 and "wordt berekend" not in r.body.decode() and {k: v for k, v in r.headers}["Cache-Control"] == "public, max-age=15"
    assert app.handle(server.Request("GET", f"/rapporten/{world['yesterday']}", {}, {}, b"", "127.0.0.1")).body.decode().count("wordt berekend") == 0


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def test_sigterm_stops_the_server_and_the_views_thread(tmp_path, monkeypatch, caplog, capsys):
    import logging

    caplog.set_level(logging.INFO)
    port = _free_port()
    w = build_world(tmp_path / "w", NOW, bind=f"127.0.0.1:{port}", public=True)
    created: list[server.App] = []

    class Spy(server.App):
        def __init__(self, *a, **k):
            super().__init__(*a, **k)
            created.append(self)

    monkeypatch.setattr(server, "App", Spy)
    seen = {}

    def later():
        c = Client(port)
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            try:
                seen["first"] = c.req("GET", "/")
                break
            except OSError:
                time.sleep(0.05)
        while time.monotonic() < deadline and not (created and created[0].store.get("coverage") and created[0].store.get("coverage").value):
            time.sleep(0.05)
        seen["later"] = c.req("GET", "/api/public/live")
        os.kill(os.getpid(), signal.SIGTERM)

    threading.Thread(target=later, daemon=True).start()
    old = signal.getsignal(signal.SIGTERM)
    try:
        rc = server.run_web(w["settings"])
    finally:
        signal.signal(signal.SIGTERM, old)
    assert rc == 0 and seen["first"][0] == 200 and seen["later"][0] == 200 and json.loads(seen["later"][2])["db_exists"] is True
    app = created[0]
    assert not app.store.running and signal.getsignal(signal.SIGTERM) == old
    stopped = [r for r in caplog.records if r.getMessage() == "web.stopped"]
    assert stopped and stopped[-1].fields["views_thread_stopped"] is True
    started = [r for r in caplog.records if r.getMessage() == "web.started"]
    assert started and started[-1].fields["bind"] == f"127.0.0.1:{port}" and started[-1].fields["live_ttl_s"] == 15
    # the durations went to logs/web-views.json, and fart status prints them
    vf = json.loads((w["settings"].logs_dir / server.VIEWS_FILE).read_text())
    assert vf["views"]["live"]["state"] == "computed" and vf["views"]["live"]["seconds"] >= 0 and "report:week-" + w["yesterday"] in vf["views"]
    assert main(["--root", str(w["settings"].root), "--quiet", "status"]) == 0
    out = capsys.readouterr().out
    line = [l for l in out.splitlines() if "web views:" in l][0]
    assert "themes 0." in line and "live 0." in line and "coverage 0." in line and "report pages 3" in line and "[written" in line and "FAILED" not in line
    assert main(["--root", str(w["settings"].root), "--quiet", "--json", "status"]) == 0
    js = json.loads(capsys.readouterr().out)
    assert js["web_views"]["exists"] is True and js["web_views"]["views"]["hot"]["ok"] is True


def test_status_without_the_views_file_and_the_owner_card(world, tmp_path, capsys):
    empty = tmp_path / "empty"
    db.connect(empty / "data" / "fart.db").close()
    assert main(["--root", str(empty), "--quiet", "status"]) == 0
    assert "web views: no timings yet" in capsys.readouterr().out
    (empty / "logs").mkdir(exist_ok=True)
    (empty / "logs" / server.VIEWS_FILE).write_text("{not json")
    assert main(["--root", str(empty), "--quiet", "status"]) == 0
    assert "web views: unreadable" in capsys.readouterr().out
    from fart.cli import web_views_line

    assert web_views_line({"exists": True, "error": None, "age_s": 12, "views": {"themes": {"seconds": 0.0213}, "live": {"seconds": 0.42}, "hot": {"seconds": 12.6, "ok": False},
                                                                                  "new": {"seconds": 0.0004}, "report:2026-10-25": {"seconds": 0.004}}}) \
        == "web views: themes 0.021 s, live 0.42 s, hot 13 s (FAILED), new 0.00 s, report pages 1 (slowest 0.00 s)  [written 12 s ago, 1 failing]"
    # the owner page: the card with the last duration per view, after a cycle
    app = server.App(world["settings"], clock=lambda: NOW)
    app.refresh_views()
    c = Client(_serve(app)[1])
    token = c.session()
    st, h, body = c.req("GET", "/eigenaar", {"Cookie": token})
    html = body.decode()
    assert st == 200 and "Weergaven van de website" in html and 'data-live="eigenaar-weergaven"' in html and 'id="weergaven"' in html
    assert html.count("berekend</td>") >= len(server.App.LIVE_VIEWS) + len(server.App.REST_VIEWS) and "web.view_slow" in html and "draait niet" in html
    st, h, body = c.req("GET", "/api/status", {"Cookie": token})
    wv = json.loads(body)["web_views"]
    assert wv["running"] is False and wv["slow_s"] == 10.0 and {r["view"] for r in wv["rows"]} >= set(server.App.LIVE_VIEWS) and all(r["seconds"] >= 0 for r in wv["rows"])


# ----- coverage from the table ---------------------------------------------------------------------------------------------
def test_coverage_view_reads_the_table_the_ingest_writes(world, tmp_path):
    s = world["settings"]
    conn = data.open_db(s.db_path)
    try:
        table = public.coverage_view(conn, NOW)
        live = data.coverage_view(conn, NOW)
        assert [(r["hour"], r["covered"], r["complete"], r["creates"], r["grads"]) for r in table["rows"]] == \
            [(r["hour"], r["covered"], r["complete"], r["creates_union"], r["grads"]) for r in live["rows"]]
        assert table["summary"] == {"hours_covered": 20, "hours_complete": 16, "hours_not_covered": 4, "creates_covered": live["summary"]["creates_union_covered"],
                                    "grads_covered": live["summary"]["grads_covered"]}
    finally:
        conn.close()
    # the current hour without its row yet (the first minute of an hour): the previous hour's row says so when it is a moment old
    c = db.connect(tmp_path / "c.db")
    prev, cur = hour_key(NOW - 3600), hour_key(NOW)
    c.execute("INSERT INTO coverage(hour, creates_union, grads, complete, covered, computed_at) VALUES(?,?,?,?,?,?)", (prev, 7, 1, 1, 1, NOW - 30))
    assert public._hour_covered(c, NOW) is True and public._day_covered(c, NOW) is True
    c.execute("UPDATE coverage SET computed_at=? WHERE hour=?", (NOW - 400, prev))
    assert public._hour_covered(c, NOW) is False                       # an old row says nothing about now
    c.execute("INSERT INTO coverage(hour, creates_union, grads, complete, covered, computed_at) VALUES(?,?,?,?,?,?)", (cur, 3, 0, 0, 0, NOW - 5))
    assert public._hour_covered(c, NOW) is False                       # the ingest's own row for this hour wins
    c.execute("UPDATE coverage SET covered=1 WHERE hour=?", (cur,))
    assert public._hour_covered(c, NOW) is True
    v = public.coverage_view(c, NOW)
    assert v["summary"]["hours_not_covered"] == 22 and v["rows"][-1]["creates"] == 3 and v["rows"][-2]["creates"] == 7 and v["rows"][0]["creates"] is None
    c.close()


# ----- deploy files and docs ---------------------------------------------------------------------------------------------------
def test_deploy_files_and_docs_for_milestone_10():
    deploy_md = (ROOT / "deploy" / "DEPLOY.md").read_text()
    for needle in ("## 5i. Milestone 10: performance (M10)", "user_version 8", "trades(received_at)", "systemctl restart fart-engine fart-web",
                   "curl -s -o /dev/null -w '%{http_code} %{time_total}s\\n' https://kingdegen.dev/", "web views:", "engine.metas_latest", "wordt berekend",
                   "logs/web-views.json"):
        assert needle in deploy_md, needle
    inst = (ROOT / "deploy" / "install.sh").read_text()
    assert "milestone 10 (user_version 8)" in inst
    decisions = (ROOT / "docs" / "DECISIONS.md").read_text()
    assert "## Milestone 10 (26 oktober 2026)" in decisions and "engine.metas_latest" in decisions and "achtergrondthread" in decisions
    readme = (ROOT / "README.md").read_text()
    assert "milestone 10" in readme.lower() and "web views" in readme
    arch = (ROOT / "docs" / "ARCHITECTURE.md").read_text()
    assert "ViewStore" in arch
    # the rule files are untouched: the fingerprint of the frozen measurement week still holds (the same test as milestone 5)
    from fart.version import RULE_FILES, rules_fingerprint

    frozen = json.loads((ROOT / "config" / "measure.json").read_text())["community_gate"]["frozen_rules"]["rules_fingerprint"]
    assert rules_fingerprint() == frozen and "fart/engine/themes.py" not in RULE_FILES and "fart/engine/service.py" not in RULE_FILES
