"""Telegram outbox: sender with a stubbed HTTP layer (success, 429 retry_after, 5xx ladder, 4xx gave up),
dedupe, rate limit, Dutch formatting, builders (observe batches, fast posts, digest, health) and the CLI."""
import io
import json
import urllib.error

import pytest

from fart import db
from fart.config import load_settings
from fart.net import http as nh
from fart.net.budget import Budget, load_source_configs
from fart.notify import format as fmt
from fart.notify import outbox
from fart.notify.service import Notifier
from fart.notify.telegram import TelegramSender

T0 = 1_793_019_900.0  # 2026-10-26T13:05:00Z (15:05 in Amsterdam, CEST until 25 Oct 2026 03:00 -> CET: 14:05)
FOOTER = "Observatie, geen advies. Alles wordt achteraf gemeten."


class Clock:
    def __init__(self, t=T0):
        self.t = t

    def __call__(self):
        return self.t


def tg_settings(tmp_path, monkeypatch, enabled="1"):
    for k in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_OWNER_CHAT_ID", "TELEGRAM_NOTIFY_ENABLED", "X_BEARER_TOKEN", "RPC_HTTP_URL", "RPC_WSS_URL"):
        monkeypatch.delenv(k, raising=False)
    (tmp_path / ".env").write_text(f"TELEGRAM_BOT_TOKEN=123:PLACEHOLDER-TG\nTELEGRAM_OWNER_CHAT_ID=4242\nTELEGRAM_NOTIFY_ENABLED={enabled}\n")
    monkeypatch.setenv("FART_RULES_VERSION", "test-rules-v1")
    s = load_settings(root=tmp_path)
    s.ensure_dirs()
    return s


class FakeResp(io.BytesIO):
    def __init__(self, body, status=200):
        super().__init__(body)
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()


def install_fake_http(monkeypatch, script):
    """script: list of ('ok', message_id) | ('http', status, headers, body) | ('net',)"""
    calls = []

    def fake_urlopen(req, timeout=None):
        calls.append({"url": req.full_url, "body": json.loads(req.data.decode()), "timeout": timeout})
        step = script.pop(0)
        if step[0] == "ok":
            return FakeResp(json.dumps({"ok": True, "result": {"message_id": step[1]}}).encode())
        if step[0] == "http":
            raise urllib.error.HTTPError(req.full_url, step[1], "err", step[2], io.BytesIO(step[3].encode()))
        raise urllib.error.URLError("network down")

    monkeypatch.setattr(nh.urllib.request, "urlopen", fake_urlopen)
    return calls


def make_budget(settings, conn, clock):
    return Budget(conn, load_source_configs(settings.config("budget"), {}), settings.lock_path, clock=clock)


# ----- formatting -----------------------------------------------------------------------------------------------
def test_formatting_dutch_times_numbers_footer():
    assert fmt.fmt_time(T0) == "26 okt 14:05 (13:05 UTC)"  # CET after the late-October switch
    assert fmt.fmt_time(1_785_000_000.0) == "25 jul 19:20 (17:20 UTC)"  # CEST in summer
    assert fmt.fmt_time(None) == "onbekend"
    assert fmt.fmt_day("2026-10-25") == "25 okt 2026"
    assert fmt.n(12345) == "12.345" and fmt.n(12.5, 1) == "12,5" and fmt.n(None) == "onbekend" and fmt.n(1234.5, 2) == "1.234,50"
    assert fmt.age(45) == "45 s" and fmt.age(600) == "10 min" and fmt.age(7200) == "2,0 u" and fmt.age(None) == "onbekend"
    assert fmt.tri(True) == "ja" and fmt.tri(False) == "nee" and fmt.tri(None) == "onbekend"
    assert fmt.esc("<b>&x") == "&lt;b&gt;&amp;x"
    dec = {"id": 7, "kind": "observe", "mint": "MintAAA", "as_of": T0, "features": {
        "name": "Zorblax <Cat>", "symbol": "ZORB", "age_s": 240, "mcap_sol": 312.5, "unique_buyers_5m": 31, "net_sol_5m": 14.25,
        "bundled": False, "creator_prior_coins_24h": 2, "x_mentions_1h": 4, "x_distinct_authors_1h": 3}}
    text = fmt.observe_batch([dec], 200.0, FOOTER)
    assert text.startswith("<b>Observaties</b> (1)") and text.endswith(f"<i>{FOOTER}</i>")
    assert "Zorblax &lt;Cat&gt;" in text and "($ZORB)" in text and "<code>MintAAA</code>" in text
    assert "marktkapitalisatie 312,5 SOL (≈ $62.500)" in text and "unieke kopers 5 min: 31 · netto SOL 5 min: 14,25" in text
    assert "gebundeld: nee · eerdere munten maker (24 u): 2" in text and "X: 4 vermeldingen, 3 accounts" in text
    assert "data: beslissing #7 · 26 okt 14:05 (13:05 UTC)" in text and "leeftijd 4 min" in text
    for word in ("LAUNCH", "koop", "BUY", "advies"):
        assert word not in text.replace("geen advies", "")
    meta = {"id": 8, "kind": "observe_meta", "term": "goldberg cat", "as_of": T0, "features": {"n_coins": 5, "distinct_creators": 4, "sol_inflow": 44.2, "unique_buyers": 30, "burst_ratio": None, "burst_ratio_is_inf": True}}
    many = fmt.observe_batch([dec] * 9 + [meta], None, FOOTER, max_items=8)
    assert "… en 2 meer" in many and "(USD onbekend)" in many
    assert "thema “goldberg cat”" in fmt.observe_batch([meta], None, FOOTER) and "burst ∞× de basislijn" in fmt.observe_batch([meta], None, FOOTER)
    fp = {"id": 9, "as_of": T0, "features": {"author": "elonmusk", "created_at": T0 - 900, "likes_per_min": 1550.4, "likes": 40000, "quotes_per_min": 45,
          "text": "Zorblax <3", "matched": [{"word": "zorblax", "n_coins": 3, "n_creators": 2}], "mints": [f"M{i}" for i in range(7)], "post_id": "199"}}
    t = fmt.fast_post(fp, FOOTER)
    assert "<b>Snelle post</b> van @elonmusk" in t and "likes/min 1.550" in t and "Zorblax &lt;3" in t and "… en 2 meer" in t and "post 199" in t
    h = fmt.health("stream_silent", {"source": "rpc_logs", "age": "6 min", "last": "x"}, T0, FOOTER)
    assert "stroom <b>rpc_logs</b> is al 6 min stil" in h and h.endswith(f"<i>{FOOTER}</i>")
    rep = {"complete": False, "universe": {"graduations": 40, "measured": 38, "pending": 2, "missing": 0, "winners": 3, "winners_organic": 1, "winners_bundled": 2, "winners_bundled_unknown": 0},
           "recall": {"winners": 3, "decision_before_first_trade": 1, "decision_before_graduation": 2}, "precision": {"precision": 0.25, "hits": 1, "known": 4, "unknown": 1, "pending": 0, "precision_organic": None},
           "coverage": {"hours_complete": 23, "creates_union": 9000, "creates_pp": 3000, "creates_rpc": 8900, "gap_seconds_by_source": {"pumpportal": 300}},
           "rules_version_now": "abc", "method_version": "m1.0"}
    d = fmt.digest("2026-10-25", rep, 1.2345, 3.0, 1_900_000, 3_000_000, FOOTER)
    assert d.startswith("<b>Dagrapport 25 okt 2026</b> · niet compleet") and "winnaars: 3 · organisch 1 · gebundeld 2" in d
    assert "geobserveerd vóór eerste trade: 1/3 · vóór graduatie: 2/3" in d and "precisie observaties: 25%" in d and "organisch onbekend" in d
    assert "dekking: 23/24 complete uren" in d and "pumpportal 300 s" in d and "X: $1,23 van $3,00 · RPC: 1.900.000 CU van 3.000.000" in d
    long = outbox.MAX_TEXT + 100
    assert len(fmt.observe_batch([dict(dec, features=dict(dec["features"], name="x" * long))], None, FOOTER)) > outbox.MAX_TEXT


# ----- outbox + sender ------------------------------------------------------------------------------------------
def test_outbox_dedupe_and_sender_paths(tmp_path, monkeypatch):
    settings = tg_settings(tmp_path, monkeypatch)
    conn = db.connect(settings.db_path)
    clock = Clock()
    budget = make_budget(settings, conn, clock)
    assert outbox.enqueue(conn, "test", "k1", "4242", "hallo", T0) == 1
    assert outbox.enqueue(conn, "test", "k1", "4242", "hallo again", T0 + 1) is None  # same dedupe_key: never twice
    assert outbox.enqueue(conn, "test", "k2", "4242", "x" * 5000, T0 + 2) == 2
    assert len(conn.execute("SELECT text FROM outbox WHERE id=2").fetchone()[0]) == outbox.MAX_TEXT
    assert outbox.counts(conn) == {"pending": 2}
    script = [
        ("ok", 501),                                                                              # row 1 sent
        ("http", 429, {"Retry-After": "7"}, json.dumps({"ok": False, "error_code": 429, "description": "Too Many Requests: retry after 7", "parameters": {"retry_after": 7}})),
        ("http", 503, {}, "bad gateway"),                                                         # 5xx -> ladder
        ("net",),                                                                                  # network -> ladder
        ("http", 400, {}, json.dumps({"ok": False, "description": "Bad Request: can't parse entities"})),  # 4xx -> gave up
    ]
    calls = install_fake_http(monkeypatch, script)
    sender = TelegramSender(budget, "123:PLACEHOLDER-TG")
    notifier = Notifier(conn, settings, budget, sender, clock=clock)
    assert notifier.enabled
    res = notifier.send_due(T0 + 2)
    assert res == {"sent": 1, "retry": 1, "gave_up": 0, "blocked": 0}
    assert calls[0]["url"] == "https://api.telegram.org/bot123:PLACEHOLDER-TG/sendMessage"
    assert calls[0]["body"] == {"chat_id": "4242", "text": "hallo", "parse_mode": "HTML", "disable_web_page_preview": True}
    r1, r2 = conn.execute("SELECT * FROM outbox ORDER BY id").fetchall()
    assert r1["status"] == "sent" and r1["message_id"] == 501 and r1["sent_at"] == T0 + 2 and r1["attempts"] == 1
    assert r2["status"] == "pending" and r2["attempts"] == 1 and r2["next_try_at"] == T0 + 9 and r2["error"].startswith("429")  # retry_after honored
    # the budget saw the 429 too: backoff until at least retry_after
    assert conn.execute("SELECT backoff_until FROM health WHERE source='telegram'").fetchone()[0] >= T0 + 9
    assert notifier.send_due(T0 + 3) == {"sent": 0, "retry": 0, "gave_up": 0, "blocked": 0}  # not due yet
    clock.t = T0 + 20
    assert notifier.send_due(clock.t) == {"sent": 0, "retry": 1, "gave_up": 0, "blocked": 0}  # 503 -> second step of the ladder (60 s)
    r2 = conn.execute("SELECT * FROM outbox WHERE id=2").fetchone()
    assert r2["attempts"] == 2 and r2["next_try_at"] == clock.t + 60 and "503" in r2["error"]
    clock.t = T0 + 20 + 60 + 30  # also past the budget backoff of a 5xx (2 s base)
    assert notifier.send_due(clock.t) == {"sent": 0, "retry": 1, "gave_up": 0, "blocked": 0}  # network error -> 300 s
    r2 = conn.execute("SELECT * FROM outbox WHERE id=2").fetchone()
    assert r2["attempts"] == 3 and r2["next_try_at"] == clock.t + 300
    clock.t += 400
    assert notifier.send_due(clock.t) == {"sent": 0, "retry": 0, "gave_up": 1, "blocked": 0}  # 400: retrying cannot help
    r2 = conn.execute("SELECT * FROM outbox WHERE id=2").fetchone()
    assert r2["status"] == "gave_up" and "parse entities" in r2["error"] and not script
    assert "PLACEHOLDER-TG" not in json.dumps(outbox.last_rows(conn))
    conn.close()


def test_retry_ladder_ends_in_gave_up_and_rate_limit(tmp_path, monkeypatch):
    settings = tg_settings(tmp_path, monkeypatch)
    conn = db.connect(settings.db_path)
    clock = Clock()
    budget = make_budget(settings, conn, clock)
    outbox.enqueue(conn, "health", "h1", "4242", "down", T0)
    script = [("http", 500, {}, "x")] * 6
    install_fake_http(monkeypatch, script)
    notifier = Notifier(conn, settings, budget, TelegramSender(budget, "t"), clock=clock)
    waits = []
    for _ in range(6):
        row = conn.execute("SELECT * FROM outbox WHERE id=1").fetchone()
        if row["status"] != "pending":
            break
        clock.t = max(clock.t, float(row["next_try_at"])) + 3600  # also clears the budget backoff
        notifier.send_due(clock.t)
        row2 = conn.execute("SELECT * FROM outbox WHERE id=1").fetchone()
        if row2["status"] == "pending":
            waits.append(round(row2["next_try_at"] - clock.t))
    assert waits == [15, 60, 300, 900, 3600]
    assert conn.execute("SELECT status, attempts FROM outbox WHERE id=1").fetchone()[:] == ("gave_up", 6)
    # the gate is in backoff after six server errors: a round sends nothing and keeps the rows pending
    outbox.enqueue(conn, "observe", "blocked-probe", "4242", "p", clock.t)
    assert notifier.send_due(clock.t) == {"sent": 0, "retry": 0, "gave_up": 0, "blocked": 1}
    assert conn.execute("SELECT status, attempts FROM outbox WHERE dedupe_key='blocked-probe'").fetchone()[:] == ("pending", 0)
    clock.t += 3600
    install_fake_http(monkeypatch, [("ok", 999)])
    assert notifier.send_due(clock.t)["sent"] == 1
    # rate limit: 20 per minute, the rest waits for the next minute
    clock.t += 120
    for i in range(25):
        outbox.enqueue(conn, "observe", f"o{i}", "4242", f"m{i}", clock.t)
    install_fake_http(monkeypatch, [("ok", 1000 + i) for i in range(25)])
    assert notifier.send_due(clock.t)["sent"] == 20
    clock.t += 30
    assert notifier.send_due(clock.t)["sent"] == 0
    clock.t += 31
    assert notifier.send_due(clock.t)["sent"] == 5
    assert outbox.counts(conn) == {"gave_up": 1, "sent": 26}
    conn.close()


# ----- builders --------------------------------------------------------------------------------------------------
def add_decision(conn, kind, as_of, mint=None, term=None, features=None):
    cur = conn.execute("INSERT INTO decisions(as_of, created_at, kind, mint, term, features_json, rules_version, verdict, reasons_json)"
                       " VALUES(?,?,?,?,?,?,'r1','observe','[]')", (as_of, as_of, kind, mint, term, json.dumps(features or {})))
    return cur.lastrowid


def test_builders_observe_fast_digest_health(tmp_path, monkeypatch):
    settings = tg_settings(tmp_path, monkeypatch)
    conn = db.connect(settings.db_path)
    clock = Clock()
    budget = make_budget(settings, conn, clock)
    notifier = Notifier(conn, settings, budget, TelegramSender(budget, "t"), clock=clock)
    # history before the first run is never dumped
    add_decision(conn, "observe", T0 - 7200, mint="OLD", features={"name": "old"})
    add_decision(conn, "fast_post", T0 - 7200, term="x:1", features={"author": "a"})
    # (the service starts after 06:10 UTC: yesterday's digest goes out once, built from a fresh report)
    assert notifier.build(T0) == {"observe": 0, "fast_post": 0, "digest": 1, "health": 0}
    first_digest = conn.execute("SELECT * FROM outbox WHERE kind='digest'").fetchone()
    assert first_digest["dedupe_key"] == "digest:2026-10-25" and "Dagrapport 25 okt 2026" in first_digest["text"]
    assert notifier.build(T0 + 1) == {"observe": 0, "fast_post": 0, "digest": 0, "health": 0}
    # new decisions -> one batch after the interval, fast posts immediately
    f = {"name": "Zorblax", "symbol": "ZORB", "age_s": 100, "mcap_sol": 50, "unique_buyers_5m": 30, "net_sol_5m": 12, "bundled": None, "creator_prior_coins_24h": 0}
    d1 = add_decision(conn, "observe", T0 + 10, mint="M1", features=f)
    d2 = add_decision(conn, "observe_meta", T0 + 20, term="goldberg cat", features={"n_coins": 3, "distinct_creators": 3, "sol_inflow": 20, "unique_buyers": 25, "burst_ratio": 4.0, "burst_ratio_is_inf": False})
    d3 = add_decision(conn, "fast_post", T0 + 30, term="x:2", features={"author": "elonmusk", "likes_per_min": 200, "likes": 5000, "matched": [], "mints": [], "post_id": "2", "text": "hi"})
    assert notifier.build(T0 + 60) == {"observe": 0, "fast_post": 1, "digest": 0, "health": 0}  # batch interval not over
    assert notifier.build(T0 + 301) == {"observe": 1, "fast_post": 0, "digest": 0, "health": 0}
    rows = {r["kind"]: r for r in conn.execute("SELECT * FROM outbox")}
    assert rows["fast_post"]["dedupe_key"] == f"fast_post:{d3}" and "@elonmusk" in rows["fast_post"]["text"]
    assert rows["observe"]["dedupe_key"] == f"observe:{d1}-{d2}" and "Zorblax" in rows["observe"]["text"] and "goldberg cat" in rows["observe"]["text"]
    assert "gebundeld: onbekend" in rows["observe"]["text"] and rows["observe"]["text"].endswith(f"<i>{FOOTER}</i>")
    # nothing new -> nothing; max 6 batches per hour -> the 7th is deferred and rides the next batch
    assert notifier.build(T0 + 700)["observe"] == 0
    for i in range(6):
        add_decision(conn, "observe", T0 + 800 + i, mint=f"B{i}", features=f)
        notifier.build(T0 + 800 + 400 * (i + 1))
    n_batches = conn.execute("SELECT COUNT(*) FROM outbox WHERE kind='observe'").fetchone()[0]
    assert n_batches == 6
    assert notifier.stats["observe_deferred"] == 1  # the sixth coin met the hourly maximum and waited
    add_decision(conn, "observe", T0 + 4000, mint="B9", features=f)
    assert notifier.build(T0 + 4100)["observe"] == 1  # the first batch fell out of the hour: deferred + new go out together
    last = conn.execute("SELECT text FROM outbox WHERE kind='observe' ORDER BY id DESC LIMIT 1").fetchone()[0]
    assert "B5" in last and "B9" in last and "(2)" in last
    # digest: not before 06:10 UTC, once per day, from the report JSON when present
    (settings.out_dir / "recall").mkdir(parents=True, exist_ok=True)
    rep = {"complete": True, "universe": {"graduations": 12, "winners": 1}, "recall": {"winners": 1}, "precision": {}, "coverage": {"hours_complete": 24},
           "rules_version_now": "r1", "method_version": "m1.0"}
    (settings.out_dir / "recall" / "2026-10-26.json").write_text(json.dumps(rep))
    conn.execute("INSERT INTO x_spend(day, kind, n, usd, ts) VALUES('2026-10-26', 'mega_search', 10, 0.42, ?)", (T0,))
    t_early = 1_793_084_400.0  # 2026-10-27T07:00Z is after 06:10 ... use 05:00Z first
    t_0500 = 1_793_077_200.0   # 2026-10-27T05:00:00Z
    assert notifier.build_digest(t_0500) == 0
    t_0611 = t_0500 + 71 * 60
    assert notifier.build_digest(t_0611) == 1
    dg = conn.execute("SELECT * FROM outbox WHERE kind='digest' ORDER BY id DESC LIMIT 1").fetchone()
    assert dg["dedupe_key"] == "digest:2026-10-26" and "Dagrapport 26 okt 2026" in dg["text"] and "graduaties: 12" in dg["text"] and "24/24 complete uren" in dg["text"]
    assert "X: niet geconfigureerd" in dg["text"]  # X not configured in this test
    assert "RPC: 0 CU" in dg["text"]
    assert notifier.build_digest(t_0611 + 600) == 0  # once per day
    # health: silent stream (ingest alive), budget 80% and cap, PumpPortal refusal; 1 h cooldown
    now = t_0611 + 1000
    for k, v in (("ingest.heartbeat", now - 10), ("ingest.pumpportal.started", now - 9000), ("ingest.pumpportal.last_msg_at", now - 400),
                 ("ingest.rpc_logs.started", now - 9000), ("ingest.rpc_logs.last_msg_at", now - 30),
                 ("ingest.pumpportal.last_http_status", 429), ("ingest.pumpportal.last_http_status_at", now - 120)):
        db.kv_set(conn, k, str(v), now)
    assert notifier.build_health(now) == 2
    texts = [r[0] for r in conn.execute("SELECT text FROM outbox WHERE kind='health' ORDER BY id")]
    assert "stroom <b>pumpportal</b> is al 6 min stil" in texts[0] and "HTTP 429" in texts[1]
    db.kv_set(conn, "ingest.heartbeat", str(now + 590), now)
    db.kv_set(conn, "ingest.rpc_logs.last_msg_at", str(now + 580), now)
    assert notifier.build_health(now + 600) == 0  # cooldown
    db.kv_set(conn, "ingest.heartbeat", str(now + 3690), now)
    db.kv_set(conn, "ingest.rpc_logs.last_msg_at", str(now + 3680), now)
    assert notifier.build_health(now + 3700) == 1  # still silent -> again after an hour; the refusal is older than an hour now
    # a dead ingest daemon is systemd's business: no silent-stream alert without a fresh heartbeat
    assert notifier.build_health(now + 7400) == 0
    # X budget at 80% (cap 3.00 -> 2.40) and at the cap
    xb = Budget(conn, load_source_configs(settings.config("budget"), {"X_DAILY_BUDGET_USD": "3.0"}), settings.lock_path, clock=Clock(now + 8000))
    notifier.budget = xb
    xb.record("x", "mega_search", 2.5, True, 200, 1.0)
    assert notifier.build_health(now + 8000) == 1
    kinds = [r[0] for r in conn.execute("SELECT dedupe_key FROM outbox WHERE kind='health' ORDER BY id DESC LIMIT 3")]
    assert kinds[0].startswith("health:budget_80:x") and "83%" in conn.execute("SELECT text FROM outbox ORDER BY id DESC LIMIT 1").fetchone()[0]
    xb.record("x", "mega_search", 0.6, True, 200, 1.0)
    assert notifier.build_health(now + 8100) == 1
    assert conn.execute("SELECT dedupe_key FROM outbox ORDER BY id DESC LIMIT 1").fetchone()[0].startswith("health:budget_cap:x")
    conn.close()


def test_disabled_builds_nothing_and_cli(tmp_path, monkeypatch, capsys):
    from fart.cli import main

    settings = tg_settings(tmp_path, monkeypatch, enabled="0")
    assert settings.telegram_configured and not settings.telegram_enabled
    conn = db.connect(settings.db_path)
    add_decision(conn, "fast_post", T0, term="x:9", features={"author": "a"})
    notifier = Notifier(conn, settings, make_budget(settings, conn, Clock()), None, clock=Clock())
    s = notifier.tick(T0)
    assert s == {"enabled": False} and conn.execute("SELECT COUNT(*) FROM outbox").fetchone()[0] == 0
    assert db.kv_get(conn, "notify.heartbeat") == str(T0)
    conn.close()
    root = str(settings.root)
    assert main(["--root", root, "--quiet", "notify", "test"]) == 2
    assert "disabled" in capsys.readouterr().out
    assert main(["--root", root, "--quiet", "notify", "outbox"]) == 0
    assert "outbox: empty" in capsys.readouterr().out
    assert main(["--root", root, "--quiet", "notify", "--once"]) == 0
    assert json.loads(capsys.readouterr().out) == {"enabled": False}
    # enabled: the test message goes through the outbox and the stubbed API
    settings2 = tg_settings(tmp_path, monkeypatch, enabled="1")
    install_fake_http(monkeypatch, [("ok", 77)])
    assert main(["--root", root, "--quiet", "notify", "test"]) == 0
    out = capsys.readouterr().out
    assert "status=sent" in out and "message_id=77" in out and "PLACEHOLDER" not in out
    assert main(["--root", root, "--quiet", "--json", "notify", "outbox"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["counts"] == {"sent": 1} and data["rows"][0]["kind"] == "test" and "Testbericht" in data["rows"][0]["text"]
    assert main(["--root", root, "--quiet", "notify", "outbox"]) == 0
    assert "test      sent" in capsys.readouterr().out
    assert settings2.telegram_enabled


def test_outbox_on_empty_database(settings, capsys):
    from fart.cli import main

    assert main(["--root", str(settings.root), "--quiet", "notify", "outbox"]) == 0
    assert "no database yet" in capsys.readouterr().out
