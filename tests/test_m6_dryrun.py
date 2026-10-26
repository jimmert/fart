"""Milestone 6, point 2: `fart social dryrun`: calls and USD per kind per day without HTTP, the hourly envelope walk."""
from __future__ import annotations

import json
import urllib.error

import pytest

from fart import db
from fart.cli import main
from fart.ingest import watch
from fart.net import http as nh
from fart.social import dryrun as dr

T0 = 1_793_059_200.0  # 2026-10-27T00:00:00Z
ASSUME = {"mega_posts_per_day": 80, "kol_posts_per_day": 200, "ca_search_share": 0.1}


def test_per_kind_numbers_follow_the_config(settings):
    cfg = settings.config("x")
    rows = {r["kind"]: r for r in dr.per_kind(cfg, n_keys=2, n_kols=13, rates={}, assumptions=ASSUME)}
    assert set(rows) == {"mega_search", "refresh", "kol_search", "ca_counts", "ca_search"}
    assert rows["mega_search"]["requests_per_day"] == 1440 and rows["mega_search"]["units"] == {"posts": 80, "authors": 9}
    assert rows["mega_search"]["usd_per_day"] == pytest.approx(80 * 0.005 + 9 * 0.01)  # 9 accounts in the query
    assert rows["refresh"]["requests_per_day"] == 720 and rows["refresh"]["usd_per_day"] == 0.0 and "VERIFY" in rows["refresh"]["basis"]
    assert rows["kol_search"]["requests_per_day"] == 480 and rows["kol_search"]["usd_per_day"] == pytest.approx(200 * 0.005 + 13 * 0.01)
    assert rows["kol_search"]["hard_cap"] == 1.0 and "bereikt" in rows["kol_search"]["note"]
    assert rows["ca_counts"]["requests_per_day"] == 192 and rows["ca_counts"]["usd_per_day"] == pytest.approx(0.96)  # 2 keys x 96 counts
    assert rows["ca_counts"]["per_key_usd_per_day"] == pytest.approx(0.48)
    assert rows["ca_search"]["requests_per_day"] == pytest.approx(19.2) and rows["ca_search"]["usd_per_day"] == pytest.approx(19.2 * 10 * 0.015)
    # observed rates replace the assumptions; without KOL accounts the KOL kind is zero; max_keys_per_loop bounds the counts
    r2 = {r["kind"]: r for r in dr.per_kind(cfg, 0, 0, {"mega": {"posts": 30, "days": 2, "posts_per_day": 15.0}}, ASSUME)}
    assert r2["mega_search"]["units"]["posts"] == 15.0 and "waargenomen over 2 dag(en)" in r2["mega_search"]["basis"]
    assert r2["kol_search"]["usd_per_day"] == 0.0 and r2["ca_counts"]["usd_per_day"] == 0.0
    r3 = {r["kind"]: r for r in dr.per_kind(cfg, 1000, 13, {}, ASSUME)}
    assert r3["ca_counts"]["requests_per_day"] == 4 * 86400 / 15 and "max_keys_per_loop" in r3["ca_counts"]["basis"]
    assert dr.accounts_in_query(cfg["mega"]["query"]) == 9


def test_hourly_walk_applies_envelope_reserve_kol_cap_and_hard_cap(settings):
    cfg = settings.config("x")
    # confirmation counts alone: $4.80/day uniform against a $3 cap -> the envelope holds the morning back, the 70% reserve caps the day at $2.10
    rows = [{"kind": "ca_counts", "usd_per_day": 4.80}]
    sim = dr.simulate_hours(cfg, 3.0, rows, T0, 24)
    assert len(sim) == 24 and sim[0]["hour"] == "2026-10-27T00" and sim[0]["peak"] is True and sim[5]["peak"] is False
    assert sim[0]["allowance_usd"] == pytest.approx(3.0 / 17.4, abs=1e-3) and sim[0]["refused"] == {"ca_counts": "hourly_envelope"}
    reasons = {v for s in sim for v in s["refused"].values()}
    assert reasons == {"hourly_envelope", "account_watch_reserve"} and sim[-1]["spent_usd"] == pytest.approx(2.1, abs=1e-6)
    # the KOL watch alone: $1.13/day hits its $1.00 sub-cap in the evening; nothing else is in the way
    sim = dr.simulate_hours(cfg, 3.0, [{"kind": "kol_search", "usd_per_day": 1.13}], T0, 24)
    assert {v for s in sim for v in s["refused"].values()} == {"kol_daily_cap"} and sim[-1]["spent_usd"] == pytest.approx(1.0, abs=1e-6)
    assert sim[-1]["backlog_usd"] > 0  # a refused search is carried, not forgotten
    # within the cap and arriving with the envelope: no refusal at all; the hard cap shows up when the day exceeds it
    rows = [{"kind": "mega_search", "usd_per_day": 0.49}, {"kind": "kol_search", "usd_per_day": 0.9}]
    assert all(not s["refused"] for s in dr.simulate_hours(cfg, 3.0, rows, T0, 24, arrival="envelope"))
    big = dr.simulate_hours(cfg, 1.0, [{"kind": "mega_search", "usd_per_day": 6.0}], T0, 24, arrival="envelope")
    assert "daily_cost_cap" in {v for s in big for v in s["refused"].values()} and big[-1]["spent_usd"] == pytest.approx(1.0, abs=1e-6)
    # today's spend seeds the walk, and a new UTC day starts from zero
    seeded = dr.simulate_hours(cfg, 3.0, [{"kind": "mega_search", "usd_per_day": 0.0}], T0 + 22 * 3600, 4, spend_today={"mega_search": 2.5})
    assert seeded[0]["spent_usd"] == 2.5 and seeded[2]["hour"].endswith("T00") and seeded[2]["spent_usd"] == 0.0


def test_dryrun_cli_counts_the_live_keys_and_never_touches_the_network(settings, conn, monkeypatch, capsys):
    def boom(req, timeout=None):
        raise AssertionError("dryrun must not call the network")

    monkeypatch.setattr(nh.urllib.request, "urlopen", boom)
    mint = "7xKXmintZorblaxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"
    conn.execute("INSERT INTO coins(mint, name, symbol, block_time, received_at, source, sources) VALUES(?,?,?,?,?,'rpc_logs','[]')", (mint, "Zorblax Cat", "ZORB", T0 - 900, T0 - 899))
    conn.execute("INSERT INTO decisions(as_of, created_at, kind, mint, features_json, rules_version, verdict, reasons_json) VALUES(?,?,'observe',?,'{}','r','observe','[]')", (T0 - 840, T0 - 840, mint))
    watch.add(conn, mint, "observe", T0 - 840, None, 200)
    conn.execute("INSERT INTO decisions(as_of, created_at, kind, term, features_json, rules_version, verdict, reasons_json) VALUES(?,?,'observe_meta','goldberg cat','{}','r','observe','[]')", (T0 - 100, T0 - 100))
    for i in range(6):  # three mega posts on each of two days: 3 posts/day observed
        conn.execute("INSERT INTO social_posts(id, kind, received_at, created_at) VALUES(?, 'mega', ?, ?)", (f"p{i}", T0 - 86400 * (1 + i // 3) + i, T0 - 86400 * (1 + i // 3)))
    conn.execute("INSERT INTO x_spend(day, kind, n, usd, ts) VALUES(?, 'mega_search', 2, 0.25, ?)", (db.day_of(T0), T0))
    conn.close()
    import fart.social.dryrun as mod

    monkeypatch.setattr(mod, "spend_today_by_kind", lambda c, now: {"mega_search": 0.25})
    now_hold = {"v": T0 + 3600}
    monkeypatch.setattr("fart.cli.now", lambda: now_hold["v"])
    assert main(["--root", str(settings.root), "--quiet", "social", "dryrun", "--hours", "3"]) == 0
    out = capsys.readouterr().out
    assert "confirmation keys now: 2 (1 mints, 1 themes)" in out and "no HTTP, nothing written" in out
    assert "observed rates: mega 3 posts/day over 2 day(s)" in out and "spent today so far: $0.25" in out
    assert "each extra confirmation key costs $0.48/day" in out and out.count("\n  2026-10-27T") == 3 and "hours with refused calls:" in out
    assert main(["--root", str(settings.root), "--quiet", "--json", "social", "dryrun", "--hours", "24", "--arrival", "envelope"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert [r["kind"] for r in data["kinds"]] == list(dr.KINDS) and len(data["hours"]) == 24 and data["arrival"] == "envelope"
    assert data["confirmation_keys"]["sample"] == [mint, "meta:goldberg cat"] and data["x_mode"] == "off"
    assert data["total_usd_per_day"] == pytest.approx(sum(r["usd_per_day"] for r in data["kinds"]))
    c2 = db.connect(settings.db_path)
    assert c2.execute("SELECT COUNT(*) FROM api_ledger").fetchone()[0] == 0 and c2.execute("SELECT COUNT(*) FROM x_spend").fetchone()[0] == 1
    c2.close()
