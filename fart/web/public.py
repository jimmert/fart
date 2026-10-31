"""The public data of the website (milestone 9): what anyone may see, in plain Dutch, with the honesty rules.

Every function here returns plain dicts for the public pages and for `/api/public/*`. They carry raw values for machines
next to Dutch display strings for the pages, and nothing operational: no budget, no cap, no key, no path, no IP address,
no planner state, no daemon log. A test (`tests/test_m9_public_site.py`) flattens every public JSON document and proves
that none of the owner vocabulary occurs in it.

The honesty rules of the reports apply to every number: observation, never advice; an hour or a day NIET GEDEKT shows no
counts (empty, never zero); an outcome that could not be measured stays "onbekend"; a decision inside its 24 hours is
"nog bezig"; a bootstrap interval that does not exist yet is "te weinig data". Nothing here writes.

Milestone 10 (performance, no change of design or content): every time window over `trades`, `coins` and `migrations`
is bounded on the indexed `received_at` first (a margin of `ET_MARGIN_S` wider than the window) and the event time
`COALESCE(block_time, received_at)` is applied to the rows in that range, so no window scans a table (schema v8 adds
`trades(received_at)`; a test proves every window is an index range); the coverage of the last 24 hours comes from the
`coverage` table the ingest recomputes every minute instead of being counted again per view; the observations fetch
their coin names and outcomes in batches; and the themes card reads the engine's own ranked metas from kv
(`engine.metas_latest`, written every tick) and computes nothing. A view that is still being computed for the first
time is the placeholder `computing()`: the card says "wordt berekend", the JSON says `{"state": "computing"}`.
"""
from __future__ import annotations

import datetime as _dt
import json
import sqlite3
from typing import Any

from ..config import Settings
from ..engine import themes as engine_themes
from ..notify.format import age as age_label, fmt_day, fmt_time, n as fmt_n
from ..util import day_bounds, day_key, day_start, hour_bounds, hour_key, utc_iso
from ..version import rules_equivalents, rules_fingerprint, rules_version, runs_frozen_rules
from . import data

H24 = 86400
WEEK_DAYS = 7
FRESH_S = 180                 # the ingest heartbeat is "vers" up to this age
STALE_S = 900                 # above this the page says the data is old
ET_MARGIN_S = 60              # a row's received_at is never more than this before its event time (our clock versus the chain's)
COVERAGE_RECENT_S = 180       # a coverage row computed within this many seconds still describes the current hour (milestone 10)
FAST_LOOKBACK_S = 86400       # a post received longer ago than this cannot become "fast" today (the X layer refreshes a post for 2 hours)
OUTCOME_NL = data.OUTCOME_NL
STATUS_NL = data.STATUS_NL
MONTHS = ["januari", "februari", "maart", "april", "mei", "juni", "juli", "augustus", "september", "oktober", "november", "december"]

# status pills: (key, label) pairs the pages and the CSS know
PILL = {
    "fresh": "vers", "stale": "oud", "unknown": "onbekend", "not_covered": "NIET GEDEKT", "few": "te weinig data",
    "winner": "winnaar", "not_winner": "geen winnaar", "pending": "nog bezig", "none": "thema", "ok": "voldaan",
    "fail": "niet voldaan", "complete": "COMPLEET", "incomplete": "NIET COMPLEET", "go": "GO", "nogo": "NO-GO",
    "soon": "binnenkort", "frozen": "bevroren", "computing": "wordt berekend",
}


# ----- small formatters ----------------------------------------------------------------------------------------------
def pct(x: Any) -> str:
    return "onbekend" if x is None else f"{round(100 * float(x))}%"


def minutes(seconds: Any) -> str:
    if seconds is None:
        return "onbekend"
    m = float(seconds) / 60.0
    return f"{m:.0f} min" if abs(m) >= 10 else f"{m:.1f} min".replace(".", ",")


def usd_short(x: Any) -> str:
    """$120.000 in Dutch digits; 'onbekend' when the value is unknown."""
    return "onbekend" if x is None else f"${fmt_n(x)}"


def short_mint(mint: str | None) -> str:
    if not mint:
        return "–"
    s = str(mint)
    return s if len(s) <= 12 else f"{s[:4]}…{s[-4:]}"


def day_label_long(day: str) -> str:
    """'maandag 27 oktober 2026' for a UTC day key."""
    try:
        d = _dt.datetime.strptime(day, "%Y-%m-%d")
    except ValueError:
        return day
    weekdays = ["maandag", "dinsdag", "woensdag", "donderdag", "vrijdag", "zaterdag", "zondag"]
    return f"{weekdays[d.weekday()]} {d.day} {MONTHS[d.month - 1]} {d.year}"


def clock_label(ts: float | None) -> str:
    """'14:05' in Europe/Amsterdam; 'onbekend' without a time."""
    if ts is None:
        return "onbekend"
    full = fmt_time(ts)          # '26 okt 14:05 (12:05 UTC)'
    try:
        return full.split(" ")[2]
    except IndexError:
        return full


def _float(v: Any) -> float | None:
    try:
        return None if v is None else float(v)
    except (TypeError, ValueError):
        return None


def _kv(conn: sqlite3.Connection, keys: tuple[str, ...]) -> dict[str, str]:
    marks = ",".join("?" for _ in keys)
    return {str(r["key"]): r["value"] for r in conn.execute(f"SELECT key, value FROM kv WHERE key IN ({marks})", keys)}


# ----- milestone 10: the placeholder of a view that is being computed for the first time ------------------------------
def computing(name: str) -> dict[str, Any]:
    """What a request gets while the background thread has not finished a view's first computation: never a number."""
    return {"state": "computing", "view": name, "generated_at": None, "generated_label": PILL["computing"],
            "note": "Dit onderdeel wordt nu berekend; binnen een halve minuut staat het hier."}


def is_computing(v: Any) -> bool:
    return isinstance(v, dict) and v.get("state") == "computing"


# ----- milestone 10: time windows as index ranges, coverage from the table --------------------------------------------
ET = "COALESCE(block_time, received_at)"


def _count_since(conn: sqlite3.Connection, table: str, t: float) -> int:
    """Rows of `coins` or `migrations` with an event time at or after `t`: the index range on `received_at` first (a margin
    of ET_MARGIN_S wider), the event time on the rows in that range. Never a scan."""
    return int(conn.execute(f"SELECT COUNT(*) FROM {table} WHERE received_at >= ? AND {ET} >= ?", (t - ET_MARGIN_S, t)).fetchone()[0])


def _newest(conn: sqlite3.Connection, sql: str, limit: int, key: str = "t") -> list[sqlite3.Row]:
    """The `limit` newest rows by event time: three times as many newest by the indexed `received_at` (the index walks
    them in order and stops), then sorted by the event time `key` of the row. `sql` ends with ORDER BY ... received_at
    DESC and takes the limit as its one parameter."""
    rows = conn.execute(sql, (3 * int(limit),)).fetchall()
    rows.sort(key=lambda r: float(r[key]), reverse=True)
    return rows[: int(limit)]


def _coverage_rows(conn: sqlite3.Connection, first_hour: str, last_hour: str) -> dict[str, dict[str, Any]]:
    """The hour rows the ingest wrote (`coverage.recompute_recent`, every minute for the current and the last three
    hours; every hour was current once), by hour key. A row written before schema v3 has no `covered`: complete then."""
    out = {}
    for r in conn.execute("SELECT hour, creates_union, grads, complete, covered, computed_at FROM coverage WHERE hour >= ? AND hour <= ? ORDER BY hour",
                          (first_hour, last_hour)):
        covered = r["covered"] if r["covered"] is not None else r["complete"]
        out[str(r["hour"])] = {"hour": str(r["hour"]), "creates_union": r["creates_union"], "grads": r["grads"], "complete": bool(r["complete"]),
                               "covered": bool(covered), "computed_at": _float(r["computed_at"])}
    return out


def _hour_covered(conn: sqlite3.Connection, now: float) -> bool:
    """Is the current hour covered? The ingest's own row for this hour; in the first minute of an hour, before that row
    exists, the previous hour's row says so when it was computed a moment ago (the counting was running then and the
    gap state carries over). No row, or an old one: not covered, honestly."""
    cur = hour_key(now)
    rows = _coverage_rows(conn, hour_key(now - 3600), cur)
    if cur in rows:
        return rows[cur]["covered"]
    prev = rows.get(hour_key(now - 3600))
    return bool(prev and prev["covered"] and prev["computed_at"] is not None and now - prev["computed_at"] <= COVERAGE_RECENT_S)


def _day_covered(conn: sqlite3.Connection, now: float) -> bool:
    rows = _coverage_rows(conn, hour_key(day_start(now)), hour_key(now))
    return any(r["covered"] for r in rows.values()) or _hour_covered(conn, now)


# ----- the measurement week -----------------------------------------------------------------------------------------
def week_position(settings: Settings, now: float) -> dict[str, Any]:
    """Where the frozen measurement week stands today: day x of 7 counted from `frozen_rules.first_full_day`."""
    gate = (settings.config("measure") or {}).get("community_gate") or {}
    fr = gate.get("frozen_rules") or {}
    first = str(fr.get("first_full_day") or "")
    today = day_key(now)
    out: dict[str, Any] = {"first_full_day": first, "first_full_day_label": fmt_day(first) if first else "onbekend", "today": today,
                           "days_total": WEEK_DAYS, "frozen_since": fr.get("since"), "frozen_rules_version": fr.get("rules_version"),
                           "rules_frozen": runs_frozen_rules(gate), "rules_equivalent": runs_frozen_rules(gate) and rules_fingerprint() != fr.get("rules_fingerprint"),
                           "fingerprint_short": rules_fingerprint()[:19], "rules_version": rules_version()}
    try:
        start = _dt.datetime.strptime(first, "%Y-%m-%d").date()
        cur = _dt.datetime.strptime(today, "%Y-%m-%d").date()
        idx = (cur - start).days + 1
    except ValueError:
        idx = None
    if idx is None:
        out.update({"day_index": None, "phase": "unknown", "phase_label": "meetweek nog niet gepland"})
    elif idx < 1:
        out.update({"day_index": 0, "phase": "before", "phase_label": f"de meetweek begint {fmt_day(first)}"})
    elif idx <= WEEK_DAYS:
        out.update({"day_index": idx, "phase": "running", "phase_label": f"dag {idx} van {WEEK_DAYS}"})
    else:
        out.update({"day_index": idx, "phase": "after", "phase_label": f"de meetweek ({fmt_day(first)} tot en met {fmt_day(_shift(first, WEEK_DAYS - 1))}) is voorbij"})
    out["end_day"] = _shift(first, WEEK_DAYS - 1) if first else None
    out["end_day_label"] = fmt_day(out["end_day"]) if out["end_day"] else "onbekend"
    return out


def _shift(day: str, days: int) -> str:
    try:
        return (_dt.datetime.strptime(day, "%Y-%m-%d") + _dt.timedelta(days=days)).strftime("%Y-%m-%d")
    except ValueError:
        return day


# ----- /api/public/status --------------------------------------------------------------------------------------------
def status_view(conn: sqlite3.Connection | None, settings: Settings, now: float) -> dict[str, Any]:
    """The hero line of the dashboard: what the radar sees right now, how fresh the data is, where the week stands."""
    site = settings.web_site
    out: dict[str, Any] = {**data.stamp(now), "site": {"name": site["site_name"], "product": site["product"], "tagline": site["tagline"],
                                                       "domain": site["domain"]},
                           "day": day_key(now), "day_label": fmt_day(day_key(now)), "refresh_s": int(site.get("refresh_s") or 30),
                           "warning": warning_text(settings), "banner": banner_text(settings), "week": week_position(settings, now)}
    if conn is None:
        out.update({"db_exists": False, "freshness": {"status": "unknown", "label": PILL["unknown"], "age_s": None, "age_label": "onbekend",
                                                      "note": "De radar heeft nog geen gegevens opgeslagen."},
                    "live": {"creates_last_hour": None, "creates_last_hour_label": "–", "hour_covered": False, "graduations_today": None,
                             "graduations_today_label": "–", "observations_today": None, "observations_today_label": "–",
                             "coins_today": None, "coins_today_label": "–", "last_observation_at": None, "last_observation_label": "nog geen"},
                    "headline": "De radar heeft nog geen gegevens: er valt nog niets te tonen.", "totals": {}})
        return out
    kv = _kv(conn, ("ingest.heartbeat", "ingest.pumpportal.last_msg_at", "ingest.rpc_logs.last_msg_at", "engine.heartbeat"))
    hb = _float(kv.get("ingest.heartbeat"))
    last_msg = max((v for v in (_float(kv.get("ingest.pumpportal.last_msg_at")), _float(kv.get("ingest.rpc_logs.last_msg_at"))) if v is not None),
                   default=None)
    age = None if hb is None else max(0.0, now - hb)
    if age is None:
        fresh = {"status": "unknown", "label": PILL["unknown"], "age_s": None, "age_label": "onbekend", "note": "De radar heeft nog nooit gedraaid."}
    elif age <= FRESH_S:
        fresh = {"status": "fresh", "label": PILL["fresh"], "age_s": round(age), "age_label": age_label(age), "note": "De radar luistert nu."}
    elif age <= STALE_S:
        fresh = {"status": "stale", "label": PILL["stale"], "age_s": round(age), "age_label": age_label(age),
                 "note": f"De laatste levensteken van de radar is {age_label(age)} oud."}
    else:
        fresh = {"status": "stale", "label": PILL["stale"], "age_s": round(age), "age_label": age_label(age),
                 "note": f"De radar heeft {age_label(age)} niets van zich laten horen: de cijfers hieronder lopen achter."}
    fresh["last_event_at"] = utc_iso(last_msg)
    fresh["last_event_label"] = clock_label(last_msg)
    out["freshness"] = fresh
    out["db_exists"] = True
    # live numbers: the last 60 minutes only count when the hour was covered (we were there to count)
    hour_covered = _hour_covered(conn, now)
    creates_1h = _count_since(conn, "coins", now - 3600) if hour_covered else None
    t0 = day_start(now)
    grads_today = _count_since(conn, "migrations", t0)
    coins_today = _count_since(conn, "coins", t0)
    obs_today = int(conn.execute("SELECT COUNT(*) FROM decisions WHERE verdict='observe' AND kind IN ('observe', 'observe_meta') AND as_of >= ?", (t0,)).fetchone()[0])
    last_dec = _last_observation_at(conn)
    day_covered = _day_covered(conn, now)
    out["live"] = {
        "creates_last_hour": creates_1h, "creates_last_hour_label": fmt_n(creates_1h) if creates_1h is not None else "–",
        "hour_covered": hour_covered,
        "graduations_today": grads_today if day_covered else None, "graduations_today_label": fmt_n(grads_today) if day_covered else "–",
        "coins_today": coins_today if day_covered else None, "coins_today_label": fmt_n(coins_today) if day_covered else "–",
        "day_covered": day_covered,
        "observations_today": obs_today, "observations_today_label": fmt_n(obs_today),
        "last_observation_at": utc_iso(last_dec), "last_observation_label": fmt_time(last_dec) if last_dec else "nog geen",
    }
    out["totals"] = {"observations": int(conn.execute("SELECT COUNT(*) FROM decisions WHERE kind IN ('observe', 'observe_meta')").fetchone()[0]),
                     "graduations": int(conn.execute("SELECT COUNT(*) FROM migrations").fetchone()[0]),
                     "coins": int(conn.execute("SELECT COUNT(*) FROM coins").fetchone()[0])}
    out["headline"] = headline(out)
    return out


def _last_observation_at(conn: sqlite3.Connection) -> float | None:
    """The newest observation: the `decisions_as_of` index walked backwards, stopping at the first observation."""
    r = conn.execute("SELECT as_of FROM decisions WHERE kind IN ('observe', 'observe_meta') ORDER BY as_of DESC LIMIT 1").fetchone()
    return None if r is None else _float(r[0])


def headline(st: dict[str, Any]) -> str:
    """One sentence with the live numbers; empty counts are named as such, never as zero."""
    live = st.get("live") or {}
    fr = st.get("freshness") or {}
    if fr.get("status") == "unknown":
        return "De radar heeft nog geen gegevens: er valt nog niets te tonen."
    parts = []
    if live.get("creates_last_hour") is not None:
        n = live["creates_last_hour"]
        parts.append(f"in het laatste uur {fmt_n(n)} nieuwe {'munt' if n == 1 else 'munten'}")
    else:
        parts.append("het laatste uur is nog niet gedekt, dus geen telling van nieuwe munten")
    if live.get("graduations_today") is not None:
        g = live["graduations_today"]
        parts.append(f"vandaag {fmt_n(g)} {'graduatie' if g == 1 else 'graduaties'}")
    o = live.get("observations_today") or 0
    parts.append(f"{fmt_n(o)} {'observatie' if o == 1 else 'observaties'}")
    lead = "De radar ziet" if fr.get("status") == "fresh" else "De radar zag"
    # two or three parts (the graduations are only counted on a covered day): "a, b en c." or "a en c.", never an index error
    return f"{lead} {parts[0]}." if len(parts) == 1 else f"{lead} {', '.join(parts[:-1])} en {parts[-1]}."


def warning_text(settings: Settings) -> str:
    return str(((settings.config("notify") or {}).get("community") or {}).get("warning")
               or "Geen advies. Hoog risico: de meeste van deze munten gaan naar nul. Alles wordt achteraf gemeten en gepubliceerd.")


def banner_text(settings: Settings) -> str:
    return str((settings.config("notify") or {}).get("footer") or "Observatie, geen advies. Alles wordt achteraf gemeten.")


# ----- /api/public/coverage -------------------------------------------------------------------------------------------
def coverage_view(conn: sqlite3.Connection | None, now: float, hours: int = 24) -> dict[str, Any]:
    """The last 24 hours per hour: covered, complete, creates and graduations (empty for an hour NIET GEDEKT)."""
    out: dict[str, Any] = {**data.stamp(now), "hours": int(hours), "rows": [], "summary": {}}
    if conn is None:
        out.update({"note": "Er zijn nog geen gegevens.", "summary": {"hours_covered": None, "hours_complete": None, "hours_not_covered": None,
                                                                   "creates_covered": None, "grads_covered": None}})
        out["sentence"] = "Er zijn nog geen gegevens over de dekking."
        return out
    # milestone 10: the hour rows the ingest wrote (recomputed every minute while the hour was current and for three
    # hours after), not counted again per view; an hour without a row was never counted: NIET GEDEKT, no number
    keys = [hour_key(now - i * 3600) for i in range(int(hours) - 1, -1, -1)]
    table = _coverage_rows(conn, keys[0], keys[-1])
    rows = []
    covered = complete = creates = grads = 0
    for i, hour in enumerate(keys):
        r = table.get(hour)
        is_cov = bool(r and r["covered"]) or (i == len(keys) - 1 and _hour_covered(conn, now))
        is_complete = bool(r and r["complete"]) and is_cov
        c = int(r["creates_union"] or 0) if (r and is_cov) else None
        g = int(r["grads"] or 0) if (r and is_cov) else None
        if is_cov:
            covered += 1
            complete += 1 if is_complete else 0
            creates += c or 0
            grads += g or 0
        rows.append({"hour": hour, "hour_label": fmt_time(_hour_start(hour)), "clock": clock_label(_hour_start(hour)), "covered": is_cov,
                     "complete": is_complete, "status": "complete" if is_complete else ("covered" if is_cov else "not_covered"),
                     "status_label": "compleet" if is_complete else ("gedekt" if is_cov else PILL["not_covered"]),
                     "creates": c, "creates_label": fmt_n(c) if c is not None else "–", "grads": g, "grads_label": fmt_n(g) if g is not None else "–"})
    out["rows"] = rows
    out["summary"] = {"hours_covered": covered, "hours_complete": complete, "hours_not_covered": len(rows) - covered,
                      "creates_covered": creates, "grads_covered": grads}
    nc = out["summary"]["hours_not_covered"]
    if nc == 0:
        out["sentence"] = f"Alle {len(rows)} uren waren gedekt: elke telling van de laatste 24 uur is compleet."
    else:
        out["sentence"] = (f"{fmt_n(nc)} van de {len(rows)} uren {'was' if nc == 1 else 'waren'} NIET GEDEKT: in {'dat uur' if nc == 1 else 'die uren'} "
                           "keek de radar niet, dus staat er geen telling. Een leeg uur is geen nul.")
    return out


def _hour_start(hour: str) -> float:
    return hour_bounds(hour)[0]


# ----- /api/public/observations ---------------------------------------------------------------------------------------
def observations_view(conn: sqlite3.Connection | None, settings: Settings, now: float, limit: int | None = None, day: str | None = None) -> dict[str, Any]:
    """Every observation (coin or theme) with when, why it stood out and how it turned out, newest first."""
    site = settings.web_site
    lim = int(limit or site.get("public_observations_limit") or 500)
    out: dict[str, Any] = {**data.stamp(now), "limit": lim, "day": day, "rows": [], "counts": {}, "definition": outcome_definition(settings)}
    if conn is None:
        out["note"] = "Er zijn nog geen gegevens."
        return out
    cfg = settings.config("measure")
    mv = str(cfg["method_version"])
    h24 = max(int(h) for h in cfg["horizons_s"])
    ecfg = settings.config("engine")
    if day:
        try:
            t0, t1 = day_bounds(day)
        except ValueError:
            out["note"] = "Onbekende dag."
            return out
        cur = conn.execute("SELECT * FROM decisions WHERE kind IN ('observe', 'observe_meta') AND as_of >= ? AND as_of < ? ORDER BY as_of DESC, id DESC LIMIT ?",
                           (t0, t1, lim))
    else:
        cur = conn.execute("SELECT * FROM decisions WHERE kind IN ('observe', 'observe_meta') ORDER BY as_of DESC, id DESC LIMIT ?", (lim,))
    decs = cur.fetchall()
    # milestone 10: the coin names and the outcomes of all rows in a few batched queries instead of two per row
    names = _coin_names(conn, [str(d["mint"]) for d in decs if d["mint"]])
    outcomes = _decision_outcomes(conn, [int(d["id"]) for d in decs if d["kind"] == "observe" and d["mint"]], now, h24, mv)
    rows = []
    counts: dict[str, int] = {}
    for d in decs:
        row = observation_row(conn, d, now, h24, mv, ecfg, names=names, outcomes=outcomes)
        counts[row["outcome"]] = counts.get(row["outcome"], 0) + 1
        rows.append(row)
    out["rows"] = rows
    out["counts"] = {"total": len(rows), "coins": sum(1 for r in rows if r["kind"] == "observe"), "themes": sum(1 for r in rows if r["kind"] == "observe_meta"),
                     **{k: counts.get(k, 0) for k in ("winner", "not_winner", "unknown", "pending")}}
    out["total_all_time"] = int(conn.execute("SELECT COUNT(*) FROM decisions WHERE kind IN ('observe', 'observe_meta')").fetchone()[0])
    return out


def outcome_definition(settings: Settings) -> str:
    w = settings.config("measure")["winner"]
    return (f"Uitkomst van een munt-observatie: winnaar als de munt binnen 24 uur na de observatie een piek van minstens ${fmt_n(w['peak_mcap_usd'])} "
            f"marktkapitalisatie én minstens ${fmt_n(w['min_volume_usd'])} volume haalde; geen winnaar als dat gemeten niet zo was; onbekend als de "
            "meting ontbreekt of niets kon vaststellen; nog bezig zolang de 24 uur niet om zijn. Een thema-observatie heeft geen eigen uitkomst.")


def _coin_names(conn: sqlite3.Connection, mints: list[str]) -> dict[str, tuple[str | None, str | None]]:
    out: dict[str, tuple[str | None, str | None]] = {}
    uniq = sorted(set(mints))
    for i in range(0, len(uniq), 400):
        chunk = uniq[i:i + 400]
        marks = ",".join("?" for _ in chunk)
        for r in conn.execute(f"SELECT mint, name, symbol FROM coins WHERE mint IN ({marks})", chunk):
            out[str(r["mint"])] = (r["name"], r["symbol"])
    return out


def _decision_outcomes(conn: sqlite3.Connection, ids: list[int], now: float, h24: int, mv: str) -> dict[int, dict[str, Any]]:
    """The newest 24 h outcome row per decision (`outcomes_decision` index), as `data.decision_outcome` reads it per row."""
    latest: dict[int, tuple[float, Any, Any]] = {}
    for i in range(0, len(ids), 400):
        chunk = ids[i:i + 400]
        marks = ",".join("?" for _ in chunk)
        for r in conn.execute(f"SELECT decision_id, measured_at, hit_100k, peak_mcap_usd FROM outcomes WHERE subject='decision' AND decision_id IN ({marks})"
                              f" AND horizon_s=? AND method_version=?", (*chunk, int(h24), mv)):
            did = int(r["decision_id"])
            if did not in latest or float(r["measured_at"]) > latest[did][0]:
                latest[did] = (float(r["measured_at"]), r["hit_100k"], r["peak_mcap_usd"])
    out: dict[int, dict[str, Any]] = {}
    for did, (_, hit, peak) in latest.items():
        out[did] = {"status": "unknown" if hit is None else ("winner" if hit else "not_winner"), "peak_mcap_usd": peak}
    return out


def observation_row(conn: sqlite3.Connection, d: sqlite3.Row, now: float, h24: int, mv: str, ecfg: dict[str, Any],
                    names: dict[str, tuple[str | None, str | None]] | None = None, outcomes: dict[int, dict[str, Any]] | None = None) -> dict[str, Any]:
    """One observation; with `names` and `outcomes` (the batches of `observations_view`) no query runs per row."""
    try:
        f = json.loads(d["features_json"]) if d["features_json"] else {}
    except json.JSONDecodeError:
        f = {}
    if not isinstance(f, dict):
        f = {}
    kind = str(d["kind"])
    as_of = float(d["as_of"])
    row: dict[str, Any] = {"id": int(d["id"]), "as_of": utc_iso(as_of), "as_of_label": fmt_time(as_of), "clock": clock_label(as_of), "day": day_key(as_of),
                           "day_label": fmt_day(day_key(as_of)), "kind": kind, "kind_label": "munt" if kind == "observe" else "thema",
                           "rules_version": d["rules_version"], "age_label": age_label(max(0.0, now - as_of))}
    if kind == "observe_meta":
        term = d["term"] or f.get("term") or ""
        row.update({"subject": str(term), "title": f"thema “{term}”", "mint": None, "mint_short": None, "name": None, "symbol": None,
                    "why": theme_why(f, ecfg), "facts": theme_facts(f), "outcome": "none", "outcome_label": "geen eigen uitkomst (thema)",
                    "outcome_short": PILL["none"], "peak_mcap_usd": None, "peak_label": "–"})
        return row
    mint = d["mint"] or f.get("mint") or ""
    if names is not None:
        c = names.get(str(mint)) if mint else None
    else:
        r = conn.execute("SELECT name, symbol FROM coins WHERE mint=?", (mint,)).fetchone() if mint else None
        c = None if r is None else (r["name"], r["symbol"])
    name = (c[0] if c is not None else None) or f.get("name") or None
    symbol = (c[1] if c is not None else None) or f.get("symbol") or None
    if not mint:
        outcome: dict[str, Any] = {"status": "unknown"}
    elif outcomes is not None:
        outcome = outcomes.get(int(d["id"])) or {"status": "pending" if as_of + h24 > now else "unknown"}
    else:
        outcome = data.decision_outcome(conn, d, now, h24, mv)
    title = (name or "naam onbekend") + (f" (${symbol})" if symbol else "")
    row.update({"subject": mint, "title": title, "mint": mint, "mint_short": short_mint(mint), "name": name, "symbol": symbol,
                "why": coin_why(f), "facts": coin_facts(f), "outcome": outcome["status"], "outcome_label": OUTCOME_NL.get(outcome["status"], outcome["status"]),
                "outcome_short": PILL.get(outcome["status"], outcome["status"]), "peak_mcap_usd": outcome.get("peak_mcap_usd"),
                "peak_label": usd_short(outcome["peak_mcap_usd"]) if outcome.get("peak_mcap_usd") is not None else "–"})
    return row


def coin_why(f: dict[str, Any]) -> str:
    """Why a coin stood out, in words a reader without the glossary understands."""
    bits = []
    ub = f.get("unique_buyers_5m")
    if ub is not None:
        bits.append(f"{fmt_n(ub)} verschillende kopers in 5 minuten")
    ns = f.get("net_sol_5m")
    if ns is not None:
        bits.append(f"{fmt_n(ns, 1)} SOL meer gekocht dan verkocht")
    share = f.get("largest_buyer_share_15m")
    if share is not None:
        bits.append(f"de grootste koper had {round(100 * float(share))}% van de instroom")
    b = f.get("bundled")
    if b is False:
        bits.append("geen bundel gezien")
    elif b is None:
        bits.append("bundeling onbekend")
    xm = f.get("x_mentions_1h")
    if xm:
        bits.append(f"{fmt_n(xm)} vermeldingen op X in het laatste uur")
    return (", ".join(bits) + ".") if bits else "de regels van dat moment gaven een observatie (details ontbreken)."


def coin_facts(f: dict[str, Any]) -> list[dict[str, Any]]:
    out = []
    if f.get("age_s") is not None:
        out.append({"key": "age", "label": "leeftijd bij observatie", "value": age_label(float(f["age_s"]))})
    if f.get("mcap_sol") is not None:
        out.append({"key": "mcap_sol", "label": "marktkapitalisatie toen", "value": f"{fmt_n(f['mcap_sol'], 1)} SOL"})
    if f.get("unique_buyers_5m") is not None:
        out.append({"key": "buyers", "label": "kopers in 5 min", "value": fmt_n(f["unique_buyers_5m"])})
    if f.get("net_sol_5m") is not None:
        out.append({"key": "net_sol", "label": "netto instroom 5 min", "value": f"{fmt_n(f['net_sol_5m'], 2)} SOL"})
    return out


def theme_why(f: dict[str, Any], ecfg: dict[str, Any]) -> str:
    hours = round(float((ecfg.get("meta") or {}).get("window_s", 10800)) / 3600)
    bits = []
    if f.get("n_coins") is not None:
        bits.append(f"{fmt_n(f.get('n_coins'))} nieuwe munten van {fmt_n(f.get('distinct_creators'))} verschillende makers in {hours} uur")
    if f.get("sol_inflow") is not None:
        bits.append(f"samen {fmt_n(f.get('sol_inflow'), 1)} SOL instroom en {fmt_n(f.get('unique_buyers'))} verschillende kopers")
    if f.get("burst_ratio_is_inf"):
        bits.append("dit thema kwam daarvoor niet voor")
    elif f.get("burst_ratio") is not None:
        bits.append(f"{fmt_n(f.get('burst_ratio'), 1)}× zo vaak als normaal")
    return (", ".join(bits) + ".") if bits else "een golf van munten rond één woord (details ontbreken)."


def theme_facts(f: dict[str, Any]) -> list[dict[str, Any]]:
    out = []
    if f.get("n_coins") is not None:
        out.append({"key": "coins", "label": "munten", "value": fmt_n(f["n_coins"])})
    if f.get("distinct_creators") is not None:
        out.append({"key": "creators", "label": "makers", "value": fmt_n(f["distinct_creators"])})
    if f.get("sol_inflow") is not None:
        out.append({"key": "inflow", "label": "instroom", "value": f"{fmt_n(f['sol_inflow'], 1)} SOL"})
    return out


def today_view(conn: sqlite3.Connection | None, settings: Settings, now: float) -> dict[str, Any]:
    """The dashboard's "Vandaag": today's observations (UTC day) in plain words, newest first, at most 12 shown."""
    day = day_key(now)
    v = observations_view(conn, settings, now, limit=12, day=day)
    v["day"] = day
    v["day_label"] = fmt_day(day)
    v["day_label_long"] = day_label_long(day)
    if conn is not None:
        t0 = day_start(now)
        v["counts"]["total_today"] = int(conn.execute("SELECT COUNT(*) FROM decisions WHERE kind IN ('observe', 'observe_meta') AND as_of >= ?", (t0,)).fetchone()[0])
    else:
        v["counts"]["total_today"] = None
    return v


# ----- reports: public shapes of out/recall/*.json ---------------------------------------------------------------------
def report_list(settings: Settings) -> list[dict[str, Any]]:
    """Every report as a public row with its key points: newest first, week reports before day reports of the same day."""
    files = data.report_files(settings)
    out = []
    for f in files:
        name = f["name"][:-3]  # without .md
        row = {"name": name, "kind": f["kind"], "kind_label": f["kind_label"], "day": f["day"], "day_label": f["day_label"], "href": f"/rapporten/{name}",
               "written_label": f["modified_label"]}
        js = _load_json(settings, name)
        if js is not None:
            row["key_points"] = day_key_points(js, settings) if f["kind"] == "dag" else week_key_points(js)
        else:
            row["key_points"] = {"status": "unknown", "status_label": PILL["unknown"], "items": []}
        out.append(row)
    return out


def report_exists(settings: Settings, name: str) -> bool:
    """Does a report of this name exist on disk? A cheap check (no parse) for a page the store has not computed yet."""
    if not data.REPORT_NAME.match(name + ".json"):
        return False
    return (data.report_dir(settings) / f"{name}.json").is_file()


def _load_json(settings: Settings, name: str) -> dict[str, Any] | None:
    if not data.REPORT_NAME.match(name + ".json"):
        return None
    p = data.report_dir(settings) / f"{name}.json"
    try:
        js = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return js if isinstance(js, dict) else None


def day_key_points(r: dict[str, Any], settings: Settings) -> dict[str, Any]:
    """The key points of one daily report, with the NIET GEDEKT rule (a day without coverage shows nothing but the hours)."""
    status = str(r.get("coverage_status") or ("complete" if r.get("complete") else "incomplete"))
    blank = status == "not_covered"
    u, rc, p, cvg = r.get("universe") or {}, r.get("recall") or {}, r.get("precision") or {}, r.get("coverage") or {}
    unk = r.get("unknowns") or {}
    hours_nc = unk.get("hours_not_covered", 24 - int(cvg.get("hours_covered", 0) or 0))
    lead = rc.get("lead_vs_graduation") or {}

    def num(v: Any) -> Any:
        return None if blank else v

    items = [
        {"key": "graduations", "label": "graduaties", "value": num(u.get("graduations")), "label_value": "–" if blank else fmt_n(u.get("graduations")),
         "note": "–" if blank else f"gemeten {fmt_n(u.get('measured', 0))}, nog bezig {fmt_n(u.get('pending', 0))}, zonder meting {fmt_n(u.get('missing', 0))}"},
        {"key": "winners", "label": "winnaars", "value": num(u.get("winners")), "label_value": "–" if blank else fmt_n(u.get("winners")),
         "note": "–" if blank else f"geen winnaar {fmt_n(u.get('not_winner', 0))}, uitkomst onbekend {fmt_n(u.get('outcome_unknown', 0))}"},
        {"key": "hit_rate", "label": "trefkans", "value": num(p.get("precision")), "label_value": "–" if blank else pct(p.get("precision")),
         "note": "–" if blank else f"{fmt_n(p.get('hits', 0))} raak van {fmt_n(p.get('known', 0))} bekend; {fmt_n(p.get('unknown', 0))} onbekend, {fmt_n(p.get('pending', 0))} nog bezig"},
        {"key": "recall", "label": "recall vóór graduatie", "value": num(rc.get("recall_before_graduation")),
         "label_value": "–" if blank else pct(rc.get("recall_before_graduation")),
         "note": "–" if blank else f"{fmt_n(rc.get('decision_before_graduation', 0))} van {fmt_n(rc.get('winners', 0))} winnaars"},
        {"key": "lead_time", "label": "mediane lead time", "value": num(lead.get("median_s")),
         "label_value": "–" if blank or lead.get("median_s") is None else minutes(lead.get("median_s")),
         "note": "–" if blank else (f"over {fmt_n(lead.get('n', 0))} winnaars met observatie" if lead.get("n") else "geen winnaar met observatie")},
        {"key": "hours_not_covered", "label": "uren NIET GEDEKT", "value": hours_nc, "label_value": fmt_n(hours_nc),
         "note": f"gedekt {fmt_n(cvg.get('hours_covered', 0))} van 24, compleet {fmt_n(cvg.get('hours_complete', 0))} van 24"},
    ]
    return {"status": status, "status_label": STATUS_NL.get(status, status), "blank": blank, "items": items,
            "note": "Geen enkel uur van deze dag was gedekt: het universum is onbekend, niet leeg, en elk percentage blijft leeg." if blank else None,
            "rules_version": r.get("rules_version_now"), "method_version": r.get("method_version")}


def week_key_points(r: dict[str, Any]) -> dict[str, Any]:
    seg = (r.get("segments") or [{}])[0]
    u, hit, rec, lt, g = seg.get("universe") or {}, seg.get("hit_rate") or {}, seg.get("recall") or {}, seg.get("lead_time") or {}, seg.get("gate") or {}
    hci = (hit.get("ci") or {})
    rb = ((rec.get("before_graduation") or {}).get("any") or {})
    rci = rb.get("ci") or {}
    med = (lt.get("any_vs_graduation") or {}).get("median_s")
    items = [
        {"key": "covered_days", "label": "gedekte dagen", "value": u.get("covered_days"), "label_value": f"{fmt_n(u.get('covered_days'))} van {fmt_n(r.get('days'))}",
         "note": "NIET GEDEKT: " + ", ".join(fmt_day(d) for d in (u.get("not_covered_days") or [])) if u.get("not_covered_days") else "elke dag gedekt"},
        {"key": "winners", "label": "winnaars", "value": u.get("winners"), "label_value": fmt_n(u.get("winners")),
         "note": f"van {fmt_n(u.get('graduations'))} graduaties op de gedekte dagen"},
        {"key": "hit_rate", "label": "trefkans", "value": hit.get("rate"), "label_value": pct(hit.get("rate")) if hit.get("elapsed") else "–",
         "note": ci_note(hci), "ci": ci_public(hci)},
        {"key": "recall", "label": "recall vóór graduatie", "value": rb.get("rate"), "label_value": pct(rb.get("rate")) if rb.get("n") is not None and (rec.get("before_graduation") or {}).get("denominator") else "–",
         "note": ci_note(rci), "ci": ci_public(rci)},
        {"key": "lead_time", "label": "mediane lead time", "value": med, "label_value": minutes(med) if med is not None else "–",
         "note": f"over {fmt_n((lt.get('any_vs_graduation') or {}).get('n', 0))} winnaars met observatie"},
    ]
    verdict = str(g.get("verdict") or "")
    return {"status": "week", "status_label": "weekrapport", "blank": False, "items": items, "verdict": verdict, "verdict_short": verdict_short(verdict),
            "verdict_kind": verdict_kind(verdict), "frozen": bool(seg.get("frozen")), "window_label": window_label(r), "note": None}


def ci_public(ci: dict[str, Any]) -> dict[str, Any]:
    iv = ci.get("interval") or {}
    if ci.get("status") == "ok" and iv:
        return {"status": "ok", "low": iv.get("low"), "high": iv.get("high"), "low_label": pct(iv.get("low")), "high_label": pct(iv.get("high")), "reason": None}
    return {"status": "few", "low": None, "high": None, "low_label": "–", "high_label": "–", "reason": ci.get("reason")}


def ci_note(ci: dict[str, Any]) -> str:
    c = ci_public(ci)
    if c["status"] == "ok":
        return f"bootstrapinterval {c['low_label']} tot {c['high_label']}"
    return "te weinig data voor een interval" + (f" ({c['reason']})" if c.get("reason") else "")


def verdict_kind(verdict: str) -> str:
    v = (verdict or "").upper()
    if v.startswith("GO"):
        return "go"
    if v.startswith("NO-GO"):
        return "nogo"
    return "few"


def verdict_short(verdict: str) -> str:
    return {"go": "GO", "nogo": "NO-GO", "few": "TE WEINIG DATA"}[verdict_kind(verdict)]


def window_label(r: dict[str, Any]) -> str:
    return (f"{fmt_day(str(r.get('start_day')))} tot en met {fmt_day(str(r.get('end_day')))} ({fmt_n(r.get('days'))} dagen"
            + (", hele kalenderweek)" if r.get("calendar_week") else ", rollend venster)"))


def reports_view(settings: Settings, now: float) -> dict[str, Any]:
    """`/api/public/reports`: the list with key points, plus the two newest for the dashboard."""
    rows = report_list(settings)
    return {**data.stamp(now), "rows": rows, "latest": rows[:2], "count": len(rows),
            "note": None if rows else "Nog geen rapporten: het eerste dagrapport verschijnt de ochtend na de eerste gedekte dag."}


# ----- the public day report ------------------------------------------------------------------------------------------
def day_report_view(settings: Settings, name: str, now: float) -> dict[str, Any] | None:
    """The structured public view of one daily report; None when the report does not exist."""
    if name.startswith("week-"):
        return None
    r = _load_json(settings, name)
    if r is None:
        return None
    kp = day_key_points(r, settings)
    blank = kp["blank"]
    cvg = r.get("coverage") or {}
    hours = []
    for h in cvg.get("hours") or []:
        covered = bool(h.get("covered"))
        hours.append({"hour": h["hour"], "clock": f"{h['hour'][-2:]}:00", "covered": covered, "complete": bool(h.get("complete")),
                      "status": "complete" if h.get("complete") else ("covered" if covered else "not_covered"),
                      "creates": h.get("creates_union") if covered else None, "grads": h.get("grads") if covered else None})
    winners = []
    for w in r.get("winners") or []:
        lead = w.get("lead_s_vs_graduation")
        winners.append({"mint": w.get("mint"), "mint_short": short_mint(w.get("mint")), "name": w.get("name") or "naam onbekend", "symbol": w.get("symbol"),
                        "graduated_label": fmt_time(_iso_ts(w.get("graduated_at"))), "bundled": w.get("bundled"),
                        "bundled_label": "ja" if w.get("bundled") else ("nee" if w.get("bundled") is False else "onbekend"),
                        "observed": w.get("decision_as_of") is not None, "observed_label": "ja" if w.get("decision_as_of") else "nee",
                        "lead_s": lead, "lead_label": minutes(lead) if lead is not None else "–",
                        "theme": w.get("theme_term"), "peak_label": usd_short(w.get("peak_mcap_usd")) if w.get("peak_mcap_usd") is not None else "onbekend"})
    u, p, rc, lt, unk, tr = (r.get(k) or {} for k in ("universe", "precision", "recall", "lead_time", "unknowns", "tracker"))
    lead = rc.get("lead_vs_graduation") or {}
    out = {
        **data.stamp(now), "kind": "dag", "name": name, "day": r.get("day") or name, "day_label": fmt_day(str(r.get("day") or name)),
        "day_label_long": day_label_long(str(r.get("day") or name)), "status": kp["status"], "status_label": kp["status_label"], "blank": blank,
        "note": kp["note"], "generated_label": fmt_time(_iso_ts(r.get("generated_at"))), "key_points": kp["items"],
        "coverage": {"hours": hours, "hours_covered": cvg.get("hours_covered"), "hours_complete": cvg.get("hours_complete"),
                     "hours_not_covered": unk.get("hours_not_covered"), "creates": None if blank else cvg.get("creates_union"),
                     "grads": None if blank else cvg.get("grads"), "first_source_start_label": fmt_time(_iso_ts(cvg.get("first_source_start"))) if cvg.get("first_source_start") else None},
        "universe": None if blank else {
            "graduations": u.get("graduations"), "measured": u.get("measured"), "pending": u.get("pending"), "missing": u.get("missing"),
            "winners": u.get("winners"), "winners_organic": u.get("winners_organic"), "winners_bundled": u.get("winners_bundled"),
            "winners_bundled_unknown": u.get("winners_bundled_unknown"), "not_winner": u.get("not_winner"), "outcome_unknown": u.get("outcome_unknown"),
            "outcome_unknown_sparse_samples": u.get("outcome_unknown_sparse_samples"), "without_create": u.get("without_create"),
            "tracker_coverage_share": u.get("tracker_coverage_share"), "tracker_coverage_label": pct(u.get("tracker_coverage_share")),
            "tracker_gap_minutes": round(float(tr.get("max_sample_gap_s", 600)) / 60) if tr else 10},
        "observations": None if blank else {
            "decisions": p.get("decisions"), "hits": p.get("hits"), "misses": p.get("misses"), "known": p.get("known"), "unknown": p.get("unknown"),
            "pending": p.get("pending"), "hit_rate": p.get("precision"), "hit_rate_label": pct(p.get("precision")) if p.get("known") else "–",
            "themes": sum(int(v.get("observe_meta", 0) or 0) for v in (r.get("observations_by_rules_version") or {}).values())},
        "recall": None if blank else {
            "winners": rc.get("winners"), "before_graduation": rc.get("decision_before_graduation"), "any_before_graduation": rc.get("any_before_graduation"),
            "theme_before_graduation": rc.get("theme_before_graduation"), "rate": rc.get("recall_before_graduation"),
            "rate_label": pct(rc.get("recall_before_graduation")) if rc.get("winners") else "–",
            "lead_median_s": lead.get("median_s"), "lead_median_label": minutes(lead.get("median_s")) if lead.get("median_s") is not None else "–",
            "lead_n": lead.get("n"), "lead_positive": lead.get("positive"), "lead_negative": lead.get("negative_or_zero")},
        "winners": [] if blank else winners,
        "unknowns": {k: unk.get(k) for k in ("graduations_outcome_unknown", "graduations_outcome_unknown_sparse_samples", "graduations_pending",
                                             "graduations_missing_outcome", "graduations_bundled_unknown", "decisions_outcome_unknown", "decisions_pending",
                                             "graduations_without_create", "hours_not_covered")},
        "versions": {"rules_version": r.get("rules_version_now"), "method_version": r.get("method_version"),
                     "rules_versions_in_decisions": r.get("rules_versions_in_decisions") or []},
        "winner_definition": r.get("winner_definition") or {},
    }
    return out


def _iso_ts(v: Any) -> float | None:
    return data.iso_to_ts(v)


# ----- the public week report -----------------------------------------------------------------------------------------
def week_report_view(settings: Settings, name: str, now: float) -> dict[str, Any] | None:
    if not name.startswith("week-"):
        return None
    r = _load_json(settings, name)
    if r is None:
        return None
    return week_public(r, name, now)


def week_public(r: dict[str, Any], name: str, now: float) -> dict[str, Any]:
    """The public shape of a week report: the first segment (the frozen rules) in full, other segments summarized."""
    segs = r.get("segments") or []
    first = segs[0] if segs else {}
    kp = week_key_points(r)
    gate = first.get("gate") or {}
    criteria = []
    for c in gate.get("criteria") or []:
        key = str(c.get("key"))
        v, t = c.get("value"), c.get("threshold")
        if v is None:
            vs = "–"
        elif key in ("hit_rate_ci_low", "recall_before_graduation_ci_low"):
            vs = pct(v)
        elif key == "median_lead_s_before_graduation":
            vs = minutes(v)
        elif isinstance(v, float):
            vs = fmt_n(v, 1)
        else:
            vs = fmt_n(v)
        ts = pct(t) if key in ("hit_rate_ci_low", "recall_before_graduation_ci_low") else (minutes(t) if key == "median_lead_s_before_graduation" else fmt_n(t))
        st = str(c.get("status") or "")
        criteria.append({"key": key, "label": CRITERIA_NL.get(key, c.get("label")), "value": v, "value_label": vs, "threshold": t,
                         "threshold_label": f"{OPS_NL.get(str(c.get('op')), c.get('op'))} {ts}", "status": st,
                         "status_kind": {"voldaan": "ok", "niet voldaan": "fail"}.get(st, "few")})
    days = []
    for d in first.get("days") or []:
        covered = bool(d.get("covered"))
        hit = d.get("hit") or {}
        rec = d.get("recall") or {}
        obs = d.get("observations") or {}
        leads = (d.get("leads") or {}).get("any_vs_graduation") or []
        rate = (hit["hits"] / hit["elapsed"]) if covered and hit.get("elapsed") else None
        rrate = (rec["any_before_graduation"] / rec["winners"]) if covered and rec.get("winners") else None
        days.append({"day": d["day"], "day_label": fmt_day(d["day"]), "covered": covered, "status": str(d.get("coverage_status") or ("incomplete" if covered else "not_covered")),
                     "status_label": STATUS_NL.get(str(d.get("coverage_status") or ""), "NIET GEDEKT" if not covered else "NIET COMPLEET"),
                     "gate_complete": bool(d.get("gate_complete")), "graduations": d.get("graduations") if covered else None,
                     "winners": d.get("winners") if covered else None, "observations": (int(obs.get("observe", 0)) + int(obs.get("observe_meta", 0))) if covered else None,
                     "observations_coins": obs.get("observe") if covered else None, "observations_themes": obs.get("observe_meta") if covered else None,
                     "hit_rate": rate, "hit_rate_label": pct(rate) if rate is not None else "–", "hits": hit.get("hits") if covered else None,
                     "elapsed": hit.get("elapsed") if covered else None, "pending": hit.get("pending") if covered else None,
                     "recall": rrate, "recall_label": pct(rrate) if rrate is not None else "–",
                     "lead_median_s": _median(leads), "hours_not_covered": d.get("hours_not_covered"),
                     "outcome_unknown": d.get("outcome_unknown") if covered else None})
    hit, rec, lt, u, obs, bs = (first.get(k) or {} for k in ("hit_rate", "recall", "lead_time", "universe", "observations", "bootstrap"))
    rb = (rec.get("before_graduation") or {})
    others = [{"rules_group_short": str(s.get("rules_group") or "")[:19], "frozen": bool(s.get("frozen")), "method_version": s.get("method_version"),
               "rules_versions": s.get("rules_versions") or [], "verdict": (s.get("gate") or {}).get("verdict")} for s in segs[1:]]
    # milestone 11: rules_versions pooled into the frozen segment through a proven-equivalent fingerprint are named as such
    equivalents = [{"fingerprint_short": str(e.get("rules_fingerprint") or "")[:19], "rules_versions": e.get("rules_versions") or [], "pr": e.get("pr"), "date": e.get("date")}
                   for e in first.get("equivalents") or [] if isinstance(e, dict)]
    equivalence_note = ("; ".join(f"rules_version(s) {', '.join(e['rules_versions'])} (vingerafdruk {e['fingerprint_short']}) tellen mee bij de bevroren regels als bewezen "
                                  f"gelijkwaardige berekening: dezelfde regels, alleen de rekentijd veranderde (PR #{e['pr']}, {fmt_day(str(e['date'] or ''))})"
                                  for e in equivalents) + ".") if equivalents else None
    return {
        **data.stamp(now), "kind": "week", "name": name, "start_day": r.get("start_day"), "end_day": r.get("end_day"), "days_total": r.get("days"),
        "window_label": window_label(r), "calendar_week": bool(r.get("calendar_week")), "generated_label": fmt_time(_iso_ts(r.get("generated_at"))),
        "frozen": bool(first.get("frozen")), "rules_versions": first.get("rules_versions") or [], "method_version": first.get("method_version"),
        "equivalents": equivalents, "equivalence_note": equivalence_note,
        "fingerprint_short": str(first.get("rules_group") or "")[:19], "key_points": kp["items"], "verdict": kp["verdict"], "verdict_short": kp["verdict_short"],
        "verdict_kind": kp["verdict_kind"], "verdict_plain": verdict_plain(kp["verdict_kind"], criteria), "gate_note": gate.get("note"), "criteria": criteria,
        "bootstrap": {"enough_data": bool(bs.get("enough_data")), "reason": bs.get("reason"), "draws": bs.get("draws"), "min_days": bs.get("min_days"),
                      "min_winners": bs.get("min_winners"), "alpha": bs.get("alpha")},
        "universe": {"covered_days": u.get("covered_days"), "complete_days": u.get("complete_days"), "not_covered_days": u.get("not_covered_days") or [],
                     "not_covered_days_label": ", ".join(fmt_day(d) for d in (u.get("not_covered_days") or [])) or "geen", "hours_not_covered": u.get("hours_not_covered"),
                     "graduations": u.get("graduations"), "measured": u.get("measured"), "pending": u.get("pending"), "winners": u.get("winners"),
                     "winners_organic": u.get("winners_organic"), "winners_bundled": u.get("winners_bundled"), "winners_bundled_unknown": u.get("winners_bundled_unknown"),
                     "not_winner": u.get("not_winner"), "outcome_unknown": u.get("outcome_unknown"), "tracker_coverage_share": u.get("tracker_coverage_share"),
                     "tracker_coverage_label": pct(u.get("tracker_coverage_share"))},
        "hit_rate": {"rate": hit.get("rate"), "rate_label": pct(hit.get("rate")) if hit.get("elapsed") else "–", "rate_known": hit.get("rate_known"),
                     "rate_known_label": pct(hit.get("rate_known")) if (hit.get("hits", 0) + hit.get("misses", 0)) else "–", "decisions": hit.get("decisions"),
                     "hits": hit.get("hits"), "misses": hit.get("misses"), "unknown": hit.get("unknown"), "pending": hit.get("pending"), "elapsed": hit.get("elapsed"),
                     "ci": ci_public(hit.get("ci") or {})},
        "recall": {"winners": rec.get("winners"), "rate": (rb.get("any") or {}).get("rate"), "rate_label": pct((rb.get("any") or {}).get("rate")) if rb.get("denominator") else "–",
                   "n": (rb.get("any") or {}).get("n"), "coin_n": (rb.get("coin") or {}).get("n"), "theme_n": (rb.get("theme") or {}).get("n"),
                   "denominator": rb.get("denominator"), "ci": ci_public((rb.get("any") or {}).get("ci") or {})},
        "lead_time": {k: (lt.get("any_vs_graduation") or {}).get(k) for k in ("n", "median_s", "p25_s", "p75_s", "positive", "negative_or_zero")}
        | {"median_label": minutes((lt.get("any_vs_graduation") or {}).get("median_s")) if (lt.get("any_vs_graduation") or {}).get("median_s") is not None else "–",
           "p25_label": minutes((lt.get("any_vs_graduation") or {}).get("p25_s")) if (lt.get("any_vs_graduation") or {}).get("p25_s") is not None else "–",
           "p75_label": minutes((lt.get("any_vs_graduation") or {}).get("p75_s")) if (lt.get("any_vs_graduation") or {}).get("p75_s") is not None else "–"},
        "observations": {"coins": obs.get("observe"), "themes": obs.get("observe_meta"), "per_day": obs.get("mean_per_day"),
                         "per_day_label": fmt_n(obs.get("mean_per_day"), 1) if obs.get("mean_per_day") is not None else "–", "covered_days": obs.get("covered_days")},
        "days": days, "other_segments": others, "winner_definition": r.get("winner_definition") or {},
    }


def _median(values: list[Any]) -> float | None:
    vs = sorted(float(v) for v in values if v is not None)
    if not vs:
        return None
    n = len(vs)
    return vs[n // 2] if n % 2 else (vs[n // 2 - 1] + vs[n // 2]) / 2.0


CRITERIA_NL = {
    "complete_days": "complete dagen (piekuren 12:00 tot 02:00 UTC gedekt)",
    "winners": "winnaars in het universum",
    "hit_rate_ci_low": "ondergrens van het bootstrapinterval van de trefkans",
    "recall_before_graduation_ci_low": "ondergrens van het bootstrapinterval van de recall vóór graduatie",
    "median_lead_s_before_graduation": "mediane lead time vóór graduatie",
    "observations_per_day": "observaties per gedekte dag, gemiddeld",
}
OPS_NL = {">=": "minstens", "<=": "hoogstens", ">": "meer dan", "<": "minder dan", "==": "precies"}


def verdict_plain(kind: str, criteria: list[dict[str, Any]]) -> str:
    """The go/no-go verdict in one plain sentence."""
    ok = [c for c in criteria if c["status_kind"] == "ok"]
    fail = [c for c in criteria if c["status_kind"] == "fail"]
    few = [c for c in criteria if c["status_kind"] == "few"]
    if kind == "go":
        return "Alle vooraf vastgelegde criteria zijn gehaald. Het rapport beslist niets; of de community-berichten aangaan is een besluit van de eigenaar."
    if not criteria:
        return "Er is nog geen toets: het eerste weekrapport moet nog geschreven worden."
    parts = [f"{len(ok)} van {len(criteria)} criteria gehaald"]
    if fail:
        parts.append(f"{len(fail)} niet gehaald")
    if few:
        parts.append(f"voor {len(few)} is er nog te weinig data")
    return ", ".join(parts) + ". Zolang niet alles gehaald is, blijven de community-berichten uit. De regels zijn bevroren en worden niet achteraf aangepast."


# ----- /api/public/week: the measurement-week card ----------------------------------------------------------------------
def week_view(settings: Settings, now: float) -> dict[str, Any]:
    """The "Meetweek" card: the position in the week plus the latest week report's figures (or "te weinig data")."""
    pos = week_position(settings, now)
    out: dict[str, Any] = {**data.stamp(now), "position": pos, "found": False}
    found = data._latest_json(settings, week=True)
    if found is None:
        out["note"] = "Nog geen weekrapport: het eerste verschijnt de ochtend na de eerste gedekte dag."
        out["criteria"] = gate_criteria_from_config(settings)
        return out
    name, r = found
    wp = week_public(r, name[:-5], now)
    out.update({"found": True, "name": wp["name"], "href": f"/rapporten/{wp['name']}", "window_label": wp["window_label"], "generated_label": wp["generated_label"],
                "frozen": wp["frozen"], "verdict": wp["verdict"], "verdict_short": wp["verdict_short"], "verdict_kind": wp["verdict_kind"], "verdict_plain": wp["verdict_plain"],
                "criteria": wp["criteria"], "universe": wp["universe"], "hit_rate": wp["hit_rate"], "recall": wp["recall"], "lead_time": wp["lead_time"],
                "observations": wp["observations"], "bootstrap": wp["bootstrap"], "days": wp["days"], "key_points": wp["key_points"]})
    return out


def gate_criteria_from_config(settings: Settings) -> list[dict[str, Any]]:
    """The frozen criteria as rows without values (before the first week report exists)."""
    gate = (settings.config("measure") or {}).get("community_gate") or {}
    rows = [
        ("complete_days", gate.get("min_complete_days"), fmt_n(gate.get("min_complete_days"))),
        ("winners", gate.get("min_winners"), fmt_n(gate.get("min_winners"))),
        ("hit_rate_ci_low", gate.get("min_hit_rate_ci_low"), pct(gate.get("min_hit_rate_ci_low"))),
        ("recall_before_graduation_ci_low", gate.get("min_recall_before_graduation_ci_low"), pct(gate.get("min_recall_before_graduation_ci_low"))),
        ("median_lead_s_before_graduation", gate.get("min_median_lead_s_before_graduation"), minutes(gate.get("min_median_lead_s_before_graduation"))),
        ("observations_per_day", gate.get("max_observations_per_day"), fmt_n(gate.get("max_observations_per_day"))),
    ]
    return [{"key": k, "label": CRITERIA_NL[k], "value": None, "value_label": "–", "threshold": t,
             "threshold_label": ("hoogstens " if k == "observations_per_day" else "minstens ") + ts, "status": "te weinig data", "status_kind": "few"}
            for k, t, ts in rows]


# ----- /methode: the frozen rules in plain words ----------------------------------------------------------------------
def method_view(settings: Settings) -> dict[str, Any]:
    from ..notify.format import coin_rules_text, meta_rules_text

    e = settings.config("engine")
    m = settings.config("measure")
    gate = m.get("community_gate") or {}
    fr = gate.get("frozen_rules") or {}
    w = m["winner"]
    bs = (m.get("week") or {}).get("bootstrap") or {}
    return {
        "coin_rules": coin_rules_text(e.get("coin") or {}), "meta_rules": meta_rules_text(e.get("meta") or {}),
        "cooldown_label": minutes(e.get("decision_cooldown_s")),
        "winner": {"peak_label": usd_short(w["peak_mcap_usd"]), "volume_label": usd_short(w["min_volume_usd"]), "hours": int(w["within_s"] // 3600)},
        "horizons": [int(h) // 3600 for h in m.get("horizons_s") or []],
        "tracker_minutes": round(float((m.get("tracker") or {}).get("max_sample_gap_s", 600)) / 60),
        "sample_minutes": round(float((m.get("sampling") or {}).get("interval_s", 300)) / 60),
        "bootstrap": {"draws": bs.get("draws"), "min_days": bs.get("min_days"), "min_winners": bs.get("min_winners"), "alpha": bs.get("alpha"),
                      "draws_label": fmt_n(bs.get("draws"))},
        "frozen": {"rules_version": fr.get("rules_version"), "since_label": fmt_time(_iso_ts(fr.get("since"))) if fr.get("since") else "onbekend",
                   "first_full_day_label": fmt_day(str(fr.get("first_full_day") or "")), "fingerprint_short": str(fr.get("rules_fingerprint") or "")[:19],
                   "fingerprint_now_short": rules_fingerprint()[:19], "same": rules_fingerprint() == fr.get("rules_fingerprint"), "rules_version_now": rules_version(),
                   # milestone 11: a fingerprint proven to compute the frozen rules' outcomes (config: frozen_rules_equivalents)
                   "equivalent": runs_frozen_rules(gate) and rules_fingerprint() != fr.get("rules_fingerprint"),
                   "equivalent_entry": {k: v for k, v in (rules_equivalents(gate).get(rules_fingerprint()) or {}).items() if k in ("date", "pr", "proof_test")}},
        "criteria": gate_criteria_from_config(settings), "method_version": m.get("method_version"),
        "warning": warning_text(settings), "banner": banner_text(settings),
    }


# =====================================================================================================================
# The live dashboard (milestone 9, second iteration): what the radar sees right now, as processed data.
# =====================================================================================================================
LIVE_WINDOW_S = 300        # "nu": the last five minutes
LIVE_LOOK_S = 900          # how far back the hot-coins query looks (the largest-buyer share uses 15 minutes, like the engine)
SOL_USD_MAX_AGE_S = 3600   # a cached SOL/USD price older than this is not used
SAMPLE_MAX_AGE_S = 7200    # a tracker sample older than this is "oud"
HOT_LIMIT = 10
NEW_LIMIT = 12
THEME_LIMIT = 8
GRAD_LIMIT = 10


def sol_label(x: Any, decimals: int = 1) -> str:
    return "onbekend" if x is None else f"{fmt_n(x, decimals)} SOL"


def _curve_params(settings: Settings):
    from ..chain.curve import CurveParams

    return CurveParams.from_config(settings.config("pumpfun"))


def sol_usd_now(conn: sqlite3.Connection, now: float) -> dict[str, Any]:
    """The SOL/USD price the tracker cached last (kv `sol_usd.cache`), when it is recent enough; nothing is fetched."""
    raw = conn.execute("SELECT value FROM kv WHERE key='sol_usd.cache'").fetchone()
    out: dict[str, Any] = {"price": None, "price_label": "onbekend", "age_s": None, "age_label": "–", "known": False}
    if raw is None:
        return out
    try:
        d = json.loads(raw[0])
        price, ts = float(d["price"]), float(d["ts"])
    except (ValueError, TypeError, KeyError):
        return out
    age = max(0.0, now - ts)
    if age > SOL_USD_MAX_AGE_S or price <= 0:
        return out
    out.update({"price": price, "price_label": f"${fmt_n(price, 2)}", "age_s": round(age), "age_label": age_label(age), "known": True})
    return out


def _mcap(virtual_sol: Any, virtual_token: Any, params) -> float | None:
    from ..chain.curve import CurveState

    try:
        if virtual_sol is None or virtual_token is None or float(virtual_token) <= 0:
            return None
        return float(CurveState(int(virtual_sol), int(virtual_token)).mcap_sol(params))
    except (TypeError, ValueError, ZeroDivisionError):
        return None


def _progress(virtual_sol: Any, virtual_token: Any, params) -> float | None:
    from ..chain.curve import CurveState, graduation_progress

    try:
        if virtual_sol is None or virtual_token is None:
            return None
        return max(0.0, min(1.0, float(graduation_progress(CurveState(int(virtual_sol), int(virtual_token)), params))))
    except (TypeError, ValueError, ZeroDivisionError):
        return None


def freshness(conn: sqlite3.Connection, now: float) -> dict[str, Any]:
    """The ingest heartbeat as a status pill: vers, oud or onbekend; the time of the last message from a source."""
    kv = _kv(conn, ("ingest.heartbeat", "ingest.pumpportal.last_msg_at", "ingest.rpc_logs.last_msg_at"))
    hb = _float(kv.get("ingest.heartbeat"))
    last_msg = max((v for v in (_float(kv.get("ingest.pumpportal.last_msg_at")), _float(kv.get("ingest.rpc_logs.last_msg_at"))) if v is not None), default=None)
    age = None if hb is None else max(0.0, now - hb)
    if age is None:
        out = {"status": "unknown", "label": PILL["unknown"], "age_s": None, "age_label": "onbekend", "note": "De radar heeft nog nooit gedraaid."}
    elif age <= FRESH_S:
        out = {"status": "fresh", "label": "live", "age_s": round(age), "age_label": age_label(age), "note": "De radar luistert nu."}
    elif age <= STALE_S:
        out = {"status": "stale", "label": PILL["stale"], "age_s": round(age), "age_label": age_label(age), "note": f"Het laatste levensteken van de radar is {age_label(age)} oud."}
    else:
        out = {"status": "stale", "label": PILL["stale"], "age_s": round(age), "age_label": age_label(age),
               "note": f"De radar heeft {age_label(age)} niets van zich laten horen: de cijfers hieronder lopen achter."}
    out["last_event_at"] = utc_iso(last_msg)
    out["last_event_label"] = clock_label(last_msg)
    return out


def _active_watchlist(conn: sqlite3.Connection) -> dict[str, str]:
    return {str(r["mint"]): str(r["reason"] or "probation") for r in conn.execute("SELECT mint, reason FROM watchlist WHERE active=1")}


def _observed_mints(conn: sqlite3.Connection, mints: list[str]) -> dict[str, float]:
    """mint -> as_of of its earliest coin observation (only for the given mints)."""
    if not mints:
        return {}
    marks = ",".join("?" for _ in mints)
    return {str(r[0]): float(r[1]) for r in conn.execute(
        f"SELECT mint, MIN(as_of) FROM decisions WHERE kind='observe' AND mint IN ({marks}) GROUP BY mint", mints)}


def _graduated_mints(conn: sqlite3.Connection, mints: list[str]) -> dict[str, float]:
    if not mints:
        return {}
    marks = ",".join("?" for _ in mints)
    return {str(r[0]): float(r[1]) for r in conn.execute(f"SELECT mint, {ET} FROM migrations WHERE mint IN ({marks})", mints)}


REASON_NL = {"probation": "nieuw (eerste 5 minuten)", "early_buyers": "vroege kopers", "meta_burst": "thema", "observe": "observatie", "graduation": "gegradueerd"}


def live_view(conn: sqlite3.Connection | None, settings: Settings, now: float) -> dict[str, Any]:
    """The hero of the live dashboard: the tempo of new coins, the money moving on the watched coins right now, the
    graduations and observations of today, the freshness, the SOL price the figures are converted with."""
    site = settings.web_site
    out: dict[str, Any] = {**data.stamp(now), "site": {"name": site["site_name"], "product": site["product"], "tagline": site["tagline"], "domain": site["domain"]},
                           "day": day_key(now), "day_label": fmt_day(day_key(now)), "refresh_s": int(site.get("live_refresh_s") or 15), "warning": warning_text(settings),
                           "banner": banner_text(settings), "window_s": LIVE_WINDOW_S}
    if conn is None:
        out.update({"db_exists": False, "freshness": {"status": "unknown", "label": PILL["unknown"], "age_s": None, "age_label": "onbekend",
                                                      "note": "De radar heeft nog geen gegevens opgeslagen.", "last_event_at": None, "last_event_label": "onbekend"},
                    "sol_usd": {"price": None, "price_label": "onbekend", "age_s": None, "age_label": "–", "known": False},
                    "creates": {"last_10m": None, "per_min": None, "per_min_label": "–", "last_hour": None, "last_hour_label": "–", "hour_covered": False},
                    "watched": {"active": None, "buyers_5m": None, "buyers_label": "–", "net_sol_5m": None, "net_sol_label": "–", "trades_5m": None, "with_trades_5m": None},
                    "graduations": {"last_hour": None, "last_hour_label": "–", "today": None, "today_label": "–", "day_covered": False},
                    "observations": {"today": None, "today_label": "–", "last_label": "nog geen"},
                    "headline": "De radar heeft nog geen gegevens: er valt nog niets te tonen."})
        return out
    out["db_exists"] = True
    out["freshness"] = freshness(conn, now)
    out["sol_usd"] = sol_usd_now(conn, now)
    hour_covered = _hour_covered(conn, now)
    c10 = _count_since(conn, "coins", now - 600)
    c60 = _count_since(conn, "coins", now - 3600)
    out["creates"] = {"last_10m": c10 if hour_covered else None, "per_min": round(c10 / 10.0, 1) if hour_covered else None,
                      "per_min_label": fmt_n(c10 / 10.0, 1) if hour_covered else "–", "last_hour": c60 if hour_covered else None,
                      "last_hour_label": fmt_n(c60) if hour_covered else "–", "hour_covered": hour_covered}
    active = _active_watchlist(conn)
    mints = list(active)
    buyers = net = trades = with_trades = buys = sells = 0
    if mints:
        marks = ",".join("?" for _ in mints)
        r = conn.execute(
            f"SELECT COUNT(DISTINCT CASE WHEN is_buy=1 THEN buyer END) AS buyers, COALESCE(SUM(CASE WHEN is_buy=1 THEN sol ELSE 0 END), 0) AS buy_sol,"
            f" COALESCE(SUM(CASE WHEN is_buy=0 THEN sol ELSE 0 END), 0) AS sell_sol, COUNT(*) AS trades, COUNT(DISTINCT mint) AS mints,"
            f" SUM(is_buy) AS buys FROM trades WHERE mint IN ({marks}) AND received_at >= ? AND {ET} >= ?", (*mints, now - LIVE_WINDOW_S - 60, now - LIVE_WINDOW_S)).fetchone()
        buyers, net, trades, with_trades = int(r["buyers"] or 0), float(r["buy_sol"]) - float(r["sell_sol"]), int(r["trades"] or 0), int(r["mints"] or 0)
        buys = int(r["buys"] or 0)
        sells = trades - buys
    out["watched"] = {"active": len(mints), "buyers_5m": buyers, "buyers_label": fmt_n(buyers), "net_sol_5m": round(net, 2), "net_sol_label": sol_label(net, 1),
                      "trades_5m": trades, "buys_5m": buys, "sells_5m": sells, "with_trades_5m": with_trades}
    t0 = day_start(now)
    day_covered = _day_covered(conn, now)
    g60 = _count_since(conn, "migrations", now - 3600)
    gday = _count_since(conn, "migrations", t0)
    out["graduations"] = {"last_hour": g60 if hour_covered else None, "last_hour_label": fmt_n(g60) if hour_covered else "–",
                          "today": gday if day_covered else None, "today_label": fmt_n(gday) if day_covered else "–", "day_covered": day_covered}
    obs_today = int(conn.execute("SELECT COUNT(*) FROM decisions WHERE kind IN ('observe', 'observe_meta') AND as_of >= ?", (t0,)).fetchone()[0])
    last_dec = _last_observation_at(conn)
    out["observations"] = {"today": obs_today, "today_label": fmt_n(obs_today), "last_at": utc_iso(last_dec), "last_label": fmt_time(last_dec) if last_dec else "nog geen"}
    out["headline"] = live_headline(out)
    return out


def live_headline(v: dict[str, Any]) -> str:
    fr = v["freshness"]
    if fr["status"] == "unknown":
        return "De radar heeft nog geen gegevens: er valt nog niets te tonen."
    c, w, g = v["creates"], v["watched"], v["graduations"]
    lead = "Nu" if fr["status"] == "fresh" else f"Bij het laatste levensteken ({fr['age_label']} geleden)"
    tempo = f"{fmt_n(c['per_min'], 1)} nieuwe munten per minuut" if c["per_min"] is not None else "een tempo dat nog niet gedekt is"
    money = (f"op de {fmt_n(w['active'])} gevolgde munten {fmt_n(w['buyers_5m'])} {'koper' if w['buyers_5m'] == 1 else 'kopers'} en {sol_label(w['net_sol_5m'], 1)} netto instroom in 5 minuten"
             if w["active"] else "nog geen gevolgde munten")
    grads = f"{fmt_n(g['last_hour'])} {'graduatie' if g['last_hour'] == 1 else 'graduaties'} in het laatste uur" if g["last_hour"] is not None else "het aantal graduaties van dit uur nog niet gedekt"
    return f"{lead}: {tempo}, {money}, {grads}."


def hot_coins_view(conn: sqlite3.Connection | None, settings: Settings, now: float, limit: int = HOT_LIMIT) -> dict[str, Any]:
    """Where the money goes right now: the watched coins with the most net SOL inflow in the last five minutes."""
    out: dict[str, Any] = {**data.stamp(now), "window_s": LIVE_WINDOW_S, "look_s": LIVE_LOOK_S, "rows": [], "watched": 0, "with_trades": 0,
                           "definition": ("De radar volgt elke nieuwe munt zijn eerste 5 minuten en daarna de munten die aan zijn regels raken; alleen van die munten "
                                          "ziet hij elke trade. Netto instroom = gekocht min verkocht, in SOL, over de laatste 5 minuten.")}
    if conn is None:
        out["note"] = "Er zijn nog geen gegevens."
        return out
    active = _active_watchlist(conn)
    out["watched"] = len(active)
    if not active:
        out["note"] = "De radar volgt op dit moment geen munten (de watchlist is leeg)."
        return out
    mints = list(active)
    marks = ",".join("?" for _ in mints)
    w5 = now - LIVE_WINDOW_S
    rows = conn.execute(
        f"SELECT mint, COUNT(DISTINCT CASE WHEN is_buy=1 AND {ET} >= ? THEN buyer END) AS buyers_5m,"
        f" COALESCE(SUM(CASE WHEN is_buy=1 AND {ET} >= ? THEN sol ELSE 0 END), 0) AS buy_5m, COALESCE(SUM(CASE WHEN is_buy=0 AND {ET} >= ? THEN sol ELSE 0 END), 0) AS sell_5m,"
        f" SUM(CASE WHEN {ET} >= ? THEN 1 ELSE 0 END) AS trades_5m, SUM(CASE WHEN {ET} >= ? AND is_buy=1 THEN 1 ELSE 0 END) AS buys_5m,"
        f" COALESCE(SUM(CASE WHEN is_buy=1 THEN sol ELSE 0 END), 0) AS buy_15m, COUNT(*) AS trades_15m, MAX({ET}) AS last_t"
        f" FROM trades WHERE mint IN ({marks}) AND received_at >= ? AND {ET} >= ? GROUP BY mint",
        (w5, w5, w5, w5, w5, *mints, now - LIVE_LOOK_S - 60, now - LIVE_LOOK_S)).fetchall()
    stats = [dict(r) for r in rows if int(r["trades_5m"] or 0) > 0]
    out["with_trades"] = len(stats)
    stats.sort(key=lambda r: (float(r["buy_5m"]) - float(r["sell_5m"]), int(r["buyers_5m"] or 0), int(r["trades_5m"] or 0)), reverse=True)
    top = stats[: int(limit)]
    if not top:
        out["note"] = f"Geen trades in de laatste {LIVE_WINDOW_S // 60} minuten op de {fmt_n(len(active))} gevolgde munten."
        return out
    params = _curve_params(settings)
    susd = sol_usd_now(conn, now)
    observed = _observed_mints(conn, [r["mint"] for r in top])
    graduated = _graduated_mints(conn, [r["mint"] for r in top])
    max_net = max(float(r["buy_5m"]) - float(r["sell_5m"]) for r in top)
    result = []
    for r in top:
        mint = str(r["mint"])
        c = conn.execute(f"SELECT name, symbol, creator, {ET} AS t, dev_buy_sol FROM coins WHERE mint=?", (mint,)).fetchone()
        last = conn.execute(f"SELECT virtual_sol, virtual_token FROM trades WHERE mint=? AND received_at >= ? AND virtual_sol IS NOT NULL ORDER BY {ET} DESC, received_at DESC LIMIT 1",
                            (mint, now - LIVE_LOOK_S - ET_MARGIN_S)).fetchone()
        big = conn.execute(f"SELECT buyer, SUM(sol) AS s FROM trades WHERE mint=? AND is_buy=1 AND received_at >= ? AND {ET} >= ? GROUP BY buyer ORDER BY s DESC LIMIT 1",
                           (mint, now - LIVE_LOOK_S - 60, now - LIVE_LOOK_S)).fetchone()
        net = float(r["buy_5m"]) - float(r["sell_5m"])
        buy15 = float(r["buy_15m"] or 0)
        share = (float(big["s"]) / buy15) if big is not None and buy15 > 0 else None
        mcap = _mcap(last["virtual_sol"], last["virtual_token"], params) if last is not None else None
        prog = _progress(last["virtual_sol"], last["virtual_token"], params) if last is not None else None
        created = float(c["t"]) if c is not None and c["t"] is not None else None
        name = (c["name"] if c is not None else None) or "naam onbekend"
        symbol = (c["symbol"] if c is not None else None) or None
        grad_t = graduated.get(mint)
        status = "graduated" if grad_t is not None else ("observed" if mint in observed else ("new" if active.get(mint) == "probation" else "watched"))
        result.append({
            "mint": mint, "mint_short": short_mint(mint), "name": name, "symbol": symbol, "title": name + (f" (${symbol})" if symbol else ""),
            "created_at": utc_iso(created), "age_s": None if created is None else round(now - created), "age_label": age_label(now - created) if created is not None else "onbekend",
            "buyers_5m": int(r["buyers_5m"] or 0), "net_sol_5m": round(net, 3), "net_sol_label": sol_label(net, 1), "buy_sol_5m": round(float(r["buy_5m"]), 3),
            "trades_5m": int(r["trades_5m"] or 0), "buys_5m": int(r["buys_5m"] or 0), "sells_5m": int(r["trades_5m"] or 0) - int(r["buys_5m"] or 0),
            "trades_15m": int(r["trades_15m"] or 0), "last_trade_age_label": age_label(now - float(r["last_t"])) if r["last_t"] is not None else "–",
            "largest_share_15m": None if share is None else round(share, 3), "largest_share_label": "onbekend" if share is None else f"{round(100 * share)}%",
            "mcap_sol": None if mcap is None else round(mcap, 2), "mcap_sol_label": sol_label(mcap, 1),
            "mcap_usd": None if mcap is None or not susd["known"] else round(mcap * susd["price"]), "mcap_usd_label": usd_short(round(mcap * susd["price"])) if mcap is not None and susd["known"] else "–",
            "progress": None if prog is None else round(prog, 3), "progress_label": "–" if prog is None else f"{round(100 * prog)}%",
            "status": status, "status_label": {"graduated": "gegradueerd", "observed": "observatie", "new": "nieuw", "watched": "gevolgd"}[status],
            "reason_label": REASON_NL.get(active.get(mint, ""), active.get(mint, "")), "observed_at": utc_iso(observed.get(mint)),
            "share_of_top": round(100 * net / max_net) if max_net > 0 and net > 0 else 0,
        })
    out["rows"] = result
    out["sol_usd"] = susd
    return out


def new_coins_view(conn: sqlite3.Connection | None, settings: Settings, now: float, limit: int = NEW_LIMIT) -> dict[str, Any]:
    """The newest coins the radar saw, with the tempo of the last ten minutes."""
    out: dict[str, Any] = {**data.stamp(now), "rows": [], "rate": {"last_10m": None, "per_min": None, "per_min_label": "–"}, "today": None,
                           "definition": "Elke pump.fun-munt zodra een van de twee bronnen hem meldt. Dev-buy = wat de maker zelf bij de start in de munt stak; startkapitalisatie = de marktkapitalisatie meteen daarna."}
    if conn is None:
        out["note"] = "Er zijn nog geen gegevens."
        return out
    hour_covered = _hour_covered(conn, now)
    c10 = _count_since(conn, "coins", now - 600)
    out["rate"] = {"last_10m": c10 if hour_covered else None, "per_min": round(c10 / 10.0, 1) if hour_covered else None, "per_min_label": fmt_n(c10 / 10.0, 1) if hour_covered else "–",
                   "hour_covered": hour_covered}
    out["today"] = _count_since(conn, "coins", day_start(now))
    active = _active_watchlist(conn)
    rows = _newest(conn, f"SELECT mint, name, symbol, creator, {ET} AS t, dev_buy_sol, initial_mcap_sol FROM coins ORDER BY received_at DESC LIMIT ?", int(limit))
    observed = _observed_mints(conn, [str(r["mint"]) for r in rows])
    result = []
    for r in rows:
        t = float(r["t"])
        prior = None
        if r["creator"]:
            prior = int(conn.execute(f"SELECT COUNT(*) FROM coins WHERE creator=? AND mint<>? AND {ET} > ? AND {ET} <= ?", (r["creator"], r["mint"], t - 86400, t)).fetchone()[0])
        result.append({
            "mint": str(r["mint"]), "mint_short": short_mint(str(r["mint"])), "name": r["name"] or "naam onbekend", "symbol": r["symbol"],
            "title": (r["name"] or "naam onbekend") + (f" (${r['symbol']})" if r["symbol"] else ""), "created_at": utc_iso(t), "created_label": fmt_time(t), "clock": clock_label(t),
            "age_s": round(now - t), "age_label": age_label(now - t), "dev_buy_sol": r["dev_buy_sol"], "dev_buy_label": sol_label(r["dev_buy_sol"], 2),
            "initial_mcap_sol": r["initial_mcap_sol"], "initial_mcap_label": sol_label(r["initial_mcap_sol"], 1),
            "creator_prior_24h": prior, "creator_prior_label": "onbekend" if prior is None else fmt_n(prior),
            "watched": str(r["mint"]) in active, "watched_label": REASON_NL.get(active.get(str(r["mint"]), ""), "niet meer gevolgd") if str(r["mint"]) in active else "niet (meer) gevolgd",
            "observed": str(r["mint"]) in observed,
        })
    out["rows"] = result
    return out


def themes_view(conn: sqlite3.Connection | None, settings: Settings, now: float, limit: int = THEME_LIMIT) -> dict[str, Any]:
    """The words that suddenly appear in many new coin names, with the money and the wallets behind them: the engine's
    own clustering and ranking, read from the snapshot the engine writes every tick (kv `engine.metas_latest`,
    milestone 10). Nothing is computed here; a snapshot older than `engine_themes.MAX_AGE_S` is not shown as current."""
    mcfg = settings.config("engine").get("meta") or {}
    window_s = float(mcfg.get("window_s", 10800))
    out: dict[str, Any] = {**data.stamp(now), "rows": [], "window_hours": round(window_s / 3600), "min_coins": int(mcfg.get("min_coins", 3)),
                           "state": "missing", "computed_at": None, "computed_label": "nog niet", "age_s": None, "age_label": "–", "bursting": 0,
                           "definition": (f"Een thema is een woord dat in de laatste {round(window_s / 3600)} uur in de namen van minstens {fmt_n(mcfg.get('min_coins', 3))} nieuwe munten "
                                          f"van verschillende makers opduikt. Opkomend = {fmt_n(mcfg.get('burst_ratio', 3), 1)}× zo vaak als de eigen basislijn van dat woord, of helemaal nieuw.")}
    if conn is None:
        out["note"] = "Er zijn nog geen gegevens."
        return out
    snap = engine_themes.read(conn)
    if snap is None:
        out["note"] = "Thema’s worden nu niet berekend: de engine heeft nog geen thema’s opgeslagen."
        return out
    as_of = float(snap["as_of"])
    age = max(0.0, now - as_of)
    out.update({"computed_at": utc_iso(as_of), "computed_label": fmt_time(as_of), "age_s": round(age), "age_label": age_label(age),
                "clusters_total": int(snap.get("clusters_total") or 0), "coins_in_window": int(snap.get("coins_in_window") or 0)})
    if age > engine_themes.MAX_AGE_S or as_of > now + ET_MARGIN_S:
        # never an old figure without saying so: no rows, the age in the note
        out["state"] = "stale"
        out["note"] = f"Thema’s worden nu niet berekend: de laatste berekening van de engine is {age_label(age)} oud."
        return out
    out["state"] = "fresh"
    metas = [m for m in snap["metas"] if isinstance(m, dict) and m.get("term")]
    if not metas:
        out["note"] = f"Geen woord komt in de laatste {round(window_s / 3600)} uur in {fmt_n(mcfg.get('min_coins', 3))} of meer nieuwe muntnamen voor."
        return out
    rows = []
    for m in metas[: int(limit)]:
        bursting = bool(m.get("bursting"))
        is_inf = bool(m.get("burst_ratio_is_inf"))
        ratio = _float(m.get("burst_ratio"))
        rows.append({"term": str(m["term"]), "n_coins": m.get("n_coins"), "distinct_creators": m.get("distinct_creators"), "graduations": m.get("graduations"),
                     "sol_inflow": m.get("sol_inflow"), "sol_inflow_label": sol_label(m.get("sol_inflow"), 1), "net_sol": m.get("net_sol"), "unique_buyers": m.get("unique_buyers"),
                     "mints_with_trades": m.get("mints_with_trades"), "burst_ratio": ratio, "burst_is_new": is_inf,
                     "burst_label": "nieuw woord" if is_inf else (f"{fmt_n(ratio, 1)}×" if ratio is not None else "–"),
                     "bursting": bursting, "status": "bursting" if bursting else "quiet", "status_label": "opkomend" if bursting else "rustig",
                     "observed_today": bool(m.get("observed_today")), "watched": bool(m.get("watched")),
                     "first_seen_label": fmt_time(_float(m.get("first_seen"))) if m.get("first_seen") is not None else "–",
                     "examples": [str(x) for x in (m.get("examples") or [])][:engine_themes.EXAMPLES]})
    out["rows"] = rows
    out["bursting"] = sum(1 for r in rows if r["bursting"])
    return out


def graduation_outcome(conn: sqlite3.Connection, mint: str, grad_t: float, now: float, h24: int, mv: str, max_gap_s: float) -> dict[str, Any]:
    """The 24 h outcome of a graduation with the tracker-coverage rule of the reports: a stored 'no winner' without dense
    samples is unknown, never 'geen winnaar'."""
    o = conn.execute("SELECT hit_100k, peak_mcap_usd FROM outcomes WHERE subject='graduation' AND mint=? AND horizon_s=? AND method_version=?"
                     " ORDER BY measured_at DESC LIMIT 1", (mint, int(h24), mv)).fetchone()
    if o is None:
        return {"status": "pending" if grad_t + h24 > now else "unknown", "peak_mcap_usd": None}
    hit = o["hit_100k"]
    if hit is None:
        return {"status": "unknown", "peak_mcap_usd": o["peak_mcap_usd"]}
    if hit:
        return {"status": "winner", "peak_mcap_usd": o["peak_mcap_usd"]}
    from ..measure.report import tracker_coverage

    cov = tracker_coverage(conn, mint, grad_t, grad_t + h24, max_gap_s)
    return {"status": "not_winner" if cov["covered"] else "unknown", "peak_mcap_usd": o["peak_mcap_usd"], "sparse": not cov["covered"]}


def graduations_view(conn: sqlite3.Connection | None, settings: Settings, now: float, limit: int = GRAD_LIMIT) -> dict[str, Any]:
    """The latest graduations: when, how old the coin was, the market cap and 24 h volume of the newest tracker sample,
    whether the radar saw the coin before, and the 24 h outcome once it exists."""
    out: dict[str, Any] = {**data.stamp(now), "rows": [], "last_hour": None, "today": None,
                           "definition": ("Een graduatie is het moment waarop een munt de bonding curve verlaat. Marktkapitalisatie en 24-uursvolume komen uit de "
                                          "prijssamples die de radar elke 5 minuten neemt (dus hoogstens 5 minuten oud); de uitkomst volgt na 24 uur.")}
    if conn is None:
        out["note"] = "Er zijn nog geen gegevens."
        return out
    cfg = settings.config("measure")
    mv = str(cfg["method_version"])
    h24 = max(int(h) for h in cfg["horizons_s"])
    max_gap = float((cfg.get("tracker") or {}).get("max_sample_gap_s", 600))
    hour_covered = _hour_covered(conn, now)
    g60 = _count_since(conn, "migrations", now - 3600)
    out["last_hour"] = g60 if hour_covered else None
    out["today"] = _count_since(conn, "migrations", day_start(now))
    rows = _newest(conn, f"SELECT m.mint, COALESCE(m.block_time, m.received_at) AS t, c.name, c.symbol, COALESCE(c.block_time, c.received_at) AS created"
                         f" FROM migrations m LEFT JOIN coins c ON c.mint = m.mint ORDER BY m.received_at DESC LIMIT ?", int(limit))
    observed = _observed_mints(conn, [str(r["mint"]) for r in rows])
    theme_obs = {}
    result = []
    for r in rows:
        mint = str(r["mint"])
        t = float(r["t"])
        s = conn.execute("SELECT ts, mcap_usd, volume_h24_usd, liquidity_usd FROM mcap_samples WHERE mint=? ORDER BY ts DESC LIMIT 1", (mint,)).fetchone()
        sample_age = None if s is None else max(0.0, now - float(s["ts"]))
        fresh_sample = s is not None and sample_age is not None and sample_age <= SAMPLE_MAX_AGE_S
        obs_t = observed.get(mint)
        lead = None if obs_t is None else t - obs_t
        oc = graduation_outcome(conn, mint, t, now, h24, mv, max_gap)
        created = None if r["created"] is None else float(r["created"])
        result.append({
            "mint": mint, "mint_short": short_mint(mint), "name": r["name"] or "naam onbekend", "symbol": r["symbol"],
            "title": (r["name"] or "naam onbekend") + (f" (${r['symbol']})" if r["symbol"] else ""), "graduated_at": utc_iso(t), "graduated_label": fmt_time(t),
            "clock": clock_label(t), "age_label": age_label(now - t), "age_at_graduation_s": None if created is None else round(t - created),
            "age_at_graduation_label": age_label(t - created) if created is not None else "onbekend",
            "mcap_usd": (None if not fresh_sample else s["mcap_usd"]), "mcap_label": usd_short(s["mcap_usd"]) if fresh_sample and s["mcap_usd"] is not None else "onbekend",
            "volume_24h_usd": (None if not fresh_sample else s["volume_h24_usd"]), "volume_label": usd_short(s["volume_h24_usd"]) if fresh_sample and s["volume_h24_usd"] is not None else "onbekend",
            "sample_age_s": None if sample_age is None else round(sample_age), "sample_age_label": "geen sample" if s is None else (age_label(sample_age) + (" (oud)" if not fresh_sample else "")),
            "observed_before": obs_t is not None and obs_t < t, "observed_label": ("ja, " + minutes(lead) + " vooraf") if obs_t is not None and lead is not None and lead > 0 else ("ja, erna" if obs_t is not None else "nee"),
            "lead_s": None if lead is None else round(lead), "outcome": oc["status"], "outcome_label": OUTCOME_NL.get(oc["status"], oc["status"]),
            "peak_label": usd_short(oc["peak_mcap_usd"]) if oc.get("peak_mcap_usd") is not None else "–",
        })
    out["rows"] = result
    return out


def social_view(conn: sqlite3.Connection | None, settings: Settings, now: float) -> dict[str, Any]:
    """What is going round on X right now, as far as the X layer runs: the keys with the most mentions in the last hour,
    the fast posts of the big accounts. Honest about its state: off, shadow (does not touch the observations) or live."""
    mode = settings.x_mode
    labels = {"off": "staat uit", "shadow": "schaduwstand", "live": "live"}
    out: dict[str, Any] = {**data.stamp(now), "mode": mode, "active": mode != "off", "mode_label": labels.get(mode, mode), "keys": [], "fast_posts": [],
                           "counts": {"keys_active": 0, "confirmed_today": 0, "posts_today": 0, "fast_today": 0},
                           "definition": ("De X-laag telt per gevolgde munt of thema hoe vaak hij op X genoemd wordt (per uur) en volgt de grote accounts op posts die "
                                          "snel likes verzamelen. In schaduwstand telt niets daarvan mee in de observaties; het staat hier om te zien wat er rondgaat.")}
    if mode == "off":
        out["note"] = "De X-laag staat uit: X is op dit moment niet gekoppeld. Zodra hij aanstaat zie je hier welke munten en woorden nu op X rondgaan."
        return out
    if conn is None:
        out["note"] = "Er zijn nog geen gegevens."
        return out
    t0 = day_start(now)
    if data.table_exists(conn, "x_keys"):
        rows = conn.execute("SELECT key, kind, decision_kind, mint, term, started_at, counts_n, last_n_1h, last_n_24h, peak_n_1h, confirmed_at, last_count_at FROM x_keys"
                            " WHERE stopped_at IS NULL ORDER BY COALESCE(last_n_1h, 0) DESC, COALESCE(peak_n_1h, 0) DESC, started_at DESC LIMIT 10").fetchall()
        keys = []
        for r in rows:
            title = r["term"] or r["key"]
            if r["mint"]:
                c = conn.execute("SELECT name, symbol FROM coins WHERE mint=?", (r["mint"],)).fetchone()
                if c is not None and (c["name"] or c["symbol"]):
                    title = (c["name"] or "") + (f" (${c['symbol']})" if c["symbol"] else "")
            keys.append({"key": r["key"], "kind": r["kind"], "kind_label": {"mint": "mint-adres", "ticker": "ticker", "name": "naam", "meta": "thema"}.get(str(r["kind"]), str(r["kind"])),
                         "title": title, "mint_short": short_mint(r["mint"]) if r["mint"] else None, "last_n_1h": r["last_n_1h"], "last_n_1h_label": fmt_n(r["last_n_1h"]) if r["last_n_1h"] is not None else "nog niet geteld",
                         "peak_n_1h": r["peak_n_1h"], "last_n_24h": r["last_n_24h"], "counts_n": r["counts_n"], "confirmed": r["confirmed_at"] is not None,
                         "confirmed_label": "ja" if r["confirmed_at"] is not None else "nee", "started_label": fmt_time(r["started_at"]),
                         "last_count_label": fmt_time(r["last_count_at"]) if r["last_count_at"] else "nog niet"})
        out["keys"] = keys
        out["counts"]["keys_active"] = int(conn.execute("SELECT COUNT(*) FROM x_keys WHERE stopped_at IS NULL").fetchone()[0])
        out["counts"]["confirmed_today"] = int(conn.execute("SELECT COUNT(*) FROM x_keys WHERE confirmed_at >= ?", (t0,)).fetchone()[0])
    if data.table_exists(conn, "social_posts"):
        out["counts"]["posts_today"] = int(conn.execute("SELECT COUNT(*) FROM social_posts WHERE received_at >= ?", (t0,)).fetchone()[0])
        out["counts"]["fast_today"] = int(conn.execute("SELECT COUNT(*) FROM social_posts WHERE received_at >= ? AND fast=1 AND COALESCE(fast_at, received_at) >= ?",
                                                       (t0 - FAST_LOOKBACK_S, t0)).fetchone()[0])
        posts = conn.execute("SELECT author, text, created_at, received_at, likes, quotes, likes_per_min, quotes_per_min, kind, fast_at FROM social_posts WHERE fast=1"
                             " ORDER BY COALESCE(fast_at, received_at) DESC LIMIT 5").fetchall()
        out["fast_posts"] = [{"author": r["author"], "text": (r["text"] or "")[:160], "created_label": fmt_time(r["created_at"] if r["created_at"] is not None else r["received_at"]),
                              "likes": r["likes"], "likes_label": fmt_n(r["likes"]) if r["likes"] is not None else "–", "likes_per_min": r["likes_per_min"],
                              "likes_per_min_label": fmt_n(r["likes_per_min"]) if r["likes_per_min"] is not None else "–", "quotes_per_min": r["quotes_per_min"], "kind": r["kind"]}
                             for r in posts]
    if not out["keys"] and not out["fast_posts"]:
        out["note"] = f"De X-laag draait ({out['mode_label']}) maar heeft op dit moment geen tellingen of snelle posts."
    return out


# =====================================================================================================================
# Milestone 12: the plays layer on the website (/plays, /api/public/plays) and the Terminal's plays panel.
# =====================================================================================================================
PLAYS_FRESH_S = 60
PLAY_STATUS_NL = {"signal": "signaal, wacht op vulling", "open": "open", "closed": "gesloten", "unknown": "onbekend (geen prijspad tot een uitstap)", "no_fill": "geen vulling"}
PLAY_EXIT_NL = {"target": "winstdoel", "trailing_stop": "trailing stop", "hard_stop": "harde stop", "time_stop": "tijdstop", "max_hold": "uiterste houdtijd",
                "graduation": "graduatie", "dev_sell": "dev-verkoop", "no_trade_after_entry": "geen trade na het signaal"}


def plays_view(conn: sqlite3.Connection | None, settings: Settings, now: float) -> dict[str, Any]:
    """`/api/public/plays`: the proven rules of config/plays.json with their measured figures, the state of the plays layer, per rule the
    paper positions (open, closed with the result after costs, unknown, no fill) and the recent history. Empty, and honest
    about it, while nothing is proven."""
    from ..plays import rules as plays_rules
    from ..plays.service import KV_HEARTBEAT, KV_RULES

    rules = plays_rules.load_rules(settings, include_inactive=True)
    pcfg = plays_rules.settings_of(settings)
    out: dict[str, Any] = {**data.stamp(now), "rules": [r.to_public() for r in rules], "active_rules": sum(1 for r in rules if r.active), "per_rule": [], "open": [], "history": [],
                           "size_sol": pcfg["size_sol"], "entry_delay_s": pcfg["entry_delay_s"], "alerts_per_hour": pcfg["alerts_per_hour"],
                           "definition": ("Een play is een munt die op dat moment aan een bewezen regel voldeed. Bewezen = de ondergrens van het 95%-interval van de verwachte waarde "
                                          "na kosten lag boven 0 op de toetshelft van een onderzoeksrapport, met minstens 100 gevallen, minder dan 20% onbekende uitkomsten en een "
                                          "mediane lead time van minstens 2 minuten. Elke play wordt als papierpositie gevolgd met dezelfde kosten als in het onderzoek; de uitkomst "
                                          "na kosten staat hier. Het platform plaatst nooit een order."),
                           "layer": {"state": "never", "label": "nog nooit getikt", "age_s": None}}
    if not rules:
        out["note"] = ("Nog geen bewezen regel: config/plays.json is leeg. Het onderzoeksharnas (fart research) toetst de vooraf vastgelegde hypotheses op onze eigen data; "
                       "pas een variant die op de toetshelft bewezen is, komt hier als regel. Tot die tijd staat alles klaar en staat hier niets.")
    out["research"] = research_state(settings)   # milestone 13: the last research run per hypothesis, under the rules
    if conn is None:
        out["note"] = out.get("note") or "Er zijn nog geen gegevens."
        return out
    kv = _kv(conn, (KV_HEARTBEAT, KV_RULES))
    hb = _float(kv.get(KV_HEARTBEAT))
    if hb is not None:
        age = max(0.0, now - hb)
        out["layer"] = {"state": "fresh" if age <= PLAYS_FRESH_S else "stale", "label": ("live" if age <= PLAYS_FRESH_S else f"oud ({age_label(age)} geen tik)"), "age_s": round(age),
                         "rules_seen": int(kv.get(KV_RULES) or 0)}
    if not data.table_exists(conn, "plays"):
        return out
    names = _coin_names(conn, [str(r[0]) for r in conn.execute("SELECT DISTINCT mint FROM plays")])
    rows = [play_row(dict(r), names, now) for r in conn.execute("SELECT * FROM plays ORDER BY as_of DESC LIMIT 500")]
    out["open"] = [r for r in rows if r["status"] in ("signal", "open")]
    out["history"] = [r for r in rows if r["status"] not in ("signal", "open")][:100]
    per = []
    for rule in rules:
        mine = [r for r in rows if r["rule"] == rule.id]
        closed = [r for r in mine if r["status"] == "closed" and r["pnl_sol"] is not None]
        pnl = [float(r["pnl_sol"]) for r in closed]
        per.append({**rule.to_public(), "signals": len(mine), "open": sum(1 for r in mine if r["status"] in ("signal", "open")), "closed": len(closed),
                    "unknown": sum(1 for r in mine if r["status"] == "unknown"), "no_fill": sum(1 for r in mine if r["status"] == "no_fill"),
                    "pnl_sum_sol": round(sum(pnl), 4) if pnl else None, "pnl_mean_sol": round(sum(pnl) / len(pnl), 4) if pnl else None,
                    "pnl_sum_label": sol_signed(sum(pnl)) if pnl else "–", "pnl_mean_label": sol_signed(sum(pnl) / len(pnl)) if pnl else "–",
                    "live_hit_rate": (sum(1 for p in pnl if p > 0) / len(pnl)) if pnl else None, "live_hit_label": pct(sum(1 for p in pnl if p > 0) / len(pnl)) if pnl else "–",
                    "measured_hit_label": pct(rule.figures.get("hit_rate")), "measured_ev_label": sol_signed(rule.figures.get("ev_sol")) if rule.figures.get("ev_sol") is not None else "–",
                    "ci_label": (f"{sol_signed(rule.figures.get('ci_low_sol'))} tot {sol_signed(rule.figures.get('ci_high_sol'))}"
                                 if rule.figures.get("ci_low_sol") is not None else "–"),
                    "rows": mine[:50]})
    out["per_rule"] = per
    out["counts"] = {"signals": len(rows), "open": len(out["open"]), "closed": sum(1 for r in rows if r["status"] == "closed"),
                     "unknown": sum(1 for r in rows if r["status"] == "unknown"), "no_fill": sum(1 for r in rows if r["status"] == "no_fill")}
    return out


def sol_signed(x: Any, decimals: int = 4) -> str:
    return "onbekend" if x is None else (f"{float(x):+.{decimals}f} SOL").replace(".", ",")


def research_state(settings: Settings) -> dict[str, Any]:
    """Milestone 13: per pre-registered hypothesis the newest report's state (the chosen variant, the verdict, the unknown share on
    the test half, the date), from out/research/latest.json; never the cases, never a path. Empty, and saying so, before the
    first run."""
    from ..research import hypotheses as hyps
    from ..research.report import load_latest

    latest = load_latest(settings)
    rows = []
    for hid in hyps.ORDER:
        h = hyps.get(hid)
        r = latest.get(hid)
        if r is None:
            rows.append({"hypothesis": hid, "title": h.title_nl, "round": 2 if hid in hyps.ROUND_TWO else 1, "state": "none", "verdict_label": "nog niet getoetst", "selected": None,
                         "selected_label": None, "unknown_share": None, "unknown_label": "–", "cases": None, "ev_label": "–", "date": None, "date_label": "–", "variants": len(h.variants)})
            continue
        ts = data.iso_to_ts(r.get("generated_at"))
        rows.append({"hypothesis": hid, "title": r.get("title") or h.title_nl, "round": 2 if hid in hyps.ROUND_TWO else 1,
                     "state": "proven" if r.get("proven") else ("rejected" if r.get("selected") else "none"), "verdict_label": r.get("verdict") or "onbekend",
                     "selected": r.get("selected"), "selected_label": r.get("selected_label"), "unknown_share": r.get("unknown_share"),
                     "unknown_label": pct(r.get("unknown_share")) if r.get("unknown_share") is not None else "–", "cases": r.get("cases"),
                     "ev_label": sol_signed(r.get("ev_sol")) if r.get("ev_sol") is not None else "–",
                     "ci_label": (f"{sol_signed(r.get('ci_low_sol'))} tot {sol_signed(r.get('ci_high_sol'))}" if r.get("ci_low_sol") is not None else "–"),
                     "date": r.get("generated_at"), "date_label": fmt_time(ts) if ts else "–", "days": len(r.get("days") or []), "variants": r.get("variants_tested"),
                     "unknown_after_follow": r.get("unknown_share_after_follow"),
                     "unknown_after_follow_label": pct(r.get("unknown_share_after_follow")) if r.get("unknown_share_after_follow") is not None else "–",
                     "window_note": r.get("window_note")})
    return {"rows": rows, "any": any(r["state"] != "none" for r in rows), "updated_label": max((r["date_label"] for r in rows if r["date"]), default="–"),
            "definition": ("Elke maandag 07:00 UTC toetst het harnas alle vooraf vastgelegde hypotheses over de laatste 30 dagen (zolang er minder dagen zijn: zoveel als er zijn). "
                           "Per hypothese staat hier de vooraf gekozen variant, het oordeel op de toetshelft, het aandeel onbekende uitkomsten en de datum. "
                           "Alleen een bewezen variant kan een regel worden; niets hier is advies.")}


def play_row(r: dict[str, Any], names: dict[str, tuple[str | None, str | None]], now: float) -> dict[str, Any]:
    try:
        e = json.loads(r.get("entry_json") or "{}")
    except ValueError:
        e = {}
    try:
        f = json.loads(r.get("features_json") or "{}")
    except ValueError:
        f = {}
    mint = str(r["mint"])
    nm = names.get(mint) or (f.get("name"), f.get("symbol"))
    name = nm[0] or "naam onbekend"
    symbol = nm[1]
    status = str(r["status"])
    pnl = r.get("pnl_sol")
    return {"id": int(r["id"]), "rule": str(r["rule"]), "version": int(r["version"]), "mint": mint, "mint_short": short_mint(mint), "name": name, "symbol": symbol,
            "title": name + (f" (${symbol})" if symbol else ""), "as_of": utc_iso(float(r["as_of"])), "as_of_label": fmt_time(float(r["as_of"])), "since_label": age_label(max(0.0, now - float(r["as_of"]))),
            "status": status, "status_label": PLAY_STATUS_NL.get(status, status), "size_sol": r.get("size_sol"),
            "signal": {"buyers_5m": f.get("unique_buyers_5m"), "net_sol_5m": f.get("net_sol_5m"), "share": f.get("largest_buyer_share_15m"), "dev_frac": f.get("dev_buy_frac_of_curve"),
                       "bundled": f.get("bundled_any")},
            "signal_label": (f"{fmt_n(f.get('unique_buyers_5m'))} kopers, {fmt_n(f.get('net_sol_5m'), 2)} SOL netto in 5 min" if f.get("unique_buyers_5m") is not None else "–"),
            "entry_curve_sol": e.get("curve_sol"), "entry_mcap_sol": e.get("mcap_sol"), "entry_label": (f"{fmt_n(e.get('curve_sol'), 1)} SOL in de curve · {fmt_n(e.get('mcap_sol'), 1)} SOL marktwaarde"
                                                                                                            if e.get("curve_sol") is not None else "–"),
            "fill_price": r.get("fill_price"), "exit_price": r.get("exit_price"), "exit_reason": r.get("exit_reason"),
            "exit_label": PLAY_EXIT_NL.get(str(r.get("exit_reason") or ""), r.get("exit_reason") or "–"), "exit_at_label": fmt_time(r["exit_time"]) if r.get("exit_time") else "–",
            "fees_sol": r.get("fees_sol"), "pnl_sol": pnl, "pnl_pct": r.get("pnl_pct"), "pnl_label": sol_signed(pnl) if pnl is not None else "–",
            "pnl_pct_label": (f"{100 * float(r['pnl_pct']):+.1f}%".replace(".", ",") if r.get("pnl_pct") is not None else "–"),
            "hold_label": age_label(float(r["exit_time"]) - float(r["entry_time"])) if r.get("exit_time") and r.get("entry_time") else "–",
            "_class": "is-winner" if pnl is not None and float(pnl) > 0 else ("is-stale" if pnl is not None and float(pnl) < 0 else "")}
