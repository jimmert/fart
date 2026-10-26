"""`fart social calibrate --day D --console-usd N` (milestone 7): which price per unit fits a day's console amount.

Our ledger (`x_spend`) books every X call at the configured prices (config/x.json -> prices) with the unit it counted
(milestone 7: `post` for search results, `request` for counts, `author` for user objects, the refresh posts at the
refresh price). The X developer console shows what the day really cost. The command puts the two next to each other,
per kind and per price component, and prints what follows: when the console amount is within 5% of ours the prices
stand (write the console figures into docs/UNVERIFIED.md and tick the item); otherwise either every price is off by
the same factor (the uniform hypothesis: console / booked) or one component is priced wrongly (one hypothesis per
component: the price that would make our total equal the console's if the other components are right). The console's
per-endpoint breakdown decides which hypothesis is true; the command cannot. It writes nothing: a changed price is a
commit of config/x.json (not a rule file: the rules fingerprint stays). Rows written before milestone 7 carry no unit
(`mixed`) and count in the totals but not in the per-component hypotheses.
"""
from __future__ import annotations

import sqlite3
from typing import Any

from ..util import day_bounds

# price component -> config key, and the (kind, unit) pairs that are booked at it
COMPONENTS: dict[str, dict[str, Any]] = {
    "post": {"price_key": "post_usd", "label": "posts returned by search/recent (mega, KOL, confirmation)",
             "pairs": [("mega_search", "post"), ("kol_search", "post"), ("ca_search", "post")]},
    "request": {"price_key": "counts_request_usd", "label": "counts/recent requests", "pairs": [("ca_counts", "request")]},
    "author": {"price_key": "author_expansion_usd", "label": "user objects (author expansions and /2/users lookups)",
               "pairs": [("mega_search", "author"), ("kol_search", "author"), ("ca_authors", "author")]},
    "refresh": {"price_key": "refresh_post_usd", "label": "posts re-read by GET /2/tweets?ids= (assumed free)", "pairs": [("refresh", "post")]},
}
TOLERANCE = 0.05


def ledger_by_kind_unit(conn: sqlite3.Connection, day: str) -> list[dict[str, Any]]:
    start, end = day_bounds(day)
    rows = [dict(r) for r in conn.execute(
        "SELECT kind, unit, COUNT(*) AS rows, COALESCE(SUM(n), 0) AS n, COALESCE(SUM(usd), 0) AS usd FROM x_spend WHERE day=? GROUP BY kind, unit ORDER BY kind, unit",
        (day,))]
    calls = {str(r["method"]): {"calls": int(r["calls"]), "usd": float(r["usd"]), "errors": int(r["errors"])} for r in conn.execute(
        "SELECT method, SUM(ok=1 AND blocked_reason IS NULL) AS calls, COALESCE(SUM(CASE WHEN ok=1 AND blocked_reason IS NULL THEN cost END), 0) AS usd,"
        " SUM(ok=0) AS errors FROM api_ledger WHERE source='x' AND ts >= ? AND ts < ? GROUP BY method", (start, end))}
    for r in rows:
        r["calls"] = (calls.get(str(r["kind"])) or {}).get("calls", 0)
    return rows


def component_of(kind: str, unit: str | None) -> str | None:
    if unit is None:
        return None
    for name, c in COMPONENTS.items():
        if (kind, unit) in c["pairs"]:
            return name
    return None


def round_price(x: float) -> float:
    return round(float(x), 4)


def calibrate(conn: sqlite3.Connection, settings, day: str, console_usd: float) -> dict[str, Any]:
    prices = {k: float(v) for k, v in settings.config("x").get("prices", {}).items() if not k.startswith("_")}
    rows = ledger_by_kind_unit(conn, day)
    comps: dict[str, dict[str, Any]] = {name: {"units": 0, "booked_usd": 0.0, "price_key": c["price_key"], "price": prices.get(c["price_key"], 0.0), "label": c["label"]}
                                        for name, c in COMPONENTS.items()}
    mixed_usd = 0.0
    mixed_units = 0
    for r in rows:
        comp = component_of(str(r["kind"]), r["unit"])
        r["component"] = comp
        if comp is None:
            mixed_usd += float(r["usd"])
            mixed_units += int(r["n"])
            continue
        comps[comp]["units"] += int(r["n"])
        comps[comp]["booked_usd"] += float(r["usd"])
    booked = sum(c["booked_usd"] for c in comps.values()) + mixed_usd
    out: dict[str, Any] = {"day": day, "console_usd": float(console_usd), "rows": rows, "components": comps, "mixed": {"usd": mixed_usd, "units": mixed_units},
                           "booked_usd": booked, "prices": prices, "cap_usd": float(settings.x_daily_budget_usd)}
    if booked <= 0:
        out["verdict"] = "nothing_booked"
        out["note"] = ("our ledger has no X spend for this day: the console amount cannot be attributed (was fart-social running with a token? "
                       "is the day the console's UTC day?)")
        return out
    gap = float(console_usd) - booked
    out["gap_usd"] = gap
    out["gap_rel"] = gap / booked
    out["uniform_factor"] = float(console_usd) / booked
    hyp: list[dict[str, Any]] = []
    for name, c in comps.items():
        if c["units"] <= 0:
            continue
        other = booked - c["booked_usd"]
        implied = (float(console_usd) - other) / c["units"]
        hyp.append({"component": name, "price_key": c["price_key"], "units": c["units"], "current_price": c["price"], "implied_price": implied,
                    "implied_price_rounded": round_price(max(0.0, implied)), "label": c["label"],
                    "config_line": f'"{c["price_key"]}": {round_price(max(0.0, implied)):g}'})
    out["single_component_hypotheses"] = hyp
    out["uniform_config_lines"] = [f'"{c["price_key"]}": {round_price(c["price"] * out["uniform_factor"]):g}' for c in comps.values() if c["units"] > 0]
    if abs(out["gap_rel"]) <= TOLERANCE:
        out["verdict"] = "confirmed"
        out["note"] = (f"the console amount is within {TOLERANCE:.0%} of our ledger: the prices stand; write the console figures (day, amount, per endpoint when "
                       f"the console shows them) into docs/UNVERIFIED.md and tick the item; no commit needed")
    else:
        out["verdict"] = "adjust"
        out["note"] = ("the console amount differs from our ledger by more than 5%: a commit of config/x.json -> prices is needed. Which lines depends on the "
                       "console's per-endpoint breakdown: every price times the uniform factor when every endpoint is off alike, else the one component "
                       "whose implied price explains the gap (a unit that differs from ours, e.g. per request instead of per post, is a code change in "
                       "fart/social/x.py, not a price change)")
    return out


def render_text(c: dict[str, Any]) -> str:
    out = [f"fart social calibrate --day {c['day']} --console-usd {c['console_usd']:.2f}  (reads x_spend and api_ledger, writes nothing)",
           f"  {'kind':<13}{'unit':<9}{'component':<10}{'rows':>6}{'units':>8}{'calls':>7}{'our USD':>10}"]
    for r in c["rows"]:
        out.append(f"  {r['kind']:<13}{str(r['unit'] or 'mixed'):<9}{str(r.get('component') or '-'):<10}{r['rows']:>6}{int(r['n']):>8}{int(r.get('calls') or 0):>7}{float(r['usd']):>10.4f}")
    out.append(f"  booked by us: ${c['booked_usd']:.4f}" + (f" (of which ${c['mixed']['usd']:.4f} in {c['mixed']['units']} pre-M7 rows without a unit)" if c["mixed"]["usd"] else "")
               + f"; console: ${c['console_usd']:.4f}; cap ${c['cap_usd']:.2f}")
    if c.get("verdict") == "nothing_booked":
        out.append(f"  {c['note']}")
        return "\n".join(out)
    out.append(f"  gap: ${c['gap_usd']:+.4f} ({100 * c['gap_rel']:+.1f}%); uniform factor console / booked = {c['uniform_factor']:.3f}")
    out.append(f"  {'component':<10}{'units':>8}{'price now':>11}{'implied if only this one is wrong':>36}  config/x.json -> prices")
    for h in c["single_component_hypotheses"]:
        out.append(f"  {h['component']:<10}{h['units']:>8}{h['current_price']:>11.4f}{h['implied_price']:>36.5f}  {h['config_line']}")
    out.append("  uniform hypothesis (every price x factor): " + ", ".join(c["uniform_config_lines"]))
    out.append(f"  verdict: {c['verdict']}: {c['note']}")
    return "\n".join(out)
