"""Milestone 3, items 1 and 2a: system mints never become migrations or watchlist entries, refused
messages are kept, pump-amm swaps map through the pool, the schema-v3 cleanup removes the bogus rows,
handler failures are counted and the raw hour files can be replayed."""
from __future__ import annotations

import json
import sqlite3
import threading

import pytest

from fart import db
from fart.ingest import store, watch
from fart.ingest.pumpportal import PumpPortalClient
from fart.ingest.raw import RawWriter
from fart.ingest.rpc_logs import RpcLogsClient
from fart.net.budget import Budget, load_source_configs

T0 = 1_761_480_000.0
USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
PUMP = "pumpCmXqMfrsAkQ5r49WcJnRayYRqmXz6ae8H7H9Dfn"


class Clock:
    def __init__(self, t=T0):
        self.t = t

    def __call__(self):
        return self.t


def make_pp(settings, conn, clock=None):
    clock = clock or Clock()
    srcs = load_source_configs(settings.config("budget"), {"RPC_DAILY_BUDGET_CU": "100000"})
    budget = Budget(conn, srcs, settings.lock_path, clock=clock)
    raw = RawWriter(settings.raw_dir, "pumpportal", flush_interval_s=0)
    return PumpPortalClient(conn, budget, raw, "wss://x", settings.config("ingest")["pumpportal"], settings.config("engine")["watchlist"],
                            connect=lambda *a, **k: None, clock=clock, trades_enabled=False, pumpfun_cfg=settings.config("pumpfun")), raw


def make_rl(settings, conn, clock=None):
    clock = clock or Clock()
    srcs = load_source_configs(settings.config("budget"), {"RPC_DAILY_BUDGET_CU": "100000"})
    budget = Budget(conn, srcs, settings.lock_path, clock=clock)
    raw = RawWriter(settings.raw_dir, "rpc_logs", flush_interval_s=0)
    return RpcLogsClient(conn, budget, raw, "wss://x", "rpc", settings.config("ingest")["rpc_logs"], settings.config("pumpfun"),
                         settings.config("engine")["watchlist"], connect=lambda *a, **k: None, clock=clock), raw


@pytest.fixture
def pp_msgs(fixtures_dir):
    return json.loads((fixtures_dir / "pumpportal_messages.json").read_text())


def test_system_mint_list_is_configured_and_matches_the_cleanup(settings):
    pf = settings.config("pumpfun")
    mints = store.system_mints(pf)
    assert mints["USDC"] == USDC and mints["PUMP"] == PUMP and mints["WSOL"] == pf["wsol_mint"] and "USDT" in mints
    assert set(mints.values()) == set(db.SCHEMA_V3_SYSTEM_MINTS)  # the migration hard-codes the same addresses
    assert store.system_mint_label(USDC, pf) == "USDC" and store.system_mint_label("SomeMint", pf) is None
    assert store.system_mints({}) == store.DEFAULT_SYSTEM_MINTS  # fallback without config


def test_pumpportal_migration_of_a_system_mint_or_unknown_coin_is_refused(settings, conn, pp_msgs):
    pp, raw = make_pp(settings, conn)
    # the VPS case of 26 Oct 2026: a migration message whose `mint` field carries USDC
    bad = dict(pp_msgs["migration"], mint=USDC, signature="sig-usdc")
    assert pp.handle_text(json.dumps(bad), T0) == "migrate"
    bad2 = dict(pp_msgs["migration"], mint=PUMP, signature="sig-pump")
    assert pp.handle_text(json.dumps(bad2), T0 + 1) == "migrate"
    # a migration of a coin we never saw created is not accepted from PumpPortal either
    unknown = dict(pp_msgs["migration"], mint="UnknownMintxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx", signature="sig-unk")
    assert pp.handle_text(json.dumps(unknown), T0 + 2) == "migrate"
    assert conn.execute("SELECT COUNT(*) FROM migrations").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM watchlist").fetchone()[0] == 0
    rows = conn.execute("SELECT * FROM rejected_messages ORDER BY id").fetchall()
    assert [r["reason"] for r in rows] == ["system_mint:USDC", "system_mint:PUMP", "unknown_coin"]
    assert rows[0]["mint"] == USDC and rows[0]["source"] == "pumpportal" and rows[0]["kind"] == "migrate"
    assert json.loads(rows[0]["raw_json"])["signature"] == "sig-usdc"  # the raw message is kept for inspection
    assert rows[0]["raw_ref"].startswith("pumpportal/")
    assert pp.counts["migrate_rejected"] == 3
    kv = {r[0]: r[1] for r in conn.execute("SELECT key, value FROM kv WHERE key LIKE 'ingest.rejected.%'")}
    assert kv == {"ingest.rejected.pumpportal.system_mint:USDC": "1", "ingest.rejected.pumpportal.system_mint:PUMP": "1",
                  "ingest.rejected.pumpportal.unknown_coin": "1"}
    # a known coin's migration is accepted (and stays off the watchlist: subscribe_graduations is false)
    assert pp.handle_text(json.dumps(pp_msgs["new_token"]), T0 + 3) == "create"
    assert pp.handle_text(json.dumps(pp_msgs["migration"]), T0 + 4) == "migrate"
    assert conn.execute("SELECT mint FROM migrations").fetchone()[0] == pp_msgs["new_token"]["mint"]
    assert conn.execute("SELECT reason FROM watchlist WHERE mint=?", (pp_msgs["new_token"]["mint"],)).fetchone()[0] == "probation"
    # a create of a system mint is ignored too
    assert pp.handle_text(json.dumps(dict(pp_msgs["new_token"], mint=USDC, signature="s2")), T0 + 5) == "create"
    assert conn.execute("SELECT COUNT(*) FROM coins WHERE mint=?", (USDC,)).fetchone()[0] == 0
    assert store.coin_known(conn, USDC) is False


def test_rpc_logs_migration_of_unknown_coin_is_stored_but_not_watched(settings, conn, fixtures_dir):
    rl, raw = make_rl(settings, conn)
    migr = (fixtures_dir / "logs_notification_migration.json").read_text()
    assert rl.handle_text(migr, T0) == "migrate"  # chain-decoded migration of a coin we never saw created
    assert conn.execute("SELECT COUNT(*) FROM migrations").fetchone()[0] == 1 and conn.execute("SELECT COUNT(*) FROM watchlist").fetchone()[0] == 0
    # a chain event naming a system mint is refused anyway (never a coin)
    from fart.chain.events import event_discriminator
    from fart.chain.base58 import b58decode
    import base64, struct

    payload = event_discriminator("CompletePumpAmmMigrationEvent") + b58decode("4fV6XUxhxxpPBv2JYohq278jfZGLCze7mUPVGNGYzfaG") + b58decode(USDC)
    payload += struct.pack("<QQQ", 1, 1, 1) + b58decode("DtWfLZLaGFM1QwoBj9oJGa2YT5Whm3WeFwkuN99sjjx1") + struct.pack("<q", int(T0)) + b58decode("6JWo6VtZqN5hs6qsPEjk23VN5YEz8SL76rkuwYabjNyq")
    note = json.loads(migr)
    note["params"]["result"]["value"]["logs"] = ["Program 6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P invoke [1]", "Program data: " + base64.b64encode(payload).decode(),
                                                 "Program 6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P success"]
    note["params"]["result"]["value"]["signature"] = "sig-usdc-chain"
    assert rl.handle_text(json.dumps(note), T0 + 1) == "migrate"
    assert conn.execute("SELECT COUNT(*) FROM migrations WHERE mint=?", (USDC,)).fetchone()[0] == 0
    assert conn.execute("SELECT reason, source FROM rejected_messages").fetchone()[:] == ("system_mint:USDC", "rpc_logs")
    assert rl.counts["migrate_rejected"] == 1


def _v2_database(path, mints_rows):
    c = sqlite3.connect(str(path), isolation_level=None)
    c.executescript(f"BEGIN;\n{db.MIGRATIONS[1]}\nPRAGMA user_version=1;\nCOMMIT;")
    c.execute("BEGIN IMMEDIATE")
    db._migrate_v2(c)
    c.execute("PRAGMA user_version=2")
    c.execute("COMMIT")
    for mint, n in mints_rows:
        c.execute("INSERT INTO migrations(mint, received_at, source, sources, dex) VALUES(?, 1, 'pumpportal', '[]', 'pump-amm')", (mint,))
        c.execute("INSERT INTO watchlist(mint, since, reason, active) VALUES(?, 1, 'graduation', 1)", (mint,))
        c.execute("INSERT INTO ws_subscriptions(mint, address, kind, reason, since) VALUES(?, ?, 'mint', 'graduation', 1)", (mint, mint))
        c.execute("INSERT INTO mcap_samples(mint, ts, src) VALUES(?, 1, 'dexscreener')", (mint,))
        for i in range(n):
            c.execute("INSERT INTO trades(mint, received_at, signature, is_buy, sol, tokens, source) VALUES(?, 1, ?, 1, 1, 1, 'pump_amm')", (mint, f"{mint[:4]}-{i}"))
    c.close()


def test_schema_v3_cleanup_removes_the_bogus_rows_once(tmp_path):
    path = tmp_path / "v2.db"
    _v2_database(path, [(USDC, 2000), (PUMP, 729), ("RealCoin111111111111111111111111111111111111", 3)])
    conn = db.connect(path)
    try:
        assert db.schema_version(conn) == 3
        assert conn.execute("SELECT COUNT(*) FROM trades").fetchone()[0] == 3
        assert conn.execute("SELECT mint FROM trades LIMIT 1").fetchone()[0].startswith("RealCoin")
        assert conn.execute("SELECT COUNT(*) FROM migrations").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM watchlist WHERE active=1").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM ws_subscriptions WHERE until IS NULL").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM mcap_samples").fetchone()[0] == 1
        rec = json.loads(db.kv_get(conn, "schema.v3.cleanup"))
        assert rec["deleted"] == {"trades": 2729, "migrations": 2, "mcap_samples": 2, "coins": 0, "watchlist": 2, "ws_subscriptions": 2}
        assert set(rec["mints"]) == set(db.SCHEMA_V3_SYSTEM_MINTS)
        # idempotent: running the cleanup again deletes nothing
        assert db._cleanup_system_mints(conn, db.SCHEMA_V3_SYSTEM_MINTS, 2.0) == {"trades": 0, "migrations": 0, "mcap_samples": 0, "coins": 0, "watchlist": 0, "ws_subscriptions": 0}
        assert db.migrate(conn) == 3
    finally:
        conn.close()


def test_handler_failure_is_counted_and_the_raw_line_can_be_replayed(settings, conn, pp_msgs, monkeypatch):
    """A handler that cannot get the write lock (another writer holds it longer than busy_timeout and the
    retries) raises, is counted in kv once the lock is free, and the raw hour file replays its data."""
    clock = Clock()
    pp, raw = make_pp(settings, conn, clock)
    conn.execute("PRAGMA busy_timeout=10")
    monkeypatch.setattr(db, "WRITE_RETRY_BASE_S", 0.001)
    holder = db.connect(settings.db_path)
    holder.execute("BEGIN IMMEDIATE")
    holder.execute("INSERT INTO kv(key, value, updated_at) VALUES('held', '1', 1)")
    try:
        with pytest.raises(sqlite3.OperationalError, match="locked"):
            pp.handle_text(json.dumps(pp_msgs["new_token"]), T0)
    finally:
        holder.execute("COMMIT")
        holder.close()
    assert pp.failures == 1 and conn.execute("SELECT COUNT(*) FROM coins").fetchone()[0] == 0
    # the failure could not be recorded while the lock was held; the next successful handle flushes it
    assert pp.handle_text(json.dumps(pp_msgs["junk"]), T0 + 1) == "info"
    kv = {r[0]: r[1] for r in conn.execute("SELECT key, value FROM kv WHERE key LIKE 'ingest.handle_failed.%'")}
    assert kv["ingest.handle_failed.pumpportal"] == "1" and kv["ingest.handle_failed.pumpportal.2025-10-26"] == "1"
    assert pp._pending_failures == 0
    raw.flush()
    # replay the hour file: the create that failed is now stored; nothing is double counted
    from fart.ingest.service import replay_raw

    monkeypatch.setattr("fart.ingest.service.wall_now", lambda: T0 + 100)
    stats = replay_raw(settings, "pumpportal", T0 - 1, T0 + 100)
    assert stats["replayed"] == 2 and stats["errors"] == 0 and stats["kinds"] == {"create": 1, "info": 1}
    assert conn.execute("SELECT COUNT(*) FROM coins").fetchone()[0] == 1
    assert conn.execute("SELECT reason FROM watchlist").fetchone()[0] == "probation"
    assert stats == replay_raw(settings, "pumpportal", T0 - 1, T0 + 100) | {"replayed": 2}  # idempotent
    assert conn.execute("SELECT COUNT(*) FROM coins").fetchone()[0] == 1
    assert db.kv_get(conn, "pp.unknown_messages") is None  # replay writes data only
    raw_files = list((settings.raw_dir / "pumpportal").rglob("*.jsonl.gz"))
    assert len(raw_files) == 1  # the replay wrote no raw copy
    # the CLI entry point
    from fart.cli import main

    conn.close()
    assert main(["--root", str(settings.root), "--quiet", "ingest", "replay", "--source", "pumpportal", "--since", "2025-10-26T11:59:00Z", "--until", str(T0 + 100)]) == 0
    assert main(["--root", str(settings.root), "--quiet", "ingest", "replay"]) == 2
    assert main(["--root", str(settings.root), "--quiet", "ingest", "replay", "--source", "pumpportal", "--since", "not-a-time"]) == 2


def test_rpc_trades_replay_maps_notifications_through_sub_id(settings, conn, fixtures_dir, pp_msgs, payloads_dir=None):
    from fart.ingest.rpc_trades import RpcTradesClient

    exp_mint = pp_msgs["new_token"]["mint"]
    store.upsert_coin(conn, store.pumpportal_create_record(pp_msgs["new_token"]), "pumpportal", T0 - 100)
    watch.add(conn, exp_mint, "early_buyers", T0 - 50, 86400, 200)
    srcs = load_source_configs(settings.config("budget"), {"RPC_DAILY_BUDGET_CU": "100000"})
    budget = Budget(conn, srcs, settings.lock_path, clock=Clock())
    raw = RawWriter(settings.raw_dir, "rpc_trades", flush_interval_s=0)
    rt = RpcTradesClient(conn, budget, raw, "wss://x", "rpc", settings.config("ingest")["rpc_trades"], settings.config("engine")["watchlist"],
                         settings.config("pumpfun"), connect=lambda *a, **k: None, clock=Clock())
    msgs = rt.sync_messages(T0)
    rt.handle_text(json.dumps({"jsonrpc": "2.0", "result": 11, "id": msgs[0]["id"]}), T0 + 1)
    note = (fixtures_dir / "logs_notification_trade_curve.json").read_text()
    rt.handle_text(note, T0 + 2)
    raw.flush()
    conn.execute("DELETE FROM trades")  # pretend the handler had failed
    from fart.ingest.service import replay_raw

    stats = replay_raw(settings, "rpc_trades", T0 - 1, T0 + 10)
    assert stats["kinds"] == {"other": 1, "trade": 1} and stats["errors"] == 0  # the subscribe confirmation means nothing in a replay
    assert conn.execute("SELECT mint, source FROM trades").fetchone()[:] == (exp_mint, "rpc_trades")
    # a notification from a subscription id that never existed stays an orphan
    stats2 = replay_raw(settings, "rpc_trades", T0 + 3600, T0 + 7200)
    assert stats2["replayed"] == 0


def test_rejected_messages_are_visible_in_status(settings, conn, pp_msgs, capsys):
    pp, raw = make_pp(settings, conn)
    pp.handle_text(json.dumps(dict(pp_msgs["migration"], mint=USDC)), T0)
    conn.close()
    from fart.cli import main

    assert main(["--root", str(settings.root), "--quiet", "--json", "status"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["counts"]["rejected_messages"] == 1
    assert data["failures"] == {"ingest.rejected.pumpportal.system_mint:USDC": "1"}
