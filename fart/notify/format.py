"""Dutch plain-language messages for the owner. Facts only, no advice words, no tier words.
Times in Europe/Amsterdam with UTC in brackets. Every message ends with the fixed footer.
HTML parse mode: names and texts from the chain or from X are escaped."""
from __future__ import annotations

import datetime as _dt
import html
import json
from typing import Any

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    ZoneInfo = None  # type: ignore[assignment]

MONTHS_NL = ["jan", "feb", "mrt", "apr", "mei", "jun", "jul", "aug", "sep", "okt", "nov", "dec"]


def _tz(name: str):
    if ZoneInfo is None:
        return _dt.timezone.utc
    try:
        return ZoneInfo(name)
    except Exception:
        return _dt.timezone.utc


def fmt_time(ts: float | None, tz_name: str = "Europe/Amsterdam") -> str:
    """'26 okt 14:05 (12:05 UTC)'; 'onbekend' when there is no time."""
    if ts is None:
        return "onbekend"
    tz = _tz(tz_name)
    local = _dt.datetime.fromtimestamp(ts, tz=tz)
    utc = _dt.datetime.fromtimestamp(ts, tz=_dt.timezone.utc)
    return f"{local.day} {MONTHS_NL[local.month - 1]} {local:%H:%M} ({utc:%H:%M} UTC)"


def fmt_day(day: str, tz_name: str = "Europe/Amsterdam") -> str:
    try:
        d = _dt.datetime.strptime(day, "%Y-%m-%d")
        return f"{d.day} {MONTHS_NL[d.month - 1]} {d.year}"
    except ValueError:
        return day


def esc(s: Any) -> str:
    return html.escape("" if s is None else str(s), quote=False)


def n(x: Any, decimals: int = 0) -> str:
    """Dutch number formatting: 12.345 and 12,5. None -> 'onbekend'."""
    if x is None:
        return "onbekend"
    try:
        v = float(x)
    except (TypeError, ValueError):
        return esc(x)
    if decimals == 0:
        return f"{int(round(v)):,}".replace(",", ".")
    s = f"{v:,.{decimals}f}"
    return s.replace(",", "\x00").replace(".", ",").replace("\x00", ".")


def age(seconds: float | None) -> str:
    if seconds is None:
        return "onbekend"
    s = int(seconds)
    if s < 90:
        return f"{s} s"
    if s < 5400:
        return f"{s // 60} min"
    return f"{s / 3600:.1f} u".replace(".", ",")


def tri(v: Any) -> str:
    return "onbekend" if v is None else ("ja" if v else "nee")


def with_footer(lines: list[str], footer: str) -> str:
    return "\n".join([*lines, "", f"<i>{esc(footer)}</i>"])


# ----- message kinds ---------------------------------------------------------------------------------
def coin_observation_lines(dec: dict[str, Any], f: dict[str, Any], sol_usd: float | None) -> list[str]:
    mcap_sol = f.get("mcap_sol")
    mcap_usd = None if mcap_sol is None or sol_usd is None else mcap_sol * sol_usd
    name = esc(f.get("name") or "?")
    sym = esc(f.get("symbol") or "?")
    lines = [
        f"<b>{name}</b> (${sym}) · leeftijd {age(f.get('age_s'))}",
        f"<code>{esc(dec.get('mint') or f.get('mint'))}</code>",
        f"marktkapitalisatie {n(mcap_sol, 1)} SOL" + (f" (≈ ${n(mcap_usd)})" if mcap_usd is not None else " (USD onbekend)"),
        f"unieke kopers 5 min: {n(f.get('unique_buyers_5m'))} · netto SOL 5 min: {n(f.get('net_sol_5m'), 2)}",
        f"gebundeld: {tri(f.get('bundled'))} · eerdere munten maker (24 u): {n(f.get('creator_prior_coins_24h'))}",
    ]
    xm = f.get("x_mentions_1h")
    if xm:
        lines.append(f"X: {n(xm)} vermeldingen, {n(f.get('x_distinct_authors_1h'))} accounts (laatste uur)")
    lines.append(f"data: beslissing #{dec['id']} · {fmt_time(dec.get('as_of'))}")
    return lines


def meta_observation_lines(dec: dict[str, Any], m: dict[str, Any]) -> list[str]:
    burst = "∞" if m.get("burst_ratio_is_inf") else n(m.get("burst_ratio"), 1)
    return [
        f"<b>thema “{esc(dec.get('term') or m.get('term'))}”</b>: {n(m.get('n_coins'))} munten van {n(m.get('distinct_creators'))} makers",
        f"instroom {n(m.get('sol_inflow'), 1)} SOL · unieke kopers {n(m.get('unique_buyers'))} · burst {burst}× de basislijn",
        f"data: beslissing #{dec['id']} · {fmt_time(dec.get('as_of'))}",
    ]


def observe_batch(decisions: list[dict[str, Any]], sol_usd: float | None, footer: str, max_items: int = 8) -> str:
    """One message for a batch of observe / observe_meta decisions (dicts with id, kind, mint, term, as_of, features)."""
    shown = decisions[:max_items]
    lines = [f"<b>Observaties</b> ({len(decisions)}) · {fmt_time(decisions[-1]['as_of']) if decisions else ''}", ""]
    for d in shown:
        f = d.get("features") or {}
        lines.extend(coin_observation_lines(d, f, sol_usd) if d["kind"] == "observe" else meta_observation_lines(d, f))
        lines.append("")
    if len(decisions) > len(shown):
        lines.append(f"… en {len(decisions) - len(shown)} meer (zie de tabel decisions).")
    while lines and lines[-1] == "":
        lines.pop()
    return with_footer(lines, footer)


def fast_post(dec: dict[str, Any], footer: str) -> str:
    f = dec.get("features") or {}
    lines = [
        f"<b>Snelle post</b> van @{esc(f.get('author') or '?')} · {fmt_time(f.get('created_at') or dec.get('as_of'))}",
        f"likes/min {n(f.get('likes_per_min'))} · likes {n(f.get('likes'))} · quotes/min {n(f.get('quotes_per_min'), 1)}",
        f"“{esc((f.get('text') or '')[:200])}”",
        "",
    ]
    for m in (f.get("matched") or [])[:3]:
        lines.append(f"woord “{esc(m.get('word'))}”: {n(m.get('n_coins'))} munten van {n(m.get('n_creators'))} makers")
    mints = f.get("mints") or []
    for mint in mints[:5]:
        lines.append(f"<code>{esc(mint)}</code>")
    if len(mints) > 5:
        lines.append(f"… en {len(mints) - 5} meer")
    lines.append(f"data: beslissing #{dec['id']} · post {esc(f.get('post_id'))}")
    return with_footer(lines, footer)


def digest(day: str, rep: dict[str, Any], x_spend_usd: float | None, x_cap_usd: float | None, cu_spent: float | None, cu_cap: float | None,
           footer: str) -> str:
    u, rc, p, cov = rep.get("universe", {}), rep.get("recall", {}), rep.get("precision", {}), rep.get("coverage", {})

    def pct(x: Any) -> str:
        return "onbekend" if x is None else f"{100 * float(x):.0f}%"

    gaps = cov.get("gap_seconds_by_source") or {}
    status = "compleet" if rep.get("complete") else ("niet gedekt (fart draaide niet)" if rep.get("coverage_status") == "not_covered" else "niet compleet")
    lines = [
        f"<b>Dagrapport {fmt_day(day)}</b> · {status}",
        "",
        f"graduaties: {n(u.get('graduations'))} (gemeten {n(u.get('measured'))}, bezig {n(u.get('pending'))}, ontbrekend {n(u.get('missing'))})",
        f"winnaars: {n(u.get('winners'))} · organisch {n(u.get('winners_organic'))} · gebundeld {n(u.get('winners_bundled'))} · onbekend {n(u.get('winners_bundled_unknown'))}",
        f"geobserveerd vóór eerste trade: {n(rc.get('decision_before_first_trade'))}/{n(rc.get('winners'))} · vóór graduatie: {n(rc.get('decision_before_graduation'))}/{n(rc.get('winners'))}",
        f"precisie observaties: {pct(p.get('precision'))} ({n(p.get('hits'))} raak van {n(p.get('known'))} bekend, {n(p.get('unknown'))} onbekend, {n(p.get('pending'))} bezig) · organisch {pct(p.get('precision_organic'))}",
        f"dekking: {n(cov.get('hours_complete'))}/24 complete uren" + (f", {n(cov.get('hours_covered'))}/24 gedekt" if cov.get("hours_covered") is not None else "")
        + f" · creates {n(cov.get('creates_union'))} (PumpPortal {n(cov.get('creates_pp'))}, RPC {n(cov.get('creates_rpc'))}) · gaten: "
        + (", ".join(f"{esc(k)} {n(v)} s" for k, v in gaps.items()) if gaps else "geen"),
        ("X: niet geconfigureerd" if x_spend_usd is None else f"X: ${n(x_spend_usd, 2)}" + (f" van ${n(x_cap_usd, 2)}" if x_cap_usd else ""))
        + f" · RPC: {n(cu_spent)} CU" + (f" van {n(cu_cap)}" if cu_cap else ""),
        f"rules_version {esc(rep.get('rules_version_now'))} · method {esc(rep.get('method_version'))}",
    ]
    return with_footer(lines, footer)


HEALTH_TEXT = {
    "stream_silent": "stroom <b>{source}</b> is al {age} stil (laatste bericht {last})",
    "budget_80": "budget <b>{source}</b> op {pct} van het dagplafond ({used} van {cap})",
    "budget_cap": "budget <b>{source}</b> heeft het dagplafond bereikt ({used} van {cap}); de bron pauzeert tot de volgende UTC-dag",
    "pumpportal_refused": "PumpPortal weigerde de verbinding met HTTP {status} om {at}",
}


def health(kind: str, params: dict[str, Any], now: float, footer: str) -> str:
    text = HEALTH_TEXT.get(kind, kind).format(**{k: esc(v) for k, v in params.items()})
    return with_footer([f"<b>Gezondheid</b> · {fmt_time(now)}", text], footer)


def test_message(now: float, rules_version: str, footer: str) -> str:
    return with_footer([f"<b>Testbericht</b> van fart · {fmt_time(now)}", f"rules_version <code>{esc(rules_version)}</code>. Als je dit leest werkt de bezorging."], footer)


def decision_to_dict(row: Any) -> dict[str, Any]:
    d = dict(row)
    try:
        d["features"] = json.loads(d.get("features_json") or "{}")
    except json.JSONDecodeError:
        d["features"] = {}
    return d


# ----- milestone 5: the community channel (off by default; preview to the owner) -----------------------------------
COMMUNITY_PREVIEW_HEADER = "VOORBEELD community-bericht"


def coin_rules_text(ccfg: dict[str, Any]) -> str:
    """The frozen coin-observation thresholds in plain Dutch (config/engine.json -> coin)."""
    parts = [f"minstens {n(ccfg.get('observe_min_unique_buyers_5m'))} unieke kopers en {n(ccfg.get('observe_min_net_sol_5m'), 1)} SOL netto instroom in 5 minuten"]
    share = ccfg.get("observe_max_largest_buyer_share")
    if share is not None:
        parts.append(f"grootste koper hoogstens {round(100 * float(share))}% van de instroom")
    if ccfg.get("observe_require_not_bundled", True):
        parts.append("geen bundel (geen instant-graduatie, dev-buy onder de helft van de curve)")
    return "; ".join(parts)


def meta_rules_text(mcfg: dict[str, Any]) -> str:
    """The frozen theme thresholds in plain Dutch (config/engine.json -> meta)."""
    return (f"minstens {n(mcfg.get('min_coins'))} munten van {n(mcfg.get('min_distinct_creators'))} makers in {round(float(mcfg.get('window_s', 10800)) / 3600)} uur, "
            f"{n(mcfg.get('burst_ratio'), 1)}× de eigen basislijn, {n(mcfg.get('observe_min_sol_inflow'), 1)} SOL instroom en "
            f"{n(mcfg.get('observe_min_unique_buyers'))} unieke kopers")


def _why_coin(f: dict[str, Any], ccfg: dict[str, Any]) -> str:
    bits = [f"{n(f.get('unique_buyers_5m'))} verschillende kopers in 5 minuten",
            f"{n(f.get('net_sol_5m'), 2)} SOL meer gekocht dan verkocht"]
    share = f.get("largest_buyer_share_15m")
    if share is not None:
        bits.append(f"grootste koper {round(100 * float(share))}% van de instroom")
    if f.get("bundled") is False:
        bits.append("geen bundel gezien")
    elif f.get("bundled") is None:
        bits.append("bundeling onbekend")
    xm = f.get("x_mentions_1h")
    if xm:
        bits.append(f"{n(xm)} vermeldingen op X in het laatste uur")
    return ", ".join(bits)


def community_message(dec: dict[str, Any], sol_usd: float | None, rules_version: str, engine_cfg: dict[str, Any], ccfg_notify: dict[str, Any],
                      footer: str, theme: str | None = None, preview: bool = False) -> str:
    """The community text for an observe (coin) or observe_meta (theme) decision. Dutch plain language:
    what was seen (coin, theme, buyers, SOL inflow, age), why it stands out, the rules (frozen version),
    a fixed warning line and the footer. No contract-address link, no buy button; the mint as text.
    `preview=True` prepends the preview header (the copy that goes to the owner)."""
    f = dec.get("features") or {}
    warning = str(ccfg_notify.get("warning") or "Geen advies. Hoog risico. Alles wordt achteraf gemeten en gepubliceerd.")
    header = str(ccfg_notify.get("preview_header") or COMMUNITY_PREVIEW_HEADER)
    lines: list[str] = []
    if preview:
        lines += [f"<b>{esc(header)}</b>", ""]
    if dec.get("kind") == "observe_meta":
        term = dec.get("term") or f.get("term")
        lines += [
            f"<b>Gezien</b> · {fmt_time(dec.get('as_of'))}",
            f"Thema “{esc(term)}”: {n(f.get('n_coins'))} nieuwe munten van {n(f.get('distinct_creators'))} verschillende makers in {round(float(engine_cfg.get('meta', {}).get('window_s', 10800)) / 3600)} uur",
            f"Instroom {n(f.get('sol_inflow'), 1)} SOL · {n(f.get('unique_buyers'))} unieke kopers · "
            + ("oneindig veel vaker dan normaal (dit thema was er niet)" if f.get("burst_ratio_is_inf") else f"{n(f.get('burst_ratio'), 1)}× zo vaak als normaal"),
        ]
        sample = [m for m in (f.get("sample_mints") or [])][:3]
        if sample:
            lines.append("Voorbeelden (mint-adres als tekst): " + ", ".join(f"<code>{esc(m)}</code>" for m in sample))
        lines += [
            "",
            f"<b>Waarom dit opvalt</b>: een golf van munten rond één woord, met echt geld erachter.",
            f"<b>Regels</b> (bevroren versie <code>{esc(rules_version)}</code>): {esc(meta_rules_text(engine_cfg.get('meta', {})))}.",
        ]
    else:
        mcap_sol = f.get("mcap_sol")
        mcap_usd = None if mcap_sol is None or sol_usd is None else float(mcap_sol) * float(sol_usd)
        lines += [
            f"<b>Gezien</b> · {fmt_time(dec.get('as_of'))}",
            f"Munt <b>{esc(f.get('name') or '?')}</b> (${esc(f.get('symbol') or '?')}), leeftijd {age(f.get('age_s'))}"
            + (f", thema “{esc(theme)}”" if theme else ", geen thema"),
            f"Kopers in 5 minuten: {n(f.get('unique_buyers_5m'))} · instroom {n(f.get('net_sol_5m'), 2)} SOL netto · "
            f"marktkapitalisatie {n(mcap_sol, 1)} SOL" + (f" (≈ ${n(mcap_usd)})" if mcap_usd is not None else ""),
            f"Mint-adres (tekst, geen link): <code>{esc(dec.get('mint') or f.get('mint'))}</code>",
            "",
            f"<b>Waarom dit opvalt</b>: {esc(_why_coin(f, engine_cfg.get('coin', {})))}.",
            f"<b>Regels</b> (bevroren versie <code>{esc(rules_version)}</code>): {esc(coin_rules_text(engine_cfg.get('coin', {})))}.",
        ]
    lines += ["", f"<b>Let op</b>: {esc(warning)}", f"data: beslissing #{dec.get('id')}"]
    return with_footer(lines, footer)


def community_weekly(rep: dict[str, Any], footer: str, ccfg_notify: dict[str, Any], sent_this_week: int | None, channel_enabled: bool,
                     preview: bool = False) -> str:
    """The weekly accountability message: the go/no-go section of the week report in plain words.
    `rep` is the week report dict (out/recall/week-<end>.json); its first segment is the frozen rules."""
    header = str(ccfg_notify.get("preview_header") or COMMUNITY_PREVIEW_HEADER)
    seg = (rep.get("segments") or [{}])[0]
    u, hit, rec, lt, g = seg.get("universe", {}), seg.get("hit_rate", {}), seg.get("recall", {}), seg.get("lead_time", {}), seg.get("gate", {})
    status_nl = {"voldaan": "✓ voldaan", "niet voldaan": "✗ niet voldaan", "te weinig data": "? te weinig data"}

    def pct(x: Any) -> str:
        return "onbekend" if x is None else f"{100 * float(x):.0f}%"

    lines: list[str] = []
    if preview:
        lines += [f"<b>{esc(header)}</b> (wekelijkse verantwoording)", ""]
    lines += [
        f"<b>Verantwoording week {fmt_day(str(rep.get('start_day')))} t/m {fmt_day(str(rep.get('end_day')))}</b>",
        "",
        f"Gemeten: {n(u.get('graduations'))} graduaties op {n(u.get('covered_days'))} gedekte dagen, {n(u.get('winners'))} winnaars "
        f"(piek ≥ $100.000 én volume ≥ $20.000 binnen 24 uur); uitkomst onbekend bij {n(u.get('outcome_unknown'))}.",
        f"Onze observaties: {n(hit.get('decisions'))} munten aangewezen; {n(hit.get('hits'))} daarvan werden winnaar ({pct(hit.get('rate'))} van de "
        f"{n(hit.get('elapsed'))} met verstreken 24 uur; {n(hit.get('unknown'))} onbekend meegeteld, {n(hit.get('pending'))} nog bezig).",
        f"Van de winnaars hadden we {n((rec.get('before_graduation') or {}).get('any', {}).get('n'))} vooraf gezien "
        f"({pct((rec.get('before_graduation') or {}).get('any', {}).get('rate'))}), mediaan "
        f"{'onbekend' if (lt.get('any_vs_graduation') or {}).get('median_s') is None else str(round(float(lt['any_vs_graduation']['median_s']) / 60)) + ' minuten'} vóór graduatie.",
        "",
        "<b>Toets voor community-berichten</b> (vooraf vastgelegd, niet achteraf aangepast):",
    ]
    for c in g.get("criteria") or []:
        lines.append(f"• {esc(c.get('label'))}: {status_nl.get(c.get('status'), esc(c.get('status')))}")
    lines += ["", f"<b>Uitkomst</b>: {esc(g.get('verdict') or 'onbekend')}. Dit bericht beslist niets; het laat zien hoe de meting ervoor staat."]
    if channel_enabled:
        lines.append(f"Community-berichten deze week: {n(sent_this_week)}.")
    else:
        lines.append("Community-berichten deze week: geen (het kanaal staat uit tot na de meetweek).")
    lines.append(f"<b>Let op</b>: {esc(str(ccfg_notify.get('warning') or ''))}")
    return with_footer(lines, footer)


def secs(v: Any) -> str:
    """A duration: '0.02 s', '0.4 s', '12 s' (two significant digits under ten seconds, whole seconds above); under five
    milliseconds '0.00 s'; '–' when there is no value. The one figure `fart status`, /eigenaar and the log summaries share."""
    try:
        x = float(v)
    except (TypeError, ValueError):
        return "–"
    if x < 0.005:
        return "0.00 s"
    return f"{x:.2g} s" if x < 10 else f"{x:.0f} s"
