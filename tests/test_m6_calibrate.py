"""Milestone 6, point 5: `fart budget calibrate --day D --dashboard-cu N`: the implied CU per byte of the websocket frames."""
from __future__ import annotations

import json

import pytest

from fart import db
from fart.cli import main
from fart.config import load_settings
from fart.net.calibrate import calibrate, round_sig
from fart.util import day_bounds

DAY = "2026-10-27"


def _keyed(tmp_path, monkeypatch):
    for k in ("RPC_HTTP_URL", "RPC_WSS_URL", "RPC_DAILY_BUDGET_CU"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("FART_RULES_VERSION", "test-rules-v1")
    (tmp_path / ".env").write_text("RPC_HTTP_URL=https://solana-mainnet.g.alchemy.com/v2/PLACEHOLDER-NOT-A-REAL-KEY\n"
                                   "RPC_WSS_URL=wss://solana-mainnet.g.alchemy.com/v2/PLACEHOLDER-NOT-A-REAL-KEY\n")
    s = load_settings(root=tmp_path)
    s.ensure_dirs()
    return s


def _ledger(conn):
    t = day_bounds(DAY)[0] + 3600
    rows = [("getTransaction", 40.0, None)] * 100 + [("getSignaturesForAddress", 40.0, None), ("getTokenLargestAccounts", 20.0, None)] * 1
    rows += [("getTokenLargestAccounts", 20.0, None)] * 2 + [("ws_connect", 10.0, None)] * 2
    rows += [("ws_notification", 120.0, 600_000), ("ws_notification", 80.0, 400_000), ("ws_trade_notification", 100.0, 500_000)]
    for i, (m, cost, nbytes) in enumerate(rows):
        conn.execute("INSERT INTO api_ledger(ts, source, method, cost, ok, bytes) VALUES(?, 'rpc', ?, ?, 1, ?)", (t + i, m, cost, nbytes))
    conn.execute("INSERT INTO api_ledger(ts, source, method, cost, ok, blocked_reason) VALUES(?, 'rpc', 'getTransaction', 0, NULL, 'daily_cost_cap')", (t,))  # ignored
    conn.execute("INSERT INTO api_ledger(ts, source, method, cost, ok, bytes) VALUES(?, 'rpc', 'ws_notification', 999, 1, 9999999)", (t + 86400,))  # another day


def test_implied_factor_candidates_and_suggestion(tmp_path, monkeypatch):
    s = _keyed(tmp_path, monkeypatch)
    conn = db.connect(s.db_path)
    _ledger(conn)
    # HTTP at the published rates: 100 x 40 + 40 + 3 x 20 + 2 x 10 (ws_connect is flat) = 4,120; websocket bytes 1,500,000
    c = calibrate(conn, s, DAY, 4120 + 300)
    assert c["source"] == "rpc" and c["http_cu_published"] == 4120 and c["ws_bytes"] == 1_500_000 and c["ws_cu_booked"] == 300
    assert c["implied_cu_per_byte"] == pytest.approx(0.0002) and c["nearest_candidate"] == 0.0002 and c["suggested_cu_per_byte"] == 0.0002
    assert c["config_line"] == '"cu_per_byte": 0.0002' and c["per_day"]["total_cu_at_implied"] == pytest.approx(4420)
    assert c["per_day"]["share_of_cap_at_implied"] == pytest.approx(4420 / 3_000_000) and c["cu_per_byte_current"] == 0.0002
    assert c["trade_stream"] == {"bytes": 500_000, "cu_at_implied": pytest.approx(100.0), "daily_share": 0.35, "share_allowance_cu": 1_050_000.0, "fits_share": True}
    methods = {m["method"]: m for m in c["methods"]}
    assert methods["getTransaction"]["published_cu"] == 4000 and methods["getTransaction"]["rows"] == 100 and methods["ws_notification"]["kind"] == "websocket"
    assert methods["ws_connect"]["kind"] == "http" and methods["ws_connect"]["published_cu"] == 20
    # the pessimistic reading of the Alchemy page
    c2 = calibrate(conn, s, DAY, 4120 + 60_000)
    assert c2["implied_cu_per_byte"] == pytest.approx(0.04) and c2["suggested_cu_per_byte"] == 0.04 and c2["per_day"]["ws_cu_at_implied"] == pytest.approx(60_000)
    assert c2["trade_stream"]["cu_at_implied"] == pytest.approx(20_000) and c2["trade_stream"]["fits_share"] is True
    # no websocket CU on the dashboard at all
    c3 = calibrate(conn, s, DAY, 4120)
    assert c3["implied_cu_per_byte"] == 0.0 and c3["suggested_cu_per_byte"] == 0.0 and "set rpc.cu_per_byte to 0" in c3["note"]
    # something in between: the measured factor rounded to two significant digits
    c4 = calibrate(conn, s, DAY, 4120 + 1500)
    assert c4["implied_cu_per_byte"] == pytest.approx(0.001) and c4["suggested_cu_per_byte"] == 0.001 and c4["suggestion_basis"].startswith("the measured factor")
    assert round_sig(0.000123456) == 0.00012 and round_sig(0.0) == 0.0 and round_sig(123456) == 120000
    # a day without websocket bytes cannot be calibrated
    c5 = calibrate(conn, s, "2026-10-29", 1000)
    assert c5["implied_cu_per_byte"] is None and "no websocket bytes" in c5["note"]
    conn.close()


def test_calibrate_cli_reads_only(tmp_path, monkeypatch, capsys):
    s = _keyed(tmp_path, monkeypatch)
    conn = db.connect(s.db_path)
    _ledger(conn)
    n_rows = conn.execute("SELECT COUNT(*) FROM api_ledger").fetchone()[0]
    conn.close()
    assert main(["--root", str(tmp_path), "--quiet", "budget", "calibrate", "--day", DAY, "--dashboard-cu", "4420"]) == 0
    out = capsys.readouterr().out
    assert "implied CU per byte: 0.0002" in out and '"cu_per_byte": 0.0002' in out and "writes nothing" in out and "PLACEHOLDER" not in out
    assert "trade stream (ws_trade_*): 500,000 bytes = 100 CU" in out
    assert main(["--root", str(tmp_path), "--quiet", "--json", "budget", "calibrate", "--day", DAY, "--dashboard-cu", "64120"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["suggested_cu_per_byte"] == 0.04 and data["dashboard_cu"] == 64120
    assert main(["--root", str(tmp_path), "--quiet", "budget", "calibrate", "--day", "nope", "--dashboard-cu", "1"]) == 2
    capsys.readouterr()
    conn = db.connect(s.db_path)
    assert conn.execute("SELECT COUNT(*) FROM api_ledger").fetchone()[0] == n_rows and db.kv_get(conn, "backup.verify.last") is None
    conn.close()
