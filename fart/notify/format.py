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
    lines = [
        f"<b>Dagrapport {fmt_day(day)}</b> · {'compleet' if rep.get('complete') else 'niet compleet'}",
        "",
        f"graduaties: {n(u.get('graduations'))} (gemeten {n(u.get('measured'))}, bezig {n(u.get('pending'))}, ontbrekend {n(u.get('missing'))})",
        f"winnaars: {n(u.get('winners'))} · organisch {n(u.get('winners_organic'))} · gebundeld {n(u.get('winners_bundled'))} · onbekend {n(u.get('winners_bundled_unknown'))}",
        f"geobserveerd vóór eerste trade: {n(rc.get('decision_before_first_trade'))}/{n(rc.get('winners'))} · vóór graduatie: {n(rc.get('decision_before_graduation'))}/{n(rc.get('winners'))}",
        f"precisie observaties: {pct(p.get('precision'))} ({n(p.get('hits'))} raak van {n(p.get('known'))} bekend, {n(p.get('unknown'))} onbekend, {n(p.get('pending'))} bezig) · organisch {pct(p.get('precision_organic'))}",
        f"dekking: {n(cov.get('hours_complete'))}/24 complete uren · creates {n(cov.get('creates_union'))} (PumpPortal {n(cov.get('creates_pp'))}, RPC {n(cov.get('creates_rpc'))}) · gaten: "
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
