"""`fart social dryrun [--hours N] [--arrival uniform|envelope]`: what the X watch would call and cost, without HTTP.

From config/x.json (intervals, prices, envelope, caps, the planner), the current confirmation keys (the observe /
observe_meta decisions of the last `confirm.window_s` that are still on the watchlist, exactly the candidates the
planner would admit) and the observed rates (posting volume in `social_posts`, posts and authors per confirmation
search in `x_spend`, keys per day and confirmed share in `x_keys`, over the last 7 days when the daemon ran; else the
`dryrun` assumptions of config/x.json), it computes per kind the requests per day, the billed units and the USD per day
at the configured prices, the total against `X_DAILY_BUDGET_USD`, the **full-load ceiling** of the planner (milestone
7: `planner.full_load_keys_per_day` keys a day, every key its maximum of counts and its one search; the formula is in
docs/UNVERIFIED.md), and walks the next `--hours` hours through the planner's own rules (hard cap, account-watch
reserve, KOL sub-cap, the hourly envelope that defers instead of refusing). Nothing is called, nothing is written. The
prices themselves are unverified (docs/UNVERIFIED.md, "X (Twitter) API v2").
"""
from __future__ import annotations

import datetime as _dt
import sqlite3
from typing import Any

from ..util import day_key
from . import planner
from .x import accounts_in_query, interval_for  # noqa: F401  (accounts_in_query re-exported for callers of the milestone-6 name)

KINDS = ("mega_search", "refresh", "kol_search", "ca_counts", "ca_search", "ca_authors")
ACCOUNT_WATCH = ("mega_search", "kol_search", "refresh")
CONFIRM = ("ca_counts", "ca_search", "ca_authors")
# the planner's admission order inside an hour: the search of a confirmed key first, then its author lookup, then the
# counts, then the account watches (the mega search is nearly free after the dedupe), the free refresh last
ORDER_IN_HOUR = ("ca_search", "ca_authors", "ca_counts", "mega_search", "kol_search", "refresh")
DEFERRED = "hourly_envelope"
DEFAULT_ASSUMPTIONS: dict[str, Any] = {"mega_posts_per_day": 80, "kol_posts_per_day": 200, "keys_per_day": None, "confirmed_share": 0.5,
                                       "counts_per_confirmed_key": 4, "posts_per_search": 10, "authors_per_search": 3}
# observed rates replace an assumption only from this many stopped keys / searches on (one refuted key is not a confirmed share of 0%)
MIN_STOPPED_FOR_RATES = 5
MIN_SEARCHES_FOR_RATES = 3


def observed_rates(conn: sqlite3.Connection, now: float, days: int = 7) -> dict[str, dict[str, Any]]:
    """What the tables saw over the last `days` days: posts per day per post kind (mega, kol) over the days that had any
    post of that kind (a day the daemon was down is not a day with zero posts); posts and authors per confirmation
    search (`x_spend` rows of kind ca_search / ca_authors); the planner's keys per day and confirmed share (`x_keys`);
    the eligible observations per day (`decisions`). Empty when nothing was stored."""
    out: dict[str, dict[str, Any]] = {}
    since = now - days * 86400
    for r in conn.execute(
        "SELECT kind, COUNT(*) AS n, COUNT(DISTINCT CAST((received_at / 86400) AS INTEGER)) AS days FROM social_posts"
        " WHERE received_at >= ? AND kind IN ('mega', 'kol') GROUP BY kind", (since,)):
        if int(r["days"]) > 0:
            out[str(r["kind"])] = {"posts": int(r["n"]), "days": int(r["days"]), "posts_per_day": int(r["n"]) / int(r["days"])}
    s = conn.execute("SELECT COUNT(*) AS rows, COALESCE(SUM(n), 0) AS posts FROM x_spend WHERE kind='ca_search' AND COALESCE(unit, 'post')='post' AND ts >= ?",
                     (since,)).fetchone()
    if int(s["rows"]) > 0:
        a = conn.execute("SELECT COUNT(*) AS rows, COALESCE(SUM(n), 0) AS authors FROM x_spend WHERE kind='ca_authors' AND ts >= ?", (since,)).fetchone()
        out["search"] = {"searches": int(s["rows"]), "posts_per_search": int(s["posts"]) / int(s["rows"]),
                         "authors_per_search": (int(a["authors"]) / int(s["rows"])) if int(s["rows"]) else 0.0}
    out.update(planner.observed_rates(conn, now, days))
    o = conn.execute(
        "SELECT COUNT(*) AS n, COUNT(DISTINCT CAST(as_of / 86400 AS INTEGER)) AS days FROM decisions"
        " WHERE kind IN ('observe', 'observe_meta') AND verdict='observe' AND as_of >= ?", (since,)).fetchone()
    if int(o["days"]) > 0:
        out["observations"] = {"observations": int(o["n"]), "days": int(o["days"]), "per_day": int(o["n"]) / int(o["days"])}
    return out


def spend_today_by_kind(conn: sqlite3.Connection, now: float) -> dict[str, float]:
    return {str(r[0]): float(r[1]) for r in conn.execute("SELECT kind, SUM(usd) FROM x_spend WHERE day=? GROUP BY kind", (day_key(now),))}


def _peak_hours(cfg: dict[str, Any]) -> int:
    return len({int(h) for h in cfg.get("envelope", {}).get("peak_hours_utc", [])})


def requests_per_day(section: dict[str, Any], cfg: dict[str, Any], default_peak: float, default_off: float) -> float:
    """Requests of an account watch per day: the peak hours at `interval_peak_s`, the rest at `interval_off_s`."""
    peak = _peak_hours(cfg)
    return peak * 3600.0 / interval_for(section, True, default_peak, default_off) + (24 - peak) * 3600.0 / interval_for(section, False, default_peak, default_off)


def key_model(cfg: dict[str, Any], rates: dict[str, Any], assumptions: dict[str, Any]) -> dict[str, Any]:
    """What one planned key costs: the ceiling (every count of the schedule and the one search at its maximum) and the
    expected cost (the observed or assumed confirmed share, counts per confirmed key, posts and authors per search)."""
    prices = cfg.get("prices", {})
    p_post, p_count, p_author = (float(prices.get(k, 0.0)) for k in ("post_usd", "counts_request_usd", "author_expansion_usd"))
    ccfg, pcfg = cfg["confirm"], cfg.get("planner", {})
    window = float(ccfg.get("window_s", 21600))
    counts_max = len(planner.schedule_counts(pcfg, window))
    posts_max = int(ccfg.get("search_max_results", 10))
    authors_max = max(0, int(pcfg.get("search_max_authors", 3)))
    st = rates.get("stopped") or {}
    se = rates.get("search") or {}
    if int(st.get("n") or 0) >= MIN_STOPPED_FOR_RATES:
        share, share_src = float(st["confirmed_share"]), f"waargenomen over {int(st['n'])} gestopte sleutels"
        c_conf = float(st["counts_per_confirmed_key"]) if st.get("counts_per_confirmed_key") is not None else float(assumptions.get("counts_per_confirmed_key", 4))
        counts_per_key, counts_src = float(st["counts_per_key"]), "waargenomen"
    else:
        share, share_src = float(assumptions.get("confirmed_share", 0.5)), "aanname (config/x.json -> dryrun)"
        c_conf = min(float(counts_max), float(assumptions.get("counts_per_confirmed_key", 4)))
        counts_per_key, counts_src = (1.0 - share) * counts_max + share * c_conf, "model: niet-bevestigde sleutels het volle schema, bevestigde het gemiddelde"
    if int(se.get("searches") or 0) >= MIN_SEARCHES_FOR_RATES:
        posts, authors, search_src = min(float(posts_max), float(se["posts_per_search"])), min(float(authors_max), float(se["authors_per_search"])), f"waargenomen over {int(se['searches'])} zoekopdrachten"
    else:
        posts, authors, search_src = min(float(posts_max), float(assumptions.get("posts_per_search", posts_max))), min(float(authors_max), float(assumptions.get("authors_per_search", authors_max))), "aanname (config/x.json -> dryrun)"
    search_ceiling = posts_max * p_post + authors_max * p_author
    return {
        "counts_max": counts_max, "count_usd": p_count, "posts_max": posts_max, "authors_max": authors_max, "post_usd": p_post, "author_usd": p_author,
        "search_ceiling_usd": search_ceiling, "key_ceiling_usd": counts_max * p_count + search_ceiling,
        "confirmed_share": share, "confirmed_share_source": share_src, "counts_per_confirmed_key": c_conf,
        "counts_per_key": counts_per_key, "counts_per_key_source": counts_src,
        "posts_per_search": posts, "authors_per_search": authors, "search_source": search_src,
        "search_expected_usd": posts * p_post + authors * p_author,
        "key_expected_usd": counts_per_key * p_count + share * (posts * p_post + authors * p_author),
        "schedule_offsets_min": [round(o / 60) for o in planner.schedule_counts(pcfg, window)],
    }


def full_load(cfg: dict[str, Any], mega_usd: float, kol_usd: float, cap: float, keys_per_day: int | None = None) -> dict[str, Any]:
    """The planner's ceiling for a day of `keys_per_day` keys (default `planner.full_load_keys_per_day`): every key
    takes every count of its schedule AND its one search with every post and author new. A key cannot do both (a
    confirmed key stops counting, a refuted key never searches), so the real day stays under it; the formula is what
    docs/UNVERIFIED.md shows."""
    m = key_model(cfg, {}, DEFAULT_ASSUMPTIONS)
    pcfg = cfg.get("planner", {})
    k = int(keys_per_day or pcfg.get("full_load_keys_per_day", 60))
    counts_usd = k * m["counts_max"] * m["count_usd"]
    search_usd = k * m["search_ceiling_usd"]
    ceiling = counts_usd + search_usd
    target = float(pcfg.get("target_daily_cap_usd", 15.0))
    watches = float(mega_usd) + float(kol_usd)
    return {
        "keys_per_day": k, "counts_per_key": m["counts_max"], "count_usd": m["count_usd"], "posts_per_search": m["posts_max"],
        "authors_per_search": m["authors_max"], "post_usd": m["post_usd"], "author_usd": m["author_usd"],
        "counts_usd": counts_usd, "search_usd": search_usd, "confirm_ceiling_usd": ceiling,
        "formula": (f"{k} x ({m['counts_max']} x ${m['count_usd']:.3f} + {m['posts_max']} x ${m['post_usd']:.3f} + {m['authors_max']} x ${m['author_usd']:.3f})"
                    f" = {k} x ${m['key_ceiling_usd']:.3f} = ${ceiling:.2f}"),
        "account_watches_usd": watches, "total_ceiling_usd": ceiling + watches,
        "target_cap_usd": target, "fits_target": ceiling + watches <= target, "cap_usd": cap, "fits_cap": ceiling + watches <= cap,
        "share_of_cap": (ceiling + watches) / cap if cap else None,
    }


def per_kind(cfg: dict[str, Any], n_keys: int, n_kols: int, rates: dict[str, dict[str, Any]], assumptions: dict[str, Any],
             keys_per_day: float | None = None) -> list[dict[str, Any]]:
    """Requests, billed units and USD per day per kind (steady state, a full UTC day). `n_keys` are the candidate keys
    right now (shown), `keys_per_day` the keys the planner admits over a day (the cost driver of the confirmation
    kinds; None = the current keys taken as a day's load)."""
    prices = cfg.get("prices", {})
    p_post, p_count, p_author, p_refresh = (float(prices.get(k, 0.0)) for k in ("post_usd", "counts_request_usd", "author_expansion_usd", "refresh_post_usd"))
    mcfg, kcfg, env = cfg["mega"], cfg["kol"], cfg.get("envelope", {})
    peak = _peak_hours(cfg)
    mega_accounts = accounts_in_query(str(mcfg.get("query", "")))
    if "mega" in rates:
        mega_posts, mega_src = float(rates["mega"]["posts_per_day"]), f"waargenomen over {rates['mega']['days']} dag(en)"
    else:
        mega_posts, mega_src = float(assumptions.get("mega_posts_per_day", 80)), "aanname (config/x.json -> dryrun)"
    if "kol" in rates:
        kol_posts, kol_src = float(rates["kol"]["posts_per_day"]), f"waargenomen over {rates['kol']['days']} dag(en)"
    else:
        kol_posts, kol_src = (float(assumptions.get("kol_posts_per_day", 200)) if n_kols else 0.0), "aanname (config/x.json -> dryrun)"
    rows: list[dict[str, Any]] = []
    # mega: one search per interval (60 s in the peak hours, 120 s outside); cost = new posts x post price + new authors x expansion price
    req = requests_per_day(mcfg, cfg, 60.0, 120.0)
    authors = min(float(mega_accounts), mega_posts)
    rows.append({"kind": "mega_search", "interval_s": f"{interval_for(mcfg, True, 60, 120):.0f}/{interval_for(mcfg, False, 60, 120):.0f}", "requests_per_day": req,
                 "units": {"posts": mega_posts, "authors": authors}, "usd_per_day": mega_posts * p_post + authors * p_author,
                 "basis": f"{mega_posts:.0f} posts/dag ({mega_src}), {mega_accounts} accounts in de query; {peak} piekuren", "hard_cap": None})
    # refresh: a re-read of the posts younger than refresh_max_age_s every refresh_interval_s (assumed free: VERIFY)
    rreq = 86400.0 / float(mcfg.get("refresh_interval_s", 120))
    per_req = min(100.0, mega_posts * float(mcfg.get("refresh_max_age_s", 7200)) / 86400.0)
    rows.append({"kind": "refresh", "interval_s": f"{float(mcfg.get('refresh_interval_s', 120)):.0f}", "requests_per_day": rreq,
                 "units": {"posts": rreq * per_req}, "usd_per_day": rreq * per_req * p_refresh,
                 "basis": f"{per_req:.1f} posts per verversing (jonger dan {float(mcfg.get('refresh_max_age_s', 7200)) / 3600:.0f} u); "
                          f"prijs {p_refresh:g} per post (aangenomen gratis: VERIFY)", "hard_cap": None})
    # kol: the batched from: query (180 s in the peak hours, 300 s outside); the KOL sub-cap bounds the day
    kreq = requests_per_day(kcfg, cfg, 180.0, 300.0) if n_kols else 0.0
    kauthors = min(float(n_kols), kol_posts)
    kcost = kol_posts * p_post + kauthors * p_author
    kol_cap = float(env.get("kol_daily_cap_usd", 1.0))
    rows.append({"kind": "kol_search", "interval_s": f"{interval_for(kcfg, True, 180, 300):.0f}/{interval_for(kcfg, False, 180, 300):.0f}", "requests_per_day": kreq,
                 "units": {"posts": kol_posts, "authors": kauthors}, "usd_per_day": kcost,
                 "basis": f"{kol_posts:.0f} posts/dag ({kol_src}), {n_kols} KOL-accounts", "hard_cap": kol_cap,
                 "note": f"KOL-plafond ${kol_cap:.2f}/dag: {'bereikt' if kcost > kol_cap else 'niet bereikt'} (ongeremd ${kcost:.2f})"})
    # the planner's keys: counts per key from the schedule and the stop rules, one search per confirmed key
    m = key_model(cfg, rates, assumptions)
    kpd = float(n_keys if keys_per_day is None else keys_per_day)
    creq = kpd * m["counts_per_key"]
    rows.append({"kind": "ca_counts", "interval_s": f"{float(cfg.get('planner', {}).get('fast_interval_s', 900)):.0f}/{float(cfg.get('planner', {}).get('slow_interval_s', 3600)):.0f}",
                 "requests_per_day": creq, "units": {"requests": creq}, "usd_per_day": creq * p_count,
                 "basis": f"{kpd:.0f} sleutels/dag x {m['counts_per_key']:.1f} tellingen per sleutel ({m['counts_per_key_source']}; schema hoogstens "
                          f"{m['counts_max']}: {', '.join(str(o) for o in m['schedule_offsets_min'])} min)", "hard_cap": None,
                 "per_key_usd_ceiling": m["counts_max"] * p_count, "per_key_usd_expected": m["counts_per_key"] * p_count})
    sreq = kpd * m["confirmed_share"]
    rows.append({"kind": "ca_search", "interval_s": "-", "requests_per_day": sreq,
                 "units": {"posts": sreq * m["posts_per_search"]}, "usd_per_day": sreq * m["posts_per_search"] * p_post,
                 "basis": f"bevestigd aandeel {m['confirmed_share']:.0%} ({m['confirmed_share_source']}): een zoekopdracht per bevestigde sleutel, "
                          f"{m['posts_per_search']:.1f} posts ({m['search_source']}; hoogstens {m['posts_max']})", "hard_cap": None,
                 "per_key_usd_ceiling": m["posts_max"] * p_post})
    areq = sreq if m["authors_max"] else 0.0
    rows.append({"kind": "ca_authors", "interval_s": "-", "requests_per_day": areq,
                 "units": {"authors": areq * m["authors_per_search"]}, "usd_per_day": areq * m["authors_per_search"] * p_author,
                 "basis": f"een gebruikersopvraag per zoekopdracht, {m['authors_per_search']:.1f} auteurs (hoogstens {m['authors_max']})", "hard_cap": None,
                 "per_key_usd_ceiling": m["authors_max"] * p_author})
    return rows


def envelope_weights(cfg: dict[str, Any]) -> list[float]:
    env = cfg.get("envelope", {})
    peak = {int(h) for h in env.get("peak_hours_utc", [])}
    return [float(env.get("peak_weight", 1.0)) if h in peak else float(env.get("off_weight", 0.4)) for h in range(24)]


def simulate_hours(cfg: dict[str, Any], cap: float, rows: list[dict[str, Any]], now: float, hours: int, spend_today: dict[str, float] | None = None,
                   arrival: str = "uniform") -> list[dict[str, Any]]:
    """Walk the next `hours` UTC hours from the current one through the planner's own rules (`XBudget.admit`), in its
    admission order: the hard cap, the account-watch reserve (confirmation kinds may use at most 1 - reserve of the
    cap) and the KOL sub-cap refuse for the rest of the day (`refused`, carried to the next day), the hourly envelope
    defers to the next hour (`deferred`, carried). The daily cost of a kind arrives uniformly over the day (`arrival`
    "uniform") or in proportion to the envelope weights ("envelope"). Nothing is lost in the model; the daemon's keys
    expire after their window, which the model does not track."""
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
        deferred: dict[str, str] = {}
        refused: dict[str, str] = {}
        deferred_usd = refused_usd = 0.0
        for kind in ORDER_IN_HOUR:
            want = daily.get(kind, 0.0) * profile + backlog[kind]
            backlog[kind] = 0.0
            if want <= 0:
                continue
            total = sum(spent.values())
            hard = [("daily_cost_cap", cap - total)]
            if kind in CONFIRM:
                hard.append(("account_watch_reserve", cap * (1.0 - reserve) - sum(spent[k] for k in CONFIRM)))
            if kind == "kol_search":
                hard.append(("kol_daily_cap", kol_cap - spent["kol_search"]))
            reason, room = min(hard, key=lambda x: x[1])
            room = max(0.0, room)
            allowed = min(want, room)
            if allowed < want - 1e-9:
                refused[kind] = reason
                refused_usd += want - allowed
                backlog[kind] += want - allowed
                want = allowed
            paced = max(0.0, min(want, allowance - total))
            if paced < want - 1e-9:
                deferred[kind] = DEFERRED
                deferred_usd += want - paced
                backlog[kind] += want - paced
            spent[kind] += paced
        out.append({"hour": t.strftime("%Y-%m-%dT%H"), "h": h, "peak": h in peak, "allowance_usd": round(allowance, 4),
                    "spent_usd": round(sum(spent.values()), 4), "deferred_usd": round(deferred_usd, 4), "deferred": deferred,
                    "refused_usd": round(refused_usd, 4), "refused": refused, "backlog_usd": round(sum(backlog.values()), 4)})
    return out


def keys_per_day_choice(rates: dict[str, Any], assumptions: dict[str, Any], n_keys_now: int, pcfg: dict[str, Any]) -> tuple[float, str]:
    """The day's key load: the configured assumption, else the planner's observed admissions, else the observed eligible
    observations, else the current candidates when there are any, else the full load."""
    if assumptions.get("keys_per_day") is not None:
        return float(assumptions["keys_per_day"]), "aanname (config/x.json -> dryrun.keys_per_day)"
    k = rates.get("keys_per_day")
    if k:
        return float(k["per_day"]), f"waargenomen: {k['keys']} sleutels over {k['days']} dag(en)"
    o = rates.get("observations")
    if o:
        return float(o["per_day"]), f"waargenomen: {o['observations']} observaties over {o['days']} dag(en)"
    if n_keys_now:
        return float(n_keys_now), "de kandidaten van dit moment als daglading"
    return float(pcfg.get("full_load_keys_per_day", 60)), "volle belasting (planner.full_load_keys_per_day)"


def build(conn: sqlite3.Connection, settings, now: float, hours: int = 24, arrival: str = "uniform") -> dict[str, Any]:
    from ..engine.meta import load_stopwords

    cfg = settings.config("x")
    pcfg = cfg.get("planner", {})
    cap = float(settings.x_daily_budget_usd)
    keys = planner.candidates(conn, now, cfg, load_stopwords(settings.config("stopwords")))
    plan_state = planner.status_counters(conn, now)
    n_kols = len(settings.config("x_kols").get("accounts", []))
    rates = observed_rates(conn, now)
    assumptions = {**DEFAULT_ASSUMPTIONS, **{k: v for k, v in (cfg.get("dryrun") or {}).items() if not k.startswith("_")}}
    kpd, kpd_src = keys_per_day_choice(rates, assumptions, len(keys), pcfg)
    rows = per_kind(cfg, len(keys), n_kols, rates, assumptions, kpd)
    by_kind = {r["kind"]: r for r in rows}
    total = sum(r["usd_per_day"] for r in rows)
    total_capped = sum(min(r["usd_per_day"], r["hard_cap"]) if r.get("hard_cap") is not None else r["usd_per_day"] for r in rows)
    spend = spend_today_by_kind(conn, now)
    sim = simulate_hours(cfg, cap, rows, now, hours, spend, arrival)
    full = full_load(cfg, by_kind["mega_search"]["usd_per_day"], min(by_kind["kol_search"]["usd_per_day"], by_kind["kol_search"]["hard_cap"] or 0.0), cap)
    env = cfg.get("envelope", {})
    plan_vs_actual = [{"kind": r["kind"], "planned_usd": r["usd_per_day"], "planned_share": (r["usd_per_day"] / cap) if cap else None,
                       "actual_usd": spend.get(r["kind"], 0.0), "actual_share": (spend.get(r["kind"], 0.0) / cap) if cap else None} for r in rows]
    notes = ["Geen netwerk: niets is aangeroepen of geschreven.",
             "Prijzen uit config/x.json zijn niet geverifieerd (docs/UNVERIFIED.md, 'X (Twitter) API v2'); `fart social calibrate` vergelijkt ze met de console.",
             "Posts en auteurs worden één keer per UTC-dag gefactureerd (x_dedupe); de schatting telt elke post en auteur één keer.",
             "Volle belasting = elke sleutel al zijn tellingen én zijn zoekopdracht; een bevestigde sleutel stopt met tellen en een weerlegde zoekt nooit, dus de echte dag blijft eronder.",
             "refresh is aangenomen gratis; een uitgestelde aanroep (envelop) wordt later op de dag gedaan, een geweigerde (plafond, reserve, KOL-plafond) de volgende dag."]
    target = float(pcfg.get("target_daily_cap_usd", 15.0))
    if abs(cap - target) > 1e-9:
        notes.append(f"De planner is afgestemd op X_DAILY_BUDGET_USD {target:.2f}; .env zegt {cap:.2f} (deploy/DEPLOY.md, 'Upgrade to milestone 7').")
    return {
        "generated_at": _dt.datetime.fromtimestamp(now, tz=_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "x_mode": settings.x_mode,
        "cap_usd": cap,
        "target_cap_usd": target,
        "prices": {k: v for k, v in cfg.get("prices", {}).items() if not k.startswith("_")},
        "envelope": {"peak_hours_utc": env.get("peak_hours_utc"), "peak_weight": env.get("peak_weight"), "off_weight": env.get("off_weight"),
                     "account_watch_reserve_fraction": env.get("account_watch_reserve_fraction"), "kol_daily_cap_usd": env.get("kol_daily_cap_usd")},
        "planner": {k: v for k, v in pcfg.items() if not k.startswith("_")},
        "confirmation_keys": {"n": len(keys), "mint": sum(1 for k in keys if k["kind"] == "mint"), "meta": sum(1 for k in keys if k["kind"] == "meta"),
                              "sample": [k["key"] for k in keys[:8]], "active_in_plan": plan_state["active_total"], "search_pending": plan_state["search_pending"],
                              "stopped_today": plan_state["stopped_today"]},
        "keys_per_day": kpd,
        "keys_per_day_source": kpd_src,
        "key_model": key_model(cfg, rates, assumptions),
        "observed_rates": rates,
        "assumptions": assumptions,
        "kinds": rows,
        "total_usd_per_day": total,
        "total_usd_per_day_after_caps": total_capped,
        "fits_cap": total_capped <= cap,
        "full_load": full,
        "plan_vs_actual": plan_vs_actual,
        "arrival": arrival,
        "hours": sim,
        "deferred_hours": [s["hour"] for s in sim if s["deferred"]],
        "refused_hours": [s["hour"] for s in sim if s["refused"]],
        "throttled_peak_hours": [s["hour"] for s in sim if (s["deferred"] or s["refused"]) and s["peak"]],
        "spend_today_usd": sum(spend.values()),
        "notes": notes,
    }


def render_text(d: dict[str, Any]) -> str:
    cap = d["cap_usd"]
    ck = d["confirmation_keys"]
    m = d["key_model"]
    out = [f"fart social dryrun  ({d['generated_at']}, X_MODE {d['x_mode']}, cap ${cap:.2f}/day, arrival {d['arrival']}; no HTTP, nothing written)",
           f"  prices: " + ", ".join(f"{k} {v}" for k, v in d["prices"].items()),
           f"  envelope: peak hours {d['envelope']['peak_hours_utc']} weight {d['envelope']['peak_weight']} / off {d['envelope']['off_weight']}; "
           f"confirmation kinds <= {100 * (1 - float(d['envelope']['account_watch_reserve_fraction'] or 0)):.0f}% of the cap; KOL cap ${float(d['envelope']['kol_daily_cap_usd'] or 0):.2f}",
           f"  planner: at most {d['planner'].get('max_active_keys')} keys at once ({d['planner'].get('max_meta_keys')} themes), counts at "
           + ", ".join(str(o) for o in m["schedule_offsets_min"]) + f" min after the decision (max {m['counts_max']}), one search of at most {m['posts_max']} posts"
           f" and {m['authors_max']} author lookups per confirmed key; refuted after {d['planner'].get('refute_after_flat_counts')} flat counts",
           f"  confirmation keys now: {ck['n']} ({ck['mint']} mints, {ck['meta']} themes)"
           + (": " + ", ".join(k[:14] for k in ck["sample"]) if ck["sample"] else "")
           + f"; in the plan: {ck['active_in_plan']} active, {ck['search_pending']} search pending; stopped today: "
           + (", ".join(f"{k} {v['n']} (${v['usd']:.2f})" for k, v in sorted(ck["stopped_today"].items())) or "none"),
           f"  keys per day used below: {d['keys_per_day']:.0f} ({d['keys_per_day_source']})",
           "",
           f"  {'kind':<12}{'interval':>10}{'req/day':>9}{'units/day':>26}{'USD/day':>9}{'% cap':>7}  basis"]
    for r in d["kinds"]:
        units = ", ".join(f"{k} {v:.0f}" for k, v in r["units"].items())
        out.append(f"  {r['kind']:<12}{str(r['interval_s']) + ' s' if r['interval_s'] != '-' else '-':>10}{r['requests_per_day']:>9.0f}{units:>26}{r['usd_per_day']:>9.2f}"
                   f"{100 * r['usd_per_day'] / cap if cap else 0:>6.0f}%  {r['basis']}" + (f"; {r['note']}" if r.get("note") else ""))
    out += [f"  {'total':<12}{'':>10}{'':>9}{'':>26}{d['total_usd_per_day']:>9.2f}{100 * d['total_usd_per_day'] / cap if cap else 0:>6.0f}%  "
            f"after the KOL cap ${d['total_usd_per_day_after_caps']:.2f} -> {'fits' if d['fits_cap'] else 'EXCEEDS'} the cap of ${cap:.2f}; "
            f"a planned key costs at most ${m['key_ceiling_usd']:.3f} (expected ${m['key_expected_usd']:.3f})"]
    f = d["full_load"]
    out += [f"  full load: {f['keys_per_day']} keys/day x ({f['counts_per_key']} counts + 1 search of {f['posts_per_search']} posts and {f['authors_per_search']} authors)"
            f" = ${f['confirm_ceiling_usd']:.2f}/day for the confirmation layer [{f['formula']}]",
            f"             + account watches ${f['account_watches_usd']:.2f} (mega, KOL after its cap) = ${f['total_ceiling_usd']:.2f}/day = "
            f"{100 * (f['share_of_cap'] or 0):.0f}% of the cap ${cap:.2f} -> {'fits' if f['fits_cap'] else 'EXCEEDS'}; "
            f"against the planner's target cap ${f['target_cap_usd']:.2f}: {'fits' if f['fits_target'] else 'EXCEEDS'}",
            f"  observed rates: " + (", ".join(_rate_line(k, v) for k, v in d["observed_rates"].items()) or "none (assumptions used)"),
            f"  spent today so far: ${d['spend_today_usd']:.2f}; planned vs actual per kind: "
            + ", ".join(f"{p['kind']} ${p['planned_usd']:.2f}/${p['actual_usd']:.2f} ({100 * (p['planned_share'] or 0):.0f}%/{100 * (p['actual_share'] or 0):.0f}%)"
                        for p in d["plan_vs_actual"]),
            "",
            f"  {'hour (UTC)':<15}{'peak':>5}{'allowance':>11}{'spent':>9}{'deferred':>9}{'refused':>9}{'backlog':>9}  deferred / refused kinds (reason)"]
    for s in d["hours"]:
        parts = [f"{k} ({v})" for k, v in s["deferred"].items()] + [f"{k} ({v})" for k, v in s["refused"].items()]
        out.append(f"  {s['hour']:<15}{'*' if s['peak'] else '':>5}{s['allowance_usd']:>11.2f}{s['spent_usd']:>9.2f}{s['deferred_usd']:>9.2f}{s['refused_usd']:>9.2f}"
                   f"{s['backlog_usd']:>9.2f}  {', '.join(parts)}")
    out.append(f"  hours with deferred calls: {len(d['deferred_hours'])} of {len(d['hours'])}; with refused calls: {len(d['refused_hours'])}; in peak hours: "
               f"{len(d['throttled_peak_hours'])}" + (" (" + ", ".join(h[-2:] for h in d["throttled_peak_hours"]) + ")" if d["throttled_peak_hours"] else ""))
    out += [f"  note: {n}" for n in d["notes"]]
    return "\n".join(out)


def _rate_line(kind: str, v: dict[str, Any]) -> str:
    if kind in ("mega", "kol"):
        return f"{kind} {v['posts_per_day']:.0f} posts/day over {v['days']} day(s)"
    if kind == "search":
        return f"{v['searches']} searches: {v['posts_per_search']:.1f} posts, {v['authors_per_search']:.1f} authors each"
    if kind == "keys_per_day":
        return f"{v['per_day']:.1f} keys/day over {v['days']} day(s)"
    if kind == "stopped":
        return f"{v['n']} stopped keys: {100 * v['confirmed_share']:.0f}% confirmed, {v['counts_per_key']:.1f} counts/key"
    if kind == "observations":
        return f"{v['per_day']:.1f} observations/day over {v['days']} day(s)"
    return f"{kind} {v}"


def planned_by_kind(conn: sqlite3.Connection, settings, now: float) -> dict[str, float]:
    """The planned USD per kind for a day, for `fart status` (the per-kind rows of the dry run, no hour walk)."""
    d = build(conn, settings, now, hours=1)
    return {r["kind"]: float(r["usd_per_day"]) for r in d["kinds"]}
