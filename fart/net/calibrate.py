"""`fart budget calibrate --day D --dashboard-cu N` (milestone 6): the implied CU per byte of the websocket frames.

Our ledger (`api_ledger`, source `rpc`) books every HTTP method at the published Alchemy compute-unit figure
of config/budget.json -> rpc.cost_table and every websocket frame as bytes x `rpc.cu_per_byte` (0.0002, with
0.04 as the unreconciled alternative; docs/UNVERIFIED.md). The one number the dashboard can settle is the
factor: implied cu_per_byte = (dashboard CU of the day - our CU of the HTTP methods at the published
rates) / our bytes of the byte-priced methods. The command prints the ledger per method, that factor, the
documented candidate it is closest to, what the day would have cost at the implied factor and at the
current one, and the config line that would follow. It writes nothing: the change is a commit of
config/budget.json (not a rule file: the rules fingerprint stays).
"""
from __future__ import annotations

import sqlite3
from typing import Any

from ..util import day_bounds

CANDIDATES = (0.0, 0.0002, 0.04)


def round_sig(x: float, digits: int = 2) -> float:
    if x == 0:
        return 0.0
    from math import floor, log10

    return round(x, -int(floor(log10(abs(x)))) + (digits - 1))


def ledger_by_method(conn: sqlite3.Connection, source: str, day: str) -> list[dict[str, Any]]:
    start, end = day_bounds(day)
    return [dict(r) for r in conn.execute(
        "SELECT method, COUNT(*) AS rows, COALESCE(SUM(cost), 0) AS cu, COALESCE(SUM(bytes), 0) AS bytes, SUM(bytes IS NOT NULL) AS byte_rows"
        " FROM api_ledger WHERE source=? AND blocked_reason IS NULL AND ts >= ? AND ts < ? GROUP BY method ORDER BY cu DESC", (source, start, end))]


def calibrate(conn: sqlite3.Connection, settings, day: str, dashboard_cu: float) -> dict[str, Any]:
    source = "rpc_public" if settings.rpc_is_public else "rpc"
    src = settings.config("budget")["sources"][source]
    table = {k: float(v) for k, v in (src.get("cost_table") or {}).items()}
    default = float(src.get("cost_default", 20))
    current = src.get("cu_per_byte")
    byte_priced = set(src.get("byte_priced") or [])
    rows = ledger_by_method(conn, source, day)
    http_cu_published = 0.0
    http_cu_booked = 0.0
    ws_bytes = 0
    ws_cu_booked = 0.0
    methods = []
    for r in rows:
        m = str(r["method"])
        if m in byte_priced:
            ws_bytes += int(r["bytes"] or 0)
            ws_cu_booked += float(r["cu"])
            methods.append({**r, "kind": "websocket", "published_cu": None})
        else:
            pub = table.get(m, default) * int(r["rows"])
            http_cu_published += pub
            http_cu_booked += float(r["cu"])
            methods.append({**r, "kind": "http", "published_cu": pub, "published_per_call": table.get(m, default), "in_table": m in table})
    out: dict[str, Any] = {
        "day": day, "source": source, "dashboard_cu": float(dashboard_cu), "methods": methods,
        "http_cu_published": http_cu_published, "http_cu_booked": http_cu_booked, "ws_bytes": ws_bytes, "ws_cu_booked": ws_cu_booked,
        "cu_per_byte_current": current, "cap": None if settings.rpc_is_public else float(settings.rpc_daily_budget_cu),
    }
    remainder = float(dashboard_cu) - http_cu_published
    out["ws_cu_dashboard"] = remainder
    if ws_bytes <= 0:
        out["implied_cu_per_byte"] = None
        out["note"] = "no websocket bytes in the ledger for this day: nothing to calibrate (was the keyed RPC stream running?)"
        return out
    implied = max(0.0, remainder) / ws_bytes
    out["implied_cu_per_byte"] = implied
    out["implied_cu_per_byte_rounded"] = round_sig(implied, 2)
    if remainder <= 0:
        out["note"] = ("the dashboard total does not exceed the HTTP methods at the published rates: no websocket CU is billed; "
                       "set rpc.cu_per_byte to 0 in a commit and write the dashboard figures into docs/UNVERIFIED.md")
    nearest = min(CANDIDATES, key=lambda c: abs(c - implied))
    out["nearest_candidate"] = nearest
    out["nearest_candidate_rel_diff"] = None if nearest == 0 else abs(implied - nearest) / nearest
    suggested = nearest if (nearest == 0 and implied <= 1e-6) or (nearest and abs(implied - nearest) / nearest <= 0.15) else out["implied_cu_per_byte_rounded"]
    out["suggested_cu_per_byte"] = suggested
    out["suggestion_basis"] = ("a documented candidate within 15%" if suggested == nearest else "the measured factor rounded to two significant digits")
    out["per_day"] = {
        "ws_cu_at_implied": ws_bytes * implied, "ws_cu_at_current": None if current is None else ws_bytes * float(current),
        "total_cu_at_implied": http_cu_published + ws_bytes * implied, "total_cu_booked": http_cu_booked + ws_cu_booked,
        "cap": out["cap"], "share_of_cap_at_implied": None if not out["cap"] else (http_cu_published + ws_bytes * implied) / out["cap"],
    }
    tcfg = settings.config("ingest").get("rpc_trades", {})
    share = float(tcfg.get("daily_cu_share", 0.35))
    trade_bytes = sum(int(r["bytes"] or 0) for r in rows if str(r["method"]).startswith("ws_trade_"))
    out["trade_stream"] = {"bytes": trade_bytes, "cu_at_implied": trade_bytes * implied, "daily_share": share,
                           "share_allowance_cu": None if not out["cap"] else share * out["cap"],
                           "fits_share": None if not out["cap"] else trade_bytes * implied <= share * out["cap"]}
    out["config_line"] = f'"cu_per_byte": {suggested:g}'
    return out


def render_text(c: dict[str, Any]) -> str:
    out = [f"fart budget calibrate --day {c['day']} --dashboard-cu {c['dashboard_cu']:,.0f}  (source {c['source']}; reads api_ledger, writes nothing)",
           f"  {'method':<26}{'kind':<10}{'rows':>7}{'our CU':>12}{'bytes':>14}{'published CU':>14}"]
    for m in c["methods"]:
        pub = "" if m["published_cu"] is None else f"{m['published_cu']:,.0f}" + ("" if m.get("in_table") else " (default)")
        out.append(f"  {m['method']:<26}{m['kind']:<10}{m['rows']:>7}{m['cu']:>12,.0f}{int(m['bytes'] or 0):>14,}{pub:>14}")
    out += [f"  HTTP methods at the published rates: {c['http_cu_published']:,.0f} CU (booked {c['http_cu_booked']:,.0f}); websocket bytes: {c['ws_bytes']:,} "
            f"(booked {c['ws_cu_booked']:,.0f} CU at cu_per_byte {c['cu_per_byte_current']})",
            f"  dashboard {c['dashboard_cu']:,.0f} CU - HTTP {c['http_cu_published']:,.0f} = {c['ws_cu_dashboard']:,.0f} CU for the websocket frames"]
    if c.get("implied_cu_per_byte") is None:
        out.append(f"  {c.get('note')}")
        return "\n".join(out)
    out.append(f"  implied CU per byte: {c['implied_cu_per_byte']:.6g} (rounded {c['implied_cu_per_byte_rounded']:g}); nearest documented value {c['nearest_candidate']:g}"
               + (f" ({100 * c['nearest_candidate_rel_diff']:.0f}% off)" if c.get("nearest_candidate_rel_diff") is not None else ""))
    if c.get("note"):
        out.append(f"  {c['note']}")
    pd = c["per_day"]
    out.append(f"  per day at the implied factor: websocket {pd['ws_cu_at_implied']:,.0f} CU + HTTP {c['http_cu_published']:,.0f} = {pd['total_cu_at_implied']:,.0f} CU"
               + (f" = {100 * pd['share_of_cap_at_implied']:.0f}% of the cap {pd['cap']:,.0f}" if pd.get("cap") else "")
               + (f"; booked today {pd['total_cu_booked']:,.0f} CU (websocket {pd['ws_cu_at_current']:,.0f} at {c['cu_per_byte_current']})" if pd.get("ws_cu_at_current") is not None else ""))
    ts = c["trade_stream"]
    if ts.get("share_allowance_cu") is not None:
        out.append(f"  trade stream (ws_trade_*): {ts['bytes']:,} bytes = {ts['cu_at_implied']:,.0f} CU at the implied factor against its daily share "
                   f"{ts['daily_share']:.0%} = {ts['share_allowance_cu']:,.0f} CU: {'fits' if ts['fits_share'] else 'does NOT fit (the envelope would pause it)'}")
    out.append(f"  config/budget.json -> sources.{c['source']}: {c['config_line']}   ({c['suggestion_basis']}; a commit, not a rule file: the rules fingerprint stays)")
    out.append("  write the dashboard figures (day, CU per method, websocket CU, our bytes, the factor) into docs/UNVERIFIED.md")
    return "\n".join(out)
