"""Milestone 13, the research side and the pages: the second round of hypotheses (H8, H9, H10) on the lab with known outcomes, the
post-graduation autopsy, the catalogue (ten hypotheses, at most twelve variants in the second round, the ts30 exit), the summary per
hypothesis (out/research/latest.json), the research window that takes what the database has, the weekly timer unit, the research
state on /plays, the owner page (the full `fart research list`, the follow card), the Terminal rows (followed, the source of the
last point) and the detail view, the verified terminal links, and the fingerprint of the frozen rule files."""
from __future__ import annotations

import json
import re
import threading
from html import unescape
from pathlib import Path

import pytest

from fart import db
from fart.config import load_settings
from fart.research import autopsy, harness as rh, hypotheses as hyps, report as rr
from fart.util import day_bounds
from fart.web import pages, public, server
from tests.test_m12_research import DAYS, KINDS, build_lab
from tests.test_m8_web import NOW, Client, build_world
from tests.test_m9_public_site import OWNER_KEY_WORDS, OWNER_PAGE_WORDS, _flatten, _headers

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(scope="module")
def lab(tmp_path_factory):
    """The M12 lab plus minute candles for its graduations: a rise of 2% per minute for twenty minutes, then a fall of 1% per minute,
    $3,000 of volume per minute, and the maker of every other graduation sold on the curve before the graduation."""
    from fart import version

    mp = pytest.MonkeyPatch()
    mp.setenv("FART_RULES_VERSION", "test-rules-v1")
    version._CACHE.pop("v", None)
    world = build_lab(tmp_path_factory.mktemp("lab13"))
    conn = world["conn"]
    conn.execute("BEGIN")
    for i, (mint, gt, winner) in enumerate(world["grads"]):
        pool = conn.execute("SELECT pool_address FROM migrations WHERE mint=?", (mint,)).fetchone()[0]
        base = 2e-5 * 180.0
        for k in range(60):
            minute = int(gt - gt % 60) + 60 * k
            mult = 1.0 + 0.02 * k if k < 20 else 1.4 - 0.01 * (k - 20)
            conn.execute("INSERT OR REPLACE INTO pool_candles(pool, minute, open, high, low, close, volume_usd, fetched_at) VALUES(?,?,?,?,?,?,?,?)",
                         (pool, minute, base * mult, base * mult * 1.01, base * mult * 0.99, base * mult, 3000.0, gt + 7200))
        if i % 2 == 1:   # the maker sold a little on the curve before the graduation: H8's counter-proof group
            creator = conn.execute("SELECT creator FROM coins WHERE mint=?", (mint,)).fetchone()[0]
            last = conn.execute("SELECT virtual_sol, virtual_token FROM trades WHERE mint=? ORDER BY COALESCE(block_time, received_at) DESC LIMIT 1", (mint,)).fetchone()
            conn.execute("INSERT INTO trades(mint, slot, block_time, received_at, signature, buyer, is_buy, sol, tokens, virtual_sol, virtual_token, source) VALUES(?,?,?,?,?,?,0,?,?,?,?,'rpc_trades')",
                         (mint, 9000, gt - 30, gt - 29.6, f"devsell-{i}", creator, 0.05, 50000.0, last["virtual_sol"], last["virtual_token"]))
    conn.execute("COMMIT")
    yield world
    conn.close()
    mp.undo()


# ----- the catalogue -----------------------------------------------------------------------------------------------------------
def test_second_round_is_pre_registered_small_and_after_graduation():
    assert hyps.ORDER == ["H4", "H5", "H1", "H3", "H7", "H2", "H6", "H8", "H9", "H10"] and hyps.ROUND_TWO == ("H8", "H9", "H10")
    counts = {hid: len(hyps.get(hid).variants) for hid in hyps.ORDER}
    assert counts["H8"] == 12 and counts["H9"] == 12 and counts["H10"] == 4 and all(counts[h] <= 12 for h in hyps.ROUND_TWO) and sum(counts.values()) == 173
    for hid in hyps.ROUND_TWO:
        h = hyps.get(hid)
        assert h.post_graduation and h.post_grad_horizon_s > 0 and h.title_nl and h.statement_nl and h.scope_nl and h.expectation_nl
        assert len({v.id for v in h.variants}) == len(h.variants) and all(v.exit in hyps.EXITS for v in h.variants)
    assert hyps.EXITS["ts30"].time_stop_s == 1800.0 and hyps.EXITS["ts30"].trailing_frac is None and hyps.EXIT_LABELS_NL["ts30"] == "tijdstop 30 min"
    assert {v.exit for v in hyps.H8.variants} == {"ladder60_tr30", "tr30", "ts30", "ts60"} and {v.entry["minutes"] for v in hyps.H8.variants} == {2, 5, 10}
    assert {v.group for v in hyps.H8.variants} == {"held_n2", "held_n5", "held_n10", "held_n5_v25k", "sold_n5"}
    assert {v.exit for v in hyps.H9.variants} == {"tr20", "tr30", "ts15", "ts60"} and {v.entry["min_drawdown"] for v in hyps.H9.variants} == {0.2, 0.3, 0.4}
    assert [v.id for v in hyps.H10.variants] == ["leader_tr30", "leader_ts60", "copy_tr30", "copy_ts60"] and hyps.H10.needs_theme
    cat = hyps.catalogue()
    assert [c["id"] for c in cat] == hyps.ORDER and next(c for c in cat if c["id"] == "H8")["variants"] == 12
    # the predicates on a hand-made context: above the graduation price with volume and the maker holding fires at the fifth minute only
    v = hyps.H8.variant("held_n5_v10k_tr30")
    f = {"minutes_after_graduation": 5.0, "price_sol": 1.1e-7, "points_since_graduation": 3, "volume_since_graduation_usd": 12000.0, "dev_sold_before_graduation": False}
    ok = hyps.Ctx("m", 0.0, 900.0, f, None, {}, {}, True, 600.0, None, None, None, 1e-7)
    assert hyps.H8.fires(v, ok) is True
    assert hyps.H8.fires(v, hyps.Ctx("m", 0.0, 900.0, {**f, "minutes_after_graduation": 4.0}, None, {}, {}, True, 600.0, None, None, None, 1e-7)) is False
    assert hyps.H8.fires(v, hyps.Ctx("m", 0.0, 900.0, {**f, "dev_sold_before_graduation": True}, None, {}, {}, True, 600.0, None, None, None, 1e-7)) is False
    assert hyps.H8.fires(hyps.H8.variant("sold_n5_v10k_tr30"), hyps.Ctx("m", 0.0, 900.0, {**f, "dev_sold_before_graduation": True}, None, {}, {}, True, 600.0, None, None, None, 1e-7)) is True
    assert hyps.H8.fires(v, hyps.Ctx("m", 0.0, 900.0, {**f, "price_sol": 0.9e-7}, None, {}, {}, True, 600.0, None, None, None, 1e-7)) is False
    assert hyps.H8.fires(v, hyps.Ctx("m", 0.0, 900.0, {**f, "volume_since_graduation_usd": None}, None, {}, {}, True, 600.0, None, None, None, 1e-7)) is False   # unknown volume: no signal
    assert hyps.H8.fires(v, hyps.Ctx("m", 0.0, 900.0, {**f, "points_since_graduation": 0}, None, {}, {}, True, 600.0, None, None, None, 1e-7)) is False       # no point yet: no signal
    v9 = hyps.H9.variant("x20_tr20")
    f9 = {"minutes_after_graduation": 12.0, "price_sol": 0.75e-7, "points_since_graduation": 12, "drawdown_from_peak": 0.25, "volume_5m_usd": 4000.0}
    assert hyps.H9.fires(v9, hyps.Ctx("m", 0.0, 1320.0, f9, None, {}, {}, True, 600.0, None, None, None, 1e-7)) is True
    assert hyps.H9.fires(hyps.H9.variant("x30_tr20"), hyps.Ctx("m", 0.0, 1320.0, f9, None, {}, {}, True, 600.0, None, None, None, 1e-7)) is False
    assert hyps.H9.fires(v9, hyps.Ctx("m", 0.0, 1320.0, {**f9, "volume_5m_usd": None}, None, {}, {}, True, 600.0, None, None, None, 1e-7)) is False           # no candles: no signal
    assert hyps.H9.fires(v9, hyps.Ctx("m", 0.0, 1320.0, {**f9, "minutes_after_graduation": 31.0}, None, {}, {}, True, 600.0, None, None, None, 1e-7)) is False
    v10 = hyps.H10.variant("leader_tr30")
    assert hyps.H10.fires(v10, hyps.Ctx("m", 0.0, 720.0, {"minutes_after_graduation": 2.0}, None, {}, {}, True, 600.0, None, {"role": "leader"}, None, 1e-7)) is True
    assert hyps.H10.fires(v10, hyps.Ctx("m", 0.0, 720.0, {"minutes_after_graduation": 2.0}, None, {}, {}, True, 600.0, None, {"role": "copy"}, None, 1e-7)) is False
    assert hyps.H10.fires(v10, hyps.Ctx("m", 0.0, 780.0, {"minutes_after_graduation": 3.0}, None, {}, {}, True, 600.0, None, {"role": "leader"}, None, 1e-7)) is False


# ----- the second round on the lab -----------------------------------------------------------------------------------------------
def test_h8_fires_on_the_graduations_with_known_outcomes_and_counts_the_counter_proof(lab):
    res = rh.run_hypothesis(lab["conn"], lab["settings"], "H8", DAYS, min_cases=1, min_select_cases=1)
    rows = {r["variant"]: r for r in res["variants"]}
    grads = len(lab["grads"])
    assert res["stats"]["graduations"] == grads == 8 and res["config"]["variants_tested"] == 12
    held5 = rows["held_n5_v10k_tr30"]["full"]
    sold5 = rows["sold_n5_v10k_tr30"]["full"]
    assert held5["signals"] == grads // 2 and sold5["signals"] == grads // 2                  # every other maker sold on the curve: the counter-proof group
    assert held5["cases"] == held5["signals"] and held5["unknown"] == 0 and held5["no_fill"] == 0 and held5["fill_sources"] == {"entry": {"candle": 4}, "exit": {"candle": 4}}
    assert rows["held_n2_v10k_tr30"]["full"]["signals"] == 0                                   # two candles of $3,000: under the $10,000 since graduation
    assert rows["held_n10_v10k_tr30"]["full"]["signals"] == grads // 2 and rows["held_n5_v25k_tr30"]["full"]["signals"] == 0   # $15,000 at five minutes: not $25,000
    case = next(c for c in res["cases"] if c["variant"] == "held_n5_v10k_tr30")
    assert case["features"]["minutes_after_graduation"] == 5.0 and case["features"]["volume_since_graduation_usd"] == 15000.0 and case["features"]["dev_sold_before_graduation"] is False
    assert case["entry_kind"] == "candle" and case["venue"] == "pool" and case["status"] == "closed"
    sold_case = next(c for c in res["cases"] if c["variant"] == "sold_n5_v10k_tr30")
    assert sold_case["features"]["dev_sold_before_graduation"] is True
    for r in rows.values():
        for half in ("full", "selection", "test"):
            m = r[half]
            assert m["signals"] == m["cases"] + m["unknown"] + m["no_fill"]
    md, js = rr.write_report(lab["settings"], res, lab["now"])
    text = md.read_text(encoding="utf-8")
    for needle in ("Dev houdt vast na graduatie", "tegenproef", "Verkopen op de pool ziet de radar niet", "12 varianten getoetst", "candle"):
        assert needle in text, needle
    again = rh.run_hypothesis(lab["conn"], lab["settings"], "H8", DAYS, min_cases=1, min_select_cases=1)
    assert json.dumps(again["variants"], sort_keys=True, default=str) == json.dumps(res["variants"], sort_keys=True, default=str)


def test_h9_and_h10_on_the_lab(lab):
    res = rh.run_hypothesis(lab["conn"], lab["settings"], "H9", DAYS, min_cases=1, min_select_cases=1)
    rows = {r["variant"]: r for r in res["variants"]}
    # the candles rise 2% a minute for twenty minutes, then fall 1% a minute: a 20% drawdown from the peak falls at minute 40, outside H9's 30 minutes;
    # the lab's rise-then-fall never reaches 20% under the peak inside the window, except where the graduation price itself is the peak
    for v, r in rows.items():
        m = r["full"]
        assert m["signals"] == m["cases"] + m["unknown"] + m["no_fill"] and m["signals"] <= len(lab["grads"])
    assert all(c["features"]["drawdown_from_peak"] >= 0.2 and c["features"]["volume_5m_usd"] is not None and c["features"]["minutes_after_graduation"] <= 30 for c in res["cases"])
    assert res["config"]["variants_tested"] == 12
    h10 = rh.run_hypothesis(lab["conn"], lab["settings"], "H10", DAYS, min_cases=1, min_select_cases=1)
    assert h10["stats"]["signals"] == 0 and h10["selected"] is None                           # no theme observations in the lab: nothing fires, nothing is claimed
    # a theme observation whose coins graduated: the first graduation is the leader, the later one a copy
    conn = lab["conn"]
    (m0, g0, _), (m1, g1, _) = lab["grads"][0], lab["grads"][1]
    conn.execute("UPDATE coins SET name='Quantum Frog', symbol='QFROG' WHERE mint IN (?, ?)", (m0, m1))
    conn.execute("INSERT INTO decisions(as_of, created_at, kind, mint, term, features_json, rules_version, verdict, reasons_json) VALUES(?,?,?,?,?,?,?,?,?)",
                 (min(g0, g1) - 600, min(g0, g1) - 599, "observe_meta", None, "quantum", "{}", "test-rules-v1", "observe", "[]"))
    conn.commit()                                          # decisions are append-only: the observation stays in the lab from here on
    h = rh.Harness(conn, lab["settings"], rh.RunConfig.from_settings(lab["settings"], "H10", DAYS, min_cases=1, min_select_cases=1))
    roles = h.theme_grad_roles()
    leader, copy = (m0, m1) if g0 <= g1 else (m1, m0)
    assert roles[leader]["role"] == "leader" and roles[copy]["role"] == "copy" and roles[leader]["term"] == "quantum"
    res10 = h.run()
    full = {r["variant"]: r["full"] for r in res10["variants"]}
    assert full["leader_tr30"]["signals"] == 1 and full["copy_tr30"]["signals"] == 1 and res10["stats"]["graduations"] == 2
    assert all(c["features"]["minutes_after_graduation"] == 2.0 for c in res10["cases"])


def test_autopsy_has_the_post_graduation_part(lab):
    res = autopsy.build_autopsy(lab["conn"], lab["settings"], DAYS, lab["now"])
    pg = res["post_graduation"]
    assert set(pg) >= {"organic_winner", "not_winner"} and pg["organic_winner"]["n"] == len(DAYS)
    ow = pg["organic_winner"]
    assert ow["with_candles"] == len(DAYS) and ow["with_points"] == len(DAYS) and ow["time_to_peak_s"]["n"] == len(DAYS) and 1100 <= ow["time_to_peak_s"]["p50"] <= 1300
    assert ow["peak_gain_24h"]["p50"] > 0 and ow["drawdown_1h"]["n"] == len(DAYS) and ow["volume_first_hour_usd"]["p50"] == pytest.approx(180000.0)
    assert ow["volume_5m_usd"]["n"] == 12 * len(DAYS) and ow["volume_5m_usd"]["p50"] == pytest.approx(15000.0)
    assert ow["dev_sold_after_graduation"].startswith("niet gemeten") and isinstance(ow["dev_sold_before_graduation"], int)
    text = autopsy.render_autopsy(res)
    for needle in ("## Na graduatie (mijlpaal 13)", "tijd tot piek", "dev verkocht ná graduatie", "niet gemeten: de radar heeft geen pool-trades", "volume per 5 min"):
        assert needle in text, needle
    md, js = autopsy.write_autopsy(lab["settings"], res)
    assert Path(md).exists() and json.loads(Path(js).read_text(encoding="utf-8"))["post_graduation"]["organic_winner"]["n"] == len(DAYS)


# ----- the summary per hypothesis and the research window ---------------------------------------------------------------------
def test_latest_json_summaries_and_the_window_that_takes_what_the_database_has(lab):
    s = lab["settings"]
    res = rh.run_hypothesis(lab["conn"], s, "H2", DAYS, min_cases=5, min_select_cases=3, variants=["m5_ts5"], days_requested=30,
                            window_note="Gevraagd venster 30 dagen; de database heeft trades vanaf 2026-10-20: venster beperkt tot 4 dagen (2026-10-20 t/m 2026-10-23); de walk-forward splitst deze 4 dagen.")
    assert res["config"]["days_requested"] == 30 and res["config"]["window_note"].startswith("Gevraagd venster 30 dagen")
    md, js = rr.write_report(s, res, lab["now"] + 7200)
    text = md.read_text(encoding="utf-8")
    assert "venster beperkt tot 4 dagen" in text
    latest = rr.load_latest(s)
    assert "H2" in latest and latest["H2"]["selected"] == res["selected"] and latest["H2"]["proven"] is True and latest["H2"]["verdict"] == "bewezen"
    assert latest["H2"]["unknown_share"] == 0.0 and latest["H2"]["cases"] == 20 and latest["H2"]["days"] == DAYS and latest["H2"]["days_requested"] == 30
    assert list(latest) == [h for h in hyps.ORDER if h in latest] and (rr.report_dir(s) / rr.LATEST_FILE).exists()
    assert all(r["name"] != "latest" for r in rr.list_reports(s))                      # the summary file is not a report
    summ = rr.summarize({"name": "x", "hypothesis": {"id": "H3", "title": "Themaleider"}, "variants": [], "selected": None, "config": {"days": DAYS}})
    assert summ["verdict"] == "geen variant gekozen" and summ["proven"] is False and summ["cases"] is None
    # the window: 30 days asked, the trades start on the first lab day -> the four lab days, with a note; a complete window has no note
    from fart.cli import research_window

    days, note = research_window(lab["conn"], DAYS[-1], 30)
    assert days == DAYS and "venster beperkt tot 4 dagen" in note and "2026-10-20" in note
    days2, note2 = research_window(lab["conn"], DAYS[-1], 2)
    assert days2 == DAYS[-2:] and note2 is None
    empty = db.connect(":memory:")
    assert research_window(empty, DAYS[-1], 30) == (rh.window_days(DAYS[-1], 30), None)
    empty.close()


def test_weekly_research_timer_unit_and_install():
    service = (ROOT / "deploy" / "fart-research.service").read_text(encoding="utf-8")
    timer = (ROOT / "deploy" / "fart-research.timer").read_text(encoding="utf-8")
    assert "research run --hypothesis all --days 30" in service and "research autopsy --days 30" in service and "User=fart" in service and "Type=oneshot" in service
    assert "ProtectSystem=strict" in service and "ReadWritePaths=/opt/fart/data /opt/fart/out /opt/fart/logs" in service and "TimeoutStartSec=5400" in service
    assert "OnCalendar=Mon *-*-* 07:00:00 UTC" in timer and "Persistent=true" in timer and "Unit=fart-research.service" in timer
    install = (ROOT / "deploy" / "install.sh").read_text(encoding="utf-8")
    assert install.count("fart-research.timer") >= 2
    track = (ROOT / "deploy" / "fart-measure-track.service").read_text(encoding="utf-8")
    assert "TimeoutStartSec=240" in track
    measure = json.loads((ROOT / "config" / "measure.json").read_text(encoding="utf-8"))
    assert measure["tracker"]["run_budget_s"] < 240 and measure["sampling"]["time_budget_s"] < measure["tracker"]["run_budget_s"]
    assert measure["follow"]["daily_cu_share"] == 0.10 and measure["follow"]["max_followed"] == 1500 and measure["follow"]["degraded_intervals_s"] == [60, 120, 300]
    web = json.loads((ROOT / "config" / "web.json").read_text(encoding="utf-8"))
    assert {t["name"]: t["verified"] for t in web["terminals"]} == {"Axiom": False, "Photon": True, "BullX": False, "GMGN": True}
    budget = json.loads((ROOT / "config" / "budget.json").read_text(encoding="utf-8"))
    assert budget["sources"]["rpc"]["cost_table"]["getMultipleAccounts"] == 20 and "31 Oct 2026" in budget["sources"]["rpc"]["_comment"]


def test_frozen_rule_files_are_byte_identical():
    from fart.version import RULE_FILES, rules_fingerprint

    fp = rules_fingerprint()
    measure = json.loads((ROOT / "config" / "measure.json").read_text(encoding="utf-8"))
    equivalents = {e["rules_fingerprint"] for e in measure["community_gate"]["frozen_rules_equivalents"]}
    assert fp == "sha256:d45c41793c64e055ef2958f69c621118dd84ff1e54e2f29e4285e3adfbb00fe4" and fp in equivalents
    assert "fart/research/hypotheses.py" not in RULE_FILES and "fart/plays/follow.py" not in RULE_FILES


# ----- the pages ---------------------------------------------------------------------------------------------------------------
@pytest.fixture(scope="module")
def web(tmp_path_factory):
    from fart import version

    mp = pytest.MonkeyPatch()
    mp.setenv("FART_RULES_VERSION", "test-rules-v1")
    for k in ("FART_WEB_PASSWORD_HASH", "FART_WEB_SECRET", "FART_WEB_BIND", "FART_WEB_PUBLIC"):
        mp.delenv(k, raising=False)
    version._CACHE.pop("v", None)
    world = build_world(tmp_path_factory.mktemp("web13"), NOW, public=True)
    s = world["settings"]
    conn = db.connect(s.db_path)
    m0, m1 = world["live_mints"][0][0], world["live_mints"][1][0]
    # a followed coin with a poll point, and a graduated coin with a sample
    conn.execute("INSERT INTO followed(mint, since, reason, until, state, curve, next_poll_at, interval_s, polls, last_point_t, last_source, created_at, updated_at, json)"
                 " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (m0, NOW - 400, "early_buyers,play:H2-v1", NOW - 400 + 86400, "active", "Curve", NOW + 30, 60.0, 6, NOW - 20, "poll", NOW - 400, NOW - 20, "{}"))
    conn.execute("INSERT INTO path_points(mint, t, source, virtual_sol, virtual_token, real_sol, real_token, complete, slot) VALUES(?,?,?,?,?,?,?,0,1)",
                 (m0, NOW - 20, "poll", 40_000_000_000, 804_750_000_000_000, 10_000_000_000, 700_000_000_000_000))
    conn.execute("INSERT INTO followed(mint, since, reason, until, state, graduated_at, created_at, updated_at) VALUES(?,?,?,?,?,?,?,?)", (m1, NOW - 7000, "observe", NOW - 7000 + 86400, "graduated", NOW - 150, NOW - 7000, NOW - 100))
    conn.execute("INSERT INTO migrations(mint, block_time, received_at, source, sources, pool_address) VALUES(?,?,?,?,?,?)", (m1, NOW - 150, NOW - 149, "rpc_logs", "[]", "PoolLive01"))
    conn.execute("INSERT OR IGNORE INTO mcap_samples(mint, ts, src, mcap_usd, sol_usd) VALUES(?,?,?,?,?)", (m1, NOW - 90, "dexscreener", 120000.0, 180.0))
    conn.commit()
    conn.close()
    # a research summary
    rr.update_latest(s, {"name": "2026-10-25-H2", "hypothesis": {"id": "H2", "title": "Graduatiemoment"}, "generated_at": "2026-10-25T07:00:00Z", "selected": "m5_ts5", "selected_proven": False,
                         "variants": [{"variant": "m5_ts5", "label": "≥ 5 SOL netto bij 75 SOL, tijdstop 5 min", "test": {"cases": 61, "signals": 80, "unknown": 15, "unknown_share": 0.197, "ev_sol": -0.02, "ci": {"low": -0.05, "high": 0.01}, "hit_rate": 0.4, "lead_median_s": 300},
                                       "follow_split": {"since": NOW - 86400, "after": {"signals": 20, "unknown_share": 0.05}}}],
                         "config": {"days": ["2026-10-18", "2026-10-24"], "days_requested": 30, "variants_tested": 9, "follow_started_at": NOW - 86400}})
    clock = [NOW]
    app = server.App(s, clock=lambda: clock[0])
    srv = server.make_server(app, "127.0.0.1", 0)
    port = srv.server_address[1]
    t = threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.1}, daemon=True)
    t.start()
    app.refresh_views()
    app.feed.cycle()
    yield {"app": app, "port": port, "clock": clock, "world": world, "client": Client(port), "m0": m0, "m1": m1}
    srv.shutdown()
    srv.server_close()
    mp.undo()


def test_plays_page_shows_the_research_state_without_owner_words(web):
    client, world = web["client"], web["world"]
    st, h, body = client.req("GET", "/plays")
    html = body.decode()
    assert st == 200 and "Onderzoeksstand per hypothese" in html and "Graduatiemoment" in html and "niet bewezen" in html and "nog niet getoetst" in html and "(ronde 2)" in html
    assert "Dev houdt vast na graduatie" in html and "Themaleider na graduatie" in html and 'style="' not in html
    for word in OWNER_PAGE_WORDS:
        assert word not in html, word
    low = html.lower()
    for word in ("winst", "rendement", "voorspelling"):
        assert word not in low, word
    doc = json.loads(client.req("GET", "/api/public/plays")[2])
    rs = doc["research"]
    assert [r["hypothesis"] for r in rs["rows"]] == hyps.ORDER and rs["any"] is True
    h2 = next(r for r in rs["rows"] if r["hypothesis"] == "H2")
    assert h2["state"] == "rejected" and h2["verdict_label"] == "niet bewezen" and h2["selected"] == "m5_ts5" and h2["unknown_label"] == "20%" and h2["cases"] == 61 and h2["date_label"]
    assert h2["unknown_after_follow_label"] == "5%" and h2["round"] == 1 and next(r for r in rs["rows"] if r["hypothesis"] == "H8")["state"] == "none"
    for key, value in _flatten(doc):
        last = key.rsplit(".", 1)[-1].split("[")[0].lower()
        for word in OWNER_KEY_WORDS:
            assert word not in last, (key, word)
    # an empty research dir: the card says so and the page still renders
    empty = public.research_state(load_settings(root=world["settings"].root / "nothing", env={"FART_RULES_VERSION": "test-rules-v1"}))
    assert empty["any"] is False and all(r["state"] == "none" for r in empty["rows"]) and "nog geen run" in pages.research_card(empty)


def test_terminal_rows_carry_followed_and_the_source_of_the_last_point(web):
    client, app, m0, m1 = web["client"], web["app"], web["m0"], web["m1"]
    snap = json.loads(client.req("GET", "/api/public/terminal")[2])
    rows = {r["mint"]: r for r in snap["rows"]}
    f0 = rows[m0]["followed"]
    assert f0["state"] == "active" and f0["state_label"] == "gevolgd (polls van de curve)" and f0["reasons"] == ["early_buyers", "play:H2-v1"] and "H2-v1" in f0["reasons_label"]
    assert "unieke kopers" in f0["reasons_label"] and f0["since_age_s"] == 400
    assert rows[m0]["last_point"]["source"] == "poll" and rows[m0]["last_point"]["age_s"] == 20 and rows[m0]["last_point"]["source_label"] == "poll van de curve"
    assert rows[m1]["followed"]["state"] == "graduated" and rows[m1]["last_point"]["source"] == "sample" and rows[m1]["last_point"]["age_s"] == 90
    quiet = rows[web["world"]["live_mints"][3][0]]
    assert quiet["followed"] is None and quiet["last_point"] is None                       # never traded, not followed: unknown stays null
    traded = next(r for r in snap["rows"] if r["mint"] not in (m0, m1) and r["last_trade_age_s"] is not None)
    assert traded["last_point"]["source"] == "trade" and traded["followed"] is None
    for key, value in _flatten(snap):
        last = key.rsplit(".", 1)[-1].split("[")[0].lower()
        for word in OWNER_KEY_WORDS:
            assert word not in last, (key, word)
    js = (ROOT / "fart" / "web" / "static" / "terminal.js").read_text(encoding="utf-8")
    assert "'gevolgd'" in js and "'laatste punt'" in js and "r.followed" in js and "r.last_point" in js and "innerHTML" not in js
    html = client.req("GET", "/terminal")[2].decode()
    m = re.search(r'id="terminal" data-config="([^"]*)"', html)
    cfg = json.loads(unescape(m.group(1)))
    assert {t["name"]: t["verified"] for t in cfg["terminals"]} == {"Axiom": False, "Photon": True, "BullX": False, "GMGN": True}
    assert "Axiom, BullX" in html and "Photon" not in html.split("Linkpatronen nog niet handmatig geverifieerd")[1][:60]


def test_owner_page_lists_the_research_and_the_follow_state(web):
    client, world = web["client"], web["world"]
    st, h, body = client.login(world["password"])
    assert st in (302, 303), st
    cookie = [v for v in Client.header(h, "set-cookie") if v.startswith("fart_session=")][0].split(";")[0]
    st, h, body = client.req("GET", "/eigenaar", {"Cookie": cookie})
    html = body.decode()
    assert st == 200
    for needle in ("Onderzoek (fart research list)", "Volgen van signalen (mijlpaal 13)", "Dev houdt vast na graduatie", "m5_ts5", "niet bewezen", "gevolgde munten", "dagaandeel", "degradatiestand", "fart-research.timer"):
        assert needle in html, needle
    assert "173 varianten" in html and "10 vooraf vastgelegde hypotheses" in html
    assert 'href="#onderzoek"' in html and 'href="#volgen"' in html
