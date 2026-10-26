"""Milestone 9: the public website (`fart web` with FART_WEB_PUBLIC=1).

The fixture world of milestone 8 (covered and NOT covered hours, decisions with every outcome status, a NIET GEDEKT day
report, a week report that says "te weinig data") is served in public mode by the real server; the tests speak HTTP to
it. The clock of the application is a list the tests move (the cache's TTL, the rate limiter's window)."""
from __future__ import annotations

import json
import re
import threading
from pathlib import Path

import pytest

from fart.config import load_settings
from fart.web import auth, charts, data, pages, public, server, theme
from tests.test_m8_web import NOW, PASSWORD, Client, build_world

ROOT = Path(__file__).resolve().parent.parent
PUBLIC_PAGES = ("/", "/observaties", "/meting", "/rapporten", "/methode", "/tools")
PUBLIC_API = ("/api/public/status", "/api/public/coverage", "/api/public/observations", "/api/public/reports", "/api/public/week", "/api/public/live",
              "/api/public/hot", "/api/public/new", "/api/public/themes", "/api/public/graduations", "/api/public/social")

# the owner vocabulary that may never reach a public JSON document (keys) or a public page
OWNER_KEY_WORDS = ("budget", "rpc_cap", "x_cap", "usd_today", "x_usd", "cu_", "secret", "hash", "password", "token", "bind", "planner", "daemon", "heartbeat",
                   "spend", "blocked", "x_mode", "api_key", "x_key", "active_keys", "stopped", "deferred", "shadow", "failures", "open_gaps", "gaps_label",
                   "db_path", "root", "ip", "schema", "plan", "ledger", "log", "paths", "cost")
OWNER_PAGE_WORDS = ("budget", "plafond", "planner", "heartbeat", "daemon", "sleutel", "/opt/", "127.0.0.1", "8792", "systemd", "Caddy", "X_MODE",
                    "logboek", "CU ", "USD", "$0.0", "schaduwbeslissing", "ReadOnlyPaths")


@pytest.fixture(scope="module")
def world(tmp_path_factory):
    from fart import version

    mp = pytest.MonkeyPatch()
    mp.setenv("FART_RULES_VERSION", "test-rules-v1")
    for k in ("FART_WEB_PASSWORD_HASH", "FART_WEB_SECRET", "FART_WEB_BIND", "FART_WEB_PUBLIC"):
        mp.delenv(k, raising=False)
    version._CACHE.pop("v", None)
    w = build_world(tmp_path_factory.mktemp("web-public"), NOW, public=True)
    assert w["settings"].web_public is True
    yield w
    mp.undo()


@pytest.fixture(scope="module")
def live(world):
    clock = [NOW]
    app = server.App(world["settings"], clock=lambda: clock[0])
    srv = server.make_server(app, "127.0.0.1", 0)
    port = srv.server_address[1]
    t = threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.1}, daemon=True)
    t.start()
    yield {"app": app, "port": port, "clock": clock, "world": world}
    srv.shutdown()
    srv.server_close()


@pytest.fixture
def client(live):
    live["app"].guard = auth.LoginGuard(live["app"].clock)
    live["app"].limiter = server.RateLimiter()      # every test starts with a fresh window
    live["clock"][0] = NOW
    live["app"].refresh_views()   # milestone 10: the public views are precomputed (by a thread in production; here by the test)
    return Client(live["port"])


def _flatten(obj, prefix: str = ""):
    """(key path, value) pairs of a JSON document."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield from _flatten(v, f"{prefix}.{k}" if prefix else str(k))
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            yield from _flatten(v, f"{prefix}[{i}]")
    else:
        yield prefix, obj


def _headers(h) -> dict[str, str]:
    return {k.lower(): v for k, v in h}


# ----- the public pages ------------------------------------------------------------------------------------------------
def test_public_pages_render_without_a_session(client, world):
    for path in PUBLIC_PAGES:
        st, h, body = client.req("GET", path)
        hd = _headers(h)
        html = body.decode()
        assert st == 200 and hd["content-type"] == "text/html; charset=utf-8", path
        assert '<html lang="nl">' in html and "KingDegen" in html and "radar" in html and "motor: fart" in html, path
        assert hd["cache-control"] == ("public, max-age=15" if path == "/" else "public, max-age=30") and hd["etag"].startswith('"') and hd["vary"] == "Cookie", path
        assert hd["content-security-policy"].startswith("default-src 'self'") and "unsafe-inline" not in hd["content-security-policy"]
        assert hd["x-frame-options"] == "DENY" and hd["x-content-type-options"] == "nosniff" and hd["referrer-policy"] == "no-referrer"
        assert 'style="' not in html and "<style" not in html and "onclick" not in html and "<script>alert" not in html, path
        assert "Observatie, geen advies. Alles wordt achteraf gemeten." in html and 'href="/methode#disclaimer"' in html
        assert 'href="/login"' in html and "Uitloggen" not in html   # a visitor sees the owner's door, never the logout form
        for word in OWNER_PAGE_WORDS:
            assert word not in html, (path, word)
        cleaned = (html.lower().replace("koopknop", "").replace("verkoop", "").replace("kopen", "").replace("koper", "")
                   .replace("er staat nergens een rendement, een winst of een voorspelling", ""))
        for forbidden in ("winst", "rendement", "signaal", "koop"):
            assert forbidden not in cleaned, (path, forbidden)
        # a matching ETag gets 304 without a body
        st2, h2, body2 = client.req("GET", path, {"If-None-Match": hd["etag"]})
        assert st2 == 304 and body2 == b"" and _headers(h2)["etag"] == hd["etag"], path
    # an unknown page is 404 for a visitor (not a login prompt), the owner routes are 401
    assert client.req("GET", "/nope")[0] == 404
    assert client.req("GET", "/eigenaar")[0] == 401 and client.req("GET", "/instellingen")[0] == 401
    assert client.req("GET", f"/rapporten/{world['yesterday']}.md")[0] == 401
    st, h, body = client.req("GET", "/api/status")
    assert st == 401 and json.loads(body) == {"error": "login required", "login": "/login"}


def test_live_dashboard_shows_processed_data(client, world):
    st, h, body = client.req("GET", "/")
    html = body.decode()
    assert st == 200 and 'data-refresh-ms="15000"' in html and "ververst elke 15 s" in html
    # the hero: tempo, money on the watched coins, graduations; the SOL price the figures are converted with
    assert ("Nu: <strong>0,0</strong> nieuwe munten per minuut, op de 4 gevolgde munten <strong>15</strong> kopers en <strong>30,9 SOL</strong> netto instroom in 5 minuten, "
            "<strong>0</strong> graduaties in het laatste uur.") in html
    assert 'class="pill pill-fresh"' in html and "De radar luistert nu." in html and "SOL $180,00 (2 min oud)" in html
    assert "gegevens van 26 okt 16:53 (15:53 UTC)" in html
    # the order of the cards: where the money goes, the newest coins and the themes, the graduations, X and today, the measurement line, the warning
    marks = ["<h2>Waar het geld nu heen gaat</h2>", "<h2>Nieuwste munten</h2>", "<h2>Thema’s die nu opkomen</h2>", "<h2>Net gegradueerd</h2>", "<h2>Wat rondgaat op X</h2>",
             "<h2>Observaties vandaag</h2>", "<h2>Meting</h2>", "<h2>Let op</h2>"]
    positions = [html.index(m) for m in marks]
    assert positions == sorted(positions)
    # hot coins: sorted by net inflow, the observation first, mcap in SOL and USD, the whale's share, the mini bars
    live = world["live_mints"]
    i0, i1, i2 = (html.index(f'title="{m}"') for m, _ in live[:3])
    assert i0 < i1 < i2 and "Quantum Frog ($QFROG)" in html and "28,5 SOL" in html and "51,5 SOL" in html and "≈ $9.266" in html and "20%" in html
    assert 'class="pill pill-winner" title="observatie">observatie<' in html and 'class="pill pill-pending" title="nieuw (eerste 5 minuten)">nieuw<' in html
    assert html.count('<svg class="mini"') == 3 and 'width="100"' in html and "3 van 4 gevolgde munten actief" in html
    assert live[3][0] not in html.split("<h2>Nieuwste munten</h2>")[0]   # the fourth live coin is not watched: not in the hot table
    # the newest coins, newest first, with dev-buy, start cap and the maker's history; the tempo pill
    newest = html.split("<h2>Nieuwste munten</h2>")[1]
    assert newest.index("Quantum Frog Returns ($QFR)") < newest.index("QUANTUMFROG 2 ($QF2)") < newest.index("Frog Quantum Pepe ($FQP)")
    assert "dev-buy 3,50 SOL · start 34,0 SOL · maker eerder: 0" in html and "0,0 per minuut" in html and "vandaag 11 nieuwe munten" in html
    # the themes: the engine's own clustering over the last three hours: "quantum" and "frog" from three makers, new words
    assert "<span class=\"theme-term\">quantum</span>" in html and "<span class=\"theme-term\">frog</span>" in html and "3 munten van 3 makers" in html
    assert "nieuw woord" in html and "2 opkomend" in html and "instroom 32,0 SOL" in html
    # the graduations: the newest sample, the stricter outcome rule, the lead time
    assert "$42.000" in html and "$110.000" in html and "ja, 20 min vooraf" in html and "ja, erna" in html and "geen sample" in html
    assert 'class="pill pill-winner">winnaar<' in html and html.count('class="pill pill-unknown">onbekend<') >= 3
    # X: off, honestly
    assert 'class="pill pill-unknown">staat uit<' in html and "De X-laag staat uit: X is op dit moment niet gekoppeld." in html
    # today's observations, compact, with the status pills; the measurement line
    assert "Observaties vandaag" in html and 'class="pill pill-pending">nog bezig<' in html and "mint-adres (tekst, geen link): <code>Live00" in html
    assert "nog niet begonnen" in html and "te weinig data" in html and 'href="/meting"' in html
    for term in ("observatie", "thema", "winnaar", "meetweek", "niet-gedekt"):
        assert f'href="/methode#begrip-{term}"' in html, term
    assert html.count('class="explain"') >= 7
    names = re.findall(r'data-live="([^"]+)"', html)
    assert len(names) == len(set(names)) and {"hero", "hot", "nieuw", "themas", "graduaties", "sociaal", "vandaag", "meting-strip"} <= set(names)
    assert "Geen advies. Hoog risico: de meeste van deze munten gaan naar nul." in html and 'href="https://' not in html


def test_measurement_page_order_and_pills(client, world):
    st, h, body = client.req("GET", "/meting")
    html = body.decode()
    assert st == 200
    marks = ["<h1>Hoe goed ziet de radar het?</h1>", "<h2>Meetweek</h2>", "<h2>Dekking en eerlijkheid</h2>", "<h2>Laatste rapporten</h2>", "<h2>Hoe dit werkt</h2>", "<h2>Let op</h2>"]
    positions = [html.index(m) for m in marks]
    assert positions == sorted(positions)
    # the week card: the position, "te weinig data" for the intervals, the verdict in plain words, the frozen rules
    assert "nog niet begonnen" in html and "27 okt 2026" in html
    assert 'class="pill pill-few">te weinig data<' in html and 'class="pill pill-nogo">NO-GO<' in html and 'class="pill pill-frozen">regels bevroren<' in html
    assert "1 van 6 criteria gehaald, 2 niet gehaald, voor 3 is er nog te weinig data." in html
    assert "minstens 15%" in html and "hoogstens 12" in html and "minstens 10 min" in html
    # the coverage card: the strip with four hatched hours, the count, the sentence
    assert html.count('class="ch-cell ch-nc"') == 4 and "4 uur NIET GEDEKT" in html
    assert "4 van de 24 uren waren NIET GEDEKT: in die uren keek de radar niet, dus staat er geen telling. Een leeg uur is geen nul." in html
    assert 'class="chart-svg chart-bars"' in html and "Nieuwe munten per uur" in html and 'class="sr-only"' in html
    for term in ("trefkans", "recall", "niet-gedekt", "meetweek", "bootstrapinterval"):
        assert f'href="/methode#begrip-{term}"' in html, term
    assert html.count('class="explain"') >= 4
    # the two newest reports with key points and links
    assert f'href="/rapporten/week-{world["yesterday"]}"' in html and f'href="/rapporten/{world["yesterday"]}"' in html
    assert "trefkans <b>50%</b>" in html and "uren NIET GEDEKT <b>19</b>" in html


def test_live_views_and_social_in_shadow_mode(world):
    s = world["settings"]
    conn = data.open_db(s.db_path)
    try:
        hot = public.hot_coins_view(conn, s, NOW)
        assert [r["status"] for r in hot["rows"]] == ["observed", "new", "new"] and hot["rows"][0]["buyers_5m"] == 9 and hot["rows"][0]["sells_5m"] == 1
        assert hot["rows"][0]["largest_share_15m"] == 0.2 and hot["rows"][2]["largest_share_label"] == "100%" and hot["rows"][0]["share_of_top"] == 100
        assert hot["rows"][0]["mcap_usd"] == 9266 and hot["rows"][0]["progress"] is not None and hot["with_trades"] == 3 and hot["watched"] == 4
        th = public.themes_view(conn, s, NOW)
        assert th["coins_in_window"] == 4 and {r["term"] for r in th["rows"]} == {"quantum", "frog"} and all(r["bursting"] and r["burst_is_new"] for r in th["rows"])
        assert th["rows"][0]["sol_inflow"] == 32.0 and th["rows"][0]["net_sol"] == 30.5 and th["rows"][0]["unique_buyers"] == 14 and th["rows"][0]["watched"] == (th["rows"][0]["term"] == "quantum")
        gr = public.graduations_view(conn, s, NOW)
        assert [r["outcome"] for r in gr["rows"]] == ["unknown", "unknown", "unknown", "winner"] and gr["rows"][2]["mcap_usd"] == 42000.0 and gr["rows"][0]["mcap_usd"] is None
        assert gr["rows"][3]["sample_age_s"] == 0 or gr["rows"][3]["sample_age_label"]   # the dense samples reach "now"
        nw = public.new_coins_view(conn, s, NOW)
        assert nw["rows"][0]["title"] == "Quantum Frog Returns ($QFR)" and nw["rows"][0]["watched"] is False and nw["rows"][3]["observed"] is True and nw["today"] == 11
        off = public.social_view(conn, s, NOW)
        assert off["active"] is False and off["keys"] == [] and "staat uit" in off["note"]
        s.x_bearer_token = "token-for-the-test"
        try:
            sh = public.social_view(conn, s, NOW)
        finally:
            s.x_bearer_token = ""
        assert sh["active"] is True and sh["mode"] == "shadow" and sh["counts"]["keys_active"] == 1 and sh["keys"][0]["title"] == "Coin 3 ($C3)" and sh["keys"][0]["last_n_1h"] == 2
        assert "token-for-the-test" not in json.dumps(sh)
        # no watched coins: an honest empty state, never a zero table
        conn2 = data.open_db(s.db_path)
        conn2.close()
        lv = public.live_view(conn, s, NOW)
        assert lv["watched"]["buyers_5m"] == 15 and lv["watched"]["net_sol_5m"] == 30.9 and lv["creates"]["last_hour"] == 4 and lv["sol_usd"]["known"] is True
    finally:
        conn.close()


def test_niet_gedekt_stays_empty_everywhere(client, world):
    nc = world["not_covered_day"]
    st, h, body = client.req("GET", f"/rapporten/{nc}")
    html = body.decode()
    assert st == 200 and 'class="pill pill-nc">NIET GEDEKT<' in html and "zaterdag 24 oktober 2026" in html
    assert "Geen enkel uur van deze dag was gedekt: het universum is onbekend, niet leeg, en elk percentage blijft leeg." in html
    assert html.count('class="kpi-value is-empty">–<') == 5 and "0%" not in html and "Winnaars van de dag" not in html
    assert html.count('class="ch-cell ch-nc"') == 24 and html.count('class="ch-bar ch-nc"') == 24
    v = json.loads(client.req("GET", f"/api/public/reports/{nc}")[2])
    assert v["blank"] is True and v["universe"] is None and v["observations"] is None and v["recall"] is None and v["winners"] == []
    assert [i["value"] for i in v["key_points"]] == [None, None, None, None, None, 24]
    assert all(i["label_value"] == "–" for i in v["key_points"][:5]) and v["coverage"]["creates"] is None
    cv = json.loads(client.req("GET", "/api/public/coverage")[2])
    ncs = [r for r in cv["rows"] if r["status"] == "not_covered"]
    assert len(ncs) == 4 and all(r["creates"] is None and r["grads"] is None and r["creates_label"] == "–" and r["status_label"] == "NIET GEDEKT" for r in ncs)
    assert cv["summary"]["hours_not_covered"] == 4 and "geen nul" in cv["sentence"]
    # the covered day: a real report page with the universe, the winners (mint as text) and the unknowns
    st, h, body = client.req("GET", f"/rapporten/{world['yesterday']}")
    html = body.decode()
    assert st == 200 and 'class="pill pill-incomplete">NIET COMPLEET<' in html and "Winnaars van de dag" in html
    assert "<code>Mint00xxxxxxxxxxxxxxxxxxxxxxxxxxxxxx</code>" in html and "&lt;script&gt;alert(1)&lt;/script&gt;" in html and "<script>alert" not in html
    assert "$120.000" in html and "Wat onbekend bleef" in html and "uren NIET GEDEKT" in html
    # the week page: te weinig data, hatched days, the band absent, the criteria
    st, h, body = client.req("GET", f"/rapporten/week-{world['yesterday']}")
    html = body.decode()
    assert st == 200 and 'class="chart-svg chart-rates"' in html and 'class="ch-band' not in html and "geen band voor trefkans: te weinig data" in html
    assert html.count('class="pill pill-nc">NIET GEDEKT<') == 2 and "1 van 6 criteria gehaald" in html and "9.999 trekkingen" in html
    # the measurement-week card data
    wk = json.loads(client.req("GET", "/api/public/week")[2])
    assert wk["found"] and wk["verdict_kind"] == "nogo" and wk["hit_rate"]["ci"]["status"] == "few" and wk["hit_rate"]["ci"]["low"] is None
    assert wk["position"]["phase"] == "before" and wk["position"]["first_full_day"] == "2026-10-27" and wk["position"]["rules_frozen"] is True
    assert [d["covered"] for d in wk["days"]] == [False, False, True] and wk["days"][0]["hit_rate"] is None and wk["days"][2]["hit_rate"] == 0.5
    assert {c["key"]: c["status_kind"] for c in wk["criteria"]}["hit_rate_ci_low"] == "few"


# ----- the charts ----------------------------------------------------------------------------------------------------------
def test_svg_charts_with_empty_and_full_data():
    # empty
    for svg in (charts.coverage_strip([]), charts.bars([], "v", "l", "Titel", "stuks"), charts.rate_lines([], None, None)):
        assert "<svg" in svg and 'class="sr-only"' in svg and 'style="' not in svg and "nog geen" in svg
    # the coverage strip and the bars: NIET GEDEKT is hatched, never a zero bar
    rows = [{"clock": f"{h:02d}:00", "status": "not_covered" if h < 3 else ("covered" if h == 3 else "complete"), "covered": h >= 3, "complete": h > 3, "creates": None if h < 3 else h}
            for h in range(24)]
    strip = charts.coverage_strip(rows)
    assert strip.count('class="ch-cell ch-nc"') == 3 and strip.count('class="ch-cell ch-covered"') == 1 and strip.count('class="ch-cell ch-complete"') == 20
    assert "20 uur compleet, 1 uur gedekt maar niet compleet, 3 uur NIET GEDEKT (gearceerd: geen telling, geen nul)" in strip
    bars = charts.bars(rows, "creates", "clock", "Nieuwe munten per uur", "munten")
    assert bars.count('class="ch-bar ch-nc"') == 3 and bars.count('class="ch-bar"') == 21 and 'fill="url(#hatch)"' in bars
    assert "3 NIET GEDEKT (gearceerd, geen telling)" in bars and "hoogste waarde 23" in bars and "<title>00:00: NIET GEDEKT</title>" in bars
    assert re.search(r'class="ch-tick" text-anchor="end">\d+<', bars) and "0,7" not in bars   # whole-number ticks for counts
    # the rate lines: a gap for a day without a value, a band when the interval exists
    days = [{"day": f"2026-11-0{i}", "day_label": f"{i} nov 2026", "covered": i != 3, "hit_rate": None if i in (3, 5) else 0.1 * i, "recall": None if i == 3 else 0.5} for i in range(1, 8)]
    ok_ci = {"status": "ok", "low": 0.12, "high": 0.4, "low_label": "12%", "high_label": "40%"}
    few_ci = {"status": "few", "low": None, "high": None, "low_label": "–", "high_label": "–"}
    svg = charts.rate_lines(days, ok_ci, few_ci)
    assert svg.count('class="ch-band ch-band-hit"') == 1 and "ch-band-recall" not in svg and "band trefkans 12% tot 40%" in svg and "geen band voor recall" in svg
    assert svg.count('class="ch-point ch-line-hit"') == 5 and svg.count('class="ch-point ch-line-recall"') == 6
    assert svg.count('class="ch-line ch-line-hit"') == 2   # the line breaks at the days without a value
    assert svg.count('<rect x="') >= 1 and "1 dagen NIET GEDEKT (gearceerd)" in svg and "<title>3 nov 2026: NIET GEDEKT</title>" in svg
    assert "trefkans op 5 van 7 dagen bekend (laagste 10%, hoogste 70%)" in svg
    assert charts._nice_top(0) == (1.0, 1) and charts._nice_top(2) == (2.0, 2) and charts._nice_top(7) == (8.0, 4) and charts._nice_top(130) == (150.0, 3) and charts._nice_top(2600) == (3000.0, 3)


# ----- the public JSON ---------------------------------------------------------------------------------------------------
def test_public_json_carries_no_owner_fields(client, world, live):
    root = str(world["settings"].root)
    token = client.session()
    owner = json.loads(client.req("GET", "/api/status", {"Cookie": token})[2])
    owner_x = json.loads(client.req("GET", "/api/x", {"Cookie": token})[2])
    sensitive_values = {str(v) for k, v in _flatten(owner["budgets"])} | {str(v) for k, v in _flatten(owner_x["budget"])} | {world["hash"], world["secret"], PASSWORD, root}
    # a day filter is an on-demand view (milestone 10): the first request says "computing" at once and asks the thread for it
    st, h, body = client.req("GET", f"/api/public/observations?day={world['yesterday']}")
    assert st == 200 and json.loads(body)["state"] == "computing" and _headers(h)["cache-control"] == "public, max-age=2"
    assert f"observations:{world['yesterday']}" in live["app"].refresh_views()
    for path in PUBLIC_API + (f"/api/public/reports/{world['yesterday']}", f"/api/public/reports/week-{world['yesterday']}", f"/api/public/observations?day={world['yesterday']}"):
        st, h, body = client.req("GET", path)
        hd = _headers(h)
        live = path.rsplit("/", 1)[-1].split("?")[0] in server.App.LIVE_VIEWS
        assert st == 200 and hd["content-type"] == "application/json; charset=utf-8" and hd["cache-control"] == f"public, max-age={15 if live else 30}" and hd["etag"], path
        text = body.decode()
        doc = json.loads(text)
        for key, value in _flatten(doc):
            last = key.rsplit(".", 1)[-1].split("[")[0].lower()
            for word in OWNER_KEY_WORDS:
                assert word not in last, (path, key, word)
            if isinstance(value, str):
                assert "/opt/" not in value and root not in value and not re.search(r"\b\d{1,3}(\.\d{1,3}){3}\b", value), (path, key, value)
        for secret in (world["hash"], world["secret"], PASSWORD, root, "127.0.0.1", "8792", "X_MODE", "fart.db"):
            assert secret not in text, (path, secret)
        for v in sensitive_values:
            if len(v) >= 6:
                assert v not in text, (path, v)
        assert "generated_at" in doc and "generated_label" in doc
    st, h, body = client.req("GET", "/api/public/nope")
    assert st == 404 and "endpoints" in json.loads(body)
    assert client.req("GET", "/api/public/observations?day=nonsense")[0] == 400
    assert client.req("GET", "/api/public/reports/2026-01-01")[0] == 404 and client.req("GET", "/api/public/reports/../x")[0] == 400
    # the status document: the live numbers, the freshness, the week position, the brand; nothing operational
    stv = json.loads(client.req("GET", "/api/public/status")[2])
    assert stv["site"] == {"name": "KingDegen", "product": "radar", "tagline": world["settings"].web_site["tagline"], "domain": "kingdegen.dev"}
    assert stv["freshness"]["status"] == "fresh" and stv["freshness"]["age_s"] == 10 and stv["live"]["observations_today"] == 5 and stv["live"]["graduations_today"] == 2
    assert stv["live"]["creates_last_hour"] == 4 and stv["live"]["hour_covered"] is True and stv["week"]["phase"] == "before"
    assert stv["headline"] == "De radar ziet in het laatste uur 4 nieuwe munten, vandaag 2 graduaties en 5 observaties."
    assert set(stv) == {"generated_at", "generated_label", "site", "day", "day_label", "refresh_s", "warning", "banner", "week", "freshness", "db_exists", "live", "totals", "headline"}
    # the observations document: plain words and the outcome rules
    ob = json.loads(client.req("GET", "/api/public/observations")[2])
    assert ob["counts"] == {"total": 7, "coins": 6, "themes": 1, "winner": 1, "not_winner": 1, "unknown": 1, "pending": 3} and ob["limit"] == 500
    labels = [(r["kind_label"], r["outcome_label"]) for r in ob["rows"]]
    assert labels == [("munt", "nog bezig"), ("munt", "nog bezig"), ("thema", "geen eigen uitkomst (thema)"), ("munt", "nog bezig"), ("munt", "onbekend"), ("munt", "geen winnaar"), ("munt", "winnaar")]
    unknown = [r for r in ob["rows"] if r["outcome"] == "unknown"][0]
    assert unknown["peak_mcap_usd"] is None and unknown["peak_label"] == "–"
    winner = [r for r in ob["rows"] if r["outcome"] == "winner"][0]
    assert winner["peak_label"] == "$150.000" and winner["title"] == "Pepe <script>alert(1)</script> coin ($C0)" and "features" not in winner
    assert "30 verschillende kopers" in winner["why"] and winner["mint_short"] == "Mint…xxxx"
    by_day = json.loads(client.req("GET", f"/api/public/observations?day={world['yesterday']}")[2])
    assert {r["day"] for r in by_day["rows"]} == {world["yesterday"]} and len(by_day["rows"]) == 2


# ----- cache, ETag and the rate limit -------------------------------------------------------------------------------------
def test_views_are_precomputed_and_a_request_never_computes(client, live):
    """Milestone 10: the store computes the views on its schedule (15 s live, 30 s the rest); a request reads the last
    value and never triggers a computation; a view that was never computed says so instead of waiting."""
    app = live["app"]
    store = app.store
    before = store.computes
    st, h, body = client.req("GET", "/api/public/coverage")
    first = json.loads(body)["generated_at"]
    for _ in range(5):
        st, h, body2 = client.req("GET", "/api/public/coverage")
        assert json.loads(body2)["generated_at"] == first
    client.req("GET", "/")
    assert store.computes == before                                  # requests computed nothing at all
    assert app.refresh_views() == []                                 # nothing is due at NOW: everything was computed at NOW
    live["clock"][0] = NOW + 16
    client.req("GET", "/")
    assert store.computes == before                                  # a request after the TTL still computes nothing: the thread does
    done = app.refresh_views()
    assert set(done) == set(server.App.LIVE_VIEWS)                   # the live views were due after 15 s, the rest after 30 s
    assert store.computes == before + len(server.App.LIVE_VIEWS)
    live["clock"][0] = NOW + 29
    assert app.refresh_views() == []
    live["clock"][0] = NOW + 31
    done = app.refresh_views()
    assert set(done) >= set(server.App.LIVE_VIEWS) | set(server.App.REST_VIEWS) and "report:week-" + live["world"]["yesterday"] in done   # the report pages ride along
    assert store.computes - before >= 2 * len(server.App.LIVE_VIEWS) + len(server.App.REST_VIEWS) + 3
    st, h, body3 = client.req("GET", "/api/public/coverage")
    assert json.loads(body3)["generated_at"] != first
    # a matching ETag is a 304; the owner's answers are never cacheable by a shared cache
    st, h3, body4 = client.req("GET", "/api/public/coverage", {"If-None-Match": _headers(h)["etag"]})
    assert st == 304 and body4 == b""
    live["clock"][0] = NOW
    token = client.session()
    st, h, body = client.req("GET", "/api/public/coverage", {"Cookie": token})
    assert st == 200 and _headers(h)["cache-control"] == "no-store" and "etag" not in _headers(h)
    st, h, body = client.req("GET", "/", {"Cookie": token})
    assert st == 200 and _headers(h)["cache-control"] == "no-store" and "Uitloggen" in body.decode()
    # the store itself: a never-computed key is None, a request for an on-demand key is remembered, the clock moving back is stale
    n = {"calls": 0}

    def compute(key, conn):
        n["calls"] += 1
        return {"key": key, "n": n["calls"]}

    clock = [1000.0]
    st_ = server.ViewStore(compute, lambda: [("a", 15.0), ("b", 30.0)], clock=lambda: clock[0], max_items=2)
    assert st_.get("a") is None and st_.misses == 1
    assert st_.refresh() == ["a", "b"] and st_.get("a").value == {"key": "a", "n": 1} and st_.hits == 1
    assert st_.refresh() == [] and n["calls"] == 2
    clock[0] = 1015.0
    assert st_.refresh() == ["a"] and n["calls"] == 3
    clock[0] = 1030.0
    assert set(st_.refresh()) == {"a", "b"}
    assert st_.request("obs:x") is None and st_.refresh() == ["obs:x"] and st_.get("obs:x").value["key"] == "obs:x"
    for k in ("obs:y", "obs:z"):
        st_.request(k)
    st_.refresh()
    assert st_.get("obs:x") is None and len(st_._wanted) == 2            # bounded: the oldest on-demand key went
    clock[0] = 900.0                                                      # the clock moved back: every entry is stale
    assert set(st_.refresh()) >= {"a", "b"}
    rows = {r["view"]: r for r in st_.stats()}
    assert rows["a"]["state"] == "computed" and rows["a"]["seconds"] >= 0 and rows["a"]["errors"] == 0
    # a failing view keeps its previous value, counts the error and is retried when due again; a slow one is logged
    calls = {"n": 0}

    def flaky(key, conn):
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("boom")
        return calls["n"]

    clock2 = [0.0]
    st2 = server.ViewStore(flaky, lambda: [("v", 10.0)], clock=lambda: clock2[0])
    st2.refresh()
    clock2[0] = 10.0
    st2.refresh()
    e = st2.get("v")
    assert e.value == 1 and e.errors == 1 and e.ok is False and "RuntimeError: boom" in e.last_error and st2.failures == 1
    assert st2.refresh() == []                                            # not retried before its TTL
    clock2[0] = 20.0
    assert st2.refresh() == ["v"] and st2.get("v").value == 3 and st2.get("v").ok is True and st2.get("v").errors == 1


def test_rate_limit_per_ip_on_public_routes(client, live):
    ip = {"X-Forwarded-For": "203.0.113.50"}
    for i in range(server.RATE_LIMIT):
        st, h, body = client.req("GET", "/api/public/status", ip)
        assert st == 200, i
    st, h, body = client.req("GET", "/api/public/status", ip)
    assert st == 429 and _headers(h)["retry-after"] == "60" and json.loads(body)["error"] == "too many requests"
    st, h, body = client.req("GET", "/", ip)
    assert st == 429 and "Te veel verzoeken" in body.decode() and _headers(h)["cache-control"] == "no-store"
    assert client.req("GET", "/api/public/status", {"X-Forwarded-For": "203.0.113.51"})[0] == 200   # another address is not affected
    assert client.req("GET", "/static/app.css", ip)[0] == 200                                         # static files are outside the limit
    assert client.req("GET", "/login", ip)[0] == 200
    token = client.session()
    assert client.req("GET", "/api/public/status", {"Cookie": token, **ip})[0] == 200                 # the owner is never limited
    live["clock"][0] = NOW + 61
    assert client.req("GET", "/api/public/status", ip)[0] == 200                                        # the window slid
    lim = server.RateLimiter(limit=2, window_s=10, max_clients=2)
    assert lim.check("a", 0.0) is None and lim.check("a", 1.0) is None and lim.check("a", 2.0) == 8.0
    assert lim.check("b", 3.0) is None and lim.check("c", 20.0) is None and len(lim._hits) <= 2   # bounded: idle clients are evicted


# ----- the owner section and the public switch ----------------------------------------------------------------------------
def test_owner_section_behind_the_login_and_the_switch(client, world, tmp_path):
    token = client.session()
    st, h, body = client.req("GET", "/eigenaar", {"Cookie": token})
    html = body.decode()
    assert st == 200 and _headers(h)["cache-control"] == "no-store" and 'content="noindex, nofollow"' in html
    for word in ("Gezondheid en versheid", "Budgetten vandaag", "X-stand en planner", "Instellingen (alleen-lezen)", "Logboek (samenvatting)", "weerlegd 1 ($0.02)",
                 "sha256:e3bf4320c45c", 'data-live="eigenaar-gezondheid"', "de ruwe Markdown"):
        assert word in html, word
    for secret in (PASSWORD, world["hash"], world["secret"], token.split("=", 1)[1]):
        assert secret not in html
    # logged in, the public pages show the owner's navigation and are never shared-cacheable
    st, h, body = client.req("GET", "/methode", {"Cookie": token})
    assert st == 200 and _headers(h)["cache-control"] == "no-store" and "Uitloggen" in body.decode()
    # the switch: FART_WEB_PUBLIC=0 puts everything behind the login (the milestone-8 suite covers the rest)
    private = build_world(tmp_path / "private", NOW, public=False)
    app = server.App(private["settings"], clock=lambda: NOW)
    assert app.handle(server.Request("GET", "/", {}, {}, b"", "127.0.0.1")).status == 401
    assert app.handle(server.Request("GET", "/api/public/status", {}, {}, b"", "127.0.0.1")).status == 401
    assert app.handle(server.Request("GET", "/methode", {}, {}, b"", "127.0.0.1")).status == 401
    assert app.handle(server.Request("GET", "/static/app.css", {}, {}, b"", "127.0.0.1")).status == 200
    assert app.handle(server.Request("GET", "/login", {}, {}, b"", "127.0.0.1")).status == 200
    # and FART_WEB_PUBLIC in the process environment overrides .env
    s = load_settings(root=private["settings"].root, env={"FART_WEB_PUBLIC": "1"})
    assert s.web_public is True and s.describe()["web_public"] is True


def test_method_tools_login_and_status_line(client, world, capsys, tmp_path):
    st, h, body = client.req("GET", "/methode")
    html = body.decode()
    assert st == 200
    for key in pages.GLOSSARY:
        assert f'id="begrip-{key}"' in html, key
    for word in ("minstens 25 unieke kopers en 10,0 SOL netto instroom in 5 minuten", "sha256:e3bf4320c45c", "150ca73cb354", "27 okt 2026", "$100.000", "$20.000",
                 "9.999", 'id="disclaimer"', "Geen advies. Hoog risico", "geen koopknoppen", 'id="go-no-go"', "hoogstens 12"):
        assert word in html, word
    st, h, body = client.req("GET", "/tools")
    assert st == 200 and "Binnenkort" in body.decode() and "dezelfde data" in body.decode()
    st, h, body = client.req("GET", "/login")
    assert st == 200 and "De openbare pagina’s hebben geen wachtwoord nodig" in body.decode() and 'content="noindex, nofollow"' in body.decode()
    # the server's own tokens of the public pages: every data-live name is unique on a page
    st, h, body = client.req("GET", "/")
    st, h, body = client.req("GET", "/meting")
    names = re.findall(r'data-live="([^"]+)"', body.decode())
    assert len(names) == len(set(names)) and {"meetweek", "dekking", "rapporten"} <= set(names)
    # fart status names the public switch
    from fart.cli import main

    assert main(["--root", str(world["settings"].root), "--quiet", "status"]) == 0
    out = capsys.readouterr().out
    assert "bind 127.0.0.1:0 (fart-web.service, read-only, behind Caddy), public section on" in out


# ----- the brand and the accent colour ----------------------------------------------------------------------------------
def test_accent_override_and_site_config(world, tmp_path):
    assert theme.accent_override("#F2B33D") == "" and theme.accent_override("red") == "" and theme.accent_override(None) == "" and theme.accent_override("#12345") == ""
    css = theme.accent_override("#3366CC")
    assert "--accent: #3366CC;" in css and "--accent-rgb: 51 102 204;" in css and "--accent-dark: #" in css and "--accent-contrast: #FFFFFF" in css
    dark = theme.parse_hex(re.search(r"--accent-dark: (#[0-9A-F]{6})", css).group(1))
    assert theme.contrast(dark, (0xF6, 0xF5, 0xF1)) >= 4.5   # the darkened twin keeps AA on the light background
    assert theme.contrast((0xF2, 0xB3, 0x3D), (0x0E, 0x11, 0x17)) >= 4.5 and theme.contrast((0x8A, 0x5A, 0x00), (0xFF, 0xFF, 0xFF)) >= 4.5
    # a configured accent reaches the served stylesheet without any inline style
    s = load_settings(root=world["settings"].root, env={})
    s.configs["web"] = {**s.configs["web"], "accent": "#3366CC", "site_name": "Proefnaam", "tagline": ""}
    app = server.App(s, clock=lambda: NOW)
    css_served = app.static["app.css"].decode()
    assert css_served.endswith(theme.accent_override("#3366CC")) and css_served.startswith("/* KingDegen radar")
    page = app.handle(server.Request("GET", "/tools", {}, {}, b"", "127.0.0.1")).body.decode()
    assert "Proefnaam" in page and 'style="' not in page and s.web_site["tagline"] == world["settings"].web_site["tagline"]   # an empty value falls back
    assert (ROOT / "config" / "web.json").exists() and json.loads((ROOT / "config" / "web.json").read_text())["accent"] == "#F2B33D"
    from fart.version import RULE_FILES

    assert "config/web.json" not in RULE_FILES


# ----- the log summary ----------------------------------------------------------------------------------------------------
def test_logs_view_counts_the_tail_without_secrets(tmp_path):
    s = load_settings(root=tmp_path, env={})
    s.ensure_dirs()
    lines = [json.dumps({"ts": NOW - 300, "level": "INFO", "logger": "web", "event": "web.started"}),
             json.dumps({"ts": NOW - 200, "level": "WARNING", "logger": "web", "event": "web.login_failed", "ip": "203.0.113.7"}),
             json.dumps({"ts": NOW - 100, "level": "ERROR", "logger": "web", "event": "web.request_failed"}), "garbage",
             json.dumps({"ts": NOW - 50, "level": "INFO", "logger": "web", "event": "web.request"})]
    (s.logs_dir / "web.log").write_text("\n".join(lines) + "\n")
    v = data.logs_view(s, NOW)
    rows = {r["name"]: r for r in v["rows"]}
    assert set(rows) == set(data.LOG_NAMES)
    assert rows["web"]["warnings"] == 1 and rows["web"]["errors"] == 1 and rows["web"]["last_error"].startswith("web.request_failed (") and rows["web"]["_class"] == "errors"
    assert rows["web"]["last_event"] == "web.request" and rows["web"]["size_label"].endswith(" B") and "203.0.113.7" not in json.dumps(v)
    assert rows["ingest"]["exists"] is False and rows["ingest"]["modified_label"] == "nooit geschreven" and rows["ingest"]["warnings"] is None


# ----- deploy files and docs ----------------------------------------------------------------------------------------------
def test_deploy_files_and_docs_for_milestone_9():
    caddy = (ROOT / "deploy" / "caddy" / "fart.caddy").read_text()
    site_line = [l for l in caddy.splitlines() if l.strip().endswith("{") and not l.strip().startswith(("#", "header", "request_body"))][0]
    assert site_line.strip() == "kingdegen.dev {" and "www.kingdegen.dev" in caddy   # named only in the comment that says why it is absent
    env = {}
    for line in (ROOT / ".env.example").read_text().splitlines():
        st = line.strip()
        if st and not st.startswith("#") and "=" in st:
            k, v = st.split("=", 1)
            env[k.strip()] = v.strip()
    assert env["FART_WEB_PUBLIC"] == "1"
    deploy_md = (ROOT / "deploy" / "DEPLOY.md").read_text()
    for needle in ("## 5h. Milestone 9: website v2", "FART_WEB_PUBLIC", "config/web.json", "curl -s https://kingdegen.dev/api/public/status", "/api/public/status",
                   "www.kingdegen.dev", "/eigenaar", "curl -sI https://kingdegen.dev/", "FART_WEB_PUBLIC=0"):
        assert needle in deploy_md, needle
    decisions = (ROOT / "docs" / "DECISIONS.md").read_text()
    assert "## Milestone 9 (26 oktober 2026)" in decisions and "openbaar dashboard, eigenaarssectie achter login" in decisions
    readme = (ROOT / "README.md").read_text()
    assert "milestone 9" in readme.lower() and "/eigenaar" in readme
    web_cfg = json.loads((ROOT / "config" / "web.json").read_text())
    assert {"site_name", "product", "tagline", "domain", "accent", "refresh_s"} <= set(web_cfg) and web_cfg["site_name"] == "KingDegen"
    icon = (ROOT / "fart" / "web" / "static" / "icon.svg").read_text()
    assert icon.startswith("<svg") and "script" not in icon
    pyproject = (ROOT / "pyproject.toml").read_text()
    assert 'fart = ["web/static/*"]' in pyproject


def test_public_views_without_a_database(tmp_path):
    """No database and no reports: every public view says so instead of showing zeros, and the pages still render."""
    s = load_settings(root=tmp_path, env={})
    s.ensure_dirs()
    (tmp_path / ".env").write_text(f"FART_WEB_PASSWORD_HASH={auth.hash_password('x' * 12)}\nFART_WEB_SECRET={auth.new_secret()}\n")
    s = load_settings(root=tmp_path, env={})
    st = public.status_view(None, s, NOW)
    assert st["db_exists"] is False and st["freshness"]["status"] == "unknown" and st["live"]["observations_today"] is None and "nog geen gegevens" in st["headline"]
    cv = public.coverage_view(None, NOW)
    assert cv["rows"] == [] and cv["summary"]["hours_not_covered"] is None
    assert public.observations_view(None, s, NOW)["rows"] == [] and public.today_view(None, s, NOW)["counts"]["total_today"] is None
    assert public.reports_view(s, NOW)["rows"] == [] and public.week_view(s, NOW)["found"] is False and len(public.week_view(s, NOW)["criteria"]) == 6
    assert public.day_report_view(s, "2026-10-25", NOW) is None and public.week_report_view(s, "week-2026-10-25", NOW) is None
    app = server.App(s, clock=lambda: NOW)
    app.refresh_views()
    for path in PUBLIC_PAGES + PUBLIC_API:
        r = app.handle(server.Request("GET", path, {}, {}, b"", "127.0.0.1"))
        assert r.status == 200, path
        text = r.body.decode()
        assert "0%" not in text and "Nog geen rapporten" in text or path != "/rapporten", path
    html = app.handle(server.Request("GET", "/", {}, {}, b"", "127.0.0.1")).body.decode()
    assert "De radar heeft nog geen gegevens" in html and "Vandaag nog geen observaties" in html and "Er zijn nog geen gegevens" in html
    assert 'class="stat-value is-empty">–<' in html and "0 SOL" not in html
    html = app.handle(server.Request("GET", "/meting", {}, {}, b"", "127.0.0.1")).body.decode()
    assert "Nog geen rapporten" in html and "nog geen gegevens" in html.lower()
