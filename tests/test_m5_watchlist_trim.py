"""Milestone 5, point 5: the M4 leftover. After the M4 upgrade (VPS, 26 Oct 2026 12:18 UTC) 195 `meta_burst`
rows written by milestone 3 kept their watchlist slots (the M4 rules only limit new additions) and probation got
5 slots while `probation_reserve` is 100 and `meta_burst_max_total` 40. The engine now trims every tick and
schema v5 trims once at the upgrade."""
from __future__ import annotations

import json
import sqlite3

from fart import db
from fart.engine.service import Engine
from fart.ingest import watch

T0 = 1_761_480_000.0  # 2025-10-26T12:00:00Z (synthetic anchor)


def _old_meta_burst_rows(conn, n, since=T0 - 3600, ttl=86400):
    """`n` meta_burst rows as milestone 3 wrote them: active, 24 h TTL, no frozen set that knows them."""
    for i in range(n):
        conn.execute("INSERT INTO watchlist(mint, since, reason, until, active, activated_at) VALUES(?,?,?,?,1,?)",
                     (f"MB{i:03d}", since + i, "meta_burst", since + i + ttl, since + i))


def _counts(conn):
    return watch.active_counts(conn)


def test_trim_closes_the_oldest_rows_above_the_bound_but_never_frozen_or_recently_traded(conn):
    _old_meta_burst_rows(conn, 50)
    conn.execute("UPDATE watchlist SET last_trade_at=? WHERE mint='MB000'", (T0 - 60,))     # traded 1 min ago: stays
    conn.execute("UPDATE watchlist SET last_trade_at=? WHERE mint='MB001'", (T0 - 900,))    # traded 15 min ago: may go
    assert watch.trim(conn, "meta_burst", 40, keep=["MB002", "MB003"], now=T0) == 10
    act = set(watch.active_mints(conn))
    assert len(act) == 40 and {"MB000", "MB002", "MB003"} <= act
    assert "MB001" not in act and not any(f"MB{i:03d}" in act for i in range(4, 12))  # the oldest eligible ones went
    assert conn.execute("SELECT until FROM watchlist WHERE mint='MB001'").fetchone()[0] == T0
    # a second run changes nothing; a list within the bound is never touched; other reasons are not counted
    assert watch.trim(conn, "meta_burst", 40, [], T0 + 1) == 0
    watch.add(conn, "E1", "early_buyers", T0, 86400, 200, 100)
    assert watch.trim(conn, "meta_burst", 40, [], T0 + 2) == 0 and watch.is_watched(conn, "E1")
    assert watch.trim(conn, "meta_burst", 0, ["MB000"], T0 + 3) == 39  # everything but the kept one


def test_195_old_meta_burst_rows_plus_reserve_100_give_at_most_40_after_one_tick_and_probation_gets_room(conn, settings):
    """The VPS state after the M4 upgrade: 195 meta_burst rows out of a cap of 200, probation reserve 100."""
    wcfg = settings.config("engine")["watchlist"]
    assert (wcfg["cap"], wcfg["probation_reserve"], wcfg["meta_burst_max_total"]) == (200, 100, 40)
    _old_meta_burst_rows(conn, 195)
    for i in range(5):
        watch.add(conn, f"P{i}", "probation", T0 - 100 + i, 300, *watch.limits(wcfg))
    assert _counts(conn) == {"meta_burst": 195, "probation": 5}
    # a new coin finds no slot: this is what starved probation on the VPS
    assert watch.add(conn, "NEW0", "probation", T0 - 1, 300, *watch.limits(wcfg)) is False
    eng = Engine(conn, settings, rpc=None)
    s = eng.tick(T0)
    assert s["meta_burst_trimmed"] == 155 and s["expired"] == 0
    c = _counts(conn)
    assert c["meta_burst"] <= 40 and c["probation"] == 5
    # probation has room again: the reserve of 100 is usable
    for i in range(100):
        assert watch.add(conn, f"NEW{i}", "probation", T0 + 1 + i, 300, *watch.limits(wcfg)), i
    assert _counts(conn)["probation"] == 105 and _counts(conn)["meta_burst"] <= 40
    # the closed rows are closed with `until` = the tick time, the kept ones are the youngest
    kept = {r[0] for r in conn.execute("SELECT mint FROM watchlist WHERE active=1 AND reason='meta_burst'")}
    assert kept == {f"MB{i:03d}" for i in range(155, 195)}
    assert conn.execute("SELECT COUNT(*) FROM watchlist WHERE active=0 AND reason='meta_burst' AND until=?", (T0,)).fetchone()[0] == 155
    # the next tick trims nothing more and the engine wrote its rules fingerprint next to its rules_version
    s2 = eng.tick(T0 + 15)
    assert s2["meta_burst_trimmed"] == 0
    assert db.kv_get(conn, "engine.rules_fingerprint.test-rules-v1", "").startswith("sha256:")


def test_engine_trim_keeps_the_frozen_sets_and_recently_traded_rows(conn, settings):
    wcfg = settings.config("engine")["watchlist"]
    _old_meta_burst_rows(conn, 60)
    conn.execute("UPDATE watchlist SET last_trade_at=? WHERE mint IN ('MB000', 'MB001')", (T0 - 30,))
    frozen = {"zorblax": {"mints": ["MB002", "MB003", "MB004"], "chosen_at": T0 - 100, "last_bursting": T0 - 10}}
    db.kv_set(conn, "engine.meta_burst_sets", json.dumps(frozen), T0 - 10)
    eng = Engine(conn, settings, rpc=None)
    assert eng.burst_sets == frozen
    s = eng.tick(T0)
    assert s["meta_burst_trimmed"] == 20
    act = set(r[0] for r in conn.execute("SELECT mint FROM watchlist WHERE active=1 AND reason='meta_burst'"))
    assert len(act) == 40 and {"MB000", "MB001", "MB002", "MB003", "MB004"} <= act
    assert wcfg["meta_burst_max_total"] == 40


def _v4_database(path, n_meta_burst, frozen=None, traded=()):
    c = sqlite3.connect(str(path), isolation_level=None)
    c.row_factory = sqlite3.Row
    c.executescript(f"BEGIN;\n{db.MIGRATIONS[1]}\nPRAGMA user_version=1;\nCOMMIT;")
    for v in (2, 3, 4):
        c.execute("BEGIN IMMEDIATE")
        db.MIGRATIONS[v](c)
        c.execute(f"PRAGMA user_version={v}")
        c.execute("COMMIT")
    _old_meta_burst_rows(c, n_meta_burst)
    for m in traded:
        c.execute("UPDATE watchlist SET last_trade_at=? WHERE mint=?", (__import__("time").time() - 30, m))
    for i in range(5):
        c.execute("INSERT INTO watchlist(mint, since, reason, until, active, activated_at) VALUES(?,?,?,?,1,?)",
                  (f"P{i}", T0, "probation", T0 + 300, T0))
    if frozen is not None:
        c.execute("INSERT INTO kv(key, value, updated_at) VALUES('engine.meta_burst_sets', ?, 1)", (json.dumps(frozen),))
    c.close()


def test_schema_v5_trims_the_meta_burst_rows_once_and_is_idempotent(tmp_path):
    path = tmp_path / "v4.db"
    frozen = {"quimbly": {"mints": ["MB000", "MB001"], "chosen_at": 1, "last_bursting": 1}}
    _v4_database(path, 195, frozen=frozen, traded=["MB002"])
    conn = db.connect(path)
    try:
        assert db.schema_version(conn) == 5 == db.SCHEMA_VERSION
        assert watch.active_counts(conn) == {"meta_burst": 40, "probation": 5}
        act = {r[0] for r in conn.execute("SELECT mint FROM watchlist WHERE active=1 AND reason='meta_burst'")}
        assert {"MB000", "MB001", "MB002"} <= act  # frozen set and the recently traded row kept
        rec = json.loads(db.kv_get(conn, "schema.v5.cleanup"))
        assert rec["counts"] == {"meta_burst_active_before": 195, "meta_burst_closed": 155, "meta_burst_active_after": 40,
                                 "frozen_mints_kept": 2, "max_total": 40}
        # a second run changes nothing
        assert db.migrate(conn) == 5
        assert db._cleanup_v5(conn, T0)["meta_burst_closed"] == 0
        assert watch.active_counts(conn) == {"meta_burst": 40, "probation": 5}
    finally:
        conn.close()


def test_schema_v5_bound_matches_the_engine_config(settings):
    assert db.SCHEMA_V5_META_BURST_MAX_TOTAL == settings.config("engine")["watchlist"]["meta_burst_max_total"]


def test_schema_v5_on_a_fresh_or_small_database_changes_nothing(conn):
    _old_meta_burst_rows(conn, 10)
    assert db._cleanup_v5(conn, T0) == {"meta_burst_active_before": 10, "meta_burst_closed": 0, "meta_burst_active_after": 10,
                                        "frozen_mints_kept": 0, "max_total": 40}
    assert db.kv_get(conn, "schema.v5.cleanup") is not None  # written by connect()
