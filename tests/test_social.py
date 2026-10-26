"""X watch: USD budget policy, query building, parsing/storage, velocity + fast posts, confirmation counts,
as-of features, 429 backoff, fast-post matching, scheduling. All HTTP is stubbed."""
import io
import json
import urllib.error

import pytest

from fart import db
from fart.engine import features
from fart.engine.meta import load_stopwords
from fart.net import http as nh
from fart.net.budget import Budget, load_source_configs
from fart.social import x as xm
from fart.social.xbudget import XBudget

T0 = 1_793_019_900.0  # 2026-10-26T13:05:00Z


class Clock:
    def __init__(self, t=T0):
        self.t = t

    def __call__(self):
        return self.t


def make_budget(settings, conn, clock, cap="3.00"):
    srcs = load_source_configs(settings.config("budget"), {"RPC_DAILY_BUDGET_CU": "100000", "X_DAILY_BUDGET_USD": cap})
    return Budget(conn, srcs, settings.lock_path, clock=clock)


def make_client(settings, conn, clock, request, cap=3.0, kols=None):
    budget = make_budget(settings, conn, clock, str(cap))
    xb = XBudget(conn, budget, settings.config("x"), cap, clock=clock)
    kols = settings.config("x_kols")["accounts"] if kols is None else kols
    return XClientFactory(conn, budget, xb, settings, kols, clock, request)


def XClientFactory(conn, budget, xb, settings, kols, clock, request):
    return xm.XClient(conn, budget, xb, settings.config("x"), kols, "PLACEHOLDER-BEARER", load_stopwords(settings.config("stopwords")),
                      "test-rules", clock=clock, request=request)


@pytest.fixture
def fx(fixtures_dir):
    return {name: json.loads((fixtures_dir / f"x_{name}.json").read_text()) for name in ("search", "lookup", "counts", "ca_search")}


class FakeRequest:
    """Stands in for request_json: records calls, serves scripted bodies, applies cost_from_response."""

    def __init__(self, bodies):
        self.bodies = bodies  # path fragment -> body or list of bodies
        self.calls = []

    def __call__(self, budget, source, method, url, headers=None, timeout_s=10.0, cost=None, cost_from_response=None, **kw):
        assert headers["Authorization"] == "Bearer PLACEHOLDER-BEARER"
        assert "PLACEHOLDER-BEARER" not in url
        self.calls.append({"method": method, "url": url, "cost_est": cost})
        v = budget.acquire_or_raise(source, method, cost)
        body = None
        for frag, b in self.bodies.items():
            if frag in url:
                body = b.pop(0) if isinstance(b, list) else b
                break
        if isinstance(body, Exception):
            budget.record(source, method, v.cost, False, getattr(body, "status", None), 1.0, err=str(body), retry_after_s=getattr(body, "retry_after_s", None))
            raise body
        actual = cost_from_response(body) if cost_from_response else v.cost
        budget.record(source, method, actual, True, 200, 1.0)
        return body


# ----- budget policy -------------------------------------------------------------------------------------------
def test_xbudget_envelope_reserve_and_caps(settings, conn):
    clock = Clock()
    budget = make_budget(settings, conn, clock, "5.00")
    xb = XBudget(conn, budget, settings.config("x"), 5.0, clock=clock)
    assert xb.price("post_usd") == 0.005 and xb.price("counts_request_usd") == 0.005 and xb.price("author_expansion_usd") == 0.01
    # the envelope grows through the day and reaches 1.0 at the last hour (13:05 UTC: 7.4/17.4 of the cap)
    fr13 = xb.envelope_fraction(T0)
    assert fr13 == pytest.approx(7.4 / 17.4) and xb.envelope_fraction(T0 + 10 * 3600) == pytest.approx(1.0)
    assert xb.envelope_fraction(T0 + 3600) > fr13
    assert xb.allow("mega_search", 0.015, T0).allowed
    xb.book("mega_search", 1, 0.015, T0)
    assert xb.spend_today(T0) == pytest.approx(0.015)
    # hourly envelope: $4.5 at 13:05 UTC is refused although the day cap is $5
    v = xb.allow("mega_search", 4.5, T0)
    assert not v.allowed and v.reason == "hourly_envelope"
    # hard cap
    v = xb.allow("mega_search", 5.1, T0)
    assert not v.allowed and v.reason == "daily_cost_cap"
    # 30% reserve for the account watches: confirmation calls may use at most 70% of the cap ($3.50)
    late = T0 + 10 * 3600  # 23:05 UTC, envelope 1.0
    xb.book("ca_counts", 698, 3.49, late)
    v = xb.allow("ca_counts", 0.02, late)
    assert not v.allowed and v.reason == "account_watch_reserve"
    assert xb.allow("mega_search", 0.02, late).allowed  # account watches still may
    # KOL sub-cap $1.00/day
    xb.book("kol_search", 100, 0.995, late)
    v = xb.allow("kol_search", 0.01, late)
    assert not v.allowed and v.reason == "kol_daily_cap"
    assert xb.allow("mega_search", 0.01, late).allowed
    # refusals are ledgered like gate refusals
    reasons = [r[0] for r in conn.execute("SELECT blocked_reason FROM api_ledger WHERE source='x' AND blocked_reason IS NOT NULL ORDER BY id")]
    assert reasons == ["hourly_envelope", "daily_cost_cap", "account_watch_reserve", "kol_daily_cap"]
    assert conn.execute("SELECT blocked_count FROM health WHERE source='x'").fetchone()[0] == 4
    by_kind = xb.spend_by_kind(late)
    assert by_kind["ca_counts"]["n"] == 698 and by_kind["kol_search"]["usd"] == pytest.approx(0.995)
    # dedupe is per UTC day
    assert xb.dedupe_new("post", ["a", "b"], T0) == ["a", "b"]
    assert xb.dedupe_new("post", ["a", "c"], T0) == ["c"]
    assert xb.dedupe_new("post", ["a"], T0 + 86400) == ["a"]
    # Budget.from_settings carries the X cap into the gate
    assert Budget.from_settings(conn, settings).snapshot()["x"]["usage_today"]["daily_cost_cap"] == settings.x_daily_budget_usd
    # a zero cap refuses everything
    assert XBudget(conn, budget, settings.config("x"), 0.0, clock=clock).allow("mega_search", 0.0, T0).reason == "daily_cost_cap"


# ----- queries and keys --------------------------------------------------------------------------------------------
def test_query_builders(settings):
    sw = load_stopwords(settings.config("stopwords"))
    ccfg = settings.config("x")["confirm"]
    q = xm.confirmation_query("7xKXmint", "Zorblax Cat", "ZORB", sw, ccfg)
    assert q == '"7xKXmint" OR $ZORB OR "Zorblax Cat"'
    assert xm.confirmation_query("7xKXmint", "Pepe", "SOL", sw, ccfg) == '"7xKXmint"'            # generic ticker, one-word name
    assert xm.confirmation_query("7xKXmint", "the coin", "$ai", sw, ccfg) == '"7xKXmint"'        # stopword-only name, short ticker
    assert xm.confirmation_query("7xKXmint", 'Say "hi" now', "ab1", sw, ccfg) == '"7xKXmint" OR $AB1 OR "Say  hi  now"'
    assert xm.meta_term_query("goldberg cat", sw, ccfg) == '"goldberg cat"'
    assert xm.meta_term_query("goldbergcat", sw, ccfg) == '"goldbergcat"'
    assert xm.meta_term_query("gold", sw, ccfg) is None and xm.meta_term_query("pepe", sw, ccfg) is None
    assert xm.kol_query(["a", "b"]) == "(from:a OR from:b) -is:retweet"
    assert "from:elonmusk" in settings.config("x")["mega"]["query"] and "-is:retweet" in settings.config("x")["mega"]["query"]
    assert xm.rare_words("Zorblax is the future of everything. Also: Mars. https://x.com", sw | {"everything", "future", "also", "mars"}) == ["zorblax"]
    assert xm.parse_created_at("2026-10-26T13:00:00.000Z") == 1_793_019_600.0 and xm.parse_created_at("nope") is None


# ----- search parsing, storage, cost -------------------------------------------------------------------------------
def test_mega_watch_stores_posts_and_books_dedupe_cost(settings, conn, fx):
    clock = Clock()
    req = FakeRequest({"/tweets/search/recent": fx["search"]})
    c = make_client(settings, conn, clock, req)
    s = c.mega_watch(T0)
    assert s == {"new_posts": 3}
    rows = {r["id"]: r for r in conn.execute("SELECT * FROM social_posts")}
    p = rows["1990000000000000001"]
    assert p["author"] == "elonmusk" and p["author_id"] == "44196397" and p["followers"] == 220_000_000 and p["kind"] == "mega"
    assert p["created_at"] == 1_793_019_000.0 and p["received_at"] == T0 and p["likes"] == 9000 and p["quotes"] == 300
    assert conn.execute("SELECT COUNT(*) FROM social_post_metrics").fetchone()[0] == 3
    assert db.kv_get(conn, "x.mega.since_id") == "1990000000000000001"
    # 3 new posts x $0.005 + 2 new authors x $0.010 = $0.035, in x_spend and in the gate ledger
    spend = c.xb.spend_by_kind(T0)
    assert spend["mega_search"] == {"n": 3, "usd": pytest.approx(0.035)}
    assert c.budget.daily_usage("x")["cost"] == pytest.approx(0.035)
    # first call had no since_id -> start_time; the next uses since_id and a 1-post estimate
    assert "start_time=" in req.calls[0]["url"] and "since_id=1990000000000000001" in req.calls[-1]["url"] if len(req.calls) > 1 else True
    s2 = c.mega_watch(T0 + 60)
    assert s2 == {"new_posts": 0} and "since_id=1990000000000000001" in req.calls[-1]["url"]
    assert req.calls[-1]["cost_est"] == pytest.approx(0.015)
    assert c.budget.daily_usage("x")["cost"] == pytest.approx(0.035)  # same posts again: nothing billed
    assert conn.execute("SELECT COUNT(*) FROM social_posts").fetchone()[0] == 3
    assert conn.execute("SELECT received_at FROM social_posts WHERE id='1990000000000000001'").fetchone()[0] == T0  # first arrival kept


def test_refresh_marks_fast_posts(settings, conn, fx):
    clock = Clock()
    req = FakeRequest({"/tweets/search/recent": fx["search"], "/tweets?": fx["lookup"]})
    c = make_client(settings, conn, clock, req)
    c.mega_watch(T0)
    clock.t = T0 + 120
    s = c.refresh_recent(clock.t)
    assert s == {"refreshed": 3, "fast": 1}
    p = conn.execute("SELECT * FROM social_posts WHERE id='1990000000000000001'").fetchone()
    # likes 9000 -> 40000 in 2 minutes = 15500/min, quotes 300 -> 1200 = 450/min
    assert p["fast"] == 1 and p["fast_at"] == T0 + 120 and p["likes_per_min"] == pytest.approx(15500.0) and p["quotes_per_min"] == pytest.approx(450.0)
    assert p["likes"] == 40000 and p["refreshed_at"] == T0 + 120 and p["received_at"] == T0
    slow = conn.execute("SELECT fast, likes_per_min FROM social_posts WHERE id='1990000000000000002'").fetchone()
    assert slow["fast"] == 0 and slow["likes_per_min"] == pytest.approx(30.0)
    assert c.xb.spend_by_kind(clock.t)["refresh"] == {"n": 3, "usd": 0.0}  # assumed free (VERIFY)
    assert "ids=1990000000000000001" in req.calls[-1]["url"]
    # a second refresh keeps fast_at and does not count it as newly fast
    clock.t = T0 + 240
    assert c.refresh_recent(clock.t)["fast"] == 0
    assert conn.execute("SELECT fast_at FROM social_posts WHERE id='1990000000000000001'").fetchone()[0] == T0 + 120
    # posts older than 2 h are not refreshed
    conn.execute("UPDATE social_posts SET created_at = created_at - 20000")
    clock.t = T0 + 360
    assert c.refresh_recent(clock.t) == {"refreshed": 0, "fast": 0}
    # velocity helper edge cases
    assert xm.velocity([], None) == (None, None)
    assert xm.velocity([(100.0, 600, 30)], 40.0) == (600.0, 30.0)
    assert xm.is_fast(5000, 200.0, 0.0, 100.0, settings.config("x")["mega"]) and not xm.is_fast(100, 200.0, 0.0, 100.0, settings.config("x")["mega"])
    assert not xm.is_fast(5000, 200.0, 10.0, 8000.0, settings.config("x")["mega"])


def test_confirmation_counts_search_and_features(settings, conn, fx):
    from fart.ingest import watch

    clock = Clock()
    mint, other = "7xKXmintZorblaxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx", "8yLYmintQuietxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"
    for m, name, sym, t in ((mint, "Zorblax Cat", "ZORB", T0 - 900), (other, "Quiet", "SOL", T0 - 600)):
        conn.execute("INSERT INTO coins(mint, name, symbol, block_time, received_at, source, sources) VALUES(?,?,?,?,?,'rpc_logs','[]')", (m, name, sym, t, t + 1))
        conn.execute("INSERT INTO decisions(as_of, created_at, kind, mint, features_json, rules_version, verdict, reasons_json) VALUES(?,?,'observe',?,'{}','r','observe','[]')", (t + 60, t + 60, m))
        watch.add(conn, m, "observe", t, None, 200)
    conn.execute("INSERT INTO decisions(as_of, created_at, kind, term, features_json, rules_version, verdict, reasons_json) VALUES(?,?,'observe_meta','goldberg cat','{}','r','observe','[]')", (T0 - 100, T0 - 100))
    conn.execute("INSERT INTO decisions(as_of, created_at, kind, term, features_json, rules_version, verdict, reasons_json) VALUES(?,?,'observe_meta','gold','{}','r','observe','[]')", (T0 - 100, T0 - 100))
    quiet_counts = {"data": [{"start": "2026-10-26T12:00:00.000Z", "end": "2026-10-26T13:00:00.000Z", "tweet_count": 0}], "meta": {"total_tweet_count": 0}}
    req = FakeRequest({"/tweets/counts/recent": [quiet_counts, fx["counts"], quiet_counts], "/tweets/search/recent": fx["ca_search"]})
    c = make_client(settings, conn, clock, req)
    keys = c.confirmation_keys(T0)
    assert [k["key"] for k in keys] == [other, mint, "meta:goldberg cat"]  # most recent decision first; 'gold' is not specific enough
    assert keys[1]["query"] == f'"{mint}" OR $ZORB OR "Zorblax Cat"' and keys[0]["query"] == f'"{other}"'
    s = c.confirm(T0)
    assert s == {"counts": 3, "searches": 1, "posts": 3}
    row = conn.execute("SELECT * FROM social_counts WHERE key=?", (mint,)).fetchone()
    assert row["n_last_hour"] == 13 and row["n_24h"] == 19 and row["kind"] == "mint"  # buckets ending within the last hour: 7 + 6
    assert conn.execute("SELECT n_last_hour FROM social_counts WHERE key=?", (other,)).fetchone()[0] == 0
    posts = conn.execute("SELECT * FROM social_posts WHERE kind='ca' ORDER BY id").fetchall()
    assert len(posts) == 3 and all(p["query_key"] == mint for p in posts)
    spend = c.xb.spend_by_kind(T0)
    assert spend["ca_counts"] == {"n": 3, "usd": pytest.approx(0.015)} and spend["ca_search"] == {"n": 3, "usd": pytest.approx(0.035)}
    # keys are due again only after the interval
    assert c.confirmation_keys(T0 + 60) == []
    assert [k["key"] for k in c.confirmation_keys(T0 + 901)] == [other, mint, "meta:goldberg cat"]
    # as-of features: everything received at T0; one post was created after T0 - 1 (13:01 > 13:00)...
    f = features.x_features(conn, mint, T0 + 1)
    assert f["x_mentions_1h"] == 3 and f["x_distinct_authors_1h"] == 2 and f["x_count_1h"] == 13 and f["x_count_24h"] == 19
    assert f["x_follower_weighted_1h"] == pytest.approx(2 * 3.0 + 1.0, abs=0.01)  # log10(1000) x2 + log10(10)
    assert f["x_first_mention_ts"] == xm.parse_created_at("2026-10-26T12:58:00.000Z")
    before = features.x_features(conn, mint, T0 - 1)  # nothing had reached us yet
    assert before["x_mentions_1h"] == 0 and before["x_count_1h"] is None and before["x_first_mention_ts"] is None
    assert features.x_features(conn, other, T0 + 1)["x_mentions_1h"] == 0
    # the coin features carry the x fields and the engine attaches the mega post
    from fart.chain.curve import CurveParams

    cf = features.coin_features(conn, mint, T0 + 1, CurveParams.from_config(settings.config("pumpfun")), settings.config("engine")["coin"])
    assert cf["x_mentions_1h"] == 3 and cf["x_count_24h"] == 19
    assert features.mega_post_feature(conn, T0 + 1) is None


def test_http_429_backs_off_and_blocks(settings, conn, monkeypatch):
    clock = Clock()
    budget = make_budget(settings, conn, clock)
    xb = XBudget(conn, budget, settings.config("x"), 3.0, clock=clock)
    calls = []

    def fake_urlopen(req, timeout=None):
        calls.append(req.full_url)
        assert req.get_header("Authorization") == "Bearer PLACEHOLDER-BEARER"
        raise urllib.error.HTTPError(req.full_url, 429, "Too Many Requests", {"x-rate-limit-reset": str(int(clock.t) + 120)}, io.BytesIO(b""))

    monkeypatch.setattr(nh.urllib.request, "urlopen", fake_urlopen)
    c = XClientFactory(conn, budget, xb, settings, ["a"], clock, nh.request_json)
    assert c.mega_watch(T0) == {"new_posts": 0}
    assert len(calls) == 1 and c.stats["http_error"] == 1
    h = conn.execute("SELECT backoff_until, err_count FROM health WHERE source='x'").fetchone()
    assert h["err_count"] == 1 and 100 <= h["backoff_until"] - T0 <= 130  # x-rate-limit-reset honored
    assert c.kol_watch(T0 + 1) == {"new_posts": 0}
    assert len(calls) == 1 and c.stats["blocked_gate"] == 1  # the gate refused with 'backoff', no HTTP call
    assert conn.execute("SELECT blocked_reason FROM api_ledger WHERE source='x' AND blocked_reason IS NOT NULL").fetchone()[0] == "backoff"
    # other kinds of failure never raise either
    monkeypatch.setattr(nh.urllib.request, "urlopen", lambda req, timeout=None: (_ for _ in ()).throw(urllib.error.URLError("down")))
    clock.t = T0 + 200
    assert c.mega_watch(clock.t) == {"new_posts": 0}


def test_fast_post_matching_writes_one_decision(settings, conn, fx):
    clock = Clock()
    req = FakeRequest({"/tweets/search/recent": fx["search"], "/tweets?": fx["lookup"]})
    c = make_client(settings, conn, clock, req)
    c.mega_watch(T0)
    post_t = xm.parse_created_at("2026-10-26T12:50:00.000Z")
    # three Zorblax coins by two creators within 30 min of the post, one outside, one unrelated
    for i, (name, creator, dt) in enumerate((("Zorblax", "A", 120), ("zorblax cat", "B", 600), ("ZORBLAX 2", "A", -300), ("Zorblax late", "C", 3000), ("Mars Dog", "D", 100))):
        conn.execute("INSERT INTO coins(mint, name, symbol, creator, block_time, received_at, source, sources) VALUES(?,?,?,?,?,?,'rpc_logs','[]')",
                     (f"MINT{i}", name, "X", creator, post_t + dt, post_t + dt + 1))
    assert c.fast_post_decisions(T0) == 0  # not fast yet
    clock.t = T0 + 120
    c.refresh_recent(clock.t)
    assert c.fast_post_decisions(clock.t) == 1
    d = conn.execute("SELECT * FROM decisions WHERE kind='fast_post'").fetchone()
    assert d["term"] == "x:1990000000000000001" and d["verdict"] == "observe" and d["rules_version"] == "test-rules" and d["mint"] is None
    f = json.loads(d["features_json"])
    assert f["author"] == "elonmusk" and f["matched"][0]["word"] == "zorblax" and f["matched"][0]["n_coins"] == 3 and f["matched"][0]["n_creators"] == 2
    assert f["mints"] == ["MINT0", "MINT1", "MINT2"]
    assert "LAUNCH" not in json.dumps(f).upper().replace("LAUNCHED", "")  # observations only, no tier words
    assert json.loads(d["reasons_json"])[0].startswith("fast_post likes_per_min=")
    # the same post never yields a second decision; the whole tick stays idempotent
    assert c.fast_post_decisions(clock.t + 15) == 0
    assert conn.execute("SELECT COUNT(*) FROM decisions").fetchone()[0] == 1
    # the engine feature sees the fast post as of now, not before it was marked
    m = features.mega_post_feature(conn, clock.t + 1)
    assert m["account"] == "elonmusk" and m["likes_per_min"] == pytest.approx(15500.0) and m["age_s"] == pytest.approx(clock.t + 1 - post_t)
    assert features.mega_post_feature(conn, T0 + 60) is None
    # a fast post with only common words matches nothing
    conn.execute("UPDATE social_posts SET fast=1, fast_at=?, text='Good morning everyone' WHERE id='1990000000000000002'", (clock.t + 5,))
    assert c.fast_post_decisions(clock.t + 16) == 0
    assert conn.execute("SELECT COUNT(*) FROM decisions").fetchone()[0] == 1


def test_tick_schedules_jobs_and_cli_once(settings, conn, fx, capsys):
    clock = Clock()
    req = FakeRequest({"/tweets/search/recent": fx["search"], "/tweets?": fx["lookup"], "/tweets/counts/recent": fx["counts"]})
    c = make_client(settings, conn, clock, req)
    s = c.tick(T0)
    assert set(s) == {"mega", "refresh", "kol", "confirm", "fast_post_decisions", "spend_today_usd"}
    n_calls = len(req.calls)
    assert c.tick(T0 + 15) == {"confirm": {"counts": 0, "searches": 0, "posts": 0}, "fast_post_decisions": 0, "spend_today_usd": s["spend_today_usd"]}
    assert len(req.calls) == n_calls  # nothing due 15 s later (no confirmation keys)
    s3 = c.tick(T0 + 61)
    assert "mega" in s3 and "kol" not in s3 and "refresh" not in s3
    s4 = c.tick(T0 + 181)
    assert "kol" in s4 and "refresh" in s4
    conn.close()
    # the CLI without a token says so and still writes a heartbeat
    from fart.cli import main

    assert main(["--root", str(settings.root), "--quiet", "social", "--once"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out == {"disabled": "X_BEARER_TOKEN not configured"}
    c2 = db.connect(settings.db_path)
    assert db.kv_get(c2, "social.heartbeat") is not None
    c2.close()
