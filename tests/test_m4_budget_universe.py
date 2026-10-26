"""Milestone 4, items 2 and 3: the calibrated cost table and per-byte websocket booking, what `fart status`
shows about it, and the complete universe (a PumpPortal migration of a coin fart never saw created is
stored with coin_unknown=1, never watched, counted under grads_without_create)."""
from __future__ import annotations

import json

import pytest

from fart import db
from fart.cli import main
from fart.ingest import coverage, store, watch
from fart.ingest.pumpportal import PumpPortalClient
from fart.ingest.raw import RawWriter
from fart.measure.report import build_report, render_markdown
from fart.net.budget import Budget, load_source_configs
from fart.util import day_bounds, day_key, hour_key

T0 = 1_761_480_000.0  # 2025-10-26T12:00:00Z
CV934 = "Cv934yS1kxVtW3Y6vS8jhk6APKpdtWbzpMcs6VHHpump"


class Clock:
    def __init__(self, t=T0):
        self.t = t

    def __call__(self):
        return self.t


# ----- 2b: the cost model -----------------------------------------------------------------------------------------
def test_rpc_cost_table_matches_the_published_alchemy_figures_and_frames_are_booked_per_byte(settings, conn):
    rpc = settings.config("budget")["sources"]["rpc"]
    assert rpc["cost_table"]["getTransaction"] == 40 and rpc["cost_table"]["getSignaturesForAddress"] == 40
    assert rpc["cost_table"]["getTokenLargestAccounts"] == 20 and rpc["cost_table"]["getAccountInfo"] == 10
    assert rpc["cu_per_byte"] == 0.0002 and "0.04" in rpc["_comment"]  # the unreconciled alternative stays documented
    assert set(rpc["byte_priced"]) == {"ws_notification", "ws_subscribe", "ws_unsubscribe", "ws_trade_notification", "ws_trade_subscribe", "ws_trade_unsubscribe"}
    assert not any(m in rpc["cost_table"] for m in rpc["byte_priced"])  # no flat per-notification estimate any more
    srcs = load_source_configs(settings.config("budget"), {"RPC_DAILY_BUDGET_CU": "3000000"})
    sc = srcs["rpc"]
    assert sc.cost_of("getTransaction") == 40 and sc.cost_of("ws_trade_notification") == 0.0 and sc.is_byte_priced("ws_notification")
    assert sc.cost_of_frames("ws_trade_notification", 1_000_000, 500) == pytest.approx(200.0)  # 1 MB at 0.0002 CU/byte
    assert sc.cost_of_frames("ws_connect", 100, 3) == 30.0  # a flat method stays n x the table
    pub = srcs["rpc_public"]
    assert pub.cu_per_byte is None and pub.cost_of_frames("ws_notification", 1000, 4) == 4.0  # no per-byte price: n x cost_default
    budget = Budget(conn, srcs, settings.lock_path, clock=Clock())
    budget.record("rpc", "ws_trade_notification", sc.cost_of_frames("ws_trade_notification", 250_000, 120), True, None, None, nbytes=250_000)
    budget.record("rpc", "getTransaction", 40.0, True, 200, 12.0)
    rows = {r["method"]: r for r in conn.execute("SELECT method, cost, bytes FROM api_ledger")}
    assert rows["ws_trade_notification"]["bytes"] == 250_000 and rows["ws_trade_notification"]["cost"] == pytest.approx(50.0)
    assert rows["getTransaction"]["bytes"] is None and rows["getTransaction"]["cost"] == 40.0
    assert budget.daily_usage("rpc")["cost"] == pytest.approx(90.0)
    # the pessimistic reading of the Alchemy page is one config value away
    cfg = json.loads(json.dumps(settings.config("budget")))
    cfg["sources"]["rpc"]["cu_per_byte"] = 0.04
    assert load_source_configs(cfg, {})["rpc"].cost_of_frames("ws_notification", 1_000_000, 1) == pytest.approx(40_000.0)


def test_status_shows_open_subscriptions_closed_reasons_and_bytes_with_cu(settings, conn, capsys):
    day = db.day_of(T0)
    import time as _t

    (settings.root / ".env").write_text("RPC_HTTP_URL=https://solana-mainnet.g.alchemy.com/v2/PLACEHOLDER-NOT-A-REAL-KEY\n"
                                        "RPC_WSS_URL=wss://solana-mainnet.g.alchemy.com/v2/PLACEHOLDER-NOT-A-REAL-KEY\n")  # keyed: source `rpc`
    now = _t.time()
    today = now - (now % 86400) + 100
    conn.execute("INSERT INTO ws_subscriptions(mint, address, kind, reason, since, notifications, trades) VALUES"
                 " ('OPEN1xxxxxxxx', 'a', 'curve', 'probation', ?, 50, 3), ('OPEN2xxxxxxxx', 'b', 'curve', 'meta_burst', ?, 500, 30)", (today, today))
    for i, why in enumerate(["envelope"] * 40 + ["capped"] * 2 + ["unwatched"] * 10 + ["graduated"] * 3):
        conn.execute("INSERT INTO ws_subscriptions(mint, address, kind, reason, since, until, close_reason, notifications) VALUES(?, 'c', 'curve', 'meta_burst', ?, ?, ?, 9000)",
                     (f"CLOSED{i}", today, today + 1, why))
    conn.execute("INSERT INTO ws_subscriptions(mint, address, kind, reason, since, until, close_reason) VALUES('OLD', 'd', 'curve', 'probation', ?, ?, 'unwatched')",
                 (today - 90000, today - 86400))  # closed yesterday: not in today's line
    conn.execute("INSERT INTO api_ledger(ts, source, method, cost, ok, bytes) VALUES (?, 'rpc', 'ws_trade_notification', 243.0, 1, 1215000), (?, 'rpc', 'getTransaction', 40, 1, NULL)", (today, today))
    conn.execute("INSERT INTO watchlist(mint, since, reason, active, activated_at) VALUES('W1', 1, 'probation', 1, 1), ('W2', 1, 'meta_burst', 1, 1)")
    conn.execute("INSERT INTO migrations(mint, received_at, source, sources, coin_unknown) VALUES(?, ?, 'pumpportal', '[]', 1)", (CV934, today))
    conn.close()
    assert main(["--root", str(settings.root), "--quiet", "--json", "status"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert [r["mint"] for r in data["top_subscriptions"]] == ["OPEN2xxxxxxxx", "OPEN1xxxxxxxx"]  # open rows only, the hot closed ones never
    assert data["subscriptions_open"] == 2 and data["subscriptions_open_by_reason"] == {"meta_burst": 1, "probation": 1}
    assert data["subscriptions_closed_today"] == {"envelope": 40, "unwatched": 10, "graduated": 3, "capped": 2}
    cu = {r["method"]: r for r in data["cu_by_method_today"]}
    assert cu["ws_trade_notification"]["bytes"] == 1215000 and cu["ws_trade_notification"]["cost"] == 243 and cu["getTransaction"]["bytes"] is None
    assert data["cu_per_byte"] == 0.0002 and data["watchlist_by_reason"] == {"meta_burst": 1, "probation": 1}
    assert data["migrations_coin_unknown"] == {"total": 1, "today": 1} and data["schema_v4_cleanup"]["counts"]["watchlist_graduation_closed"] == 0
    assert main(["--root", str(settings.root), "--quiet", "status"]) == 0
    out = capsys.readouterr().out
    assert "subscriptions open: 2 (meta_burst 1, probation 1); closed today: envelope 40, unwatched 10, graduated 3, capped 2" in out
    assert "top open subscriptions (notifications): OPEN2xxx… curve/meta_burst 500 (trades 30); OPEN1xxx… curve/probation 50 (trades 3)" in out
    assert "ws_trade_notification 243 CU = 1,215,000 bytes (1 batches)" in out and "getTransaction 40 (1 req)" in out and "[cu_per_byte 0.0002]" in out
    assert "graduations without a known create (coin_unknown, never watched): 1 total, 1 today" in out
    assert "schema v4 cleanup" in out and "PLACEHOLDER-NOT-A-REAL-KEY" not in out and day


# ----- 3: the universe stays complete ------------------------------------------------------------------------------
def make_pp(settings, conn):
    srcs = load_source_configs(settings.config("budget"), {"RPC_DAILY_BUDGET_CU": "100000"})
    budget = Budget(conn, srcs, settings.lock_path, clock=Clock())
    raw = RawWriter(settings.raw_dir, "pumpportal", flush_interval_s=0)
    return PumpPortalClient(conn, budget, raw, "wss://x", settings.config("ingest")["pumpportal"], settings.config("engine")["watchlist"],
                            connect=lambda *a, **k: None, clock=Clock(), trades_enabled=False, pumpfun_cfg=settings.config("pumpfun"))


def test_pumpportal_migration_of_a_coin_created_before_fart_is_stored_flagged_and_never_watched(settings, conn, fixtures_dir):
    fx = json.loads((fixtures_dir / "pumpportal_migration_unknown_coin.json").read_text())
    msg = fx["migration"]
    assert msg["mint"] == CV934 and store.classify_pumpportal(msg) == "migrate"
    pp = make_pp(settings, conn)
    assert pp.handle_text(json.dumps(msg), T0) == "migrate"
    row = conn.execute("SELECT * FROM migrations").fetchone()
    assert row["mint"] == CV934 and row["coin_unknown"] == 1 and row["source"] == "pumpportal" and row["signature"] == msg["signature"]
    assert conn.execute("SELECT COUNT(*) FROM rejected_messages").fetchone()[0] == 0  # not a refusal any more
    assert conn.execute("SELECT COUNT(*) FROM watchlist").fetchone()[0] == 0  # never watched
    assert not watch.is_watched(conn, CV934)
    assert pp.counts["migrate_coin_unknown"] == 1 and "migrate_rejected" not in pp.counts
    assert db.kv_get(conn, "pp.migration_coin_unknown") == "1" and db.kv_get(conn, "ingest.rejected.pumpportal.unknown_coin") is None
    # the universe: the coverage hour and the daily report count it under grads_without_create
    db.kv_set(conn, "ingest.pumpportal.first_started", str(T0 - 86400), T0)
    g = coverage.gap_open(conn, "pumpportal", T0 - 86400, "startup")
    coverage.gap_close(conn, g, T0 - 86400 + 2)
    h = coverage.compute_hour(conn, hour_key(T0), T0 + 3600)
    assert h["grads"] == 1 and h["grads_without_create"] == 1
    day = day_key(T0)
    rep = build_report(conn, settings, day, T0 + 2 * 86400)
    assert rep["universe"]["graduations"] == 1 and rep["universe"]["without_create"] == 1 and rep["ops"]["migrations_coin_unknown"] == 1
    assert rep["unknowns"]["graduations_without_create"] == 1 and rep["graduations"][0]["mint"] == CV934
    md = render_markdown(rep)
    assert "waarvan zonder bekende create: 1" in md and "Graduaties zonder bekende create (coin_unknown, nooit gevolgd): 1" in md
    # a duplicate from the RPC stream merges into the same row and keeps the flag
    assert store.upsert_migration(conn, {"mint": CV934, "signature": msg["signature"], "pool_address": "Pool1", "dex": "pump-amm", "slot": 5, "block_time": T0 - 1}, "rpc_logs", T0 + 1) is False
    row = conn.execute("SELECT coin_unknown, pool_address, sources FROM migrations").fetchone()
    assert row[:] == (1, "Pool1", '["pumpportal", "rpc_logs"]')
    # the create arrives later (sweep + backfill, or a replay): the graduation is complete now and the flag clears
    assert store.upsert_coin(conn, {"mint": CV934, "name": "Older coin", "symbol": "OLD", "creator": "Cr"}, "rpc_backfill", T0 + 2)
    assert conn.execute("SELECT coin_unknown FROM migrations").fetchone()[0] == 0
    assert coverage.compute_hour(conn, hour_key(T0), T0 + 3600)["grads_without_create"] == 0
    # a real refusal is still a refusal: a system mint in the same message shape
    pp.handle_text(json.dumps(dict(msg, mint=settings.config("pumpfun")["system_mints"]["USDC"], signature="s-usdc")), T0 + 3)
    assert conn.execute("SELECT reason FROM rejected_messages").fetchone()[0] == "system_mint:USDC"
    assert conn.execute("SELECT COUNT(*) FROM migrations").fetchone()[0] == 1
    # migration_rejection has no unknown-coin reason any more
    assert store.migration_rejection(conn, "SomeMintNeverSeen111111111111111111111111111", settings.config("pumpfun")) is None
    assert store.migration_rejection(conn, None, settings.config("pumpfun")) == "no_mint"
    assert day_bounds(day)[0] <= T0
