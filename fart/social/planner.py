"""The X confirmation planner (milestone 7): which keys are counted, in which order, how often, and when a key stops.

Keys come only from the engine's observations. A coin observation (`observe` decision of the last `confirm.window_s`
whose mint is on the active watchlist and has not graduated) gives one key: its mint, plus `$TICKER` when the ticker
is long enough and not generic, plus the exact name when it has two or more words (`confirmation_query`). A theme
observation (`observe_meta`) gives a key only when its term is at least `confirm.min_meta_term_len` characters and not
generic (`meta_term_query`), and only the `planner.max_meta_keys` strongest bursting themes (by the decision's own
`sol_inflow`, then `unique_buyers`) are candidates. The order is hard: the youngest coin observation first, then the
other coins by age, then the themes by strength. At most `planner.max_active_keys` keys are planned at once; a
candidate that ranks above the lowest active key displaces it (stop reason `displaced`), a lower one waits. A key is
planned at most once per `confirm.window_s` (one episode per key per window), whatever the number of decisions.

Per key the schedule is adaptive: a count as soon as the key is admitted, then every `fast_interval_s` during the
first `fast_phase_s` after the decision and every `slow_interval_s` after that, at most `max_counts_per_key` counts in
all (12 with the defaults: nine in the first two hours, three in the four hours after). A key leaves the plan as soon
as it is decided, and the row says why (`x_keys.stop_reason`):

* `confirmed`: a count reached `confirm.search_when_count_1h` in the last hour; exactly one search follows (at most
  `confirm.search_max_results` posts, a user lookup for at most `search_max_authors` authors) and the key stops;
* `refuted`: `refute_after_flat_counts` consecutive counts without growth of the 24 h count (the first count has no
  predecessor and never counts as flat): three flat counts after the first one, 45 minutes and $0.02 at the defaults;
* `expired`: the key is older than `confirm.window_s`, or no further count fits in the window;
* `graduated` / `dead`: the coin graduated or left the active watchlist;
* `displaced`: a higher-ranked candidate took its slot while the plan was full;
* `exhausted`: `max_counts_per_key` counts without a decision.

The planner spends nothing itself: the social daemon (`fart/social/x.py`) asks it what is due, pays through the budget
planner (`fart/social/xbudget.py`) and records what came back. Every count row stays in `shadow_confirmations` (shadow
mode) or `social_counts` (live); `x_keys` is the planner's own state and the book of what each key cost.
"""
from __future__ import annotations

import json
import re
import sqlite3
from typing import Any

from ..engine.meta import normalize, words
from ..util import day_start

COIN, META = 0, 1
STOP_REASONS = ("confirmed", "refuted", "expired", "graduated", "dead", "displaced", "exhausted")
REQUEST_SEARCH = "ca_search"
REQUEST_COUNT = "ca_counts"
_TICKER_RE = re.compile(r"[^A-Z0-9]")


# ----- keys and queries ---------------------------------------------------------------------------------------
def clean_ticker(symbol: str | None) -> str:
    return _TICKER_RE.sub("", str(symbol or "").upper().lstrip("$"))


def confirmation_query(mint: str, name: str | None, symbol: str | None, stopwords: set[str], ccfg: dict[str, Any]) -> str:
    """`"<mint>" OR $TICKER OR "<exact name>"` on unambiguous keys only: the ticker when it is long
    enough and not generic, the name when it has >= 2 words that are not all stopwords."""
    terms = [f'"{mint}"']
    ticker = clean_ticker(symbol)
    generic = {g.upper() for g in ccfg.get("generic_tickers", [])}
    if len(ticker) >= int(ccfg.get("min_ticker_len", 3)) and ticker not in generic and ticker.lower() not in stopwords and not ticker.isdigit():
        terms.append(f"${ticker}")
    nw = words(name)
    if len(nw) >= 2 and any(w not in stopwords for w in nw):
        clean_name = re.sub(r'["\\\n\r]+', " ", str(name)).strip()
        if clean_name:
            terms.append(f'"{clean_name}"')
    return " OR ".join(terms)


def meta_term_query(term: str, stopwords: set[str], ccfg: dict[str, Any]) -> str | None:
    """A theme term is searched only when it is specific: at least `min_meta_term_len` characters, not a stopword, not a
    generic ticker, not a number (milestone 7 drops the milestone-2 allowance for short bigrams)."""
    t = normalize(term).strip()
    if not t or t in stopwords or t.isdigit():
        return None
    generic = {g.lower() for g in ccfg.get("generic_tickers", [])}
    if t in generic:
        return None
    if len(t) >= int(ccfg.get("min_meta_term_len", 8)):
        return f'"{t}"'
    return None


def _theme_strength(features_json: str | None) -> tuple[float, int]:
    try:
        f = json.loads(features_json) if features_json else {}
    except json.JSONDecodeError:
        f = {}
    if not isinstance(f, dict):
        f = {}
    try:
        inflow = float(f.get("sol_inflow") or 0.0)
    except (TypeError, ValueError):
        inflow = 0.0
    try:
        buyers = int(f.get("unique_buyers") or 0)
    except (TypeError, ValueError):
        buyers = 0
    return inflow, buyers


def candidates(conn: sqlite3.Connection, now: float, cfg: dict[str, Any], stopwords: set[str]) -> list[dict[str, Any]]:
    """The ranked candidate keys right now: coin observations youngest first, then the strongest themes (at most
    `max_meta_keys`). Each entry carries its query, the decision it belongs to and its priority (`prio_class`,
    `prio_value`: lower sorts first). `fart social dryrun` counts them; `sync()` admits them."""
    ccfg = cfg["confirm"]
    pcfg = cfg.get("planner", {})
    window = float(ccfg.get("window_s", 21600))
    out: list[dict[str, Any]] = []
    for r in conn.execute(
        "SELECT d.id, d.mint, MAX(d.as_of) AS t, c.name, c.symbol FROM decisions d JOIN coins c ON c.mint = d.mint"
        " WHERE d.kind='observe' AND d.verdict='observe' AND d.as_of >= ? AND d.mint IN (SELECT mint FROM watchlist WHERE active=1)"
        " AND d.mint NOT IN (SELECT mint FROM migrations) GROUP BY d.mint ORDER BY t DESC, d.id DESC", (now - window,)):
        out.append({"key": r["mint"], "kind": "mint", "query": confirmation_query(r["mint"], r["name"], r["symbol"], stopwords, ccfg),
                    "decision_id": int(r["id"]), "decision_kind": "observe", "decision_as_of": float(r["t"]), "mint": r["mint"], "term": None,
                    "prio_class": COIN, "prio_value": -float(r["t"])})
    themes: list[tuple[tuple[float, int, float], dict[str, Any]]] = []
    for r in conn.execute(
        "SELECT id, term, MAX(as_of) AS t, features_json FROM decisions WHERE kind='observe_meta' AND verdict='observe' AND as_of >= ?"
        " GROUP BY term ORDER BY t DESC", (now - window,)):
        q = meta_term_query(r["term"], stopwords, ccfg)
        if not q:
            continue
        inflow, buyers = _theme_strength(r["features_json"])
        themes.append(((inflow, buyers, float(r["t"])), {
            "key": f"meta:{r['term']}", "kind": "meta", "query": q, "decision_id": int(r["id"]), "decision_kind": "observe_meta",
            "decision_as_of": float(r["t"]), "mint": None, "term": r["term"], "prio_class": META, "prio_value": -(inflow + buyers * 1e-6)}))
    themes.sort(key=lambda x: x[0], reverse=True)
    out.extend(c for _, c in themes[: max(0, int(pcfg.get("max_meta_keys", 5)))])
    for i, c in enumerate(out):
        c["rank"] = i
    return out


# ----- the plan -------------------------------------------------------------------------------------------------------
def _prio(row: Any) -> tuple[int, float]:
    return int(row["prio_class"]), float(row["prio_value"])


def active_rows(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM x_keys WHERE stopped_at IS NULL ORDER BY prio_class, prio_value, started_at").fetchall()


def _stop(conn: sqlite3.Connection, key: str, decision_id: int, now: float, reason: str) -> None:
    conn.execute("UPDATE x_keys SET stopped_at=?, stop_reason=?, next_due_at=NULL, search_pending=0 WHERE key=? AND decision_id=? AND stopped_at IS NULL",
                 (now, reason, key, int(decision_id)))


def _graduated(conn: sqlite3.Connection, mint: str) -> bool:
    return conn.execute("SELECT 1 FROM migrations WHERE mint=?", (mint,)).fetchone() is not None


def _watched(conn: sqlite3.Connection, mint: str) -> bool:
    return conn.execute("SELECT 1 FROM watchlist WHERE mint=? AND active=1", (mint,)).fetchone() is not None


def sync(conn: sqlite3.Connection, now: float, cfg: dict[str, Any], stopwords: set[str], rules_version: str) -> dict[str, Any]:
    """Stop what the world decided (window, graduation, death), then admit the candidates in order, displacing the
    lowest active key when the plan is full and the candidate ranks higher. One short write transaction (call inside
    `db.write_tx`). Returns the counters of this sync."""
    ccfg = cfg["confirm"]
    pcfg = cfg.get("planner", {})
    window = float(ccfg.get("window_s", 21600))
    max_active = max(0, int(pcfg.get("max_active_keys", 20)))
    stats: dict[str, Any] = {"admitted": 0, "stopped": {}, "waiting": 0}

    def stop(key: str, decision_id: int, reason: str) -> None:
        _stop(conn, key, decision_id, now, reason)
        stats["stopped"][reason] = stats["stopped"].get(reason, 0) + 1

    for r in active_rows(conn):
        reason = None
        if now - float(r["decision_as_of"]) >= window:
            reason = "confirmed" if r["confirmed_at"] is not None else "expired"
        elif r["kind"] == "mint" and r["mint"]:
            if _graduated(conn, r["mint"]):
                reason = "graduated"
            elif not _watched(conn, r["mint"]):
                reason = "dead"
        if reason:
            stop(r["key"], int(r["decision_id"]), reason)
    active = [dict(r) for r in active_rows(conn)]
    for c in candidates(conn, now, cfg, stopwords):
        episode = conn.execute("SELECT 1 FROM x_keys WHERE key=? AND decision_as_of > ?", (c["key"], c["decision_as_of"] - window)).fetchone()
        if episode is not None:
            continue  # already planned, or planned once in this window
        if len(active) >= max_active:
            victim = active[-1] if active else None
            if max_active <= 0 or victim is None or not (_prio(c) < _prio(victim)):
                stats["waiting"] += 1
                continue
            stop(victim["key"], int(victim["decision_id"]), "displaced")
            active.pop()
        conn.execute(
            "INSERT INTO x_keys(key, decision_id, kind, decision_kind, decision_as_of, mint, term, query, prio_class, prio_value, rank, started_at,"
            " next_due_at, rules_version) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (c["key"], c["decision_id"], c["kind"], c["decision_kind"], c["decision_as_of"], c["mint"], c["term"], c["query"], c["prio_class"],
             c["prio_value"], c.get("rank"), now, now, rules_version))
        active.append({**c, "started_at": now})
        active.sort(key=lambda a: (int(a["prio_class"]), float(a["prio_value"]), float(a["started_at"])))
        stats["admitted"] += 1
    stats["active"] = len(active)
    return stats


def _request(row: Any, what: str) -> dict[str, Any]:
    return {"request": what, "key": row["key"], "kind": row["kind"], "query": row["query"], "decision_id": int(row["decision_id"]),
            "decision_kind": row["decision_kind"], "decision_as_of": float(row["decision_as_of"]), "mint": row["mint"], "term": row["term"],
            "counts_n": int(row["counts_n"]), "rank": row["rank"]}


def due_requests(conn: sqlite3.Connection, now: float) -> list[dict[str, Any]]:
    """What the plan wants right now, in admission order: the pending search of every confirmed key first (the most
    information per dollar: it completes a confirmation), then the due counts, youngest coin first, themes last."""
    rows = active_rows(conn)
    searches = [_request(r, REQUEST_SEARCH) for r in rows if r["search_pending"]]
    counts = [_request(r, REQUEST_COUNT) for r in rows
              if not r["search_pending"] and r["confirmed_at"] is None and r["next_due_at"] is not None and float(r["next_due_at"]) <= now]
    return searches + counts


def schedule_counts(pcfg: dict[str, Any], window_s: float) -> list[float]:
    """The offsets (seconds after the decision) of the counts a key gets when nothing decides it earlier: every
    `fast_interval_s` during `fast_phase_s`, then every `slow_interval_s`, inside the window, at most
    `max_counts_per_key`. Pure; the dry run and the tests derive the per-key maximum from it."""
    fast_phase = float(pcfg.get("fast_phase_s", 7200))
    fast, slow = float(pcfg.get("fast_interval_s", 900)), float(pcfg.get("slow_interval_s", 3600))
    max_counts = int(pcfg.get("max_counts_per_key", 12))
    out = [0.0]
    while len(out) < max_counts:
        t = out[-1] + (fast if out[-1] < fast_phase else slow)
        if t >= float(window_s):
            break
        out.append(t)
    return out


def next_due(decision_as_of: float, count_at: float, pcfg: dict[str, Any], window_s: float) -> float | None:
    """The next count after one taken at `count_at`; None when no further count fits in the key's window."""
    elapsed = count_at - decision_as_of
    step = float(pcfg.get("fast_interval_s", 900)) if elapsed < float(pcfg.get("fast_phase_s", 7200)) else float(pcfg.get("slow_interval_s", 3600))
    nxt = count_at + step
    return nxt if nxt < decision_as_of + float(window_s) else None


def record_count(conn: sqlite3.Connection, req: dict[str, Any], now: float, n_last_hour: int | None, n_24h: int | None, threshold: int,
                 usd: float, cfg: dict[str, Any]) -> str:
    """Book one count against its key and decide the key: 'confirmed' (a search is now pending), 'refuted',
    'exhausted', 'expired' (no further count fits) or 'counted' (the next count is scheduled)."""
    ccfg = cfg["confirm"]
    pcfg = cfg.get("planner", {})
    window = float(ccfg.get("window_s", 21600))
    row = conn.execute("SELECT * FROM x_keys WHERE key=? AND decision_id=?", (req["key"], int(req["decision_id"]))).fetchone()
    if row is None:
        return "unknown"
    counts_n = int(row["counts_n"]) + 1
    flat = int(row["flat_streak"])
    if int(row["counts_n"]) > 0:
        cur = n_24h if n_24h is not None else n_last_hour
        prev = row["last_n_24h"] if row["last_n_24h"] is not None else row["last_n_1h"]
        if cur is not None and prev is not None:
            flat = 0 if int(cur) > int(prev) else flat + 1
    peak = row["peak_n_1h"]
    if n_last_hour is not None and (peak is None or int(n_last_hour) > int(peak)):
        peak = int(n_last_hour)
    confirmed_at = row["confirmed_at"]
    search_pending = 0
    due = None
    stop_reason = None
    if n_last_hour is not None and int(n_last_hour) >= int(threshold) and confirmed_at is None:
        confirmed_at = now
        outcome = "confirmed"
        if int(pcfg.get("max_searches_per_key", 1)) > int(row["searches_n"]):
            search_pending = 1
        else:
            stop_reason = "confirmed"
    elif flat >= int(pcfg.get("refute_after_flat_counts", 3)):
        outcome = stop_reason = "refuted"
    elif counts_n >= int(pcfg.get("max_counts_per_key", 12)):
        outcome = stop_reason = "exhausted"
    else:
        due = next_due(float(row["decision_as_of"]), now, pcfg, window)
        if due is None:
            outcome = stop_reason = "expired"
        else:
            outcome = "counted"
    # a count without information (an unparseable body) is paid and counted but keeps the previous baseline for the growth test
    last_1h = n_last_hour if n_last_hour is not None else row["last_n_1h"]
    last_24h = n_24h if n_24h is not None else row["last_n_24h"]
    conn.execute(
        "UPDATE x_keys SET counts_n=?, last_count_at=?, last_n_1h=?, last_n_24h=?, peak_n_1h=?, flat_streak=?, confirmed_at=?, search_pending=?,"
        " next_due_at=?, usd=usd+?, stopped_at=COALESCE(stopped_at, ?), stop_reason=COALESCE(stop_reason, ?) WHERE key=? AND decision_id=?",
        (counts_n, now, last_1h, last_24h, peak, flat, confirmed_at, search_pending, due, float(usd), now if stop_reason else None, stop_reason,
         req["key"], int(req["decision_id"])))
    return outcome


def record_search(conn: sqlite3.Connection, req: dict[str, Any], now: float, authors_n: int, usd: float) -> None:
    """The one search of a confirmed key (posts and author lookups together) and the end of the key."""
    conn.execute(
        "UPDATE x_keys SET searches_n=searches_n+1, authors_n=authors_n+?, usd=usd+?, usd_search=usd_search+?, search_pending=0,"
        " stopped_at=COALESCE(stopped_at, ?), stop_reason=COALESCE(stop_reason, 'confirmed') WHERE key=? AND decision_id=?",
        (int(authors_n), float(usd), float(usd), now, req["key"], int(req["decision_id"])))


# ----- views ----------------------------------------------------------------------------------------------------------
def stopped_between(conn: sqlite3.Connection, start: float, end: float) -> dict[str, dict[str, Any]]:
    """Keys that stopped in [start, end) per stop reason: how many, what they cost, how many counts they took."""
    out: dict[str, dict[str, Any]] = {}
    for r in conn.execute(
        "SELECT stop_reason, COUNT(*) AS n, COALESCE(SUM(usd), 0) AS usd, COALESCE(SUM(counts_n), 0) AS counts, COALESCE(SUM(searches_n), 0) AS searches"
        " FROM x_keys WHERE stopped_at >= ? AND stopped_at < ? GROUP BY stop_reason", (start, end)):
        out[str(r["stop_reason"])] = {"n": int(r["n"]), "usd": float(r["usd"]), "counts": int(r["counts"]), "searches": int(r["searches"])}
    return out


def observed_rates(conn: sqlite3.Connection, now: float, days: int = 7) -> dict[str, Any]:
    """What the planner saw over the last `days` days, for the dry run: keys admitted per day (over the days that had
    any), the share of stopped keys that were confirmed, the counts a confirmed key needed and a key took on average.
    Empty when no key was ever planned."""
    since = now - days * 86400
    row = conn.execute(
        "SELECT COUNT(*) AS n, COUNT(DISTINCT CAST(started_at / 86400 AS INTEGER)) AS days FROM x_keys WHERE started_at >= ?", (since,)).fetchone()
    out: dict[str, Any] = {}
    if int(row["n"]) > 0 and int(row["days"]) > 0:
        out["keys_per_day"] = {"keys": int(row["n"]), "days": int(row["days"]), "per_day": int(row["n"]) / int(row["days"])}
    st = conn.execute(
        "SELECT COUNT(*) AS n, SUM(stop_reason='confirmed') AS confirmed, AVG(counts_n) AS counts, SUM(stop_reason='refuted') AS refuted,"
        " AVG(CASE WHEN stop_reason='confirmed' THEN counts_n END) AS counts_confirmed FROM x_keys WHERE stopped_at >= ? AND stop_reason IS NOT NULL", (since,)).fetchone()
    if st is not None and int(st["n"] or 0) > 0:
        out["stopped"] = {"n": int(st["n"]), "confirmed": int(st["confirmed"] or 0), "refuted": int(st["refuted"] or 0),
                          "confirmed_share": int(st["confirmed"] or 0) / int(st["n"]), "counts_per_key": float(st["counts"] or 0.0),
                          "counts_per_confirmed_key": None if st["counts_confirmed"] is None else float(st["counts_confirmed"])}
    return out


def status_counters(conn: sqlite3.Connection, now: float) -> dict[str, Any]:
    """The planner line of `fart status`: active keys by kind, searches pending, what stopped today and why."""
    t0 = day_start(now)
    active = {str(r[0]): int(r[1]) for r in conn.execute("SELECT kind, COUNT(*) FROM x_keys WHERE stopped_at IS NULL GROUP BY kind")}
    pending = int(conn.execute("SELECT COUNT(*) FROM x_keys WHERE stopped_at IS NULL AND search_pending=1").fetchone()[0])
    due = int(conn.execute("SELECT COUNT(*) FROM x_keys WHERE stopped_at IS NULL AND confirmed_at IS NULL AND next_due_at IS NOT NULL AND next_due_at <= ?", (now,)).fetchone()[0])
    stopped = stopped_between(conn, t0, t0 + 86400.0)
    started = int(conn.execute("SELECT COUNT(*) FROM x_keys WHERE started_at >= ?", (t0,)).fetchone()[0])
    return {"active": active, "active_total": sum(active.values()), "search_pending": pending, "due_now": due, "started_today": started,
            "stopped_today": stopped, "usd_today_on_keys": sum(v["usd"] for v in stopped.values())}
