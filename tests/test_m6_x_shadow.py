"""Milestone 6, point 1: X in shadow mode (X_MODE). With a bearer token the default is `shadow`: fart-social runs
completely within its budget, but nothing of it reaches `decisions`: fast posts -> `shadow_decisions`, confirmation
counts -> `shadow_confirmations`, the engine passes no mega post and neutral X features. Without a token everything
is as before milestone 6. `live` needs X_MODE=live AND the `x_live` rules key. All HTTP is stubbed."""
from __future__ import annotations

import json
import sqlite3
import urllib.error

import pytest

from fart import db
from fart.cli import main
from fart.config import load_settings
from fart.engine import features
from fart.engine.service import X_NEUTRAL, Engine
from fart.ingest import watch
from fart.measure.report import build_report, render_markdown
from fart.net import http as nh
from fart.social import x as xm
from fart.util import day_bounds, day_key
from tests.test_engine import T0 as E0, add_coin, add_trade
from tests.test_social import Clock, FakeRequest, T0, XClientFactory, make_budget
from tests.test_social import fx  # noqa: F401  (fixture)
from fart.social.xbudget import XBudget


def x_settings(tmp_path, monkeypatch, token="PLACEHOLDER-BEARER", mode=None, x_live=None):
    for k in ("X_BEARER_TOKEN", "X_MODE", "RPC_HTTP_URL", "RPC_WSS_URL", "TELEGRAM_BOT_TOKEN"):
        monkeypatch.delenv(k, raising=False)
    env = f"X_BEARER_TOKEN={token}\n" + (f"X_MODE={mode}\n" if mode is not None else "")
    tmp_path.mkdir(parents=True, exist_ok=True)
    (tmp_path / ".env").write_text(env)
    monkeypatch.setenv("FART_RULES_VERSION", "test-rules-v1")
    s = load_settings(root=tmp_path)
    s.ensure_dirs()
    if x_live is not None:
        s.configs["engine"]["x_live"] = x_live
    return s


def shadow_client(settings, conn, clock, request, cap=3.0):
    budget = make_budget(settings, conn, clock, str(cap))
    xb = XBudget(conn, budget, settings.config("x"), cap, clock=clock)
    c = XClientFactory(conn, budget, xb, settings, settings.config("x_kols")["accounts"], clock, request)
    c.shadow = True
    return c


# ----- the mode ---------------------------------------------------------------------------------------------------------
def test_x_mode_resolution_and_describe(tmp_path, monkeypatch):
    s = x_settings(tmp_path, monkeypatch, token="")
    assert s.x_mode == "off" and not s.x_active and s.x_mode_note is None and s.describe()["x_mode"] == "off"
    s = x_settings(tmp_path, monkeypatch)  # a token, X_MODE unset: shadow
    assert s.x_mode == "shadow" and s.x_shadow and s.x_active and not s.x_live and s.x_mode_note is None
    assert s.describe()["x_mode_requested"] == "unset" and "PLACEHOLDER" not in json.dumps(s.describe())
    assert x_settings(tmp_path, monkeypatch, mode="off").x_mode == "off"
    assert x_settings(tmp_path, monkeypatch, mode="Shadow").x_mode == "shadow"
    # live is refused without the rules key: shadow, with a note; with the key it is live
    s = x_settings(tmp_path, monkeypatch, mode="live")
    assert s.x_mode == "shadow" and not s.x_live_allowed and "refused" in s.x_mode_note and "x_live" in s.x_mode_note
    s = x_settings(tmp_path, monkeypatch, mode="live", x_live=True)
    assert s.x_mode == "live" and s.x_live and s.x_live_allowed and s.x_mode_note is None
    assert x_settings(tmp_path, monkeypatch, mode="shadow", x_live=True).x_mode == "shadow"  # the key alone changes nothing
    # live or shadow without a token is off, and says so; an unknown value falls back and says so
    s = x_settings(tmp_path, monkeypatch, token="", mode="live")
    assert s.x_mode == "off" and "without X_BEARER_TOKEN" in s.x_mode_note
    s = x_settings(tmp_path, monkeypatch, mode="loud")
    assert s.x_mode == "shadow" and "not one of" in s.x_mode_note
    # the process environment wins over .env, like every other key
    monkeypatch.setenv("X_MODE", "off")
    assert load_settings(root=tmp_path).x_mode == "off"


def test_x_neutral_matches_the_rule_file_on_empty_tables(conn):
    assert features.x_features(conn, "AnyMint", T0) == X_NEUTRAL  # pins the engine's neutral set to features.x_features


# ----- shadow writes ----------------------------------------------------------------------------------------------------
def test_shadow_fast_post_lands_in_shadow_decisions_and_never_in_decisions(settings, conn, fx):
    clock = Clock()
    req = FakeRequest({"/tweets/search/recent": fx["search"], "/tweets?": fx["lookup"]})
    c = shadow_client(settings, conn, clock, req)
    c.mega_watch(T0)
    post_t = xm.parse_created_at("2026-10-26T12:50:00.000Z")
    for i, (name, creator, dt) in enumerate((("Zorblax", "A", 120), ("zorblax cat", "B", 600), ("ZORBLAX 2", "A", -300))):
        conn.execute("INSERT INTO coins(mint, name, symbol, creator, block_time, received_at, source, sources) VALUES(?,?,?,?,?,?,'rpc_logs','[]')",
                     (f"MINT{i}", name, "X", creator, post_t + dt, post_t + dt + 1))
    clock.t = T0 + 120
    c.refresh_recent(clock.t)
    assert c.fast_post_decisions(clock.t) == 1
    assert conn.execute("SELECT COUNT(*) FROM decisions").fetchone()[0] == 0
    d = conn.execute("SELECT * FROM shadow_decisions").fetchone()
    assert d["kind"] == "shadow" and d["would_kind"] == "fast_post" and d["term"] == "x:1990000000000000001" and d["verdict"] == "observe"
    assert json.loads(d["features_json"])["mints"] == ["MINT0", "MINT1", "MINT2"] and d["rules_version"] == "test-rules"
    # idempotent per post, and the money is booked exactly as in live mode
    assert c.fast_post_decisions(clock.t + 15) == 0 and conn.execute("SELECT COUNT(*) FROM shadow_decisions").fetchone()[0] == 1
    assert c.xb.spend_by_kind(clock.t)["mega_search"] == {"n": 3, "usd": pytest.approx(0.035)}
    assert c.tick(clock.t + 61)["x_mode"] == "shadow"
    # the engine would see the fast post as a mega feature, but only in live mode (next test); append-only like decisions
    assert features.mega_post_feature(conn, clock.t + 1)["account"] == "elonmusk"
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("DELETE FROM shadow_decisions")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE shadow_decisions SET verdict='x'")
    # the notifier never sends it: there is no fast_post decision to build from
    from fart.net.budget import Budget, load_source_configs
    from fart.notify.service import Notifier

    n = Notifier(conn, settings, Budget(conn, load_source_configs(settings.config("budget"), {}), settings.lock_path, clock=clock), None, clock=clock)
    assert n.build_fast_posts(clock.t + 1) == 0 and conn.execute("SELECT COUNT(*) FROM outbox").fetchone()[0] == 0


def test_shadow_confirmation_counts_land_in_shadow_confirmations_linked_to_the_decision(settings, conn, fx):
    clock = Clock()
    mint = "7xKXmintZorblaxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"
    conn.execute("INSERT INTO coins(mint, name, symbol, block_time, received_at, source, sources) VALUES(?,?,?,?,?,'rpc_logs','[]')", (mint, "Zorblax Cat", "ZORB", T0 - 900, T0 - 899))
    cur = conn.execute("INSERT INTO decisions(as_of, created_at, kind, mint, features_json, rules_version, verdict, reasons_json) VALUES(?,?,'observe',?,'{}','r','observe','[]')", (T0 - 840, T0 - 840, mint))
    did = cur.lastrowid
    watch.add(conn, mint, "observe", T0 - 840, None, 200)
    cur = conn.execute("INSERT INTO decisions(as_of, created_at, kind, term, features_json, rules_version, verdict, reasons_json) VALUES(?,?,'observe_meta','goldberg cat','{}','r','observe','[]')", (T0 - 100, T0 - 100))
    mid = cur.lastrowid
    quiet = {"data": [{"start": "2026-10-26T12:00:00.000Z", "end": "2026-10-26T13:00:00.000Z", "tweet_count": 0}], "meta": {"total_tweet_count": 0}}
    req = FakeRequest({"/tweets/counts/recent": [fx["counts"], quiet], "/tweets/search/recent": fx["ca_search"]})
    c = shadow_client(settings, conn, clock, req)
    keys = c.confirmation_keys(T0)
    assert [(k["key"], k["decision_id"], k["decision_kind"]) for k in keys] == [(mint, did, "observe"), ("meta:goldberg cat", mid, "observe_meta")]
    assert c.confirm(T0) == {"counts": 2, "searches": 1, "posts": 3}  # the 13-per-hour count still triggers the search (same calls, same money)
    assert conn.execute("SELECT COUNT(*) FROM social_counts").fetchone()[0] == 0
    rows = conn.execute("SELECT * FROM shadow_confirmations ORDER BY id").fetchall()
    assert [(r["key"], r["decision_id"], r["n_last_hour"], r["n_24h"], r["threshold"], r["confirmed_1h"], r["confirmed_24h"]) for r in rows] == [
        (mint, did, 13, 19, 10, 1, 1), ("meta:goldberg cat", mid, 0, 0, 10, 0, 0)]
    assert rows[0]["decision_as_of"] == T0 - 840 and rows[0]["rules_version"] == "test-rules"
    # the keys are due again after the interval, judged on the shadow table
    assert c.confirmation_keys(T0 + 60) == [] and len(c.confirmation_keys(T0 + 901)) == 2
    # the features of the mint stay what they are without X counts (the engine neutralizes the ca posts as well)
    assert features.x_features(conn, mint, T0 + 1)["x_count_1h"] is None
    assert c.xb.spend_by_kind(T0)["ca_counts"] == {"n": 2, "usd": pytest.approx(0.01)}
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("DELETE FROM shadow_confirmations")


# ----- the engine guarantee -------------------------------------------------------------------------------------------
def _passing_world(conn):
    add_coin(conn, "M", E0, name="Goldberg One", dev_buy=1.0)
    watch.add(conn, "M", "probation", E0, 300, 200)
    for i in range(30):
        add_trade(conn, "M", E0 + 1 + i, f"B{i}", 0.5, sig=f"b{i}", vsol=(31 + i * 0.5) * 1e9, vtok=1.0e15)
    for i in range(3):
        add_coin(conn, f"G{i}", E0 - 100 * (i + 1), name=f"Goldberg {i}", creator=f"C{i}")


def _x_world(conn):
    """What a shadow day leaves behind in the social tables: a fast mega post, confirmation posts and a count for M."""
    conn.execute("INSERT INTO social_posts(id, author, author_id, text, created_at, received_at, likes, kind, fast, fast_at, likes_per_min) VALUES"
                 " ('p1', 'elonmusk', '1', 'Goldberg to the moon', ?, ?, 50000, 'mega', 1, ?, 900.0)", (E0 - 60, E0 - 59, E0 + 20))
    conn.execute("INSERT INTO social_posts(id, author, author_id, text, created_at, received_at, likes, kind, query_key, followers) VALUES"
                 " ('p2', 'someone', '2', 'M is pumping', ?, ?, 10, 'ca', 'M', 1000)", (E0 + 10, E0 + 11))
    conn.execute("INSERT INTO social_counts(key, kind, ts, n_last_hour, n_24h, query) VALUES('M', 'mint', ?, 13, 19, 'q')", (E0 + 20,))


def _decision_rows(conn):
    return [tuple(r) for r in conn.execute("SELECT as_of, kind, mint, term, features_json, verdict, reasons_json FROM decisions ORDER BY id")]


def test_shadow_mode_leaves_decisions_byte_for_byte_as_without_x(tmp_path, monkeypatch):
    off = x_settings(tmp_path / "off", monkeypatch, token="")
    sh = x_settings(tmp_path / "shadow", monkeypatch)
    assert off.x_mode == "off" and sh.x_mode == "shadow"
    c_off, c_sh = db.connect(off.db_path), db.connect(sh.db_path)
    _passing_world(c_off)
    _passing_world(c_sh)
    _x_world(c_sh)
    assert features.x_features(c_sh, "M", E0 + 40)["x_mentions_1h"] == 1 and features.mega_post_feature(c_sh, E0 + 40) is not None
    s_off = Engine(c_off, off, rpc=None).tick(E0 + 40)
    s_sh = Engine(c_sh, sh, rpc=None).tick(E0 + 40)
    assert s_off["coin_decisions"] == s_sh["coin_decisions"] == 1 and s_sh["meta_decisions"] == s_off["meta_decisions"]
    assert _decision_rows(c_off) == _decision_rows(c_sh)  # nothing of X reached the decisions
    f = json.loads(c_sh.execute("SELECT features_json FROM decisions WHERE kind='observe'").fetchone()[0])
    assert f["mega_post"] is None and f["x_mentions_1h"] == 0 and f["x_count_1h"] is None
    assert db.kv_get(c_sh, "engine.x_mode") == "shadow" and db.kv_get(c_off, "engine.x_mode") == "off"
    # live (X_MODE=live plus the rules key): the same world now carries the mega post and the X counts
    live = x_settings(tmp_path / "live", monkeypatch, mode="live", x_live=True)
    c_live = db.connect(live.db_path)
    _passing_world(c_live)
    _x_world(c_live)
    Engine(c_live, live, rpc=None).tick(E0 + 40)
    f2 = json.loads(c_live.execute("SELECT features_json FROM decisions WHERE kind='observe'").fetchone()[0])
    assert f2["mega_post"]["account"] == "elonmusk" and f2["x_mentions_1h"] == 1 and f2["x_count_1h"] == 13
    assert _decision_rows(c_live) != _decision_rows(c_sh)
    for c in (c_off, c_sh, c_live):
        c.close()


# ----- the daemon and the CLI --------------------------------------------------------------------------------------------
def test_social_once_runs_in_shadow_with_a_token_and_idles_with_mode_off(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(nh.urllib.request, "urlopen", lambda req, timeout=None: (_ for _ in ()).throw(urllib.error.URLError("no network in tests")))
    s = x_settings(tmp_path, monkeypatch)
    assert main(["--root", str(tmp_path), "--quiet", "social", "--once"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["x_mode"] == "shadow" and out["mega"] == {"new_posts": 0} and out["fast_post_decisions"] == 0  # ran, got nothing (stubbed network)
    c = db.connect(s.db_path)
    assert db.kv_get(c, "social.x_mode") == "shadow" and c.execute("SELECT COUNT(*) FROM decisions").fetchone()[0] == 0
    c.close()
    x_settings(tmp_path, monkeypatch, mode="off")
    assert main(["--root", str(tmp_path), "--quiet", "social", "--once"]) == 0
    assert json.loads(capsys.readouterr().out) == {"disabled": "X_MODE=off", "x_mode": "off"}
    # status shows the mode, the note of a refused live and the shadow counters, never the token
    x_settings(tmp_path, monkeypatch, mode="live")
    assert main(["--root", str(tmp_path), "--quiet", "status"]) == 0
    out = capsys.readouterr().out
    assert "mode shadow (X_MODE live;" in out and "x mode note: X_MODE=live refused" in out and "PLACEHOLDER" not in out
    assert "x shadow: decisions total=0 today=0; confirmations today=0" in out and "daemons report social=off engine=not started" in out
    assert main(["--root", str(tmp_path), "--quiet", "--json", "status"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["x_mode"]["effective"] == "shadow" and data["x_mode"]["requested"] == "live" and data["x_mode"]["live_allowed"] is False
    assert data["x_shadow"]["decisions_total"] == 0 and data["settings"]["x_mode"] == "shadow"


# ----- the reports --------------------------------------------------------------------------------------------------------
def test_day_and_week_report_carry_the_x_shadow_section(settings, conn, monkeypatch):
    from fart.measure import week as wk
    from tests.test_m5_week_report import _grad_with_outcome

    day = "2026-10-27"
    t0 = day_bounds(day)[0] + 10 * 3600
    now = t0 + 200000
    _grad_with_outcome(conn, "WINNER1", t0 + 600, 1, 250000.0, 300)       # a winner, graduated 10 min after the shadow decision
    _grad_with_outcome(conn, "DENSE_LOSER", t0 + 700, 0, 40000.0, 300)    # measured, covered: no winner
    _grad_with_outcome(conn, "SPARSE_LOSER", t0 + 800, 0, 40000.0, 1200)  # measured on sparse samples: unknown
    _grad_with_outcome(conn, "LATE_GRAD", t0 + 90000, 0, 40000.0, 300)    # graduated after the 24 h window: not a winner for that decision
    db.kv_set(conn, "ingest.pumpportal.first_started", str(t0 - 86400), t0)

    def shadow(as_of, mints, term):
        conn.execute("INSERT INTO shadow_decisions(as_of, created_at, kind, mint, term, features_json, rules_version, verdict, reasons_json, would_kind)"
                     " VALUES(?,?,'shadow',NULL,?,?,'r1','observe','[]','fast_post')", (as_of, as_of, term, json.dumps({"mints": mints})))

    shadow(t0, ["WINNER1", "DENSE_LOSER"], "x:1")
    shadow(t0 + 10, ["DENSE_LOSER", "NEVER_GRADUATED"], "x:2")
    shadow(t0 + 20, ["SPARSE_LOSER"], "x:3")
    shadow(t0 + 30, ["LATE_GRAD"], "x:4")
    shadow(now - 3600, ["WINNER1"], "x:5")  # pending: its 24 h are not over (same UTC day as `now`, so a different report day)
    cur = conn.execute("INSERT INTO decisions(as_of, created_at, kind, mint, features_json, rules_version, verdict, reasons_json) VALUES(?,?,'observe','WINNER1','{}','r1','observe','[]')", (t0 - 300, t0 - 300))
    d1 = cur.lastrowid
    cur = conn.execute("INSERT INTO decisions(as_of, created_at, kind, term, features_json, rules_version, verdict, reasons_json) VALUES(?,?,'observe_meta','goldberg','{}','r1','observe','[]')", (t0 + 100, t0 + 100))
    d2 = cur.lastrowid
    for ts, did, kind, key, n1h, n24 in ((t0 + 50, d1, "observe", "WINNER1", 12, 30), (t0 + 950, d1, "observe", "WINNER1", 3, 30),
                                         (t0 + 200, d2, "observe_meta", "meta:goldberg", 2, 5)):
        conn.execute("INSERT INTO shadow_confirmations(ts, decision_id, decision_kind, decision_as_of, key, kind, n_last_hour, n_24h, query, threshold,"
                     " confirmed_1h, confirmed_24h, rules_version) VALUES(?,?,?,?,?,?,?,?,'q',10,?,?,'r1')",
                     (ts, did, kind, t0, key, "mint" if kind == "observe" else "meta", n1h, n24, 1 if n1h >= 10 else 0, 1 if n24 >= 10 else 0))
    rep = build_report(conn, settings, day, now)
    xs = rep["x_shadow"]
    assert xs["active"] and xs["x_mode_now"] == "off" and xs["decisions"] == {"total": 4, "by_would_kind": {"fast_post": 4}}
    assert xs["confirmations"] == {"counts_taken": 3, "keys": 2, "decisions_with_counts": 2, "confirmed_1h": 1, "confirmed_24h": 1, "threshold": 10, "observations_of_day": 2}
    assert [(i["term"], i["outcome"]) for i in xs["items"]] == [("x:1", "winner"), ("x:2", "not_winner"), ("x:3", "unknown"), ("x:4", "not_winner")]
    assert xs["outcomes"] == {"elapsed": 4, "winner": 1, "not_winner": 2, "unknown": 1, "pending": 0, "rate": 0.25, "rate_known": pytest.approx(1 / 3)}
    # the pending one belongs to the day of `now`
    rep2 = build_report(conn, settings, day_key(now), now)
    assert rep2["x_shadow"]["outcomes"]["pending"] == 1 and rep2["x_shadow"]["outcomes"]["elapsed"] == 0
    # the real tables are untouched by the report and the shadow rows never became decisions or outcomes
    assert conn.execute("SELECT COUNT(*) FROM decisions").fetchone()[0] == 2 and conn.execute("SELECT COUNT(*) FROM outcomes WHERE subject='decision'").fetchone()[0] == 0
    md = render_markdown(rep)
    assert "## X-schaduw" in md and "Beslissingen die X zou hebben toegevoegd: **4** (fast_post 4)" in md
    assert "bevestigd binnen 1 u (telling ≥ 10): **1**, binnen 24 u: **1**" in md
    assert "verstreken 4 (winnaar 1, geen winnaar 2, onbekend 1), nog bezig 0; trefkans 25%, over bekende uitkomsten 33%" in md
    # a day without shadow rows says so
    rep0 = build_report(conn, settings, "2026-10-20", now)
    assert rep0["x_shadow"]["active"] is False and "Geen schaduwgegevens" in render_markdown(rep0)
    # the week report sums the days
    wrep = wk.build_week_report(conn, settings, day_key(t0 + 86400), 2, now)
    assert wrep["x_shadow"]["decisions"] == 4 and wrep["x_shadow"]["confirmed_1h"] == 1 and wrep["x_shadow"]["winner"] == 1 and wrep["x_shadow"]["active_days"] == 1
    wmd = wk.render_markdown(wrep)
    assert "## X-schaduw" in wmd and f"| {day} | 4 | 3 | 1 | 1 | 2 | 4 | 1 | 2 | 1 | 0 |" in wmd
    assert "dezelfde noemerregels als de echte trefkans" in wmd
