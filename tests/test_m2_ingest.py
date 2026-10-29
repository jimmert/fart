"""Milestone 2 ingest changes: PumpPortal key mode, restart gaps + sweep recovery, schema v1 -> v2."""
import json
import sqlite3

import pytest

from fart import db
from fart.ingest import coverage
from fart.ingest.pumpportal import PumpPortalClient, http_status_of
from fart.ingest.raw import RawWriter
from fart.ingest.sweep import sweep_signatures
from fart.net.budget import Budget, load_source_configs
from fart.util import hour_bounds

T0 = 1_761_480_000.0


class Clock:
    def __init__(self, t=T0):
        self.t = t

    def __call__(self):
        return self.t


def make_pp(settings, conn, trades_enabled):
    srcs = load_source_configs(settings.config("budget"), {"RPC_DAILY_BUDGET_CU": "100000"})
    budget = Budget(conn, srcs, settings.lock_path, clock=Clock())
    raw = RawWriter(settings.raw_dir, "pumpportal", flush_interval_s=0)
    return PumpPortalClient(conn, budget, raw, "wss://x", settings.config("ingest")["pumpportal"], settings.config("engine")["watchlist"],
                            connect=lambda *a, **k: None, clock=Clock(), trades_enabled=trades_enabled)


@pytest.fixture
def pp_msgs(fixtures_dir):
    return json.loads((fixtures_dir / "pumpportal_messages.json").read_text())


def test_pumpportal_without_key_sends_no_trade_subscriptions(settings, conn, pp_msgs):
    pp = make_pp(settings, conn, trades_enabled=False)
    assert pp.handle_text(json.dumps(pp_msgs["new_token"]), 1.0) == "create"  # probation entry exists now
    assert conn.execute("SELECT COUNT(*) FROM watchlist WHERE active=1").fetchone()[0] == 1
    assert pp.subscription_messages(now=2.0) == [] and pp.subscribed == set()
    assert pp.stats()["trades_enabled"] is False
    # a trade message still normalizes when it arrives (e.g. after a key is added later)
    assert pp.handle_text(json.dumps(pp_msgs["trade"]), 3.0) == "trade"
    pp2 = make_pp(settings, conn, trades_enabled=True)
    assert pp2.subscription_messages(now=4.0) == [{"method": "subscribeTokenTrade", "keys": [pp_msgs["new_token"]["mint"]]}]


def test_pumpportal_refusal_message_and_http_status(settings, conn):
    pp = make_pp(settings, conn, trades_enabled=False)
    msg = {"message": "subscribeTokenTrade and subscribeAccountTrade are only available when connecting with an API key funded with at least some balance."}
    assert pp.handle_text(json.dumps(msg), 1.0) == "info"
    assert pp.handle_text(json.dumps(msg), 2.0) == "info"
    assert conn.execute("SELECT value FROM kv WHERE key='pp.trade_sub_refused'").fetchone()[0] == "2"
    assert conn.execute("SELECT value FROM kv WHERE key='ingest.pumpportal.trade_sub_refused_at'").fetchone()[0] == "2.0"
    assert pp.handle_text(json.dumps({"message": "Successfully subscribed to token creation events."}), 3.0) == "info"
    assert conn.execute("SELECT value FROM kv WHERE key='pp.trade_sub_refused'").fetchone()[0] == "2"

    class WithCode(Exception):
        status_code = 403

    class Resp:
        status_code = 429

    class WithResponse(Exception):
        response = Resp()

    assert http_status_of(WithCode("x")) == 403
    assert http_status_of(WithResponse("x")) == 429
    assert http_status_of(Exception("server rejected WebSocket connection: HTTP 403")) == 403
    assert http_status_of(Exception("boom")) is None


def test_settings_key_mode_and_flags(tmp_path, monkeypatch):
    from fart.config import load_settings

    for k in ("PUMPPORTAL_API_KEY", "X_BEARER_TOKEN", "TELEGRAM_BOT_TOKEN", "TELEGRAM_OWNER_CHAT_ID", "TELEGRAM_NOTIFY_ENABLED", "X_DAILY_BUDGET_USD"):
        monkeypatch.delenv(k, raising=False)
    s = load_settings(root=tmp_path)
    assert s.pumpportal_trades_enabled is False and s.pumpportal_connect_url == s.pumpportal_wss_url
    assert s.x_configured is False and s.telegram_configured is False and s.telegram_enabled is False and s.x_daily_budget_usd == 3.0
    (tmp_path / ".env").write_text(
        "PUMPPORTAL_API_KEY=PLACEHOLDER-PP-KEY\nX_BEARER_TOKEN=PLACEHOLDER-X-TOKEN\nX_DAILY_BUDGET_USD=1.5\n"
        "TELEGRAM_BOT_TOKEN=123:PLACEHOLDER-TG\nTELEGRAM_OWNER_CHAT_ID=42\n"
    )
    s = load_settings(root=tmp_path)
    assert s.pumpportal_trades_enabled is True and s.pumpportal_connect_url.endswith("?api-key=PLACEHOLDER-PP-KEY")
    assert s.x_configured and s.telegram_configured and s.telegram_enabled is False and s.x_daily_budget_usd == 1.5
    desc = json.dumps(s.describe())
    for secret in ("PLACEHOLDER-PP-KEY", "PLACEHOLDER-X-TOKEN", "PLACEHOLDER-TG"):
        assert secret not in desc
    assert s.describe()["pumpportal_api_key"] == "configured" and s.describe()["telegram"] == "configured"
    (tmp_path / ".env").write_text("TELEGRAM_BOT_TOKEN=1:x\nTELEGRAM_OWNER_CHAT_ID=42\nTELEGRAM_NOTIFY_ENABLED=1\n")
    assert load_settings(root=tmp_path).telegram_enabled is True


def test_restart_gap_relabel_and_sweep_recovery(conn):
    hour = "2025-10-26T12"
    start, end = hour_bounds(hour)
    conn.execute("INSERT INTO kv(key, value, updated_at) VALUES('ingest.pumpportal.started','1',0)")
    conn.execute("INSERT INTO kv(key, value, updated_at) VALUES('ingest.rpc_logs.started','1',0)")
    coverage.gap_open(conn, "pumpportal", start + 100, "shutdown")   # the previous process stopped here
    coverage.gap_open(conn, "rpc_logs", start + 100, "shutdown")
    assert coverage.close_dangling_gaps(conn, start + 400, relabel="restart") == 2
    rows = conn.execute("SELECT reason, end FROM coverage_gaps ORDER BY id").fetchall()
    assert all(r["reason"] == "restart" and r["end"] == start + 400 for r in rows)
    cap = coverage.gap_open(conn, "rpc_logs", start + 1000, "budget_cap")
    coverage.gap_close(conn, cap, start + 1300)
    # not complete while unrecovered
    assert coverage.compute_hour(conn, hour, now=end + 1)["complete"] is False
    # a complete sweep that started after the restart gaps recovers them, never the budget-cap gap
    assert coverage.mark_recovered_by_sweep(conn, sweep_started_at=start + 500, now=start + 505) == 2
    assert coverage.mark_recovered_by_sweep(conn, sweep_started_at=start + 2000, now=start + 2005) == 0
    row = coverage.compute_hour(conn, hour, now=end + 1)
    assert [g["recovered"] for g in row["gaps"]] == [True, True, False]
    assert row["complete"] is True  # pumpportal has no unrecovered gap
    # a later unrecovered disconnect on pumpportal too -> the hour is not complete
    g = coverage.gap_open(conn, "pumpportal", start + 2000, "disconnect")
    coverage.gap_close(conn, g, start + 2100)
    assert coverage.compute_hour(conn, hour, now=end + 1)["complete"] is False


class ErrRpc:
    def get_signatures_for_address(self, *a, **k):
        from fart.chain.rpc import RpcError

        raise RpcError(-32000, "down")


class EmptyRpc:
    def get_signatures_for_address(self, *a, **k):
        return []


def test_sweep_opens_rpc_sigs_gap_on_error_and_recovers_on_success(settings, conn):
    pf = settings.config("pumpfun")
    addrs = {"create": pf["mint_authority"], "migrate": pf["migration_account"]}
    cfg = settings.config("ingest")["backfill"]
    g = coverage.gap_open(conn, "pumpportal", T0 - 600, "restart")
    coverage.gap_close(conn, g, T0 - 500)
    out = sweep_signatures(conn, ErrRpc(), addrs, cfg, now=T0)
    assert out["gap"] == "sweep_error" and "error" in out["create"]
    assert coverage.open_gap_id(conn, "rpc_sigs") is not None
    # still failing: no second gap
    sweep_signatures(conn, ErrRpc(), addrs, cfg, now=T0 + 300)
    assert conn.execute("SELECT COUNT(*) FROM coverage_gaps WHERE source='rpc_sigs'").fetchone()[0] == 1
    # first successful run: closes the sweep gap but is a first run (no last signature) -> no recovery yet
    out = sweep_signatures(conn, EmptyRpc(), addrs, cfg, now=T0 + 600)
    assert coverage.open_gap_id(conn, "rpc_sigs") is None and "recovered_gaps" not in out
    db.kv_set(conn, "ingest.sweep.create.last_sig", "sigA", T0 + 600)
    db.kv_set(conn, "ingest.sweep.migrate.last_sig", "sigB", T0 + 600)
    out = sweep_signatures(conn, EmptyRpc(), addrs, cfg, now=T0 + 900)
    assert out["recovered_gaps"] == 1
    assert conn.execute("SELECT recovered_by FROM coverage_gaps WHERE source='pumpportal'").fetchone()[0] == "sweep"


def test_migration_v1_to_v2_preserves_milestone1_data(tmp_path):
    path = tmp_path / "old.db"
    c = sqlite3.connect(str(path), isolation_level=None)
    c.executescript(f"BEGIN;\n{db.MIGRATIONS[1]}\nPRAGMA user_version=1;\nCOMMIT;")
    c.execute("INSERT INTO coins(mint, received_at, source, sources) VALUES('M1', 1, 'pumpportal', '[\"pumpportal\"]')")
    c.execute("INSERT INTO migrations(mint, received_at, source, sources) VALUES('M1', 2, 'rpc_logs', '[]')")
    c.execute("INSERT INTO trades(mint, received_at, signature, is_buy, sol, tokens, source) VALUES('M1', 3, 's', 1, 1, 1, 'pumpportal')")
    c.execute("INSERT INTO decisions(as_of, created_at, kind, mint, features_json, rules_version, verdict, reasons_json) VALUES(1,1,'observe','M1','{}','r','observe','[]')")
    c.execute("INSERT INTO outcomes(decision_id, subject, mint, anchor_time, horizon_s, measured_at, method_version) VALUES(1,'decision','M1',1,3600,2,'m1.0')")
    c.execute("INSERT INTO coverage(hour, creates_union, complete) VALUES('2025-10-26T12', 5, 1)")
    c.execute("INSERT INTO coverage_gaps(start, end, source, reason) VALUES(1, 2, 'pumpportal', 'disconnect')")
    c.execute("INSERT INTO raw_undecoded(received_at, source, discriminator, reason) VALUES(1, 'rpc_logs', 'b1310cd2a076a774', 'unknown_discriminator')")
    assert c.execute("PRAGMA user_version").fetchone()[0] == 1
    c.close()
    conn = db.connect(path)
    try:
        assert db.schema_version(conn) == db.SCHEMA_VERSION >= 8  # v1 -> v2 -> ... -> the current version in one go
        counts = db.table_counts(conn, ("coins", "migrations", "trades", "decisions", "outcomes", "coverage", "coverage_gaps", "raw_undecoded"))
        assert all(v == 1 for v in counts.values()), counts
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        for t in ("social_posts", "social_post_metrics", "social_counts", "x_spend", "x_dedupe", "outbox", "ws_subscriptions", "rejected_messages"):
            assert t in tables
        cols = {r[1] for r in conn.execute("PRAGMA table_info(coverage_gaps)")}
        assert {"recovered_at", "recovered_by"} <= cols
        assert "program" in {r[1] for r in conn.execute("PRAGMA table_info(raw_undecoded)")}
        assert "sub_id" in {r[1] for r in conn.execute("PRAGMA table_info(ws_subscriptions)")}
        assert "covered" in {r[1] for r in conn.execute("PRAGMA table_info(coverage)")}
        assert "activated_at" in {r[1] for r in conn.execute("PRAGMA table_info(watchlist)")}  # milestone 4
        assert "coin_unknown" in {r[1] for r in conn.execute("PRAGMA table_info(migrations)")}
        assert "close_reason" in {r[1] for r in conn.execute("PRAGMA table_info(ws_subscriptions)")}
        assert "bytes" in {r[1] for r in conn.execute("PRAGMA table_info(api_ledger)")}
        assert "unit" in {r[1] for r in conn.execute("PRAGMA table_info(x_spend)")} and "x_keys" in tables  # milestone 7
        assert conn.execute("SELECT coin_unknown FROM migrations WHERE mint='M1'").fetchone()[0] == 0  # M1 has a create
        assert conn.execute("SELECT recovered_at FROM coverage_gaps").fetchone()[0] is None
        assert db.migrate(conn) == db.SCHEMA_VERSION  # idempotent
        # the append-only triggers survived
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("DELETE FROM decisions")
    finally:
        conn.close()
