import sqlite3

import pytest

from fart import db


def test_schema_created_and_versioned(conn):
    assert db.schema_version(conn) == db.SCHEMA_VERSION
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    for t in ("coins", "migrations", "trades", "watchlist", "coverage", "coverage_gaps", "features_asof",
              "decisions", "outcomes", "paper", "api_ledger", "health", "kv", "rate_buckets", "mcap_samples"):
        assert t in tables
    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"


def test_migrate_is_idempotent(conn):
    assert db.migrate(conn) == db.SCHEMA_VERSION
    assert db.migrate(conn) == db.SCHEMA_VERSION


def test_decisions_and_outcomes_are_append_only(conn):
    conn.execute(
        "INSERT INTO decisions(as_of, created_at, kind, mint, features_json, rules_version, verdict, reasons_json)"
        " VALUES(1, 1, 'observe', 'M', '{}', 'r1', 'observe', '[]')"
    )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE decisions SET verdict='x'")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("DELETE FROM decisions")
    conn.execute(
        "INSERT INTO outcomes(decision_id, subject, mint, anchor_time, horizon_s, measured_at, method_version)"
        " VALUES(1, 'decision', 'M', 1, 3600, 2, 'm1.0')"
    )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE outcomes SET hit_100k=1")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("DELETE FROM outcomes")
    # recomputation = a new row, never an update
    conn.execute(
        "INSERT INTO outcomes(decision_id, subject, mint, anchor_time, horizon_s, measured_at, method_version)"
        " VALUES(1, 'decision', 'M', 1, 3600, 3, 'm1.1')"
    )
    assert conn.execute("SELECT COUNT(*) FROM outcomes").fetchone()[0] == 2


def test_readonly_connection(settings, conn):
    ro = db.connect_ro(settings.db_path)
    assert ro.execute("SELECT COUNT(*) FROM coins").fetchone()[0] == 0
    with pytest.raises(sqlite3.OperationalError):
        ro.execute("INSERT INTO kv(key, value, updated_at) VALUES('a', 'b', 1)")
    ro.close()


def test_kv_roundtrip(conn):
    assert db.kv_get(conn, "x") is None
    db.kv_set(conn, "x", "1", 10.0)
    db.kv_set(conn, "x", "2", 11.0)
    assert db.kv_get(conn, "x") == "2"


def test_settings_never_expose_secret_values(tmp_path, monkeypatch):
    from fart.config import load_settings

    (tmp_path / ".env").write_text("RPC_HTTP_URL=https://solana-mainnet.g.alchemy.com/v2/SECRETKEY123\nRPC_DAILY_BUDGET_CU=1234\n")
    monkeypatch.delenv("RPC_HTTP_URL", raising=False)
    monkeypatch.delenv("RPC_DAILY_BUDGET_CU", raising=False)
    s = load_settings(root=tmp_path)
    assert s.rpc_daily_budget_cu == 1234
    assert s.rpc_is_public is False
    desc = s.describe()
    assert "SECRETKEY123" not in str(desc)
    assert desc["rpc_http"] == "https://solana-mainnet.g.alchemy.com"


def test_settings_public_fallback_without_key(settings):
    assert settings.rpc_is_public is True
    assert settings.rpc_http_url.startswith("https://api.mainnet-beta.solana.com")
