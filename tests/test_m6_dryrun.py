"""Milestone 6, point 2, as rebuilt by milestone 7: `fart social dryrun`: calls and USD per kind per day without HTTP, the planner's
full-load formula, the observed rates, the hourly walk through the planner's rules (hard caps refuse, the envelope defers)."""
from __future__ import annotations

import json

import pytest

from fart import db
from fart.cli import main
from fart.ingest import watch
from fart.net import http as nh
from fart.social import dryrun as dr
from fart.social import planner

T0 = 1_793_059_200.0  # 2026-10-27T00:00:00Z
ASSUME = dict(dr.DEFAULT_ASSUMPTIONS)


def test_per_kind_numbers_follow_the_config(settings):
    cfg = settings.config("x")
    rows = {r["kind"]: r for r in dr.per_kind(cfg, n_keys=2, n_kols=13, rates={}, assumptions=ASSUME)}
    assert set(rows) == set(dr.KINDS) == {"mega_search", "refresh", "kol_search", "ca_counts", "ca_search", "ca_authors"}
    # the account watches: 13 peak hours at the peak interval, 11 off hours at the off interval; the money follows the posts, not the requests
    assert rows["mega_search"]["requests_per_day"] == 13 * 60 + 11 * 30 == 1110 and rows["mega_search"]["units"] == {"posts": 80, "authors": 9}
    assert rows["mega_search"]["usd_per_day"] == pytest.approx(80 * 0.005 + 9 * 0.01)  # 9 accounts in the query
    assert rows["refresh"]["requests_per_day"] == 720 and rows["refresh"]["usd_per_day"] == 0.0 and "VERIFY" in rows["refresh"]["basis"]
    assert rows["kol_search"]["requests_per_day"] == 13 * 20 + 11 * 12 == 392 and rows["kol_search"]["usd_per_day"] == pytest.approx(200 * 0.005 + 13 * 0.01)
    assert rows["kol_search"]["hard_cap"] == 1.0 and "bereikt" in rows["kol_search"]["note"]
    # the planner's keys: 2 keys as a day's load, the model's 8 counts per key (half the keys the full 12, half confirmed after 4), one search per confirmed key
    assert rows["ca_counts"]["requests_per_day"] == pytest.approx(16.0) and rows["ca_counts"]["usd_per_day"] == pytest.approx(0.08)
    assert rows["ca_counts"]["per_key_usd_ceiling"] == pytest.approx(0.06) and "schema hoogstens 12" in rows["ca_counts"]["basis"]
    assert rows["ca_search"]["requests_per_day"] == pytest.approx(1.0) and rows["ca_search"]["usd_per_day"] == pytest.approx(0.05)
    assert rows["ca_authors"]["requests_per_day"] == pytest.approx(1.0) and rows["ca_authors"]["usd_per_day"] == pytest.approx(0.03)
    # observed rates replace the assumptions and say so; without KOL accounts the KOL kind is zero
    rates = {"mega": {"posts": 30, "days": 2, "posts_per_day": 15.0},
             "stopped": {"n": 10, "confirmed": 3, "refuted": 5, "confirmed_share": 0.3, "counts_per_key": 5.0, "counts_per_confirmed_key": 2.0},
             "search": {"searches": 4, "posts_per_search": 6.0, "authors_per_search": 1.5}}
    r2 = {r["kind"]: r for r in dr.per_kind(cfg, 0, 0, rates, ASSUME, keys_per_day=20)}
    assert r2["mega_search"]["units"]["posts"] == 15.0 and "waargenomen over 2 dag(en)" in r2["mega_search"]["basis"]
    assert r2["kol_search"]["usd_per_day"] == 0.0 and r2["kol_search"]["requests_per_day"] == 0.0
    assert r2["ca_counts"]["requests_per_day"] == pytest.approx(100.0) and "waargenomen" in r2["ca_counts"]["basis"]  # 20 keys x 5 counts
    assert r2["ca_search"]["requests_per_day"] == pytest.approx(6.0) and r2["ca_search"]["usd_per_day"] == pytest.approx(6 * 6 * 0.005)
    assert r2["ca_authors"]["usd_per_day"] == pytest.approx(6 * 1.5 * 0.01) and "waargenomen over 4 zoekopdrachten" in r2["ca_search"]["basis"]
    assert dr.accounts_in_query(cfg["mega"]["query"]) == 9


def test_full_load_formula_and_schedule(settings):
    cfg = settings.config("x")
    offsets = planner.schedule_counts(cfg["planner"], cfg["confirm"]["window_s"])
    assert offsets == [0, 900, 1800, 2700, 3600, 4500, 5400, 6300, 7200, 10800, 14400, 18000] and len(offsets) == 12  # 9 in the first 2 h, 3 hourly
    assert planner.schedule_counts({**cfg["planner"], "max_counts_per_key": 5}, 21600) == [0, 900, 1800, 2700, 3600]
    assert planner.schedule_counts(cfg["planner"], 3600) == [0, 900, 1800, 2700]
    f = dr.full_load(cfg, 0.49, 1.0, 15.0)
    # 60 keys x (12 counts x $0.005 + 10 posts x $0.005 + 3 authors x $0.010) = 60 x $0.14 = $8.40; with the watches $9.89 of the $15 cap
    assert (f["keys_per_day"], f["counts_per_key"], f["posts_per_search"], f["authors_per_search"]) == (60, 12, 10, 3)
    assert f["counts_usd"] == pytest.approx(3.60) and f["search_usd"] == pytest.approx(4.80) and f["confirm_ceiling_usd"] == pytest.approx(8.40)
    assert f["formula"] == "60 x (12 x $0.005 + 10 x $0.005 + 3 x $0.010) = 60 x $0.140 = $8.40"
    assert f["total_ceiling_usd"] == pytest.approx(9.89) and f["fits_cap"] and f["fits_target"] and f["share_of_cap"] == pytest.approx(9.89 / 15)
    assert dr.full_load(cfg, 0.49, 1.0, 3.0)["fits_cap"] is False  # the milestone-2 cap cannot carry the full load
    assert dr.full_load(cfg, 0.0, 0.0, 15.0, keys_per_day=100)["confirm_ceiling_usd"] == pytest.approx(14.0)
    m = dr.key_model(cfg, {}, ASSUME)
    assert m["key_ceiling_usd"] == pytest.approx(0.14) and m["key_expected_usd"] == pytest.approx(8 * 0.005 + 0.5 * 0.08)


def test_hourly_walk_defers_on_the_envelope_and_refuses_on_the_hard_rules(settings):
    cfg = settings.config("x")
    # confirmation counts alone: $4.80/day uniform against a $3 cap -> the envelope DEFERS the morning's surplus, the 70% reserve refuses above $2.10
    rows = [{"kind": "ca_counts", "usd_per_day": 4.80}]
    sim = dr.simulate_hours(cfg, 3.0, rows, T0, 24)
    assert len(sim) == 24 and sim[0]["hour"] == "2026-10-27T00" and sim[0]["peak"] is True and sim[5]["peak"] is False
    assert sim[0]["allowance_usd"] == pytest.approx(3.0 / 17.4, abs=1e-3) and sim[0]["deferred"] == {"ca_counts": "hourly_envelope"} and sim[0]["refused"] == {}
    assert {v for s in sim for v in s["refused"].values()} == {"account_watch_reserve"} and sim[-1]["spent_usd"] == pytest.approx(2.1, abs=1e-6)
    assert sim[-1]["backlog_usd"] > 0  # what the reserve refused waits for the next day in the model
    # the KOL watch alone: $1.13/day hits its $1.00 sub-cap in the evening; nothing else is in the way
    sim = dr.simulate_hours(cfg, 3.0, [{"kind": "kol_search", "usd_per_day": 1.13}], T0, 24)
    assert {v for s in sim for v in s["refused"].values()} == {"kol_daily_cap"} and sim[-1]["spent_usd"] == pytest.approx(1.0, abs=1e-6)
    # within the cap and arriving with the envelope: no deferral and no refusal at all; the hard cap shows up when the day exceeds it
    rows = [{"kind": "mega_search", "usd_per_day": 0.49}, {"kind": "kol_search", "usd_per_day": 0.9}]
    assert all(not s["deferred"] and not s["refused"] for s in dr.simulate_hours(cfg, 3.0, rows, T0, 24, arrival="envelope"))
    big = dr.simulate_hours(cfg, 1.0, [{"kind": "mega_search", "usd_per_day": 6.0}], T0, 24, arrival="envelope")
    assert "daily_cost_cap" in {v for s in big for v in s["refused"].values()} and big[-1]["spent_usd"] == pytest.approx(1.0, abs=1e-6)
    # the full load at the tuned cap: no refusal, deferrals only where the envelope paces the morning
    full = [{"kind": k, "usd_per_day": v} for k, v in (("ca_counts", 3.6), ("ca_search", 3.0), ("ca_authors", 1.8), ("mega_search", 0.49), ("kol_search", 1.0))]
    sim = dr.simulate_hours(cfg, 15.0, full, T0, 24)
    assert not any(s["refused"] for s in sim) and sim[-1]["spent_usd"] == pytest.approx(9.89, abs=1e-6) and sim[-1]["backlog_usd"] == pytest.approx(0.0, abs=1e-6)
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
    conn.execute("INSERT INTO x_spend(day, kind, n, usd, ts, unit) VALUES(?, 'mega_search', 2, 0.25, ?, 'post')", (db.day_of(T0), T0))
    conn.close()
    import fart.social.dryrun as mod

    monkeypatch.setattr(mod, "spend_today_by_kind", lambda c, now: {"mega_search": 0.25})
    now_hold = {"v": T0 + 3600}
    monkeypatch.setattr("fart.cli.now", lambda: now_hold["v"])
    assert main(["--root", str(settings.root), "--quiet", "social", "dryrun", "--hours", "3"]) == 0
    out = capsys.readouterr().out
    assert "confirmation keys now: 2 (1 mints, 1 themes)" in out and "no HTTP, nothing written" in out and "in the plan: 0 active" in out
    assert "observed rates: mega 3 posts/day over 2 day(s), 2.0 observations/day over 1 day(s)" in out and "spent today so far: $0.25" in out
    assert "a planned key costs at most $0.140" in out and out.count("\n  2026-10-27T") == 3 and "hours with deferred calls:" in out
    assert "full load: 60 keys/day x (12 counts + 1 search of 10 posts and 3 authors) = $8.40/day" in out and "[60 x (12 x $0.005 + 10 x $0.005 + 3 x $0.010) = 60 x $0.140 = $8.40]" in out
    assert "EXCEEDS the cap of $3.00" not in out  # the expected load of 2 keys a day fits even the old cap
    assert "of the cap $3.00 -> EXCEEDS; against the planner's target cap $15.00: fits" in out  # the full load does not fit $3: the deployer sets 15.00
    assert "De planner is afgestemd op X_DAILY_BUDGET_USD 15.00; .env zegt 3.00" in out
    assert main(["--root", str(settings.root), "--quiet", "--json", "social", "dryrun", "--hours", "24", "--arrival", "envelope"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert [r["kind"] for r in data["kinds"]] == list(dr.KINDS) and len(data["hours"]) == 24 and data["arrival"] == "envelope"
    assert data["confirmation_keys"]["sample"] == [mint, "meta:goldberg cat"] and data["x_mode"] == "off"
    assert data["total_usd_per_day"] == pytest.approx(sum(r["usd_per_day"] for r in data["kinds"]))
    assert data["full_load"]["confirm_ceiling_usd"] == pytest.approx(8.4) and data["full_load"]["fits_cap"] is False and data["full_load"]["fits_target"] is True
    assert data["keys_per_day"] == pytest.approx(2.0) and "observaties" in data["keys_per_day_source"]
    assert [p["kind"] for p in data["plan_vs_actual"]] == list(dr.KINDS) and data["plan_vs_actual"][0]["actual_usd"] == 0.25
    c2 = db.connect(settings.db_path)
    assert c2.execute("SELECT COUNT(*) FROM api_ledger").fetchone()[0] == 0 and c2.execute("SELECT COUNT(*) FROM x_spend").fetchone()[0] == 1
    assert c2.execute("SELECT COUNT(*) FROM x_keys").fetchone()[0] == 0  # the dry run never admits a key
    c2.close()
