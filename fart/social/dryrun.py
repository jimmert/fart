"""`fart social dryrun [--hours N]` (milestone 6): what the X watch would call and cost, without HTTP.

From config/x.json (intervals, prices, envelope, caps), the current confirmation keys (the observe /
observe_meta decisions of the last `confirm.window_s` that are still on the watchlist, exactly the keys
`fart-social` would count) and the posting volume (observed in `social_posts` over the last days when the
daemon ran, else the `dryrun` assumptions of config/x.json), it computes per kind the requests per day, the
billed units (posts, author expansions, requests) and the USD per day at the configured prices, the total
against `X_DAILY_BUDGET_USD`, and walks the next `--hours` hours through the hourly envelope, the 30%
account-watch reserve and the KOL sub-cap to show in which hours calls would be refused. Nothing is called,
nothing is written. The prices themselves are unverified (docs/UNVERIFIED.md, "X (Twitter) API v2").
"""
from __future__ import annotations

import datetime as _dt
import re
import sqlite3
from typing import Any

from ..util import day_key

KINDS = ("mega_search", "refresh", "kol_search", "ca_counts", "ca_search")
ACCOUNT_WATCH = ("mega_search", "kol_search", "refresh")
CONFIRM = ("ca_counts", "ca_search")
ORDER_IN_HOUR = ("mega_search", "refresh", "kol_search", "ca_counts", "ca_search")
DEFAULT_ASSUMPTIONS = {"mega_posts_per_day": 80, "kol_posts_per_day": 200, "ca_search_share": 0.1}


def accounts_in_query(query: str) -> int:
    return len(set(re.findall(r"from:([A-Za-z0-9_]+)", query or "")))


def observed_rates(conn: sqlite3.Connection, now: float, days: int = 7) -> dict[str, dict[str, Any]]:
    """Posts per day per post kind (mega, kol) over the last `days` days, counted over the days that had any
    post of that kind (a day the daemon was down is not a day with zero posts). Empty when nothing was stored."""
    out: dict[str, dict[str, Any]] = {}
    for r in conn.execute(
        "SELECT kind, COUNT(*) AS n, COUNT(DISTINCT CAST((received_at / 86400) AS INTEGER)) AS days FROM social_posts"
        " WHERE received_at >= ? AND kind IN ('mega', 'kol') GROUP BY kind", (now - days * 86400,)):
        if int(r["days"]) > 0:
            out[str(r["kind"])] = {"posts": int(r["n"]), "days": int(r["days"]), "posts_per_day": int(r["n"]) / int(r["days"])}
    return out


def spend_today_by_kind(conn: sqlite3.Connection, now: float) -> dict[str, float]:
    return {str(r[0]): float(r[1]) for r in conn.execute("SELECT kind, SUM(usd) FROM x_spend WHERE day=? GROUP BY kind", (day_key(now),))}


def per_kind(cfg: dict[str, Any], n_keys: int, n_kols: int, rates: dict[str, dict[str, Any]], assumptions: dict[str, Any]) -> list[dict[str, Any]]:
    """Requests, billed units and USD per day per kind (steady state, a full UTC day)."""
    prices = cfg.get("prices", {})
    p_post, p_counts, p_author, p_refresh = (float(prices.get(k, 0.0)) for k in ("post_usd", "counts_request_usd", "author_expansion_usd", "refresh_post_usd"))
    mcfg, kcfg, ccfg, env = cfg["mega"], cfg["kol"], cfg["confirm"], cfg.get("envelope", {})
    loop = float(cfg.get("loop_interval_s", 15))
    mega_accounts = accounts_in_query(str(mcfg.get("query", "")))
    if "mega" in rates:
        mega_posts, mega_src = float(rates["mega"]["posts_per_day"]), f"waargenomen over {rates['mega']['days']} dag(en)"
    else:
        mega_posts, mega_src = float(assumptions.get("mega_posts_per_day", 80)), "aanname (config/x.json -> dryrun)"
    if "kol" in rates:
        kol_posts, kol_src = float(rates["kol"]["posts_per_day"]), f"waargenomen over {rates['kol']['days']} dag(en)"
    else:
        kol_posts, kol_src = (float(assumptions.get("kol_posts_per_day", 200)) if n_kols else 0.0), "aanname (config/x.json -> dryrun)"
    share = float(assumptions.get("ca_search_share", 0.1))
    rows: list[dict[str, Any]] = []
    # mega: one search per interval; cost = new posts x post price + new authors x expansion price (once per UTC day each)
    req = 86400.0 / float(mcfg.get("interval_s", 60))
    authors = min(float(mega_accounts), mega_posts)
    rows.append({"kind": "mega_search", "interval_s": float(mcfg.get("interval_s", 60)), "requests_per_day": req,
                 "units": {"posts": mega_posts, "authors": authors}, "usd_per_day": mega_posts * p_post + authors * p_author,
                 "basis": f"{mega_posts:.0f} posts/dag ({mega_src}), {mega_accounts} accounts in de query", "hard_cap": None})
    # refresh: a re-read of the posts younger than refresh_max_age_s every refresh_interval_s (assumed free: VERIFY)
    rreq = 86400.0 / float(mcfg.get("refresh_interval_s", 120))
    per_req = min(100.0, mega_posts * float(mcfg.get("refresh_max_age_s", 7200)) / 86400.0)
    rows.append({"kind": "refresh", "interval_s": float(mcfg.get("refresh_interval_s", 120)), "requests_per_day": rreq,
                 "units": {"posts": rreq * per_req}, "usd_per_day": rreq * per_req * p_refresh,
                 "basis": f"{per_req:.1f} posts per verversing (jonger dan {float(mcfg.get('refresh_max_age_s', 7200)) / 3600:.0f} u); "
                          f"prijs {p_refresh:g} per post (aangenomen gratis: VERIFY)", "hard_cap": None})
    # kol: the batched from: query; the KOL sub-cap bounds the day
    kreq = 86400.0 / float(kcfg.get("interval_s", 180)) if n_kols else 0.0
    kauthors = min(float(n_kols), kol_posts)
    kcost = kol_posts * p_post + kauthors * p_author
    kol_cap = float(env.get("kol_daily_cap_usd", 1.0))
    rows.append({"kind": "kol_search", "interval_s": float(kcfg.get("interval_s", 180)), "requests_per_day": kreq,
                 "units": {"posts": kol_posts, "authors": kauthors}, "usd_per_day": kcost,
                 "basis": f"{kol_posts:.0f} posts/dag ({kol_src}), {n_kols} KOL-accounts", "hard_cap": kol_cap,
                 "note": f"KOL-plafond ${kol_cap:.2f}/dag: {'bereikt' if kcost > kol_cap else 'niet bereikt'} (ongeremd ${kcost:.2f})"})
    # confirmation counts: one counts/recent per key per interval, at most max_keys_per_loop per loop
    creq_wanted = float(n_keys) * 86400.0 / float(ccfg.get("interval_s", 900))
    creq_max = float(ccfg.get("max_keys_per_loop", 4)) * 86400.0 / loop
    creq = min(creq_wanted, creq_max)
    rows.append({"kind": "ca_counts", "interval_s": float(ccfg.get("interval_s", 900)), "requests_per_day": creq,
                 "units": {"requests": creq}, "usd_per_day": creq * p_counts,
                 "basis": f"{n_keys} sleutels nu (observaties van de laatste {float(ccfg.get('window_s', 21600)) / 3600:.0f} u op de watchlist en "
                          f"thema-termen), {86400.0 / float(ccfg.get('interval_s', 900)):.0f} tellingen per sleutel per dag"
                          + (f"; begrensd door max_keys_per_loop ({creq_max:.0f}/dag)" if creq_wanted > creq_max else ""), "hard_cap": None,
                 "per_key_usd_per_day": 86400.0 / float(ccfg.get("interval_s", 900)) * p_counts})
    # confirmation searches: a share of the counts reaches search_when_count_1h; upper bound: every post and author new
    smax = float(ccfg.get("search_max_results", 10))
    sreq = creq * share
    rows.append({"kind": "ca_search", "interval_s": None, "requests_per_day": sreq,
                 "units": {"posts": sreq * smax, "authors": sreq * smax}, "usd_per_day": sreq * smax * (p_post + p_author),
                 "basis": f"aandeel {share:.0%} van de tellingen haalt {int(ccfg.get('search_when_count_1h', 10))} posts/uur (aanname); "
                          f"bovengrens {smax:.0f} nieuwe posts en auteurs per zoekopdracht", "hard_cap": None})
    return rows


def envelope_weights(cfg: dict[str, Any]) -> list[float]:
    env = cfg.get("envelope", {})
    peak = {int(h) for h in env.get("peak_hours_utc", [])}
    return [float(env.get("peak_weight", 1.0)) if h in peak else float(env.get("off_weight", 0.4)) for h in range(24)]


def simulate_hours(cfg: dict[str, Any], cap: float, rows: list[dict[str, Any]], now: float, hours: int, spend_today: dict[str, float] | None = None,
                   arrival: str = "uniform") -> list[dict[str, Any]]:
    """Walk the next `hours` UTC hours from the current one through the daemon's own refusal rules (XBudget.allow):
    hard cap, hourly envelope (cumulative share of the cap through the hour), account-watch reserve (confirmation
    kinds may use at most 1 - reserve of the cap), KOL sub-cap. The daily cost of a kind arrives uniformly over
    the day (`arrival` "uniform") or in proportion to the envelope weights ("envelope"); a refused search
    (mega, KOL) is carried to the next hour (since_id picks the posts up later), a refused count is lost."""
    env = cfg.get("envelope", {})
    weights = envelope_weights(cfg)
    total_w = sum(weights) or 1.0
    reserve = float(env.get("account_watch_reserve_fraction", 0.30))
    kol_cap = float(env.get("kol_daily_cap_usd", 1.0))
    peak = {int(h) for h in env.get("peak_hours_utc", [])}
    daily = {r["kind"]: float(r["usd_per_day"]) for r in rows}
    spent = {k: 0.0 for k in KINDS}
    for k, v in (spend_today or {}).items():
        if k in spent:
            spent[k] = float(v)
    backlog = {k: 0.0 for k in KINDS}
    t0 = _dt.datetime.fromtimestamp(now, tz=_dt.timezone.utc).replace(minute=0, second=0, microsecond=0)
    out: list[dict[str, Any]] = []
    for i in range(int(hours)):
        t = t0 + _dt.timedelta(hours=i)
        h = t.hour
        if i > 0 and h == 0:
            spent = {k: 0.0 for k in KINDS}  # a new UTC day: the ledger starts at zero, the backlog stays
        profile = (weights[h] / total_w) if arrival == "envelope" else (1.0 / 24.0)
        allowance = cap * sum(weights[: h + 1]) / total_w
        refused: dict[str, str] = {}
        refused_usd = 0.0
        for kind in ORDER_IN_HOUR:
            want = daily.get(kind, 0.0) * profile + backlog[kind]
            backlog[kind] = 0.0
            if want <= 0:
                continue
            total = sum(spent.values())
            limits = [("daily_cost_cap", cap - total), ("hourly_envelope", allowance - total)]
            if kind in CONFIRM:
                limits.append(("account_watch_reserve", cap * (1.0 - reserve) - sum(spent[k] for k in CONFIRM)))
            if kind == "kol_search":
                limits.append(("kol_daily_cap", kol_cap - spent["kol_search"]))
            reason, room = min(limits, key=lambda x: x[1])
            room = max(0.0, room)
            allowed = min(want, room)
            if allowed < want - 1e-9:
                refused[kind] = reason
                refused_usd += want - allowed
                if kind in ("mega_search", "kol_search"):
                    backlog[kind] = want - allowed
            spent[kind] += allowed
        out.append({"hour": t.strftime("%Y-%m-%dT%H"), "h": h, "peak": h in peak, "allowance_usd": round(allowance, 4),
                    "spent_usd": round(sum(spent.values()), 4), "refused_usd": round(refused_usd, 4), "refused": refused,
                    "backlog_usd": round(sum(backlog.values()), 4)})
    return out


def build(conn: sqlite3.Connection, settings, now: float, hours: int = 24, arrival: str = "uniform") -> dict[str, Any]:
    from ..engine.meta import load_stopwords
    from .x import confirmation_candidates

    cfg = settings.config("x")
    cap = float(settings.x_daily_budget_usd)
    keys = confirmation_candidates(conn, now, cfg["confirm"], load_stopwords(settings.config("stopwords")))
    n_kols = len(settings.config("x_kols").get("accounts", []))
    rates = observed_rates(conn, now)
    assumptions = {**DEFAULT_ASSUMPTIONS, **{k: v for k, v in (cfg.get("dryrun") or {}).items() if not k.startswith("_")}}
    rows = per_kind(cfg, len(keys), n_kols, rates, assumptions)
    total = sum(r["usd_per_day"] for r in rows)
    total_capped = sum(min(r["usd_per_day"], r["hard_cap"]) if r.get("hard_cap") is not None else r["usd_per_day"] for r in rows)
    sim = simulate_hours(cfg, cap, rows, now, hours, spend_today_by_kind(conn, now), arrival)
    env = cfg.get("envelope", {})
    return {
        "generated_at": _dt.datetime.fromtimestamp(now, tz=_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "x_mode": settings.x_mode,
        "cap_usd": cap,
        "prices": {k: v for k, v in cfg.get("prices", {}).items() if not k.startswith("_")},
        "envelope": {"peak_hours_utc": env.get("peak_hours_utc"), "peak_weight": env.get("peak_weight"), "off_weight": env.get("off_weight"),
                     "account_watch_reserve_fraction": env.get("account_watch_reserve_fraction"), "kol_daily_cap_usd": env.get("kol_daily_cap_usd")},
        "confirmation_keys": {"n": len(keys), "mint": sum(1 for k in keys if k["kind"] == "mint"), "meta": sum(1 for k in keys if k["kind"] == "meta"),
                              "sample": [k["key"] for k in keys[:8]]},
        "observed_rates": rates,
        "assumptions": assumptions,
        "kinds": rows,
        "total_usd_per_day": total,
        "total_usd_per_day_after_caps": total_capped,
        "fits_cap": total_capped <= cap,
        "arrival": arrival,
        "hours": sim,
        "throttled_hours": [s["hour"] for s in sim if s["refused"]],
        "throttled_peak_hours": [s["hour"] for s in sim if s["refused"] and s["peak"]],
        "spend_today_usd": sum(spend_today_by_kind(conn, now).values()),
        "notes": ["Geen netwerk: niets is aangeroepen of geschreven.",
                  "Prijzen uit config/x.json zijn niet geverifieerd (docs/UNVERIFIED.md, 'X (Twitter) API v2').",
                  "Posts en auteurs worden één keer per UTC-dag gefactureerd (x_dedupe); de schatting telt elke post en auteur één keer.",
                  "ca_search is een bovengrens (elke post en auteur nieuw); refresh is aangenomen gratis."],
    }


def render_text(d: dict[str, Any]) -> str:
    cap = d["cap_usd"]
    out = [f"fart social dryrun  ({d['generated_at']}, X_MODE {d['x_mode']}, cap ${cap:.2f}/day, arrival {d['arrival']}; no HTTP, nothing written)",
           f"  prices: " + ", ".join(f"{k} {v}" for k, v in d["prices"].items()),
           f"  envelope: peak hours {d['envelope']['peak_hours_utc']} weight {d['envelope']['peak_weight']} / off {d['envelope']['off_weight']}; "
           f"confirmation kinds <= {100 * (1 - float(d['envelope']['account_watch_reserve_fraction'] or 0)):.0f}% of the cap; KOL cap ${float(d['envelope']['kol_daily_cap_usd'] or 0):.2f}",
           f"  confirmation keys now: {d['confirmation_keys']['n']} ({d['confirmation_keys']['mint']} mints, {d['confirmation_keys']['meta']} themes)"
           + (": " + ", ".join(k[:14] for k in d["confirmation_keys"]["sample"]) if d["confirmation_keys"]["sample"] else ""),
           "",
           f"  {'kind':<12}{'interval':>9}{'req/day':>9}{'units/day':>26}{'USD/day':>9}{'% cap':>7}  basis"]
    for r in d["kinds"]:
        units = ", ".join(f"{k} {v:.0f}" for k, v in r["units"].items())
        iv = "-" if r["interval_s"] is None else f"{r['interval_s']:.0f} s"
        out.append(f"  {r['kind']:<12}{iv:>9}{r['requests_per_day']:>9.0f}{units:>26}{r['usd_per_day']:>9.2f}{100 * r['usd_per_day'] / cap if cap else 0:>6.0f}%  {r['basis']}"
                   + (f"; {r['note']}" if r.get("note") else ""))
    pk = next((r for r in d["kinds"] if r["kind"] == "ca_counts"), {})
    out += [f"  {'total':<12}{'':>9}{'':>9}{'':>26}{d['total_usd_per_day']:>9.2f}{100 * d['total_usd_per_day'] / cap if cap else 0:>6.0f}%  "
            f"after the KOL cap ${d['total_usd_per_day_after_caps']:.2f} -> {'fits' if d['fits_cap'] else 'EXCEEDS'} the cap of ${cap:.2f}"
            + (f"; each extra confirmation key costs ${pk['per_key_usd_per_day']:.2f}/day" if pk.get("per_key_usd_per_day") is not None else ""),
            f"  observed rates: " + (", ".join(f"{k} {v['posts_per_day']:.0f} posts/day over {v['days']} day(s)" for k, v in d["observed_rates"].items()) or "none (assumptions used)"),
            f"  spent today so far: ${d['spend_today_usd']:.2f}",
            "",
            f"  {'hour (UTC)':<15}{'peak':>5}{'allowance':>11}{'spent':>9}{'refused':>9}{'backlog':>9}  refused kinds (reason)"]
    for s in d["hours"]:
        ref = ", ".join(f"{k} ({v})" for k, v in s["refused"].items())
        out.append(f"  {s['hour']:<15}{'*' if s['peak'] else '':>5}{s['allowance_usd']:>11.2f}{s['spent_usd']:>9.2f}{s['refused_usd']:>9.2f}{s['backlog_usd']:>9.2f}  {ref}")
    out.append(f"  hours with refused calls: {len(d['throttled_hours'])} of {len(d['hours'])}; in peak hours: {len(d['throttled_peak_hours'])}"
               + (" (" + ", ".join(h[-2:] for h in d["throttled_peak_hours"]) + ")" if d["throttled_peak_hours"] else ""))
    out += [f"  note: {n}" for n in d["notes"]]
    return "\n".join(out)
