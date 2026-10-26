"""Milestone 7: the cost-aware X confirmation planner.

Point 1: keys only from observations (the mint, a non-generic ticker, the exact multi-word name; the strongest themes), in a hard
order (youngest coin first, themes last), at most `max_active_keys`, and a key stops for the right reason (confirmed after exactly
one search, refuted after three flat counts, expired, graduated, dead, displaced, exhausted). Point 2: counts every 15 min in the
first two hours and hourly after, at most 12 counts and one search with at most 10 posts and 3 author lookups; the account watches
slow down outside the peak hours. Point 3: the budget planner never exceeds the cap, defers on the envelope and prefers the
cheapest high-priority request. Point 4: the reports' quality-per-dollar lines. Point 5: `fart social calibrate`. Plus `fart status`
and schema v7. All HTTP is stubbed."""
from __future__ import annotations

import json
import random

import pytest

from fart import db
from fart.cli import main
from fart.engine.meta import load_stopwords
from fart.ingest import watch
from fart.measure import week as wk
from fart.measure.report import build_report, render_markdown
from fart.social import planner
from fart.social import x as xm
from fart.social.calibrate import calibrate
from fart.social.xbudget import XBudget
from fart.util import day_bounds, day_key
from tests.test_social import Clock, FakeRequest, T0, XClientFactory, make_budget
from tests.test_social import fx  # noqa: F401  (fixture)

FAR = "2027-01-01T00:00:00.000Z"  # a bucket end that is always "within the last hour" for the clocks used here


def coin(conn, mint, name, symbol, t, graduated=False, watched=True):
    conn.execute("INSERT INTO coins(mint, name, symbol, block_time, received_at, source, sources) VALUES(?,?,?,?,?,'rpc_logs','[]')", (mint, name, symbol, t, t + 1))
    if graduated:
        conn.execute("INSERT INTO migrations(mint, block_time, received_at, source, sources) VALUES(?,?,?,'rpc_logs','[]')", (mint, t + 100, t + 101))
    if watched:
        watch.add(conn, mint, "observe", t, None, 200)


def observe(conn, mint, t):
    cur = conn.execute("INSERT INTO decisions(as_of, created_at, kind, mint, features_json, rules_version, verdict, reasons_json) VALUES(?,?,'observe',?,'{}','r','observe','[]')",
                       (t, t, mint))
    return cur.lastrowid


def theme(conn, term, t, inflow, buyers=10):
    cur = conn.execute("INSERT INTO decisions(as_of, created_at, kind, term, features_json, rules_version, verdict, reasons_json) VALUES(?,?,'observe_meta',?,?,'r','observe','[]')",
                       (t, t, term, json.dumps({"sol_inflow": inflow, "unique_buyers": buyers})))
    return cur.lastrowid


def counts_body(n_last_hour, total=None):
    return {"data": [{"start": "2026-12-31T23:00:00.000Z", "end": FAR, "tweet_count": n_last_hour}], "meta": {"total_tweet_count": n_last_hour if total is None else total}}


USERS = {"data": [{"id": "1001", "username": "degen_a", "public_metrics": {"followers_count": 999}}, {"id": "1002", "username": "degen_b", "public_metrics": {"followers_count": 9}}]}


def client(settings, conn, clock, req, cap=15.0, cfg=None, shadow=True):
    budget = make_budget(settings, conn, clock, str(cap))
    xcfg = cfg or settings.config("x")
    xb = XBudget(conn, budget, xcfg, cap, clock=clock)
    c = xm.XClient(conn, budget, xb, xcfg, settings.config("x_kols")["accounts"], "PLACEHOLDER-BEARER", load_stopwords(settings.config("stopwords")),
                   "test-rules", clock=clock, request=req, shadow=shadow)
    return c


# ----- point 1: keys, order, stop reasons --------------------------------------------------------------------------------------
def test_candidates_come_from_observations_in_a_hard_order(settings, conn):
    cfg = settings.config("x")
    sw = load_stopwords(settings.config("stopwords"))
    coin(conn, "A" * 44, "Alpha Dog", "ALPHA", T0 - 3100)
    observe(conn, "A" * 44, T0 - 3000)
    coin(conn, "B" * 44, "Beta", "SOL", T0 - 700)              # generic ticker, one-word name: the mint alone
    observe(conn, "B" * 44, T0 - 600)                           # the youngest coin observation: first
    coin(conn, "C" * 44, "Gamma Cat", "GAM", T0 - 200, graduated=True)
    observe(conn, "C" * 44, T0 - 100)                           # graduated: never a key
    coin(conn, "D" * 44, "Delta Cat", "DELT", T0 - 300, watched=False)
    observe(conn, "D" * 44, T0 - 200)                           # dead (off the watchlist): never a key
    coin(conn, "E" * 44, "Epsilon", "EPS", T0 - 30000)
    observe(conn, "E" * 44, T0 - 25000)                         # older than the 6 h window
    for i, (term, inflow) in enumerate((("goldberg cat", 50.0), ("zorblax coin", 80.0), ("quantum frog", 10.0), ("gold", 999.0), ("pepe", 999.0),
                                        ("moonshot dog", 20.0), ("galactic hamster", 30.0), ("silver spoon", 5.0))):
        theme(conn, term, T0 - 50 - i, inflow)
    cands = planner.candidates(conn, T0, cfg, sw)
    assert [c["key"] for c in cands] == ["B" * 44, "A" * 44, "meta:zorblax coin", "meta:goldberg cat", "meta:galactic hamster", "meta:moonshot dog", "meta:quantum frog"]
    assert cands[0]["query"] == f'"{"B" * 44}"' and cands[1]["query"] == f'"{"A" * 44}" OR $ALPHA OR "Alpha Dog"'
    assert [c["prio_class"] for c in cands] == [0, 0, 1, 1, 1, 1, 1] and [c["rank"] for c in cands] == list(range(7))
    assert cands[0]["decision_kind"] == "observe" and cands[2]["decision_kind"] == "observe_meta" and cands[2]["query"] == '"zorblax coin"'
    # 'gold' and 'pepe' are too short / generic whatever their strength; at most 5 themes; a short bigram is no longer a key (milestone 7)
    assert xm.meta_term_query("ai cat", sw, cfg["confirm"]) is None and xm.meta_term_query("goldberg cat", sw, cfg["confirm"]) == '"goldberg cat"'
    assert len(planner.candidates(conn, T0, {**cfg, "planner": {**cfg["planner"], "max_meta_keys": 2}}, sw)) == 4


def test_sync_admits_stops_and_displaces_for_the_right_reasons(settings, conn):
    cfg = json.loads(json.dumps(settings.config("x")))
    cfg["planner"]["max_active_keys"] = 3
    cfg["planner"]["max_meta_keys"] = 2
    sw = load_stopwords(settings.config("stopwords"))
    coin(conn, "A" * 44, "Alpha Dog", "ALPHA", T0 - 3100)
    observe(conn, "A" * 44, T0 - 3000)
    coin(conn, "B" * 44, "Beta Fish", "BETA", T0 - 700)
    observe(conn, "B" * 44, T0 - 600)
    theme(conn, "goldberg cat", T0 - 50, 50.0)
    theme(conn, "zorblax coin", T0 - 40, 30.0)
    theme(conn, "quantum frog", T0 - 30, 10.0)   # third theme: beyond max_meta_keys
    st = planner.sync(conn, T0, cfg, sw, "rv")
    assert st["admitted"] == 3 and st["active"] == 3 and st["waiting"] == 1 and st["stopped"] == {}
    assert [r["key"] for r in planner.active_rows(conn)] == ["B" * 44, "A" * 44, "meta:goldberg cat"]
    assert [r["next_due_at"] for r in planner.active_rows(conn)] == [T0, T0, T0]  # a count as soon as a key is admitted
    assert planner.sync(conn, T0 + 1, cfg, sw, "rv")["admitted"] == 0  # idempotent: no second episode within the window
    # a younger coin observation outranks the theme and displaces it; the waiting theme still waits
    coin(conn, "F" * 44, "Fox Trot", "FOXT", T0 + 50)
    observe(conn, "F" * 44, T0 + 60)
    st = planner.sync(conn, T0 + 70, cfg, sw, "rv")
    assert st["admitted"] == 1 and st["stopped"] == {"displaced": 1} and [r["key"] for r in planner.active_rows(conn)] == ["F" * 44, "B" * 44, "A" * 44]
    assert conn.execute("SELECT stop_reason FROM x_keys WHERE key='meta:goldberg cat'").fetchone()[0] == "displaced"
    # graduation and death stop a coin key; the freed slots go to the strongest waiting themes
    conn.execute("INSERT INTO migrations(mint, block_time, received_at, source, sources) VALUES(?,?,?,'rpc_logs','[]')", ("B" * 44, T0 + 80, T0 + 81))
    conn.execute("UPDATE watchlist SET active=0, until=? WHERE mint=?", (T0 + 85, "A" * 44))
    st = planner.sync(conn, T0 + 90, cfg, sw, "rv")
    # goldberg cat had its episode and still counts among the 2 strongest themes, so quantum frog (the third) is no candidate
    assert st["stopped"] == {"graduated": 1, "dead": 1} and st["admitted"] == 1
    assert [r["key"] for r in planner.active_rows(conn)] == ["F" * 44, "meta:zorblax coin"]
    assert conn.execute("SELECT COUNT(*) FROM x_keys WHERE key='meta:quantum frog'").fetchone()[0] == 0
    # the window ends a key: expired without a confirmation, confirmed with one (even when the search never ran)
    conn.execute("UPDATE x_keys SET confirmed_at=? WHERE key=?", (T0 + 100, "meta:zorblax coin"))
    st = planner.sync(conn, T0 + 60 + 21600, cfg, sw, "rv")
    stopped = {r["key"]: r["stop_reason"] for r in conn.execute("SELECT key, stop_reason FROM x_keys WHERE stopped_at IS NOT NULL")}
    assert stopped["F" * 44] == "expired" and stopped["meta:zorblax coin"] == "confirmed"
    assert st["stopped"] == {"expired": 1, "confirmed": 1} and planner.active_rows(conn) == []
    # a new observation of an old key opens a new episode only once the old episode is a window old
    observe(conn, "F" * 44, T0 + 60 + 21600 + 10)
    watch.add(conn, "F" * 44, "observe", T0 + 60 + 21600 + 10, None, 200)
    st = planner.sync(conn, T0 + 60 + 21600 + 20, cfg, sw, "rv")
    assert st["admitted"] == 1 and conn.execute("SELECT COUNT(*) FROM x_keys WHERE key=?", ("F" * 44,)).fetchone()[0] == 2
    assert set(planner.STOP_REASONS) == {"confirmed", "refuted", "expired", "graduated", "dead", "displaced", "exhausted"}


# ----- point 2: the schedule and the per-key maxima ------------------------------------------------------------------------------
def test_count_schedule_and_decision_rules(settings, conn):
    cfg = json.loads(json.dumps(settings.config("x")))
    pcfg, window = cfg["planner"], cfg["confirm"]["window_s"]
    d = T0
    assert planner.next_due(d, d, pcfg, window) == d + 900 and planner.next_due(d, d + 7000, pcfg, window) == d + 7900
    assert planner.next_due(d, d + 7900, pcfg, window) == d + 11500 and planner.next_due(d, d + 20000, pcfg, window) is None
    sw = load_stopwords(settings.config("stopwords"))
    coin(conn, "A" * 44, "Alpha Dog", "ALPHA", d - 100)
    observe(conn, "A" * 44, d)
    coin(conn, "B" * 44, "Beta Fish", "BETA", d - 100)
    observe(conn, "B" * 44, d)
    planner.sync(conn, d, cfg, sw, "rv")
    reqs = {r["key"]: r for r in planner.due_requests(conn, d)}
    a, b = reqs["A" * 44], reqs["B" * 44]
    # refuted: the first count has no predecessor; three consecutive counts without growth of the 24 h count end the key
    assert planner.record_count(conn, a, d, 2, 5, 10, 0.005, cfg) == "counted"
    assert planner.record_count(conn, a, d + 900, 1, 5, 10, 0.005, cfg) == "counted"      # flat 1
    assert planner.record_count(conn, a, d + 1800, 3, 6, 10, 0.005, cfg) == "counted"     # growth: the streak resets
    assert planner.record_count(conn, a, d + 2700, None, None, 10, 0.005, cfg) == "counted"  # no information: the streak stays
    assert planner.record_count(conn, a, d + 3600, 3, 6, 10, 0.005, cfg) == "counted"     # flat 1
    assert planner.record_count(conn, a, d + 4500, 2, 6, 10, 0.005, cfg) == "counted"     # flat 2
    assert planner.record_count(conn, a, d + 5400, 2, 6, 10, 0.005, cfg) == "refuted"     # flat 3
    row = conn.execute("SELECT * FROM x_keys WHERE key=?", ("A" * 44,)).fetchone()
    assert row["stop_reason"] == "refuted" and row["counts_n"] == 7 and row["usd"] == pytest.approx(0.035) and row["peak_n_1h"] == 3 and row["next_due_at"] is None
    # confirmed: the threshold in the last hour; one search is now pending; counts stop; the search ends the key
    assert planner.record_count(conn, b, d, 12, 12, 10, 0.005, cfg) == "confirmed"
    row = conn.execute("SELECT * FROM x_keys WHERE key=?", ("B" * 44,)).fetchone()
    assert row["confirmed_at"] == d and row["search_pending"] == 1 and row["next_due_at"] is None and row["stopped_at"] is None
    assert [r["request"] for r in planner.due_requests(conn, d + 5000)] == ["ca_search"]
    planner.record_search(conn, b, d + 10, 2, 0.08)
    row = conn.execute("SELECT * FROM x_keys WHERE key=?", ("B" * 44,)).fetchone()
    assert row["stop_reason"] == "confirmed" and row["searches_n"] == 1 and row["authors_n"] == 2 and row["usd"] == pytest.approx(0.085) and row["usd_search"] == pytest.approx(0.08)
    assert planner.due_requests(conn, d + 20000) == []
    # exhausted: the per-key maximum of counts; expired: no further count fits in the window
    cfg2 = json.loads(json.dumps(cfg))
    cfg2["planner"]["max_counts_per_key"] = 2
    coin(conn, "C" * 44, "Gamma Ray", "GAMM", d - 100)
    observe(conn, "C" * 44, d)
    planner.sync(conn, d + 1, cfg2, sw, "rv")
    c = next(r for r in planner.due_requests(conn, d + 1) if r["key"] == "C" * 44)
    assert planner.record_count(conn, c, d + 1, 1, 1, 10, 0.005, cfg2) == "counted" and planner.record_count(conn, c, d + 901, 2, 2, 10, 0.005, cfg2) == "exhausted"
    coin(conn, "D" * 44, "Delta Wave", "DELW", d - 100)
    observe(conn, "D" * 44, d)
    planner.sync(conn, d + 2, cfg, sw, "rv")
    dreq = next(r for r in planner.due_requests(conn, d + 2) if r["key"] == "D" * 44)
    assert planner.record_count(conn, dreq, d + 21000, 1, 1, 10, 0.005, cfg) == "expired"  # a count at 5 h 50: the next would fall outside the window
    st = planner.stopped_between(conn, d, d + 86400)
    assert {k: v["n"] for k, v in st.items()} == {"refuted": 1, "confirmed": 1, "exhausted": 1, "expired": 1} and st["refuted"]["usd"] == pytest.approx(0.035)


def test_client_round_trip_in_shadow_mode(settings, conn, fx):
    clock = Clock()
    coin(conn, "A" * 44, "Alpha Dog", "ALPHA", T0 - 100)
    da = observe(conn, "A" * 44, T0 - 10)
    coin(conn, "B" * 44, "Beta Fish", "BETA", T0 - 90)
    db_ = observe(conn, "B" * 44, T0 - 5)   # younger: first in the plan
    req = FakeRequest({"/tweets/counts/recent": [counts_body(13, 19), counts_body(0), counts_body(0), counts_body(0), counts_body(0)],
                       "/tweets/search/recent": fx["ca_search"], "/users?": USERS})
    c = client(settings, conn, clock, req)
    s = c.confirm(T0)
    # B confirmed at its first count -> one search (3 posts) + one lookup of the 2 authors -> stopped; A counted (0)
    assert (s["counts"], s["searches"], s["posts"], s["authors"], s["admitted"], s["active"]) == (2, 1, 3, 2, 2, 2)
    assert s["outcomes"] == {"confirmed": 1, "counted": 1}
    b = conn.execute("SELECT * FROM x_keys WHERE key=?", ("B" * 44,)).fetchone()
    assert b["stop_reason"] == "confirmed" and b["counts_n"] == 1 and b["searches_n"] == 1 and b["authors_n"] == 2 and b["decision_id"] == db_
    assert b["usd"] == pytest.approx(0.005 + 3 * 0.005 + 2 * 0.01) and b["usd_search"] == pytest.approx(0.035)
    assert [u for u in req.calls if "/users?" in u["url"]][0]["url"].count("ids=1001%2C1002") == 1 or "ids=1001,1002" in [u for u in req.calls if "/users?" in u["url"]][0]["url"]
    assert "expansions" not in [u for u in req.calls if "/tweets/search/recent" in u["url"]][0]["url"]  # no author expansion on the confirmation search
    assert conn.execute("SELECT author, followers FROM social_posts WHERE id='1990000000000000101'").fetchone()[:] == ("degen_a", 999)
    spend = c.xb.spend_by_kind(T0)
    assert spend["ca_search"] == {"n": 3, "usd": pytest.approx(0.015), "units": {"post": 3}} and spend["ca_authors"] == {"n": 2, "usd": pytest.approx(0.02), "units": {"author": 2}}
    assert conn.execute("SELECT COUNT(*) FROM shadow_confirmations").fetchone()[0] == 2 and conn.execute("SELECT COUNT(*) FROM social_counts").fetchone()[0] == 0
    # A: flat counts every 15 minutes; refuted at the fourth count (45 min, $0.02); nothing after that; B never counted again
    for t in (T0 + 900, T0 + 1800, T0 + 2700):
        clock.t = t
        s = c.confirm(t)
        assert s["counts"] == 1 and s["searches"] == 0
    a = conn.execute("SELECT * FROM x_keys WHERE key=?", ("A" * 44,)).fetchone()
    assert a["stop_reason"] == "refuted" and a["counts_n"] == 4 and a["usd"] == pytest.approx(0.02) and a["decision_id"] == da
    clock.t = T0 + 3600
    assert c.confirm(T0 + 3600)["counts"] == 0 and len(req.calls) == 5 + 1 + 1  # 5 counts, 1 search, 1 lookup
    assert conn.execute("SELECT COUNT(*) FROM shadow_confirmations WHERE decision_id=?", (da,)).fetchone()[0] == 4
    assert planner.status_counters(conn, T0 + 3600)["stopped_today"] == {"confirmed": {"n": 1, "usd": pytest.approx(0.04), "counts": 1, "searches": 1},
                                                                         "refuted": {"n": 1, "usd": pytest.approx(0.02), "counts": 4, "searches": 0}}
    assert c.stats["ca_authors"] == 2 and "deferred_ca_counts" not in c.stats


def test_account_watch_intervals_follow_the_peak_hours(settings, conn, fx):
    clock = Clock()
    req = FakeRequest({"/tweets/search/recent": fx["search"], "/tweets?": fx["lookup"], "/tweets/counts/recent": fx["counts"]})
    c = client(settings, conn, clock, req, shadow=False)
    mcfg, kcfg = settings.config("x")["mega"], settings.config("x")["kol"]
    assert xm.interval_for(mcfg, True, 60, 120) == 60 and xm.interval_for(mcfg, False, 60, 120) == 120
    assert xm.interval_for(kcfg, True, 180, 300) == 180 and xm.interval_for(kcfg, False, 180, 300) == 300
    assert xm.interval_for({"interval_s": 45}, False, 60, 120) == 45  # an old config keeps its single interval
    off = T0 - 8 * 3600  # 05:05 UTC: outside the peak hours
    assert not c.xb.is_peak(off) and c.xb.is_peak(T0)
    db.kv_set(conn, "x.mega.last", str(off - 61), off)
    db.kv_set(conn, "x.kol.last", str(off - 181), off)
    db.kv_set(conn, "x.refresh.last", str(off), off)
    clock.t = off
    s = c.tick(off)
    assert "mega" not in s and "kol" not in s  # 61 s and 181 s are not due outside the peak hours
    db.kv_set(conn, "x.mega.last", str(off - 121), off)
    db.kv_set(conn, "x.kol.last", str(off - 301), off)
    s = c.tick(off + 1)
    assert "mega" in s and "kol" in s
    clock.t = T0
    db.kv_set(conn, "x.mega.last", str(T0 - 61), T0)
    db.kv_set(conn, "x.kol.last", str(T0 - 181), T0)
    s = c.tick(T0)
    assert "mega" in s and "kol" in s  # in the peak hours the old cadence stands


# ----- point 3: the budget planner -----------------------------------------------------------------------------------------------
@pytest.mark.parametrize("cap", [10.0, 12.5, 15.0, 20.0])
def test_planner_never_exceeds_the_cap_whatever_the_responses(settings, conn, cap):
    clock = Clock(T0 - 13 * 3600 + 60)  # 00:06 UTC: a whole day ahead
    budget = make_budget(settings, conn, clock, str(cap))
    xb = XBudget(conn, budget, settings.config("x"), cap, clock=clock)
    rng = random.Random(int(cap * 100))
    kinds = ["ca_counts", "ca_search", "ca_authors", "mega_search", "kol_search", "refresh"]
    admitted = deferred = refused = 0
    for step in range(4000):
        now = clock.t + step * 20.0  # 4000 requests over ~22 hours
        kind = rng.choice(kinds)
        ceiling = {"ca_counts": 0.005, "ca_search": 0.08, "ca_authors": 0.03, "mega_search": 0.19, "kol_search": 0.23, "refresh": 0.0}[kind]
        expected = ceiling * rng.random()
        v = xb.admit(kind, ceiling, expected, now)
        if v.allowed:
            admitted += 1
            actual = ceiling * rng.random()  # whatever the response: never above the ceiling
            xb.book(kind, 1, actual, now)
            assert xb.spend_today(now) <= cap + 1e-9
        elif v.deferred:
            deferred += 1
            assert v.reason == "hourly_envelope" and v.retry_at == (int(now) // 3600 + 1) * 3600.0
        else:
            refused += 1
            assert v.reason in ("daily_cost_cap", "account_watch_reserve", "kol_daily_cap")
    assert xb.spend_today(clock.t + 4000 * 20.0) <= cap and admitted > 0 and deferred > 0 and refused > 0
    assert xb.spend_today(clock.t + 4000 * 20.0, ("ca_counts", "ca_search", "ca_authors")) <= 0.7 * cap + 1e-9  # the reserve held
    assert xb.spend_today(clock.t + 4000 * 20.0, ("kol_search",)) <= 1.0 + 1e-9  # the KOL cap held
    # deferrals are counted, never ledgered; refusals are ledgered
    assert sum(xb.deferred_today(clock.t).values()) == deferred
    assert conn.execute("SELECT COUNT(*) FROM api_ledger WHERE source='x' AND blocked_reason='hourly_envelope'").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM api_ledger WHERE source='x' AND blocked_reason IS NOT NULL").fetchone()[0] == refused


def test_admission_prefers_the_search_then_counts_and_defers_the_dear_one_first(settings, conn, fx):
    clock = Clock()
    cap = 0.5  # 13:05 UTC: the envelope allows 0.5 x 7.4 / 17.4 = $0.2126 of the day so far
    coin(conn, "A" * 44, "Alpha Dog", "ALPHA", T0 - 100)
    observe(conn, "A" * 44, T0 - 10)
    coin(conn, "B" * 44, "Beta Fish", "BETA", T0 - 90)
    observe(conn, "B" * 44, T0 - 5)
    req = FakeRequest({"/tweets/counts/recent": [counts_body(13, 19), counts_body(0), counts_body(1)], "/tweets/search/recent": fx["ca_search"], "/users?": USERS})
    c = client(settings, conn, clock, req, cap=cap)
    c.xb.book("mega_search", 1, 0.15, T0)  # $0.0626 of envelope room: a count ($0.005) fits, the search ceiling ($0.08) does not
    s = c.confirm(T0)
    assert (s["counts"], s["searches"], s["deferred"]) == (2, 0, 1)  # B confirmed; its search deferred, A still counted
    assert [r["request"] for r in planner.due_requests(conn, T0 + 1)] == ["ca_search"]  # the search is first in the plan
    assert c.xb.deferred_today(T0) == {"ca_search": 1} and conn.execute("SELECT COUNT(*) FROM api_ledger WHERE blocked_reason IS NOT NULL").fetchone()[0] == 0
    # the next hour's allowance admits the search first, then the due count
    t1 = T0 + 3600  # 14:05 UTC: allowance 0.5 x 8.4 / 17.4 = $0.2414
    clock.t = t1
    s = c.confirm(t1)
    assert (s["searches"], s["counts"], s["deferred"]) == (1, 1, 0)
    assert conn.execute("SELECT stop_reason FROM x_keys WHERE key=?", ("B" * 44,)).fetchone()[0] == "confirmed"
    # a hard refusal (the cap itself) is ledgered and ends the round for the counts
    conn.execute("DELETE FROM x_spend")
    c.xb.book("mega_search", 1, 0.499, t1)
    clock.t = t1 + 900
    s = c.confirm(t1 + 900)
    assert s["counts"] == 0 and c.stats.get("blocked_daily_cost_cap") == 1
    assert conn.execute("SELECT blocked_reason FROM api_ledger WHERE blocked_reason IS NOT NULL").fetchone()[0] == "daily_cost_cap"
    assert c.xb.spend_today(t1 + 900) <= cap


# ----- point 5: the console comparison ------------------------------------------------------------------------------------------------
def _ledger(conn, day="2026-10-27"):
    t = day_bounds(day)[0] + 3600
    rows = [("mega_search", "post", 3, 0.015), ("mega_search", "author", 2, 0.02), ("kol_search", "post", 10, 0.05), ("ca_counts", "request", 40, 0.2),
            ("ca_search", "post", 12, 0.06), ("ca_authors", "author", 4, 0.04), ("refresh", "post", 50, 0.0)]
    for k, u, n, usd in rows:
        conn.execute("INSERT INTO x_spend(day, kind, n, usd, ts, unit) VALUES(?,?,?,?,?,?)", (day, k, n, usd, t, u))
    conn.execute("INSERT INTO x_spend(day, kind, n, usd, ts) VALUES(?, 'mega_search', 1, 0.005, ?)", (day, t))  # a pre-M7 row: no unit
    for k, n in (("mega_search", 100), ("ca_counts", 40), ("ca_search", 3), ("ca_authors", 3)):
        for i in range(n):
            conn.execute("INSERT INTO api_ledger(ts, source, method, cost, ok) VALUES(?, 'x', ?, 0.001, 1)", (t + i, k))


def test_calibrate_puts_the_console_next_to_the_ledger_and_writes_nothing(settings, conn, capsys):
    _ledger(conn)
    c = calibrate(conn, settings, "2026-10-27", 0.60)
    assert c["booked_usd"] == pytest.approx(0.39) and c["mixed"] == {"usd": pytest.approx(0.005), "units": 1} and c["gap_rel"] == pytest.approx(0.21 / 0.39)
    assert c["uniform_factor"] == pytest.approx(0.6 / 0.39) and c["verdict"] == "adjust"
    comps = c["components"]
    assert comps["post"]["units"] == 25 and comps["request"]["units"] == 40 and comps["author"]["units"] == 6 and comps["refresh"]["units"] == 50
    hyp = {h["component"]: h for h in c["single_component_hypotheses"]}
    assert hyp["request"]["implied_price"] == pytest.approx((0.60 - 0.19) / 40) and hyp["request"]["config_line"] == '"counts_request_usd": 0.0102'
    assert hyp["post"]["implied_price"] == pytest.approx((0.60 - 0.265) / 25) and hyp["author"]["config_line"] == '"author_expansion_usd": 0.045'  # the pre-M7 row counts in 'other'
    assert c["uniform_config_lines"][0] == '"post_usd": 0.0077'
    assert calibrate(conn, settings, "2026-10-27", 0.40)["verdict"] == "confirmed"       # within 5%
    assert calibrate(conn, settings, "2026-10-28", 1.0)["verdict"] == "nothing_booked"   # a day without rows
    n_spend, n_ledger = conn.execute("SELECT COUNT(*) FROM x_spend").fetchone()[0], conn.execute("SELECT COUNT(*) FROM api_ledger").fetchone()[0]
    conn.close()
    root = str(settings.root)
    assert main(["--root", root, "--quiet", "social", "calibrate", "--day", "2026-10-27", "--console-usd", "0.60"]) == 0
    out = capsys.readouterr().out
    assert "writes nothing" in out and "uniform factor console / booked = 1.538" in out and '"counts_request_usd": 0.0102' in out and "verdict: adjust" in out
    assert "pre-M7 rows without a unit" in out and "PLACEHOLDER" not in out
    assert main(["--root", root, "--quiet", "--json", "social", "calibrate", "--day", "2026-10-27", "--console-usd", "0.39"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["verdict"] == "confirmed" and data["console_usd"] == 0.39
    assert main(["--root", root, "--quiet", "social", "calibrate", "--day", "nope", "--console-usd", "1"]) == 2
    assert main(["--root", root, "--quiet", "social", "calibrate", "--console-usd", "1"]) == 2
    capsys.readouterr()
    c2 = db.connect(settings.db_path)
    assert c2.execute("SELECT COUNT(*) FROM x_spend").fetchone()[0] == n_spend and c2.execute("SELECT COUNT(*) FROM api_ledger").fetchone()[0] == n_ledger
    c2.close()


# ----- point 4: quality per dollar in the reports -------------------------------------------------------------------------------------
def test_reports_show_quality_per_dollar(settings, conn):
    from tests.test_m5_week_report import _grad_with_outcome

    day = "2026-10-27"
    t0 = day_bounds(day)[0] + 10 * 3600
    now = t0 + 200000
    db.kv_set(conn, "ingest.pumpportal.first_started", str(t0 - 86400), t0)
    # three coin observations of the day: W (confirmed within the hour, a winner), L (counted, never confirmed, a known miss), N (never counted, unknown)
    for mint, name in (("WINNER1", "Win One"), ("LOSER1", "Lose One"), ("NOCOUNT", "No Count")):
        conn.execute("INSERT INTO coins(mint, name, symbol, block_time, received_at, source, sources) VALUES(?,?,?,?,?,'rpc_logs','[]')", (mint, name, "XXX", t0 - 10, t0 - 9))
    dw = observe(conn, "WINNER1", t0)
    dl = observe(conn, "LOSER1", t0 + 60)
    dn = observe(conn, "NOCOUNT", t0 + 120)
    dm = theme(conn, "goldberg cat", t0 + 180, 50.0)
    for did, mint, hit in ((dw, "WINNER1", 1), (dl, "LOSER1", 0)):
        conn.execute("INSERT INTO outcomes(decision_id, subject, mint, anchor_time, horizon_s, measured_at, peak_mcap_usd, peak_src, volume_usd, hit_100k, bundled, method_version)"
                     " VALUES(?, 'decision', ?, ?, 86400, ?, 1, 'dexscreener', 1, ?, 0, 'm1.0')", (did, mint, t0, t0 + 90000, hit))
    _grad_with_outcome(conn, "GRAD", t0 + 600, 1, 250000.0, 300)  # a graduation so the day has a universe

    def count(ts, did, kind, key, n1h, n24):
        conn.execute("INSERT INTO shadow_confirmations(ts, decision_id, decision_kind, decision_as_of, key, kind, n_last_hour, n_24h, query, threshold,"
                     " confirmed_1h, confirmed_24h, rules_version) VALUES(?,?,?,?,?,?,?,?,'q',10,?,?,'r')",
                     (ts, did, kind, t0, key, "mint" if kind == "observe" else "meta", n1h, n24, 1 if n1h >= 10 else 0, 1 if n24 >= 10 else 0))

    count(t0 + 50, dw, "observe", "WINNER1", 12, 30)          # confirmed 50 s after the decision
    count(t0 + 950, dw, "observe", "WINNER1", 15, 40)         # a count after the confirmation: waste (the M6 behaviour)
    count(t0 + 1850, dw, "observe", "WINNER1", 16, 45)        # waste
    count(t0 + 100, dl, "observe", "LOSER1", 1, 1)
    count(t0 + 1000, dl, "observe", "LOSER1", 1, 1)
    count(t0 + 7400, dm, "observe_meta", "meta:goldberg cat", 11, 11)  # the theme: confirmed, but two hours after the decision
    for kind, unit, n, usd in (("ca_counts", "request", 6, 0.03), ("ca_search", "post", 10, 0.05), ("ca_authors", "author", 3, 0.03), ("mega_search", "post", 5, 0.025)):
        conn.execute("INSERT INTO x_spend(day, kind, n, usd, ts, unit) VALUES(?,?,?,?,?,?)", (day, kind, n, usd, t0 + 100, unit))
    for key, did, reason, usd, counts, searches in (("WINNER1", dw, "confirmed", 0.095, 3, 1), ("LOSER1", dl, "refuted", 0.02, 4, 0), ("meta:goldberg cat", dm, "confirmed", 0.085, 1, 1),
                                                   ("OLDKEY", 999, "expired", 0.06, 12, 0)):
        conn.execute("INSERT INTO x_keys(key, decision_id, kind, decision_kind, decision_as_of, query, started_at, counts_n, searches_n, usd, stopped_at, stop_reason)"
                     " VALUES(?,?,?,?,?,'q',?,?,?,?,?,?)", (key, did, "meta" if key.startswith("meta:") else "mint", "observe", t0, t0, counts, searches, usd, t0 + 8000, reason))
    rep = build_report(conn, settings, day, now)
    q = rep["x_shadow"]["quality"]
    assert q["spend_usd"] == {"ca_counts": pytest.approx(0.03), "ca_search": pytest.approx(0.05), "ca_authors": pytest.approx(0.03), "total": pytest.approx(0.11)}
    assert q["counts_taken"] == 6 and q["confirmed_observations"] == 2 and q["usd_per_confirmed"] == pytest.approx(0.055)
    assert q["observations"] == 4 and q["observations_counted"] == 3 and q["confirmed_any"] == 2 and q["confirmed_within_1h"] == 1
    assert q["confirmed_within_1h_share"] == pytest.approx(1 / 3) and q["counts_after_first_confirmation"] == 2 and q["waste_usd"] == pytest.approx(2 * 0.03 / 6)
    assert {k: v["n"] for k, v in q["keys_stopped"].items()} == {"confirmed": 2, "refuted": 1, "expired": 1} and q["refuted"] == {"n": 1, "usd": pytest.approx(0.02)}
    ob = q["outcomes_by_confirmation"]
    assert (ob["confirmed"]["decisions"], ob["confirmed"]["elapsed"], ob["confirmed"]["winner"], ob["confirmed"]["rate"]) == (1, 1, 1, 1.0)
    assert (ob["unconfirmed"]["decisions"], ob["unconfirmed"]["not_winner"], ob["unconfirmed"]["rate"]) == (1, 1, 0.0)
    assert (ob["not_counted"]["decisions"], ob["not_counted"]["unknown"], ob["not_counted"]["elapsed"], ob["not_counted"]["rate"]) == (1, 1, 1, 0.0)  # unknown stays in the denominator
    assert rep["x_shadow"]["confirmations"]["confirmed_1h"] == 2  # the milestone-6 block is untouched
    md = render_markdown(rep)
    assert "Kosten van de X-bevestiging op deze dag: $0.11 (tellingen $0.03, zoekopdrachten $0.05, auteursopvragingen $0.03); 6 tellingen; per bevestigde observatie: **$0.06** (2 bevestigd)" in md
    assert "bevestigd binnen 1 u na de beslissing: **1** (33%); ooit bevestigd: 2" in md and "verspilling): 2 (≈ $0.01)" in md
    assert "gestopt op deze dag: confirmed 2, expired 1, refuted 1 (kosten $0.02); weerlegd kostte samen $0.02" in md
    assert "bevestigd: 1 observaties, verstreken 1 (winnaar 1, geen winnaar 0, onbekend 0), nog bezig 0, trefkans 100%" in md
    assert "zonder telling: 1 observaties, verstreken 1 (winnaar 0, geen winnaar 0, onbekend 1), nog bezig 0, trefkans 0%" in md and "Geen advies" in md
    # a day without any X activity keeps the milestone-6 wording
    assert "Geen schaduwgegevens" in render_markdown(build_report(conn, settings, "2026-10-20", now))
    # the week report sums the days and prints the quality table
    wrep = wk.build_week_report(conn, settings, day_key(t0 + 86400), 2, now)
    xs = wrep["x_shadow"]
    assert xs["spend_usd"] == pytest.approx(0.11) and xs["confirmed_observations"] == 2 and xs["usd_per_confirmed"] == pytest.approx(0.055)
    assert xs["confirmed_within_1h"] == 1 and xs["observations_counted"] == 3 and xs["waste_counts"] == 2 and xs["refuted_n"] == 1 and xs["refuted_usd"] == pytest.approx(0.02)
    assert xs["keys_stopped"] == {"confirmed": 2, "expired": 1, "refuted": 1} and xs["by_confirmation"]["confirmed"]["winner"] == 1 and xs["by_confirmation"]["unconfirmed"]["rate"] == 0.0
    wmd = wk.render_markdown(wrep)
    assert "### Kwaliteit per dollar van de X-bevestiging" in wmd and f"| {day} | $0.11 | 2 | $0.06 | 3 | 1 | 2 | 1 ($0.02) |" in wmd
    assert "per bevestigde observatie: **$0.06**" in wmd and "geen advies over X live" in wmd


# ----- status and schema ----------------------------------------------------------------------------------------------------------
def test_status_shows_the_planner_and_plan_versus_actual(settings, conn, capsys):
    t = 1_793_059_200.0 + 13 * 3600
    conn.execute("INSERT INTO x_keys(key, decision_id, kind, decision_kind, decision_as_of, query, started_at, next_due_at, counts_n) VALUES('M1', 1, 'mint', 'observe', ?, 'q', ?, ?, 2)", (t - 100, t - 100, t - 1))
    conn.execute("INSERT INTO x_keys(key, decision_id, kind, decision_kind, decision_as_of, query, started_at, confirmed_at, search_pending) VALUES('M2', 2, 'mint', 'observe', ?, 'q', ?, ?, 1)", (t - 200, t - 200, t - 10))
    conn.execute("INSERT INTO x_keys(key, decision_id, kind, decision_kind, decision_as_of, query, started_at, counts_n, usd, stopped_at, stop_reason) VALUES('M3', 3, 'mint', 'observe', ?, 'q', ?, 4, 0.02, ?, 'refuted')", (t - 5000, t - 5000, t - 500))
    conn.execute("INSERT INTO x_spend(day, kind, n, usd, ts, unit) VALUES(?, 'ca_counts', 6, 0.03, ?, 'request')", (day_key(t), t))
    db.counter_bump(conn, f"x.planner.deferred.ca_search.{day_key(t)}", 2, t)
    conn.close()
    import fart.cli as cli

    cli_now = {"v": t}
    import pytest as _pt

    mp = _pt.MonkeyPatch()
    mp.setattr(cli, "now", lambda: cli_now["v"])
    try:
        assert main(["--root", str(settings.root), "--quiet", "status"]) == 0
        out = capsys.readouterr().out
        assert "x planner: active keys 2 (mint 2), search pending 1, due now 1; started today 3; stopped today: refuted 1 ($0.02); deferred today: ca_search 2" in out
        assert "x plan vs spend today (planned/actual USD, % of the $3.00 cap): " in out and "ca_counts $" in out and "/$0.03 (" in out
        assert "x spend today: ca_counts $0.030 (6)" in out
        assert main(["--root", str(settings.root), "--quiet", "--json", "status"]) == 0
        data = json.loads(capsys.readouterr().out)
        assert data["x_planner"]["active_total"] == 2 and data["x_planner"]["search_pending"] == 1 and data["x_planner"]["stopped_today"]["refuted"]["n"] == 1
        plan = {p["kind"]: p for p in data["x_plan"]}
        assert plan["ca_counts"]["actual_usd"] == pytest.approx(0.03) and plan["ca_counts"]["planned_usd"] is not None and plan["ca_search"]["deferred"] == 2
        assert plan["ca_counts"]["actual_share"] == pytest.approx(0.01) and data.get("x_plan_error") is None
    finally:
        mp.undo()


def test_schema_v7_is_idempotent_and_the_old_spend_rows_keep_working(settings):
    c = db.connect(settings.db_path)
    assert db.schema_version(c) == db.SCHEMA_VERSION >= 7 and db.migrate(c) == db.SCHEMA_VERSION  # v7 ran (milestone 10 adds v8 on top)
    tables = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "x_keys" in tables and "unit" in {r[1] for r in c.execute("PRAGMA table_info(x_spend)")}
    c.execute("INSERT INTO x_spend(day, kind, n, usd, ts) VALUES('2026-10-26', 'mega_search', 10, 0.42, 1)")  # a milestone-6 row
    budget = make_budget(settings, c, Clock(), "15.00")
    xb = XBudget(c, budget, settings.config("x"), 15.0, clock=Clock(1_792_972_800.0 + 10))  # 2026-10-26
    assert xb.spend_by_kind()["mega_search"] == {"n": 10, "usd": pytest.approx(0.42), "units": {"mixed": 10}}
    c.close()
