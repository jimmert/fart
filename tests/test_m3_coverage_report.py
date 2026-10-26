"""Milestone 3, item 4: hours before the first start and periods without a running source are not
covered; the daily report says NIET GEDEKT with empty percentages; the enrich queue serves migrations
first within a CU-based daily limit that the report shows."""
from __future__ import annotations

import json

import pytest

from fart import db
from fart.ingest import coverage
from fart.ingest.sweep import enrich_backlog, enrich_daily_limit, process_enrich_queue
from fart.measure.report import build_report, render_markdown
from fart.util import day_bounds, day_key, hour_bounds

T0 = 1_761_480_000.0  # 2025-10-26T12:00:00Z


def _coin(conn, mint, t, sources=("pumpportal",)):
    conn.execute("INSERT INTO coins(mint, received_at, block_time, source, sources) VALUES(?,?,?,?,?)", (mint, t, t - 1, sources[0], json.dumps(list(sources))))


def test_hours_before_the_first_start_are_not_covered(conn):
    day_start, day_end = day_bounds("2025-10-26")
    first = day_start + 4 * 3600 + 53 * 60  # the VPS: first run at 04:53 UTC
    assert coverage.source_active_since(conn, "pumpportal") is None
    # before milestone 3 only `started` existed (overwritten at every start) plus the startup gap rows
    db.kv_set(conn, "ingest.pumpportal.started", str(first + 7200), first + 7200)  # a later restart
    g = coverage.gap_open(conn, "pumpportal", first, "startup")
    coverage.gap_close(conn, g, first + 3)
    db.kv_set(conn, "ingest.rpc_logs.first_started", str(first), first)
    g = coverage.gap_open(conn, "rpc_logs", first, "startup")
    coverage.gap_close(conn, g, first + 5)
    assert coverage.source_active_since(conn, "pumpportal") == first  # the earliest gap row wins over the overwritten key
    assert coverage.source_active_since(conn, "rpc_logs") == first
    now = day_end + 10
    h03 = coverage.compute_hour(conn, "2025-10-26T03", now)
    assert h03["covered"] is False and h03["complete"] is False and h03["covered_by"] == []
    h04 = coverage.compute_hour(conn, "2025-10-26T04", now)  # started at 04:53: covered for 7 minutes, never complete
    assert h04["covered"] is True and h04["complete"] is False
    h05 = coverage.compute_hour(conn, "2025-10-26T05", now)
    assert h05["covered"] is True and h05["complete"] is True
    # a current hour: covered (the source is running), not complete (not over)
    h_now = coverage.compute_hour(conn, "2025-10-27T00", day_end + 10)
    assert h_now["covered"] is True and h_now["complete"] is False
    # an unrecovered gap spanning the whole hour for every source -> not covered; a partial gap -> covered, not complete
    s, e = hour_bounds("2025-10-26T10")
    for src in ("pumpportal", "rpc_logs"):
        gid = coverage.gap_open(conn, src, s - 60, "disconnect")
        coverage.gap_close(conn, gid, e + 60)
    h10 = coverage.compute_hour(conn, "2025-10-26T10", now)
    assert h10["covered"] is False and h10["complete"] is False
    s, e = hour_bounds("2025-10-26T11")
    for src in ("pumpportal", "rpc_logs"):
        gid = coverage.gap_open(conn, src, s + 600, "disconnect")
        coverage.gap_close(conn, gid, s + 1200)
    h11 = coverage.compute_hour(conn, "2025-10-26T11", now)
    assert h11["covered"] is True and h11["complete"] is False
    # write_hour stores the covered flag
    row = coverage.write_hour(conn, "2025-10-26T03", now)
    assert row["covered"] is False and conn.execute("SELECT covered, complete FROM coverage WHERE hour='2025-10-26T03'").fetchone()[:] == (0, 0)


def test_downtime_gap_from_a_stale_heartbeat(conn):
    db.kv_set(conn, "ingest.pumpportal.first_started", str(T0 - 7200), T0 - 7200)
    db.kv_set(conn, "ingest.rpc_logs.first_started", str(T0 - 7200), T0 - 7200)
    db.kv_set(conn, "ingest.rpc_sigs.first_started", str(T0 - 7200), T0 - 7200)
    # a crash at T0-3600 (last heartbeat), restart now: no shutdown gap was written
    assert coverage.record_downtime(conn, T0 - 3600, T0 - 3600 + 30) == 0  # 30 s is a normal restart, not downtime
    assert coverage.record_downtime(conn, T0 - 3600, T0) == 3  # pumpportal, rpc_logs, rpc_sigs; rpc_trades never ran
    rows = conn.execute("SELECT source, start, end, reason FROM coverage_gaps ORDER BY source").fetchall()
    assert [r["source"] for r in rows] == ["pumpportal", "rpc_logs", "rpc_sigs"]
    assert all(r["reason"] == "down" and r["start"] == T0 - 3600 and r["end"] == T0 for r in rows)
    # a source with an open (shutdown) gap gets no second gap
    coverage.gap_open(conn, "pumpportal", T0 + 100, "shutdown")
    assert coverage.record_downtime(conn, T0 + 100, T0 + 10000) == 2  # rpc_logs and rpc_sigs only
    assert coverage.record_downtime(conn, None, T0) == 0
    # the crash happened at 11:00 sharp: hour 11 was down from start to end -> not covered; hour 10 was fine
    h = coverage.compute_hour(conn, "2025-10-26T11", T0 + 20000)
    assert h["covered"] is False and h["complete"] is False
    h = coverage.compute_hour(conn, "2025-10-26T10", T0 + 20000)
    assert h["covered"] is True and h["complete"] is True
    # a `down` gap is recovered by a complete sweep like a restart gap
    assert coverage.mark_recovered_by_sweep(conn, sweep_started_at=T0 + 1, now=T0 + 5) >= 2


def _report_world(conn, settings, first_start):
    """A day with one graduation and sources that started at `first_start`."""
    day_start, _ = day_bounds("2025-10-26")
    for src in ("pumpportal", "rpc_logs"):
        db.kv_set(conn, f"ingest.{src}.first_started", str(first_start), first_start)
        g = coverage.gap_open(conn, src, first_start, "startup")
        coverage.gap_close(conn, g, first_start + 2)
    _coin(conn, "M", day_start + 3600)
    conn.execute("INSERT INTO migrations(mint, block_time, received_at, source, sources) VALUES('M', ?, ?, 'rpc_logs', '[]')", (day_start + 7200, day_start + 7201))


def test_report_says_not_covered_for_a_day_before_the_first_start(settings, conn):
    """The VPS report of 25 Oct 2026 said COMPLEET, 24/24 while fart first ran on 26 Oct 04:53 UTC."""
    day_start, _ = day_bounds("2025-10-26")
    _report_world(conn, settings, day_start + 86400 + 4 * 3600 + 53 * 60)  # first start the NEXT day at 04:53
    rep = build_report(conn, settings, "2025-10-26", day_start + 3 * 86400)
    assert rep["coverage_status"] == "not_covered" and rep["covered"] is False and rep["complete"] is False
    assert rep["coverage"]["hours_covered"] == 0 and rep["coverage"]["hours_complete"] == 0
    assert rep["coverage"]["first_source_start"] == "2025-10-27T04:53:00Z"
    assert rep["unknowns"]["hours_not_covered"] == 24
    md = render_markdown(rep)
    assert "**NIET GEDEKT**" in md and "Geen enkel uur van deze dag was gedekt" in md and "2025-10-27T04:53:00Z" in md
    assert "COMPLEET" not in md.replace("NIET COMPLEET", "").replace("NIET GEDEKT", "")
    # the percentages stay empty (no 0%, no 100%): recall, precision, win rate
    assert "%" not in md.split("## Samenvatting")[1].split("## Dekking")[0]
    assert "| 2025-10-26T03 |" in md and "| nee | nee |" in md
    assert "universum NIET GEDEKT" in md
    # the next day, with the sources running from 04:53: covered, not complete, percentages present
    _coin(conn, "N", day_start + 86400 + 6 * 3600)
    rep2 = build_report(conn, settings, "2025-10-27", day_start + 3 * 86400)
    assert rep2["coverage_status"] == "incomplete" and rep2["coverage"]["hours_covered"] == 20 and rep2["coverage"]["hours_complete"] == 19
    md2 = render_markdown(rep2)
    assert "**NIET COMPLEET**" in md2 and "Gedekte uren: 20/24 · complete uren: 19/24" in md2
    assert "| 2025-10-27T04 | 0 | 0 | 4 | ja | nee |" in md2 and "| 2025-10-27T03 | 0 | 0 | 0 | nee | nee |" in md2  # 2 startup gaps of 2 s


def test_report_shows_enrich_limit_and_failures(settings, conn):
    day_start, _ = day_bounds("2025-10-26")
    _report_world(conn, settings, day_start - 86400)
    day = "2025-10-26"
    db.kv_set(conn, f"enrich.tx.{day}", "1500", T0)
    db.kv_set(conn, f"enrich.limit.{day}", "1500", T0)
    db.counter_bump(conn, f"enrich.skipped_budget.{day}", 64, T0)
    db.counter_bump(conn, f"engine.tick_failed.{day}", 159, T0)
    db.counter_bump(conn, f"ingest.handle_failed.rpc_logs.{day}", 2, T0)
    db.counter_bump(conn, f"rpc_trades.subscription_capped.{day}", 1, T0)
    conn.execute("INSERT INTO enrich_queue(key, kind, requested_at) VALUES('s1', 'create_tx', 1), ('s2', 'migrate_tx', 1), ('s3', 'create_tx', 1)")
    conn.execute("INSERT INTO rejected_messages(received_at, source, kind, reason, mint) VALUES(?, 'pumpportal', 'migrate', 'system_mint:USDC', 'x')", (T0,))
    rep = build_report(conn, settings, day, day_start + 2 * 86400)
    ops = rep["ops"]
    assert ops["enrich"] == {"used": 1500, "limit": 1500, "skipped_budget": 64, "backlog": {"migrate_tx": 1, "create_tx": 2}}
    assert ops["engine_tick_failed"] == 159 and ops["handle_failed"] == {"pumpportal": 0, "rpc_logs": 2, "rpc_trades": 0}
    assert ops["rejected_messages"] == {"system_mint:USDC": 1} and ops["rpc_trades"] == {"subscription_capped": 1, "envelope_paused": 0}
    md = render_markdown(rep)
    assert "## Verrijking en fouten" in md and "1500 van 1500 vandaag gebruikt, 64 keer overgeslagen" in md
    assert "wachtrij nu: 1 migraties, 2 creates" in md and "Mislukte engine-ticks: 159" in md and "rpc_logs 2" in md
    assert "system_mint:USDC 1" in md and "1 abonnementen afgekapt" in md
    # the digest carries the status label
    from fart.notify import format as fmt

    d = fmt.digest(day, dict(rep, coverage_status="not_covered", complete=False), None, None, 0, None, "f")
    assert "niet gedekt (fart draaide niet)" in d
    d2 = fmt.digest(day, rep, None, None, 0, None, "f")
    assert "niet compleet" in d2 and "/24 gedekt" in d2


class FakeRpc:
    def __init__(self, cap=None, cost=59.0):
        self.calls = []
        self.source = "rpc"
        self._cap, self._cost = cap, cost

        class _SC:
            daily_cost = cap

            def cost_of(self_inner, method):
                return cost

        class _B:
            def source(self_inner, name):
                return _SC()

        self.budget = _B()

    def available(self, method="*"):
        return True

    def get_transaction(self, sig):
        self.calls.append(sig)
        return None  # no transaction: counts as fetched, stored as empty


def test_enrich_limit_is_cu_based_and_migrations_go_first(settings, conn):
    cfg = dict(settings.config("ingest")["backfill"], **settings.config("ingest")["enrich"])
    assert cfg["max_cu_fraction_per_day"] == 0.15 and cfg["max_transactions_per_day"] == 1500
    assert enrich_daily_limit(cfg, 3_000_000, 59) == int(0.15 * 3_000_000 / 59) == 7627
    assert enrich_daily_limit(cfg, None, 59) == 1500  # public RPC: the fixed fallback
    assert enrich_daily_limit(dict(cfg, max_cu_fraction_per_day=None), 3_000_000, 59) == 1500
    for i, kind in enumerate(("create_tx", "create_tx", "migrate_tx", "create_tx", "migrate_tx")):
        conn.execute("INSERT INTO enrich_queue(key, kind, requested_at) VALUES(?, ?, ?)", (f"s{i}", kind, 100 + i))
    assert enrich_backlog(conn) == {"migrate_tx": 2, "create_tx": 3}
    rpc = FakeRpc(cap=59 * 3 / 0.15)  # a cap that allows exactly 3 transactions a day
    stats = process_enrich_queue(conn, rpc, cfg, now=T0)
    assert stats["limit"] == 3 and stats["fetched"] == 3 and stats["skipped_budget"] == 2
    assert rpc.calls == ["s2", "s4", "s0"]  # both migrations first (oldest first), then the oldest create
    assert db.kv_get(conn, f"enrich.tx.{day_key(T0)}") == "3" and db.kv_get(conn, f"enrich.limit.{day_key(T0)}") == "3"
    assert db.kv_get(conn, f"enrich.skipped_budget.{day_key(T0)}") == "2"
    assert enrich_backlog(conn) == {"migrate_tx": 0, "create_tx": 2}
    stats2 = process_enrich_queue(conn, rpc, cfg, now=T0 + 60)
    assert stats2["fetched"] == 0 and stats2["skipped_budget"] == 2 and db.kv_get(conn, f"enrich.skipped_budget.{day_key(T0)}") == "4"
