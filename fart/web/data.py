"""The data behind the website (milestone 8): read-only views of the database and of `out/recall/`.

Every function here takes a read-only connection (`open_db`: `mode=ro` plus `PRAGMA query_only`) or only the settings,
and returns plain dicts: raw values for machines next to Dutch display strings (`*_label`) for the pages. The honesty
rules of the reports apply to every number: an hour that was not covered shows no counts (NIET GEDEKT stays empty, never
zero), a day that was not covered shows no universe and no percentages, an outcome that is not known stays "onbekend",
a decision whose 24 h have not passed is "nog bezig". Nothing here gives advice and nothing here writes.
"""
from __future__ import annotations

import datetime as _dt
import json
import re
import sqlite3
from pathlib import Path
from typing import Any

from .. import db
from ..config import Settings
from ..ingest import coverage as cov
from ..notify.format import age as age_label, fmt_day, fmt_time, n as fmt_n
from ..util import day_key, day_start, utc_iso
from ..version import rules_fingerprint, rules_version

H24 = 86400
DECISIONS_LIMIT = 200
REPORT_NAME = re.compile(r"^(week-)?\d{4}-\d{2}-\d{2}\.(md|json)$")
STATUS_NL = {"complete": "COMPLEET", "incomplete": "NIET COMPLEET", "not_covered": "NIET GEDEKT"}
OUTCOME_NL = {"winner": "winnaar", "not_winner": "geen winnaar", "unknown": "onbekend", "pending": "nog bezig",
              "none": "geen eigen uitkomst (thema)"}
X_MODE_NL = {"off": "uit", "shadow": "schaduw (niets bereikt de beslissingen)", "live": "live"}
STOP_NL = {"confirmed": "bevestigd", "refuted": "weerlegd", "expired": "verlopen", "graduated": "gegradueerd", "dead": "dood",
           "displaced": "verdrongen", "exhausted": "uitgeput"}

# the daemons and timers the dashboard watches: (key, label, kv key, the age above which the row says "oud")
FRESHNESS = (
    ("ingest", "fart-ingest (heartbeat)", "ingest.heartbeat", 180),
    ("pumpportal", "PumpPortal-stroom: laatste bericht", "ingest.pumpportal.last_msg_at", 900),
    ("rpc_logs", "RPC-logstroom: laatste bericht", "ingest.rpc_logs.last_msg_at", 900),
    ("rpc_trades", "RPC-tradestroom: laatste bericht", "ingest.rpc_trades.last_msg_at", 900),
    ("engine", "fart-engine (heartbeat)", "engine.heartbeat", 180),
    ("social", "fart-social (heartbeat)", "social.heartbeat", 180),
    ("notify", "fart-notify (heartbeat)", "notify.heartbeat", 180),
    ("track", "fart-measure-track (timer, elke 5 min)", "measure.track.last_run", 900),
    ("report", "fart-measure-report (dagelijks 06:00 UTC)", "measure.report.last_run", 26 * 3600),
    ("week", "fart-measure-week (dagelijks 06:30 UTC)", "measure.week.last_run", 26 * 3600),
)


# ----- the read-only connection ------------------------------------------------------------------------------------
def open_db(path: Path | str) -> sqlite3.Connection:
    """A read-only connection: `mode=ro` plus `PRAGMA query_only=1`; a write raises `sqlite3.OperationalError`.

    The daemons keep the database in WAL mode. A read-only connection can read a WAL database as long as its `-wal` and
    `-shm` files exist or can be created; under the unit's `ReadOnlyPaths=/opt/fart/data` they cannot be created, so
    when the daemons are all stopped (a clean close removes both files) the normal open fails at the first query with
    "unable to open database file". The fallback then opens the file with `immutable=1`, which reads the main file
    without locks: correct while no writer runs (the files are absent exactly then) and harmless otherwise, because
    every connection here lives for one request. Raises FileNotFoundError when there is no database."""
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(str(p))
    try:
        conn = db.connect_ro(p)
        try:
            conn.execute("PRAGMA query_only=1")
            conn.execute("SELECT name FROM sqlite_master LIMIT 1").fetchall()
            return conn
        except sqlite3.OperationalError:
            conn.close()
            raise
    except sqlite3.OperationalError as e:
        msg = str(e).lower()
        if "unable to open" not in msg and "readonly" not in msg:
            raise
    conn = sqlite3.connect(f"file:{p.as_posix()}?mode=ro&immutable=1", uri=True, timeout=5.0, isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=1")
    return conn


def table_exists(conn: sqlite3.Connection, name: str) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone() is not None


# ----- small formatters ----------------------------------------------------------------------------------------------
def stamp(now: float) -> dict[str, Any]:
    return {"generated_at": utc_iso(now), "generated_label": fmt_time(now)}


def pct(x: Any, blank: bool = False) -> str:
    if blank:
        return "–"
    return "onbekend" if x is None else f"{100 * float(x):.0f}%"


def usd(x: Any) -> str:
    return "onbekend" if x is None else f"${float(x):.2f}"


def minutes(seconds: Any) -> str:
    return "onbekend" if seconds is None else f"{float(seconds) / 60:.0f} min"


def ts_label(ts: Any) -> str:
    try:
        return fmt_time(float(ts)) if ts is not None else "nooit"
    except (TypeError, ValueError):
        return "onbekend"


def iso_to_ts(value: Any) -> float | None:
    if not value:
        return None
    try:
        s = str(value)
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        d = _dt.datetime.fromisoformat(s)
        if d.tzinfo is None:
            d = d.replace(tzinfo=_dt.timezone.utc)
        return d.timestamp()
    except ValueError:
        return None


def _kv(conn: sqlite3.Connection, prefixes: tuple[str, ...]) -> dict[str, str]:
    where = " OR ".join("key LIKE ?" for _ in prefixes)
    return {str(r["key"]): r["value"] for r in conn.execute(f"SELECT key, value FROM kv WHERE {where}", tuple(f"{p}%" for p in prefixes))}


def _float(v: Any) -> float | None:
    try:
        return None if v is None else float(v)
    except (TypeError, ValueError):
        return None


# ----- /api/status ---------------------------------------------------------------------------------------------------
def status_view(conn: sqlite3.Connection | None, settings: Settings, now: float) -> dict[str, Any]:
    out: dict[str, Any] = {**stamp(now), "rules_version": rules_version(), "rules_fingerprint": rules_fingerprint(),
                           "rules_fingerprint_short": rules_fingerprint()[:19], "db_exists": conn is not None, "banner": settings.config("notify").get("footer")}
    if conn is None:
        out.update({"note": "Er is nog geen database (ingest heeft nog nooit gedraaid).", "schema_version": None, "daemons": [], "counts": {},
                    "open_gaps": [], "failures": {}, "decisions_today": {"observe": None, "observe_meta": None, "total": None, "last": "nooit"},
                    "budgets": {}, "x": {}})
        return out
    kv = _kv(conn, ("ingest.", "engine.", "measure.", "social.", "notify.", "backup.", "rpc_trades.", "x."))
    out["schema_version"] = db.schema_version(conn)
    rows = []
    for key, label, kv_key, expect in FRESHNESS:
        ts = _float(kv.get(kv_key))
        rows.append(_fresh_row(key, label, ts, expect, now))
    bv = kv.get("backup.verify.last")
    bts = None
    bstatus = ""
    if bv:
        try:
            d = json.loads(bv)
            bts = iso_to_ts(d.get("checked_at"))
            bstatus = str(d.get("status") or "")
        except (ValueError, AttributeError):
            bts = None
    row = _fresh_row("backup", "fart-backup verify (dagelijks 05:30 UTC)", bts, 26 * 3600, now)
    if bstatus:
        row["status_label"] += f" · laatste controle {bstatus}"
    rows.append(row)
    out["daemons"] = rows
    out["counts"] = db.table_counts(conn, ("coins", "migrations", "trades", "watchlist", "decisions", "outcomes", "coverage_gaps", "outbox"))
    out["watchlist_active"] = int(conn.execute("SELECT COUNT(*) FROM watchlist WHERE active=1").fetchone()[0])
    out["trades_by_source_today"] = {str(r[0]): int(r[1]) for r in conn.execute(
        "SELECT source, COUNT(*) FROM trades WHERE received_at >= ? GROUP BY source", (day_start(now),))}
    out["open_gaps"] = [{"source": r["source"], "since": utc_iso(r["start"]), "since_label": ts_label(r["start"]), "reason": r["reason"]}
                        for r in conn.execute("SELECT source, start, reason FROM coverage_gaps WHERE end IS NULL ORDER BY start")]
    out["open_gaps_label"] = "; ".join(f"{g['source']} sinds {g['since_label']} ({g['reason']})" for g in out["open_gaps"]) or "geen"
    fails = {}
    for k, v in kv.items():
        if k.startswith(("engine.tick_failed", "ingest.handle_failed.", "ingest.rejected.", "rpc_trades.subscription_capped", "rpc_trades.envelope_paused")):
            if re.search(r"\.\d{4}-\d{2}-\d{2}$", k) or k.endswith((".last", ".last_at", "_at")):
                continue
            fails[k] = v
    out["failures"] = dict(sorted(fails.items()))
    out["failures_label"] = ", ".join(f"{k} {v}" for k, v in out["failures"].items()) or "geen"
    t0 = day_start(now)
    dec = {str(r[0]): int(r[1]) for r in conn.execute(
        "SELECT kind, COUNT(*) FROM decisions WHERE verdict='observe' AND as_of >= ? GROUP BY kind", (t0,))}
    last_dec = conn.execute("SELECT MAX(as_of) FROM decisions").fetchone()[0]
    out["decisions_today"] = {"observe": dec.get("observe", 0), "observe_meta": dec.get("observe_meta", 0),
                              "total": dec.get("observe", 0) + dec.get("observe_meta", 0), "day": day_key(now),
                              "last_at": utc_iso(last_dec) if last_dec else None, "last": ts_label(last_dec)}
    # budgets: estimated CU today against the cap, X USD today against the cap
    rpc_source = "rpc_public" if settings.rpc_is_public else "rpc"
    cu = float(conn.execute("SELECT COALESCE(SUM(cost), 0) FROM api_ledger WHERE source=? AND blocked_reason IS NULL AND ts >= ?",
                            (rpc_source, t0)).fetchone()[0])
    cu_blocked = int(conn.execute("SELECT COUNT(*) FROM api_ledger WHERE source=? AND blocked_reason IS NOT NULL AND ts >= ?", (rpc_source, t0)).fetchone()[0])
    cap = None if settings.rpc_is_public else float(settings.rpc_daily_budget_cu)
    x_usd = float(conn.execute("SELECT COALESCE(SUM(usd), 0) FROM x_spend WHERE day=?", (day_key(now),)).fetchone()[0])
    x_blocked = int(conn.execute("SELECT COUNT(*) FROM api_ledger WHERE source='x' AND blocked_reason IS NOT NULL AND ts >= ?", (t0,)).fetchone()[0])
    x_cap = float(settings.x_daily_budget_usd)
    out["budgets"] = {
        "rpc": {"source": rpc_source, "cu_today": round(cu), "cu_today_label": fmt_n(round(cu)), "cap": cap,
                "cap_label": fmt_n(cap) if cap is not None else "geen plafond (publieke RPC)", "share": (cu / cap) if cap else None,
                "share_label": pct(cu / cap) if cap else "–", "blocked_today": cu_blocked,
                "note": "schatting uit de kostentabel (config/budget.json); de ijking met het Alchemy-dashboard staat open"},
        "x": {"usd_today": round(x_usd, 4), "usd_today_label": usd(x_usd), "cap": x_cap, "cap_label": usd(x_cap), "share": (x_usd / x_cap) if x_cap else None,
              "share_label": pct(x_usd / x_cap) if x_cap else "–", "blocked_today": x_blocked, "mode": settings.x_mode,
              "mode_label": X_MODE_NL.get(settings.x_mode, settings.x_mode)},
    }
    out["x"] = {"mode": settings.x_mode, "mode_label": X_MODE_NL.get(settings.x_mode, settings.x_mode), "requested": settings.x_mode_requested or "unset",
                "note": settings.x_mode_note, "social_daemon": kv.get("social.x_mode"), "engine_daemon": kv.get("engine.x_mode"),
                "token": "configured" if settings.x_configured else "not configured"}
    return out


def _fresh_row(key: str, label: str, ts: float | None, expect_s: float, now: float) -> dict[str, Any]:
    if ts is None:
        return {"key": key, "label": label, "last_at": None, "last": "nooit", "age_s": None, "age": "–", "status": "nooit",
                "status_label": "nooit gezien", "expect_s": expect_s, "_class": "never"}
    age_s = max(0.0, now - ts)
    fresh = age_s <= expect_s
    return {"key": key, "label": label, "last_at": utc_iso(ts), "last": fmt_time(ts), "age_s": round(age_s), "age": age_label(age_s),
            "status": "fresh" if fresh else "stale", "status_label": "vers" if fresh else f"oud (ouder dan {age_label(expect_s)})",
            "expect_s": expect_s, "_class": "" if fresh else "stale"}


# ----- /api/coverage -------------------------------------------------------------------------------------------------
def coverage_view(conn: sqlite3.Connection | None, now: float, hours: int = 24) -> dict[str, Any]:
    out: dict[str, Any] = {**stamp(now), "hours": int(hours), "rows": [], "summary": {}}
    if conn is None:
        out["note"] = "Er is nog geen database."
        return out
    rows = []
    covered = complete = 0
    creates = grads = 0
    for h in cov.report(conn, hours, now):
        start, _ = _hour_bounds(h["hour"])
        gaps = [{"source": g["source"], "reason": g["reason"], "seconds": g["seconds"], "recovered": bool(g.get("recovered"))} for g in h["gaps"]]
        gap_label = "; ".join(f"{g['source']} {g['seconds']:.0f} s ({g['reason'] or '?'}{', hersteld' if g['recovered'] else ''})" for g in gaps) or "geen"
        is_cov = bool(h["covered"])
        row = {"hour": h["hour"], "hour_label": fmt_time(start), "covered": is_cov, "complete": bool(h["complete"]),
               "covered_label": "ja" if is_cov else "NIET GEDEKT", "complete_label": ("ja" if h["complete"] else "nee") if is_cov else "–",
               "gaps": gaps, "gaps_label": gap_label, "_class": "" if is_cov else "notcovered"}
        for k in ("creates_pp", "creates_rpc", "creates_union", "grads", "grads_without_create"):
            row[k] = int(h[k]) if is_cov else None   # NIET GEDEKT stays empty: we were not there to count
        row["gap_s"] = round(sum(float(g["seconds"]) for g in gaps if not g["recovered"]))
        if is_cov:
            covered += 1
            creates += int(h["creates_union"])
            grads += int(h["grads"])
            complete += 1 if h["complete"] else 0
        rows.append(row)
    out["rows"] = rows
    out["summary"] = {"hours_covered": covered, "hours_complete": complete, "hours_not_covered": len(rows) - covered,
                      "creates_union_covered": creates, "grads_covered": grads}
    return out


def _hour_bounds(hour: str) -> tuple[float, float]:
    from ..util import hour_bounds

    return hour_bounds(hour)


# ----- /api/decisions ------------------------------------------------------------------------------------------------
def decisions_view(conn: sqlite3.Connection | None, settings: Settings, now: float, limit: int = DECISIONS_LIMIT) -> dict[str, Any]:
    out: dict[str, Any] = {**stamp(now), "limit": int(limit), "rows": []}
    if conn is None:
        out["note"] = "Er is nog geen database."
        return out
    cfg = settings.config("measure")
    mv = str(cfg["method_version"])
    h24 = max(int(h) for h in cfg["horizons_s"])
    rows = []
    for d in conn.execute("SELECT * FROM decisions ORDER BY as_of DESC, id DESC LIMIT ?", (int(limit),)):
        try:
            feats = json.loads(d["features_json"]) if d["features_json"] else {}
        except json.JSONDecodeError:
            feats = {}
        try:
            reasons = json.loads(d["reasons_json"]) if d["reasons_json"] else []
        except json.JSONDecodeError:
            reasons = []
        kind = str(d["kind"])
        name = None
        if d["mint"]:
            c = conn.execute("SELECT name, symbol FROM coins WHERE mint=?", (d["mint"],)).fetchone()
            if c is not None:
                name = f"{c['name'] or ''}" + (f" (${c['symbol']})" if c["symbol"] else "")
        outcome = decision_outcome(conn, d, now, h24, mv) if kind == "observe" and d["mint"] else {"status": "none"}
        rows.append({
            "id": int(d["id"]), "as_of": utc_iso(d["as_of"]), "as_of_label": fmt_time(d["as_of"]), "kind": kind,
            "kind_label": {"observe": "munt", "observe_meta": "thema"}.get(kind, kind), "mint": d["mint"], "term": d["term"],
            "subject": d["mint"] or d["term"] or "", "name": name, "verdict": d["verdict"], "rules_version": d["rules_version"],
            "reasons": reasons, "reasons_label": "; ".join(str(r) for r in reasons) or "–",
            "features": feats if isinstance(feats, dict) else {}, "features_label": features_label(feats),
            "outcome": outcome["status"], "outcome_label": OUTCOME_NL.get(outcome["status"], outcome["status"]),
            "peak_mcap_usd": outcome.get("peak_mcap_usd"), "peak_label": fmt_n(outcome["peak_mcap_usd"]) if outcome.get("peak_mcap_usd") is not None else "–",
            "_class": {"winner": "winner", "not_winner": "", "unknown": "unknown", "pending": "pending", "none": ""}.get(outcome["status"], ""),
        })
    out["rows"] = rows
    out["definition"] = ("uitkomst van een munt-observatie = winnaar als de munt binnen 24 u na de beslissing de winnaarsdefinitie haalde "
                         f"(piek ≥ ${cfg['winner']['peak_mcap_usd']:,} én volume ≥ ${cfg['winner']['min_volume_usd']:,}), geen winnaar als dat gemeten niet zo was, "
                         "onbekend als de meting ontbreekt of niets kon vaststellen, nog bezig zolang de 24 u niet om zijn; een thema-observatie heeft geen eigen uitkomst")
    return out


def decision_outcome(conn: sqlite3.Connection, d: sqlite3.Row, now: float, h24: int, method_version: str) -> dict[str, Any]:
    o = conn.execute(
        "SELECT hit_100k, peak_mcap_usd FROM outcomes WHERE subject='decision' AND decision_id=? AND horizon_s=? AND method_version=?"
        " ORDER BY measured_at DESC LIMIT 1", (int(d["id"]), int(h24), method_version)).fetchone()
    if o is None:
        return {"status": "pending" if float(d["as_of"]) + h24 > now else "unknown"}
    hit = o["hit_100k"]
    status = "unknown" if hit is None else ("winner" if hit else "not_winner")
    return {"status": status, "peak_mcap_usd": o["peak_mcap_usd"]}


def features_label(feats: Any) -> str:
    """`key=value` pairs of the scalar features, sorted; a nested value shows as its size."""
    if not isinstance(feats, dict) or not feats:
        return "–"
    parts = []
    for k in sorted(feats):
        v = feats[k]
        if isinstance(v, bool):
            s = "ja" if v else "nee"
        elif isinstance(v, (int, float)):
            s = f"{v:.4g}" if isinstance(v, float) else str(v)
        elif v is None:
            s = "–"
        elif isinstance(v, (list, dict)):
            s = f"[{len(v)}]"
        else:
            s = str(v)[:40]
        parts.append(f"{k}={s}")
    return " · ".join(parts)


# ----- /api/x --------------------------------------------------------------------------------------------------------
def x_view(conn: sqlite3.Connection | None, settings: Settings, now: float) -> dict[str, Any]:
    from ..social import planner as xplanner
    from ..social.shadow import status_counters as shadow_counters

    out: dict[str, Any] = {**stamp(now)}
    kv = _kv(conn, ("social.", "engine.x_mode", "x.")) if conn is not None else {}
    out["mode"] = {"effective": settings.x_mode, "label": X_MODE_NL.get(settings.x_mode, settings.x_mode), "requested": settings.x_mode_requested or "unset",
                   "note": settings.x_mode_note, "live_allowed": settings.x_live_allowed, "token": "configured" if settings.x_configured else "not configured",
                   "social_daemon": kv.get("social.x_mode") or "niet gestart", "engine_daemon": kv.get("engine.x_mode") or "niet gestart"}
    cap = float(settings.x_daily_budget_usd)
    if conn is None:
        out.update({"note": "Er is nog geen database.", "budget": {"cap": cap, "cap_label": usd(cap), "spend_today": 0.0, "spend_label": usd(0.0),
                                                                     "share_label": pct(0.0), "blocked_today": 0},
                    "spend_by_kind": [], "planner": {}, "active_keys": [], "stopped_rows": [], "plan": [], "plan_error": None, "shadow": {}})
        return out
    t0 = day_start(now)
    day = day_key(now)
    spend_rows = [{"kind": str(r["kind"]), "n": int(r["n"]), "usd": round(float(r["usd"]), 4), "usd_label": usd(r["usd"])} for r in conn.execute(
        "SELECT kind, SUM(n) AS n, SUM(usd) AS usd FROM x_spend WHERE day=? GROUP BY kind ORDER BY usd DESC", (day,))]
    spend = sum(r["usd"] for r in spend_rows)
    blocked = int(conn.execute("SELECT COUNT(*) FROM api_ledger WHERE source='x' AND blocked_reason IS NOT NULL AND ts >= ?", (t0,)).fetchone()[0])
    out["budget"] = {"cap": cap, "cap_label": usd(cap), "spend_today": round(spend, 4), "spend_label": usd(spend),
                     "share": (spend / cap) if cap else None, "share_label": pct(spend / cap) if cap else "–", "blocked_today": blocked}
    out["spend_by_kind"] = spend_rows
    if table_exists(conn, "x_keys"):
        pl = xplanner.status_counters(conn, now)
        deferred = {}
        suffix = f".{day}"
        for k, v in kv.items():
            if k.startswith("x.planner.deferred.") and k.endswith(suffix):
                deferred[k[len("x.planner.deferred."):-len(suffix)]] = int(v or 0)
        stopped = pl.get("stopped_today") or {}
        out["planner"] = {**pl, "deferred_today": deferred,
                          "active_label": ", ".join(f"{k} {v}" for k, v in sorted((pl.get("active") or {}).items())) or "geen",
                          "stopped_label": ", ".join(f"{STOP_NL.get(k, k)} {v['n']} ({usd(v['usd'])})" for k, v in sorted(stopped.items())) or "geen",
                          "deferred_label": ", ".join(f"{k} {v}" for k, v in sorted(deferred.items())) or "geen"}
        out["stopped_rows"] = [{"reason": k, "reason_label": STOP_NL.get(k, k), "n": v["n"], "usd": round(v["usd"], 4), "usd_label": usd(v["usd"]),
                                "counts": v["counts"], "searches": v["searches"]} for k, v in sorted(stopped.items())]
        out["active_keys"] = [{
            "key": r["key"], "kind": r["kind"], "decision_kind": r["decision_kind"], "started_at": utc_iso(r["started_at"]), "started_label": fmt_time(r["started_at"]),
            "counts_n": int(r["counts_n"]), "last_n_1h": r["last_n_1h"], "peak_n_1h": r["peak_n_1h"], "next_due_label": ts_label(r["next_due_at"]) if r["next_due_at"] else "–",
            "search_pending": bool(r["search_pending"]), "search_pending_label": "ja" if r["search_pending"] else "nee", "confirmed": r["confirmed_at"] is not None,
            "confirmed_label": "ja" if r["confirmed_at"] is not None else "nee", "usd": round(float(r["usd"]), 4), "usd_label": usd(r["usd"])}
            for r in conn.execute("SELECT * FROM x_keys WHERE stopped_at IS NULL ORDER BY prio_class, prio_value DESC, started_at LIMIT 50")]
    else:
        out["planner"] = {"note": "geen x_keys-tabel (schema vóór mijlpaal 7)"}
        out["stopped_rows"] = []
        out["active_keys"] = []
    out["plan"], out["plan_error"] = plan_rows(conn, settings, now, cap)
    out["shadow"] = {}
    if table_exists(conn, "shadow_decisions"):
        sh = shadow_counters(conn, now)
        sh["last_decision_label"] = ts_label(sh.get("last_decision_at"))
        out["shadow"] = sh
    return out


def plan_rows(conn: sqlite3.Connection, settings: Settings, now: float, cap: float) -> tuple[list[dict[str, Any]], str | None]:
    """The dry-run model's planned USD per kind next to today's actual spend; the page never dies on the model."""
    try:
        from ..social.dryrun import build

        d = build(conn, settings, now, hours=1)
    except Exception as e:  # noqa: BLE001  the plan model is read-only and advisory for the page
        return [], f"{type(e).__name__}: {str(e)[:120]}"
    rows = []
    for p in d.get("plan_vs_actual") or []:
        rows.append({"kind": p["kind"], "planned_usd": None if p["planned_usd"] is None else round(float(p["planned_usd"]), 4),
                     "planned_label": usd(p["planned_usd"]), "actual_usd": round(float(p["actual_usd"]), 4), "actual_label": usd(p["actual_usd"]),
                     "planned_share_label": pct(p.get("planned_share")), "actual_share_label": pct(p.get("actual_share"))})
    return rows, None


# ----- reports: out/recall/ ------------------------------------------------------------------------------------------
def report_dir(settings: Settings) -> Path:
    return settings.out_dir / "recall"


def report_files(settings: Settings) -> list[dict[str, Any]]:
    """The Markdown reports under out/recall/, newest first, week reports before day reports of the same day."""
    d = report_dir(settings)
    if not d.exists():
        return []
    out = []
    for p in d.iterdir():
        if not p.is_file() or not REPORT_NAME.match(p.name) or not p.name.endswith(".md"):
            continue
        week = p.name.startswith("week-")
        day = p.name[len("week-") if week else 0:-3]
        try:
            st = p.stat()
        except OSError:
            continue
        out.append({"name": p.name, "kind": "week" if week else "dag", "kind_label": "weekrapport" if week else "dagrapport", "day": day,
                    "day_label": fmt_day(day), "modified_label": fmt_time(st.st_mtime), "bytes": st.st_size, "href": f"/rapporten/{p.name}"})
    out.sort(key=lambda r: (r["day"], r["kind"] == "week"), reverse=True)
    return out


def read_report(settings: Settings, name: str) -> str | None:
    """The text of one report; None for a name outside the allowed shape or a missing file (no path from the client is used)."""
    if not REPORT_NAME.match(name or "") or not name.endswith(".md"):
        return None
    p = report_dir(settings) / name
    try:
        return p.read_text(encoding="utf-8")
    except OSError:
        return None


def _latest_json(settings: Settings, week: bool) -> tuple[str, dict[str, Any]] | None:
    d = report_dir(settings)
    if not d.exists():
        return None
    names = sorted(p.name for p in d.iterdir() if p.is_file() and REPORT_NAME.match(p.name) and p.name.endswith(".json")
                   and p.name.startswith("week-") == week)
    for name in reversed(names):
        try:
            data = json.loads((d / name).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(data, dict):
            return name, data
    return None


def report_latest_view(settings: Settings, now: float) -> dict[str, Any]:
    """The key points of the newest daily report: universe, winners, hit rate, recall, lead time, hours not covered."""
    out: dict[str, Any] = {**stamp(now), "found": False, "items": []}
    found = _latest_json(settings, week=False)
    if found is None:
        out["note"] = "Nog geen dagrapport (de timer schrijft het eerste om 06:00 UTC)."
        return out
    name, r = found
    status = str(r.get("coverage_status") or ("complete" if r.get("complete") else "incomplete"))
    blank = status == "not_covered"
    u, rc, p, cvg = r.get("universe") or {}, r.get("recall") or {}, r.get("precision") or {}, r.get("coverage") or {}
    day = str(r.get("day") or name[:-5])
    unk = r.get("unknowns") or {}
    hours_nc = unk.get("hours_not_covered", 24 - int(cvg.get("hours_covered", 0) or 0))
    lead = (rc.get("lead_vs_graduation") or {})

    def num(v: Any) -> Any:
        return None if blank else v

    def s(v: Any) -> str:
        return "–" if blank or v is None else str(v)

    items = [
        {"key": "universe", "label": "Universum: graduaties van de dag", "value": s(u.get("graduations")),
         "note": "–" if blank else f"gemeten {u.get('measured', 0)}, nog bezig {u.get('pending', 0)}, ontbrekend {u.get('missing', 0)}, zonder bekende create {u.get('without_create', 0)}"},
        {"key": "winners", "label": "Winnaars (piek ≥ $100k én volume ≥ $20k binnen 24 u)", "value": s(u.get("winners")),
         "note": "–" if blank else f"organisch {u.get('winners_organic', 0)}, gebundeld {u.get('winners_bundled', 0)}, bundeling onbekend {u.get('winners_bundled_unknown', 0)}; "
                                   f"geen winnaar {u.get('not_winner', 0)}, uitkomst onbekend {u.get('outcome_unknown', 0)}"},
        {"key": "hit_rate", "label": "Trefkans van de munt-observaties van de dag", "value": pct(p.get("precision"), blank),
         "note": "–" if blank else f"{p.get('hits', 0)} raak van {p.get('known', 0)} bekend; {p.get('unknown', 0)} onbekend, {p.get('pending', 0)} nog bezig ({p.get('decisions', 0)} beslissingen)"},
        {"key": "recall", "label": "Recall: winnaars met een munt-observatie vóór graduatie", "value": pct(rc.get("recall_before_graduation"), blank),
         "note": "–" if blank else f"{rc.get('decision_before_graduation', 0)} van {rc.get('winners', 0)} winnaars; met thema-beslissing vóór graduatie {rc.get('theme_before_graduation', 0)}"},
        {"key": "lead_time", "label": "Lead time t.o.v. graduatie (mediaan)", "value": "–" if blank or lead.get("median_s") is None else minutes(lead.get("median_s")),
         "note": "–" if blank else (f"n {lead.get('n', 0)}; op tijd {lead.get('positive', 0)}, te laat {lead.get('negative_or_zero', 0)}" if lead.get("n") else "geen winnaar met beslissing")},
        {"key": "hours_not_covered", "label": "Uren NIET GEDEKT", "value": str(hours_nc),
         "note": f"gedekt {cvg.get('hours_covered', 0)}/24, compleet {cvg.get('hours_complete', 0)}/24" + (f"; eerste start van een bron {cvg.get('first_source_start')}" if blank and cvg.get("first_source_start") else "")},
    ]
    out.update({
        "found": True, "file": name, "day": day, "day_label": fmt_day(day), "report_generated_at": r.get("generated_at"),
        "report_generated_label": ts_label(iso_to_ts(r.get("generated_at"))), "status": status, "status_label": STATUS_NL.get(status, status),
        "rules_version": r.get("rules_version_now"), "method_version": r.get("method_version"), "href": f"/rapporten/{day}.md",
        "blank": blank, "items": items,
        "raw": {"graduations": num(u.get("graduations")), "winners": num(u.get("winners")), "hit_rate": num(p.get("precision")),
                "recall_before_graduation": num(rc.get("recall_before_graduation")), "median_lead_s_vs_graduation": num(lead.get("median_s")),
                "hours_not_covered": hours_nc},
        "note": "Alle percentages zijn leeg: geen enkel uur van deze dag was gedekt; het universum is onbekend, niet leeg." if blank else None,
    })
    return out


def week_latest_view(settings: Settings, now: float) -> dict[str, Any]:
    """The go/no-go section of the newest week report: per segment the verdict and every frozen criterion."""
    out: dict[str, Any] = {**stamp(now), "found": False, "segments": []}
    found = _latest_json(settings, week=True)
    if found is None:
        out["note"] = "Nog geen weekrapport (de timer schrijft het eerste om 06:30 UTC)."
        return out
    name, r = found
    segs = []
    for seg in r.get("segments") or []:
        g = seg.get("gate") or {}
        u = seg.get("universe") or {}
        crit = []
        for c in g.get("criteria") or []:
            key = str(c.get("key"))
            v, t = c.get("value"), c.get("threshold")
            if v is None:
                vs = "–"
            elif key in ("hit_rate_ci_low", "recall_before_graduation_ci_low"):
                vs = pct(v)
            elif key == "median_lead_s_before_graduation":
                vs = minutes(v)
            elif isinstance(v, float):
                vs = f"{v:.1f}"
            else:
                vs = str(v)
            ts = pct(t) if key in ("hit_rate_ci_low", "recall_before_graduation_ci_low") else (minutes(t) if key == "median_lead_s_before_graduation" else str(t))
            crit.append({"key": key, "label": c.get("label"), "value": v, "value_label": vs, "threshold": t, "threshold_label": f"{c.get('op')} {ts}",
                         "status": c.get("status"), "_class": {"voldaan": "ok", "niet voldaan": "fail"}.get(str(c.get("status")), "")})
        group = str(seg.get("rules_group") or "")
        segs.append({"rules_group": group, "rules_group_short": group[:19], "frozen": bool(seg.get("frozen")), "method_version": seg.get("method_version"),
                     "rules_versions": seg.get("rules_versions") or [],
                     "title": f"Regels {group[:19]}" + (" (bevroren meetweek)" if seg.get("frozen") else "") + f" · method_version {seg.get('method_version')}",
                     "verdict": g.get("verdict"), "note": g.get("note"), "criteria": crit,
                     "covered_days": u.get("covered_days"), "days_total": len(seg.get("days") or []), "complete_days": u.get("complete_days"),
                     "not_covered_days": u.get("not_covered_days") or [], "hours_not_covered": u.get("hours_not_covered"), "winners": u.get("winners"),
                     "days_label": f"gedekt {u.get('covered_days')} van {len(seg.get('days') or [])}, compleet {u.get('complete_days')}"
                                   + (f", NIET GEDEKT: {', '.join(u.get('not_covered_days') or [])}" if u.get("not_covered_days") else "")})
    fr = r.get("frozen_rules") or {}
    out.update({"found": True, "file": name, "start_day": r.get("start_day"), "end_day": r.get("end_day"), "days": r.get("days"),
                "window_label": f"{fmt_day(str(r.get('start_day')))} t/m {fmt_day(str(r.get('end_day')))} ({r.get('days')} dagen"
                                + (", hele kalenderweek)" if r.get("calendar_week") else ", rollend venster)"),
                "report_generated_label": ts_label(iso_to_ts(r.get("generated_at"))), "rules_fingerprint_now": r.get("rules_fingerprint_now"),
                "frozen_rules": fr, "synthetic": bool(r.get("synthetic")), "href": f"/rapporten/{name[:-5]}.md", "segments": segs,
                "verdict": segs[0]["verdict"] if segs else None, "segment_title": segs[0]["title"] if segs else None})
    return out


# ----- /instellingen -------------------------------------------------------------------------------------------------
def settings_view(settings: Settings) -> dict[str, Any]:
    """Read-only: versions, the frozen go/no-go criteria, the active configuration without secrets."""
    from . import auth

    m = settings.config("measure")
    gate = m.get("community_gate") or {}
    e = settings.config("engine")
    x = settings.config("x")
    ncfg = settings.config("notify")
    desc = settings.describe()
    fr = gate.get("frozen_rules") or {}

    def rows(d: dict[str, Any], keys: list[tuple[str, str]]) -> list[dict[str, Any]]:
        return [{"label": label, "value": _plain(d.get(k))} for k, label in keys]

    sections = [
        {"title": "Versies", "rows": [
            {"label": "rules_version (deze uitrol)", "value": rules_version()},
            {"label": "Vingerafdruk van de regelbestanden", "value": rules_fingerprint()},
            {"label": "Bevroren regels van de meetweek", "value": f"rules_version {fr.get('rules_version')} sinds {fr.get('since')}, vingerafdruk {fr.get('rules_fingerprint')}, eerste volle dag {fr.get('first_full_day')}"},
            {"label": "Vingerafdruk gelijk aan de bevroren meetweek", "value": "ja" if rules_fingerprint() == fr.get("rules_fingerprint") else "NEE (andere regels dan de meetweek)"},
            {"label": "method_version (meetmethode)", "value": m.get("method_version")},
            {"label": "Regelbestanden", "value": ", ".join(_rule_files())},
        ]},
        {"title": "Bevroren go/no-go-criteria (community-alerts)", "rows": [
            {"label": "Minimaal complete dagen (piekuren gedekt)", "value": gate.get("min_complete_days")},
            {"label": "Piekuren (UTC)", "value": ", ".join(str(h) for h in gate.get("peak_hours_utc") or [])},
            {"label": "Minimaal winnaars in het universum", "value": gate.get("min_winners")},
            {"label": "Ondergrens bootstrapinterval trefkans", "value": pct(gate.get("min_hit_rate_ci_low"))},
            {"label": "Ondergrens bootstrapinterval recall vóór graduatie", "value": pct(gate.get("min_recall_before_graduation_ci_low"))},
            {"label": "Mediane lead time vóór graduatie, minimaal", "value": minutes(gate.get("min_median_lead_s_before_graduation"))},
            {"label": "Hoogstens observaties per dag", "value": gate.get("max_observations_per_day")},
            {"label": "Winnaarsdefinitie", "value": f"piek ≥ ${m['winner']['peak_mcap_usd']:,} én volume ≥ ${m['winner']['min_volume_usd']:,} binnen {m['winner']['within_s'] // 3600} u"},
        ]},
        {"title": "Regels van de engine (config/engine.json)", "rows": [
            {"label": "Munt: minimaal unieke kopers in 5 min", "value": e["coin"].get("observe_min_unique_buyers_5m")},
            {"label": "Munt: minimaal netto SOL in 5 min", "value": e["coin"].get("observe_min_net_sol_5m")},
            {"label": "Munt: hoogstens aandeel grootste koper", "value": e["coin"].get("observe_max_largest_buyer_share")},
            {"label": "Munt: niet gebundeld vereist", "value": _plain(e["coin"].get("observe_require_not_bundled"))},
            {"label": "Thema: burst ratio, minimaal munten, minimaal makers", "value": f"{e['meta'].get('burst_ratio')}, {e['meta'].get('min_coins')}, {e['meta'].get('min_distinct_creators')}"},
            {"label": "Thema: minimaal SOL-instroom en unieke kopers", "value": f"{e['meta'].get('observe_min_sol_inflow')}, {e['meta'].get('observe_min_unique_buyers')}"},
            {"label": "Watchlist: plafond, probation-reserve, meta_burst totaal", "value": f"{e['watchlist'].get('cap')}, {e['watchlist'].get('probation_reserve')}, {e['watchlist'].get('meta_burst_max_total')}"},
            {"label": "Beslissings-cooldown", "value": minutes(e.get("decision_cooldown_s"))},
            {"label": "X live toegestaan (x_live in config/engine.json)", "value": "ja" if settings.x_live_allowed else "nee"},
        ]},
        {"title": "Actieve instellingen (.env, zonder geheimen)", "rows": rows(desc, [
            ("rpc_http", "RPC (host)"), ("rpc_is_public", "Publieke RPC zonder sleutel"), ("rpc_daily_budget_cu", "Dagplafond CU"),
            ("pumpportal", "PumpPortal (host)"), ("pumpportal_api_key", "PumpPortal-sleutel"), ("pumpportal_trades", "Trades via"),
            ("x_bearer_token", "X bearer token"), ("x_mode", "X-stand (effectief)"), ("x_mode_requested", "X_MODE gevraagd"), ("x_mode_note", "X-stand opmerking"),
            ("x_daily_budget_usd", "Dagplafond X (USD)"), ("telegram", "Telegram eigenaar"), ("telegram_notify_enabled", "Telegram meldingen aan"),
            ("telegram_community", "Telegram community"), ("telegram_community_enabled", "Community aan"), ("telegram_community_preview", "Community-voorbeeld naar eigenaar"),
            ("web_bind", "Website luistert op"), ("web_password_hash", "Website wachtwoordhash"), ("web_secret", "Website cookie-geheim"), ("root", "FART_ROOT"),
        ])},
        {"title": "X-planner (config/x.json, geen regelbestand)", "rows": [
            {"label": "Prijzen (post, telling, auteur, verversing)", "value": f"${x['prices'].get('post_usd')}, ${x['prices'].get('counts_request_usd')}, ${x['prices'].get('author_expansion_usd')}, ${x['prices'].get('refresh_post_usd')} (niet geverifieerd)"},
            {"label": "Hoogstens actieve sleutels", "value": (x.get("planner") or {}).get("max_active_keys")},
            {"label": "Venster per sleutel", "value": minutes((x.get("confirm") or {}).get("window_s"))},
            {"label": "Bevestigd bij telling in het laatste uur ≥", "value": (x.get("confirm") or {}).get("search_when_count_1h")},
            {"label": "Reserve voor mega + KOL, KOL-plafond", "value": f"{pct((x.get('envelope') or {}).get('account_watch_reserve_fraction'))}, {usd((x.get('envelope') or {}).get('kol_daily_cap_usd'))}"},
        ]},
        {"title": "Community-kanaal (config/notify.json)", "rows": [
            {"label": "Hoogstens per uur, per munt/thema per", "value": f"{ncfg['community'].get('max_per_hour')}, {minutes(ncfg['community'].get('per_key_cooldown_s'))}"},
            {"label": "Stille uren (Europe/Amsterdam)", "value": f"{ncfg['community']['quiet_hours'].get('start')} tot {ncfg['community']['quiet_hours'].get('end')}"},
            {"label": "Vaste waarschuwing", "value": ncfg["community"].get("warning")},
        ]},
        {"title": "Deze website", "rows": [
            {"label": "Luistert op (alleen loopback, achter Caddy)", "value": settings.web_bind},
            {"label": "Database", "value": f"{settings.db_path} (alleen-lezen: mode=ro, PRAGMA query_only)"},
            {"label": "Sessie", "value": f"cookie {auth.SESSION_COOKIE}, HttpOnly, Secure, SameSite=Strict, verloopt na {auth.SESSION_TTL_S // 3600} uur"},
            {"label": "Loginbewaking", "value": f"hoogstens {auth.MAX_FAILED} mislukte pogingen per {auth.FAIL_WINDOW_S // 60} minuten per IP en per account, daarna {auth.LOCK_S // 60} minuten wachten"},
            {"label": "Verzoeken groter dan", "value": "64 KB worden geweigerd"},
            {"label": "Schrijft", "value": "niets behalve logs/web.log; geen knoppen die iets veranderen, ook niet voor de eigenaar"},
        ]},
    ]
    return {"sections": sections}


def _rule_files() -> list[str]:
    from ..version import RULE_FILES

    return list(RULE_FILES)


def _plain(v: Any) -> Any:
    if isinstance(v, bool):
        return "ja" if v else "nee"
    if v is None:
        return "–"
    return v
