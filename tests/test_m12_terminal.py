"""Milestone 12, part A: the Terminal. The feed (one computation shared by every viewer), the server-sent-events stream over a
real socket (a snapshot first, a delta within one cycle of a new trade, a resume with Last-Event-ID, a follow list, the
heartbeat, the connection bound, a closed tab noticed), the /terminal and /plays pages for a visitor (no owner vocabulary,
no inline script or style, no buy button, no profit words), the snapshot JSON (no owner fields; unknown stays unknown),
the presets and the link patterns, and the strictness of static/terminal.js (DOM calls only, no HTML from data, no wallet)."""
from __future__ import annotations

import copy
import json
import re
import socket
import threading
import time
from html import unescape
from pathlib import Path

import pytest

from fart import db
from fart.web import pages, public, server, terminal
from tests.test_m8_web import NOW, Client, build_world
from tests.test_m9_public_site import OWNER_KEY_WORDS, OWNER_PAGE_WORDS, _flatten, _headers

ROOT = Path(__file__).resolve().parent.parent
TRADE_SQL = ("INSERT INTO trades(mint, block_time, received_at, signature, buyer, is_buy, sol, tokens, virtual_sol, virtual_token, source)"
             " VALUES(?,?,?,?,?,?,?,?,?,?,?)")


# ----- the world and the server ------------------------------------------------------------------------------------------
@pytest.fixture(scope="module")
def world(tmp_path_factory):
    from fart import version

    mp = pytest.MonkeyPatch()
    mp.setenv("FART_RULES_VERSION", "test-rules-v1")
    for k in ("FART_WEB_PASSWORD_HASH", "FART_WEB_SECRET", "FART_WEB_BIND", "FART_WEB_PUBLIC"):
        mp.delenv(k, raising=False)
    version._CACHE.pop("v", None)
    yield build_world(tmp_path_factory.mktemp("terminal-root"), NOW, public=True)
    mp.undo()


@pytest.fixture(scope="module")
def live(world):
    """The application on a real server. The feed is cycled by the tests (deterministic), not by its thread."""
    clock = [NOW]
    app = server.App(world["settings"], clock=lambda: clock[0])
    srv = server.make_server(app, "127.0.0.1", 0)
    port = srv.server_address[1]
    t = threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.1}, daemon=True)
    t.start()
    app.refresh_views()
    app.feed.cycle()
    yield {"app": app, "port": port, "clock": clock, "world": world, "client": Client(port)}
    srv.shutdown()
    srv.server_close()


# ----- raw server-sent events --------------------------------------------------------------------------------------------
def open_stream(port: int, query: str = "", extra_headers: str = "") -> socket.socket:
    s = socket.create_connection(("127.0.0.1", port), timeout=5)
    s.sendall(f"GET /api/public/stream{query} HTTP/1.1\r\nHost: t\r\nAccept: text/event-stream\r\n{extra_headers}\r\n".encode())
    return s


def read_for(sock: socket.socket, seconds: float) -> bytes:
    sock.settimeout(0.25)
    buf, end = b"", time.time() + seconds
    while time.time() < end:
        try:
            chunk = sock.recv(65536)
            if not chunk:
                break
            buf += chunk
        except socket.timeout:
            continue
    return buf


def dechunk(data: bytes) -> bytes:
    out = b""
    while data:
        line, sep, data = data.partition(b"\r\n")
        if not sep:
            break
        try:
            n = int(line.strip(), 16)
        except ValueError:
            break
        if n == 0:
            break
        out += data[:n]
        data = data[n + 2:]
    return out


def parse_events(text: str) -> list[dict]:
    evs = []
    for block in text.split("\n\n"):
        ev: dict = {}
        for line in block.splitlines():
            if line.startswith("id: "):
                ev["id"] = int(line[4:])
            elif line.startswith("event: "):
                ev["event"] = line[7:]
            elif line.startswith("data: "):
                ev["data"] = line[6:]
            elif line.startswith("retry: "):
                ev["retry"] = int(line[7:])
            elif line.startswith(":"):
                ev["comment"] = line[1:].strip()
        if ev:
            evs.append(ev)
    return evs


def head_and_events(raw: bytes) -> tuple[str, list[dict]]:
    head, _, rest = raw.partition(b"\r\n\r\n")
    return head.decode(), parse_events(dechunk(rest).decode())


def more_events(sock: socket.socket, seconds: float) -> list[dict]:
    return parse_events(dechunk(read_for(sock, seconds)).decode())


# ----- the pages --------------------------------------------------------------------------------------------------------
def test_terminal_and_plays_pages_render_for_a_visitor(live):
    client = live["client"]
    mint = live["world"]["live_mints"][0][0]
    for path in ("/terminal", "/plays", f"/terminal?mint={mint}"):
        st, h, body = client.req("GET", path)
        hd = _headers(h)
        html = body.decode()
        assert st == 200 and hd["content-type"] == "text/html; charset=utf-8", path
        assert hd["content-security-policy"].startswith("default-src 'self'") and "unsafe-inline" not in hd["content-security-policy"], path
        assert hd["x-frame-options"] == "DENY" and hd["cache-control"] == "public, max-age=30" and hd["vary"] == "Cookie", path
        assert 'style="' not in html and "<style" not in html and "onclick" not in html and "javascript:" not in html, path
        assert 'href="/login"' in html and "Uitloggen" not in html, path
        assert "Observatie, geen advies. Alles wordt achteraf gemeten." in html and 'href="/methode#disclaimer"' in html, path
        for word in OWNER_PAGE_WORDS:
            assert word not in html, (path, word)
        low = html.lower()
        for word in ("winst", "rendement", "voorspelling"):
            assert word not in low, (path, word)
        cleaned = low.replace("koopknop", "").replace("verkoop", "").replace("kopen", "").replace("koper", "").replace("gekocht", "")
        assert "koop" not in cleaned, path       # never a buy button, never a buy word: a link opens another site
        assert 'href="/terminal"' in html and 'href="/plays"' in html, path   # the navigation carries both new pages
    st, h, body = client.req("GET", "/terminal")
    html = body.decode()
    assert '<script src="/static/terminal.js" defer></script>' in html and "/static/app.js" not in html
    m = re.search(r'id="terminal" data-config="([^"]*)"', html)
    assert m, "the configuration travels in a data attribute"
    cfg = json.loads(unescape(m.group(1)))
    assert [t["name"] for t in cfg["terminals"]] == ["Axiom", "Photon", "BullX", "GMGN"]
    assert all(t["url"].startswith("https://") and "{mint}" in t["url"] and t["verified"] is False for t in cfg["terminals"])
    assert {p["id"] for p in cfg["presets"]} == {"nieuw", "momentum", "bijna", "organisch", "thema", "makers"}
    assert [c["id"] for c in cfg["columns"]] == [c[0] for c in terminal.COLUMNS] and len(cfg["columns"]) == 17
    assert cfg["links"] == {"pumpfun": "https://pump.fun/coin/{mint}", "dexscreener": "https://dexscreener.com/solana/{mint}", "solscan": "https://solscan.io/token/{mint}"}
    assert cfg["stream"] == "/api/public/stream" and cfg["snapshot"] == "/api/public/terminal" and cfg["pulse_s"] == 15 and cfg["follow_max"] == 50
    assert cfg["creates_window_s"] == 1800
    for needle in ('id="t-body"', 'id="t-filters"', 'data-preset="organisch"', 'data-preset="bijna"', 'id="t-follow"', 'id="t-plays"', 'id="d-note"', 'id="t-sound"',
                   "1 tot 5", "config/plays.json is leeg", "docs/UNVERIFIED.md", "Axiom, Photon, BullX, GMGN", 'data-sort="buyers"', 'data-col="x"', "kopieer het mint-adres"):
        assert needle in html, needle
    assert html.count("<th ") == 17
    st2, h2, body2 = client.req("GET", "/terminal", {"If-None-Match": _headers(h)["etag"]})
    assert st2 == 304 and body2 == b""
    st, h, body = client.req("GET", "/static/terminal.js")
    assert st == 200 and _headers(h)["content-type"] == "text/javascript; charset=utf-8" and _headers(h)["cache-control"] == "public, max-age=300"
    # the tools page points at the Terminal and Plays and still promises no buy buttons
    html = client.req("GET", "/tools")[2].decode()
    assert 'href="/terminal"' in html and 'href="/plays"' in html and "koopknop" in html


def test_terminal_config_keeps_only_https_patterns_with_a_mint(world):
    s2 = copy.deepcopy(world["settings"])
    s2.configs.setdefault("web", {})["terminals"] = [{"name": "Good", "url": "https://x.example/t/{mint}", "verified": True}, {"name": "NoMint", "url": "https://x.example/t/"},
                         {"name": "Plain", "url": "http://x.example/{mint}"}, {"name": "", "url": "https://x.example/{mint}"}, "nonsense",
                         {"name": "A very long terminal name that gets cut off here", "url": "https://a.example/{mint}"}] + [{"name": f"T{i}", "url": f"https://t{i}.example/{{mint}}"} for i in range(6)]
    cfg = terminal.terminal_config(s2)
    assert [t["name"] for t in cfg["terminals"]] == ["Good", "A very long terminal nam", "T0", "T1", "T2"]   # https + {mint} only, 5 at most
    assert cfg["terminals"][0]["verified"] is True and cfg["terminals"][1]["verified"] is False
    assert terminal.parse_follow("") == [] and terminal.parse_follow(None) == []
    mint = "Mint05" + "x" * 30
    assert terminal.parse_follow(f"{mint}, bad one ,{mint},{'y' * 60},{'z' * 30}") == [mint, mint, "z" * 30]
    assert len(terminal.parse_follow(",".join("a" * 31 for _ in range(300)))) == 200


def test_presets_and_columns_are_the_pre_registered_ones():
    by = {p["id"]: p for p in terminal.PRESETS}
    assert by["nieuw"]["filter"] == {"max_age_s": 300}
    assert by["organisch"]["filter"] == {"bundled": "no", "max_dev_frac": 0.02, "max_share": 0.15}
    assert by["bijna"]["filter"]["min_curve_sol"] == 70 and "graduated" not in by["bijna"]["filter"]["status"]
    assert by["momentum"]["filter"]["rising"] is True and by["thema"]["filter"] == {"theme": "burning"} and by["makers"]["filter"] == {"maker": "track"}
    assert all(p["label"] and p["text"] for p in terminal.PRESETS)
    ids = [c[0] for c in terminal.COLUMNS]
    assert ids == ["age", "coin", "mcap", "prog", "buyers", "net", "bs", "share", "dev", "bundle", "maker", "theme", "x", "last", "status", "plays", "links"]
    assert [c[0] for c in terminal.COLUMNS if not c[2]] == ["x"]     # X mentions are off by default: the measurement is sparse
    assert terminal.STATUS_NL["observe"] == "observatie" and terminal.STATUS_NL["graduated"] == "gegradueerd"


# ----- the snapshot JSON and the plays JSON --------------------------------------------------------------------------------
def test_snapshot_and_plays_json_carry_no_owner_fields_and_keep_unknown_unknown(live):
    client, world = live["client"], live["world"]
    root = str(world["settings"].root)
    for path in ("/api/public/terminal", "/api/public/plays"):
        st, h, body = client.req("GET", path)
        hd = _headers(h)
        assert st == 200 and hd["content-type"] == "application/json; charset=utf-8" and hd["etag"], path
        assert hd["cache-control"] == ("public, max-age=1" if path.endswith("terminal") else "public, max-age=30"), path
        text = body.decode()
        doc = json.loads(text)
        for key, value in _flatten(doc):
            last = key.rsplit(".", 1)[-1].split("[")[0].lower()
            for word in OWNER_KEY_WORDS:
                assert word not in last, (path, key, word)
            if isinstance(value, str):
                assert "/opt/" not in value and root not in value and not re.search(r"\b\d{1,3}(\.\d{1,3}){3}\b", value), (path, key, value)
        for secret in (world["hash"], world["secret"], root, "127.0.0.1", "8792", "fart.db", "X_MODE"):
            assert secret not in text, (path, secret)
        assert "generated_at" in doc and "generated_label" in doc
    snap = json.loads(client.req("GET", "/api/public/terminal")[2])
    rows = {r["mint"]: r for r in snap["rows"]}
    live_mints = [m for m, _ in world["live_mints"]]
    assert snap["n"] == len(snap["rows"]) >= 4 and snap["engine_age_s"] == 20 and snap["sol_usd"] == {"price": 180.0, "known": True, "age_s": 120}
    assert snap["rows"] == sorted(snap["rows"], key=lambda r: -r["created_t"]) and snap["creates_window_s"] == 1800 and snap["plays_rules"] == []
    quiet = rows[live_mints[3]]           # created 20 minutes ago, never traded, not on the watchlist: everything unknown stays None
    assert quiet["status"] == "unwatched" and quiet["status_label"] == "niet gevolgd" and quiet["age_s"] == 1200
    assert quiet["mcap_sol"] is None and quiet["mcap_usd"] is None and quiet["prog"] is None and quiet["curve_sol"] is None and quiet["share"] is None
    assert quiet["bundled"] is None and quiet["buyers_5m"] == 0 and quiet["net_sol_5m"] == 0.0 and quiet["last_trade_age_s"] is None
    assert quiet["maker_stats"] is None and quiet["x_1h"] is None and quiet["d_buyers"] is None and quiet["plays"] == []
    one = rows[live_mints[2]]             # one buy: the largest buyer holds 100%, the bundle flag cannot be judged from one trade
    assert one["buyers_5m"] == 1 and one["share"] == 1.0 and one["bundled"] is None and one["status"] == "probation" and one["trades_total"] == 1
    assert one["mcap_sol"] > 0 and 0 < one["prog"] < 1 and one["mcap_usd"] == round(one["mcap_sol"] * 180)
    whale = rows[live_mints[0]]           # twelve buys by eight wallets, a sell, a whale: known figures, an observation
    assert whale["status"] == "observe" and whale["observed"] is True and whale["graduated"] is False
    assert whale["buyers_5m"] == 9 and whale["buys_5m"] == 13 and whale["sells_5m"] == 1 and whale["trades_total"] == 14
    assert whale["bundled"] is False and whale["bundle_reasons"] == [] and whale["dev_frac"] == round(0.5 / 85, 4) and whale["dev_buy_sol"] == 0.5
    assert whale["dev_sold"] is False and whale["theme"]["term"] in ("quantum", "frog") and whale["theme"]["bursting"] is True
    assert whale["mint_short"] == whale["mint"][:4] + "…" + whale["mint"][-4:] and whale["plays"] == [] and whale["maker"] == "CrA"
    assert 0 < whale["share"] <= 1 and whale["net_sol_5m"] > 0
    plays = json.loads(client.req("GET", "/api/public/plays")[2])
    assert plays["active_rules"] == 0 and plays["rules"] == [] and plays["per_rule"] == [] and plays["layer"]["state"] == "never"
    assert plays["counts"] == {"signals": 0, "open": 0, "closed": 0, "unknown": 0, "no_fill": 0} and "config/plays.json" in plays["note"]
    assert plays["size_sol"] == 0.5 and plays["entry_delay_s"] == 20 and plays["alerts_per_hour"] == 6 and "bewezen" in plays["definition"].lower()


# ----- the stream -----------------------------------------------------------------------------------------------------
def test_stream_sends_a_snapshot_then_deltas_resumes_follows_and_bounds(live):
    app, port, clock, world, client = live["app"], live["port"], live["clock"], live["world"], live["client"]
    feed = app.feed
    clock[0] = NOW + 100
    feed.cycle()
    before = feed.connections
    sock = open_stream(port)
    head, evs = head_and_events(read_for(sock, 1.0))
    assert head.startswith("HTTP/1.1 200")
    hl = head.lower()
    for needle in ("content-type: text/event-stream; charset=utf-8", "transfer-encoding: chunked", "cache-control: no-cache, no-store", "x-accel-buffering: no"):
        assert needle in hl, needle
    assert "content-length" not in hl
    assert evs[0].get("retry") == 3000
    names = [e["event"] for e in evs if "event" in e]
    assert names[0] == "snapshot" and "plays" in names and "heartbeat" not in names[:1]
    snap = json.loads(next(e["data"] for e in evs if e.get("event") == "snapshot"))
    assert snap["n"] >= 4 and all("mint" in r for r in snap["rows"])
    plays_ev = json.loads(next(e["data"] for e in evs if e.get("event") == "plays"))
    assert plays_ev["rows"] == [] and plays_ev["rules"] == []
    last_id = max(e["id"] for e in evs if "id" in e)
    assert feed.connections == before + 1
    # a trade arrives (its transaction commits a moment after its received_at, as on the VPS): the next cycle sends one delta
    conn = db.connect(world["settings"].db_path)
    mint = world["live_mints"][0][0]
    buyers_before = next(r for r in snap["rows"] if r["mint"] == mint)["buyers_5m"]
    t = clock[0] + 1
    conn.execute(TRADE_SQL, (mint, t - 1.5, t - 1, "sig-m12-stream-1", "Fresh", 1, 3.0, 3e6, 60_000_000_000, 500_000_000_000_000, "rpc_trades"))
    conn.commit()
    clock[0] = t
    out = feed.cycle()
    assert out["updates"] == 1 and out["removed"] == 0
    evs2 = more_events(sock, 1.0)
    rows_ev = [e for e in evs2 if e.get("event") == "rows"]
    assert len(rows_ev) == 1 and rows_ev[0]["id"] > last_id and "snapshot" not in [e.get("event") for e in evs2]
    d = json.loads(rows_ev[0]["data"])
    assert [u["mint"] for u in d["upd"]] == [mint] and d["del"] == [] and d["n"] == snap["n"]
    assert d["upd"][0]["buyers_5m"] == buyers_before + 1 and d["upd"][0]["trades_total"] == 15 and d["upd"][0]["last_trade_age_s"] == 2
    new_last = rows_ev[0]["id"]
    sock.close()
    # the same trade again (a replayed row with the same signature) changes nothing
    conn.execute("INSERT OR IGNORE INTO trades(mint, block_time, received_at, signature, buyer, is_buy, sol, tokens, virtual_sol, virtual_token, source) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                 (mint, t - 1.5, t - 1, "sig-m12-stream-1", "Fresh", 1, 3.0, 3e6, 60_000_000_000, 500_000_000_000_000, "rpc_trades"))
    conn.commit()
    clock[0] += 1
    assert feed.cycle()["updates"] == 0
    # a resume: the viewer that saw `last_id` gets the delta it missed and no new snapshot
    s2 = open_stream(port, "", f"Last-Event-ID: {last_id}\r\n")
    _, evs_r = head_and_events(read_for(s2, 1.0))
    names_r = [e["event"] for e in evs_r if "event" in e]
    assert "snapshot" not in names_r and [e["id"] for e in evs_r if e.get("event") == "rows"] == [new_last]
    s2.close()
    # a viewer with an id the server does not know gets a fresh snapshot (the query form of the id works too)
    s3 = open_stream(port, "?last=999999")
    _, evs_s = head_and_events(read_for(s3, 1.0))
    assert [e["event"] for e in evs_s if "event" in e][0] == "snapshot"
    s3.close()
    s3b = open_stream(port, "", "Last-Event-ID: nonsense\r\n")
    _, evs_s = head_and_events(read_for(s3b, 1.0))
    assert [e["event"] for e in evs_s if "event" in e][0] == "snapshot"
    s3b.close()
    # a follow list keeps a coin outside the window and off the watchlist in this viewer's universe
    old = world["mints"][5][0]
    assert old not in {r["mint"] for r in snap["rows"]}
    s4 = open_stream(port, f"?follow={old},not-a-mint")
    read_for(s4, 0.5)
    assert old in feed._followed() and "not-a-mint" not in feed._followed()
    clock[0] += 1
    feed.cycle()
    evs_f = more_events(s4, 1.0)
    d = json.loads(next(e["data"] for e in evs_f if e.get("event") == "rows"))
    followed = next(u for u in d["upd"] if u["mint"] == old)
    assert followed["status"] == "unwatched" and followed["name"] == "Coin 5" and followed["age_s"] > 3600
    assert old in {r["mint"] for r in feed.snapshot()["rows"]}
    s4.close()
    # the heartbeat: every heartbeat_s, with the engine's age and the number of viewers
    clock[0] += feed.heartbeat_s + 1
    s5 = open_stream(port)
    read_for(s5, 0.4)
    feed.cycle()
    hb = [e for e in more_events(s5, 1.0) if e.get("event") == "heartbeat"]
    assert hb, "a heartbeat event"
    beat = json.loads(hb[0]["data"])
    assert beat["engine_age_s"] == round(clock[0] - (NOW - 20)) and beat["viewers"] >= 1 and beat["coins"] >= 5 and beat["t"]
    s5.close()
    # a closed tab is noticed within seconds (the stream checks its socket every second)
    deadline = time.time() + 6
    while feed.connections > before and time.time() < deadline:
        time.sleep(0.2)
    assert feed.connections == before
    assert old not in feed._followed()
    clock[0] += 1
    out = feed.cycle()
    assert out["removed"] == 1                            # the followed coin leaves the universe with its viewer
    # the connection bound: one viewer too many gets a 503 with Retry-After, never a half-open stream
    a = open_stream(port)
    read_for(a, 0.3)
    old_max = feed.max_connections
    feed.max_connections = feed.connections
    try:
        st, h, body = client.req("GET", "/api/public/stream")
        assert st == 503 and _headers(h)["retry-after"] == "30" and json.loads(body) == {"error": "too many live connections", "retry_after_s": 30}
    finally:
        feed.max_connections = old_max
        a.close()
    status = app.views_status(clock[0])["terminal"]
    assert status["max_connections"] == 64 and status["heartbeat_s"] == 15 and status["cycles"] >= 5 and status["errors"] == 0
    conn.close()


def test_stream_resume_after_a_quiet_spell_and_the_dropped_event(live):
    app, port, clock = live["app"], live["port"], live["clock"]
    feed = app.feed
    clock[0] += 2
    feed.cycle()
    s = open_stream(port)
    _, evs = head_and_events(read_for(s, 0.8))
    last = max(e["id"] for e in evs if "id" in e)
    # a viewer up to date with the server: nothing to send first, the stream simply waits
    s2 = open_stream(port, f"?last={feed._seq}")
    _, evs2 = head_and_events(read_for(s2, 0.8))
    assert [e for e in evs2 if "event" in e] == [] and evs2[0].get("retry") == 3000
    s2.close()
    s.close()
    assert last <= feed._seq
    # a viewer that cannot keep up is told so once and then disconnected
    sub, _ = feed.subscribe([], None)
    for i in range(terminal.QUEUE_MAX + 5):
        sub.put(terminal.Event(i, "rows", "{}"))
    assert sub.dropped is True
    feed.unsubscribe(sub)
    deadline = time.time() + 6
    while feed.connections > 0 and time.time() < deadline:
        time.sleep(0.2)
    assert feed.connections == 0


# ----- the script --------------------------------------------------------------------------------------------------------
def test_terminal_script_builds_dom_strictly_and_never_trades():
    js = (ROOT / "fart" / "web" / "static" / "terminal.js").read_text(encoding="utf-8")
    for forbidden in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval(", "new Function", "wallet", "signTransaction", "sendTransaction",
                      "http://", "setAttribute('style'", "swap(", "/buy"):
        assert forbidden not in js, forbidden
    for needed in ("new EventSource(", "'last='", "'follow='", "fart.terminal.follow", "fart.terminal.notes", "fart.terminal.filters", "fart.terminal.cols",
                   "noopener", "'/api/public/stream'", "'/api/public/terminal'", "AudioContext", "case 'j'", "case 'k'", "case 'c'", "case 'f'", "case '5'",
                   "replaceState", "navigator.clipboard", "visibilitychange", "'dropped'", "'heartbeat'", "'snapshot'", "'rows'", "'plays'", "createElement"):
        assert needed in js, needed
    assert js.count("textContent") > 20   # every cell is text set with DOM calls
    css = (ROOT / "fart" / "web" / "static" / "app.css").read_text(encoding="utf-8")
    for needed in (".t-table", ".t-detail", '.chip[aria-pressed="true"]', ".density-dense", ".t-toast", ".follow-item", ".play-head", "@media (max-width: 639px)"):
        assert needed in css, needed


# ----- the Plays page: empty, then with a rule -------------------------------------------------------------------------------
def test_plays_page_and_view_empty_then_filled(live):
    client, app, world = live["client"], live["app"], live["world"]
    html = client.req("GET", "/plays")[2].decode()
    assert "config/plays.json is leeg" in html and "Nog niets bewezen" in html and 'href="/terminal"' in html and "nog nooit getikt" in html
    s = world["settings"]
    from fart.research import hypotheses as hyps

    v = hyps.get("H2").variant("m5_ts5")
    rule = {"id": "H2-v1", "name": hyps.get("H2").title_nl, "hypothesis": "H2", "variant": v.id, "version": 1, "label": v.label_nl, "entry": dict(v.entry), "exit": v.exit,
            "exit_rules": v.rules.to_dict(), "exit_text": v.rules.describe_nl(), "promoted_at": "2026-10-28T06:00:00Z", "report": "2026-10-28-H2", "active": True,
            "figures": {"ev_sol": 0.041, "ev_pct": 0.082, "ci_low_sol": 0.012, "ci_high_sol": 0.07, "hit_rate": 0.58, "cases": 131, "unknown_share": 0.05, "lead_median_s": 420,
                        "capacity_median_sol": 3.1, "test_days": ["2026-10-26", "2026-10-27"], "size_sol": 0.5, "entry_delay_s": 20}}
    cfg = {"version": 1, "size_sol": 0.5, "entry_delay_s": 20, "alerts": {"max_per_hour": 6}, "rules": [rule]}
    conn = db.connect(s.db_path)
    m0, m1 = world["live_mints"][0][0], world["live_mints"][1][0]
    cols = ("rule, version, hypothesis, variant, mint, as_of, created_at, features_json, entry_json, size_sol, status, entry_time, entry_price, fill_price, tokens, fees_sol,"
            " exit_time, exit_price, exit_reason, exit_sol_net, pnl_sol, pnl_pct, rules_version, json")
    conn.execute(f"INSERT INTO plays({cols}) VALUES({','.join('?' * 24)})",
                 ("H2-v1", 1, "H2", "m5_ts5", m0, NOW - 1200, NOW - 1199, json.dumps({"unique_buyers_5m": 12, "net_sol_5m": 6.5, "curve_sol": 75.4}), json.dumps({"curve_sol": 75.4, "mcap_sol": 220.0}),
                  0.5, "closed", NOW - 1180, 1e-7, 1.02e-7, 4.9e6, 0.0101, NOW - 880, 1.3e-7, "time_stop", 0.579, 0.079, 0.158, "test-rules-v1", "{}"))
    conn.execute(f"INSERT INTO plays({cols}) VALUES({','.join('?' * 24)})",
                 ("H2-v1", 1, "H2", "m5_ts5", m1, NOW - 60, NOW - 59, json.dumps({"unique_buyers_5m": 7, "net_sol_5m": 5.2, "curve_sol": 75.1}), json.dumps({"curve_sol": 75.1, "mcap_sol": 219.0}),
                  0.5, "open", NOW - 40, 1e-7, 1.01e-7, 4.95e6, 0.005, None, None, None, None, None, None, "test-rules-v1", "{}"))
    conn.commit()
    old = s.configs.get("plays")
    s.configs["plays"] = cfg
    try:
        view = public.plays_view(conn, s, NOW)
        assert view["active_rules"] == 1 and view["rules"][0]["id"] == "H2-v1" and view["counts"] == {"signals": 2, "open": 1, "closed": 1, "unknown": 0, "no_fill": 0}
        pr = view["per_rule"][0]
        assert pr["id"] == "H2-v1" and pr["closed"] == 1 and pr["open"] == 1 and pr["unknown"] == 0 and pr["no_fill"] == 0 and pr["cases"] == 131
        assert "0,079" in pr["pnl_sum_label"] and "58" in pr["measured_hit_label"] and "100" in pr["live_hit_label"] and len(pr["rows"]) == 2
        assert pr["rows"][0]["status"] == "open" and pr["rows"][1]["status"] == "closed" and pr["rows"][1]["exit_label"] and pr["rows"][1]["mint"] == m0
        for key, value in _flatten(view):
            last = key.rsplit(".", 1)[-1].split("[")[0].lower()
            for word in OWNER_KEY_WORDS:
                assert word not in last, (key, word)
        html = pages.plays_page(app.ctx(None, NOW), view)
        assert "H2-v1" in html and "resultaat na kosten" in html and m0 in html and "tijdstop" in html.lower() and "Quantum Frog" in html
        for word in OWNER_PAGE_WORDS:
            assert word not in html, word
        low = html.lower()
        assert "winst" not in low and "rendement" not in low and 'style="' not in html
        # the terminal feed shows the rule and the open play once it recomputes
        clock = live["clock"]
        clock[0] += 6
        app.feed.cycle()
        snap = app.feed.snapshot()
        assert [r["id"] for r in snap["plays_rules"]] == ["H2-v1"]
        plays_now = app.feed.plays()
        assert [p["mint"] for p in plays_now["rows"]] == [m1] and plays_now["rows"][0]["status"] == "open" and plays_now["rows"][0]["hit_rate"] == 0.58
        assert next(r for r in snap["rows"] if r["mint"] == m1)["plays"] == ["H2-v1"]
    finally:
        if old is None:
            s.configs.pop("plays", None)
        else:
            s.configs["plays"] = old
        conn.execute("DELETE FROM plays")
        conn.commit()
        conn.close()
        live["clock"][0] += 6
        app.feed.cycle()
