"""Per-mint RPC trade stream: subscription policy, protocol diffing, decoding, cost booking, reconnects."""
import asyncio
import base64
import hashlib
import json

import pytest

from fart.chain.base58 import b58encode
from fart.chain.pda import bonding_curve_address
from fart.ingest import store, watch
from fart.ingest.raw import RawWriter
from fart.ingest.rpc_trades import RpcTradesClient, fill_block_times
from fart.net.budget import Blocked, Budget, load_source_configs

T0 = 1_761_480_000.0
TS = 1_761_480_000
AMM = "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA"


def pk(seed: str) -> str:
    return b58encode(hashlib.sha256(seed.encode()).digest())


class Clock:
    def __init__(self, t=T0):
        self.t = t

    def __call__(self):
        return self.t


class FakeWS:
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


def make_client(settings, conn, clock, cu_cap="100000", wcfg_over=None, cfg_over=None, connect=None):
    srcs = load_source_configs(settings.config("budget"), {"RPC_DAILY_BUDGET_CU": cu_cap})
    budget = Budget(conn, srcs, settings.lock_path, clock=clock)
    raw = RawWriter(settings.raw_dir, "rpc_trades", flush_interval_s=0)
    wcfg = dict(settings.config("engine")["watchlist"], **(wcfg_over or {}))
    cfg = dict(settings.config("ingest")["rpc_trades"], **(cfg_over or {}))
    rt = RpcTradesClient(conn, budget, raw, "wss://x", "rpc", cfg, wcfg, settings.config("pumpfun"),
                         connect=connect or (lambda *a, **k: None), clock=clock)
    return rt, budget


@pytest.fixture
def pp_msgs(fixtures_dir):
    return json.loads((fixtures_dir / "pumpportal_messages.json").read_text())


@pytest.fixture
def payloads(fixtures_dir):
    return json.loads((fixtures_dir / "events_payloads.json").read_text())


def add_coin(conn, mint, curve=None, creator="C"):
    conn.execute("INSERT INTO coins(mint, name, symbol, creator, bonding_curve, received_at, source, sources) VALUES(?,?,?,?,?,?,'pumpportal','[]')",
                 (mint, mint[:4], mint[:3], creator, curve, T0 - 100))


def test_desired_policy_addresses_priority_and_cap(settings, conn):
    pf = settings.config("pumpfun")
    m_obs, p1, p2, g1, g2 = pk("obs"), pk("prob1"), pk("prob2"), pk("grad1"), pk("grad2")
    curve_obs = pk("curve-obs")
    for m, curve in ((m_obs, curve_obs), (p1, None), (p2, None), (g1, None), (g2, None)):
        add_coin(conn, m, curve)
    conn.execute("INSERT INTO migrations(mint, received_at, pool_address, source, sources) VALUES(?,?,?,'rpc_logs','[]')", (g1, T0 - 50, pk("pool-g1")))
    conn.execute("INSERT INTO migrations(mint, received_at, pool_address, source, sources) VALUES(?,?,NULL,'pumpportal','[]')", (g2, T0 - 40))
    watch.add(conn, m_obs, "observe", T0 - 100, None, 200)
    watch.add(conn, p1, "probation", T0 - 10, 300, 200)
    watch.add(conn, p2, "probation", T0 - 5, 300, 200)
    watch.add(conn, g1, "graduation", T0 - 50, 86400, 200)
    watch.add(conn, g2, "graduation", T0 - 40, 86400, 200)
    rt, _ = make_client(settings, conn, Clock(), wcfg_over={"rpc_trades_cap": 4})
    d = rt.desired(T0)
    assert set(d) == {m_obs, g1, g2, p2}  # observe > graduation > probation; the newest probation wins the last slot
    assert d[m_obs] == (curve_obs, "curve", "observe")
    assert d[g1] == (pk("pool-g1"), "pool", "graduation")
    assert d[g2] == (g2, "mint", "graduation")  # migrated, pool unknown -> the mint itself
    assert d[p2] == (bonding_curve_address(p2, pf["program_id"]), "curve", "probation")  # curve unknown -> derived PDA
    assert rt.policy == {"active_watchlist": 5, "eligible": 5, "subscribed": 4, "cap": 4, "cu_fraction": 0.0, "dropping_low_priority": False}
    # mention=mint: always the mint before graduation
    rt2, _ = make_client(settings, conn, Clock(), cfg_over={"mention": "mint"})
    assert rt2.desired(T0)[m_obs] == (m_obs, "mint", "observe")
    # expiry is applied
    assert len(rt.desired(T0 + 400)) == 3  # both probation entries expired


def test_cu_policy_drops_probation_above_fraction(settings, conn):
    rt, budget = make_client(settings, conn, Clock(), cu_cap="1000")
    for seed, reason in (("a", "observe"), ("b", "probation"), ("c", "graduation"), ("d", "early_buyers"), ("e", "meta_burst")):
        add_coin(conn, pk(seed), pk("curve-" + seed))
        watch.add(conn, pk(seed), reason, T0 - 1, None, 200)
    assert len(rt.desired(T0)) == 5
    budget.record("rpc", "ws_notification", 850.0, True, None, None)  # 85% of the daily cap
    d = rt.desired(T0)
    assert {v[2] for v in d.values()} == {"observe", "early_buyers", "meta_burst"}
    assert rt.policy["dropping_low_priority"] is True and rt.policy["cu_fraction"] == 0.85
    assert rt.cap_reached() is False


def test_sync_and_notifications_end_to_end(settings, conn, fixtures_dir, pp_msgs, payloads):
    exp = payloads["expected"]
    mint, curve, pool = exp["mint"], exp["curve"], exp["pool"]
    store.upsert_coin(conn, store.pumpportal_create_record(pp_msgs["new_token"]), "pumpportal", T0 - 100)
    watch.add(conn, mint, "early_buyers", T0 - 50, 86400, 200)
    rt, budget = make_client(settings, conn, Clock())
    msgs = rt.sync_messages(T0)
    assert len(msgs) == 1 and msgs[0]["method"] == "logsSubscribe"
    assert msgs[0]["params"] == [{"mentions": [curve]}, {"commitment": "confirmed"}]
    rid = msgs[0]["id"]
    assert rt.sync_messages(T0 + 0.5) == []  # pending, nothing new
    assert rt.handle_text(json.dumps({"jsonrpc": "2.0", "result": 11, "id": rid}), T0 + 1) == "subscribed"
    note = (fixtures_dir / "logs_notification_trade_curve.json").read_text()  # subscription 11
    assert rt.handle_text(note, T0 + 2) == "trade"
    row = conn.execute("SELECT * FROM trades WHERE source='rpc_trades'").fetchone()
    assert row["mint"] == mint and row["buyer"] == exp["buyer3"] and row["is_buy"] == 1
    assert row["sol"] == pytest.approx(0.7) and row["slot"] == 370_000_050 and row["block_time"] == TS + 30
    assert row["virtual_sol"] == 32_200_000_000
    assert rt.handle_text(note, T0 + 3) == "notification"  # duplicate signature: no second row
    assert conn.execute("SELECT COUNT(*) FROM trades").fetchone()[0] == 1
    other = json.loads(note)
    other["params"]["subscription"] = 99
    assert rt.handle_text(json.dumps(other), T0 + 4) == "orphan"
    assert rt.handle_text("garbage", T0 + 4.5) == "unparseable"
    # the migration transaction arrives through the curve subscription: migrations gets the pool
    mig = (fixtures_dir / "logs_notification_migration_with_pool.json").read_text()
    assert rt.handle_text(mig, T0 + 5) == "notification"
    assert conn.execute("SELECT pool_address, source FROM migrations WHERE mint=?", (mint,)).fetchone()["pool_address"] == pool
    # next sync moves the subscription from the curve to the pool
    msgs = rt.sync_messages(T0 + 6)
    assert [m["method"] for m in msgs] == ["logsUnsubscribe", "logsSubscribe"]
    assert msgs[0]["params"] == [11] and msgs[1]["params"][0] == {"mentions": [pool]}
    assert rt.handle_text(json.dumps({"jsonrpc": "2.0", "result": True, "id": msgs[0]["id"]}), T0 + 7) == "unsubscribed"
    assert rt.handle_text(json.dumps({"jsonrpc": "2.0", "result": 12, "id": msgs[1]["id"]}), T0 + 7) == "subscribed"
    assert rt.handle_text((fixtures_dir / "logs_notification_trade_pool.json").read_text(), T0 + 8) == "trade"
    assert rt.handle_text((fixtures_dir / "logs_notification_trade_pool_sell.json").read_text(), T0 + 9) == "trade"
    amm = conn.execute("SELECT * FROM trades WHERE source='pump_amm' ORDER BY received_at").fetchall()
    assert len(amm) == 2
    assert amm[0]["is_buy"] == 1 and amm[0]["sol"] == pytest.approx(2.0) and amm[0]["tokens"] == pytest.approx(10_000_000.0)
    assert amm[0]["virtual_sol"] == 100_000_000_000 and amm[0]["virtual_token"] == 200_000_000_000_000 and amm[0]["block_time"] == TS + 600
    assert amm[0]["buyer"] == exp["amm_buyer"] and amm[0]["mint"] == mint
    assert amm[1]["is_buy"] == 0 and amm[1]["sol"] == pytest.approx(0.8)
    # a BuyEvent for another pool on our subscription is rejected (layout self-check), counted, not stored
    raw_buy = base64.b64decode(payloads["amm_buy"])
    off = 8 + 14 * 8
    bad_payload = raw_buy[:off] + b"\x11" * 32 + raw_buy[off + 32:]
    bad = json.loads((fixtures_dir / "logs_notification_trade_pool.json").read_text())
    bad["params"]["result"]["value"]["signature"] = "sig-bad-pool"
    bad["params"]["result"]["value"]["logs"] = [f"Program {AMM} invoke [1]", f"Program data: {base64.b64encode(bad_payload).decode()}", f"Program {AMM} success"]
    assert rt.handle_text(json.dumps(bad), T0 + 10) == "notification"
    assert rt.counts["amm_pool_mismatch"] == 1
    assert conn.execute("SELECT value FROM kv WHERE key='decode.pump_amm_pool_mismatch'").fetchone()[0] == "1"
    assert conn.execute("SELECT COUNT(*) FROM trades").fetchone()[0] == 3
    # per-subscription bookkeeping
    rows = conn.execute("SELECT * FROM ws_subscriptions ORDER BY since").fetchall()
    assert [r["kind"] for r in rows] == ["curve", "pool"]
    assert rows[0]["until"] is not None and rows[0]["notifications"] == 3 and rows[0]["trades"] == 1
    assert rows[1]["until"] is None and rows[1]["notifications"] == 3 and rows[1]["trades"] == 2 and rows[1]["reason"] == "early_buyers"
    # cost booking: every notification (orphan included), every subscribe and unsubscribe
    rt.flush_cost()
    ledger = {r[0]: r[1] for r in conn.execute("SELECT method, SUM(cost) FROM api_ledger WHERE source='rpc' GROUP BY method")}
    assert ledger == {"ws_notification": 70.0, "ws_subscribe": 20.0, "ws_unsubscribe": 10.0}
    assert rt.stats()["notifications"] == 7
    # the watchlist entry expires -> unsubscribe; nothing left
    conn.execute("UPDATE watchlist SET active=0")
    msgs = rt.sync_messages(T0 + 20)
    assert [m["method"] for m in msgs] == ["logsUnsubscribe"] and msgs[0]["params"] == [12] and rt.subs == {}
    assert conn.execute("SELECT COUNT(*) FROM ws_subscriptions WHERE until IS NULL").fetchone()[0] == 0
    # an error on a pending subscribe drops it
    watch.add(conn, mint, "observe", T0 + 21, None, 200)
    msgs = rt.sync_messages(T0 + 21)
    assert msgs[-1]["method"] == "logsSubscribe" and msgs[-1]["params"][0] == {"mentions": [pool]}
    assert rt.handle_text(json.dumps({"jsonrpc": "2.0", "error": {"code": -32602, "message": "bad"}, "id": msgs[-1]["id"]}), T0 + 22) == "error"
    assert rt.subs == {} and rt.counts["subscribe_error"] == 1
    # a removal that races the confirmation: unsubscribe is queued for the session loop
    msgs = rt.sync_messages(T0 + 23)
    rid = msgs[-1]["id"]
    conn.execute("UPDATE watchlist SET active=0")
    assert rt.sync_messages(T0 + 24) == []  # not confirmed yet: marked for removal
    assert rt.handle_text(json.dumps({"jsonrpc": "2.0", "result": 13, "id": rid}), T0 + 25) == "subscribed"
    assert rt.outgoing and rt.outgoing[0]["method"] == "logsUnsubscribe" and rt.outgoing[0]["params"] == [13]
    assert 13 not in rt.by_sub


def test_run_loop_resubscribes_after_disconnect(settings, conn, pp_msgs, payloads):
    mint = payloads["expected"]["mint"]
    store.upsert_coin(conn, store.pumpportal_create_record(pp_msgs["new_token"]), "pumpportal", T0 - 100)
    watch.add(conn, mint, "early_buyers", T0 - 50, 86400, 200)
    sessions = [
        FakeWS([json.dumps({"jsonrpc": "2.0", "result": 11, "id": 1})], then=ConnectionError("closed by peer")),
        FakeWS([], then=ConnectionError("again")),
    ]
    opened = []
    stop = asyncio.Event()

    def connect(url, **kw):
        if not sessions:
            stop.set()
            return FakeWS([], then=ConnectionError("no more"))
        ws = sessions.pop(0)
        opened.append(ws)
        return ws

    rt, budget = make_client(settings, conn, Clock(), cfg_over={"backoff_min_s": 0.01, "backoff_max_s": 0.02, "sync_interval_s": 0.05}, connect=connect)
    asyncio.run(asyncio.wait_for(rt.run(stop), timeout=10))
    assert rt.connects >= 2
    assert opened[0].sent[0]["method"] == "logsSubscribe" and opened[0].sent[0]["params"][0] == {"mentions": [payloads["expected"]["curve"]]}
    assert opened[1].sent[0]["method"] == "logsSubscribe"  # re-subscribed after the reconnect
    reasons = [r[0] for r in conn.execute("SELECT reason FROM coverage_gaps WHERE source='rpc_trades' ORDER BY id")]
    assert reasons[0] == "startup" and "disconnect" in reasons and reasons[-1] == "shutdown"
    rows = conn.execute("SELECT until FROM ws_subscriptions ORDER BY since").fetchall()
    assert len(rows) >= 2 and all(r["until"] is not None for r in rows)  # every session's row closed at shutdown
    assert budget.daily_usage("rpc")["requests"] >= rt.connects


class FakeRpc:
    def __init__(self, blocked=False):
        self.blocked = blocked
        self.calls = []

    def available(self, method="*"):
        return not self.blocked

    def get_block_time(self, slot):
        self.calls.append(slot)
        if slot == 7:
            return None
        return 1_700_000_000 + slot


def test_fill_block_times(conn):
    for i, slot in enumerate((5, 5, 6, 7)):
        conn.execute("INSERT INTO trades(mint, slot, received_at, signature, is_buy, sol, tokens, source) VALUES('M', ?, ?, ?, 1, 1, 1, 'rpc_trades')",
                     (slot, T0 + i, f"s{i}"))
    conn.execute("INSERT INTO trades(mint, slot, received_at, signature, is_buy, sol, tokens, source) VALUES('M', 9, ?, 's9', 1, 1, 1, 'pumpportal')", (T0,))
    rpc = FakeRpc()
    st = fill_block_times(conn, rpc, 10, T0)
    assert st == {"slots": 3, "filled": 2, "skipped_budget": 0, "errors": 0}
    assert sorted(rpc.calls) == [5, 6, 7]  # once per slot
    assert conn.execute("SELECT COUNT(*) FROM trades WHERE block_time IS NULL").fetchone()[0] == 2  # slot 7 (no block) + the PumpPortal row
    assert conn.execute("SELECT block_time FROM trades WHERE signature='s0'").fetchone()[0] == 1_700_000_005
    assert fill_block_times(conn, FakeRpc(blocked=True), 10, T0)["skipped_budget"] == 1
