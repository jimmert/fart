"""The research reports (milestone 12): `out/research/<date>-<id>.md|json` in Dutch, honest about every variant, and the
promotion of a proven variant into config/plays.json (`fart research promote`).

A report carries the pre-registered statement verbatim, the data it ran on, the execution assumptions, every variant on
the selection half, the test half and the whole window, the walk-forward choice with the proven test per criterion, the
conditioning splits and the sensitivity of the chosen variant, the unknown and no-fill counts, and the multiplicity
note. A rejected variant is a row like any other. Nothing is rounded away: the JSON next to the Markdown has the cases.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ..config import Settings
from ..util import atomic_write_json, atomic_write_text, day_key, utc_iso
from ..version import rules_version
from . import hypotheses as hyps

REPORT_DIR = "research"


def report_dir(settings: Settings) -> Path:
    return settings.out_dir / REPORT_DIR


def report_name(hypothesis: str, now: float) -> str:
    return f"{day_key(now)}-{str(hypothesis).upper()}"


# ----- formatting -----------------------------------------------------------------------------------------------------------
def _sol(x: Any) -> str:
    return "onbekend" if x is None else f"{float(x):+.4f} SOL".replace(".", ",")


def _sol_abs(x: Any) -> str:
    return "onbekend" if x is None else f"{float(x):.3f} SOL".replace(".", ",")


def _pct(x: Any, signed: bool = True) -> str:
    if x is None:
        return "onbekend"
    v = 100 * float(x)
    return (f"{v:+.1f}%" if signed else f"{v:.1f}%").replace(".", ",")


def _n(x: Any) -> str:
    return "onbekend" if x is None else f"{int(x):,}".replace(",", ".")


def _min(seconds: Any) -> str:
    if seconds is None:
        return "onbekend"
    m = float(seconds) / 60.0
    return (f"{m:.1f} min" if abs(m) < 10 else f"{m:.0f} min").replace(".", ",")


def _dec(x: Any, digits: int = 2) -> str:
    return "onbekend" if x is None else f"{float(x):.{digits}f}".replace(".", ",")


def _ci(ci: dict[str, Any] | None) -> str:
    if not ci:
        return "te weinig data"
    return f"[{_sol(ci.get('low'))} – {_sol(ci.get('high'))}]"


def _verdict_word(v: dict[str, Any]) -> str:
    return "BEWEZEN" if v.get("proven") else "niet bewezen"


# ----- markdown -------------------------------------------------------------------------------------------------------------
def render_markdown(r: dict[str, Any]) -> str:
    h = r["hypothesis"]
    cfg = r["config"]
    st = r.get("stats") or {}
    lines = [
        f"# Onderzoeksrapport {h['id']} · {h['title']}",
        "",
        f"Gegenereerd {r.get('generated_at')} · rules_version van de code {r.get('rules_version')} · venster {cfg['days'][0]} t/m {cfg['days'][-1]} ({len(cfg['days'])} dagen)"
        + (" · **BEPERKT TOT %s MUNTEN (rooktest)**" % _n(cfg["limit_coins"]) if cfg.get("limit_coins") else ""),
        "",
        "## Vooraf vastgelegde hypothese",
        "",
        f"**Stelling.** {h['statement']}",
        "",
        f"**Bereik.** {h['scope']}",
        "",
        f"**Wat de data moet laten zien.** {h['expectation']}",
    ]
    for note in h.get("notes") or []:
        lines.append(f"- {note}")
    lines += [
        "",
        "## Data en uitvoering",
        "",
        f"- Munten in het venster: {_n(st.get('coins_considered'))}, waarvan met trades {_n(st.get('coins_with_trades'))}; door de voorfilter weggelaten {_n(st.get('coins_prefiltered'))}, "
        f"doorgerekend {_n(st.get('coins_evaluated'))}; ticks getoetst {_n(st.get('ticks'))}; signalen {_n(st.get('signals'))}; rekentijd {st.get('seconds')} s."
        + (f" Thema-observaties in het venster: {_n(st.get('theme_decisions'))}." if h["id"] == "H3" else ""),
        f"- Instap {cfg['entry_delay_s']:.0f} s na het signaal (de mens), gevuld met buy_quote op de curvestand van de eerste trade daarna; inzet {_dec(cfg['size_sol'], 2)} SOL; "
        f"uitstap eveneens {cfg['exit_delay_s']:.0f} s na het moment waarop de regel vuurt.",
        f"- Kosten: curvefee {_dec(cfg['curve_fee_bps'] / 100, 2)}% per kant, priority fee {_dec(cfg['costs']['priority_fee_sol'], 4)} SOL per transactie, prijsimpact uit de curve; na graduatie "
        f"PumpSwap-fee {_dec(cfg['costs']['pumpswap_fee_bps'] / 100, 2)}% en impact uit de DexScreener-liquiditeit (85 SOL aangenomen als die ontbreekt, gemarkeerd).",
        f"- Zonder prijspad tot de uitstap is de uitkomst **onbekend** en telt apart; een signaal zonder trade om op in te stappen binnen 10 minuten is **geen vulling**. "
        "Geen van beide telt ooit als winst of verlies.",
        f"- Intervallen: blokbootstrap met de UTC-dagen als blokken, {_n(cfg['bootstrap']['draws'])} trekkingen, vaste seed {cfg['bootstrap']['seed']}, alpha {_dec(cfg['bootstrap']['alpha'], 2)}.",
        f"- Walk-forward: parameters gekozen op de eerste helft van de dagen ({', '.join(cfg['selection_days'])}), getoetst op de tweede helft "
        f"({', '.join(cfg['test_days']) or 'GEEN: te weinig dagen'}). Bewezen = ondergrens van het 95%-interval van de verwachte waarde na kosten > 0 op de toetshelft, "
        f"minstens {cfg['proven']['min_cases']} gevallen, onbekend-aandeel onder {round(100 * cfg['proven']['max_unknown_share'])}%, mediane lead time ≥ {round(cfg['proven']['min_lead_s'] / 60)} minuten.",
        f"- {r.get('multiplicity_note')}",
        "",
        "## Oordeel",
        "",
    ]
    sel = r.get("selected")
    rows = {v["variant"]: v for v in r["variants"]}
    if sel is None:
        lines.append(f"**Geen variant gekozen**: geen variant had minstens {cfg['min_select_cases']} gevallen op de keuzehelft. Niets is bewezen; er wordt niets gepromoveerd.")
    else:
        v = rows[sel]
        t = v["test"]
        lines += [
            f"Gekozen op de keuzehelft (hoogste verwachte waarde): **{sel}** ({v['label']}). Op de toetshelft: verwachte waarde {_sol(t.get('ev_sol'))} per trade "
            f"({_pct(t.get('ev_pct'))}), interval {_ci(t.get('ci'))}, trefkans {_pct(t.get('hit_rate'), signed=False)}, {_n(t.get('cases'))} gevallen, onbekend {_n(t.get('unknown'))} "
            f"({_pct(t.get('unknown_share'), signed=False)}), geen vulling {_n(t.get('no_fill'))}, mediane lead time {_min(t.get('lead_median_s'))}.",
            "",
            f"**{_verdict_word(v['verdict'])}.**",
            "",
            "| criterium | waarde | toets |",
            "|---|---:|---|",
        ]
        for c in v["verdict"].get("criteria") or []:
            val = c["value"]
            key = c["key"]
            vs = _sol(val) if key == "ci_low_positive" else (_pct(val, signed=False) if key == "max_unknown" else (_min(val) if key == "min_lead" else _n(val)))
            lines.append(f"| {c['label']} | {vs} | {'voldaan' if c['ok'] else 'niet voldaan'} |")
        if v["verdict"].get("note"):
            lines.append(f"\n{v['verdict']['note']}")
        others = [p for p in r.get("proven_variants") or [] if p != sel]
        if others:
            lines += ["", f"Andere varianten met een positieve ondergrens op de toetshelft (niet vooraf gekozen, dus geen bewijs, wel ter inzage): {', '.join(others)}."]
    lines += ["", "## Alle varianten", "", "Per variant de keuzehelft, de toetshelft en het hele venster. Een lege toetshelft betekent te weinig dagen. Onbekend en geen vulling staan er altijd bij.", ""]
    lines += ["| variant | instap | uitstap | keuze: EV / n | toets: EV [interval] / n / onbekend / lead | heel venster: EV / trefkans / mediaan / p10 / p90 / max. drawdown / n | oordeel |",
              "|---|---|---|---|---|---|---|"]
    for v in r["variants"]:
        s, t, f = v["selection"], v["test"], v["full"]
        lines.append(
            f"| `{v['variant']}` | {_entry_label(v)} | {v['exit_label']} | {_sol(s.get('ev_sol'))} / {_n(s.get('cases'))} | "
            f"{_sol(t.get('ev_sol'))} {_ci(t.get('ci'))} / {_n(t.get('cases'))} / {_n(t.get('unknown'))} / {_min(t.get('lead_median_s'))} | "
            f"{_sol(f.get('ev_sol'))} / {_pct(f.get('hit_rate'), signed=False)} / {_pct(f.get('median_pct'))} / {_pct(f.get('p10_pct'))} / {_pct(f.get('p90_pct'))} / "
            f"{_sol_abs(f.get('max_drawdown_sol'))} / {_n(f.get('cases'))} (onb. {_n(f.get('unknown'))}, geen vulling {_n(f.get('no_fill'))}) | "
            f"{'**BEWEZEN**' if v['verdict'].get('proven') else ('gekozen, niet bewezen' if v['variant'] == sel else 'afgewezen')} |")
    groups = r.get("groups") or []
    if groups:
        lines += ["", "## Per instapgroep (heel venster, alle uitstapregels samen)", "", "| groep | signalen | gevallen | onbekend | geen vulling | EV per trade | trefkans | mediaan | lead mediaan | capaciteit (5% impact) |",
                  "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|"]
        for g in groups:
            lines.append(f"| {g['group']} | {_n(g['signals'])} | {_n(g['cases'])} | {_n(g['unknown'])} | {_n(g['no_fill'])} | {_sol(g['ev_sol'])} | {_pct(g['hit_rate'], signed=False)} | "
                         f"{_pct(g['median_pct'])} | {_min(g['lead_median_s'])} | {_sol_abs(g['capacity_median_sol'])} |")
    cond = r.get("conditioning") or {}
    if cond:
        lines += ["", f"## Conditionering van de gekozen variant `{sel}` (splitsing, geen aparte regels)", ""]
        for key, g in cond.items():
            lines += [f"**{g['label']}**", "", "| deel | signalen | gevallen | onbekend | EV per trade | trefkans | mediaan |", "|---|---:|---:|---:|---:|---:|---:|"]
            for row in g["rows"]:
                lines.append(f"| {row['label']} | {_n(row['signals'])} | {_n(row['cases'])} | {_n(row['unknown'])} | {_sol(row['ev_sol'])} | {_pct(row['hit_rate'], signed=False)} | {_pct(row['median_pct'])} |")
            lines.append("")
    sens = r.get("sensitivity") or {}
    if sens:
        lines += [f"## Gevoeligheid van de gekozen variant `{sel}`", "", "| instapvertraging | gevallen | onbekend | geen vulling | EV per trade | EV % | trefkans | mediaan |", "|---:|---:|---:|---:|---:|---:|---:|---:|"]
        for d in sens.get("entry_delay") or []:
            lines.append(f"| {d['entry_delay_s']:.0f} s | {_n(d['cases'])} | {_n(d['unknown'])} | {_n(d['no_fill'])} | {_sol(d['ev_sol'])} | {_pct(d['ev_pct'])} | {_pct(d['hit_rate'], signed=False)} | {_pct(d['median_pct'])} |")
        lines += ["", "| inzet | gevallen | EV per trade | EV % | trefkans | mediane prijsimpact bij instap |", "|---:|---:|---:|---:|---:|---:|"]
        for d in sens.get("size") or []:
            lines.append(f"| {_dec(d['size_sol'], 1)} SOL | {_n(d['cases'])} | {_sol(d['ev_sol'])} | {_pct(d['ev_pct'])} | {_pct(d['hit_rate'], signed=False)} | {_pct(d['entry_impact_median'], signed=False)} |")
    if r.get("cases_truncated"):
        lines += ["", f"De JSON naast dit rapport bewaart de eerste {_n(len(r.get('cases') or []))} van {_n(r.get('cases_total'))} gevallen; de cijfers hierboven zijn over alle gevallen berekend.", ""]
    lines += ["", "## Onbekend en geen vulling", "",
              "Een uitkomst is onbekend als het prijspad eindigt voordat een uitstapregel kon vullen (de munt verliet de watchlist, geen samples na graduatie, geen candles). "
              "Geen vulling: binnen 10 minuten na het signaal plus de vertraging was er geen trade om op in te stappen. Beide staan in elke tabel hierboven en tellen nooit mee als resultaat.", ""]
    exits = r.get("exit_reasons") or {}
    if exits:
        lines += ["Uitstapredenen over alle gevallen van het hele venster: " + ", ".join(f"{k} {v}" for k, v in exits.items()) + ".", ""]
    lines += ["## Eerlijkheid", "",
              "- Dit rapport is een terugrekening op onze eigen data: de trades die de radar zag (elke nieuwe munt zijn eerste 5 minuten, daarna de gevolgde munten), de prijssamples van de tracker en de candles van B.5. Wat de radar niet zag, is er niet.",
              "- De regels zijn vóór de run vastgelegd (fart/research/hypotheses.py); de keuze van de variant gebeurt op de keuzehelft, het oordeel op de toetshelft. Een variant die op de toetshelft toevallig goed uitkomt zonder vooraf gekozen te zijn is geen bewijs.",
              "- Elke getoetste variant staat in de tabel, ook de afgewezen. Een 'bewezen' regel is een meting over een korte periode met brede intervallen, geen belofte; de Plays-laag meet haar vanaf de promotie opnieuw, op papier, met dezelfde kosten.",
              "- Dit rapport is geen advies. Het platform plaatst nooit een order.", ""]
    return "\n".join(lines)


def _entry_label(v: dict[str, Any]) -> str:
    e = v.get("entry") or {}
    return ", ".join(f"{k}={val}" for k, val in e.items()) or "–"




# ----- writing --------------------------------------------------------------------------------------------------------------
def finalize(result: dict[str, Any], now: float) -> dict[str, Any]:
    result["generated_at"] = utc_iso(now)
    result["rules_version"] = rules_version()
    result["kind"] = "research"
    result["name"] = report_name(result["hypothesis"]["id"], now)
    return result


def write_report(settings: Settings, result: dict[str, Any], now: float) -> tuple[Path, Path]:
    result = finalize(result, now)
    d = report_dir(settings)
    d.mkdir(parents=True, exist_ok=True)
    js = d / f"{result['name']}.json"
    md = d / f"{result['name']}.md"
    atomic_write_json(js, result)
    atomic_write_text(md, render_markdown(result))
    return md, js


def list_reports(settings: Settings) -> list[dict[str, Any]]:
    d = report_dir(settings)
    if not d.exists():
        return []
    out = []
    for p in sorted(d.glob("*.json"), reverse=True):
        try:
            r = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(r, dict) or r.get("kind") not in ("research", "autopsy"):
            continue
        out.append({"name": p.stem, "kind": r.get("kind"), "hypothesis": (r.get("hypothesis") or {}).get("id") if r.get("kind") == "research" else None,
                    "generated_at": r.get("generated_at"), "days": (r.get("config") or {}).get("days") or r.get("days"), "selected": r.get("selected"),
                    "selected_proven": r.get("selected_proven"), "proven_variants": r.get("proven_variants") or [], "variants_tested": (r.get("config") or {}).get("variants_tested")})
    return out


def load_report(settings: Settings, name: str) -> dict[str, Any] | None:
    p = report_dir(settings) / f"{name}.json"
    if not p.is_file():
        return None
    try:
        r = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return r if isinstance(r, dict) else None


# ----- promotion ------------------------------------------------------------------------------------------------------------
class PromoteError(Exception):
    pass


def promote(settings: Settings, hypothesis: str, variant: str, now: float, report: str | None = None, plays_path: Path | None = None) -> dict[str, Any]:
    """Write a proven variant of a report into config/plays.json as a new rule version. Refuses an unproven variant, a
    variant that is not the report's pre-chosen one, or a variant without a report: honesty is the gate here."""
    hid = str(hypothesis).upper()
    hyp = hyps.get(hid)
    v = hyp.variant(variant)
    if v is None:
        raise PromoteError(f"onbekende variant {variant!r} van {hid}")
    reports = [r for r in list_reports(settings) if r["kind"] == "research" and r["hypothesis"] == hid]
    if report is None:
        if not reports:
            raise PromoteError(f"geen onderzoeksrapport voor {hid}: draai eerst `fart research run --hypothesis {hid}`")
        report = reports[0]["name"]
    r = load_report(settings, report)
    if r is None or (r.get("hypothesis") or {}).get("id") != hid:
        raise PromoteError(f"rapport {report!r} bestaat niet of hoort niet bij {hid}")
    row = next((x for x in r.get("variants") or [] if x["variant"] == variant), None)
    if row is None:
        raise PromoteError(f"variant {variant!r} staat niet in rapport {report}")
    if r.get("selected") != variant:
        raise PromoteError(f"variant {variant!r} is niet de vooraf gekozen variant van {report} (gekozen: {r.get('selected')}); een andere variant promoveren is kiezen op de toetshelft")
    if not (row.get("verdict") or {}).get("proven"):
        failed = [c["label"] for c in (row.get("verdict") or {}).get("criteria") or [] if not c.get("ok")]
        raise PromoteError(f"variant {variant!r} is niet bewezen in {report}: " + ("; ".join(failed) or "geen toetshelft"))
    path = plays_path or (settings.root / "config" / "plays.json")
    if not path.exists():
        from ..version import REPO_ROOT

        path = REPO_ROOT / "config" / "plays.json"
    cfg = json.loads(path.read_text(encoding="utf-8"))
    rules = list(cfg.get("rules") or [])
    version = 1 + max((int(x.get("version") or 0) for x in rules if x.get("hypothesis") == hid), default=0)
    t = row["test"]
    rule = {
        "id": f"{hid}-v{version}", "name": hyp.title_nl, "hypothesis": hid, "variant": variant, "version": version, "label": row["label"],
        "promoted_at": utc_iso(now), "report": report, "entry": dict(row["entry"]), "exit": row["exit"], "exit_rules": row["exit_rules"], "exit_text": row["exit_text"],
        "figures": {"ev_sol": t.get("ev_sol"), "ev_pct": t.get("ev_pct"), "ci_low_sol": (t.get("ci") or {}).get("low"), "ci_high_sol": (t.get("ci") or {}).get("high"),
                    "hit_rate": t.get("hit_rate"), "cases": t.get("cases"), "unknown_share": t.get("unknown_share"), "lead_median_s": t.get("lead_median_s"),
                    "capacity_median_sol": t.get("capacity_median_sol"), "test_days": r["config"]["test_days"], "size_sol": r["config"]["size_sol"],
                    "entry_delay_s": r["config"]["entry_delay_s"]},
        "active": True,
    }
    for old in rules:
        if old.get("hypothesis") == hid and old.get("active", True):
            old["active"] = False
            old["superseded_by"] = rule["id"]
    rules.append(rule)
    cfg["rules"] = rules
    path.write_text(json.dumps(cfg, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return rule
