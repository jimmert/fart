"""Server-rendered HTML of the website (milestone 8): Dutch, plain language, times in Europe/Amsterdam with UTC in
brackets, the banner on every page. Every value comes from the same JSON the API serves (`fart/web/data.py`); an
element carries `data-src` and `data-path` so `static/app.js` can refresh it from the endpoint every 60 s with the
same rules (`fmt`: None is "–", True is "ja", False is "nee"). No inline script or style anywhere (the CSP forbids
them); nothing on any page changes anything."""
from __future__ import annotations

import html
import json
from typing import Any

BANNER = "Observatie, geen advies. Alles wordt achteraf gemeten."
SITE = "kingdegen.dev"
NAV = (("/", "Dashboard"), ("/observaties", "Observaties"), ("/rapporten", "Rapporten"), ("/instellingen", "Instellingen"))

Col = tuple[str, str] | tuple[str, str, str]   # (key, header[, css class])


def esc(v: Any) -> str:
    return html.escape("" if v is None else str(v), quote=True)


def get_path(obj: Any, path: str) -> Any:
    cur = obj
    for part in (path.split(".") if path else []):
        if cur is None:
            return None
        if isinstance(cur, list):
            try:
                cur = cur[int(part)]
            except (ValueError, IndexError):
                return None
        elif isinstance(cur, dict):
            cur = cur.get(part)
        else:
            return None
    return cur


def fmt(v: Any) -> str:
    """The same rules as app.js: None/empty is an en dash, booleans ja/nee, lists joined."""
    if v is None or v == "":
        return "–"
    if v is True:
        return "ja"
    if v is False:
        return "nee"
    if isinstance(v, list):
        return ", ".join(str(x) for x in v)
    if isinstance(v, dict):
        return json.dumps(v, ensure_ascii=False)
    return str(v)


def scalar(src: str, path: str, data: Any, tag: str = "span", cls: str = "") -> str:
    c = f' class="{esc(cls)}"' if cls else ""
    return f'<{tag}{c} data-src="{esc(src)}" data-path="{esc(path)}">{esc(fmt(get_path(data, path)))}</{tag}>'


def table(src: str, path: str, data: Any, cols: list[Col], empty: str = "geen", cls: str = "") -> str:
    rows = get_path(data, path) or []
    spec = json.dumps([[c[0], c[1], c[2] if len(c) > 2 else ""] for c in cols], ensure_ascii=False)
    head = "".join(f'<th{" class=" + chr(34) + esc(c[2]) + chr(34) if len(c) > 2 and c[2] else ""}>{esc(c[1])}</th>' for c in cols)
    body = []
    if not rows:
        body.append(f'<tr><td colspan="{len(cols)}" class="muted">{esc(empty)}</td></tr>')
    for r in rows:
        rc = f' class="{esc(r.get("_class"))}"' if isinstance(r, dict) and r.get("_class") else ""
        cells = []
        for c in cols:
            cc = f' class="{esc(c[2])}"' if len(c) > 2 and c[2] else ""
            cells.append(f"<td{cc}>{esc(fmt(get_path(r, c[0])))}</td>")
        body.append(f"<tr{rc}>{''.join(cells)}</tr>")
    tc = f' class="{esc(cls)}"' if cls else ""
    return (f'<table{tc}><thead><tr>{head}</tr></thead><tbody data-src="{esc(src)}" data-path="{esc(path)}" data-kind="rows" '
            f"data-cols='{esc(spec)}' data-empty=\"{esc(empty)}\">{''.join(body)}</tbody></table>")


def kv(items: list[tuple[str, str]]) -> str:
    """A definition list of (label, html) pairs; the html is already escaped by the caller."""
    return "<dl class=\"kv\">" + "".join(f"<dt>{esc(k)}</dt><dd>{v}</dd>" for k, v in items) + "</dl>"


def layout(title: str, body: str, *, active: str = "", logged_in: bool = False, logout_csrf: str = "", footer: str = "",
           script: bool = False) -> str:
    nav = "".join(f'<a href="{esc(href)}"{" class=" + chr(34) + "active" + chr(34) if href == active else ""}>{esc(label)}</a>' for href, label in NAV)
    logout = ""
    if logged_in:
        logout = (f'<form method="post" action="/logout"><input type="hidden" name="csrf" value="{esc(logout_csrf)}">'
                  f'<button type="submit">Uitloggen</button></form>')
    head_nav = f'<a class="brand" href="/">fart · {esc(SITE)}</a>' + (f"<nav>{nav}</nav>{logout}" if logged_in else "")
    js = '<script src="/static/app.js" defer></script>' if script else ""
    return (
        '<!DOCTYPE html><html lang="nl"><head><meta charset="utf-8">'
        f"<title>{esc(title)} · fart</title>"
        '<meta name="viewport" content="width=device-width, initial-scale=1"><meta name="referrer" content="no-referrer">'
        '<meta name="robots" content="noindex, nofollow"><link rel="stylesheet" href="/static/app.css">'
        f"{js}</head><body>"
        f'<header class="top">{head_nav}</header>'
        f'<div class="banner">{esc(BANNER)}</div>'
        f"<main>{body}</main>"
        f"<footer>{footer}</footer></body></html>"
    )


# ----- pages ----------------------------------------------------------------------------------------------------------
def login_page(csrf: str, error: str | None = None) -> str:
    err = f'<p class="error">{esc(error)}</p>' if error else ""
    body = (
        '<div class="login"><h1>Inloggen</h1>'
        "<p>Deze site toont de operationele gegevens van fart en vraagt daarom een wachtwoord. Alles is alleen-lezen.</p>"
        f"{err}"
        f'<form method="post" action="/login"><input type="hidden" name="csrf" value="{esc(csrf)}">'
        '<p><label for="password">Wachtwoord</label><br><input type="password" id="password" name="password" autocomplete="current-password" required autofocus></p>'
        '<p><button class="primary" type="submit">Inloggen</button></p></form></div>'
    )
    return body


DAEMON_COLS: list[Col] = [("label", "onderdeel"), ("last", "laatst gezien"), ("age", "leeftijd", "num"), ("status_label", "status", "status")]
COVERAGE_COLS: list[Col] = [("hour_label", "uur"), ("creates_pp", "creates PP", "num"), ("creates_rpc", "creates RPC", "num"), ("creates_union", "unie", "num"),
                            ("grads", "graduaties", "num"), ("grads_without_create", "zonder create", "num"), ("gap_s", "gat (s)", "num"), ("gaps_label", "gaten"),
                            ("covered_label", "gedekt"), ("complete_label", "compleet")]
PLAN_COLS: list[Col] = [("kind", "soort"), ("planned_label", "gepland", "num"), ("actual_label", "werkelijk", "num"), ("planned_share_label", "gepland, % plafond", "num"),
                        ("actual_share_label", "werkelijk, % plafond", "num")]
KEY_COLS: list[Col] = [("key", "sleutel"), ("kind", "soort"), ("started_label", "gestart"), ("counts_n", "tellingen", "num"), ("last_n_1h", "laatste uur", "num"),
                       ("peak_n_1h", "piek per uur", "num"), ("confirmed_label", "bevestigd"), ("search_pending_label", "zoekopdracht wacht"), ("next_due_label", "volgende telling"),
                       ("usd_label", "kosten", "num")]
STOPPED_COLS: list[Col] = [("reason_label", "stopreden"), ("n", "sleutels", "num"), ("usd_label", "kosten", "num"), ("counts", "tellingen", "num"), ("searches", "zoekopdrachten", "num")]
REPORT_COLS: list[Col] = [("label", "kernpunt"), ("value", "waarde", "num"), ("note", "toelichting")]
GATE_COLS: list[Col] = [("label", "criterium"), ("value_label", "waarde", "num"), ("threshold_label", "drempel", "num"), ("status", "toets", "status")]
DECISION_COLS: list[Col] = [("as_of_label", "tijd"), ("kind_label", "soort"), ("subject", "munt / thema"), ("name", "naam"), ("outcome_label", "uitkomst"),
                            ("peak_label", "piek mcap (USD)", "num"), ("rules_version", "rules_version"), ("reasons_label", "redenen"), ("features_label", "features", "features")]
SPEND_COLS: list[Col] = [("kind", "soort"), ("n", "eenheden", "num"), ("usd_label", "USD", "num")]


def dashboard(v: dict[str, Any]) -> str:
    st, cv, x, rp, wk = v["status"], v["coverage"], v["x"], v["report"], v["week"]
    note = f'<p class="notice">{esc(st["note"])}</p>' if st.get("note") else ""
    s = (lambda path, src="status": scalar(src, path, v[src]))  # noqa: E731
    health = (
        '<section class="card"><h2>Gezondheid en versheid</h2>'
        + table("status", "daemons", st, DAEMON_COLS, empty="geen database")
        + '<p class="muted">Open gaten in de stromen: ' + s("open_gaps_label") + " · mislukkingen (tellers): " + s("failures_label")
        + " · schema " + s("schema_version") + "</p></section>"
    )
    decisions = (
        '<section class="card"><h2>Beslissingen vandaag (UTC-dag ' + s("decisions_today.day") + ")</h2>"
        + kv([("Munt-observaties", s("decisions_today.observe")), ("Thema-observaties", s("decisions_today.observe_meta")),
              ("Samen", s("decisions_today.total")), ("Laatste beslissing", s("decisions_today.last")),
              ("Watchlist actief", s("watchlist_active")), ("Beslissingen in totaal", s("counts.decisions"))])
        + '<p class="muted"><a href="/observaties">De laatste 200 beslissingen met features en uitkomst</a></p></section>'
    )
    budgets = (
        '<section class="card"><h2>Budgetten vandaag</h2>'
        + kv([("CU (schatting) tegenover plafond", s("budgets.rpc.cu_today_label") + " van " + s("budgets.rpc.cap_label") + " (" + s("budgets.rpc.share_label") + ")"),
              ("RPC-aanroepen geblokkeerd door een plafond", s("budgets.rpc.blocked_today")),
              ("X tegenover plafond", s("budgets.x.usd_today_label") + " van " + s("budgets.x.cap_label") + " (" + s("budgets.x.share_label") + ")"),
              ("X-aanroepen geweigerd door een plafond", s("budgets.x.blocked_today"))])
        + '<p class="muted">' + s("budgets.rpc.note") + "</p></section>"
    )
    xs = (lambda path: scalar("x", path, x))  # noqa: E731
    xcard = (
        '<section class="card"><h2>X-stand en planner</h2>'
        + kv([("Stand (effectief)", xs("mode.label")), ("X_MODE gevraagd", xs("mode.requested")), ("Opmerking", xs("mode.note")),
              ("Daemons melden (social / engine)", xs("mode.social_daemon") + " / " + xs("mode.engine_daemon")),
              ("Uitgegeven vandaag", xs("budget.spend_label") + " van " + xs("budget.cap_label") + " (" + xs("budget.share_label") + ")"),
              ("Actieve sleutels", xs("planner.active_total") + " (" + xs("planner.active_label") + ")"),
              ("Zoekopdracht wacht / telling nu due", xs("planner.search_pending") + " / " + xs("planner.due_now")),
              ("Gestart vandaag", xs("planner.started_today")), ("Gestopt vandaag", xs("planner.stopped_label")),
              ("Uitgesteld vandaag (envelop)", xs("planner.deferred_label")),
              ("Schaduwbeslissingen vandaag / totaal", xs("shadow.decisions_today") + " / " + xs("shadow.decisions_total")),
              ("Schaduwtellingen vandaag (bevestigd 1 u / 24 u)", xs("shadow.confirmations_today") + " (" + xs("shadow.confirmed_1h_today") + " / " + xs("shadow.confirmed_24h_today") + ")")])
        + "<h3>Plan tegenover uitgaven (per soort, vandaag)</h3>" + table("x", "plan", x, PLAN_COLS, empty="geen plan")
        + '<p class="muted">planfout: ' + xs("plan_error") + "</p>"
        + "<h3>Actieve sleutels</h3>" + table("x", "active_keys", x, KEY_COLS, empty="geen actieve sleutels")
        + "<h3>Gestopt vandaag per reden</h3>" + table("x", "stopped_rows", x, STOPPED_COLS, empty="geen sleutel gestopt vandaag")
        + "<h3>Uitgaven vandaag per soort</h3>" + table("x", "spend_by_kind", x, SPEND_COLS, empty="geen uitgaven")
        + "</section>"
    )
    cvs = (lambda path: scalar("coverage", path, cv))  # noqa: E731
    coverage = (
        '<section class="card"><h2>Dekking van de laatste 24 uur</h2>'
        + '<p class="muted">Gedekte uren: ' + cvs("summary.hours_covered") + " · complete uren: " + cvs("summary.hours_complete")
        + " · NIET GEDEKT: " + cvs("summary.hours_not_covered") + " (een uur zonder actieve bron blijft leeg: wij waren er niet om te tellen)</p>"
        + table("coverage", "rows", cv, COVERAGE_COLS, empty="geen database")
        + "</section>"
    )
    rs = (lambda path: scalar("report", path, rp))  # noqa: E731
    report = (
        '<section class="card"><h2>Laatste dagrapport</h2>'
        + kv([("Dag", rs("day_label")), ("Status", rs("status_label")), ("Gegenereerd", rs("report_generated_label")),
              ("rules_version / method_version", rs("rules_version") + " / " + rs("method_version"))])
        + '<p class="muted">' + rs("note") + "</p>"
        + table("report", "items", rp, REPORT_COLS, empty="nog geen dagrapport")
        + '<p class="muted"><a href="/rapporten">Alle rapporten</a></p></section>'
    )
    ws = (lambda path: scalar("week", path, wk))  # noqa: E731
    week = (
        '<section class="card"><h2>Go/no-go van het laatste weekrapport (bevroren criteria)</h2>'
        + kv([("Venster", ws("window_label")), ("Segment", ws("segment_title")), ("Dagen", ws("segments.0.days_label")),
              ("Gegenereerd", ws("report_generated_label"))])
        + '<p class="verdict">' + ws("verdict") + "</p>"
        + table("week", "segments.0.criteria", wk, GATE_COLS, empty="nog geen weekrapport")
        + '<p class="muted">' + ws("segments.0.note") + "</p>"
        + '<p class="muted">' + ws("note") + "</p></section>"
    )
    body = ("<h1>Dashboard</h1>" + note + '<p id="refresh-state">Ververst elke 60 s</p>'
            + '<div class="grid">' + health + decisions + budgets + "</div>" + coverage + xcard + report + week)
    return body


def observations_page(v: dict[str, Any]) -> str:
    d = v["decisions"]
    note = f'<p class="notice">{esc(d["note"])}</p>' if d.get("note") else ""
    return ("<h1>Observaties</h1>" + note
            + '<p class="muted">De laatste ' + scalar("decisions", "limit", d) + " beslissingen, nieuwste eerst. " + esc(d.get("definition") or "") + "</p>"
            + '<p id="refresh-state">Ververst elke 60 s</p>'
            + '<section class="card">' + table("decisions", "rows", d, DECISION_COLS, empty="nog geen beslissingen") + "</section>")


def reports_page(files: list[dict[str, Any]], body_html: str | None = None, name: str | None = None) -> str:
    items = "".join(f'<li><a href="{esc(f["href"])}">{esc(f["name"])}</a> · {esc(f["kind_label"])} {esc(f["day_label"])} '
                    f'<span class="muted">(geschreven {esc(f["modified_label"])}, {str(f"{f["bytes"]:,}").replace(",", ".")} bytes)</span></li>' for f in files)
    listing = f"<ul>{items}</ul>" if files else '<p class="muted">Nog geen rapporten onder out/recall/ (het eerste dagrapport komt om 06:00 UTC).</p>'
    if body_html is None:
        return "<h1>Rapporten</h1><p>Dag- en weekrapporten zoals de timers ze schreven (out/recall/*.md), nieuwste eerst.</p>" + f'<section class="card">{listing}</section>'
    return (f'<p class="muted"><a href="/rapporten">← alle rapporten</a> · {esc(name)}</p>'
            f'<section class="card report">{body_html}</section>')


def settings_page(v: dict[str, Any]) -> str:
    parts = ["<h1>Instellingen (alleen-lezen)</h1>",
             "<p>Wat hier staat verandert alleen door een commit en een nieuwe uitrol; de site zelf kan niets wijzigen. Geheimen staan nooit op deze pagina.</p>"]
    for sec in v["sections"]:
        rows = "".join(f"<tr><th>{esc(r['label'])}</th><td>{esc(fmt(r['value']))}</td></tr>" for r in sec["rows"])
        parts.append(f'<section class="card"><h2>{esc(sec["title"])}</h2><table><tbody>{rows}</tbody></table></section>')
    return "".join(parts)


def error_page(status: int, message: str) -> str:
    return f"<h1>{status}</h1><p>{esc(message)}</p>"
