"""Per-mint RPC trade stream: subscription policy (pre-graduation only since milestone 3), protocol
diffing, decoding (pump-amm swaps through the pool -> mint mapping), cost booking, reconnects.
The milestone-4 hysteresis of sync_messages() has its own tests (tests/test_m4_watchlist_stream.py);
the protocol tests here switch it off (subscribe_after_syncs 1, unsubscribe_after_s 0)."""
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


NO_HYSTERESIS = {"subscribe_after_syncs": 1, "unsubscribe_after_s": 0}


def make_client(settings, conn, clock, cu_cap="100000", wcfg_over=None, cfg_over=None, connect=None, hysteresis=False):
    srcs = load_source_configs(settings.config("budget"), {"RPC_DAILY_BUDGET_CU": cu_cap})
    budget = Budget(conn, srcs, settings.lock_path, clock=clock)
    raw = RawWriter(settings.raw_dir, "rpc_trades", flush_interval_s=0)
    wcfg = dict(settings.config("engine")["watchlist"], **(wcfg_over or {}))
    cfg = dict(settings.config("ingest")["rpc_trades"], **({} if hysteresis else NO_HYSTERESIS), **(cfg_over or {}))
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
    m_obs, p1, p2, g1, g2, mb = pk("obs"), pk("prob1"), pk("prob2"), pk("grad1"), pk("grad2"), pk("meta")
    curve_obs = pk("curve-obs")
    for m, curve in ((m_obs, curve_obs), (p1, None), (p2, None), (g1, None), (g2, None), (mb, pk("curve-mb"))):
        add_coin(conn, m, curve)
    conn.execute("INSERT INTO migrations(mint, received_at, pool_address, source, sources) VALUES(?,?,?,'rpc_logs','[]')", (g1, T0 - 50, pk("pool-g1")))
    conn.execute("INSERT INTO migrations(mint, received_at, pool_address, source, sources) VALUES(?,?,NULL,'pumpportal','[]')", (g2, T0 - 40))
    watch.add(conn, m_obs, "observe", T0 - 100, None, 200)
    watch.add(conn, p1, "probation", T0 - 10, 300, 200)
    watch.add(conn, p2, "probation", T0 - 5, 300, 200)
    watch.add(conn, g1, "graduation", T0 - 50, 86400, 200)
    watch.add(conn, g2, "graduation", T0 - 40, 86400, 200)
    watch.add(conn, mb, "meta_burst", T0 - 20, 86400, 200)
    rt, _ = make_client(settings, conn, Clock(), wcfg_over={"rpc_trades_cap": 2})
    d = rt.desired(T0)
    # milestone 3: graduated coins and the observe/graduation reasons are never subscribed; never a pool address
    assert set(d) == {mb, p2}  # meta_burst > probation; the newest probation wins the last slot
    assert d[mb] == (pk("curve-mb"), "curve", "meta_burst")
    assert d[p2] == (bonding_curve_address(p2, pf["program_id"]), "curve", "probation")  # curve unknown -> derived PDA
    assert all(v[1] in ("curve", "mint") for v in d.values())
    assert rt.policy["active_watchlist"] == 6 and rt.policy["eligible"] == 3 and rt.policy["subscribed"] == 2 and rt.policy["cap"] == 2
    assert rt.policy["graduated_excluded"] == 2 and rt.policy["envelope_paused"] is False and rt.policy["muted"] == 0
    share = settings.config("ingest")["rpc_trades"]["daily_cu_share"]
    assert share == 0.35 and rt.policy["daily_cu_share"] == share and "hourly_cu_fraction" not in settings.config("ingest")["rpc_trades"]
    assert rt.policy["hourly_cu_allowance"] == round(100000 * share / 24)  # the hourly envelope is the daily share spread over 24 hours
    # a graduation of a subscribed coin drops it at the next sync (nothing follows the coin onto the pool)
    conn.execute("INSERT INTO migrations(mint, received_at, pool_address, source, sources) VALUES(?,?,?,'rpc_logs','[]')", (mb, T0 + 1, pk("pool-mb")))
    assert set(rt.desired(T0 + 2)) == {p2, p1}
    # mention=mint: always the mint
    rt2, _ = make_client(settings, conn, Clock(), cfg_over={"mention": "mint"})
    assert rt2.desired(T0)[p1] == (p1, "mint", "probation")
    # expiry is applied
    assert rt.desired(T0 + 400) == {}  # both probation entries expired, the rest is graduated or not a stream reason


def test_cu_policy_drops_probation_above_fraction(settings, conn):
    rt, budget = make_client(settings, conn, Clock(), cu_cap="1000", cfg_over={"daily_cu_share": 1.0})
    for seed, reason in (("b", "probation"), ("d", "early_buyers"), ("e", "meta_burst")):
        add_coin(conn, pk(seed), pk("curve-" + seed))
        watch.add(conn, pk(seed), reason, T0 - 1, None, 200)
    assert len(rt.desired(T0)) == 3
    budget.record("rpc", "ws_notification", 850.0, True, None, None)  # 85% of the daily cap (the log stream's spend counts too)
    d = rt.desired(T0)
    assert {v[2] for v in d.values()} == {"early_buyers", "meta_burst"}
    assert rt.policy["dropping_low_priority"] is True and rt.policy["cu_fraction"] == 0.85
    assert rt.cap_reached() is False


def test_hourly_envelope_pauses_the_whole_stream(settings, conn):
    """The stream's own CU per UTC hour (ws_trade_* ledger rows + pending frames) may not exceed daily_cu_share / 24 x the daily cap."""
    clock = Clock(T0)  # 2025-10-26T12:00:00Z exactly
    rt, budget = make_client(settings, conn, clock, cu_cap="24000", cfg_over={"daily_cu_share": 1.0})
    for seed in ("a", "b"):
        add_coin(conn, pk(seed), pk("curve-" + seed))
        watch.add(conn, pk(seed), "probation", T0 - 1, None, 200)
    assert rt.hourly_allowance() == pytest.approx(1000.0)
    assert len(rt.desired(T0)) == 2 and rt.policy["envelope_paused"] is False
    # the log stream's notifications do not count against the trade stream's envelope
    budget.record("rpc", "ws_notification", 5000.0, True, None, None)
    assert len(rt.desired(T0 + 1)) == 2
    # 100 pending (unflushed) trade notifications of 50,000 bytes x 0.0002 CU/byte reach the allowance -> everything dropped
    rt._pending_bytes["ws_trade_notification"], rt._pending_frames["ws_trade_notification"] = 5_000_000, 100
    assert rt.stream_cost_this_hour(T0 + 2) == pytest.approx(1000.0)
    assert rt.desired(T0 + 2) == {} and rt.policy["envelope_paused"] is True and rt.policy["stream_cu_this_hour"] == 1000
    assert conn.execute("SELECT value FROM kv WHERE key='rpc_trades.envelope_paused'").fetchone()[0] == "1"
    assert conn.execute("SELECT value FROM kv WHERE key='rpc_trades.envelope_paused.2025-10-26'").fetchone()[0] == "1"
    rt.flush_cost()  # booked as ws_trade_notification at T0+2 (the current hour), with the byte count
    assert rt.desired(T0 + 3) == {} and conn.execute("SELECT value FROM kv WHERE key='rpc_trades.envelope_paused'").fetchone()[0] == "1"  # counted once per episode
    ledger = {r[0]: r[1] for r in conn.execute("SELECT method, SUM(cost) FROM api_ledger WHERE source='rpc' GROUP BY method")}
    assert ledger["ws_trade_notification"] == 1000.0 and ledger["ws_notification"] == 5000.0
    assert conn.execute("SELECT bytes FROM api_ledger WHERE method='ws_trade_notification'").fetchone()[0] == 5_000_000
    # the next UTC hour starts with an empty envelope: the subscriptions come back
    clock.t = T0 + 3600
    assert len(rt.desired(clock.t)) == 2 and rt.policy["envelope_paused"] is False
    # the daily cap still closes the stream entirely
    budget.record("rpc", "ws_trade_notification", 18000.0, True, None, None)
    assert rt.cap_reached() is True


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
    sub_bytes = len(json.dumps(msgs[0]).encode())
    notif_bytes = 0
    assert rt.sync_messages(T0 + 0.5) == []  # pending, nothing new
    sub_resp = json.dumps({"jsonrpc": "2.0", "result": 11, "id": rid})
    assert rt.handle_text(sub_resp, T0 + 1) == "subscribed"
    sub_bytes += len(sub_resp.encode())
    assert conn.execute("SELECT sub_id FROM ws_subscriptions WHERE mint=?", (mint,)).fetchone()[0] == 11  # for replays
    note = (fixtures_dir / "logs_notification_trade_curve.json").read_text()  # subscription 11
    assert rt.handle_text(note, T0 + 2) == "trade"
    notif_bytes += 2 * len(note.encode())  # this one and the duplicate below
    row = conn.execute("SELECT * FROM trades WHERE source='rpc_trades'").fetchone()
    assert row["mint"] == mint and row["buyer"] == exp["buyer3"] and row["is_buy"] == 1
    assert row["sol"] == pytest.approx(0.7) and row["slot"] == 370_000_050 and row["block_time"] == TS + 30
    assert row["virtual_sol"] == 32_200_000_000
    assert rt.handle_text(note, T0 + 3) == "notification"  # duplicate signature: no second row
    assert conn.execute("SELECT COUNT(*) FROM trades").fetchone()[0] == 1
    other = json.loads(note)
    other["params"]["subscription"] = 99
    other_text = json.dumps(other)
    assert rt.handle_text(other_text, T0 + 4) == "orphan"
    notif_bytes += len(other_text.encode())  # an orphan is still a frame Alchemy delivered
    assert rt.handle_text("garbage", T0 + 4.5) == "unparseable"
    # a pump-amm swap whose pool is unknown is rejected and counted, never attributed to the subscription's mint
    early_pool_buy = json.loads((fixtures_dir / "logs_notification_trade_pool.json").read_text())
    early_pool_buy["params"]["subscription"] = 11
    early_pool_buy["params"]["result"]["value"]["signature"] = "sig-early-pool-buy"
    early_text = json.dumps(early_pool_buy)
    assert rt.handle_text(early_text, T0 + 4.7) == "notification"
    notif_bytes += len(early_text.encode())
    assert rt.counts["amm_pool_unknown"] == 1 and conn.execute("SELECT value FROM kv WHERE key='decode.pump_amm_pool_unknown'").fetchone()[0] == "1"
    assert conn.execute("SELECT COUNT(*) FROM trades").fetchone()[0] == 1
    # the migration transaction arrives through the curve subscription: migrations gets the pool (CreatePoolEvent)
    mig = (fixtures_dir / "logs_notification_migration_with_pool.json").read_text()
    assert rt.handle_text(mig, T0 + 5) == "notification"
    notif_bytes += len(mig.encode())
    assert conn.execute("SELECT pool_address, source FROM migrations WHERE mint=?", (mint,)).fetchone()["pool_address"] == pool
    # a swap on that pool, seen on the still-open curve subscription, resolves its mint through migrations.pool_address
    pool_trade = (fixtures_dir / "logs_notification_trade_pool.json").read_text().replace('"subscription": 12', '"subscription": 11')
    assert rt.handle_text(pool_trade, T0 + 5.5) == "trade"
    notif_bytes += len(pool_trade.encode())
    amm = conn.execute("SELECT * FROM trades WHERE source='pump_amm'").fetchall()
    assert len(amm) == 1 and amm[0]["mint"] == mint and amm[0]["is_buy"] == 1 and amm[0]["sol"] == pytest.approx(2.0)
    assert amm[0]["tokens"] == pytest.approx(10_000_000.0) and amm[0]["virtual_sol"] == 100_000_000_000 and amm[0]["block_time"] == TS + 600
    assert amm[0]["buyer"] == exp["amm_buyer"]
    # milestone 3: the graduated coin is dropped at the next sync; nothing follows it onto the pool
    msgs = rt.sync_messages(T0 + 6)
    assert [m["method"] for m in msgs] == ["logsUnsubscribe"] and msgs[0]["params"] == [11] and rt.subs == {}
    assert rt.policy["graduated_excluded"] == 1
    unsub_bytes = len(json.dumps(msgs[0]).encode())
    unsub_resp = json.dumps({"jsonrpc": "2.0", "result": True, "id": msgs[0]["id"]})
    assert rt.handle_text(unsub_resp, T0 + 7) == "unsubscribed"
    unsub_bytes += len(unsub_resp.encode())
    pool_sell = (fixtures_dir / "logs_notification_trade_pool_sell.json").read_text()
    assert rt.handle_text(pool_sell, T0 + 8) == "orphan"  # subscription 12 never existed
    notif_bytes += len(pool_sell.encode())
    # per-subscription bookkeeping, with the milestone-4 close reason
    rows = conn.execute("SELECT * FROM ws_subscriptions ORDER BY since").fetchall()
    assert [r["kind"] for r in rows] == ["curve"]
    assert rows[0]["until"] is not None and rows[0]["notifications"] == 5 and rows[0]["trades"] == 2 and rows[0]["reason"] == "early_buyers"
    assert rows[0]["close_reason"] == "graduated" and rt.counts["closed_graduated"] == 1
    # cost booking under the stream's own ledger methods, per byte (milestone 4): every notification frame
    # (orphan included), the subscribe request and its answer, the unsubscribe request and its answer
    rt.flush_cost()
    rate = settings.config("budget")["sources"]["rpc"]["cu_per_byte"]
    ledger = {r["method"]: r for r in conn.execute("SELECT method, SUM(cost) AS cost, SUM(bytes) AS bytes FROM api_ledger WHERE source='rpc' GROUP BY method")}
    assert set(ledger) == {"ws_trade_notification", "ws_trade_subscribe", "ws_trade_unsubscribe"}
    assert ledger["ws_trade_notification"]["bytes"] == notif_bytes and ledger["ws_trade_notification"]["cost"] == pytest.approx(notif_bytes * rate)
    assert ledger["ws_trade_subscribe"]["bytes"] == sub_bytes and ledger["ws_trade_subscribe"]["cost"] == pytest.approx(sub_bytes * rate)
    assert ledger["ws_trade_unsubscribe"]["bytes"] == unsub_bytes and ledger["ws_trade_unsubscribe"]["cost"] == pytest.approx(unsub_bytes * rate)
    assert rt.stats()["notifications"] == 7  # 7 notification frames ("garbage" is none)
    # a fresh, not-graduated coin: an error on a pending subscribe drops it
    mint2 = pk("mint-2")
    add_coin(conn, mint2, pk("curve-2"))
    watch.add(conn, mint2, "early_buyers", T0 + 21, None, 200)
    msgs = rt.sync_messages(T0 + 21)
    assert msgs[-1]["method"] == "logsSubscribe" and msgs[-1]["params"][0] == {"mentions": [pk("curve-2")]}
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
    assert conn.execute("SELECT COUNT(*) FROM ws_subscriptions WHERE until IS NULL").fetchone()[0] == 0


def test_per_subscription_hourly_cap(settings, conn, fixtures_dir, pp_msgs, payloads):
    """One hot subscription (2,000 notifications in an hour) is dropped for the rest of its hour and counted."""
    exp = payloads["expected"]
    mint = exp["mint"]
    store.upsert_coin(conn, store.pumpportal_create_record(pp_msgs["new_token"]), "pumpportal", T0 - 100)
    watch.add(conn, mint, "early_buyers", T0 - 50, 86400, 200)
    rt, _ = make_client(settings, conn, Clock(), cfg_over={"max_notifications_per_subscription_per_hour": 5, "daily_cu_share": 1.0})
    msgs = rt.sync_messages(T0)
    assert rt.handle_text(json.dumps({"jsonrpc": "2.0", "result": 11, "id": msgs[0]["id"]}), T0 + 1) == "subscribed"
    note = json.loads((fixtures_dir / "logs_notification_trade_curve.json").read_text())
    for i in range(5):
        note["params"]["result"]["value"]["signature"] = f"sig-{i}"
        assert rt.handle_text(json.dumps(note), T0 + 2 + i) in ("trade", "notification")
    note["params"]["result"]["value"]["signature"] = "sig-6"
    assert rt.handle_text(json.dumps(note), T0 + 10) == "capped"
    assert rt.outgoing[0]["method"] == "logsUnsubscribe" and rt.outgoing[0]["params"] == [11]
    assert rt.subs == {} and 11 not in rt.by_sub and rt.muted_until[mint] == T0 + 3600  # the UTC hour of T0
    assert conn.execute("SELECT value FROM kv WHERE key='rpc_trades.subscription_capped'").fetchone()[0] == "1"
    assert json.loads(conn.execute("SELECT value FROM kv WHERE key='rpc_trades.last_capped'").fetchone()[0])["mint"] == mint
    assert conn.execute("SELECT until, close_reason FROM ws_subscriptions").fetchone()[:] == (T0 + 10, "capped")
    # not re-subscribed while muted; back next hour
    assert rt.sync_messages(T0 + 11) == [] and rt.policy["muted"] == 1
    msgs = rt.sync_messages(T0 + 3601)
    assert [m["method"] for m in msgs] == ["logsSubscribe"] and rt.policy["muted"] == 0
    assert conn.execute("SELECT COUNT(*) FROM trades").fetchone()[0] == 5


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
    rows = conn.execute("SELECT until, close_reason FROM ws_subscriptions ORDER BY since").fetchall()
    assert len(rows) >= 2 and all(r["until"] is not None for r in rows)  # every session's row closed at shutdown
    assert [r["close_reason"] for r in rows][:2] == ["disconnect", "disconnect"] or [r["close_reason"] for r in rows][-1] == "shutdown"
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
