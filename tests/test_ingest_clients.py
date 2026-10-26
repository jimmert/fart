"""Websocket clients with a fake connection; sweep/enrich with a fake RPC."""
import asyncio
import json

import pytest

from fart.ingest import coverage
from fart.ingest.pumpportal import PumpPortalClient
from fart.ingest.raw import RawWriter, iter_raw
from fart.ingest.rpc_logs import RpcLogsClient
from fart.ingest.sweep import process_enrich_queue, sweep_signatures
from fart.net.budget import Budget, load_source_configs


class Clock:
    def __init__(self, t=1_761_480_000.0):
        self.t = t

    def __call__(self):
        return self.t


class FakeWS:
    """Yields scripted messages, then raises the scripted exception (or hangs for the watchdog)."""

    def __init__(self, messages, then=None):
        self.messages = list(messages)
        self.then = then
        self.sent = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def send(self, text):
        self.sent.append(json.loads(text))

    async def recv(self):
        if self.messages:
            await asyncio.sleep(0)
            return self.messages.pop(0)
        if self.then is None:
            await asyncio.sleep(3600)
        raise self.then

    async def close(self):
        pass


def make_budget(settings, conn, clock):
    srcs = load_source_configs(settings.config("budget"), {"RPC_DAILY_BUDGET_CU": "100000"})
    return Budget(conn, srcs, settings.lock_path, clock=clock)


@pytest.fixture
def pp_msgs(fixtures_dir):
    return json.loads((fixtures_dir / "pumpportal_messages.json").read_text())


def test_pumpportal_handle_text_end_to_end(settings, conn, pp_msgs):
    clock = Clock()
    raw = RawWriter(settings.raw_dir, "pumpportal", flush_interval_s=0)
    pp = PumpPortalClient(conn, make_budget(settings, conn, clock), raw, "wss://x", settings.config("ingest")["pumpportal"],
                          settings.config("engine")["watchlist"], connect=lambda *a, **k: None, clock=clock, trades_enabled=True)
    assert pp.handle_text(json.dumps(pp_msgs["junk"]), 1.0) == "info"
    assert pp.handle_text(json.dumps(pp_msgs["new_token"]), 2.0) == "create"
    assert pp.handle_text(json.dumps(pp_msgs["trade"]), 3.0) == "trade"  # mint on probation -> stored
    assert pp.handle_text(json.dumps(pp_msgs["migration"]), 4.0) == "migrate"
    assert pp.handle_text("not json", 5.0) == "unparseable"
    assert pp.handle_text(json.dumps({"txType": "buy", "mint": "UNWATCHED", "signature": "s"}), 6.0) == "trade"
    assert pp.counts["trade_unwatched"] == 1
    assert conn.execute("SELECT COUNT(*) FROM coins").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM trades").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM migrations").fetchone()[0] == 1  # the coin is known: migration accepted
    # milestone 3: a graduation no longer puts the coin on the watchlist (subscribe_graduations=false); probation stays
    assert conn.execute("SELECT reason FROM watchlist WHERE mint=?", (pp_msgs["new_token"]["mint"],)).fetchone()[0] == "probation"
    raw.flush()
    rows = list(iter_raw(settings.raw_dir / "pumpportal" / "19700101" / "00.jsonl.gz"))
    assert len(rows) == 6 and rows[4]["msg"] == "not json"
    msgs = pp.subscription_messages(now=10.0)
    assert msgs == [{"method": "subscribeTokenTrade", "keys": [pp_msgs["new_token"]["mint"]]}]
    assert pp.subscription_messages(now=10.0) == []
    conn.execute("UPDATE watchlist SET active=0")
    assert pp.subscription_messages(now=11.0) == [{"method": "unsubscribeTokenTrade", "keys": [pp_msgs["new_token"]["mint"]]}]


def test_pumpportal_loop_reconnects_with_gaps_and_connect_gate(settings, conn, pp_msgs, monkeypatch):
    clock = Clock()
    budget = make_budget(settings, conn, clock)
    raw = RawWriter(settings.raw_dir, "pumpportal", flush_interval_s=0)
    cfg = dict(settings.config("ingest")["pumpportal"], backoff_min_s=0.01, backoff_max_s=0.02, watchdog_idle_s=0.15)
    sessions = [
        FakeWS([json.dumps(pp_msgs["new_token"])], then=ConnectionError("closed by peer")),
        FakeWS([json.dumps(pp_msgs["trade"])]),  # then silence -> watchdog fires
        FakeWS([], then=ConnectionError("again")),
    ]
    opened = []

    def connect(url, **kw):
        if not sessions:
            stop.set()
            return FakeWS([], then=ConnectionError("no more"))
        ws = sessions.pop(0)
        opened.append(ws)
        return ws

    pp = PumpPortalClient(conn, budget, raw, "wss://x", cfg, settings.config("engine")["watchlist"], connect=connect, clock=clock, trades_enabled=True)
    stop = asyncio.Event()
    asyncio.run(asyncio.wait_for(pp.run(stop), timeout=10))
    assert pp.connects >= 3
    assert pp.failures >= 2
    # subscriptions were (re)sent on every connect
    assert opened[0].sent[0] == {"method": "subscribeNewToken"} and opened[0].sent[1] == {"method": "subscribeMigration"}
    assert any(m.get("method") == "subscribeTokenTrade" for m in opened[1].sent)  # probation mint resubscribed
    gaps = conn.execute("SELECT reason, end FROM coverage_gaps ORDER BY id").fetchall()
    assert gaps[0]["reason"] == "startup" and gaps[0]["end"] is not None
    assert "disconnect" in {g["reason"] for g in gaps}
    assert gaps[-1]["reason"] == "shutdown"
    # the connect gate counted every connect
    assert budget.daily_usage("pumpportal")["requests"] == pp.connects


def test_pumpportal_connect_gate_blocks_after_burst(settings, conn, pp_msgs):
    clock = Clock()
    budget = make_budget(settings, conn, clock)
    for _ in range(12):
        assert budget.acquire("pumpportal", "connect").allowed
    v = budget.acquire("pumpportal", "connect")
    assert not v.allowed and v.reason == "rate_limit"
    assert v.retry_in_s > 200  # ~5 minutes per token at 12/hour


def test_rpc_logs_handle_text(settings, conn, fixtures_dir):
    clock = Clock()
    budget = make_budget(settings, conn, clock)
    raw = RawWriter(settings.raw_dir, "rpc_logs", flush_interval_s=0)
    rl = RpcLogsClient(conn, budget, raw, "wss://x", "rpc", settings.config("ingest")["rpc_logs"], settings.config("pumpfun"),
                       settings.config("engine")["watchlist"], connect=lambda *a, **k: None, clock=clock)
    create = (fixtures_dir / "logs_notification_create.json").read_text()
    migr = (fixtures_dir / "logs_notification_migration.json").read_text()
    assert rl.handle_text(json.dumps({"jsonrpc": "2.0", "result": 7, "id": 1}), 1.0) == "other"
    assert rl.handle_text(create, 2.0) == "create"
    assert rl.handle_text(migr, 3.0) == "migrate"
    bad = json.loads(create)
    bad["params"]["result"]["value"]["err"] = {"InstructionError": [0, "Custom"]}
    assert rl.handle_text(json.dumps(bad), 4.0) == "failed_tx"
    mixed = json.loads(create)
    mixed["params"]["result"]["value"]["logs"] = json.loads((fixtures_dir / "logs_mixed_bad.json").read_text())["logs"]
    mixed["params"]["result"]["value"]["signature"] = "othersig"
    assert rl.handle_text(json.dumps(mixed), 5.0) == "create"  # the v1 create inside the bad batch
    assert rl.handle_text("garbage", 6.0) == "unparseable"
    coin = conn.execute("SELECT * FROM coins").fetchone()
    assert coin["slot"] == 370_000_001 and coin["block_time"] == 1_761_480_000 and coin["dev_buy_sol"] == 1.5
    assert conn.execute("SELECT COUNT(*) FROM trades").fetchone()[0] == 1  # dev buy stored (probation)
    assert conn.execute("SELECT pool_address FROM migrations").fetchone()[0] is not None
    assert conn.execute("SELECT COUNT(*) FROM raw_undecoded").fetchone()[0] == 4
    assert conn.execute("SELECT value FROM kv WHERE key='decode.unknown_discriminator'").fetchone()[0] == "1"
    assert conn.execute("SELECT value FROM kv WHERE key='ingest.rpc_logs.last_create_sig'").fetchone()[0] == "othersig"
    rl.flush_cost()
    assert budget.daily_usage("rpc")["cost"] == 4 * 10  # 4 notifications x ws_notification estimate
    assert not rl.cap_reached()


def test_rpc_logs_pauses_at_daily_cap(settings, conn, fixtures_dir):
    clock = Clock()
    srcs = load_source_configs(settings.config("budget"), {"RPC_DAILY_BUDGET_CU": "25"})
    budget = Budget(conn, srcs, settings.lock_path, clock=clock)
    raw = RawWriter(settings.raw_dir, "rpc_logs", flush_interval_s=0)
    rl = RpcLogsClient(conn, budget, raw, "wss://x", "rpc", settings.config("ingest")["rpc_logs"], settings.config("pumpfun"),
                       settings.config("engine")["watchlist"], connect=lambda *a, **k: None, clock=clock)
    create = (fixtures_dir / "logs_notification_create.json").read_text()
    rl.handle_text(create, 1.0)
    rl.handle_text(create, 2.0)
    rl.flush_cost()
    assert rl.cap_reached()


class FakeRpc:
    def __init__(self, sigs_by_address, txs):
        self.sigs = sigs_by_address
        self.txs = txs
        self.calls = []

    def available(self, method="*"):
        return True

    def get_signatures_for_address(self, address, limit=1000, before=None, until=None):
        self.calls.append(("sigs", address, before, until))
        items = self.sigs.get(address, [])
        if until:
            idx = [i for i, s in enumerate(items) if s["signature"] == until]
            items = items[: idx[0]] if idx else items
        if before:
            idx = [i for i, s in enumerate(items) if s["signature"] == before]
            items = items[idx[0] + 1:] if idx else []
        return items[:limit]

    def get_transaction(self, sig):
        self.calls.append(("tx", sig))
        return self.txs[sig]


def test_sweep_fills_chain_time_and_backfills_missed(settings, conn, fixtures_dir):
    pf = settings.config("pumpfun")
    tx_create = json.loads((fixtures_dir / "tx_create.json").read_text())["result"]
    tx_migr = json.loads((fixtures_dir / "tx_migration.json").read_text())["result"]
    sig_known = tx_create["transaction"]["signatures"][0]
    # a PumpPortal-only coin with the known signature and no chain time yet
    conn.execute("INSERT INTO coins(mint, signature, received_at, source, sources) VALUES('M1', ?, 100, 'pumpportal', '[\"pumpportal\"]')", (sig_known,))
    sigs = {
        pf["mint_authority"]: [
            {"signature": "sigMISSED", "slot": 370_000_002, "blockTime": 1_761_480_001, "err": None},
            {"signature": sig_known, "slot": 370_000_001, "blockTime": 1_761_480_000, "err": None},
            {"signature": "sigFAILED", "slot": 370_000_000, "blockTime": 1_761_479_999, "err": {"x": 1}},
        ],
        pf["migration_account"]: [{"signature": "sigMIGR", "slot": 370_000_900, "blockTime": 1_761_480_090, "err": None}],
    }
    rpc = FakeRpc(sigs, {"sigMISSED": tx_create, "sigMIGR": tx_migr})
    res = sweep_signatures(conn, rpc, {"create": pf["mint_authority"], "migrate": pf["migration_account"]},
                           settings.config("ingest")["backfill"], now=1000.0)
    assert res["create"] == {"signatures": 3, "pages": 1, "filled": 1, "missed": 1, "failed_tx": 1, "first_run": True, "truncated": False}
    row = conn.execute("SELECT slot, block_time, sources FROM coins WHERE mint='M1'").fetchone()
    assert row["slot"] == 370_000_001 and row["block_time"] == 1_761_480_000 and "rpc_sigs" in row["sources"]
    assert conn.execute("SELECT value FROM kv WHERE key='ingest.sweep.create.last_sig'").fetchone()[0] == "sigMISSED"
    queued = {(r[0], r[1]) for r in conn.execute("SELECT key, kind FROM enrich_queue WHERE done=0")}
    assert queued == {("sigMISSED", "create_tx"), ("sigMIGR", "migrate_tx")}

    cfg = dict(settings.config("ingest")["backfill"], **settings.config("ingest")["enrich"])
    stats = process_enrich_queue(conn, rpc, cfg, now=1001.0)
    assert stats["fetched"] == 2 and stats["creates"] == 1 and stats["migrations"] == 1
    # the synthetic create tx carries the fixture mint, now known via rpc_backfill
    assert conn.execute("SELECT COUNT(*) FROM coins").fetchone()[0] == 2
    assert conn.execute("SELECT source FROM coins WHERE mint<>'M1'").fetchone()[0] == "rpc_backfill"
    assert conn.execute("SELECT dex, pool_address FROM migrations").fetchone()["dex"] == "pump-amm"
    assert conn.execute("SELECT COUNT(*) FROM enrich_queue WHERE done=0").fetchone()[0] == 0
    # second sweep: incremental from last_sig, nothing new
    res2 = sweep_signatures(conn, rpc, {"create": pf["mint_authority"], "migrate": pf["migration_account"]},
                            settings.config("ingest")["backfill"], now=2000.0)
    assert res2["create"]["signatures"] == 0 and res2["create"]["first_run"] is False
    assert rpc.calls[-2][3] == "sigMISSED"  # until=last seen signature


def test_enrich_queue_respects_daily_cap_and_records_errors(settings, conn, fixtures_dir):
    from fart.chain.rpc import RpcError

    class ErrRpc(FakeRpc):
        def get_transaction(self, sig):
            raise RpcError(-32000, "boom")

    conn.execute("INSERT INTO enrich_queue(key, kind, requested_at) VALUES('s1','create_tx',1)")
    cfg = dict(settings.config("ingest")["backfill"], **settings.config("ingest")["enrich"], max_transactions_per_day=0)
    stats = process_enrich_queue(conn, ErrRpc({}, {}), cfg, now=10.0)
    assert stats["skipped_budget"] == 1
    cfg["max_transactions_per_day"] = 10
    stats = process_enrich_queue(conn, ErrRpc({}, {}), cfg, now=11.0)
    assert stats["errors"] == 1
    row = conn.execute("SELECT attempts, last_error FROM enrich_queue").fetchone()
    assert row["attempts"] == 1 and "boom" in row["last_error"]


def test_retention_archives_and_prunes(settings, conn):
    from fart.ingest.retention import maybe_run_retention, run_retention

    now = 1_761_480_000.0
    conn.execute("INSERT INTO trades(mint, received_at, signature, is_buy, sol, tokens, source) VALUES('OLD', ?, 's1', 1, 1, 1, 'pumpportal')", (now - 20 * 86400,))
    conn.execute("INSERT INTO trades(mint, received_at, signature, is_buy, sol, tokens, source) VALUES('NEW', ?, 's2', 1, 1, 1, 'pumpportal')", (now - 86400,))
    conn.execute("INSERT INTO trades(mint, received_at, signature, is_buy, sol, tokens, source) VALUES('W', ?, 's3', 1, 1, 1, 'pumpportal')", (now - 20 * 86400,))
    conn.execute("INSERT INTO watchlist(mint, since, reason, active) VALUES('W', ?, 'observe', 1)", (now - 20 * 86400,))
    conn.execute("INSERT INTO api_ledger(ts, source, method, cost) VALUES(?, 'rpc', 'x', 1)", (now - 40 * 86400,))
    conn.execute("INSERT INTO decisions(as_of, created_at, kind, features_json, rules_version, verdict, reasons_json) VALUES(?, ?, 'observe', '{}', 'r', 'observe', '[]')", (now - 100 * 86400, now - 100 * 86400))
    out = run_retention(conn, settings.raw_dir, settings.archive_dir, settings.config("ingest")["retention"], now)
    assert out["trades_archived"] == 1 and out["ledger_archived"] == 1
    assert {r[0] for r in conn.execute("SELECT mint FROM trades")} == {"NEW", "W"}
    assert conn.execute("SELECT COUNT(*) FROM decisions").fetchone()[0] == 1  # ledgers untouched
    archived = list(settings.archive_dir.glob("trades-*.jsonl.gz"))
    assert len(archived) == 1
    assert maybe_run_retention(conn, settings.raw_dir, settings.archive_dir, settings.config("ingest")["retention"], now + 60) is None
