"""Milestone 8: the website (`fart web`).

A fixture world (a database with covered and NOT covered hours, decisions with every outcome status, planner rows,
daily and week reports under out/recall/) is served by the real `ThreadingHTTPServer` on an ephemeral loopback port;
the tests speak HTTP to it. The clock of the application is a list the tests move (session expiry, the lock)."""
from __future__ import annotations

import http.client
import json
import logging
import re
import sqlite3
import threading
import urllib.parse
from pathlib import Path

import pytest

from fart import db
from fart.cli import main
from fart.config import load_settings
from fart.ingest import coverage
from fart.log import _JsonFormatter
from fart.measure.report import build_report, render_markdown
from fart.measure.week import build_week_report, render_markdown as render_week
from fart.util import atomic_write_json, atomic_write_text, day_key, hour_bounds, hour_key
from fart.web import auth, data, markdown, pages, server

ROOT = Path(__file__).resolve().parent.parent
PASSWORD = "wachtwoord-van-jim-123"
NOW = 1_793_030_000.0  # 2026-10-26T15:53:20Z
MV = "m1.0"


# ----- the world -------------------------------------------------------------------------------------------------------
def build_world(root: Path, now: float, password: str = PASSWORD, bind: str = "127.0.0.1:0", public: bool = False) -> dict:
    """`public=False` is the milestone-8 behaviour (FART_WEB_PUBLIC=0: everything behind the login); milestone 9's tests
    build the same world with `public=True`."""
    root.mkdir(parents=True, exist_ok=True)
    secret = auth.new_secret()
    pw_hash = auth.hash_password(password)
    (root / ".env").write_text(f"FART_WEB_PASSWORD_HASH={pw_hash}\nFART_WEB_SECRET={secret}\nFART_WEB_BIND={bind}\nFART_WEB_PUBLIC={'1' if public else '0'}\n")
    settings = load_settings(root=root, env={"FART_RULES_VERSION": "test-rules-v1"})
    settings.ensure_dirs()
    conn = db.connect(settings.db_path)
    first = now - 20 * 3600  # the sources first ran 20 hours ago: the hours before are NIET GEDEKT
    for src in ("pumpportal", "rpc_logs"):
        db.kv_set(conn, f"ingest.{src}.first_started", str(first), first)
        g = coverage.gap_open(conn, src, first, "startup")
        coverage.gap_close(conn, g, first + 2)
    s, _ = hour_bounds(hour_key(now - 5 * 3600))  # a disconnect over a whole hour: NIET GEDEKT in the middle of the window
    for src in ("pumpportal", "rpc_logs"):
        gid = coverage.gap_open(conn, src, s - 60, "disconnect")
        coverage.gap_close(conn, gid, s + 3660)
    for k, t in (("ingest.heartbeat", now - 10), ("engine.heartbeat", now - 20), ("social.heartbeat", now - 3600),
                 ("measure.track.last_run", now - 100), ("measure.report.last_run", now - 90000), ("ingest.pumpportal.last_msg_at", now - 30)):
        db.kv_set(conn, k, str(t), t)
    db.kv_set(conn, "engine.x_mode", "off", now)
    db.kv_set(conn, "social.x_mode", "off", now)
    db.kv_set(conn, "engine.tick_failed", "2", now)
    db.kv_set(conn, "x.planner.deferred.ca_counts." + day_key(now), "3", now)
    mints = []
    for i in range(10):
        t = first + 3600 + i * 5400
        mint = f"Mint{i:02d}" + "x" * 30
        name = "Pepe <script>alert(1)</script> coin" if i == 0 else f"Coin {i}"
        conn.execute("INSERT INTO coins(mint, name, symbol, received_at, block_time, source, sources) VALUES(?,?,?,?,?,?,?)",
                     (mint, name, f"C{i}", t, t - 1, "pumpportal", json.dumps(["pumpportal"])))
        mints.append((mint, t))
    for mint, t in mints[:4]:
        conn.execute("INSERT INTO migrations(mint, block_time, received_at, source, sources) VALUES(?,?,?,?,?)", (mint, t + 1800, t + 1801, "rpc_logs", "[]"))
    feats = {"unique_buyers_5m": 30, "net_sol_5m": 12.5, "largest_buyer_share_15m": 0.2, "bundled": False, "trades_total": 44, "x_mentions_1h": 0}
    hits = {0: 1, 1: 0, 2: None}  # winner, no winner, unknown; 3: pending (1 h old); 4: elapsed without an outcome row -> unknown
    dec_ids = {}
    for i, (mint, t) in enumerate(mints[:5]):
        as_of = (now - 3600) if i == 3 else (t + 600)
        cur = conn.execute("INSERT INTO decisions(as_of, created_at, kind, mint, term, features_json, rules_version, verdict, reasons_json) VALUES(?,?,?,?,?,?,?,?,?)",
                           (as_of, as_of + 1, "observe", mint, None, json.dumps({**feats, "mint": mint}), "test-rules-v1", "observe", json.dumps(["unique_buyers_5m=30>=25"])))
        dec_ids[i] = cur.lastrowid
        if i in hits:
            hit = hits[i]
            conn.execute("INSERT INTO outcomes(decision_id, subject, mint, anchor_time, horizon_s, measured_at, peak_mcap_usd, hit_100k, bundled, method_version)"
                         " VALUES(?,?,?,?,?,?,?,?,?,?)", (dec_ids[i], "decision", mint, as_of, 86400, as_of + 86400, 150000 if hit else (50000 if hit == 0 else None), hit, 0, MV))
    for mint, t in mints[:4]:
        conn.execute("INSERT INTO outcomes(decision_id, subject, mint, anchor_time, horizon_s, measured_at, peak_mcap_usd, hit_100k, bundled, method_version)"
                     " VALUES(?,?,?,?,?,?,?,?,?,?)", (None, "graduation", mint, t + 1800, 86400, t + 1800 + 86400, 120000, 1 if mint == mints[0][0] else 0, 0, MV))
    conn.execute("INSERT INTO decisions(as_of, created_at, kind, mint, term, features_json, rules_version, verdict, reasons_json) VALUES(?,?,?,?,?,?,?,?,?)",
                 (now - 7200, now - 7199, "observe_meta", None, "pepe coin", json.dumps({"n_coins": 5, "distinct_creators": 4, "burst_ratio": 4.2, "sol_inflow": 31.0, "unique_buyers": 40}),
                  "test-rules-v1", "observe", json.dumps(["burst_ratio=4.2"])))
    conn.execute("INSERT INTO x_spend(day, kind, n, usd, ts, unit) VALUES(?,?,?,?,?,?)", (day_key(now), "ca_counts", 6, 0.03, now - 600, "request"))
    conn.execute("INSERT INTO api_ledger(ts, source, method, cost, ok, status) VALUES(?,?,?,?,?,?)", (now - 500, "rpc_public", "getTransaction", 1234, 1, 200))
    conn.execute("INSERT INTO api_ledger(ts, source, method, cost, ok, status, blocked_reason) VALUES(?,?,?,?,?,?,?)", (now - 400, "x", "ca_counts", 0, 0, None, "daily_cost_cap"))
    conn.execute("INSERT INTO x_keys(key, decision_id, kind, decision_kind, decision_as_of, mint, query, started_at, next_due_at, counts_n, last_n_1h, peak_n_1h, usd)"
                 " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)", (mints[3][0], dec_ids[3], "mint", "observe", now - 3600, mints[3][0], mints[3][0], now - 3600, now + 600, 4, 2, 3, 0.02))
    conn.execute("INSERT INTO x_keys(key, decision_id, kind, decision_kind, decision_as_of, mint, query, started_at, counts_n, usd, stopped_at, stop_reason)"
                 " VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", (mints[2][0], dec_ids[2], "mint", "observe", now - 7200, mints[2][0], mints[2][0], now - 7200, 4, 0.02, now - 1800, "refuted"))
    # milestone 9, the live dashboard: four coins of the last hour around one word, watched coins with trades right now (one of them an
    # observation with a whale and two sells), the cached SOL price, the engine's burst sets, tracker samples for the two oldest graduations
    live = []
    for i, (name, sym, creator) in enumerate((("Quantum Frog", "QFROG", "CrA"), ("Frog Quantum Pepe", "FQP", "CrB"), ("QUANTUMFROG 2", "QF2", "CrC"), ("Quantum Frog Returns", "QFR", "CrD"))):
        t = now - 3000 + i * 600
        mint = f"Live{i:02d}" + "y" * 30
        conn.execute("INSERT INTO coins(mint, name, symbol, creator, received_at, block_time, dev_buy_sol, initial_mcap_sol, source, sources) VALUES(?,?,?,?,?,?,?,?,?,?)",
                     (mint, name, sym, creator, t + 1, t, 0.5 + i, 31.0 + i, "pumpportal", json.dumps(["pumpportal"])))
        live.append((mint, t))
    vsol, vtok = 30_000_000_000, 1_073_000_000_000_000
    for j, (mint, t) in enumerate(live[:3]):
        conn.execute("INSERT INTO watchlist(mint, since, reason, until, active, last_trade_at, activated_at) VALUES(?,?,?,?,1,?,?)",
                     (mint, t, "observe" if j == 0 else "probation", now + 3600, now - 30, t))
        for k in range((12, 5, 1)[j]):
            tt = now - 280 + k * 20
            sol = 2.0 if j == 0 else 0.4
            vsol += int(sol * 1e9)
            vtok -= int(sol * 1e9 * 1000)
            conn.execute("INSERT INTO trades(mint, block_time, received_at, signature, buyer, is_buy, sol, tokens, virtual_sol, virtual_token, source) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                         (mint, tt, tt + 1, f"sig-{mint}-{k}", f"Buyer{k % 8}" if j == 0 else f"B{j}-{k}", 1, sol, sol * 1e6, vsol, vtok, "rpc_trades"))
    whale_mint = live[0][0]
    conn.execute("INSERT INTO trades(mint, block_time, received_at, signature, buyer, is_buy, sol, tokens, virtual_sol, virtual_token, source) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                 (whale_mint, now - 100, now - 99, f"sig-{whale_mint}-sell1", "Buyer0", 0, 1.5, 1.5e6, vsol - int(1.5e9), vtok + int(1.5e12), "rpc_trades"))
    conn.execute("INSERT INTO trades(mint, block_time, received_at, signature, buyer, is_buy, sol, tokens, virtual_sol, virtual_token, source) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                 (whale_mint, now - 60, now - 59, f"sig-{whale_mint}-whale", "Whale", 1, 6.0, 6e6, vsol + int(4.5e9), vtok - int(4.5e12), "rpc_trades"))
    conn.execute("INSERT INTO decisions(as_of, created_at, kind, mint, term, features_json, rules_version, verdict, reasons_json) VALUES(?,?,?,?,?,?,?,?,?)",
                 (now - 240, now - 239, "observe", whale_mint, None, json.dumps({**feats, "mint": whale_mint, "name": "Quantum Frog", "symbol": "QFROG"}), "test-rules-v1", "observe", json.dumps(["unique_buyers_5m=30>=25"])))
    conn.execute("INSERT INTO watchlist(mint, since, reason, until, active, last_trade_at, activated_at) VALUES(?,?,?,?,1,?,?)", (mints[4][0], now - 7200, "early_buyers", now + 3600, None, now - 7200))
    db.kv_set(conn, "sol_usd.cache", json.dumps({"ts": now - 120, "price": 180.0, "src": "dexscreener"}), now - 120)
    db.kv_set(conn, "engine.meta_burst_sets", json.dumps({"quantum": {"mints": [live[0][0], live[1][0]], "chosen_at": now - 900, "last_bursting": now - 60}}), now - 60)
    ts = mints[0][1] + 1800
    while ts <= mints[0][1] + 1800 + 86400:   # the winner: a sample every five minutes
        conn.execute("INSERT INTO mcap_samples(mint, ts, src, mcap_usd, volume_h24_usd, sol_usd) VALUES(?,?,?,?,?,?)", (mints[0][0], ts, "dexscreener", 110000.0, 45000.0, 180.0))
        ts += 300
    conn.execute("INSERT INTO mcap_samples(mint, ts, src, mcap_usd, volume_h24_usd, sol_usd) VALUES(?,?,?,?,?,?)", (mints[1][0], now - 200, "dexscreener", 42000.0, 9000.0, 180.0))
    conn.commit()
    out = settings.out_dir / "recall"
    for days_back in (2, 1):  # the day before yesterday lies before the first start (NIET GEDEKT); yesterday is partly covered
        day = day_key(now - days_back * 86400)
        rep = build_report(conn, settings, day, now)
        atomic_write_json(out / f"{day}.json", rep)
        atomic_write_text(out / f"{day}.md", render_markdown(rep))
    end_day = day_key(now - 86400)
    wk = build_week_report(conn, settings, end_day, 3, now)
    atomic_write_json(out / f"week-{end_day}.json", wk)
    atomic_write_text(out / f"week-{end_day}.md", render_week(wk))
    conn.close()
    return {"settings": settings, "password": password, "hash": pw_hash, "secret": secret, "mints": mints, "decision_ids": dec_ids,
            "not_covered_day": day_key(now - 2 * 86400), "yesterday": end_day, "live_mints": live}


@pytest.fixture(scope="module")
def world(tmp_path_factory):
    """The world under a pinned rules_version (the cache of `fart.version` is cleared so the order of the modules does not matter)."""
    from fart import version

    mp = pytest.MonkeyPatch()
    mp.setenv("FART_RULES_VERSION", "test-rules-v1")
    for k in ("FART_WEB_PASSWORD_HASH", "FART_WEB_SECRET", "FART_WEB_BIND", "FART_WEB_PUBLIC"):
        mp.delenv(k, raising=False)
    version._CACHE.pop("v", None)
    yield build_world(tmp_path_factory.mktemp("web-root"), NOW)
    mp.undo()


@pytest.fixture(scope="module")
def live(world):
    """The application on a real server; `live['clock']` is the list the tests move."""
    clock = [NOW]
    app = server.App(world["settings"], clock=lambda: clock[0])
    srv = server.make_server(app, "127.0.0.1", 0)
    port = srv.server_address[1]
    t = threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.1}, daemon=True)
    t.start()
    yield {"app": app, "port": port, "clock": clock, "world": world}
    srv.shutdown()
    srv.server_close()


class Client:
    def __init__(self, port: int):
        self.port = port

    def req(self, method: str, path: str, headers: dict | None = None, body: bytes | str | None = None):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        if isinstance(body, str):
            body = body.encode("utf-8")
        c.request(method, path, body=body, headers=headers or {})
        r = c.getresponse()
        data_ = r.read()
        out = (r.status, r.getheaders(), data_)
        c.close()
        return out

    @staticmethod
    def header(headers, name: str):
        return [v for k, v in headers if k.lower() == name.lower()]

    def login_form(self):
        st, h, body = self.req("GET", "/login")
        assert st == 200
        cookie = [v for v in self.header(h, "set-cookie") if v.startswith(auth.CSRF_COOKIE + "=")][0].split(";")[0]
        token = re.search(r'name="csrf" value="([^"]+)"', body.decode()).group(1)
        assert cookie.split("=", 1)[1] == token
        return cookie, token

    def login(self, password: str, extra_headers: dict | None = None):
        cookie, token = self.login_form()
        form = urllib.parse.urlencode({"csrf": token, "password": password})
        return self.req("POST", "/login", {"Cookie": cookie, "Content-Type": "application/x-www-form-urlencoded", **(extra_headers or {})}, form)

    def session(self, password: str = PASSWORD) -> str:
        st, h, body = self.login(password)
        assert st == 303, (st, body[:200])
        return [v for v in self.header(h, "set-cookie") if v.startswith(auth.SESSION_COOKIE + "=")][0].split(";")[0]


@pytest.fixture
def client(live):
    live["app"].guard = auth.LoginGuard(live["app"].clock)  # every test starts without a lock
    live["clock"][0] = NOW
    return Client(live["port"])


# ----- start-up and the helper commands --------------------------------------------------------------------------------
def test_refuses_to_start_without_both_env_values(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("FART_RULES_VERSION", "test-rules-v1")
    for k in ("FART_WEB_PASSWORD_HASH", "FART_WEB_SECRET", "FART_WEB_BIND", "FART_WEB_PUBLIC"):
        monkeypatch.delenv(k, raising=False)
    assert main(["--root", str(tmp_path), "--quiet", "web"]) == 2
    err = capsys.readouterr().err
    assert "FART_WEB_PASSWORD_HASH is empty" in err and "FART_WEB_SECRET is empty" in err and "fart web hash-password" in err and "not started" in err
    (tmp_path / ".env").write_text("FART_WEB_PASSWORD_HASH=not-a-hash\nFART_WEB_SECRET=short\n")
    assert main(["--root", str(tmp_path), "--quiet", "web"]) == 2
    err = capsys.readouterr().err
    assert "is not a hash made by" in err and "too short" in err
    with pytest.raises(auth.WebConfigError):
        server.App(load_settings(root=tmp_path, env={}))
    (tmp_path / ".env").write_text(f"FART_WEB_PASSWORD_HASH={auth.hash_password('x' * 12)}\nFART_WEB_SECRET={auth.new_secret()}\nFART_WEB_BIND=nonsense\n")
    assert main(["--root", str(tmp_path), "--quiet", "web"]) == 2
    assert "FART_WEB_BIND must be host:port" in capsys.readouterr().err


def test_hash_password_and_new_secret(tmp_path, capsys, monkeypatch):
    import getpass

    answers = iter(["geheim-wachtwoord-1", "geheim-wachtwoord-1"])
    monkeypatch.setattr(getpass, "getpass", lambda prompt="": next(answers))
    assert main(["--root", str(tmp_path), "--quiet", "web", "hash-password"]) == 0
    out = capsys.readouterr()
    h = out.out.strip()
    assert h.startswith("scrypt:16384:8:1:") and len(h.split(":")) == 6 and "geheim" not in h and out.err == ""
    assert auth.verify_password("geheim-wachtwoord-1", h) and not auth.verify_password("geheim-wachtwoord-2", h)
    assert auth.hash_password("geheim-wachtwoord-1") != h  # a fresh salt every time
    answers = iter(["geheim-wachtwoord-1", "anders"])
    monkeypatch.setattr(getpass, "getpass", lambda prompt="": next(answers))
    assert main(["--root", str(tmp_path), "--quiet", "web", "hash-password"]) == 2
    assert capsys.readouterr().out == ""  # nothing printed on a mismatch
    answers = iter(["kort", "kort"])
    monkeypatch.setattr(getpass, "getpass", lambda prompt="": next(answers))
    assert main(["--root", str(tmp_path), "--quiet", "web", "hash-password"]) == 2
    assert "at least 12" in capsys.readouterr().err
    assert main(["--root", str(tmp_path), "--quiet", "web", "new-secret"]) == 0
    s = capsys.readouterr().out.strip()
    assert re.fullmatch(r"[0-9a-f]{64}", s) and auth.check_config(h, s) == []
    assert auth.parse_hash("scrypt:3:8:1:" + "00" * 16 + ":" + "00" * 32) is None  # n must be a power of two in bounds
    assert auth.parse_hash("md5:1:1:1:00:00") is None and not auth.verify_password("x", None)


def test_status_mentions_the_website(tmp_path, capsys):
    db.connect(tmp_path / "data" / "fart.db").close()
    assert main(["--root", str(tmp_path), "--quiet", "status"]) == 0
    assert "web: password hash not configured, secret not configured, bind 127.0.0.1:8792" in capsys.readouterr().out
    assert main(["--root", str(tmp_path), "--quiet", "--json", "status"]) == 0
    s = json.loads(capsys.readouterr().out)["settings"]
    assert s["web_password_hash"] == "not configured" and s["web_secret"] == "not configured" and s["web_bind"] == "127.0.0.1:8792"


# ----- login ----------------------------------------------------------------------------------------------------------------
def test_login_wrong_password_csrf_and_cookie_rules(client, live, caplog):
    caplog.set_level(logging.INFO)
    st, h, body = client.req("GET", "/")
    assert st == 401 and b"Inloggen" in body and b'name="password"' in body and "Observatie, geen advies" in body.decode()
    st, h, body = client.req("GET", "/api/status")
    assert st == 401 and json.loads(body) == {"error": "login required", "login": "/login"}
    # wrong password: 401 with the message, counted
    st, h, body = client.login("fout")
    assert st == 401 and "Onjuist wachtwoord" in body.decode() and "4 pogingen" in body.decode()
    # no CSRF token, a mismatching one, and a token signed under another secret: 403, never counted as a password failure
    cookie, token = client.login_form()
    form = urllib.parse.urlencode({"password": PASSWORD})
    st, h, body = client.req("POST", "/login", {"Cookie": cookie, "Content-Type": "application/x-www-form-urlencoded"}, form)
    assert st == 403 and "verlopen of ongeldig" in body.decode()
    other = auth.Signer(auth.new_secret()).csrf_token(NOW)
    form = urllib.parse.urlencode({"csrf": other, "password": PASSWORD})
    st, h, body = client.req("POST", "/login", {"Cookie": f"{auth.CSRF_COOKIE}={other}", "Content-Type": "application/x-www-form-urlencoded"}, form)
    assert st == 403
    form = urllib.parse.urlencode({"csrf": token, "password": PASSWORD})
    st, h, body = client.req("POST", "/login", {"Cookie": f"{auth.CSRF_COOKIE}={other}", "Content-Type": "application/x-www-form-urlencoded"}, form)
    assert st == 403
    assert live["app"].guard.remaining("127.0.0.1") == 4
    # the right password: a session cookie with the right attributes, the CSRF cookie cleared
    st, h, body = client.login(PASSWORD)
    assert st == 303 and client.header(h, "location") == ["/eigenaar"]
    cookies = client.header(h, "set-cookie")
    sess = [c for c in cookies if c.startswith(auth.SESSION_COOKIE + "=")][0]
    assert "HttpOnly" in sess and "Secure" in sess and "SameSite=Strict" in sess and "Max-Age=43200" in sess and "Path=/" in sess
    assert any(c.startswith(auth.CSRF_COOKIE + "=;") and "Max-Age=0" in c for c in cookies)
    token = sess.split(";")[0]
    st, h, body = client.req("GET", "/", {"Cookie": token})
    assert st == 200 and b"Dashboard" in body
    st, h, body = client.req("GET", "/login", {"Cookie": token})
    assert st == 303 and client.header(h, "location") == ["/eigenaar"]  # already logged in
    # a tampered cookie, one under another secret, an expired one
    bad = token[:-4] + ("0000" if not token.endswith("0000") else "1111")
    assert client.req("GET", "/", {"Cookie": bad})[0] == 401
    foreign = auth.Signer(auth.new_secret()).session_token(NOW)
    assert client.req("GET", "/api/status", {"Cookie": f"{auth.SESSION_COOKIE}={foreign}"})[0] == 401
    live["clock"][0] = NOW + auth.SESSION_TTL_S - 5
    assert client.req("GET", "/api/status", {"Cookie": token})[0] == 200
    live["clock"][0] = NOW + auth.SESSION_TTL_S + 1
    assert client.req("GET", "/", {"Cookie": token})[0] == 401
    live["clock"][0] = NOW
    # logout needs POST with the form token; afterwards the cookie is dead even though it has not expired
    assert client.req("GET", "/logout", {"Cookie": token})[0] == 405
    st, h, body = client.req("GET", "/", {"Cookie": token})
    lt = re.search(r'action="/logout"><input type="hidden" name="csrf" value="([^"]+)"', body.decode()).group(1)
    st, h, body = client.req("POST", "/logout", {"Cookie": token, "Content-Type": "application/x-www-form-urlencoded"}, urllib.parse.urlencode({"csrf": "wrong"}))
    assert st == 403
    st, h, body = client.req("POST", "/logout", {"Cookie": token, "Content-Type": "application/x-www-form-urlencoded"}, urllib.parse.urlencode({"csrf": lt}))
    assert st == 303 and client.header(h, "location") == ["/login"] and any("Max-Age=0" in c for c in client.header(h, "set-cookie"))
    assert client.req("GET", "/", {"Cookie": token})[0] == 401
    assert client.req("POST", "/logout")[0] == 401
    # a POST to a page is refused: the site is read-only for the owner too
    token2 = client.session()
    assert client.req("POST", "/observaties", {"Cookie": token2, "Content-Type": "application/x-www-form-urlencoded"}, "x=1")[0] == 405
    # the log never carries the password, the hash, the secret or a cookie value; the failure is there with the IP
    text = caplog.text + json.dumps([getattr(r, "fields", {}) for r in caplog.records], default=str)
    w = live["world"]
    for secret in (PASSWORD, w["hash"], w["secret"], token.split("=", 1)[1], token2.split("=", 1)[1]):
        assert secret not in text
    failed = [r for r in caplog.records if r.getMessage() == auth.LOG_EVENT_FAILED]
    assert failed and failed[0].fields["ip"] == "127.0.0.1"


def test_lockout_per_ip_and_per_account(client, live):
    for i in range(4):
        st, h, body = client.login("fout", {"X-Forwarded-For": "203.0.113.5"})
        assert st == 401
    st, h, body = client.login("fout", {"X-Forwarded-For": "203.0.113.5"})
    assert st == 429 and "Te veel mislukte pogingen" in body.decode() and client.header(h, "retry-after") == ["900"]
    # locked: the right password is refused without being tried, from the same IP and (account lock) from another
    st, h, body = client.login(PASSWORD, {"X-Forwarded-For": "203.0.113.5"})
    assert st == 429 and "Wacht 15 minuten" in body.decode()
    st, h, body = client.login(PASSWORD, {"X-Forwarded-For": "198.51.100.9"})
    assert st == 429
    live["clock"][0] = NOW + auth.LOCK_S - 30
    assert client.login(PASSWORD, {"X-Forwarded-For": "198.51.100.9"})[0] == 429
    live["clock"][0] = NOW + auth.LOCK_S + 1
    assert client.login(PASSWORD, {"X-Forwarded-For": "198.51.100.9"})[0] == 303
    live["clock"][0] = NOW
    # the window: four failures, ten minutes later a fifth does not lock
    g = auth.LoginGuard(lambda: 0.0)
    for i in range(4):
        assert g.record_failure("1.1.1.1", 100.0 + i) is None
    assert g.record_failure("1.1.1.1", 100.0 + auth.FAIL_WINDOW_S + 1) is None and g.wait_s("1.1.1.1", 800.0) is None
    assert g.remaining("1.1.1.1", 800.0) == 4


def test_guard_replays_failures_from_the_log(tmp_path):
    fmt = _JsonFormatter()
    lines = []
    for i, (ts, ip) in enumerate([(NOW - 2 * 3600, "9.9.9.9")] + [(NOW - 60 + i, "203.0.113.7") for i in range(5)]):
        rec = logging.makeLogRecord({"name": "web", "levelno": logging.WARNING, "levelname": "WARNING", "msg": auth.LOG_EVENT_FAILED, "created": ts,
                                     "fields": {"ip": ip, "remaining": 0}})
        lines.append(fmt.format(rec))
    lines.append("not json at all")
    lines.append(json.dumps({"ts": NOW - 10, "event": "web.request", "ip": "203.0.113.8"}))
    log_path = tmp_path / "web.log"
    log_path.write_text("\n".join(lines) + "\n")
    g = auth.LoginGuard(lambda: NOW)
    assert g.load_from_log(log_path, NOW) == 5  # the two-hour-old failure is outside the window that can still matter
    assert g.wait_s("203.0.113.7", NOW) is not None and g.wait_s("9.9.9.9", NOW) is not None  # the account lock holds for every IP
    assert auth.LoginGuard(lambda: NOW).load_from_log(tmp_path / "missing.log", NOW) == 0


# ----- read-only ------------------------------------------------------------------------------------------------------------
def test_database_is_opened_read_only(world, client, monkeypatch):
    conn = data.open_db(world["settings"].db_path)
    try:
        assert conn.execute("PRAGMA query_only").fetchone()[0] == 1
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("INSERT INTO kv(key, value, updated_at) VALUES('web.test', '1', 0)")
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("UPDATE kv SET value='x' WHERE key='ingest.heartbeat'")
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("DELETE FROM x_keys")
    finally:
        conn.close()
    with pytest.raises(FileNotFoundError):
        data.open_db(world["settings"].root / "nope.db")
    # serving every page and endpoint leaves the database file byte for byte as it was
    before = world["settings"].db_path.read_bytes()
    token = client.session()
    for path in ("/", "/observaties", "/rapporten", "/eigenaar", "/methode", "/tools", f"/rapporten/{world['yesterday']}", f"/rapporten/{world['yesterday']}.md",
                 "/api/status", "/api/coverage", "/api/decisions", "/api/x", "/api/report/latest", "/api/week/latest", "/api/public/status", "/api/public/week"):
        assert client.req("GET", path, {"Cookie": token})[0] == 200, path
    assert world["settings"].db_path.read_bytes() == before
    # the fallback: when the WAL files cannot be used (ReadOnlyPaths and no running writer) the file is read as immutable
    real = db.connect_ro

    def broken(path):
        raise sqlite3.OperationalError("unable to open database file")

    monkeypatch.setattr(db, "connect_ro", broken)
    conn = data.open_db(world["settings"].db_path)
    try:
        assert conn.execute("SELECT COUNT(*) FROM coins").fetchone()[0] == 14
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("INSERT INTO kv(key, value, updated_at) VALUES('web.test', '1', 0)")
    finally:
        conn.close()
    monkeypatch.setattr(db, "connect_ro", real)


# ----- the JSON endpoints -----------------------------------------------------------------------------------------------------
def _get_json(client, token, path):
    st, h, body = client.req("GET", path, {"Cookie": token})
    assert st == 200 and client.header(h, "content-type") == ["application/json; charset=utf-8"], (path, st)
    return json.loads(body)


def test_api_status_coverage_decisions_and_x(client, world):
    token = client.session()
    st = _get_json(client, token, "/api/status")
    assert st["db_exists"] and st["schema_version"] == db.SCHEMA_VERSION and st["rules_version"] == "test-rules-v1"
    assert st["generated_label"].endswith("UTC)") and st["banner"] == "Observatie, geen advies. Alles wordt achteraf gemeten."
    by = {r["key"]: r for r in st["daemons"]}
    assert by["ingest"]["status"] == "fresh" and by["ingest"]["age_s"] == 10 and by["engine"]["status"] == "fresh"
    assert by["social"]["status"] == "stale" and by["social"]["_class"] == "stale"
    assert by["notify"]["status"] == "nooit" and by["notify"]["last"] == "nooit" and by["notify"]["age"] == "–"
    assert by["report"]["status"] == "fresh" and by["week"]["status"] == "nooit" and by["backup"]["status"] == "nooit"
    # today (UTC): the coin decisions of 00:03, 03:03 and 14:53 plus the theme decision of 13:53
    assert st["decisions_today"] == {**st["decisions_today"], "observe": 4, "observe_meta": 1, "total": 5}
    assert st["budgets"]["rpc"]["cu_today"] == 1234 and st["budgets"]["rpc"]["cu_today_label"] == "1.234" and st["budgets"]["rpc"]["cap"] is None
    assert st["budgets"]["rpc"]["share_label"] == "–" and st["budgets"]["x"]["usd_today_label"] == "$0.03" and st["budgets"]["x"]["blocked_today"] == 1
    assert st["failures"] == {"engine.tick_failed": "2"} and st["open_gaps"] == []
    cv = _get_json(client, token, "/api/coverage")
    assert cv["hours"] == 24 and len(cv["rows"]) == 24
    nc = [r for r in cv["rows"] if not r["covered"]]
    assert len(nc) == 4 and cv["summary"]["hours_not_covered"] == 4  # three hours before the first start, one disconnect hour
    for r in nc:  # NIET GEDEKT stays empty: no zeros
        assert r["covered_label"] == "NIET GEDEKT" and r["complete_label"] == "–" and r["_class"] == "notcovered"
        assert r["creates_pp"] is None and r["creates_rpc"] is None and r["creates_union"] is None and r["grads"] is None and r["grads_without_create"] is None
    covered = [r for r in cv["rows"] if r["covered"]]
    assert all(isinstance(r["creates_union"], int) for r in covered) and sum(r["creates_union"] for r in covered) >= 8
    assert any(r["grads"] for r in covered) and all(r["hour_label"].endswith("UTC)") for r in cv["rows"])
    assert "disconnect" in nc[-1]["gaps_label"]
    dc = _get_json(client, token, "/api/decisions")
    assert dc["limit"] == 200 and len(dc["rows"]) == 7
    labels = [(r["kind_label"], r["outcome_label"]) for r in dc["rows"]]
    assert labels == [("munt", "nog bezig"), ("munt", "nog bezig"), ("thema", "geen eigen uitkomst (thema)"), ("munt", "nog bezig"), ("munt", "onbekend"), ("munt", "geen winnaar"), ("munt", "winnaar")]
    unknown = [r for r in dc["rows"] if r["outcome"] == "unknown"][0]
    assert unknown["peak_mcap_usd"] is None and unknown["peak_label"] == "–"  # unknown stays unknown, never 0
    winner = [r for r in dc["rows"] if r["outcome"] == "winner"][0]
    assert winner["peak_label"] == "150.000" and winner["name"] == "Pepe <script>alert(1)</script> coin ($C0)"
    assert "unique_buyers_5m=30" in winner["features_label"] and winner["features"]["bundled"] is False and winner["rules_version"] == "test-rules-v1"
    theme = [r for r in dc["rows"] if r["kind"] == "observe_meta"][0]
    assert theme["subject"] == "pepe coin" and "burst_ratio=4.2" in theme["features_label"] and theme["as_of_label"].endswith("UTC)")
    x = _get_json(client, token, "/api/x")
    assert x["mode"]["effective"] == "off" and x["mode"]["label"] == "uit" and x["mode"]["social_daemon"] == "off"
    assert x["budget"]["spend_label"] == "$0.03" and x["budget"]["blocked_today"] == 1
    assert x["planner"]["active_total"] == 1 and x["planner"]["stopped_label"] == "weerlegd 1 ($0.02)" and x["planner"]["deferred_label"] == "ca_counts 3"
    assert x["active_keys"][0]["key"] == world["mints"][3][0] and x["active_keys"][0]["counts_n"] == 4 and x["stopped_rows"][0]["reason_label"] == "weerlegd"
    assert x["plan_error"] is None and {p["kind"] for p in x["plan"]} == {"mega_search", "refresh", "kol_search", "ca_counts", "ca_search", "ca_authors"}
    assert x["spend_by_kind"] == [{"kind": "ca_counts", "n": 6, "usd": 0.03, "usd_label": "$0.03"}]
    st, h, body = client.req("GET", "/api/nope", {"Cookie": token})
    assert st == 404 and "endpoints" in json.loads(body)


def test_api_report_and_week_latest(client, world):
    token = client.session()
    rp = _get_json(client, token, "/api/report/latest")
    assert rp["found"] and rp["day"] == world["yesterday"] and rp["status"] == "incomplete" and rp["status_label"] == "NIET COMPLEET"
    keys = [i["key"] for i in rp["items"]]
    assert keys == ["universe", "winners", "hit_rate", "recall", "lead_time", "hours_not_covered"]
    items = {i["key"]: i for i in rp["items"]}
    assert items["universe"]["value"] == "2" and items["winners"]["value"] == "1" and items["hours_not_covered"]["value"] == "19"  # first start 19:53 UTC
    # the second graduation was measured "no winner" without tracker samples: the report calls that outcome unknown (milestone 5), never "geen winnaar"
    assert "gemeten 2" in items["universe"]["note"] and "geen winnaar 0, uitkomst onbekend 1" in items["winners"]["note"]
    assert items["hit_rate"]["value"].endswith("%") and items["lead_time"]["value"].endswith("min") and rp["href"] == f"/rapporten/{world['yesterday']}.md"
    assert rp["raw"]["hours_not_covered"] == 19 and rp["rules_version"] == "test-rules-v1" and rp["method_version"] == MV
    wk = _get_json(client, token, "/api/week/latest")
    assert wk["found"] and wk["end_day"] == world["yesterday"] and wk["days"] == 3 and "rollend venster" in wk["window_label"]
    seg = wk["segments"][0]
    assert seg["frozen"] is True and seg["rules_versions"] == ["test-rules-v1"] and seg["method_version"] == MV and seg["verdict"].startswith("NO-GO")
    assert "(bevroren meetweek)" in seg["title"] and wk["segment_title"] == seg["title"]
    statuses = {c["key"]: c["status"] for c in seg["criteria"]}
    assert statuses["complete_days"] == "niet voldaan" and statuses["hit_rate_ci_low"] == "te weinig data" and statuses["observations_per_day"] == "voldaan"
    assert {c["key"] for c in seg["criteria"]} == {"complete_days", "winners", "hit_rate_ci_low", "recall_before_graduation_ci_low", "median_lead_s_before_graduation", "observations_per_day"}
    tl = {c["key"]: c for c in seg["criteria"]}
    assert tl["hit_rate_ci_low"]["value_label"] == "–" and tl["hit_rate_ci_low"]["threshold_label"] == ">= 15%" and tl["median_lead_s_before_graduation"]["threshold_label"] == ">= 10 min"
    assert "NIET GEDEKT" in seg["days_label"] and world["not_covered_day"] in seg["not_covered_days"]


def test_report_latest_of_a_not_covered_day_stays_empty(world, tmp_path):
    """Only the NIET GEDEKT day exists: every key point is empty, nothing is zero."""
    root = tmp_path / "nc"
    (root / "out" / "recall").mkdir(parents=True)
    src = world["settings"].out_dir / "recall"
    for ext in ("json", "md"):
        (root / "out" / "recall" / f"{world['not_covered_day']}.{ext}").write_bytes((src / f"{world['not_covered_day']}.{ext}").read_bytes())
    settings = load_settings(root=root, env={})
    rp = data.report_latest_view(settings, NOW)
    assert rp["found"] and rp["status"] == "not_covered" and rp["status_label"] == "NIET GEDEKT" and rp["blank"] is True
    for i in rp["items"]:
        if i["key"] == "hours_not_covered":
            assert i["value"] == "24"
        else:
            assert i["value"] == "–" and i["note"] == "–", i
    assert rp["raw"] == {"graduations": None, "winners": None, "hit_rate": None, "recall_before_graduation": None, "median_lead_s_vs_graduation": None, "hours_not_covered": 24}
    assert "0%" not in json.dumps(rp["items"]) and "universum is onbekend" in rp["note"]
    assert data.week_latest_view(settings, NOW)["found"] is False
    empty = load_settings(root=tmp_path / "empty", env={})
    assert data.report_latest_view(empty, NOW)["found"] is False and data.report_files(empty) == []
    # no database at all: the views say so instead of showing zeros
    st = data.status_view(None, empty, NOW)
    assert st["db_exists"] is False and st["daemons"] == [] and st["decisions_today"]["observe"] is None
    assert data.coverage_view(None, NOW)["rows"] == [] and data.decisions_view(None, empty, NOW)["rows"] == []
    assert data.x_view(None, empty, NOW)["planner"] == {}


# ----- HTML -------------------------------------------------------------------------------------------------------------------
def test_html_dashboard_observations_reports_and_settings(client, world):
    """Private mode (FART_WEB_PUBLIC=0): the same pages as the public site, but every one of them behind the login."""
    token = client.session()
    st, h, body = client.req("GET", "/", {"Cookie": token})
    html = body.decode()
    assert st == 200 and client.header(h, "content-type") == ["text/html; charset=utf-8"]
    assert "Observatie, geen advies. Alles wordt achteraf gemeten." in html and '<html lang="nl">' in html
    for word in ("Waar het geld nu heen gaat", "Nieuwste munten", "Net gegradueerd", "Wat rondgaat op X", "Observaties vandaag", "Meting", "Let op", "Uitloggen",
                 "/static/app.js", "/static/app.css", 'data-live="hot"', "NIET GEDEKT", "te weinig data", "NO-GO", "nog bezig"):
        assert word in html, word
    st, h, body = client.req("GET", "/meting", {"Cookie": token})
    html = body.decode()
    assert st == 200
    for word in ("Meetweek", "Dekking en eerlijkheid", "Laatste rapporten", "Hoe dit werkt", 'data-live="meetweek"'):
        assert word in html, word
    assert re.search(r"\d{1,2} okt \d\d:\d\d \(\d\d:\d\d UTC\)", html)  # Europe/Amsterdam with UTC in brackets
    assert "<script>alert" not in html
    assert "onclick" not in html and "<style" not in html and 'style="' not in html  # nothing inline: the CSP forbids it
    for forbidden in ("winst", "rendement", "advies:", "koop", "signaal"):
        assert forbidden not in html.lower().replace("geen advies", "").replace("koopknop", ""), forbidden
    # the operational cards of milestone 8 live on /eigenaar now
    st, h, body = client.req("GET", "/eigenaar", {"Cookie": token})
    html = body.decode()
    assert st == 200
    for word in ("Gezondheid en versheid", "Dekking van de laatste 24 uur", "Beslissingen vandaag", "X-stand en planner", "Budgetten vandaag",
                 "Laatste dagrapport", "Go/no-go van het laatste weekrapport", "Instellingen (alleen-lezen)", "Logboek", "Uitloggen"):
        assert word in html, word
    assert html.count('class="is-nc"') == 4 and "NIET GEDEKT" in html and "nooit gezien" in html and "weerlegd 1 ($0.02)" in html
    assert data.rules_fingerprint() in html and "sha256:e3bf4320c45c" in html and "test-rules-v1" in html and "m1.0" in html
    assert "Bevroren go/no-go-criteria" in html and "configured" in html and "Deze website" in html
    for secret in (PASSWORD, world["hash"], world["secret"], token.split("=", 1)[1]):
        assert secret not in html
    st, h, body = client.req("GET", "/instellingen", {"Cookie": token})
    assert st == 303 and client.header(h, "location") == ["/eigenaar#instellingen"]
    st, h, body = client.req("GET", "/observaties", {"Cookie": token})
    html = body.decode()
    assert st == 200 and "Observaties" in html and "30 verschillende kopers in 5 minuten" in html and "geen eigen uitkomst (thema)" in html and "nog bezig" in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html and "<script>alert" not in html
    st, h, body = client.req("GET", "/rapporten", {"Cookie": token})
    html = body.decode()
    assert st == 200 and f"/rapporten/{world['yesterday']}" in html and f"/rapporten/week-{world['yesterday']}" in html and f"/rapporten/{world['not_covered_day']}" in html
    assert html.index(f"week-{world['yesterday']}") < html.index(f"/rapporten/{world['not_covered_day']}")  # newest first
    st, h, body = client.req("GET", f"/rapporten/{world['not_covered_day']}.md", {"Cookie": token})
    html = body.decode()
    assert st == 200 and "<h1>Dagrapport recall" in html and "<table>" in html and "<strong>NIET GEDEKT</strong>" in html
    assert "<script>alert" not in html and "Geen enkel uur van deze dag was gedekt" in html
    st, h, body = client.req("GET", f"/rapporten/{world['yesterday']}.md", {"Cookie": token})
    html = body.decode()
    assert st == 200 and "<script>alert" not in html and "&lt;script&gt;alert(1)&lt;/script&gt;" in html  # the coin name from the report stays text
    assert client.req("GET", "/rapporten/../.env", {"Cookie": token})[0] == 400
    assert client.req("GET", "/rapporten/.env", {"Cookie": token})[0] == 404
    assert client.req("GET", "/rapporten/2026-01-01.md", {"Cookie": token})[0] == 404
    assert client.req("GET", "/rapporten/2026-01-01", {"Cookie": token})[0] == 404
    st, h, body = client.req("GET", f"/rapporten/week-{world['yesterday']}.md", {"Cookie": token})
    assert st == 200 and "Go/no-go community-alerts" in body.decode()
    st, h, body = client.req("GET", "/onbekend", {"Cookie": token})
    assert st == 404 and "bestaat niet" in body.decode()
    st, h, body = client.req("HEAD", "/login")
    assert st == 200 and body == b"" and int(client.header(h, "content-length")[0]) > 100


def test_security_headers_on_every_response(client):
    token = client.session()
    cases = [("GET", "/", None, 401), ("GET", "/login", None, 200), ("GET", "/static/app.css", None, 200), ("GET", "/static/app.js", None, 200),
             ("GET", "/static/icon.svg", None, 200), ("GET", "/api/status", None, 401), ("GET", "/api/public/status", None, 401), ("GET", "/methode", None, 401),
             ("GET", "/", token, 200), ("GET", "/api/coverage", token, 200), ("GET", "/api/public/status", token, 200), ("GET", "/nope", token, 404),
             ("GET", "/static/nope.css", None, 404), ("PUT", "/", None, 405)]
    for method, path, cookie, expected in cases:
        st, h, body = client.req(method, path, {"Cookie": cookie} if cookie else {})
        assert st == expected, (method, path, st)
        hd = {k.lower(): v for k, v in h}
        assert hd["content-security-policy"].startswith("default-src 'self'") and "unsafe-inline" not in hd["content-security-policy"], path
        assert "frame-ancestors 'none'" in hd["content-security-policy"] and "script-src 'self'" in hd["content-security-policy"]
        assert hd["x-content-type-options"] == "nosniff" and hd["referrer-policy"] == "no-referrer" and hd["x-frame-options"] == "DENY", path
        assert hd["server"] == "fart" and "python" not in hd["server"].lower()
        assert hd["cache-control"] == ("public, max-age=300" if path.startswith("/static/") and st == 200 else "no-store"), path
    st, h, body = client.req("GET", "/static/app.js")
    js = body.decode()
    assert "30000" in js and "eval(" not in js and "innerHTML" not in js and "textContent" in js and "DOMParser" in js and "data-live" in js
    assert client.header(h, "content-type") == ["text/javascript; charset=utf-8"]


def test_request_size_limit_and_methods(client):
    big = "x" * (64 * 1024 + 1)
    st, h, body = client.req("POST", "/login", {"Content-Type": "application/x-www-form-urlencoded"}, big)
    assert st == 413 and b"64 KB" in body
    st, h, body = client.req("GET", "/login", {"X-A": "y" * 40000, "X-B": "z" * 30000})
    assert st == 431
    st, h, body = client.req("POST", "/login", {"Transfer-Encoding": "chunked", "Content-Type": "application/x-www-form-urlencoded"}, None)
    assert st == 411
    st, h, body = client.req("GET", "/login", {"Content-Length": "5"}, "abcde")
    assert st == 400
    assert client.req("DELETE", "/")[0] == 405 and client.req("OPTIONS", "/")[0] == 405
    # just under the limit is fine (the password field is simply wrong)
    cookie, token = client.login_form()
    form = urllib.parse.urlencode({"csrf": token, "password": "p" * 60000})
    st, h, body = client.req("POST", "/login", {"Cookie": cookie, "Content-Type": "application/x-www-form-urlencoded"}, form)
    assert st == 401


# ----- the Markdown subset and the page helpers -------------------------------------------------------------------------------
def test_markdown_subset_is_safe():
    text = ("# Kop\n\nTekst met **vet**, `code` en <b>html</b> en [link](http://evil) <img src=x onerror=alert(1)>.\n\n"
            "- een\n- twee **x**\n\n1. eerste\n2. tweede\n\n| a | b | c |\n|---|---:|:---:|\n| 1 | 2 | 3 |\n| `<i>` | **4** | 5 |\n\nNa de tabel.\n\n### Diep\n\n---\n")
    out = markdown.render(text)
    assert "<h1>Kop</h1>" in out and "<strong>vet</strong>" in out and "<code>code</code>" in out
    assert "&lt;b&gt;html&lt;/b&gt;" in out and "<b>" not in out and "<img" not in out and "onerror" in out and "<a " not in out
    assert out.count("<li>") == 4 and "<ul>" in out and "<ol>" in out
    assert '<th class="num">b</th>' in out and '<th class="center">c</th>' in out and '<td class="num"><strong>4</strong></td>' in out
    assert "<td><code>&lt;i&gt;</code></td>" in out and "<p>Na de tabel.</p>" in out and "<h3>Diep</h3>" in out and "<hr>" in out
    assert markdown.inline("a ** b") == "a ** b" and markdown.inline("`**x**`") == "<code>**x**</code>"
    assert pages.fmt(None) == "–" and pages.fmt(True) == "ja" and pages.fmt(False) == "nee" and pages.fmt([1, 2]) == "1, 2" and pages.fmt(0) == "0"
    assert pages.get_path({"a": [{"b": 1}]}, "a.0.b") == 1 and pages.get_path({"a": []}, "a.0.b") is None and pages.get_path(None, "x") is None
    t = pages.table([("a", "A", "num")], [{"a": "<b>", "_class": "is-nc"}])
    assert '<tr class="is-nc"><td class="num" data-label="A">&lt;b&gt;</td></tr>' in t and "<colgroup>" not in t
    t = pages.table([("a", "A", "num"), ("b", "B")], [{"a": None}], widths=("30", "70"))
    assert '<col class="w-30"><col class="w-70">' in t and 'data-label="A">–</td>' in t
    assert "geen" in pages.table([("a", "A")], [])


# ----- deploy files ------------------------------------------------------------------------------------------------------------
def test_unit_installer_env_example_and_caddy_file():
    unit = (ROOT / "deploy" / "fart-web.service").read_text()
    lines = {l.split("=", 1)[0]: l.split("=", 1)[1] for l in unit.splitlines() if "=" in l and not l.startswith("#") and not l.startswith("[")}
    assert lines["ReadWritePaths"] == "/opt/fart/logs" and set(lines["ReadOnlyPaths"].split()) == {"/opt/fart/data", "/opt/fart/out"}
    assert lines["CapabilityBoundingSet"] == "" and lines["AmbientCapabilities"] == "" and lines["ProtectSystem"] == "strict"
    assert lines["User"] == "fart" and lines["NoNewPrivileges"] == "yes" and lines["IPAddressDeny"] == "any" and lines["IPAddressAllow"] == "localhost"
    assert "SystemCallFilter=@system-service" in unit and "SystemCallFilter=~@privileged @resources" in unit
    assert lines["ExecStart"].endswith("python -m fart --quiet web") and lines["RestartPreventExitStatus"] == "2" and lines["Restart"] == "on-failure"
    for other in ("fart-engine.service", "fart-social.service"):
        ref = (ROOT / "deploy" / other).read_text()
        for key in ("ProtectHome=yes", "PrivateTmp=yes", "PrivateDevices=yes", "ProtectKernelTunables=yes", "RestrictNamespaces=yes", "LockPersonality=yes", "UMask=0027"):
            assert key in ref and key in unit, key
    inst = (ROOT / "deploy" / "install.sh").read_text()
    assert "FART_WEB_PASSWORD_HASH" in inst and "FART_WEB_SECRET" in inst and "fart-web.service" in inst and "NOT started" in inst and "/srv/fart/fart.caddy" in inst
    env = {}
    for line in (ROOT / ".env.example").read_text().splitlines():
        s = line.strip()
        if s and not s.startswith("#") and "=" in s:
            k, v = s.split("=", 1)
            env[k.strip()] = v.strip()
    assert env["FART_WEB_PASSWORD_HASH"] == "" and env["FART_WEB_SECRET"] == "" and env["FART_WEB_BIND"] == "127.0.0.1:8792"
    caddy = (ROOT / "deploy" / "caddy" / "fart.caddy").read_text()
    assert "kingdegen.dev {" in caddy and "www.kingdegen.dev" not in caddy.split("{", 1)[0].split("\n")[-1] and "encode zstd gzip" in caddy and "reverse_proxy 127.0.0.1:8792" in caddy
    assert "request_body {" in caddy and "max_size 64KB" in caddy and "Strict-Transport-Security" in caddy and "-Server" in caddy
    directives = {l.strip().split()[0] for l in caddy.splitlines() if l.strip() and not l.strip().startswith(("#", "}", "?", "-"))
                  and not l.strip().endswith("{") or l.strip().endswith("{") and not l.strip().startswith("#")}
    allowed = {"kingdegen.dev", "encode", "request_body", "max_size", "header", "Strict-Transport-Security", "defer", "reverse_proxy"}
    assert directives <= allowed, directives - allowed  # plain Caddy 2.6.2 directives only, no plugin
    deploy_md = (ROOT / "deploy" / "DEPLOY.md").read_text()
    for needle in ("## 5g. Milestone 8: website", "fart-setsecret.sh", "caddy validate", "Caddyfile.before-fart-", "launch.kingdegen.dev",
                   "systemctl restart larp-scanner-https", "curl -sI https://kingdegen.dev", "journalctl -u larp-scanner-https", "import /srv/fart/fart.caddy"):
        assert needle in deploy_md, needle
