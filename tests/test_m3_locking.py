"""Milestone 3, item 1: SQLite write transactions.

Two connections on one WAL database reproduce the VPS failure of 26 Oct 2026 (a plain BEGIN that reads
first and writes after another connection committed fails at once with SQLITE_BUSY_SNAPSHOT, without
consulting busy_timeout) and show that the milestone-3 helpers (reads outside, one short BEGIN IMMEDIATE,
retries with jitter) succeed under the same interleaving. Plus: the failure counters and the engine tick
under a concurrent writer."""
from __future__ import annotations

import json
import sqlite3
import threading
import time

import pytest

from fart import db
from fart.engine.service import Engine
from fart.ingest import watch

T0 = 1_761_480_000.0


def test_connection_defaults(settings, conn):
    assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 15000 == db.BUSY_TIMEOUT_MS
    ro = db.connect_ro(settings.db_path)
    assert ro.execute("PRAGMA busy_timeout").fetchone()[0] == 15000
    ro.close()


def test_plain_begin_snapshot_conflict_versus_immediate(settings, conn):
    """The exact VPS pattern: BEGIN, read, another connection commits, then the first write -> locked."""
    a = conn
    b = db.connect(settings.db_path)
    try:
        a.execute("INSERT INTO watchlist(mint, since, reason, active) VALUES('M', 1, 'probation', 1)")
        # --- the milestone-2 pattern ---------------------------------------------------------------
        a.execute("BEGIN")
        assert a.execute("SELECT COUNT(*) FROM watchlist").fetchone()[0] == 1  # the snapshot starts here
        b.execute("INSERT INTO kv(key, value, updated_at) VALUES('ingest.heartbeat', '1', 1)")  # another process commits
        t0 = time.monotonic()
        with pytest.raises(sqlite3.OperationalError, match="locked"):
            a.execute("UPDATE watchlist SET active=0 WHERE mint='M'")
        # SQLITE_BUSY_SNAPSHOT returns at once: busy_timeout (15 s) was never consulted
        assert time.monotonic() - t0 < 2.0
        a.execute("ROLLBACK")
        assert a.execute("SELECT active FROM watchlist WHERE mint='M'").fetchone()[0] == 1
        # --- the milestone-3 pattern: read in a closed snapshot, write in a short IMMEDIATE transaction ---
        with db.read_tx(a):
            n = a.execute("SELECT COUNT(*) FROM watchlist").fetchone()[0]
        b.execute("INSERT INTO kv(key, value, updated_at) VALUES('ingest.heartbeat2', '1', 1)")  # commits in between again
        db.write_tx(a, lambda c: c.execute("UPDATE watchlist SET active=0 WHERE mint='M'"), what="test")
        assert n == 1 and a.execute("SELECT active FROM watchlist WHERE mint='M'").fetchone()[0] == 0
        # and the other way around: b's IMMEDIATE write while a holds a read snapshot is fine (WAL)
        with db.read_tx(a):
            a.execute("SELECT COUNT(*) FROM kv").fetchone()
            db.write_tx(b, lambda c: c.execute("INSERT INTO kv(key, value, updated_at) VALUES('k3', '1', 1)"))
        assert a.execute("SELECT COUNT(*) FROM kv WHERE key='k3'").fetchone()[0] == 1
    finally:
        b.close()


def test_write_tx_rolls_back_and_only_retries_busy_errors(settings, conn, monkeypatch):
    calls = {"n": 0}

    def flaky(c):
        calls["n"] += 1
        c.execute("INSERT INTO kv(key, value, updated_at) VALUES('flaky', ?, 1)", (str(calls["n"]),))
        if calls["n"] < 3:
            raise sqlite3.OperationalError("database is locked")
        return calls["n"]

    monkeypatch.setattr(db, "WRITE_RETRY_BASE_S", 0.001)
    assert db.write_tx(conn, flaky, what="flaky") == 3
    assert conn.execute("SELECT value FROM kv WHERE key='flaky'").fetchone()[0] == "3"  # the failed attempts were rolled back
    # after the last attempt the busy error propagates
    with pytest.raises(sqlite3.OperationalError, match="locked"):
        db.write_tx(conn, lambda c: (_ for _ in ()).throw(sqlite3.OperationalError("database is locked")), attempts=2)
    # a non-busy error is never retried
    calls["n"] = 0

    def broken(c):
        calls["n"] += 1
        raise sqlite3.IntegrityError("boom")

    with pytest.raises(sqlite3.IntegrityError):
        db.write_tx(conn, broken)
    assert calls["n"] == 1
    assert conn.in_transaction is False
    assert db.is_busy_error(sqlite3.OperationalError("database is locked")) and not db.is_busy_error(sqlite3.OperationalError("no such table"))


def test_write_tx_waits_out_a_real_writer(settings, conn, monkeypatch):
    """Another connection holds the write lock longer than our busy_timeout: attempt 1 fails, the retry succeeds."""
    holder = db.connect(settings.db_path)
    conn.execute("PRAGMA busy_timeout=20")
    monkeypatch.setattr(db, "WRITE_RETRY_BASE_S", 0.25)
    held = threading.Event()
    release = threading.Event()

    def hold():
        holder.execute("BEGIN IMMEDIATE")
        holder.execute("INSERT INTO kv(key, value, updated_at) VALUES('held', '1', 1)")
        held.set()
        release.wait(5.0)
        holder.execute("COMMIT")

    t = threading.Thread(target=hold)
    t.start()
    held.wait(5.0)
    timer = threading.Timer(0.1, release.set)
    timer.start()
    try:
        db.write_tx(conn, lambda c: c.execute("INSERT INTO kv(key, value, updated_at) VALUES('mine', '1', 1)"), what="contended")
    finally:
        release.set()
        t.join(5.0)
        holder.close()
    assert {r[0] for r in conn.execute("SELECT key FROM kv WHERE key IN ('held', 'mine')")} == {"held", "mine"}


def test_record_failure_counts_all_time_and_per_day(conn):
    assert db.record_failure(conn, "engine.tick_failed", T0, detail="OperationalError: database is locked") is True
    assert db.record_failure(conn, "engine.tick_failed", T0 + 1) is True
    kv = {r[0]: r[1] for r in conn.execute("SELECT key, value FROM kv WHERE key LIKE 'engine.tick_failed%'")}
    assert kv["engine.tick_failed"] == "2" and kv["engine.tick_failed.2025-10-26"] == "2"
    assert kv["engine.tick_failed.last_at"] == str(T0 + 1) and "locked" in kv["engine.tick_failed.last"]
    db.counter_bump_daily(conn, "x.y", 3, T0)
    assert db.counters(conn, ("x.",)) == {"x.y": "3", "x.y.2025-10-26": "3"}


def add_coin(conn, mint, t, name, creator="C"):
    conn.execute("INSERT INTO coins(mint, name, symbol, creator, block_time, received_at, dev_buy_sol, source, sources)"
                 " VALUES(?,?,?,?,?,?,1.0,'pumpportal','[]')", (mint, name, name[:4].upper(), creator, t, t + 0.5))


def test_engine_tick_survives_a_concurrent_writer(settings, conn):
    """The ingest daemon commits continuously while the engine computes features and metas; every tick
    must still commit its decisions, promotions and heartbeat (the milestone-2 tick failed at watch.add)."""
    for i in range(3):
        add_coin(conn, f"G{i}", T0 - 100 * (i + 1), f"Goldberg {i}", creator=f"C{i}")
    add_coin(conn, "M", T0, "Goldberg One")
    watch.add(conn, "M", "probation", T0, 300, 200)
    for i in range(30):
        conn.execute("INSERT INTO trades(mint, block_time, received_at, signature, buyer, is_buy, sol, tokens, virtual_sol, virtual_token, source)"
                     " VALUES('M', ?, ?, ?, ?, 1, 0.5, 500000, ?, 1e15, 'pumpportal')", (T0 + 1 + i, T0 + 1.5 + i, f"b{i}", f"B{i}", (31 + i * 0.5) * 1e9))
    stop = threading.Event()
    writes = {"n": 0}

    def writer():
        c = db.connect(settings.db_path)
        i = 0
        while not stop.is_set():
            i += 1
            db.write_tx(c, lambda x, i=i: (x.execute("INSERT INTO kv(key, value, updated_at) VALUES(?, '1', 1) ON CONFLICT(key) DO UPDATE SET value=value",
                                                      (f"w{i}",)),
                                           x.execute("INSERT OR IGNORE INTO coins(mint, name, received_at, source, sources) VALUES(?, 'Goldberg w', ?, 'rpc_logs', '[]')",
                                                     (f"W{i}", T0 + 40 + i * 0.001))))
            writes["n"] += 1
            time.sleep(0.0005)
        c.close()

    t = threading.Thread(target=writer)
    t.start()
    try:
        eng = Engine(conn, settings, rpc=None)
        s1 = eng.tick(T0 + 40)
        s2 = eng.tick(T0 + 55)
        s3 = eng.tick(T0 + 70)
    finally:
        stop.set()
        t.join(10.0)
    assert writes["n"] > 0
    assert s1["coins"] == 1 and s1["promoted"] == 1 and s1["coin_decisions"] == 1 and s1["metas"] >= 1
    assert s2["coin_decisions"] == 0 and s3["coin_decisions"] == 0  # cooldown
    assert conn.execute("SELECT value FROM kv WHERE key='engine.heartbeat'").fetchone()[0] == str(T0 + 70)
    # promoted to early_buyers, then upgraded to meta_burst by the bursting 'goldberg' cluster (higher priority)
    assert conn.execute("SELECT reason FROM watchlist WHERE mint='M'").fetchone()[0] in ("early_buyers", "meta_burst")
    assert conn.execute("SELECT COUNT(*) FROM watchlist WHERE reason='meta_burst'").fetchone()[0] >= 3
    assert conn.execute("SELECT COUNT(*) FROM decisions").fetchone()[0] >= 1
    assert "engine.tick_failed" not in {r[0] for r in conn.execute("SELECT key FROM kv")}
    assert conn.in_transaction is False
