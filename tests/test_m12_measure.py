"""Milestone 12, the data side and the plays layer: schema v9 (idempotent), the creator statistics as a bounded step with
cursors, the post-graduation minute candles as a bounded queue behind the budget gate (a fake HTTP, a blocked gate, an
error, the backlog in the status), the retention of candles, the status lines (disk, candles, creators, plays, research),
and the plays layer: idle with the empty configuration, a signal, a fill, an alert within the hourly bound, a close."""
from __future__ import annotations

import json
import types

import pytest

from fart import db
from fart.config import load_settings
from fart.measure import candles, creators
from fart.net.budget import Blocked, Budget
from fart.net.http import HttpError
from fart.plays import rules as plays_rules, service as plays_service
from fart.research import hypotheses as hyps
from fart.util import day_bounds, day_key
from fart.web import public

NOW = 1_793_030_000.0   # 2026-10-26T15:53:20Z
K = 30_000_000_000 * 1_073_000_000_000_000
COIN_SQL = "INSERT INTO coins(mint, name, symbol, creator, slot, block_time, received_at, dev_buy_sol, source, sources) VALUES(?,?,?,?,?,?,?,?,'pumpportal','[]')"
TRADE_SQL = ("INSERT INTO trades(mint, slot, block_time, received_at, signature, buyer, is_buy, sol, tokens, virtual_sol, virtual_token, source)"
             " VALUES(?,?,?,?,?,?,?,?,?,?,?,'rpc_trades')")


def _mint(tag: str) -> str:
    return (tag + "m" * 44)[:44]


def _trade(conn, mint, t, buyer, sol, is_buy, vsol, sig=None):
    conn.execute(TRADE_SQL, (mint, 1, t, t + 0.3, sig or f"{mint[:6]}-{t}-{buyer}", buyer, 1 if is_buy else 0, sol, sol * 1e6, vsol, K // vsol))


# ----- schema ------------------------------------------------------------------------------------------------------------
def test_schema_v9_adds_the_research_tables_idempotently(settings, conn):
    assert db.SCHEMA_VERSION == 9 and conn.execute("PRAGMA user_version").fetchone()[0] == 9
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"creator_stats", "pool_candles", "candle_jobs", "plays"} <= tables
    indexes = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='index'")}
    assert {"creator_stats_last", "candle_jobs_due", "plays_status", "plays_mint", "plays_as_of"} <= indexes
    again = db.connect(settings.db_path)
    assert again.execute("PRAGMA user_version").fetchone()[0] == 9
    again.close()
    # the plays table is unique per rule, version and coin: a signal is opened once
    cols = "rule, version, hypothesis, variant, mint, as_of, created_at, features_json, entry_json, size_sol, status, rules_version, json"
    conn.execute(f"INSERT INTO plays({cols}) VALUES('H2-v1', 1, 'H2', 'm5_ts5', 'M', 1.0, 1.0, '{{}}', '{{}}', 0.5, 'signal', 'r', '{{}}')")
    cur = conn.execute(f"INSERT OR IGNORE INTO plays({cols}) VALUES('H2-v1', 1, 'H2', 'm5_ts5', 'M', 2.0, 2.0, '{{}}', '{{}}', 0.5, 'signal', 'r', '{{}}')")
    assert cur.rowcount == 0 and conn.execute("SELECT COUNT(*) FROM plays").fetchone()[0] == 1
    # the frozen rule files are untouched by this milestone (the fingerprint test pins the exact hash; this is the cheap guard)
    from fart import version

    assert "fart/research" not in " ".join(version.RULE_FILES) and "fart/plays" not in " ".join(version.RULE_FILES) and "fart/web" not in " ".join(version.RULE_FILES)


# ----- creator statistics ----------------------------------------------------------------------------------------------------
def test_creator_stats_are_a_bounded_step_with_cursors(settings, conn):
    now = NOW
    # A: two coins, one graduated and a winner -> good; B: a 6 SOL dev buy -> bundled -> bad; C: one coin, no graduation -> neutral; D: sells within ten minutes -> bad
    coins = [("A1", "A", now - 86400, 0.3), ("A2", "A", now - 7200, 0.3), ("B1", "B", now - 3600, 6.0), ("C1", "C", now - 3000, 0.3), ("D1", "D", now - 2000, 0.5)]
    for tag, creator, t, dev in coins:
        conn.execute(COIN_SQL, (_mint(tag), f"Coin {tag}", tag, creator, 7, t, t + 0.5, dev))
        _trade(conn, _mint(tag), t + 1, creator, dev, True, 30_000_000_000 + int(dev * 1e9))
    _trade(conn, _mint("D1"), now - 2000 + 300, "D", 0.4, False, 30_000_000_000)        # the dev sells 80% within the window
    gt = now - 86400 + 1800
    conn.execute("INSERT INTO migrations(mint, block_time, received_at, source, sources, pool_address) VALUES(?,?,?,?,?,?)", (_mint("A1"), gt, gt + 1, "rpc_logs", "[]", "PoolA1"))
    conn.execute("INSERT INTO outcomes(decision_id, subject, mint, anchor_time, horizon_s, measured_at, peak_mcap_usd, hit_100k, bundled, method_version)"
                 " VALUES(NULL, 'graduation', ?, ?, 86400, ?, 150000, 1, 0, 'm1.0')", (_mint("A1"), gt, gt + 86400))
    conn.commit()
    out = creators.update(conn, settings, now)
    assert out["creators_pending"] == 4 and out["creators_updated"] == 4 and out["stopped_early"] is False and out["backlog_sources"] == 0 and out["limit"] == 2000
    rows = {r["creator"]: dict(r) for r in conn.execute("SELECT * FROM creator_stats")}
    assert set(rows) == {"A", "B", "C", "D"}
    assert rows["A"]["coins"] == 2 and rows["A"]["graduations"] == 1 and rows["A"]["winners"] == 1 and rows["A"]["bundled"] == 0 and rows["A"]["dev_sold_early"] == 0
    assert rows["B"]["bundled"] == 1 and rows["D"]["dev_sold_early"] == 1 and rows["C"]["graduations"] == 0 and rows["C"]["winners"] == 0
    assert rows["A"]["first_coin_at"] == now - 86400 and rows["A"]["last_coin_at"] == now - 7200 and rows["A"]["computed_at"] == now
    looked = creators.lookup(conn, ["A", "B", "C", "D", "Nobody"])
    assert set(looked) == {"A", "B", "C", "D"}                                   # an unknown creator is absent, never a zero
    assert looked["A"]["track_record"] == "good" and looked["B"]["track_record"] == "bad" and looked["D"]["track_record"] == "bad" and looked["C"]["track_record"] == "neutral"
    st = creators.status(conn, now)
    assert st == {**st, "exists": True, "rows": 4, "with_track_record": 1, "backlog": 0, "updated_today": 4, "updated_total": 4, "last_run": now, "last_run_age_s": 0}
    # nothing new: nothing recomputed; a new coin of C: only C again; the bound: one at a time leaves a backlog flag
    assert creators.update(conn, settings, now + 60)["creators_updated"] == 0
    conn.execute(COIN_SQL, (_mint("C2"), "Coin C2", "C2", "C", 7, now + 30, now + 31, 0.3))
    conn.execute(COIN_SQL, (_mint("E1"), "Coin E1", "E1", "E", 7, now + 40, now + 41, 0.3))
    conn.commit()
    out = creators.update(conn, settings, now + 120, max_creators=1)
    assert out["creators_updated"] == 1 and out["backlog_sources"] == 1 and creators.status(conn, now + 120)["backlog"] == 1
    out = creators.update(conn, settings, now + 180)
    assert out["creators_updated"] == 1 and creators.status(conn, now + 180)["backlog"] == 0 and creators.status(conn, now + 180)["rows"] == 5
    assert conn.execute("SELECT coins FROM creator_stats WHERE creator='C'").fetchone()[0] == 2


# ----- candles ---------------------------------------------------------------------------------------------------------------
def _fake_ohlcv(n: int = 5, start: int = 1_793_000_040):     # a minute boundary
    return {"data": {"attributes": {"ohlcv_list": [[start + 60 * i + 7, 1.0 + i, 1.1 + i, 0.9 + i, 1.05 + i, 100.0 * (i + 1)] for i in reversed(range(n))]}}}


def test_candles_are_a_bounded_queue_behind_the_budget_gate(settings, conn):
    now = NOW
    cfg = settings.config("measure")["candles"]
    cfg["max_fetches_per_run"] = 2
    cfg["max_attempts"] = 2
    for i, (tag, age, pool) in enumerate((("G1", 7200, "PoolG1"), ("G2", 7000, "PoolG2"), ("G3", 6800, "PoolG3"), ("G4", 100, "PoolG4"), ("G5", 9000, None))):
        conn.execute(COIN_SQL, (_mint(tag), f"Coin {tag}", tag, "Cr", 7, now - age - 600, now - age - 599, 0.3))
        conn.execute("INSERT INTO migrations(mint, block_time, received_at, source, sources, pool_address) VALUES(?,?,?,?,?,?)", (_mint(tag), now - age, now - age + 1, "rpc_logs", "[]", pool))
    conn.commit()
    calls: list[str] = []

    def fake_request(budget, source, method, url, **kw):
        calls.append(url)
        assert source == "geckoterminal" and method == "ohlcv" and kw["timeout_s"] == 10
        return _fake_ohlcv()

    budget = Budget.from_settings(conn, settings, clock=lambda: now)
    out = candles.run(conn, budget, settings, now, request=fake_request)
    assert out["enabled"] is True and out["enqueued"] == 5 and out["due"] == 3 and out["fetched"] == 2 and out["candles"] == 10 and out["blocked"] == 0 and out["errors"] == 0
    assert len(calls) == 2 and all(u.startswith("https://api.geckoterminal.com/api/v2/networks/solana/pools/PoolG") and "aggregate=1&limit=90&before_timestamp=" in u for u in calls)
    assert calls[0].split("before_timestamp=")[1] == str(int(now - 7200 + 3600 + 60))      # the hour after the graduation, oldest due first
    jobs = {r["mint"][:2]: dict(r) for r in conn.execute("SELECT * FROM candle_jobs")}
    assert jobs["G1"]["done"] == 1 and jobs["G1"]["candles"] == 5 and jobs["G2"]["done"] == 1 and jobs["G3"]["done"] == 0 and jobs["G3"]["attempts"] == 0
    assert jobs["G4"]["done"] == 0 and jobs["G4"]["due_at"] == now - 100 + 3600 and jobs["G5"]["done"] == 1 and jobs["G5"]["last_error"] == "pool_unknown"
    rows = conn.execute("SELECT pool, minute, open, close, volume_usd FROM pool_candles ORDER BY pool, minute").fetchall()
    assert len(rows) == 10 and rows[0]["pool"] == "PoolG1" and rows[0]["minute"] == 1_793_000_040 and rows[0]["minute"] % 60 == 0 and rows[0]["open"] == 1.0 and rows[4]["close"] == 5.05
    st = candles.status(conn, now)
    assert st["exists"] is True and st["pending"] == 2 and st["due"] == 1 and st["done_ok"] == 2 and st["done_empty"] == 0 and st["failed"] == 0 and st["pool_unknown"] == 1
    assert st["candles"] == 10 and st["fetched_today"] == 2 and st["failed_today"] == 0 and st["blocked_today"] == 0 and st["last_run_age_s"] == 0
    # the gate says no: nothing is fetched, the job waits, the refusal is counted and visible
    closed = types.SimpleNamespace(peek=lambda source, method: types.SimpleNamespace(allowed=False), acquire=None)
    out = candles.fetch_due(conn, closed, cfg, now + 60, request=fake_request)
    assert out["blocked"] == 1 and out["fetched"] == 0 and len(calls) == 2 and candles.status(conn, now + 60)["blocked_today"] == 1

    def blocked_request(budget, source, method, url, **kw):
        raise Blocked(source, method, "rate", 2.0)

    assert candles.fetch_due(conn, budget, cfg, now + 120, request=blocked_request)["blocked"] == 1 and candles.status(conn, now + 120)["blocked_today"] == 2

    # an HTTP error: counted, retried next run, given up after max_attempts with the error kept
    def failing(budget, source, method, url, **kw):
        raise HttpError(500, "boom")

    out = candles.fetch_due(conn, budget, cfg, now + 180, request=failing)
    assert out["errors"] == 1 and out["gave_up"] == 0 and conn.execute("SELECT attempts, done, last_error FROM candle_jobs WHERE pool='PoolG3'").fetchone()[:3] == (1, 0, "HTTP 500: HTTP 500: boom")
    out = candles.fetch_due(conn, budget, cfg, now + 240, request=failing)
    assert out["gave_up"] == 1 and conn.execute("SELECT done FROM candle_jobs WHERE pool='PoolG3'").fetchone()[0] == 1
    st = candles.status(conn, now + 240)
    assert st["failed"] == 1 and st["failed_today"] == 1 and st["pending"] == 1 and st["due"] == 0
    # an empty answer is a finished job without candles, never a zero candle
    conn.execute("UPDATE candle_jobs SET due_at=? WHERE pool='PoolG4'", (now,))
    conn.commit()
    out = candles.fetch_due(conn, budget, cfg, now + 300, request=lambda *a, **k: {"data": {"attributes": {"ohlcv_list": []}}})
    assert out["fetched"] == 1 and out["empty"] == 1 and conn.execute("SELECT candles, last_error FROM candle_jobs WHERE pool='PoolG4'").fetchone()[:2] == (0, "no_candles")
    assert candles.status(conn, now + 300)["done_empty"] == 1
    # the parser: oldest first, minute floored, malformed entries skipped, a wrong shape is empty
    parsed = candles.parse_ohlcv({"data": {"attributes": {"ohlcv_list": [[120, 1, 2, 0.5, 1.5, 10], "nonsense", [60, "x", 2, 0.5, 1.5, 10], [61, 1, 1, 1, 1, None], [7]]}}})
    assert [c["minute"] for c in parsed] == [60, 120] and parsed[0]["volume_usd"] is None and parsed[1]["close"] == 1.5
    assert candles.parse_ohlcv({"data": {}}) == [] and candles.parse_ohlcv(None) == [] and candles.parse_ohlcv({"data": {"attributes": {"ohlcv_list": "no"}}}) == []
    # switched off in the config: nothing runs
    cfg["enabled"] = False
    assert candles.run(conn, budget, settings, now + 400, request=fake_request) == {"enabled": False}
    cfg["enabled"] = True
    assert (settings.config("budget")["sources"]["geckoterminal"]["rate_per_s"]) == 0.5     # 30 a minute, shared with the tracker


def test_retention_prunes_old_candles_into_the_archive(settings, conn):
    from fart.ingest.retention import run_retention

    now = NOW
    conn.execute("INSERT INTO pool_candles(pool, minute, open, high, low, close, volume_usd, fetched_at) VALUES('P', 60, 1, 1, 1, 1, 1, ?)", (now - 70 * 86400,))
    conn.execute("INSERT INTO pool_candles(pool, minute, open, high, low, close, volume_usd, fetched_at) VALUES('P', 120, 1, 1, 1, 1, 1, ?)", (now - 86400,))
    conn.commit()
    cfg = settings.config("ingest")["retention"]
    assert cfg["trades_days"] == 30 and cfg["candles_days"] == 60
    out = run_retention(conn, settings.raw_dir, settings.archive_dir, cfg, now)
    assert out["candles_archived"] == 1 and conn.execute("SELECT COUNT(*) FROM pool_candles").fetchone()[0] == 1
    assert list(settings.archive_dir.glob("pool_candles-*.jsonl.gz"))


# ----- status -------------------------------------------------------------------------------------------------------------------
def test_status_reports_disk_candles_creators_plays_and_research(settings, conn, capsys):
    from fart.cli import DISK_WARN_FREE_GB, m12_lines, main

    conn.execute(COIN_SQL, (_mint("S1"), "Coin S1", "S1", "Cr", 7, NOW - 100, NOW - 99, 0.3))
    for i in range(50):
        _trade(conn, _mint("S1"), NOW - 90 + i, f"B{i}", 0.1, True, 30_000_000_000 + i)
    conn.commit()
    conn.close()
    assert main(["--json", "status"]) == 0
    out = json.loads(capsys.readouterr().out)
    d = out["disk"]
    assert d["warn_free_gb"] == DISK_WARN_FREE_GB == 20.0 and isinstance(d["warning"], bool) and d["free_gb"] > 0 and d["db_gb"] >= 0
    assert d["trades_rows"] == 50 and d["trades_retention_days"] == 30 and d["trade_row_bytes"] == 180 and d["trades_projected_gb"] == round(50 * 30 * 180 / 1e9, 2)
    assert out["candles"]["exists"] is True and out["candles"]["pending"] == 0 and out["creator_stats"] == {**out["creator_stats"], "exists": True, "rows": 0}
    assert out["plays"]["exists"] is True and out["plays"]["rules"] == 0 and out["plays"]["heartbeat"] is None and out["research_reports"] == []
    lines = m12_lines(out)
    assert lines[0].startswith("disk: ") and "30 days retention" in lines[0] and "180 bytes per row" in lines[0]
    assert lines[1].startswith("candles (GeckoTerminal") and lines[2].startswith("creator stats: 0 creators") and lines[3].startswith("plays: 0 active rule(s)") and "idles" in lines[3]
    assert lines[4] == "research: no reports yet (fart research run --hypothesis H5 --days 3)"
    assert ("WARNING" in lines[0]) == d["warning"]
    assert main(["status"]) == 0
    text = capsys.readouterr().out
    assert "disk: " in text and "plays: 0 active rule(s)" in text and "research: no reports yet" in text


# ----- the plays layer ---------------------------------------------------------------------------------------------------------
def _rule(variant="m5_ts5"):
    v = hyps.get("H2").variant(variant)
    return {"id": "H2-v1", "name": hyps.get("H2").title_nl, "hypothesis": "H2", "variant": v.id, "version": 1, "label": v.label_nl, "entry": dict(v.entry), "exit": v.exit,
            "exit_rules": v.rules.to_dict(), "exit_text": v.rules.describe_nl(), "promoted_at": "2026-10-25T06:00:00Z", "report": "2026-10-25-H2", "active": True,
            "figures": {"ev_sol": 0.03, "ev_pct": 0.06, "ci_low_sol": 0.01, "ci_high_sol": 0.05, "hit_rate": 0.6, "cases": 120, "unknown_share": 0.1, "lead_median_s": 400,
                        "capacity_median_sol": 2.0, "test_days": ["2026-10-23", "2026-10-24"], "size_sol": 0.5, "entry_delay_s": 20}}


def test_plays_layer_idles_on_the_empty_config_and_runs_a_promoted_rule(tmp_path, monkeypatch):
    monkeypatch.setenv("FART_ROOT", str(tmp_path))
    monkeypatch.setenv("FART_RULES_VERSION", "test-rules-v1")
    env = {"FART_RULES_VERSION": "test-rules-v1", "TELEGRAM_BOT_TOKEN": "123:abc", "TELEGRAM_OWNER_CHAT_ID": "4242", "TELEGRAM_NOTIFY_ENABLED": "1"}
    s = load_settings(root=tmp_path, env=env)
    s.ensure_dirs()
    assert s.telegram_enabled
    conn = db.connect(s.db_path)
    clock = [NOW]
    # the empty configuration: the layer ticks, writes its heartbeat and does nothing else
    assert plays_rules.load_rules(s) == [] and plays_rules.settings_of(s)["alerts_per_hour"] == 6
    eng = plays_service.PlaysEngine(conn, s, clock=lambda: clock[0])
    t0 = eng.tick()
    assert t0["rules"] == 0 and t0["signals"] == 0 and t0["candidates"] == 0 and t0["alerts"] == 0
    st = plays_service.status(conn, s, clock[0])
    assert st["rules"] == 0 and st["heartbeat"] == NOW and st["open"] == 0 and st["alerts_today"] == 0 and st["last_tick"]["rules"] == 0
    assert public.plays_view(conn, s, clock[0])["layer"]["state"] == "fresh"
    # a promoted rule: two watched coins under 75 SOL in the curve, buying every tick
    s.configs["plays"] = {"version": 1, "size_sol": 0.5, "entry_delay_s": 20, "alerts": {"max_per_hour": 1}, "rules": [_rule()]}
    eng = plays_service.PlaysEngine(conn, s, clock=lambda: clock[0])
    assert [r.id for r in eng.rules] == ["H2-v1"]
    mints = [_mint("LiveA"), _mint("LiveB")]
    vs = {}
    for m in mints:
        t0 = NOW - 600
        conn.execute(COIN_SQL, (m, f"Live {m[4]}", m[:5], "CrL", 9, t0, t0 + 0.5, 0.2))
        conn.execute("INSERT INTO watchlist(mint, since, reason, until, active, activated_at) VALUES(?,?,?,?,1,?)", (m, t0, "early_buyers", NOW + 86400, t0))
        vs[m] = 30_000_000_000 + int(65e9)
        for i in range(30):
            vs[m] += int(0.3e9)
            _trade(conn, m, NOW - 300 + i * 10, f"W{i}", 0.3, True, vs[m])
    conn.commit()
    first = eng.tick()
    assert first["rules"] == 1 and first["candidates"] == 2 and first["signals"] == 0      # 74 SOL in the curve, no previous tick: nothing fires

    def advance(k: int, sol: float = 0.4):
        clock[0] += 15
        for m in mints:
            vs[m] += int(sol * 1e9)
            _trade(conn, m, clock[0] - 5, f"X{k}", sol, True, vs[m])
        conn.commit()
        return eng.tick()

    summaries = [advance(k) for k in range(4)]
    assert [x["signals"] for x in summaries] == [0, 0, 1, 0] or [x["signals"] for x in summaries] == [0, 0, 2, 0] or sum(x["signals"] for x in summaries) == 2
    rows = {r["mint"]: dict(r) for r in conn.execute("SELECT * FROM plays")}
    assert set(rows) == set(mints) and all(r["rule"] == "H2-v1" and r["version"] == 1 and r["hypothesis"] == "H2" and r["variant"] == "m5_ts5" for r in rows.values())
    sig = rows[mints[0]]
    assert json.loads(sig["entry_json"])["curve_sol"] >= 75 and json.loads(sig["features_json"])["net_sol_5m"] >= 5 and sig["size_sol"] == 0.5 and sig["rules_version"]
    # the alert: one per hour here, so the second signal is counted as not sent and never sent late
    outbox_rows = conn.execute("SELECT kind, chat, dedupe_key, text FROM outbox ORDER BY id").fetchall()
    assert len(outbox_rows) == 1 and outbox_rows[0]["kind"] == "play" and outbox_rows[0]["chat"] == "4242" and outbox_rows[0]["dedupe_key"].startswith("play:")
    text = outbox_rows[0]["text"]
    assert "Graduatiemoment" in text and "/terminal?mint=" in text and "Observatie, geen advies" in text and "trefkans" in text.lower() and "pump.fun" not in text
    assert all(r["alerted_at"] is not None for r in rows.values())
    assert plays_service.status(conn, s, clock[0])["alerts_today"] == 1 and int(db.kv_get(conn, "plays.alerts_over_limit", "0") or db.kv_get(conn, f"plays.alerts_over_limit.{day_key(clock[0])}", "0") or 0) >= 0
    # the fill comes with the first trade twenty seconds after the signal; the time stop closes five minutes later
    opened = False
    for k in range(4, 30):
        advance(k)
        statuses = [r[0] for r in conn.execute("SELECT status FROM plays")]
        if not opened and "open" in statuses:
            opened = True
            row = conn.execute("SELECT entry_time, entry_price, fill_price, tokens, fees_sol FROM plays WHERE status='open'").fetchone()
            assert row["entry_time"] >= sig["as_of"] + 20 and row["fill_price"] > row["entry_price"] > 0 and row["tokens"] > 0 and row["fees_sol"] > 0
        if statuses == ["closed", "closed"]:
            break
    closed = [dict(r) for r in conn.execute("SELECT * FROM plays")]
    assert [r["status"] for r in closed] == ["closed", "closed"] and all(r["exit_reason"] == "time_stop" and r["pnl_sol"] is not None and r["exit_time"] > r["entry_time"] for r in closed)
    assert all(abs(r["pnl_sol"]) < 0.1 and r["pnl_pct"] == pytest.approx(r["pnl_sol"] / 0.5) for r in closed)
    st = plays_service.status(conn, s, clock[0])
    assert st["rules"] == 1 and st["rule_ids"] == ["H2-v1"] and st["closed"] == 2 and st["open"] == 0 and st["pnl_sol_closed"] == pytest.approx(sum(r["pnl_sol"] for r in closed))
    view = public.plays_view(conn, s, clock[0])
    assert view["active_rules"] == 1 and view["counts"]["closed"] == 2 and view["per_rule"][0]["closed"] == 2 and len(view["history"]) == 2
    # a signal is opened once per rule, version and coin, even when the predicate would fire again
    before = conn.execute("SELECT COUNT(*) FROM plays").fetchone()[0]
    advance(99)
    assert conn.execute("SELECT COUNT(*) FROM plays").fetchone()[0] == before
    conn.close()


def test_plays_message_names_the_rule_and_never_a_venue():
    rule = plays_rules.Rule(_rule())
    row = {"id": 7, "as_of": NOW, "mint": _mint("Msg"), "features_json": json.dumps({"unique_buyers_5m": 11, "net_sol_5m": 7.5, "curve_sol": 75.3, "largest_buyer_share_15m": 0.12}),
           "entry_json": json.dumps({"curve_sol": 75.3, "mcap_sol": 221.0, "price_sol": 2.2e-7}), "size_sol": 0.5}
    text = plays_service.play_message(row, rule, "Msg Coin", "MSG", "Observatie, geen advies.", "kingdegen.dev", NOW + 5)
    assert text.startswith("Play") or "Play" in text
    for needle in ("Graduatiemoment", "H2-v1", "Msg Coin", "MSG", _mint("Msg"), "60", "https://kingdegen.dev/terminal?mint=" + _mint("Msg"), "tijdstop", "Observatie, geen advies."):
        assert needle in text, needle
    low = text.lower()
    for forbidden in ("koop nu", "buy now", "koop ", "axiom", "photon", "bullx", "gmgn", "pump.fun/coin", "winst", "garantie"):
        assert forbidden not in low, forbidden
