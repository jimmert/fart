"""Milestone 4, items 1 and 4: a stable watchlist (strict eviction, `since` kept, a reserve for
probation, a bounded and frozen meta_burst choice) and a trade stream with hysteresis, so the
subscription churn of 26 Oct 2026 (5,298 subscriptions for 238 mints in 52 minutes, probation starved)
cannot come back; the idle expiry no longer empties a stable set every tick; schema v4 closes the
milestone-2 graduation rows."""
from __future__ import annotations

import hashlib
import json
import sqlite3

import pytest

from fart import db
from fart.chain.base58 import b58encode
from fart.engine import decide
from fart.engine.service import Engine
from fart.ingest import store, watch
from fart.ingest.raw import RawWriter
from fart.ingest.rpc_logs import RpcLogsClient
from fart.ingest.rpc_trades import RpcTradesClient
from fart.net.budget import Budget, load_source_configs

T0 = 1_761_480_000.0  # 2025-10-26T12:00:00Z


def pk(seed: str) -> str:
    return b58encode(hashlib.sha256(seed.encode()).digest())


class Clock:
    def __init__(self, t=T0):
        self.t = t

    def __call__(self):
        return self.t


def make_rt(settings, conn, clock, cfg_over=None, wcfg_over=None, cu_cap="3000000"):
    srcs = load_source_configs(settings.config("budget"), {"RPC_DAILY_BUDGET_CU": cu_cap})
    budget = Budget(conn, srcs, settings.lock_path, clock=clock)
    raw = RawWriter(settings.raw_dir, "rpc_trades", flush_interval_s=0)
    cfg = dict(settings.config("ingest")["rpc_trades"], **(cfg_over or {}))
    wcfg = dict(settings.config("engine")["watchlist"], **(wcfg_over or {}))
    return RpcTradesClient(conn, budget, raw, "wss://x", "rpc", cfg, wcfg, settings.config("pumpfun"), connect=lambda *a, **k: None, clock=clock), budget


def add_coin(conn, mint, t, name="x", symbol="X", creator="C", curve=None):
    conn.execute("INSERT INTO coins(mint, name, symbol, creator, bonding_curve, block_time, received_at, dev_buy_sol, source, sources)"
                 " VALUES(?,?,?,?,?,?,?,1.0,'pumpportal','[\"pumpportal\"]')", (mint, name, symbol, creator, curve, t, t + 0.5))


def active(conn):
    return {r[0]: r[1] for r in conn.execute("SELECT mint, reason FROM watchlist WHERE active=1")}


# ----- 1a: strict eviction, `since` never reset ------------------------------------------------------------------
def test_add_evicts_only_strictly_lower_priority_and_never_resets_since(conn):
    assert watch.add(conn, "A", "meta_burst", T0, 86400, 3)
    assert watch.add(conn, "B", "meta_burst", T0 + 1, 86400, 3)
    assert watch.add(conn, "C", "meta_burst", T0 + 2, 86400, 3)
    # equal priority never evicts (the milestone-3 behaviour: D would have replaced A)
    assert watch.add(conn, "D", "meta_burst", T0 + 3, 86400, 3) is False
    assert set(active(conn)) == {"A", "B", "C"}
    # lower priority never evicts either
    assert watch.add(conn, "E", "early_buyers", T0 + 4, 86400, 3) is False
    assert watch.add(conn, "P", "probation", T0 + 4, 300, 3) is False
    # strictly higher priority evicts the lowest-priority, oldest entry
    assert watch.add(conn, "O", "observe", T0 + 5, None, 3)
    assert set(active(conn)) == {"B", "C", "O"}
    assert conn.execute("SELECT active, until FROM watchlist WHERE mint='A'").fetchone()[:] == (0, T0 + 5)
    # a repeated add of an active mint changes neither `since` nor `activated_at`
    row0 = conn.execute("SELECT since, activated_at, until FROM watchlist WHERE mint='B'").fetchone()
    assert watch.add(conn, "B", "meta_burst", T0 + 100, 86400, 3)
    row1 = conn.execute("SELECT since, activated_at, until FROM watchlist WHERE mint='B'").fetchone()
    assert (row1["since"], row1["activated_at"]) == (row0["since"], row0["activated_at"]) == (T0 + 1, T0 + 1)
    assert row1["until"] == T0 + 100 + 86400  # the later expiry wins (an upgrade in place)
    # a reactivation keeps the original `since` (stable ordering) and moves `activated_at`
    conn.execute("UPDATE watchlist SET active=0 WHERE mint='O'")
    assert watch.add(conn, "A", "meta_burst", T0 + 200, 86400, 3)
    row = conn.execute("SELECT since, activated_at, active, reason FROM watchlist WHERE mint='A'").fetchone()
    assert row[:] == (T0, T0 + 200, 1, "meta_burst")
    assert watch.active_mints(conn) == ["A", "B", "C"]  # ordered by the original `since`
    assert watch.active_counts(conn) == {"meta_burst": 3}


# ----- 1b: the probation reserve --------------------------------------------------------------------------------
def test_probation_keeps_its_slots_when_meta_burst_fills_the_list(conn):
    """The VPS watchlist held 141 meta_burst + 35 graduation rows; probation got 17 of 5,298 subscriptions."""
    cap, reserve = 10, 5
    for i in range(30):
        watch.add(conn, f"MB{i:02d}", "meta_burst", T0 + i, 86400, cap, reserve)
    assert sorted(active(conn)) == [f"MB{i:02d}" for i in range(5)]  # the shared slots (cap - reserve), the rest refused
    for i in range(5):
        assert watch.add(conn, f"P{i}", "probation", T0 + 100 + i, 300, cap, reserve), i  # the reserve is probation's
    assert watch.add(conn, "P5", "probation", T0 + 110, 300, cap, reserve) is False  # the list is full now
    assert watch.add(conn, "MB99", "meta_burst", T0 + 111, 86400, cap, reserve) is False  # never into the reserve
    assert len(active(conn)) == 10 and watch.active_counts(conn) == {"meta_burst": 5, "probation": 5}
    # a probation expiry frees a slot for the next new coin
    assert watch.expire(conn, T0 + 100 + 300) == 1  # P0
    assert watch.add(conn, "P5", "probation", T0 + 401, 300, cap, reserve)
    # probation may overflow its reserve into unused shared slots; a higher priority reclaims the oldest overflow
    conn.execute("DELETE FROM watchlist")
    for i in range(3):
        watch.add(conn, f"MB{i}", "meta_burst", T0 + i, 86400, cap, reserve)
    for i in range(7):
        assert watch.add(conn, f"P{i}", "probation", T0 + 10 + i, 300, cap, reserve)
    assert watch.active_counts(conn) == {"meta_burst": 3, "probation": 7}
    assert watch.add(conn, "MB3", "meta_burst", T0 + 20, 86400, cap, reserve)
    assert watch.active_counts(conn) == {"meta_burst": 4, "probation": 6} and "P0" not in active(conn)
    assert watch.limits({"cap": 200, "probation_reserve": 100}) == (200, 100) and watch.limits({"cap": 10, "probation_reserve": 50}) == (10, 10)


# ----- 1d: the reproduction, cap 10 and 30 meta_burst candidates over three ticks -------------------------------
def test_cap_10_with_30_meta_burst_candidates_over_three_ticks_gives_zero_resubscriptions(settings, conn):
    """What the VPS did every engine tick: offer ~840 meta_burst candidates to a list of 200. With equal-priority
    eviction each add displaced the oldest meta_burst row and set `since=now`, the survivors were the last
    candidates of the tick, and because the metas re-rank as trades arrive the candidate order (and so the
    surviving set) differed per tick: sync_messages() unsubscribed and re-subscribed the difference every
    15 s. Here: cap 10, 30 candidates whose order rotates per tick. After the fix the first 10 stay."""
    pf = settings.config("pumpfun")
    mints = [pk(f"cand-{i}") for i in range(30)]
    for i, m in enumerate(mints):
        add_coin(conn, m, T0 - 600 + i, curve=pk(f"curve-{i}"))
    clock = Clock(T0)
    rt, _ = make_rt(settings, conn, clock, cfg_over={"subscribe_after_syncs": 1, "unsubscribe_after_s": 0}, wcfg_over={"rpc_trades_cap": 150})
    sent: list[dict] = []
    for tick in range(3):
        now = T0 + 15 * tick
        order = mints[tick * 7:] + mints[:tick * 7]  # the metas re-ranked: a different order each tick
        for m in order:
            watch.add(conn, m, "meta_burst", now, 86400, 10, 0)
        clock.t = now + 5
        msgs = rt.sync_messages(clock.t)
        sent.append({"tick": tick, "subscribe": sum(m["method"] == "logsSubscribe" for m in msgs),
                     "unsubscribe": sum(m["method"] == "logsUnsubscribe" for m in msgs)})
    assert sent[0] == {"tick": 0, "subscribe": 10, "unsubscribe": 0}
    assert sent[1] == {"tick": 1, "subscribe": 0, "unsubscribe": 0}
    assert sent[2] == {"tick": 2, "subscribe": 0, "unsubscribe": 0}
    assert sorted(active(conn)) == sorted(mints[:10])  # the first ten candidates of tick 0, untouched since
    assert conn.execute("SELECT COUNT(*) FROM ws_subscriptions").fetchone()[0] == 10
    assert conn.execute("SELECT COUNT(*) FROM ws_subscriptions WHERE until IS NOT NULL").fetchone()[0] == 0
    assert rt.policy["subscribed"] == 10 and rt.policy["eligible"] == 10
    assert all(rt.subs[m].address == pk(f"curve-{mints.index(m)}") for m in mints[:10]) and pf["program_id"]


# ----- 1b: the engine's stable, bounded meta_burst choice -------------------------------------------------------
def _members(term):
    return lambda t: {"alpha": ["a1", "a2", "a3", "a4", "a5", "a6"], "beta": ["b1", "b2", "b3"], "gamma": ["g1", "g2", "g3", "g4", "g5", "g6", "g7"]}.get(t, [])


def test_meta_burst_sets_are_frozen_bounded_and_released_after_the_grace_period():
    members = _members(None)
    sets, released = decide.meta_burst_sets({}, ["alpha", "beta"], members, set(), T0, per_meta=5, max_total=7, release_s=600)
    assert sets["alpha"]["mints"] == ["a1", "a2", "a3", "a4", "a5"] and sets["beta"]["mints"] == ["b1", "b2"]  # 7 in all, strongest first
    assert released == []
    # a new coin in a bursting meta never displaces a chosen one (the set is frozen while the meta bursts)
    sets2, released = decide.meta_burst_sets(sets, ["alpha", "beta"], lambda t: ["a0"] + members(t), set(), T0 + 15, 5, 7, 600)
    assert sets2["alpha"]["mints"] == ["a1", "a2", "a3", "a4", "a5"] and released == [] and sets2["alpha"]["chosen_at"] == T0
    # gamma starts bursting while the total is full: it gets nothing until a set is released
    sets3, _ = decide.meta_burst_sets(sets2, ["gamma", "alpha", "beta"], members, set(), T0 + 30, 5, 7, 600)
    assert "gamma" not in sets3
    # beta stops bursting: within the grace period nothing changes (a meta flickering around the threshold)
    sets4, released = decide.meta_burst_sets(sets3, ["alpha"], members, set(), T0 + 45, 5, 7, 600)
    assert sets4["beta"]["mints"] == ["b1", "b2"] and released == []
    # ... after the grace period beta is released and gamma takes the room
    sets5, released = decide.meta_burst_sets(sets4, ["gamma", "alpha"], members, set(), T0 + 45 + 600, 5, 7, 600)
    assert "beta" not in sets5 and released == ["b1", "b2"] and sets5["gamma"]["mints"] == ["g1", "g2"]
    # a graduated member leaves its set and is not replaced; a set that emptied is dropped, and because its meta
    # is bursting still it is chosen afresh from the youngest non-graduated members within the room left
    sets6, released = decide.meta_burst_sets(sets5, ["gamma", "alpha"], members, {"a3", "g1", "g2"}, T0 + 700, 5, 7, 600)
    assert sets6["alpha"]["mints"] == ["a1", "a2", "a4", "a5"] and released == ["a3", "g1", "g2"]
    assert sets6["gamma"]["mints"] == ["g3", "g4", "g5"] and sets6["gamma"]["chosen_at"] == T0 + 700  # 4 + 3 = 7
    # a mint chosen by one meta is not chosen again by another
    sets8, _ = decide.meta_burst_sets({}, ["alpha", "alpha-twin"], lambda t: ["a1", "a2"], set(), T0, 5, 40, 600)
    assert sets8 == {"alpha": {"mints": ["a1", "a2"], "chosen_at": T0, "last_bursting": T0}}


def test_engine_chooses_a_stable_meta_burst_set_and_the_stream_never_resubscribes(settings, conn):
    """Three engine ticks with six bursting metas of six coins (30 candidates of 5 per meta), watchlist cap 10:
    the choice is made once, persisted in kv, unchanged by the next ticks and by a restart; the trade
    stream sends its ten subscriptions once and nothing more."""
    wcfg = settings.configs["engine"]["watchlist"]
    wcfg.update({"cap": 10, "probation_reserve": 0, "meta_burst_per_meta": 5, "meta_burst_max_total": 40, "rpc_trades_cap": 150})
    terms = ["zorblax", "quimbly", "frandor", "plemtok", "vexrim", "dunthar"]  # one token each, no shared word, no bigram
    for ti, term in enumerate(terms):
        for i in range(6):
            add_coin(conn, pk(f"{term}-{i}"), T0 - 3000 + 100 * i + ti, name=term.capitalize(), symbol=term[:3].upper(), creator=f"C{i}")
    clock = Clock(T0)
    rt, _ = make_rt(settings, conn, clock, cfg_over={"subscribe_after_syncs": 1, "unsubscribe_after_s": 0}, wcfg_over=wcfg)
    eng = Engine(conn, settings, rpc=None)
    sent = []
    for tick in range(3):
        s = eng.tick(T0 + 15 * tick)
        assert s["bursting_metas"] == 6 and s["meta_burst_sets"] == 6 and s["meta_burst_mints"] == 30 and s["meta_burst_released"] == 0
        clock.t = T0 + 15 * tick + 5
        msgs = rt.sync_messages(clock.t)
        sent.append((sum(m["method"] == "logsSubscribe" for m in msgs), sum(m["method"] == "logsUnsubscribe" for m in msgs)))
    assert sent == [(10, 0), (0, 0), (0, 0)]
    sets = json.loads(db.kv_get(conn, "engine.meta_burst_sets"))
    assert set(sets) == set(terms) and all(len(v["mints"]) == 5 for v in sets.values())
    # the five youngest of every meta: coin 0 (the oldest) is never chosen
    for term in terms:
        assert set(sets[term]["mints"]) == {pk(f"{term}-{i}") for i in range(1, 6)}
    assert eng.burst_sets == sets and len(active(conn)) == 10 and set(active(conn).values()) == {"meta_burst"}
    watched = set(active(conn))
    # a restart loads the same choice from kv and changes nothing
    eng2 = Engine(conn, settings, rpc=None)
    assert eng2.burst_sets == sets
    s = eng2.tick(T0 + 60)
    assert s["meta_burst_released"] == 0 and set(active(conn)) == watched
    clock.t = T0 + 65
    assert rt.sync_messages(clock.t) == []
    # a meta that stops bursting (its coins drop out of the window) is released after the grace period
    conn.execute("DELETE FROM coins WHERE name = 'Dunthar'")
    s = eng2.tick(T0 + 75)
    assert "dunthar" in eng2.burst_sets and s["meta_burst_released"] == 0  # grace
    s = eng2.tick(T0 + 75 + 601)
    assert "dunthar" not in eng2.burst_sets and s["meta_burst_released"] == 5 and len(active(conn)) <= 10
    assert not {pk(f"dunthar-{i}") for i in range(6)} & set(active(conn))
    assert "dunthar" not in json.loads(db.kv_get(conn, "engine.meta_burst_sets"))


# ----- 1c: hysteresis in sync_messages() and the close reasons ----------------------------------------------------
def test_stream_hysteresis_absorbs_a_watchlist_flicker_and_records_close_reasons(settings, conn):
    pf = settings.config("pumpfun")
    m1, m2, m3 = pk("m1"), pk("m2"), pk("m3")
    for m in (m1, m2, m3):
        add_coin(conn, m, T0 - 100, curve=pk("curve-" + m[:4]))
    watch.add(conn, m1, "meta_burst", T0 - 50, 86400, 200)
    clock = Clock(T0)
    rt, budget = make_rt(settings, conn, clock)  # the repository values: subscribe after 2 syncs, unsubscribe after 60 s
    assert rt.cfg["subscribe_after_syncs"] == 2 and rt.cfg["unsubscribe_after_s"] == 60
    assert rt.sync_messages(T0) == []  # desired once: not yet
    msgs = rt.sync_messages(T0 + 5)
    assert [m["method"] for m in msgs] == ["logsSubscribe"] and m1 in rt.subs
    rid = msgs[0]["id"]
    assert rt.handle_text(json.dumps({"jsonrpc": "2.0", "result": 11, "id": rid}), T0 + 6) == "subscribed"
    # a flicker: the row is gone for one engine tick and comes back -> nothing is sent
    conn.execute("UPDATE watchlist SET active=0 WHERE mint=?", (m1,))
    assert rt.sync_messages(T0 + 10) == [] and rt.sync_messages(T0 + 15) == [] and rt.stats()["pending_unsubscribe"] == 1
    assert rt.undesired_reasons == {m1: "unwatched"}
    watch.add(conn, m1, "meta_burst", T0 + 20, 86400, 200)  # the engine re-adds it (reactivation, `since` kept)
    assert rt.sync_messages(T0 + 20) == [] and rt.stats()["pending_unsubscribe"] == 0 and m1 in rt.subs
    # a mint desired in only one sync is never subscribed
    watch.add(conn, m2, "probation", T0 + 25, 300, 200)
    assert rt.sync_messages(T0 + 25) == []
    conn.execute("UPDATE watchlist SET active=0 WHERE mint=?", (m2,))
    assert rt.sync_messages(T0 + 30) == [] and m2 not in rt.subs and m2 not in rt._desired_streak
    # gone for real: unsubscribed once the 60 s are over, the row closed with the reason
    conn.execute("UPDATE watchlist SET active=0 WHERE mint=?", (m1,))
    for t in (35, 40, 60, 90):
        assert rt.sync_messages(T0 + t) == [], t
    msgs = rt.sync_messages(T0 + 95)
    assert [m["method"] for m in msgs] == ["logsUnsubscribe"] and msgs[0]["params"] == [11] and rt.subs == {}
    assert conn.execute("SELECT close_reason FROM ws_subscriptions WHERE mint=?", (m1,)).fetchone()[0] == "unwatched"
    assert rt.counts["closed_unwatched"] == 1
    # the other close reasons: graduation, the envelope, the per-subscription cap, the CU policy, the stream cap
    def subscribe(mint, reason, t):
        watch.add(conn, mint, reason, t, 86400, 200)
        rt.sync_messages(t)
        msgs = rt.sync_messages(t + 5)
        assert [m["method"] for m in msgs] == ["logsSubscribe"], (mint, msgs)
        rt.handle_text(json.dumps({"jsonrpc": "2.0", "result": 20 + len(rt.by_sub), "id": msgs[0]["id"]}), t + 6)

    subscribe(m1, "meta_burst", T0 + 200)
    conn.execute("INSERT INTO migrations(mint, received_at, source, sources) VALUES(?, ?, 'rpc_logs', '[]')", (m1, T0 + 210))
    rt.sync_messages(T0 + 210)
    assert rt.undesired_reasons == {m1: "graduated"}
    assert [m["method"] for m in rt.sync_messages(T0 + 271)] == ["logsUnsubscribe"]
    assert conn.execute("SELECT close_reason FROM ws_subscriptions WHERE mint=? ORDER BY id DESC LIMIT 1", (m1,)).fetchone()[0] == "graduated"
    subscribe(m2, "early_buyers", T0 + 300)
    rt._pending_bytes["ws_trade_notification"] = 10 ** 9  # the envelope: paused for the rest of the hour
    rt.sync_messages(T0 + 310)
    assert rt.policy["envelope_paused"] is True and rt.undesired_reasons == {m2: "envelope"}
    assert [m["method"] for m in rt.sync_messages(T0 + 371)] == ["logsUnsubscribe"]
    assert conn.execute("SELECT close_reason FROM ws_subscriptions WHERE mint=? ORDER BY id DESC LIMIT 1", (m2,)).fetchone()[0] == "envelope"
    rt._pending_bytes["ws_trade_notification"] = 0
    clock.t = T0 + 3600  # the next UTC hour: the envelope is empty again; m2 is still undesired (unwatched by now)
    conn.execute("UPDATE watchlist SET active=0 WHERE mint=?", (m2,))
    subscribe(m3, "probation", T0 + 3600)
    budget.record("rpc", "getTransaction", 0.85 * 3_000_000, True, 200, 1.0)  # 85% of the daily cap: probation is dropped
    rt.sync_messages(T0 + 3610)
    assert rt.policy["dropping_low_priority"] is True and rt.undesired_reasons == {m3: "cu_policy"}
    conn.execute("DELETE FROM api_ledger")
    watch.add(conn, m3, "meta_burst", T0 + 3620, 86400, 200)  # promoted: desired again before the 60 s passed
    assert rt.sync_messages(T0 + 3620) == [] and m3 in rt.subs and rt.stats()["pending_unsubscribe"] == 0
    rt.watch_cfg["rpc_trades_cap"] = 0
    rt.sync_messages(T0 + 3630)
    assert rt.undesired_reasons == {m3: "stream_cap"}
    assert pf["program_id"]


def test_reconnect_resubscribes_at_once_but_a_fresh_start_waits_one_sync(settings, conn):
    m1 = pk("m1")
    add_coin(conn, m1, T0 - 100, curve=pk("curve-m1"))
    watch.add(conn, m1, "meta_burst", T0 - 50, 86400, 200)
    rt, _ = make_rt(settings, conn, Clock())
    assert rt.sync_messages(T0) == [] and [m["method"] for m in rt.sync_messages(T0 + 5)] == ["logsSubscribe"]
    # the session drops: the rows are closed with `disconnect`, the streak survives, the reconnect subscribes at once
    rt._reset_subscriptions(T0 + 10, "disconnect")
    assert conn.execute("SELECT close_reason FROM ws_subscriptions").fetchone()[0] == "disconnect" and rt.subs == {}
    assert [m["method"] for m in rt.sync_messages(T0 + 11)] == ["logsSubscribe"]
    rt._reset_subscriptions(T0 + 20, "shutdown")
    assert [r[0] for r in conn.execute("SELECT close_reason FROM ws_subscriptions ORDER BY id")] == ["disconnect", "shutdown"]


# ----- 4: the idle expiry and the stable set; graduation rows -------------------------------------------------------
def test_idle_expiry_counts_from_the_last_activation_so_a_stable_set_is_not_emptied_every_tick(conn):
    """A coin that comes back after a stream pause (no trades for hours) used to be expired at once when
    `since` was kept; the idle window now runs from `activated_at`."""
    assert watch.add(conn, "A", "meta_burst", T0 - 4 * 3600, 86400, 200)
    assert watch.expire(conn, T0, dead_after_idle_s=7200) == 1  # 4 h without a trade since activation: idle
    assert watch.add(conn, "A", "meta_burst", T0, 86400, 200)   # the engine re-adds it (its meta still bursts)
    row = conn.execute("SELECT since, activated_at FROM watchlist WHERE mint='A'").fetchone()
    assert row[:] == (T0 - 4 * 3600, T0)
    for t in (15, 30, 3600, 7199):
        assert watch.expire(conn, T0 + t, dead_after_idle_s=7200) == 0, t  # a full idle window again, not every tick
    assert watch.is_watched(conn, "A")
    assert watch.expire(conn, T0 + 7200, dead_after_idle_s=7200) == 1
    # a trade keeps it alive; probation never expires by idleness, only by its TTL
    watch.add(conn, "A", "meta_burst", T0 + 7300, 86400, 200)
    conn.execute("UPDATE watchlist SET last_trade_at=? WHERE mint='A'", (T0 + 10000,))
    assert watch.expire(conn, T0 + 10000 + 7199, dead_after_idle_s=7200) == 0
    watch.add(conn, "P", "probation", T0 + 10000, 300, 200)
    assert watch.expire(conn, T0 + 10000 + 299, dead_after_idle_s=7200) == 0
    assert watch.expire(conn, T0 + 10000 + 300, dead_after_idle_s=7200) == 1 and not watch.is_watched(conn, "P") and watch.is_watched(conn, "A")
    # a row from before milestone 4 (activated_at NULL) falls back to `since`
    conn.execute("INSERT INTO watchlist(mint, since, reason, active) VALUES('OLD', ?, 'meta_burst', 1)", (T0,))
    assert watch.expire(conn, T0 + 7200, dead_after_idle_s=7200) >= 1 and not watch.is_watched(conn, "OLD")


def _v3_database(path, now_rows):
    c = sqlite3.connect(str(path), isolation_level=None)
    c.row_factory = sqlite3.Row
    c.executescript(f"BEGIN;\n{db.MIGRATIONS[1]}\nPRAGMA user_version=1;\nCOMMIT;")
    for v in (2, 3):
        c.execute("BEGIN IMMEDIATE")
        db.MIGRATIONS[v](c)
        c.execute(f"PRAGMA user_version={v}")
        c.execute("COMMIT")
    for mint, reason, since in now_rows:
        c.execute("INSERT INTO watchlist(mint, since, reason, until, active) VALUES(?, ?, ?, ?, 1)", (mint, since, reason, since + 86400))
        c.execute("INSERT INTO ws_subscriptions(mint, address, kind, reason, since) VALUES(?, ?, 'curve', ?, ?)", (mint, mint, reason, since))
    c.execute("INSERT INTO coins(mint, received_at, source, sources) VALUES('K', 1, 'pumpportal', '[]')")
    c.execute("INSERT INTO migrations(mint, received_at, source, sources) VALUES('K', 2, 'rpc_logs', '[]'), ('U', 3, 'rpc_logs', '[]')")
    c.close()


def test_schema_v4_closes_the_graduation_rows_and_flags_migrations_without_a_create(tmp_path):
    """The VPS still had 35 active `graduation` rows (TTL 24 h) from milestone 2."""
    path = tmp_path / "v3.db"
    _v3_database(path, [(f"G{i}", "graduation", T0 + i) for i in range(35)] + [("E1", "early_buyers", T0), ("M1", "meta_burst", T0)])
    conn = db.connect(path)
    try:
        assert db.schema_version(conn) == db.SCHEMA_VERSION  # v4 and then v5 (milestone 5) in one go
        assert conn.execute("SELECT COUNT(*) FROM watchlist WHERE active=1 AND reason='graduation'").fetchone()[0] == 0
        assert {r[0] for r in conn.execute("SELECT mint FROM watchlist WHERE active=1")} == {"E1", "M1"}
        assert conn.execute("SELECT COUNT(*) FROM ws_subscriptions WHERE until IS NULL").fetchone()[0] == 2
        assert conn.execute("SELECT COUNT(*) FROM ws_subscriptions WHERE close_reason='schema_v4'").fetchone()[0] == 35
        assert {r[0]: r[1] for r in conn.execute("SELECT mint, coin_unknown FROM migrations")} == {"K": 0, "U": 1}
        assert conn.execute("SELECT COUNT(*) FROM watchlist WHERE activated_at IS NULL").fetchone()[0] == 0
        rec = json.loads(db.kv_get(conn, "schema.v4.cleanup"))
        assert rec["counts"] == {"watchlist_graduation_closed": 35, "ws_subscriptions_closed": 35, "migrations_coin_unknown": 1, "watchlist_activated_at_filled": 37}
        assert db.migrate(conn) == db.SCHEMA_VERSION
        assert db._cleanup_v4(conn, T0) == {"watchlist_graduation_closed": 0, "ws_subscriptions_closed": 0, "migrations_coin_unknown": 0, "watchlist_activated_at_filled": 0}
    finally:
        conn.close()


def test_rpc_logs_never_adds_a_graduation_row_while_subscribe_graduations_is_false(settings, conn, fixtures_dir):
    pp_msgs = json.loads((fixtures_dir / "pumpportal_messages.json").read_text())
    store.upsert_coin(conn, store.pumpportal_create_record(pp_msgs["new_token"]), "pumpportal", T0 - 100)
    srcs = load_source_configs(settings.config("budget"), {"RPC_DAILY_BUDGET_CU": "100000"})
    budget = Budget(conn, srcs, settings.lock_path, clock=Clock())
    wcfg = settings.config("engine")["watchlist"]
    assert wcfg["subscribe_graduations"] is False
    rl = RpcLogsClient(conn, budget, RawWriter(settings.raw_dir, "rpc_logs", flush_interval_s=0), "wss://x", "rpc", settings.config("ingest")["rpc_logs"],
                       settings.config("pumpfun"), wcfg, connect=lambda *a, **k: None, clock=Clock())
    migr = (fixtures_dir / "logs_notification_migration.json").read_text()
    assert rl.handle_text(migr, T0) == "migrate"
    assert conn.execute("SELECT coin_unknown FROM migrations").fetchone()[0] == 0 and conn.execute("SELECT COUNT(*) FROM watchlist").fetchone()[0] == 0
    # only a funded PumpPortal key (subscribe_graduations true) follows a graduated coin, within the shared slots
    rl2 = RpcLogsClient(conn, budget, RawWriter(settings.raw_dir, "rpc_logs", flush_interval_s=0), "wss://x", "rpc", settings.config("ingest")["rpc_logs"],
                        settings.config("pumpfun"), dict(wcfg, subscribe_graduations=True), connect=lambda *a, **k: None, clock=Clock())
    assert rl2.handle_text(migr, T0 + 1) == "migrate"
    assert conn.execute("SELECT reason FROM watchlist").fetchone()[0] == "graduation"
