"""Server-rendered HTML of the website (milestone 9): Dutch, plain language, one renderer.

Every page is built here from the dicts of `fart/web/public.py` (the public pages) and `fart/web/data.py` (the owner
section). The page is the only renderer: `static/app.js` refreshes it by fetching the same page and swapping the
elements that carry `data-live`, so what the browser shows after a refresh is exactly what the server would have
rendered. No inline script or style anywhere (the Content-Security-Policy forbids them); nothing on any page changes
anything; every value from the chain or from a report is HTML-escaped.

Honesty on every card: a key figure, one line in plain words about what it means, a status pill (vers, oud, NIET
GEDEKT, onbekend, te weinig data, ...), and the technical words linked to the glossary on /methode.

Milestone 10: a view the background thread has not computed yet arrives as the placeholder `public.computing()`; every
card and page renders it as the same card with the pill "wordt berekend" and one sentence, nothing else changes.
"""
from __future__ import annotations

import html
import json
from typing import Any

from ..notify.format import n as fmt_n

BANNER = "Observatie, geen advies. Alles wordt achteraf gemeten."
NAV_PUBLIC = (("/", "Dashboard"), ("/observaties", "Observaties"), ("/meting", "Meting"), ("/rapporten", "Rapporten"), ("/methode", "Methode"), ("/tools", "Tools"))
OWNER_PATH = "/eigenaar"
GLOSSARY = {
    "graduatie": "Graduatie", "universum": "Universum", "observatie": "Observatie", "trefkans": "Trefkans", "recall": "Recall", "lead-time": "Lead time",
    "thema": "Thema (meta)", "niet-gedekt": "NIET GEDEKT", "winnaar": "Winnaar", "gebundeld": "Gebundeld", "bootstrapinterval": "Bootstrapinterval",
    "meetweek": "Meetweek", "regels": "Regels en vingerafdruk", "onbekend": "Onbekend en nog bezig", "compleet": "Gedekt en compleet",
}

Col = tuple[str, str] | tuple[str, str, str]   # (key, header[, css class])


def esc(v: Any) -> str:
    return html.escape("" if v is None else str(v), quote=True)


def fmt(v: Any) -> str:
    """None or empty is an en dash, booleans ja/nee, lists joined, numbers as they are (labels come pre-formatted)."""
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


# ----- small components -----------------------------------------------------------------------------------------------
def pill(kind: str, label: str | None = None, title: str | None = None) -> str:
    from .public import PILL

    text = label if label is not None else PILL.get(kind, kind)
    cls = {"not_covered": "nc", "not_winner": "not-winner"}.get(kind, kind)
    t = f' title="{esc(title)}"' if title else ""
    return f'<span class="pill pill-{esc(cls)}"{t}>{esc(text)}</span>'


def term(key: str, text: str | None = None) -> str:
    """A glossary link: dotted underline, anchor on /methode."""
    return f'<a class="glossary-link" href="/methode#begrip-{esc(key)}">{esc(text or GLOSSARY.get(key, key))}</a>'


def kpi(value: str, unit: str = "", label: str | None = None) -> str:
    empty = value in ("–", "", "onbekend", None)
    cls = ' class="kpi-value is-empty"' if empty else ' class="kpi-value"'
    lab = f'<span class="kpi-label">{esc(label)}</span>' if label else ""
    u = f'<span class="kpi-unit">{esc(unit)}</span>' if unit else ""
    return f'<div class="kpi">{lab}<span{cls}>{esc(value)}</span>{u}</div>'


def explain(text_html: str) -> str:
    """The "wat betekent dit" line; `text_html` is already escaped by the caller (it may carry glossary links)."""
    return f'<p class="explain">{text_html}</p>'


def card(title: str, body: str, *, pill_html: str = "", foot: str = "", cls: str = "", live: str | None = None, heading: str = "h2", anchor: str | None = None) -> str:
    live_attr = f' data-live="{esc(live)}"' if live else ""
    c = f" {esc(cls)}" if cls else ""
    a = f' id="{esc(anchor)}"' if anchor else ""
    f = f'<div class="card-foot">{foot}</div>' if foot else ""
    return (f'<section class="card{c}"{live_attr}{a}><div class="card-head"><{heading}>{esc(title)}</{heading}>{pill_html}</div>{body}{f}</section>')


def table(cols: list[Col], rows: list[dict[str, Any]], empty: str = "geen", cls: str = "stack", caption: str | None = None,
          widths: tuple[str, ...] | None = None) -> str:
    """A table with fixed columns (`widths`: a percentage per column, else equal), right-aligned numbers (class `num`), a
    stacked layout on narrow screens (each cell carries its header as `data-label`) and an empty-state row. Row class
    from `_class`."""
    head = "".join(f'<th{_cls(c)}>{esc(c[1])}</th>' for c in cols)
    colgroup = ("<colgroup>" + "".join(f'<col class="w-{esc(str(w).rstrip("%"))}">' for w in widths) + "</colgroup>") if widths else ""
    body = []
    if not rows:
        body.append(f'<tr class="empty"><td colspan="{len(cols)}">{esc(empty)}</td></tr>')
    for r in rows:
        rc = f' class="{esc(r.get("_class"))}"' if r.get("_class") else ""
        cells = []
        for c in cols:
            v = r.get(c[0])
            cells.append(f'<td{_cls(c)} data-label="{esc(c[1])}">{v if isinstance(v, Raw) else esc(fmt(v))}</td>')
        body.append(f"<tr{rc}>{''.join(cells)}</tr>")
    cap = f"<caption class=\"sr-only\">{esc(caption)}</caption>" if caption else ""
    return f'<div class="table-wrap"><table class="table {esc(cls)}">{cap}{colgroup}<thead><tr>{head}</tr></thead><tbody>{"".join(body)}</tbody></table></div>'


class Raw(str):
    """A cell value that is already HTML (escaped by the producer)."""


def _cls(c: Col) -> str:
    return f' class="{esc(c[2])}"' if len(c) > 2 and c[2] else ""


def kv(items: list[tuple[str, str]]) -> str:
    """A definition list of (label, html) pairs; the html is already escaped by the caller."""
    return '<dl class="kv">' + "".join(f"<dt>{esc(k)}</dt><dd>{v}</dd>" for k, v in items) + "</dl>"


def empty_state(text: str) -> str:
    return f'<p class="empty-state">{esc(text)}</p>'


def is_computing(v: Any) -> bool:
    return isinstance(v, dict) and v.get("state") == "computing"


COMPUTING_TEXT = "Dit onderdeel wordt nu berekend; binnen een halve minuut staat het hier. Tot die tijd staat er geen cijfer."


def computing_card(title: str, live: str | None = None, foot: str = "", note: str | None = None, cls: str = "") -> str:
    """The card of a view that is still being computed: the same title and region, the pill, one sentence, no number."""
    return card(title, explain(esc(COMPUTING_TEXT)) + empty_state(note or "wordt berekend …"), pill_html=pill("computing"), live=live, foot=foot, cls=cls)


def computing_page(title: str, name: str = "") -> str:
    head = (f'<section class="hero"><p class="eyebrow">{esc(title)}</p><h1>{esc(name) if name else esc(title)}</h1>'
            f'<div class="hero-meta">{pill("computing")}<span>{esc(COMPUTING_TEXT)}</span></div></section>')
    return head + f'<div class="section">{computing_card(title)}</div>'


MARK = ('<svg class="mark" viewBox="0 0 28 28" aria-hidden="true" focusable="false"><circle class="ring" cx="14" cy="14" r="11"/>'
        '<path class="crown" d="M8 19 L9.5 11 L13 14.5 L14 9 L15 14.5 L18.5 11 L20 19 Z"/></svg>')


# ----- layout -----------------------------------------------------------------------------------------------------------
def layout(ctx: dict[str, Any], title: str, body: str, *, active: str = "", script: bool = True, robots: str | None = None, description: str | None = None,
           refresh_s: int | None = None) -> str:
    """`ctx`: site (name, product, tagline, domain, refresh_s), public (bool), logged_in (bool), logout_csrf, footer."""
    site = ctx["site"]
    refresh = int(refresh_s or site.get("refresh_s", 30))
    name = f"{site['name']} {site['product']}"
    nav_items = list(NAV_PUBLIC) if (ctx.get("public") or ctx.get("logged_in")) else []
    nav = "".join(f'<a href="{esc(href)}"{" class=" + chr(34) + "active" + chr(34) if href == active else ""}>{esc(label)}</a>' for href, label in nav_items)
    if ctx.get("logged_in"):
        nav += (f'<a href="{OWNER_PATH}" class="owner-link{" active" if active == OWNER_PATH else ""}">Eigenaar</a>'
                f'<form method="post" action="/logout"><input type="hidden" name="csrf" value="{esc(ctx.get("logout_csrf", ""))}">'
                f'<button type="submit">Uitloggen</button></form>')
    elif ctx.get("public"):
        nav += '<a href="/login" class="owner-link">Eigenaar</a>'
    js = '<script src="/static/app.js" defer></script>' if script else ""
    desc = description or site["tagline"]
    rob = f'<meta name="robots" content="{esc(robots)}">' if robots else ""
    return (
        '<!DOCTYPE html><html lang="nl"><head><meta charset="utf-8">'
        f"<title>{esc(title)} · {esc(name)}</title>"
        '<meta name="viewport" content="width=device-width, initial-scale=1"><meta name="referrer" content="no-referrer">'
        f'<meta name="description" content="{esc(desc)}">{rob}<meta name="theme-color" content="#0E1117">'
        '<link rel="icon" href="/static/icon.svg" type="image/svg+xml"><link rel="stylesheet" href="/static/app.css">'
        f'{js}</head><body data-refresh-ms="{refresh * 1000}">'
        '<a class="skip-link" href="#inhoud">Naar de inhoud</a>'
        f'<header class="site-head"><div class="inner"><a class="brand" href="/">{MARK}<span>{esc(site["name"])}</span><span class="product">{esc(site["product"])}</span></a>'
        f'<nav class="site-nav" aria-label="Hoofdmenu">{nav}</nav></div>'
        f'<p class="banner">{esc(ctx.get("banner") or BANNER)}</p></header>'
        f'<main id="inhoud" class="page">{body}</main>'
        f'<footer class="site-foot"><div class="inner"><div class="foot-row"><span>{esc(site["name"])} {esc(site["product"])} · {esc(site["domain"])}</span>'
        f'<span>motor: fart</span><span>{ctx.get("footer", "")}</span></div>'
        '<div class="foot-row"><span>Observatie, geen advies. Niets op deze site is een aanbeveling om iets te kopen of te verkopen.</span>'
        f'<a href="/methode#disclaimer">volledige disclaimer</a></div><div class="foot-row"><span id="refresh-state">Ververst elke {refresh} s zolang dit tabblad open staat.</span></div></div></footer>'
        "</body></html>"
    )


# ----- the live dashboard ------------------------------------------------------------------------------------------------
def dashboard(ctx: dict[str, Any], v: dict[str, Any]) -> str:
    """`/`: what the radar sees right now, as processed data: the tempo, where the money goes, the newest coins, the
    themes that come up, the latest graduations, what goes round on X, today's observations, and one line of measurement."""
    live = v["live"]
    site = ctx["site"]
    if is_computing(live):
        hero = (f'<section class="hero" data-live="hero"><p class="eyebrow">{esc(site["name"])} {esc(site["product"])} · live</p>'
                f'<h1>Wat de radar nu ziet</h1><p class="lede">{esc(COMPUTING_TEXT)}</p>'
                f'<div class="hero-meta">{pill("computing")}<span>{esc(live.get("note") or "")}</span></div></section>')
        return _dashboard_body(hero, v, str(ctx.get("warning") or ""))
    fr = live["freshness"]
    c, w, g = live["creates"], live["watched"], live["graduations"]
    stats = (
        _stat("nieuwe munten per minuut", c["per_min_label"], None, f"{fmt_n(c['last_10m'])} in de laatste 10 min" if c["last_10m"] is not None else "uur nog niet gedekt")
        + _stat("kopers, laatste 5 min", w["buyers_label"], None, f"op {fmt_n(w['active'])} gevolgde munten" if w["active"] is not None else "geen gegevens")
        + _stat("netto instroom, 5 min", w["net_sol_label"] if w["net_sol_5m"] is not None else "–", None, f"{fmt_n(w['trades_5m'])} trades, {fmt_n(w['with_trades_5m'])} munten" if w["trades_5m"] is not None else "geen gegevens")
        + _stat("graduaties, laatste uur", g["last_hour_label"], None, f"vandaag {g['today_label']}" if g["today"] is not None else "uur nog niet gedekt")
    )
    sol = live.get("sol_usd") or {}
    sol_note = f"SOL {sol['price_label']} ({sol['age_label']} oud)" if sol.get("known") else "SOL-prijs onbekend: bedragen alleen in SOL"
    hero = (
        f'<section class="hero" data-live="hero"><p class="eyebrow">{esc(site["name"])} {esc(site["product"])} · live</p>'
        f'<h1>Wat de radar nu ziet</h1>'
        f'<p class="lede">{_live_headline_html(live)}</p>'
        f'<div class="hero-meta">{pill(fr["status"], fr["label"], fr.get("note"))}<span>{esc(fr["note"])}</span>'
        f'<span>gegevens van {esc(live["generated_label"])}</span><span>ververst elke {int(live.get("refresh_s", 15))} s</span><span>{esc(sol_note)}</span></div>'
        f'<div class="hero-stats hero-stats-4">{stats}</div></section>'
    )
    return _dashboard_body(hero, v, live["warning"])


def _dashboard_body(hero: str, v: dict[str, Any], warning: str) -> str:
    hot = hot_card(v["hot"])
    new = new_coins_card(v["new"])
    themes = themes_card(v["themes"])
    grads = graduations_card(v["graduations"])
    social = social_card(v["social"])
    today = today_card(v["today"], compact=True)
    meting = measurement_strip(v["week"], v["coverage"])
    warn = warning_block(warning) if warning else ""
    return (hero + f'<div class="section">{hot}</div><div class="grid grid-2 section">{new}{themes}</div><div class="section">{grads}</div>'
            f'<div class="grid grid-2 section">{social}{today}</div><div class="section">{meting}</div><div class="section">{warn}</div>')


def _live_headline_html(live: dict[str, Any]) -> str:
    fr = live["freshness"]
    if fr["status"] == "unknown":
        return esc(live["headline"])
    c, w, g = live["creates"], live["watched"], live["graduations"]
    lead = "Nu" if fr["status"] == "fresh" else f"Bij het laatste levensteken ({esc(fr['age_label'])} geleden)"
    tempo = f"<strong>{esc(c['per_min_label'])}</strong> nieuwe munten per minuut" if c["per_min"] is not None else "een tempo dat nog niet gedekt is"
    if w["active"]:
        money = (f"op de {esc(fmt_n(w['active']))} gevolgde munten <strong>{esc(w['buyers_label'])}</strong> {'koper' if w['buyers_5m'] == 1 else 'kopers'} en "
                 f"<strong>{esc(w['net_sol_label'])}</strong> netto instroom in 5 minuten")
    else:
        money = "nog geen gevolgde munten"
    grads = (f"<strong>{esc(g['last_hour_label'])}</strong> {'graduatie' if g['last_hour'] == 1 else 'graduaties'} in het laatste uur"
             if g["last_hour"] is not None else "het aantal graduaties van dit uur nog niet gedekt")
    return f"{lead}: {tempo}, {money}, {grads}."


def mini_bar(share: int | float | None) -> str:
    """A tiny inline bar (0 to 100) drawn as SVG so no inline style is needed."""
    w = max(0, min(100, int(share or 0)))
    return f'<svg class="mini" viewBox="0 0 100 6" preserveAspectRatio="none" aria-hidden="true" focusable="false"><rect x="0" y="0" width="{w}" height="6" rx="1"/></svg>'


HOT_COLS: list[Col] = [("coin", "munt", "wide"), ("age_label", "leeftijd"), ("buyers_5m", "kopers", "num"), ("inflow", "netto instroom", "num wide"),
                       ("trades", "trades", "num"), ("mcap", "mcap", "num"), ("largest_share_label", "grootste koper", "num"), ("status", "status")]


def hot_card(h: dict[str, Any]) -> str:
    if is_computing(h):
        return computing_card("Waar het geld nu heen gaat", live="hot")
    rows = []
    for r in h.get("rows") or []:
        rows.append({
            "coin": Raw(f'<span class="coin-title">{esc(r["title"])}</span><code class="coin-mint" title="{esc(r["mint"])}">{esc(r["mint_short"])}</code>'),
            "age_label": r["age_label"], "buyers_5m": fmt_n(r["buyers_5m"]),
            "inflow": Raw(f'<span class="num-bar"><span>{esc(r["net_sol_label"])}</span>{mini_bar(r["share_of_top"])}</span>'),
            "trades": Raw(f'<span class="stack-2"><span>{esc(fmt_n(r["trades_5m"]))}</span><span class="caption">{esc(fmt_n(r["buys_5m"]))}× gekocht · {esc(fmt_n(r["sells_5m"]))}× verkocht</span></span>'),
            "mcap": Raw(f'<span class="stack-2"><span>{esc(r["mcap_sol_label"])}</span><span class="caption">{esc("≈ " + r["mcap_usd_label"] if r["mcap_usd"] is not None else "USD onbekend")}</span></span>'),
            "largest_share_label": r["largest_share_label"],
            "status": Raw(pill({"graduated": "complete", "observed": "winner", "new": "pending", "watched": "unknown"}[r["status"]], r["status_label"], r.get("reason_label"))),
            "_class": "is-observed" if r["status"] == "observed" else "",
        })
    body = table(HOT_COLS, rows, empty=h.get("note") or "geen trades op dit moment", widths=("25", "10", "9", "17", "11", "11", "9", "8"))
    head_pill = pill("fresh", f"{fmt_n(h.get('with_trades', 0))} van {fmt_n(h.get('watched', 0))} gevolgde munten actief") if h.get("rows") else pill("unknown", "stil")
    ex = explain(esc(h.get("definition") or "") + f" Kopers, instroom en trades over de laatste 5 minuten; grootste koper over 15 minuten. "
                 f"Een hoge instroom is een {term('observatie', 'waarneming')}, geen oordeel: de meeste munten vallen daarna terug.")
    return card("Waar het geld nu heen gaat", ex + body, pill_html=head_pill, live="hot", foot='<a href="/methode#begrip-observatie">Wat de radar volgt en waarom</a>')


def new_coins_card(n: dict[str, Any]) -> str:
    if is_computing(n):
        return computing_card("Nieuwste munten", live="nieuw")
    rate = n.get("rate") or {}
    items = []
    for r in n.get("rows") or []:
        tag = pill("winner", "observatie") if r.get("observed") else (pill("pending", "gevolgd") if r.get("watched") else "")
        items.append(f'<li class="tick"><time class="tick-time" datetime="{esc(r["created_at"])}">{esc(r["clock"])}</time>'
                     f'<span class="tick-body"><span class="tick-title">{esc(r["title"])}</span>'
                     f'<span class="tick-meta">{esc(r["age_label"])} oud · dev-buy {esc(r["dev_buy_label"])} · start {esc(r["initial_mcap_label"])} · maker eerder: {esc(r["creator_prior_label"])}</span></span>{tag}</li>')
    body = f'<ol class="ticker">{"".join(items)}</ol>' if items else empty_state(n.get("note") or "Nog geen munten gezien.")
    head_pill = pill("fresh", f"{rate.get('per_min_label', '–')} per minuut") if rate.get("per_min") is not None else pill("unknown", "tempo onbekend")
    ex = explain(esc(n.get("definition") or "") + " Een nieuwe munt is nog geen observatie; de radar kijkt de eerste 5 minuten mee.")
    foot = f'<span class="muted">vandaag {esc(fmt_n(n.get("today")))} nieuwe munten</span>' if n.get("today") is not None else ""
    return card("Nieuwste munten", ex + body, pill_html=head_pill, live="nieuw", foot=foot)


def themes_card(t: dict[str, Any]) -> str:
    if is_computing(t):
        return computing_card("Thema’s die nu opkomen", live="themas")
    items = []
    for r in t.get("rows") or []:
        tags = pill("winner" if r["bursting"] else "unknown", r["status_label"])
        if r.get("observed_today"):
            tags += pill("winner", "observatie vandaag")
        ex = ", ".join(r.get("examples") or [])
        items.append(f'<li class="theme"><div class="theme-head"><span class="theme-term">{esc(r["term"])}</span>{tags}</div>'
                     f'<div class="theme-meta">{esc(fmt_n(r["n_coins"]))} munten van {esc(fmt_n(r["distinct_creators"]))} makers · instroom {esc(r["sol_inflow_label"])} · '
                     f'{esc(fmt_n(r["unique_buyers"]))} kopers · {esc(r["burst_label"])}</div>'
                     + (f'<div class="caption">bijv. {esc(ex)}</div>' if ex else "") + "</li>")
    body = f'<ol class="themes">{"".join(items)}</ol>' if items else empty_state(t.get("note") or "Geen thema op dit moment.")
    if t.get("state") in ("stale", "missing"):
        # milestone 10: the engine's snapshot is old or absent: the card says so with the age and shows no figure
        head_pill = pill("unknown", "nu niet berekend")
    else:
        head_pill = pill("winner", f"{fmt_n(t.get('bursting', 0))} opkomend") if t.get("bursting") else pill("unknown", "niets opkomend")
    ex = explain(esc(t.get("definition") or "") + f" Een {term('thema')} zegt iets over de markt, niet over één munt.")
    foot = '<a href="/methode#begrip-thema">Hoe een thema geteld wordt</a>'
    if t.get("computed_label") and t.get("state") == "fresh":
        foot = f'<span class="muted">berekend door de engine {esc(t["computed_label"])} ({esc(t.get("age_label") or "–")} geleden)</span>' + foot
    return card("Thema’s die nu opkomen", ex + body, pill_html=head_pill, live="themas", foot=foot)


GRAD_COLS: list[Col] = [("coin", "munt", "wide"), ("when", "gegradueerd"), ("age_at_graduation_label", "na", "num"), ("mcap", "mcap nu", "num"),
                        ("volume", "volume 24 u", "num"), ("observed_label", "vooraf gezien"), ("outcome", "uitkomst")]


def graduations_card(g: dict[str, Any]) -> str:
    if is_computing(g):
        return computing_card("Net gegradueerd", live="graduaties")
    rows = []
    for r in g.get("rows") or []:
        rows.append({
            "coin": Raw(f'<span class="coin-title">{esc(r["title"])}</span><code class="coin-mint" title="{esc(r["mint"])}">{esc(r["mint_short"])}</code>'),
            "when": Raw(f'<span class="stack-2"><span>{esc(r["clock"])}</span><span class="caption">{esc(r["age_label"])} geleden</span></span>'),
            "age_at_graduation_label": r["age_at_graduation_label"],
            "mcap": Raw(f'<span class="stack-2"><span>{esc(r["mcap_label"])}</span><span class="caption">{esc(r["sample_age_label"] if r["sample_age_label"].startswith("geen") else "sample " + r["sample_age_label"])}</span></span>'),
            "volume": r["volume_label"], "observed_label": r["observed_label"],
            "outcome": Raw(pill(r["outcome"], r["outcome_label"])), "_class": "is-winner" if r["outcome"] == "winner" else "",
        })
    body = table(GRAD_COLS, rows, empty=g.get("note") or "nog geen graduaties", widths=("26", "12", "9", "17", "12", "12", "12"))
    head_pill = pill("fresh", f"{fmt_n(g['last_hour'])} in het laatste uur") if g.get("last_hour") is not None else pill("unknown", "uur nog niet gedekt")
    ex = explain(esc(g.get("definition") or "") + f" De uitkomst volgt de {term('winnaar', 'winnaarsdefinitie')}: onbekend blijft onbekend, nog bezig is nog bezig.")
    foot = f'<span class="muted">vandaag {esc(fmt_n(g.get("today")))} graduaties</span><a href="/observaties">Observaties met uitkomst</a>' if g.get("today") is not None else ""
    return card("Net gegradueerd", ex + body, pill_html=head_pill, live="graduaties", foot=foot)


def social_card(sv: dict[str, Any]) -> str:
    if is_computing(sv):
        return computing_card("Wat rondgaat op X", live="sociaal")
    if not sv.get("active"):
        body = empty_state(sv.get("note") or "De X-laag staat uit.")
        head_pill = pill("unknown", "staat uit")
    else:
        head_pill = pill("pending" if sv["mode"] == "shadow" else "fresh", sv["mode_label"])
        parts = []
        keys = sv.get("keys") or []
        if keys:
            rows = [{"title": Raw(f'<span class="coin-title">{esc(k["title"])}</span>' + (f'<code class="coin-mint">{esc(k["key"])}</code>' if k.get("mint_short") else "")),
                     "kind_label": k["kind_label"], "last_n_1h_label": k["last_n_1h_label"], "peak_n_1h": fmt_n(k["peak_n_1h"]) if k["peak_n_1h"] is not None else "–",
                     "confirmed": Raw(pill("winner", "bevestigd") if k["confirmed"] else pill("unknown", "nog niet")), "_class": ""} for k in keys]
            parts.append("<h3>Meest genoemd, laatste uur</h3>" + table([("title", "munt of thema", "wide"), ("kind_label", "soort"), ("last_n_1h_label", "laatste uur", "num"),
                                                                       ("peak_n_1h", "piek per uur", "num"), ("confirmed", "bevestigd")], rows, widths=("40", "15", "15", "15", "15")))
        posts = sv.get("fast_posts") or []
        if posts:
            items = "".join(f'<li class="post"><div class="post-head"><span class="post-author">@{esc(p["author"])}</span><span class="caption">{esc(p["created_label"])} · '
                            f'{esc(p["likes_label"])} likes, {esc(p["likes_per_min_label"])} per minuut</span></div><p class="post-text">{esc(p["text"])}</p></li>' for p in posts)
            parts.append(f"<h3>Snelle posts van grote accounts</h3><ol class=\"posts\">{items}</ol>")
        if not parts:
            parts.append(empty_state(sv.get("note") or "Nog geen tellingen."))
        c = sv.get("counts") or {}
        parts.append(f'<p class="caption">actieve tellingen {esc(fmt_n(c.get("keys_active")))} · vandaag bevestigd {esc(fmt_n(c.get("confirmed_today")))} · posts vandaag {esc(fmt_n(c.get("posts_today")))}</p>')
        body = "".join(parts)
    ex = explain(esc(sv.get("definition") or ""))
    return card("Wat rondgaat op X", ex + body, pill_html=head_pill, live="sociaal", foot='<a href="/methode#begrip-observatie">Waarom X nooit de aanleiding is</a>')


def measurement_strip(wk: dict[str, Any], cov: dict[str, Any]) -> str:
    if is_computing(wk) or is_computing(cov):
        return computing_card("Meting", live="meting-strip", foot='<a href="/meting">De meetweek, de dekking en de rapporten</a>')
    pos = wk.get("position") or {}
    phase = {"running": pos.get("phase_label"), "before": "nog niet begonnen", "after": "voorbij"}.get(pos.get("phase"), "onbekend")
    s = cov.get("summary") or {}
    nc = s.get("hours_not_covered")
    body = f'<div class="kpi-row">{kpi(phase or "–", "", "meetweek")}'
    if wk.get("found"):
        hr = wk["hit_rate"]
        body += kpi(hr["rate_label"], "", "trefkans tot nu toe") + kpi(f'{hr["ci"]["low_label"]} tot {hr["ci"]["high_label"]}' if hr["ci"]["status"] == "ok" else "te weinig data", "", "bootstrapinterval")
        body += kpi(wk["verdict_short"], "", "go/no-go")
    body += kpi(fmt_n(nc) if nc is not None else "–", "van 24 uur", "NIET GEDEKT") + "</div>"
    ex = explain(f"Dit dashboard laat zien wat de radar nú ziet; de {term('meetweek')} meet achteraf of zijn observaties iets waard waren. "
                 f"Een uur {term('niet-gedekt', 'NIET GEDEKT')} is een uur waarin hij niet keek.")
    return card("Meting", ex + body, pill_html=pill("frozen", "regels bevroren") if pos.get("rules_frozen") else "", live="meting-strip",
                foot='<a href="/meting">De meetweek, de dekking en de rapporten</a>')


def measurement_page(ctx: dict[str, Any], v: dict[str, Any]) -> str:
    """`/meting`: the measurement dashboard: the week, the coverage, the latest reports, how it works, the warning."""
    from . import charts

    wk, cov, reps = v["week"], v["coverage"], v["reports"]
    pos = wk.get("position") or {}
    meta = (f'<div class="hero-meta">{pill("computing")}<span>{esc(COMPUTING_TEXT)}</span></div>' if is_computing(wk) else
            f'<div class="hero-meta">{pill("frozen", "regels bevroren") if pos.get("rules_frozen") else pill("unknown", "regels niet bevroren")}'
            f'<span>{esc(pos.get("phase_label") or "")}</span><span>gegevens van {esc(wk["generated_label"])}</span></div>')
    hero = ('<section class="hero"><p class="eyebrow">Meting</p><h1>Hoe goed ziet de radar het?</h1>'
            '<p class="lede">Elke observatie en elke graduatie wordt achteraf gemeten: winnaar of niet, op tijd of te laat, en wat onbekend bleef. Dit is de stand van de meetweek, '
            'de dekking van de laatste 24 uur en de laatste rapporten.</p>' + meta + '</section>')
    return (hero + f'<div class="grid grid-2 section">{week_card(wk)}{coverage_card(cov, charts)}</div><div class="section">{reports_card(reps)}</div>'
            f'<div class="section">{how_card()}</div><div class="section">{warning_block(v["warning"])}</div>')


def _stat(label: str, value: str, empty_value: str | None, note: str) -> str:
    val = empty_value if empty_value is not None else value
    cls = "stat-value is-empty" if val in ("–", "onbekend") else "stat-value"
    return f'<div class="stat"><div class="stat-label">{esc(label)}</div><div class="{cls}">{esc(val)}</div><div class="stat-note">{esc(note)}</div></div>'


def _headline_html(st: dict[str, Any]) -> str:
    """The headline sentence with the numbers in bold (built here, escaped piece by piece)."""
    live = st["live"]
    fr = st["freshness"]
    if fr["status"] == "unknown":
        return esc(st["headline"])
    lead = "De radar ziet" if fr["status"] == "fresh" else "De radar zag"
    if live["creates_last_hour"] is not None:
        n = live["creates_last_hour"]
        a = f"in het laatste uur <strong>{esc(fmt_n(n))}</strong> nieuwe {'munt' if n == 1 else 'munten'}"
    else:
        a = "het laatste uur nog niet gedekt (geen telling van nieuwe munten)"
    if live["graduations_today"] is not None:
        g = live["graduations_today"]
        b = f"vandaag <strong>{esc(fmt_n(g))}</strong> {'graduatie' if g == 1 else 'graduaties'}"
    else:
        b = "vandaag nog geen gedekt uur"
    o = live["observations_today"] or 0
    c = f"<strong>{esc(fmt_n(o))}</strong> {'observatie' if o == 1 else 'observaties'}"
    return f"{lead} {a}, {b} en {c}."


def today_card(today: dict[str, Any], compact: bool = False) -> str:
    if is_computing(today):
        return computing_card("Observaties vandaag" if compact else "Vandaag", live="vandaag", cls="" if compact else "section")
    rows = (today.get("rows") or [])[: 5 if compact else None]
    total = (today.get("counts") or {}).get("total_today")
    if rows:
        items = "".join(obs_item(r) for r in rows)
        body = f'<ol class="obs-list">{items}</ol>'
        if total and total > len(rows):
            body += f'<p class="caption">De {esc(fmt_n(len(rows)))} nieuwste van {esc(fmt_n(total))} vandaag; <a href="/observaties">alle observaties</a>.</p>'
    else:
        body = empty_state("Vandaag nog geen observaties. De radar kijkt wel; de drempels zijn streng, en een dag zonder observatie is een gewone dag.")
    ex = explain(f"Een {term('observatie')} is een munt of een {term('thema')} die op dat moment aan de bevroren regels voldeed. "
                 f"De uitkomst komt pas na 24 uur: {term('winnaar')}, geen winnaar, {term('onbekend', 'onbekend')} of nog bezig. Dagen lopen in UTC, zoals in de rapporten.")
    p = pill("fresh", f"{fmt_n(total)} vandaag" if total is not None else "–") if rows else pill("unknown", "nog niets vandaag")
    return card(("Observaties vandaag" if compact else f"Vandaag · {today.get('day_label_long', '')}"), ex + body, pill_html=p,
                foot='<a href="/observaties">Alle observaties met uitkomst</a>', live="vandaag", cls="" if compact else "section")


def obs_item(r: dict[str, Any]) -> str:
    kind = "winner" if r["outcome"] == "winner" else ""
    mint = f'<span>mint-adres (tekst, geen link): <code>{esc(r["mint"])}</code></span>' if r.get("mint") else ""
    peak = f'<span>piek binnen 24 u: {esc(r["peak_label"])}</span>' if r.get("peak_label") not in (None, "–") else ""
    return (f'<li class="obs{" is-winner" if kind else ""}"><div class="obs-head"><span class="obs-kind">{esc(r["kind_label"])}</span>'
            f'<span class="obs-title">{esc(r["title"])}</span><time class="obs-time" datetime="{esc(r["as_of"])}">{esc(r["as_of_label"])}</time></div>'
            f'<p class="obs-why">Waarom opgevallen: {esc(r["why"])}</p>'
            f'<div class="obs-foot">{pill(r["outcome"], r["outcome_label"])}{peak}{mint}</div></li>')


def week_card(wk: dict[str, Any]) -> str:
    if is_computing(wk):
        return computing_card("Meetweek", live="meetweek", foot='<a href="/methode#go-no-go">Wat de toets betekent</a>')
    pos = wk["position"]
    head_pill = pill("frozen", "regels bevroren") if pos.get("rules_frozen") else pill("unknown", "regels niet bevroren")
    phase_value = {"running": pos["phase_label"], "before": "nog niet begonnen", "after": "voorbij"}.get(pos["phase"], "onbekend")
    top = f'<div class="kpi-row">{kpi(phase_value, "", "meetweek")}'
    if wk.get("found"):
        u, hr, rc, lt = wk["universe"], wk["hit_rate"], wk["recall"], wk["lead_time"]
        top += (kpi(f"{fmt_n(u['covered_days'])} van {fmt_n(wk.get('days') and len(wk['days']))}", "", "gedekte dagen")
                + kpi(fmt_n(u["winners"]), "", "winnaars in het universum")
                + kpi(hr["rate_label"], "", "trefkans tot nu toe") + kpi(rc["rate_label"], "", "recall vóór graduatie")
                + kpi(lt["median_label"], "", "mediane lead time") + "</div>")
        ci_bits = []
        for name, ci in (("trefkans", hr["ci"]), ("recall", rc["ci"])):
            if ci["status"] == "ok":
                ci_bits.append(f"{term(name)}: bootstrapinterval {esc(ci['low_label'])} tot {esc(ci['high_label'])}")
            else:
                ci_bits.append(f"{term(name)}: {pill('few')}")
        verdict = (f'<div class="kpi">{pill(wk["verdict_kind"], wk["verdict_short"])}<span class="kpi-unit">go/no-go-toets van {esc(wk["window_label"])}</span></div>'
                   f'<p class="small">{esc(wk["verdict_plain"])}</p>')
        crit = table([("label", "criterium"), ("value_label", "nu", "num"), ("threshold_label", "eis", "num"), ("status", "toets", "status")],
                     [{**c, "status": Raw(pill(c["status_kind"], c["status"])), "_class": ""} for c in wk["criteria"]], empty="nog geen toets", cls="stack",
                     widths=("45", "15", "20", "20"))
        body = top + f'<p class="small">{" · ".join(ci_bits)}</p>' + verdict + crit
        foot = f'<a href="{esc(wk["href"])}">Het weekrapport</a><a href="/methode#go-no-go">Wat de toets betekent</a>'
    else:
        top += "</div>"
        crit = table([("label", "criterium"), ("threshold_label", "eis", "num"), ("status", "toets", "status")],
                     [{**c, "status": Raw(pill("few")), "_class": ""} for c in wk.get("criteria") or []], empty="nog geen toets", cls="stack", widths=("55", "20", "25"))
        body = top + f'<p class="notice">{esc(wk.get("note") or "Nog geen weekrapport.")}</p>' + crit
        foot = '<a href="/methode#go-no-go">Wat de toets betekent</a>'
    ex = explain(f"De {term('meetweek')} loopt van {esc(pos['first_full_day_label'])} tot en met {esc(pos['end_day_label'])} met bevroren {term('regels')}: "
                 f"de drempels en de toets zijn vóóraf vastgelegd en worden niet achteraf aangepast. {term('trefkans')} en {term('recall')} krijgen een "
                 f"{term('bootstrapinterval')} zodra er genoeg dagen en winnaars zijn; tot die tijd staat er “te weinig data”.")
    return card("Meetweek", ex + body, pill_html=head_pill, foot=foot, live="meetweek")


def coverage_card(cov: dict[str, Any], charts: Any) -> str:
    if is_computing(cov):
        return computing_card("Dekking en eerlijkheid", live="dekking", foot='<a href="/methode#dekking">Hoe de dekking werkt</a>')
    s = cov.get("summary") or {}
    nc = s.get("hours_not_covered")
    p = pill("not_covered", f"{fmt_n(nc)} uur NIET GEDEKT") if nc else (pill("fresh", "alle uren gedekt") if s.get("hours_covered") else pill("unknown"))
    strip = charts.coverage_strip(cov.get("rows") or [])
    bars = charts.bars(cov.get("rows") or [], "creates", "clock", "Nieuwe munten per uur", "munten")
    body = (f'<div class="kpi-row">{kpi(fmt_n(nc) if nc is not None else "–", "van 24 uur", "NIET GEDEKT")}'
            f'{kpi(fmt_n(s.get("hours_complete")) if s.get("hours_complete") is not None else "–", "van 24 uur", "compleet")}'
            f'{kpi(fmt_n(s.get("creates_covered")) if s.get("creates_covered") is not None else "–", "in de gedekte uren", "nieuwe munten")}</div>'
            + strip + f'<p class="small">{esc(cov.get("sentence") or "")}</p>' + bars)
    ex = explain(f"Een uur is {term('compleet', 'gedekt')} als de radar er was om te tellen, en compleet als hij het hele uur meekeek. "
                 f"Een uur {term('niet-gedekt', 'NIET GEDEKT')} blijft leeg: wij waren er niet, dus is de telling onbekend, niet nul.")
    return card("Dekking en eerlijkheid", ex + body, pill_html=p, foot='<a href="/methode#dekking">Hoe de dekking werkt</a>', live="dekking")


def reports_card(reps: dict[str, Any]) -> str:
    if is_computing(reps):
        return computing_card("Laatste rapporten", live="rapporten", foot='<a href="/rapporten">Alle rapporten</a>')
    rows = reps.get("latest") or []
    if not rows:
        body = empty_state(reps.get("note") or "Nog geen rapporten.")
    else:
        body = '<ul class="report-list">' + "".join(report_item(r) for r in rows) + "</ul>"
    ex = explain("Elke ochtend schrijft de radar een dagrapport over de vorige UTC-dag en een weekrapport over de laatste zeven dagen. "
                 f"Een dag zonder gedekt uur heet {term('niet-gedekt', 'NIET GEDEKT')}: zijn cijfers blijven leeg.")
    return card("Laatste rapporten", ex + body, pill_html=pill("fresh", f"{fmt_n(reps.get('count', 0))} rapporten") if rows else "",
                foot='<a href="/rapporten">Alle rapporten</a>', live="rapporten")


def report_item(r: dict[str, Any]) -> str:
    kp = r.get("key_points") or {}
    st = kp.get("status", "unknown")
    if kp.get("status") == "week":
        p = pill(kp.get("verdict_kind", "few"), kp.get("verdict_short", "–"))
    else:
        p = pill({"complete": "complete", "incomplete": "incomplete", "not_covered": "not_covered"}.get(st, "unknown"), kp.get("status_label", "onbekend"))
    items = "".join(f"<li>{esc(i['label'])} <b>{esc(i['label_value'])}</b></li>" for i in kp.get("items") or [])
    note = f'<p class="caption">{esc(kp["note"])}</p>' if kp.get("note") else ""
    return (f'<li class="report-item"><div class="report-head"><a href="{esc(r["href"])}">{esc(r["kind_label"].capitalize())} {esc(r["day_label"])}</a>'
            f'{p}</div><ul class="kp">{items}</ul>{note}</li>')


def how_card() -> str:
    steps = (
        '<ol class="steps">'
        '<li class="step"><h3>Inlezen</h3><p>De radar leest elke nieuwe pump.fun-munt en elke graduatie uit twee onafhankelijke bronnen en schrijft op wanneer hij er níet bij was.</p></li>'
        '<li class="step"><h3>Observeren</h3><p>Vaste, bevroren regels wijzen munten en thema’s aan die opvallen: veel verschillende kopers, echte instroom, geen bundel. Dat is een observatie, geen advies.</p></li>'
        '<li class="step"><h3>Meten</h3><p>Na 24 uur wordt elke observatie en elke graduatie gemeten: winnaar of niet, en wat onbekend blijft heet onbekend. Alles komt in de rapporten.</p></li>'
        "</ol>"
    )
    ex = explain("Drie stappen, elke dag opnieuw, met dezelfde bevroren regels. De details, de drempels en de begrippen staan op de methodepagina.")
    return card("Hoe dit werkt", ex + steps, foot='<a href="/methode">De methode, de regels en de begrippenlijst</a>')


def warning_block(text: str) -> str:
    return (f'<section class="warning" aria-label="Waarschuwing"><h2>Let op</h2><p>{esc(text)}</p>'
            '<p>Deze site toont wat een meetinstrument ziet. Een observatie is geen aansporing om iets te doen; de meeste nieuwe munten verliezen hun hele waarde. '
            'Er staan geen koopknoppen en geen links naar handelsplaatsen; een mint-adres staat alleen als tekst zodat je kunt controleren wat gemeten werd.</p></section>')


# ----- observations -----------------------------------------------------------------------------------------------------
def observations_page(ctx: dict[str, Any], v: dict[str, Any]) -> str:
    if is_computing(v):
        return computing_page("Observaties", "Elke observatie, met uitkomst").replace('<div class="section">', '<div class="section" data-live="observaties">', 1)
    rows = v.get("rows") or []
    c = v.get("counts") or {}
    head = (f'<section class="hero"><p class="eyebrow">Observaties</p><h1>Elke observatie, met uitkomst</h1>'
            f'<p class="lede">Wat de radar aanwees, wanneer, waarom, en hoe het afliep. Nieuwste eerst; thema’s hebben geen eigen uitkomst.</p>'
            f'<div class="hero-meta">{pill("fresh", f"{fmt_n(c.get("total", 0))} getoond")}{pill("winner", f"{fmt_n(c.get("winner", 0))} winnaar")}'
            f'{pill("not_winner", f"{fmt_n(c.get("not_winner", 0))} geen winnaar")}{pill("unknown", f"{fmt_n(c.get("unknown", 0))} onbekend")}'
            f'{pill("pending", f"{fmt_n(c.get("pending", 0))} nog bezig")}<span>gegevens van {esc(v["generated_label"])}</span></div></section>')
    ex = explain(esc(v.get("definition") or "") + f" Lees meer over {term('trefkans')}, {term('winnaar')} en {term('onbekend', 'onbekend')} op de methodepagina.")
    if not rows:
        body = empty_state(v.get("note") or "Nog geen observaties.")
    else:
        groups: list[tuple[str, str, list[dict[str, Any]]]] = []
        for r in rows:
            if groups and groups[-1][0] == r["day"]:
                groups[-1][2].append(r)
            else:
                groups.append((r["day"], r["day_label"], [r]))
        body = ""
        for day, label, items in groups:
            body += (f'<section class="day-group"><h3>{esc(label)}<span class="caption">{fmt_n(len(items))} {"observatie" if len(items) == 1 else "observaties"}</span></h3>'
                     f'<ol class="obs-list">{"".join(obs_item(r) for r in items)}</ol></section>')
        total_all = v.get("total_all_time")
        if total_all and total_all > len(rows):
            body += f'<p class="caption">De {fmt_n(len(rows))} nieuwste van {fmt_n(total_all)} observaties in totaal.</p>'
    return head + f'<div data-live="observaties">{ex}{body}</div>'


# ----- reports ----------------------------------------------------------------------------------------------------------
def reports_page(ctx: dict[str, Any], v: dict[str, Any]) -> str:
    if is_computing(v):
        return computing_page("Rapporten", "Dag- en weekrapporten").replace('<div class="section">', '<div class="section" data-live="rapporten">', 1)
    rows = v.get("rows") or []
    head = ('<section class="hero"><p class="eyebrow">Rapporten</p><h1>Dag- en weekrapporten</h1>'
            '<p class="lede">Elke ochtend een dagrapport over de vorige UTC-dag en een weekrapport over de laatste zeven dagen: het universum, de winnaars, '
            'de trefkans, de recall en wat onbekend bleef.</p></section>')
    if not rows:
        body = empty_state(v.get("note") or "Nog geen rapporten.")
    else:
        weeks = [r for r in rows if r["kind"] == "week"]
        days = [r for r in rows if r["kind"] != "week"]
        body = ""
        if weeks:
            body += '<section class="section"><div class="section-head"><h2>Weekrapporten</h2></div><ul class="report-list">' + "".join(report_item(r) for r in weeks) + "</ul></section>"
        if days:
            body += '<section class="section"><div class="section-head"><h2>Dagrapporten</h2></div><ul class="report-list">' + "".join(report_item(r) for r in days) + "</ul></section>"
    return head + f'<div data-live="rapporten">{body}</div>'


def day_report_page(ctx: dict[str, Any], v: dict[str, Any]) -> str:
    from . import charts

    st = v["status"]
    p = pill({"complete": "complete", "incomplete": "incomplete", "not_covered": "not_covered"}.get(st, "unknown"), v["status_label"])
    kps = "".join(kpi(i["label_value"], "", i["label"]) for i in v["key_points"])
    head = (f'<section class="hero"><p class="report-nav"><a href="/rapporten">← alle rapporten</a></p><p class="eyebrow">Dagrapport</p>'
            f'<h1>{esc(v["day_label_long"])}</h1><div class="hero-meta">{p}<span>geschreven {esc(v["generated_label"])}</span></div>'
            + (f'<p class="notice is-nc">{esc(v["note"])}</p>' if v.get("note") else "") + f'<div class="kpi-row section">{kps}</div></section>')
    cov = v["coverage"]
    cov_rows = [{"clock": h["clock"], "status": h["status"], "covered": h["covered"], "complete": h["complete"], "creates": h["creates"]} for h in cov["hours"]]
    coverage = card("Dekking", explain(f"Per uur: compleet, gedekt of {term('niet-gedekt', 'NIET GEDEKT')}. Een leeg uur is geen nul.")
                    + f'<div class="kpi-row">{kpi(fmt_n(cov.get("hours_covered")), "van 24", "gedekt")}{kpi(fmt_n(cov.get("hours_complete")), "van 24", "compleet")}'
                    f'{kpi(fmt_n(cov.get("hours_not_covered")), "van 24", "NIET GEDEKT")}</div>'
                    + charts.coverage_strip(cov_rows) + charts.bars(cov_rows, "creates", "clock", "Nieuwe munten per uur", "munten"),
                    pill_html=pill("not_covered", f"{fmt_n(cov.get('hours_not_covered'))} uur") if cov.get("hours_not_covered") else "")
    if v["blank"]:
        return head + f'<div class="grid section">{coverage}</div>' + versions_block(v)
    u, o, rc = v["universe"], v["observations"], v["recall"]
    universe = card("Universum: alle graduaties van de dag",
                    explain(f"Het {term('universum')} is elke {term('graduatie')} die de radar zag, ook munten waar hij nooit naar keek. "
                            f"Een {term('winnaar')} haalde binnen 24 uur de winnaarsdefinitie; onbekend blijft onbekend.")
                    + f'<div class="kpi-row">{kpi(fmt_n(u["graduations"]), "", "graduaties")}{kpi(fmt_n(u["winners"]), "", "winnaars")}'
                    f'{kpi(fmt_n(u["not_winner"]), "", "geen winnaar")}{kpi(fmt_n(u["outcome_unknown"]), "", "onbekend")}</div>'
                    + kv([("gemeten", esc(fmt_n(u["measured"]))), ("nog bezig", esc(fmt_n(u["pending"]))), ("zonder meting", esc(fmt_n(u["missing"]))),
                          ("winnaars organisch / gebundeld / bundeling onbekend", f'{esc(fmt_n(u["winners_organic"]))} / {esc(fmt_n(u["winners_bundled"]))} / {esc(fmt_n(u["winners_bundled_unknown"]))}'),
                          ("onbekend door te weinig samples", esc(fmt_n(u["outcome_unknown_sparse_samples"]))),
                          ("graduaties met samples minstens elke " + esc(fmt_n(u["tracker_gap_minutes"])) + " min", esc(u["tracker_coverage_label"]))]))
    obs = card("Observaties van de dag",
               explain(f"De {term('trefkans')} is het deel van de munt-observaties dat binnen 24 uur winnaar werd, gerekend over de observaties met een bekende uitkomst; "
                       "onbekend en nog bezig staan ernaast en worden nooit weggemoffeld.")
               + f'<div class="kpi-row">{kpi(fmt_n(o["decisions"]), "", "munt-observaties")}{kpi(fmt_n(o["themes"]), "", "thema-observaties")}'
               f'{kpi(o["hit_rate_label"], "", "trefkans")}</div>'
               + kv([("raak", esc(fmt_n(o["hits"]))), ("mis", esc(fmt_n(o["misses"]))), ("onbekend", esc(fmt_n(o["unknown"]))), ("nog bezig", esc(fmt_n(o["pending"])))]))
    recall = card("Recall en lead time",
                  explain(f"{term('recall')}: het deel van de winnaars dat al een observatie had vóór zijn graduatie. {term('lead-time', 'Lead time')}: "
                          "hoeveel eerder die observatie kwam (negatief is te laat).")
                  + f'<div class="kpi-row">{kpi(rc["rate_label"], "", "recall vóór graduatie")}{kpi(rc["lead_median_label"], "", "mediane lead time")}</div>'
                  + kv([("winnaars met munt-observatie vóór graduatie", f'{esc(fmt_n(rc["before_graduation"]))} van {esc(fmt_n(rc["winners"]))}'),
                        ("met munt- óf thema-observatie", esc(fmt_n(rc["any_before_graduation"]))), ("op tijd / te laat", f'{esc(fmt_n(rc["lead_positive"]))} / {esc(fmt_n(rc["lead_negative"]))}')]))
    wrows = [{"name": w["name"] + (f" (${w['symbol']})" if w.get("symbol") else ""), "graduated_label": w["graduated_label"], "bundled_label": w["bundled_label"],
              "observed_label": w["observed_label"], "lead_label": w["lead_label"], "peak_label": w["peak_label"], "mint": Raw(f"<code>{esc(w['mint'])}</code>"),
              "_class": ""} for w in v["winners"]]
    winners = card("Winnaars van de dag",
                   explain(f"Elke {term('winnaar')} met of hij {term('gebundeld')} was, of de radar hem vooraf zag en met hoeveel voorsprong. Het mint-adres staat als tekst, zonder link.")
                   + table([("name", "munt"), ("graduated_label", "gegradueerd"), ("bundled_label", "gebundeld"), ("observed_label", "vooraf gezien"),
                            ("lead_label", "lead time", "num"), ("peak_label", "piek", "num"), ("mint", "mint-adres", "wide")], wrows, empty="geen winnaars op deze dag",
                           widths=("20", "15", "10", "10", "10", "10", "25")))
    unk = v["unknowns"]
    unknowns = card("Wat onbekend bleef",
                    explain("Alles wat de radar niet kon meten, met de reden. Onbekend telt nooit als nul en nooit als raak.")
                    + kv([("graduaties met onbekende uitkomst", esc(fmt_n(unk.get("graduations_outcome_unknown")))),
                          ("waarvan door te weinig samples", esc(fmt_n(unk.get("graduations_outcome_unknown_sparse_samples")))),
                          ("graduaties nog bezig", esc(fmt_n(unk.get("graduations_pending")))), ("graduaties zonder meting", esc(fmt_n(unk.get("graduations_missing_outcome")))),
                          ("bundeling onbekend", esc(fmt_n(unk.get("graduations_bundled_unknown")))), ("observaties met onbekende uitkomst", esc(fmt_n(unk.get("decisions_outcome_unknown")))),
                          ("observaties nog bezig", esc(fmt_n(unk.get("decisions_pending")))), ("graduaties zonder bekende create", esc(fmt_n(unk.get("graduations_without_create")))),
                          ("uren NIET GEDEKT", esc(fmt_n(unk.get("hours_not_covered"))))]))
    return head + f'<div class="grid grid-2 section">{coverage}{universe}{obs}{recall}</div><div class="section">{winners}</div><div class="section">{unknowns}</div>' + versions_block(v)


def versions_block(v: dict[str, Any]) -> str:
    ver = v.get("versions") or {}
    return (f'<p class="caption section">regels (rules_version) {esc(ver.get("rules_version"))} · meetmethode {esc(ver.get("method_version"))}'
            + (f' · regels in de observaties van deze dag: {esc(", ".join(ver.get("rules_versions_in_decisions") or []))}' if ver.get("rules_versions_in_decisions") else "")
            + f' · <a href="/methode#begrip-regels">wat dat betekent</a></p>')


def week_report_page(ctx: dict[str, Any], v: dict[str, Any]) -> str:
    from . import charts

    kps = "".join(kpi(i["label_value"], "", i["label"]) for i in v["key_points"])
    head = (f'<section class="hero"><p class="report-nav"><a href="/rapporten">← alle rapporten</a></p><p class="eyebrow">Weekrapport</p>'
            f'<h1>{esc(v["window_label"])}</h1><div class="hero-meta">{pill(v["verdict_kind"], v["verdict_short"])}'
            f'{pill("frozen", "bevroren regels") if v["frozen"] else pill("unknown", "andere regels")}<span>geschreven {esc(v["generated_label"])}</span></div>'
            f'<p class="lede">{esc(v["verdict_plain"])}</p><div class="kpi-row section">{kps}</div></section>')
    days = v["days"]
    obs_chart = charts.bars([{"day_label": d["day_label"], "covered": d["covered"], "observations": d["observations"]} for d in days],
                            "observations", "day_label", "Observaties per dag", "observaties", tick_every=1)
    rate_chart = charts.rate_lines(days, v["hit_rate"]["ci"], v["recall"]["ci"])
    hr, rc, lt, u, bs = v["hit_rate"], v["recall"], v["lead_time"], v["universe"], v["bootstrap"]
    crit = table([("label", "criterium"), ("value_label", "nu", "num"), ("threshold_label", "eis", "num"), ("status", "toets", "status")],
                 [{**c, "status": Raw(pill(c["status_kind"], c["status"])), "_class": ""} for c in v["criteria"]], empty="nog geen toets", widths=("45", "15", "20", "20"))
    gate = card("Go/no-go-toets", explain(f"Zes vooraf vastgelegde criteria ({term('meetweek')}); het rapport toont alleen of ze gehaald zijn en beslist niets. "
                                           f"De twee intervalcriteria wachten op een {term('bootstrapinterval')}.") + crit
                + (f'<p class="caption">{esc(v["gate_note"])}</p>' if v.get("gate_note") else ""), pill_html=pill(v["verdict_kind"], v["verdict_short"]))
    hit = card("Trefkans", explain(f"{term('trefkans')} = raak binnen 24 uur gedeeld door de munt-observaties waarvan de 24 uur om zijn; een {term('onbekend', 'onbekende')} uitkomst telt mee in de noemer.")
               + f'<div class="kpi-row">{kpi(hr["rate_label"], "", "trefkans")}{kpi(hr["rate_known_label"], "", "over bekende uitkomsten")}'
               f'{kpi(hr["ci"]["low_label"] + " tot " + hr["ci"]["high_label"] if hr["ci"]["status"] == "ok" else "–", "", "bootstrapinterval")}</div>'
               + kv([("munt-observaties", esc(fmt_n(hr["decisions"]))), ("raak", esc(fmt_n(hr["hits"]))), ("mis", esc(fmt_n(hr["misses"]))), ("onbekend", esc(fmt_n(hr["unknown"]))),
                     ("nog bezig", esc(fmt_n(hr["pending"]))), ("interval", esc("te weinig data: " + str(hr["ci"]["reason"]) if hr["ci"]["status"] != "ok" else f"{hr['ci']['low_label']} tot {hr['ci']['high_label']}"))]),
               pill_html=pill("few") if hr["ci"]["status"] != "ok" else "")
    recall = card("Recall en lead time", explain(f"{term('recall')} vóór graduatie: winnaars met een munt- óf thema-observatie vóór hun graduatie. {term('lead-time', 'Lead time')}: graduatie min de vroegste observatie.")
                  + f'<div class="kpi-row">{kpi(rc["rate_label"], "", "recall vóór graduatie")}{kpi(lt["median_label"], "", "mediane lead time")}'
                  f'{kpi(rc["ci"]["low_label"] + " tot " + rc["ci"]["high_label"] if rc["ci"]["status"] == "ok" else "–", "", "bootstrapinterval")}</div>'
                  + kv([("winnaars met observatie vooraf", f'{esc(fmt_n(rc["n"]))} van {esc(fmt_n(rc["denominator"]))}'), ("alleen munt / alleen thema", f'{esc(fmt_n(rc["coin_n"]))} / {esc(fmt_n(rc["theme_n"]))}'),
                        ("lead time p25 / mediaan / p75", f'{esc(lt["p25_label"])} / {esc(lt["median_label"])} / {esc(lt["p75_label"])}'),
                        ("op tijd / te laat", f'{esc(fmt_n(lt["positive"]))} / {esc(fmt_n(lt["negative_or_zero"]))}')]),
                  pill_html=pill("few") if rc["ci"]["status"] != "ok" else "")
    universe = card("Universum van de week", explain(f"Alle {term('graduatie', 'graduaties')} op de gedekte dagen. Een dag {term('niet-gedekt', 'NIET GEDEKT')} telt niet mee en is nooit een dag met nul winnaars.")
                    + f'<div class="kpi-row">{kpi(fmt_n(u["covered_days"]), "", "gedekte dagen")}{kpi(fmt_n(u["complete_days"]), "", "complete dagen")}'
                    f'{kpi(fmt_n(u["graduations"]), "", "graduaties")}{kpi(fmt_n(u["winners"]), "", "winnaars")}</div>'
                    + kv([("NIET GEDEKT", esc(u["not_covered_days_label"])), ("uren NIET GEDEKT in totaal", esc(fmt_n(u["hours_not_covered"]))),
                          ("winnaars organisch / gebundeld / onbekend", f'{esc(fmt_n(u["winners_organic"]))} / {esc(fmt_n(u["winners_bundled"]))} / {esc(fmt_n(u["winners_bundled_unknown"]))}'),
                          ("geen winnaar / uitkomst onbekend", f'{esc(fmt_n(u["not_winner"]))} / {esc(fmt_n(u["outcome_unknown"]))}'),
                          ("observaties per gedekte dag", esc(v["observations"]["per_day_label"])), ("tracker-dekking", esc(u["tracker_coverage_label"]))]))
    drows = [{"day_label": d["day_label"], "status": Raw(pill({"complete": "complete", "incomplete": "incomplete", "not_covered": "not_covered"}.get(d["status"], "unknown"), d["status_label"])),
              "graduations": "–" if d["graduations"] is None else fmt_n(d["graduations"]), "winners": "–" if d["winners"] is None else fmt_n(d["winners"]),
              "observations": "–" if d["observations"] is None else fmt_n(d["observations"]), "hit_rate_label": d["hit_rate_label"], "recall_label": d["recall_label"],
              "hours_not_covered": fmt_n(d["hours_not_covered"]), "_class": "is-nc" if not d["covered"] else ""} for d in days]
    per_day = card("Per dag", explain("Dezelfde cijfers per dag. Lege cellen zijn geen nullen: op een dag NIET GEDEKT is het universum onbekend.")
                   + obs_chart + rate_chart
                   + table([("day_label", "dag"), ("status", "dekking"), ("graduations", "graduaties", "num"), ("winners", "winnaars", "num"), ("observations", "observaties", "num"),
                            ("hit_rate_label", "trefkans", "num"), ("recall_label", "recall", "num"), ("hours_not_covered", "uren NIET GEDEKT", "num")], drows, empty="geen dagen"))
    boot = (f'<p class="caption section">Bootstrap: {esc(fmt_n(bs.get("draws")))} trekkingen met de gedekte dagen als blokken; een interval pas vanaf '
            f'{esc(fmt_n(bs.get("min_days")))} gedekte dagen en {esc(fmt_n(bs.get("min_winners")))} winnaars'
            + (f' (nu: {esc(bs.get("reason"))})' if bs.get("reason") else "") + f'. Regels {esc(v["fingerprint_short"])} ({esc(", ".join(v["rules_versions"]))}), meetmethode {esc(v["method_version"])}.'
            + (f' Andere regels in dit venster: {esc(", ".join(s["rules_group_short"] for s in v["other_segments"]))} (apart getoetst, niet op deze pagina).' if v.get("other_segments") else "") + "</p>")
    return head + f'<div class="section">{gate}</div><div class="grid grid-2 section">{hit}{recall}</div><div class="section">{universe}</div><div class="section">{per_day}</div>' + boot


# ----- the method page --------------------------------------------------------------------------------------------------
def method_page(ctx: dict[str, Any], m: dict[str, Any]) -> str:
    if is_computing(m):
        return computing_page("Methode", "Hoe de radar werkt en wat de cijfers betekenen")
    w = m["winner"]
    fr = m["frozen"]
    crit = table([("label", "criterium"), ("threshold_label", "eis", "num")], [{**c, "_class": ""} for c in m["criteria"]], empty="geen criteria", widths=("70", "30"))
    toc = "".join(f'<a href="#{esc(a)}">{esc(t)}</a>' for a, t in (("werkwijze", "Werkwijze"), ("regels", "Bevroren regels"), ("meting", "Wat gemeten wordt"),
                                                                     ("dekking", "Dekking"), ("betekenis", "Wat de cijfers wel en niet zeggen"), ("go-no-go", "Go/no-go"),
                                                                     ("begrippen", "Begrippenlijst"), ("disclaimer", "Disclaimer")))
    gl = [
        ("graduatie", "Het moment waarop een pump.fun-munt de bonding curve verlaat en op pump-amm verhandeld wordt (de migratie). Het universum van de meting bestaat uit graduaties."),
        ("universum", "Alle graduaties van een UTC-dag zoals de radar ze zelf zag, ook de munten waar hij nooit naar keek. Zonder volledig universum is recall een leugen; daarom staat de dekking bovenaan."),
        ("observatie", "Een munt of een thema dat op een bepaald moment aan de bevroren regels voldeed. Een observatie is een feit over dat moment, geen advies en geen voorspelling."),
        ("thema", "Een woord dat opeens in veel nieuwe muntnamen van verschillende makers opduikt (ook wel meta). Een thema-observatie heeft geen eigen uitkomst; hij telt mee voor de recall als een winnaar dat woord in zijn naam had."),
        ("winnaar", f"Een graduatie of observatie die binnen {w['hours']} uur een piek-marktkapitalisatie van minstens {w['peak_label']} én een volume van minstens {w['volume_label']} haalde. Is de piek onbekend, dan is de uitkomst onbekend; is de piek bekend en te laag, dan geen winnaar."),
        ("trefkans", "Het deel van de munt-observaties dat binnen 24 uur winnaar werd. In het weekrapport en de toets: raak gedeeld door de observaties waarvan de 24 uur om zijn, met een onbekende uitkomst meegeteld in de noemer (een niet-meetbare observatie is geen aangetoonde treffer). Nog bezig staat apart."),
        ("recall", "Het deel van de winnaars dat al een observatie (munt of thema) had vóór zijn graduatie. Een winnaar zonder observatie is een gemiste winnaar; dit cijfer laat dat zien."),
        ("lead-time", "Graduatie min de vroegste observatie, in minuten. Positief is op tijd, negatief is te laat. Het rapport toont de mediaan, p25 en p75."),
        ("gebundeld", "Een munt heet gebundeld als hij binnen een minuut na de create gradueerde of als de maker meteen de helft van de curve kocht. Zonder die informatie is bundeling onbekend. Alleen organische winnaars zeggen iets over vroeg zien."),
        ("compleet", f"Een uur is gedekt als minstens één bron van creates een deel van dat uur actief was; compleet als het uur voorbij is en een bron het hele uur zonder gat meekeek. Een dag is compleet voor de toets als geen enkel piekuur (12:00 tot 02:00 UTC) NIET GEDEKT was."),
        ("niet-gedekt", "Een uur of dag waarin de radar niet keek. Zijn tellingen blijven leeg: een leeg vak is geen nul, en een dag NIET GEDEKT telt niet mee in de week en is nooit een dag met nul winnaars."),
        ("onbekend", "Onbekend: de meting kon niets vaststellen (geen samples, te weinig samples, geen volume). Nog bezig: de 24 uur zijn nog niet om. Beide worden apart geteld en nooit als nul of als raak gerekend."),
        ("bootstrapinterval", f"Een betrouwbaarheidsinterval voor de trefkans en de recall over de week, berekend door de gedekte dagen {m['bootstrap']['draws_label']} keer met teruglegging te trekken (dagen als blokken, vaste seed). Pas vanaf {fmt_n(m['bootstrap']['min_days'])} gedekte dagen en {fmt_n(m['bootstrap']['min_winners'])} winnaars; tot die tijd staat er “te weinig data”."),
        ("meetweek", f"Zeven dagen met bevroren regels, vanaf {fr['first_full_day_label']}. Aan het einde toetst het weekrapport zes vooraf vastgelegde criteria. De uitkomst beslist niets vanzelf; de eigenaar beslist, en de cijfers staan hier voor iedereen."),
        ("regels", f"De drempels die bepalen wat een observatie wordt, staan in bestanden met een vingerafdruk (nu {fr['fingerprint_now_short']}). Elke observatie draagt de versie van de regels die hem maakte. Verandert een regel, dan verandert de vingerafdruk en begint een nieuw segment in de rapporten; achteraf aanpassen is daardoor zichtbaar."),
    ]
    glossary = '<dl class="glossary">' + "".join(f'<dt id="begrip-{esc(k)}">{esc(GLOSSARY[k])}</dt><dd>{esc(t)}</dd>' for k, t in gl) + "</dl>"
    body = (
        f'<section class="hero"><p class="eyebrow">Methode</p><h1>Hoe de radar werkt en wat de cijfers betekenen</h1>'
        f'<p class="lede">{esc(ctx["site"]["name"])} {esc(ctx["site"]["product"])} is een meetinstrument: hij leest alle nieuwe pump.fun-munten, wijst er met vaste regels een paar aan, '
        f'en meet achteraf eerlijk wat er van kwam. Geen advies, geen koopknop, geen verborgen cijfers.</p><nav class="toc" aria-label="Op deze pagina">{toc}</nav></section>'
        '<article class="prose">'
        '<h2 id="werkwijze">Werkwijze in drie stappen</h2>'
        '<ol><li><strong>Inlezen.</strong> Elke nieuwe munt en elke graduatie komt binnen via twee onafhankelijke bronnen (een publieke datastroom en de Solana-RPC); wat de ene mist vult de andere aan, en wat beide missen staat als gat in de dekking. Elke rij krijgt een chain-tijd en een ontvangsttijd, zodat terugkijken onmogelijk is: een kenmerk wordt berekend zoals het op dat moment bekend was.</li>'
        '<li><strong>Observeren.</strong> Een kleine engine kijkt elke vijftien seconden naar de jongste munten en de woorden in hun namen en wijst met bevroren drempels aan wat opvalt. Dat is een observatie: een feit over dat moment, dat meteen wordt opgeslagen met de versie van de regels.</li>'
        '<li><strong>Meten.</strong> Na 1, 6 en 24 uur wordt van elke observatie én van elke graduatie (ook die zonder observatie) de piek-marktkapitalisatie en het volume gemeten. Winnaar of niet; onbekend blijft onbekend. Elke ochtend komt een dagrapport, elke ochtend ook een weekrapport over de laatste zeven dagen.</li></ol>'
        '<h2 id="regels">Welke regels bevroren zijn</h2>'
        f'<p>Sinds {esc(fr["since_label"])} draait de radar op bevroren regels (versie <code>{esc(fr["rules_version"])}</code>, vingerafdruk <code>{esc(fr["fingerprint_short"])}</code>); '
        f'de eerste volle meetdag is {esc(fr["first_full_day_label"])}. De code van nu heeft {"dezelfde" if fr["same"] else "een ANDERE"} vingerafdruk ({esc(fr["fingerprint_now_short"])}).</p>'
        f'<p><strong>Een munt wordt een observatie</strong> bij: {esc(m["coin_rules"])}. Daarna geldt per munt een pauze van {esc(m["cooldown_label"])} voordat dezelfde munt opnieuw een observatie kan worden.</p>'
        f'<p><strong>Een thema wordt een observatie</strong> bij: {esc(m["meta_rules"])}.</p>'
        '<p>Meer dan dit is er niet: geen machine learning, geen geheime score, geen handmatige keuze. Wie de regels wil nalezen vindt ze in de broncode van de motor (fart) en in elk rapport bij “Versies”.</p>'
        '<h2 id="meting">Wat gemeten wordt</h2>'
        f'<p><strong>Winnaar</strong> = piek-marktkapitalisatie van minstens {esc(w["peak_label"])} én volume van minstens {esc(w["volume_label"])} binnen {esc(fmt_n(w["hours"]))} uur na het ankermoment (de graduatie voor het universum, de observatie voor de trefkans). '
        f'De piek komt uit de eigen trades voor munten die de radar volgde en anders uit prijssamples die elke {esc(fmt_n(m["sample_minutes"]))} minuten genomen worden. Een graduatie zonder sample minstens elke {esc(fmt_n(m["tracker_minutes"]))} minuten telt als “uitkomst onbekend”, nooit als “geen winnaar”: een piek tussen schaarse samples kan gemist zijn.</p>'
        '<p><strong>Trefkans</strong> (van onze observaties), <strong>recall</strong> (van alle winnaars) en <strong>lead time</strong> (hoeveel eerder we er waren) staan in elk rapport; de begrippenlijst hieronder geeft de precieze definitie. '
        f'Over de week komt er een bootstrapinterval bij zodra er {esc(fmt_n(m["bootstrap"]["min_days"]))} gedekte dagen en {esc(fmt_n(m["bootstrap"]["min_winners"]))} winnaars zijn; zeven dagen geven brede intervallen, en dat is eerlijk.</p>'
        f'<p>De meetmethode heeft een eigen versie ({esc(m["method_version"])}); verandert de definitie, dan verandert die versie en worden oude rijen nooit overschreven.</p>'
        '<h2 id="dekking">Dekking: wanneer we er niet waren</h2>'
        '<p>Een radar die een uur uit stond, heeft dat uur niets gezien. Zulke uren heten NIET GEDEKT en blijven overal leeg: in de balk op het dashboard (gearceerd), in de tabellen (een streepje) en in de week (de dag telt niet mee). '
        'Een leeg vak is dus nooit een nul. Een uur is gedekt als minstens één bron een deel van het uur actief was en compleet als een bron het hele uur zonder gat meekeek; alleen complete uren maken een dag compleet voor de toets.</p>'
        '<h2 id="betekenis">Wat de cijfers wel en niet zeggen</h2>'
        '<ul><li>Ze zeggen wat een vast meetinstrument achteraf zag. Ze zeggen niet dat een munt gaat stijgen, en een observatie is geen aansporing om iets te doen.</li>'
        '<li>Een trefkans over een handvol observaties zegt weinig; daarom staat er een interval zodra dat kan en “te weinig data” zolang het niet kan.</li>'
        '<li>Een hoge recall met een korte lead time betekent dat de radar vooral zag wat al bijna gegradueerd was; daarom staat de lead time ernaast.</li>'
        '<li>Ongeveer de helft van alle graduaties is gebundeld (de maker kocht zelf de curve vol). Alleen organische winnaars zeggen iets over vroeg zien; het rapport splitst ze.</li>'
        '<li>Er staat nergens een rendement, een winst of een voorspelling. Wat onbekend is, heet onbekend.</li></ul>'
        '<h2 id="go-no-go">De go/no-go-toets van de meetweek</h2>'
        '<p>Vóór de meetweek zijn zes criteria vastgelegd waaraan de cijfers moeten voldoen voordat de eigenaar overweegt observaties met een community te delen. Het weekrapport toont per criterium “voldaan”, “niet voldaan” of “te weinig data” en één oordeel. Het rapport beslist niets en verstuurt niets; de criteria zijn startwaarden van de bouwer en worden niet achteraf aangepast.</p>'
        + crit +
        '<h2 id="begrippen">Begrippenlijst</h2>' + glossary +
        '<h2 id="disclaimer">Disclaimer</h2>'
        f'<p><strong>{esc(m["warning"])}</strong></p>'
        '<p>Deze website en de bijbehorende berichten zijn een meetinstrument en een verslag daarvan. Niets erop is financieel advies, een aanbeveling, een aansporing of een voorspelling. Memecoins zijn extreem riskant: de meeste nieuwe munten verliezen hun hele waarde, vaak binnen uren, en een piek die hier als “winnaar” staat kan allang voorbij zijn op het moment dat je hem leest. '
        'De cijfers zijn zo eerlijk als de dekking toelaat: wat de radar niet zag, staat als onbekend of NIET GEDEKT. De eigenaar handelt niet op basis van deze site, verkoopt niets, en ontvangt niets van de makers van munten die hier genoemd worden. Mint-adressen staan uitsluitend als tekst, zodat je kunt controleren wat gemeten werd; er zijn geen links naar handelsplaatsen en geen koopknoppen. '
        'Wie toch handelt, doet dat volledig op eigen risico en verantwoordelijkheid.</p>'
        f'<p class="caption">{esc(m["banner"])}</p></article>'
    )
    return body


# ----- tools --------------------------------------------------------------------------------------------------------------
def tools_page(ctx: dict[str, Any]) -> str:
    return ('<section class="hero"><p class="eyebrow">Tools</p><h1>Binnenkort</h1>'
            '<p class="lede">Hier komen hulpmiddelen die op dezelfde data bouwen als het dashboard en de rapporten: dezelfde observaties, dezelfde metingen, dezelfde eerlijkheidsregels.</p></section>'
            f'<div class="section placeholder">{pill("soon")}<p class="small muted">Eerst de meetweek, dan de tools. Wat hier komt, gebruikt geen andere bron dan wat je op deze site al ziet, '
            'en krijgt geen functie die de site tot een koopknop maakt.</p><p class="small"><a href="/">Terug naar het dashboard</a></p></div>')


# ----- login and errors -----------------------------------------------------------------------------------------------------
def login_page(ctx: dict[str, Any], csrf: str, error: str | None = None) -> str:
    err = f'<p class="error" role="alert">{esc(error)}</p>' if error else ""
    note = ("De openbare pagina’s hebben geen wachtwoord nodig. Dit is de ingang van de eigenaar: de gezondheid van de radar, de budgetten en de instellingen."
            if ctx.get("public") else "Deze site staat op dit moment helemaal achter het wachtwoord van de eigenaar. Alles is alleen-lezen.")
    return (f'<div class="login"><section class="card"><div class="card-head"><h1>Inloggen</h1></div><p class="small muted">{esc(note)}</p>{err}'
            f'<form method="post" action="/login"><input type="hidden" name="csrf" value="{esc(csrf)}">'
            '<div class="field"><label for="password">Wachtwoord</label><input type="password" id="password" name="password" autocomplete="current-password" required autofocus></div>'
            '<p class="section"><button class="btn btn-primary" type="submit">Inloggen</button></p></form></section>'
            + ('<p class="caption"><a href="/">Terug naar het dashboard</a></p>' if ctx.get("public") else "") + "</div>")


def error_page(status: int, message: str) -> str:
    return f'<section class="hero"><p class="eyebrow">{esc(status)}</p><h1>{esc(message)}</h1><p class="lede"><a href="/">Terug naar het dashboard</a></p></section>'


# ----- the owner section ------------------------------------------------------------------------------------------------------
DAEMON_COLS: list[Col] = [("label", "onderdeel"), ("last", "laatst gezien"), ("age", "leeftijd", "num"), ("status_label", "status")]
COVERAGE_COLS: list[Col] = [("hour_label", "uur"), ("creates_pp", "creates PP", "num"), ("creates_rpc", "creates RPC", "num"), ("creates_union", "unie", "num"),
                            ("grads", "graduaties", "num"), ("grads_without_create", "zonder create", "num"), ("gap_s", "gat (s)", "num"), ("gaps_label", "gaten", "wide"),
                            ("covered_label", "gedekt"), ("complete_label", "compleet")]
PLAN_COLS: list[Col] = [("kind", "soort"), ("planned_label", "gepland", "num"), ("actual_label", "werkelijk", "num"), ("planned_share_label", "gepland, % plafond", "num"),
                        ("actual_share_label", "werkelijk, % plafond", "num")]
KEY_COLS: list[Col] = [("key", "sleutel", "wide"), ("kind", "soort"), ("started_label", "gestart"), ("counts_n", "tellingen", "num"), ("last_n_1h", "laatste uur", "num"),
                       ("peak_n_1h", "piek per uur", "num"), ("confirmed_label", "bevestigd"), ("search_pending_label", "zoekopdracht wacht"), ("next_due_label", "volgende telling"),
                       ("usd_label", "kosten", "num")]
STOPPED_COLS: list[Col] = [("reason_label", "stopreden"), ("n", "sleutels", "num"), ("usd_label", "kosten", "num"), ("counts", "tellingen", "num"), ("searches", "zoekopdrachten", "num")]
REPORT_COLS: list[Col] = [("label", "kernpunt"), ("value", "waarde", "num"), ("note", "toelichting", "wide")]
GATE_COLS: list[Col] = [("label", "criterium"), ("value_label", "waarde", "num"), ("threshold_label", "drempel", "num"), ("status", "toets", "status")]
DECISION_COLS: list[Col] = [("as_of_label", "tijd"), ("kind_label", "soort"), ("subject", "munt / thema", "wide"), ("name", "naam"), ("outcome_label", "uitkomst"),
                            ("peak_label", "piek mcap (USD)", "num"), ("rules_version", "rules_version"), ("reasons_label", "redenen", "wide"), ("features_label", "features", "features wide")]
SPEND_COLS: list[Col] = [("kind", "soort"), ("n", "eenheden", "num"), ("usd_label", "USD", "num")]
LOG_COLS: list[Col] = [("name", "log"), ("size_label", "grootte", "num"), ("modified_label", "laatste regel"), ("warnings", "waarschuwingen (staart)", "num"),
                       ("errors", "fouten (staart)", "num"), ("last_error", "laatste fout", "wide")]
VIEW_COLS: list[Col] = [("view", "onderdeel"), ("seconds_label", "laatste duur", "num"), ("age_label", "berekend", "num"), ("state_label", "stand"),
                        ("errors", "fouten", "num"), ("last_error", "laatste fout", "wide")]


def _rowcls(rows: list[dict[str, Any]], mapping: dict[str, str]) -> list[dict[str, Any]]:
    return [{**r, "_class": mapping.get(str(r.get("_class") or ""), "")} for r in rows]


def owner_page(ctx: dict[str, Any], v: dict[str, Any]) -> str:
    st, cv, x, rp, wk, sv, lg = v["status"], v["coverage"], v["x"], v["report"], v["week"], v["settings"], v["logs"]
    note = f'<p class="notice">{esc(st["note"])}</p>' if st.get("note") else ""
    anchors = "".join(f'<a href="#{a}">{t}</a>' for a, t in (("gezondheid", "Gezondheid"), ("dekking", "Dekking"), ("beslissingen", "Beslissingen"), ("budgetten", "Budgetten"),
                                                                 ("x", "X en planner"), ("rapport", "Rapporten"), ("weergaven", "Weergaven"), ("instellingen", "Instellingen"),
                                                                 ("logboek", "Logboek")))
    head = (f'<section class="hero"><p class="eyebrow">Eigenaar</p><h1>Operationeel overzicht</h1><p class="lede">Alles wat de openbare pagina’s niet tonen: versheid van daemons en timers, budgetten, de X-planner, de instellingen en het logboek. Alleen-lezen, ook voor jou.</p>'
            f'<div class="hero-meta"><span>gegevens van {esc(st["generated_label"])}</span><span>schema {esc(fmt(st.get("schema_version")))}</span><span>rules_version {esc(st.get("rules_version"))}</span></div></section>'
            f'<nav class="owner-nav" aria-label="Secties">{anchors}</nav>{note}')
    health = card("Gezondheid en versheid", table(DAEMON_COLS, _rowcls(st.get("daemons") or [], {"stale": "is-stale", "never": "is-never"}), empty="geen database", widths=("40", "25", "10", "25"))
                  + kv([("open gaten in de stromen", esc(st.get("open_gaps_label"))), ("mislukkingen (tellers)", esc(st.get("failures_label")))]
                       + engine_tick_items(st.get("engine_tick"))), anchor="gezondheid", live="eigenaar-gezondheid")
    coverage = card("Dekking van de laatste 24 uur", f'<p class="small muted">gedekt {esc(fmt(get_path(cv, "summary.hours_covered")))} · compleet {esc(fmt(get_path(cv, "summary.hours_complete")))} · '
                    f'NIET GEDEKT {esc(fmt(get_path(cv, "summary.hours_not_covered")))} (een uur zonder actieve bron blijft leeg)</p>'
                    + table(COVERAGE_COLS, _rowcls(cv.get("rows") or [], {"notcovered": "is-nc"}), empty="geen database"), anchor="dekking", live="eigenaar-dekking")
    dt = st.get("decisions_today") or {}
    decisions = card(f"Beslissingen vandaag (UTC-dag {dt.get('day', '')})",
                     kv([("munt-observaties", esc(fmt(dt.get("observe")))), ("thema-observaties", esc(fmt(dt.get("observe_meta")))), ("samen", esc(fmt(dt.get("total")))),
                         ("laatste beslissing", esc(fmt(dt.get("last")))), ("watchlist actief", esc(fmt(st.get("watchlist_active")))), ("beslissingen in totaal", esc(fmt(get_path(st, "counts.decisions"))))]),
                     anchor="beslissingen", live="eigenaar-beslissingen", foot='<a href="/observaties">De observaties met uitkomst</a>')
    b = st.get("budgets") or {}
    budgets = card("Budgetten vandaag",
                   kv([("CU (schatting) tegenover plafond", f'{esc(get_path(b, "rpc.cu_today_label"))} van {esc(get_path(b, "rpc.cap_label"))} ({esc(get_path(b, "rpc.share_label"))})'),
                       ("RPC-aanroepen geblokkeerd door een plafond", esc(fmt(get_path(b, "rpc.blocked_today")))),
                       ("X tegenover plafond", f'{esc(get_path(b, "x.usd_today_label"))} van {esc(get_path(b, "x.cap_label"))} ({esc(get_path(b, "x.share_label"))})'),
                       ("X-aanroepen geweigerd door een plafond", esc(fmt(get_path(b, "x.blocked_today"))))]) + f'<p class="caption">{esc(get_path(b, "rpc.note"))}</p>',
                   anchor="budgetten", live="eigenaar-budgetten")
    xg = (lambda path: esc(fmt(get_path(x, path))))  # noqa: E731
    xcard = card("X-stand en planner",
                 kv([("stand (effectief)", xg("mode.label")), ("X_MODE gevraagd", xg("mode.requested")), ("opmerking", xg("mode.note")),
                     ("daemons melden (social / engine)", xg("mode.social_daemon") + " / " + xg("mode.engine_daemon")),
                     ("uitgegeven vandaag", xg("budget.spend_label") + " van " + xg("budget.cap_label") + " (" + xg("budget.share_label") + ")"),
                     ("actieve sleutels", xg("planner.active_total") + " (" + xg("planner.active_label") + ")"),
                     ("zoekopdracht wacht / telling nu due", xg("planner.search_pending") + " / " + xg("planner.due_now")),
                     ("gestart vandaag", xg("planner.started_today")), ("gestopt vandaag", xg("planner.stopped_label")), ("uitgesteld vandaag (envelop)", xg("planner.deferred_label")),
                     ("schaduwbeslissingen vandaag / totaal", xg("shadow.decisions_today") + " / " + xg("shadow.decisions_total")),
                     ("schaduwtellingen vandaag (bevestigd 1 u / 24 u)", xg("shadow.confirmations_today") + " (" + xg("shadow.confirmed_1h_today") + " / " + xg("shadow.confirmed_24h_today") + ")")])
                 + "<h3>Plan tegenover uitgaven (per soort, vandaag)</h3>" + table(PLAN_COLS, x.get("plan") or [], empty="geen plan")
                 + (f'<p class="caption">planfout: {xg("plan_error")}</p>' if x.get("plan_error") else "")
                 + "<h3>Actieve sleutels</h3>" + table(KEY_COLS, x.get("active_keys") or [], empty="geen actieve sleutels")
                 + "<h3>Gestopt vandaag per reden</h3>" + table(STOPPED_COLS, x.get("stopped_rows") or [], empty="geen sleutel gestopt vandaag")
                 + "<h3>Uitgaven vandaag per soort</h3>" + table(SPEND_COLS, x.get("spend_by_kind") or [], empty="geen uitgaven"), anchor="x", live="eigenaar-x")
    report = card("Laatste dagrapport (ruwe kernpunten)",
                  kv([("dag", esc(fmt(rp.get("day_label")))), ("status", esc(fmt(rp.get("status_label")))), ("gegenereerd", esc(fmt(rp.get("report_generated_label")))),
                      ("rules_version / method_version", f'{esc(fmt(rp.get("rules_version")))} / {esc(fmt(rp.get("method_version")))}')])
                  + (f'<p class="notice is-nc">{esc(rp["note"])}</p>' if rp.get("note") else "") + table(REPORT_COLS, rp.get("items") or [], empty="nog geen dagrapport"),
                  anchor="rapport", foot=(f'<a href="{esc(rp["href"])}">de openbare pagina</a><a href="/rapporten/{esc(rp["day"])}.md">de ruwe Markdown</a>' if rp.get("found") else ""))
    seg0 = (wk.get("segments") or [{}])[0] if wk.get("found") else {}
    week = card("Go/no-go van het laatste weekrapport (bevroren criteria)",
                kv([("venster", esc(fmt(wk.get("window_label")))), ("segment", esc(fmt(wk.get("segment_title")))), ("dagen", esc(fmt(seg0.get("days_label")))),
                    ("gegenereerd", esc(fmt(wk.get("report_generated_label"))))])
                + f'<p class="kpi-value">{esc(fmt(wk.get("verdict")))}</p>'
                + table(GATE_COLS, _rowcls([{**c, "status": Raw(pill({"ok": "ok", "fail": "fail"}.get(str(c.get("_class")), "few"), str(c.get("status"))))} for c in seg0.get("criteria") or []], {}), empty="nog geen weekrapport")
                + (f'<p class="caption">{esc(seg0.get("note"))}</p>' if seg0.get("note") else "") + (f'<p class="notice">{esc(wk["note"])}</p>' if wk.get("note") else ""),
                foot=(f'<a href="{esc(wk["href"][:-3])}">de openbare pagina</a><a href="{esc(wk["href"])}">de ruwe Markdown</a>' if wk.get("found") else ""))
    raw_reports = "".join(f'<li><a href="{esc(f["href"])}">{esc(f["name"])}</a> <span class="muted">({esc(f["kind_label"])} {esc(f["day_label"])}, geschreven {esc(f["modified_label"])})</span></li>' for f in v.get("report_files") or [])
    reports_raw = card("Alle rapporten als ruwe Markdown", f'<ul class="small">{raw_reports}</ul>' if raw_reports else empty_state("nog geen rapporten"))
    sections = ""
    for sec in sv.get("sections") or []:
        rows = "".join(f'<tr><th scope="row">{esc(r["label"])}</th><td>{esc(fmt(r["value"]))}</td></tr>' for r in sec["rows"])
        sections += f'<h3>{esc(sec["title"])}</h3><div class="table-wrap"><table class="table"><tbody>{rows}</tbody></table></div>'
    settings_card = card("Instellingen (alleen-lezen)", '<p class="small muted">Wat hier staat verandert alleen door een commit en een nieuwe uitrol; geheimen staan nooit op deze pagina.</p>' + sections, anchor="instellingen")
    logs = card("Logboek (samenvatting)", '<p class="small muted">De staart van elk logbestand: waarschuwingen en fouten geteld, de laatste fout benoemd. Nooit een cookie, sleutel of wachtwoord.</p>'
                + table(LOG_COLS, _rowcls(lg.get("rows") or [], {"errors": "is-stale"}), empty="geen logbestanden", widths=("15", "10", "20", "10", "10", "35")), anchor="logboek", live="eigenaar-logboek")
    views = views_card(v.get("views") or {})
    return (head + f'<div class="grid grid-2 section owner">{health}{decisions}{budgets}{report}</div><div class="section owner">{coverage}</div>'
            f'<div class="section owner">{xcard}</div><div class="section owner">{week}</div><div class="section owner">{reports_raw}</div>'
            f'<div class="section owner">{views}</div><div class="section owner">{settings_card}</div><div class="section owner">{logs}</div>')


def engine_tick_items(et: dict[str, Any] | None) -> list[tuple[str, str]]:
    """Milestone 11: the budget of the last completed engine tick (every step) and the slow-tick counters, for the health
    card of /eigenaar; the same figures `fart status` prints (kv engine.tick_last, engine.tick_slow)."""
    if not et:
        return []
    last = et.get("last") or {}
    if last:
        value = (f'{pill("stale" if last.get("slow") else "fresh", last.get("tick_label"))} {esc(last.get("status_label"))}; as_of {esc(last.get("as_of_label"))} '
                 f'({esc(fmt_n(last.get("age_s")))} s geleden): {esc(last.get("steps_label"))}; {esc(fmt_n(last.get("coins")))} gevolgde munten, '
                 f'{esc(fmt_n(last.get("metas")))} thema’s, basislijn {esc(fmt_n(last.get("baseline_coins")))} munten')
    else:
        value = esc("nog geen voltooide tick geregistreerd (de engine schrijft engine.tick_last vanaf zijn tweede tick; milestone 11)")
    slow = (f'totaal {esc(fmt_n(et.get("slow_total")))}, vandaag {esc(fmt_n(et.get("slow_today")))} '
            f'(een tick langer dan het interval van {esc(secs_label(et.get("interval_s")))}; geteld in engine.tick_slow, hoogstens één waarschuwing per uur in het log)')
    return [("engine-tick (laatste voltooide, duur per stap)", value), ("trage engine-ticks", slow)]


def secs_label(s: Any) -> str:
    """'0.02 s', '0.4 s', '12 s': two significant digits under ten seconds, '0.00 s' under five milliseconds (the figure
    `fart status` prints too)."""
    try:
        v = float(s)
    except (TypeError, ValueError):
        return "–"
    if v < 0.005:
        return "0.00 s"
    return f"{v:.2g} s" if v < 10 else f"{v:.0f} s"


def views_card(vs: dict[str, Any]) -> str:
    """Milestone 10: what the background thread computes, how long every view took the last time, what failed."""
    rows = []
    for r in vs.get("rows") or []:
        age = r.get("age_s")
        rows.append({"view": r["view"], "seconds_label": secs_label(r.get("seconds")), "age_label": ("–" if age is None else f"{fmt_n(age)} s geleden"),
                     "state_label": {"computed": "berekend", "computing": "wordt berekend", "failed": "mislukt"}.get(str(r.get("state")), str(r.get("state"))),
                     "errors": fmt_n(r.get("errors") or 0), "last_error": r.get("last_error") or "geen",
                     "_class": "is-stale" if (r.get("errors") or not r.get("ok", True)) else ""})
    intro = (f'<p class="small muted">Eén achtergrondthread berekent de openbare onderdelen ({"draait" if vs.get("running") else "draait niet: alleen de testaanroep berekent"}): '
             f'de live-onderdelen elke {esc(fmt_n(vs.get("live_ttl_s")))} s, de rest elke {esc(fmt_n(vs.get("ttl_s")))} s; een verzoek leest alleen en wacht nergens op. '
             f'Een berekening langer dan {esc(fmt_n(vs.get("slow_s")))} s komt in het logboek (web.view_slow); dezelfde duren staan in logs/{esc(vs.get("file"))} voor fart status. '
             f'Berekeningen {esc(fmt_n(vs.get("computes")))}, mislukt {esc(fmt_n(vs.get("failures")))}.</p>')
    return card("Weergaven van de website (achtergrondberekening)", intro + table(VIEW_COLS, rows, empty="nog niets berekend", widths=("22", "12", "16", "14", "8", "28")),
                anchor="weergaven", live="eigenaar-weergaven")


def raw_report_page(ctx: dict[str, Any], body_html: str, name: str, public_href: str | None) -> str:
    back = f'<a href="/rapporten">← alle rapporten</a>' + (f'<a href="{esc(public_href)}">de openbare pagina</a>' if public_href else "")
    return f'<p class="report-nav">{back}<span class="muted">{esc(name)} (ruwe Markdown, alleen voor de eigenaar)</span></p><section class="card report-md">{body_html}</section>'
