"""Milestone 6, point 3: `fart engine explain --day` and the daily report section "Waarom wel of geen observaties",
from the per-day candidate maxima the engine writes (engine_candidates). Read-only; thresholds unchanged; no advice."""
from __future__ import annotations

import json

from fart import db
from fart.cli import main
from fart.engine.explain import build_explain, markdown_lines, percentiles, render_text
from fart.engine.service import Engine
from fart.ingest import coverage as cov
from fart.ingest import watch
from fart.ingest.retention import run_retention
from fart.measure.report import build_report, render_markdown
from fart.util import day_key
from tests.test_engine import T0, add_coin, add_trade

DAY = day_key(T0)  # 2025-10-26, hour 12 UTC


def _world(conn):
    add_coin(conn, "M", T0, name="Goldberg One", dev_buy=1.0)          # 30 buyers, 15 SOL: passes every coin threshold
    watch.add(conn, "M", "probation", T0, 300, 200)
    for i in range(30):
        add_trade(conn, "M", T0 + 1 + i, f"B{i}", 0.5, sig=f"b{i}", vsol=(31 + i * 0.5) * 1e9, vtok=1.0e15)
    add_coin(conn, "Q", T0 + 5, name="Quiet", symbol="QT", dev_buy=None)  # probation without a single trade
    watch.add(conn, "Q", "probation", T0 + 5, 300, 200)
    for i in range(3):
        add_coin(conn, f"G{i}", T0 - 100 * (i + 1), name=f"Goldberg {i}", creator=f"C{i}")
    g = cov.gap_open(conn, "rpc_trades", T0 + 100, "budget_cap")  # ten minutes without the trade stream in hour 12
    cov.gap_close(conn, g, T0 + 700)
    db.counter_bump_daily(conn, "rpc_trades.envelope_paused", 2, T0 + 50)


def test_engine_writes_candidate_maxima_and_explain_counts_them(settings, conn):
    _world(conn)
    eng = Engine(conn, settings, rpc=None)
    s1 = eng.tick(T0 + 40)
    assert s1["coin_decisions"] == 1 and s1["candidates_written"] == 2 + s1["metas"]  # M, Q and every ranked meta
    s2 = eng.tick(T0 + 55)  # the meta_burst coins G0..G2 are watched now and get a row
    assert s2["coin_decisions"] == 0 and s2["candidates_written"] == 5 + s2["metas"]
    rows = {r["key"]: dict(r) for r in conn.execute("SELECT * FROM engine_candidates WHERE day=? AND kind='coin'", (DAY,))}
    m = rows["M"]
    assert m["ticks"] == 2 and m["reason"] == "probation" and m["max_unique_buyers_5m"] == 30 and m["max_net_sol_5m"] == 15.0
    assert m["passed_momentum"] == 1 and m["passed_all"] == 1 and m["max_trades_total"] == 30 and m["bundled_ever"] == 0
    assert m["bundled_last"] is None  # 55 s old: an instant graduation cannot be ruled out yet (features.py, unchanged)
    assert 0 < m["min_largest_buyer_share_15m"] <= m["max_largest_buyer_share_15m"] <= 0.35
    assert rows["Q"]["max_trades_total"] == 0 and rows["Q"]["bundled_last"] is None and rows["Q"]["passed_momentum"] == 0
    assert {rows[f"G{i}"]["reason"] for i in range(3)} == {"meta_burst"} and rows["G0"]["ticks"] == 1
    meta = conn.execute("SELECT * FROM engine_candidates WHERE day=? AND kind='meta' AND key='goldberg'", (DAY,)).fetchone()
    assert meta["ticks"] == 2 and meta["burst_inf"] == 1 and meta["max_sol_inflow"] == 15.0 and meta["max_unique_buyers"] == 30
    assert meta["max_n_coins"] == 4 and meta["max_distinct_creators"] == 4 and meta["passed_momentum"] == 1 and meta["passed_all"] == 0  # 15 SOL < 20
    # the maxima never shrink: a tick with fewer buyers keeps 30; the bundled tri-state follows the last tick (now known false)
    eng.tick(T0 + 3600)
    assert conn.execute("SELECT max_unique_buyers_5m, ticks, bundled_last FROM engine_candidates WHERE key='M'").fetchone()[:] == (30, 3, 0)

    e = build_explain(conn, settings, DAY, T0 + 7200)
    c = e["coins"]
    assert e["available"] and c["candidates"] == 5 and c["by_reason"] == {"meta_burst": 3, "probation": 2}
    assert c["with_trades"] == 1 and c["probation"] == 2 and c["probation_with_trades"] == 1 and c["probation_trade_share"] == 0.5
    met = {t["key"]: t["met"] for t in c["thresholds"]}
    assert met == {"unique_buyers_5m": 1, "net_sol_5m": 1, "momentum_both": 1, "largest_buyer_share": 5, "not_bundled": 5, "all": 1}
    assert c["decisions"] == 1 and c["decisions_distinct_mints"] == 1 and c["features_asof_snapshots"] == 1
    assert c["distribution"]["unique_buyers_5m"] == {"n": 5, "p50": 0.0, "p90": 30.0, "max": 30.0}
    assert c["bundled"] == {"ever_true": 0, "known_false": 1, "unknown": 4}
    h12 = next(h for h in e["hours"] if h["hour"] == 12)
    assert h12["candidates_first_seen"] == 5 and h12["probation_first_seen"] == 2 and h12["probation_with_trades"] == 1 and h12["probation_trade_share"] == 0.5
    assert h12["stream_gap_s"] == 600 and h12["stream_gap_reasons"] == ["budget_cap"]
    assert e["stream"]["gap_seconds"] == 600 and e["stream"]["envelope_paused"] == 2 and e["stream"]["engine_tick_failed"] == 0
    mt = {t["key"]: t["met"] for t in e["metas"]["thresholds"]}
    assert e["metas"]["candidates"] >= 1 and mt["coins_and_creators"] >= 1 and mt["burst_ratio"] >= 1 and mt["sol_inflow"] == 0 and mt["unique_buyers"] == 1
    assert mt["bursting"] >= 1 and mt["all"] == 0 and e["metas"]["decisions"] == 0
    assert e["metas"]["distribution"]["burst_ratio"]["n"] == 0 and e["metas"]["distribution"]["burst_ratio_inf"] >= 1
    assert c["top"][0]["mint"] == "M" and c["top"][0]["passed_all"] is True and e["metas"]["top"][0]["term"] == "goldberg"
    assert "Geen advies" in e["disclaimer"]
    txt = render_text(e)
    assert "coin candidates: 5 (meta_burst 3, probation 2); with trades 1 (20%); probation with trades 1/2 (50%)" in txt
    assert "unique_buyers_5m" in txt and "budget_cap" in txt and "thresholds unchanged" in txt and "goldberg" in txt
    # the daily report carries the same table
    rep = build_report(conn, settings, DAY, T0 + 7200)
    assert rep["explain"]["coins"]["candidates"] == 5
    md = render_markdown(rep)
    assert "## Waarom wel of geen observaties" in md and "Munt-kandidaten die de engine deze dag zag: **5** (meta_burst 3, probation 2)" in md
    assert "| unieke kopers in 5 min | >= 25 | 1 |" in md and "| 12 | 5 | 2 | 1 | 50% | 600 (budget_cap) |" in md
    assert "de drempels blijven wat ze zijn" in md and "\n".join(markdown_lines(e)).startswith("## Waarom wel of geen observaties")


def test_explain_cli_is_read_only_and_honest_without_rows(settings, conn, capsys):
    _world(conn)
    Engine(conn, settings, rpc=None).tick(T0 + 40)
    before = [tuple(r) for r in conn.execute("SELECT * FROM engine_candidates ORDER BY kind, key")]
    conn.close()
    assert main(["--root", str(settings.root), "--quiet", "engine", "explain", "--day", DAY]) == 0
    out = capsys.readouterr().out
    assert out.startswith(f"fart engine explain --day {DAY}") and "coin threshold" in out and "read-only" in out
    assert main(["--root", str(settings.root), "--quiet", "--json", "engine", "explain", "--day", DAY]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["coins"]["candidates"] == 2 and data["thresholds"]["coin"]["observe_min_unique_buyers_5m"] == 25
    assert main(["--root", str(settings.root), "--quiet", "engine", "explain", "--day", "2020-01-01"]) == 0
    assert "geen kandidaatgegevens voor deze dag" in capsys.readouterr().out
    assert main(["--root", str(settings.root), "--quiet", "engine", "explain", "--day", "not-a-day"]) == 2
    c2 = db.connect(settings.db_path)
    assert [tuple(r) for r in c2.execute("SELECT * FROM engine_candidates ORDER BY kind, key")] == before  # nothing changed
    assert c2.execute("SELECT COUNT(*) FROM decisions").fetchone()[0] == 1
    c2.close()
    # status names the day's candidate rows
    assert main(["--root", str(settings.root), "--quiet", "status"]) == 0
    assert "engine candidates today:" in capsys.readouterr().out


def test_percentiles_and_retention_of_candidate_rows(settings, conn):
    assert percentiles([]) == {"n": 0, "p50": None, "p90": None, "max": None}
    assert percentiles([3, 1, 2]) == {"n": 3, "p50": 2.0, "p90": 3.0, "max": 3.0}
    assert percentiles([10] * 10) == {"n": 10, "p50": 10.0, "p90": 10.0, "max": 10.0}
    conn.execute("INSERT INTO engine_candidates(day, kind, key, first_as_of, last_as_of, ticks) VALUES('2024-01-01', 'coin', 'OLD', 1, 1, 1)")
    conn.execute("INSERT INTO engine_candidates(day, kind, key, first_as_of, last_as_of, ticks) VALUES(?, 'coin', 'NEW', ?, ?, 1)", (DAY, T0, T0))
    out = run_retention(conn, settings.raw_dir, settings.archive_dir, settings.config("ingest")["retention"], T0 + 86400)
    assert out["candidates_archived"] == 1 and settings.config("ingest")["retention"]["candidates_days"] == 30
    assert [r[0] for r in conn.execute("SELECT key FROM engine_candidates")] == ["NEW"]
    assert list(settings.archive_dir.glob("engine_candidates-*.jsonl.gz"))
