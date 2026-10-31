"""Outcome tracker: for every `observe` decision AND every graduation (a universe independent of
decisions) measure at +1 h, +6 h, +24 h: peak market cap (own trades for watched mints,
DexScreener samples otherwise), volume, hit_100k, bundled. Missing data stays unknown (NULL),
never 0. Rows are append-only; a new method_version writes new rows.

Milestone 5 (fidelity, not a method change): a run that cannot sample every subject (more mints than
`max_mints_per_run`, DexScreener blocked by the gate or failing) counts the mints it skipped in kv
`measure.track.skipped` (all time) and `measure.track.skipped.<day>`, with the reasons in
`measure.track.skipped.last`; `fart status` shows them. The reports count a graduation without samples at
least every 10 minutes as "uitkomst onbekend", never as "geen winnaar" (`fart/measure/report.py`).

Milestone 13 (capacity, not a method change either): the tracker samples every subject of its window every run
(`sampling.max_mints_per_run` 2,000 against some 1,100 subjects on the VPS; the DexScreener gate at 200 per minute),
newest anchor first when a bound bites, inside a time budget per step (`sampling.time_budget_s` for the sampling,
`tracker.run_budget_s` for the whole run against the unit's `TimeoutStartSec=240`: the creator statistics and the candle
queue get what is left). Every run records the duration of each step and its counts in kv `measure.track.last`, which
`fart status` prints next to the share of the last 24 hours' graduations with a sample at least every 10 minutes.
"""
from __future__ import annotations

import json
import sqlite3
import time
from typing import Any

from .. import db
from ..chain.curve import CurveParams, CurveState
from ..config import Settings
from ..engine import features
from ..log import get_logger
from ..net.budget import Budget
from ..util import now as wall_now
from .paper import points_from_rows, simulate
from .prices import PriceFeed

log = get_logger("measure")

ET = "COALESCE(block_time, received_at)"
KV_LAST = "measure.track.last"          # milestone 13: the last run's step durations and counts (JSON)
RUN_BUDGET_S = 220.0                    # the whole run must end inside the unit's TimeoutStartSec=240
SAMPLE_BUDGET_S = 150.0                 # the sampling step's share of it; the rest goes to the outcomes, the creator statistics and the candles
CREATOR_STATS_MAX_S = 60.0


def sol_usd_at(conn: sqlite3.Connection, t: float, tolerance_s: float = 3600.0) -> tuple[float | None, str]:
    """SOL/USD nearest to t from stored samples (any mint), else None."""
    row = conn.execute(
        "SELECT sol_usd, ts FROM mcap_samples WHERE sol_usd IS NOT NULL AND ts BETWEEN ? AND ? ORDER BY ABS(ts-?) LIMIT 1",
        (t - tolerance_s, t + tolerance_s, t),
    ).fetchone()
    if row is None:
        return None, "none"
    return float(row["sol_usd"]), f"sample:{int(abs(row['ts'] - t))}s"


def subjects(conn: sqlite3.Connection, now: float, window_s: float) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for r in conn.execute(
        "SELECT id, mint, as_of FROM decisions WHERE kind='observe' AND verdict='observe' AND mint IS NOT NULL AND as_of >= ?",
        (now - window_s,),
    ):
        out.append({"subject": "decision", "decision_id": r["id"], "mint": r["mint"], "anchor_time": float(r["as_of"])})
    for r in conn.execute(f"SELECT mint, {ET} AS t FROM migrations WHERE {ET} >= ?", (now - window_s,)):
        out.append({"subject": "graduation", "decision_id": None, "mint": r["mint"], "anchor_time": float(r["t"])})
    return out


def _has_outcome(conn: sqlite3.Connection, s: dict[str, Any], horizon_s: int, method_version: str) -> bool:
    if s["subject"] == "decision":
        q = "SELECT 1 FROM outcomes WHERE subject='decision' AND decision_id=? AND horizon_s=? AND method_version=?"
        args = (s["decision_id"], horizon_s, method_version)
    else:
        q = "SELECT 1 FROM outcomes WHERE subject='graduation' AND mint=? AND horizon_s=? AND method_version=?"
        args = (s["mint"], horizon_s, method_version)
    return conn.execute(q, args).fetchone() is not None


def measure_window(conn: sqlite3.Connection, mint: str, start: float, end: float, params: CurveParams) -> dict[str, Any]:
    """Peak market cap and volume in [start, end] from own trades and from samples."""
    res: dict[str, Any] = {"peak_mcap_usd": None, "peak_src": None, "volume_usd": None, "volume_src": None,
                           "peak_mcap_sol": None, "trades_n": 0, "samples_n": 0, "sol_usd": None, "sol_usd_src": None}
    rows = conn.execute(
        f"SELECT sol, virtual_sol, virtual_token, {ET} AS t FROM trades WHERE mint=? AND {ET} >= ? AND {ET} <= ?",
        (mint, start, end),
    ).fetchall()
    res["trades_n"] = len(rows)
    sol_usd, src = sol_usd_at(conn, start)
    res["sol_usd"], res["sol_usd_src"] = sol_usd, src
    if rows:
        peak_sol = None
        vol_sol = 0.0
        for r in rows:
            vol_sol += float(r["sol"] or 0.0)
            if r["virtual_sol"] and r["virtual_token"]:
                m = CurveState(int(r["virtual_sol"]), int(r["virtual_token"])).mcap_sol(params)
                peak_sol = m if peak_sol is None else max(peak_sol, m)
        res["peak_mcap_sol"] = peak_sol
        res["volume_sol"] = round(vol_sol, 6)
        if sol_usd is not None:
            if peak_sol is not None:
                res["peak_mcap_usd"] = peak_sol * sol_usd
                res["peak_src"] = "trades"
            res["volume_usd"] = vol_sol * sol_usd
            res["volume_src"] = "trades"
    samples = conn.execute(
        "SELECT ts, mcap_usd, fdv_usd, volume_h24_usd, json FROM mcap_samples WHERE mint=? AND ts >= ? AND ts <= ? ORDER BY ts",
        (mint, start, end + 600),  # a sample shortly after the window end still describes the window's tail
    ).fetchall()
    res["samples_n"] = len(samples)
    if samples:
        caps = [(s["mcap_usd"] if s["mcap_usd"] is not None else s["fdv_usd"]) for s in samples]
        caps = [c for c in caps if c is not None]
        if caps:
            pk = max(caps)
            if res["peak_mcap_usd"] is None or pk > res["peak_mcap_usd"]:
                res["peak_src"] = "dexscreener" if res["peak_mcap_usd"] is None else "both"
                res["peak_mcap_usd"] = pk
            elif res["peak_src"] == "trades":
                res["peak_src"] = "both"
        if res["volume_usd"] is None:
            last = samples[-1]
            horizon = end - start
            key = "volume_h1_usd" if horizon <= 3600 else "volume_h6_usd" if horizon <= 21600 else "volume_h24_usd"
            try:
                js = json.loads(last["json"]) if last["json"] else {}
            except json.JSONDecodeError:
                js = {}
            v = js.get(key, last["volume_h24_usd"] if key == "volume_h24_usd" else None)
            if v is not None:
                res["volume_usd"] = float(v)
                res["volume_src"] = f"dexscreener:{key}"
    return res


def hit_100k(peak: float | None, volume: float | None, wcfg: dict[str, Any]) -> bool | None:
    if peak is None:
        return None
    if peak < float(wcfg["peak_mcap_usd"]):
        return False
    if volume is None:
        return None
    return volume >= float(wcfg["min_volume_usd"])


def write_outcome(conn: sqlite3.Connection, s: dict[str, Any], horizon_s: int, measured_at: float, m: dict[str, Any],
                  hit: bool | None, bundled: bool | None, method_version: str, extra: dict[str, Any]) -> int:
    payload = dict(m)
    payload.update(extra)
    cur = conn.execute(
        "INSERT INTO outcomes(decision_id, subject, mint, anchor_time, horizon_s, measured_at, peak_mcap_usd, peak_src, volume_usd,"
        " hit_100k, bundled, sol_usd, json, method_version) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (s["decision_id"], s["subject"], s["mint"], s["anchor_time"], horizon_s, measured_at, m["peak_mcap_usd"], m["peak_src"],
         m["volume_usd"], None if hit is None else int(hit), None if bundled is None else int(bundled), m["sol_usd"],
         json.dumps(payload, default=str), method_version),
    )
    return int(cur.lastrowid)


class Tracker:
    def __init__(self, conn: sqlite3.Connection, settings: Settings, budget: Budget, clock=wall_now):
        self.conn = conn
        self.settings = settings
        self.cfg = settings.config("measure")
        self.pf = settings.config("pumpfun")
        self.params = CurveParams.from_config(self.pf)
        self.budget = budget
        self.clock = clock
        self.feed = PriceFeed(conn, budget, self.cfg, clock)
        self.method_version = str(self.cfg["method_version"])

    def ccfg(self) -> dict[str, Any]:
        c = dict(self.settings.config("engine")["coin"])
        c.setdefault("bundled_graduation_window_s", self.pf.get("bundled_graduation_window_s", 60))
        c.setdefault("bundled_dev_buy_fraction", self.pf.get("bundled_dev_buy_fraction", 0.5))
        return c

    def run(self, sample: bool = True, deadline: float | None = None) -> dict[str, Any]:
        """One run: sample every subject of the window (newest anchor first; the cap and the time budget cut the oldest),
        then write the outcomes that fell due. `deadline` is a `time.perf_counter()` value for the sampling step (default:
        `sampling.time_budget_s` from now); the step durations land in `stats["seconds"]`."""
        t0 = time.perf_counter()
        now = self.clock()
        scfg = self.cfg["sampling"]
        window = float(scfg.get("track_window_s", 90000))
        if deadline is None:
            deadline = t0 + float(scfg.get("time_budget_s", SAMPLE_BUDGET_S))
        subs = subjects(self.conn, now, window)
        stats: dict[str, Any] = {"subjects": len(subs), "sampled": None, "outcomes": 0, "paper": 0, "sol_usd": None, "seconds": {}}
        if sample and subs:
            sol_usd, src = self.feed.sol_usd()
            stats["sol_usd"] = sol_usd
            stats["sol_usd_src"] = src
            newest: dict[str, float] = {}
            for s in subs:
                newest[s["mint"]] = max(newest.get(s["mint"], 0.0), float(s["anchor_time"]))
            all_mints = sorted(newest, key=lambda m: (-newest[m], m))     # newest anchor first: a bound cuts the subjects whose window is nearly over
            mints = all_mints[: int(scfg.get("max_mints_per_run", 2000))]
            stats["sampled"] = self.feed.sample_mints(mints, sol_usd, deadline=deadline)
            skipped = {"over_cap": len(all_mints) - len(mints), "dexscreener": int(stats["sampled"].get("skipped", 0)), "time_budget": int(stats["sampled"].get("time_budget", 0))}
            stats["skipped"] = skipped
            n_skipped = sum(skipped.values())
            if n_skipped:
                def _count(c, n=n_skipped, detail=dict(skipped)) -> None:
                    db.counter_bump_daily(c, "measure.track.skipped", n, now)
                    db.kv_set(c, "measure.track.skipped.last", json.dumps({"at": now, **detail}), now)

                db.write_tx(self.conn, _count, what="measure.skipped")
                log.warning("track.skipped", **skipped, subjects=len(all_mints), max_mints_per_run=len(mints))
        stats["seconds"]["sample"] = round(time.perf_counter() - t0, 2)
        t1 = time.perf_counter()
        stats["outcomes"], stats["paper"] = self.measure_due(now)
        stats["seconds"]["outcomes"] = round(time.perf_counter() - t1, 2)
        db.kv_set(self.conn, "measure.track.last_run", str(now), now)
        log.info("track.done", **{k: (v if not isinstance(v, dict) else json.dumps(v)) for k, v in stats.items()})
        return stats

    def measure_due(self, now: float) -> tuple[int, int]:
        n_out = n_paper = 0
        ccfg = self.ccfg()
        wcfg = self.cfg["winner"]
        horizons = [int(h) for h in self.cfg["horizons_s"]]
        for s in subjects(self.conn, now, float(self.cfg["sampling"].get("track_window_s", 90000)) + max(horizons)):
            for h in horizons:
                end = s["anchor_time"] + h
                if end > now or _has_outcome(self.conn, s, h, self.method_version):
                    continue
                m = measure_window(self.conn, s["mint"], s["anchor_time"], end, self.params)
                f = features.coin_features(self.conn, s["mint"], end, self.params, ccfg)
                bundled = None if f is None else f.get("bundled")
                hit = hit_100k(m["peak_mcap_usd"], m["volume_usd"], wcfg)
                extra = {"bundled_reasons": None if f is None else f.get("bundled_reasons"),
                         "coin_known": f is not None, "graduated_by_end": None if f is None else f.get("graduated"),
                         "data_quality": "ok" if m["peak_mcap_usd"] is not None and m["volume_usd"] is not None else "partial" if m["peak_mcap_usd"] is not None else "unknown"}
                paper_due = s["subject"] == "decision" and h == max(horizons)

                def _write(c, s=s, h=h, m=m, hit=hit, bundled=bundled, extra=extra, paper_due=paper_due) -> int:
                    write_outcome(c, s, h, now, m, hit, bundled, self.method_version, extra)
                    return self.paper_for(s, now, c) if paper_due else 0

                n_paper += db.write_tx(self.conn, _write, what="measure.outcome")
                n_out += 1
        return n_out, n_paper

    def paper_for(self, s: dict[str, Any], now: float, conn: sqlite3.Connection | None = None) -> int:
        conn = self.conn if conn is None else conn
        if conn.execute("SELECT 1 FROM paper WHERE decision_id=? AND method_version=?", (s["decision_id"], self.method_version)).fetchone():
            return 0
        pcfg = self.cfg["paper"]
        rows = conn.execute(
            f"SELECT block_time, received_at, virtual_sol, virtual_token FROM trades WHERE mint=? AND {ET} >= ? AND {ET} <= ? ORDER BY {ET}",
            (s["mint"], s["anchor_time"], s["anchor_time"] + float(pcfg.get("entry_delay_s", 20)) + float(pcfg.get("max_hold_s", 86400)) + 3600),
        ).fetchall()
        res = simulate(points_from_rows(rows), self.params, pcfg, s["anchor_time"])
        conn.execute(
            "INSERT INTO paper(decision_id, entry_as_of, entry_price, size_sol, fill_price, slippage_bps, fees, exit_as_of, exit_price,"
            " exit_reason, pnl, json, method_version, computed_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (s["decision_id"], res.entry_as_of, res.entry_price, res.size_sol, res.fill_price, res.slippage_bps, res.fees_sol,
             res.exit_time, res.exit_price, res.exit_reason, res.pnl_sol, json.dumps(res.to_json(), default=str), self.method_version, now),
        )
        return 1


def run_track(settings: Settings) -> int:
    settings.ensure_dirs()
    conn = db.connect(settings.db_path)
    try:
        budget = Budget.from_settings(conn, settings)
        t0 = time.perf_counter()
        tracker = Tracker(conn, settings, budget)
        run_budget = float((tracker.cfg.get("tracker") or {}).get("run_budget_s", RUN_BUDGET_S))
        sample_budget = float(tracker.cfg["sampling"].get("time_budget_s", SAMPLE_BUDGET_S))
        now = wall_now()
        stats = tracker.run(deadline=t0 + min(sample_budget, run_budget))
        # milestone 12: two bounded steps of their own next to the tracker (never by the engine): the creator statistics and the
        # minute candles after graduation. Each one is isolated: a failure is logged and counted and never stops the tracker.
        # milestone 13: they get what is left of the run budget, so the whole run ends inside the unit's start timeout.
        extra = run_extra_steps(conn, budget, settings, wall_now(), deadline=t0 + run_budget)
        record_run(conn, now, stats, extra, time.perf_counter() - t0, run_budget)
    finally:
        conn.close()
    return 0


def run_extra_steps(conn: sqlite3.Connection, budget: Budget, settings: Settings, now: float, deadline: float | None = None) -> dict[str, Any]:
    """The milestone-12 steps of the track timer: `creator_stats` (fart/measure/creators.py) and the candle queue
    (fart/measure/candles.py), each bounded per run and visible in `fart status`. `deadline` (perf_counter) is the end of the
    whole run: the creator statistics get at most CREATOR_STATS_MAX_S of what is left, the candles the rest."""
    from . import candles, creators

    out: dict[str, Any] = {}

    def _left() -> float:
        return float("inf") if deadline is None else max(0.0, deadline - time.perf_counter())

    steps = (("creator_stats", lambda: creators.update(conn, settings, now, time_budget_s=max(5.0, min(CREATOR_STATS_MAX_S, _left() - 20.0)))),
             ("candles", lambda: candles.run(conn, budget, settings, now, deadline=None if deadline is None else deadline - 5.0)))
    for name, fn in steps:
        t0 = time.perf_counter()
        try:
            out[name] = fn()
        except Exception as e:  # noqa: BLE001  one step must never take the tracker down
            log.error(f"track.{name}_failed", exc_info=True)
            db.record_failure(conn, f"measure.{name}_failed", now, detail=f"{type(e).__name__}: {str(e)[:150]}")
            out[name] = {"error": f"{type(e).__name__}: {str(e)[:120]}"}
        if isinstance(out[name], dict):
            out[name]["seconds"] = round(time.perf_counter() - t0, 2)
    return out


def record_run(conn: sqlite3.Connection, now: float, stats: dict[str, Any], extra: dict[str, Any], total_s: float, run_budget_s: float) -> dict[str, Any]:
    """kv `measure.track.last` (milestone 13): the step durations and counts of the run, for `fart status`."""
    sampled = stats.get("sampled") or {}
    seconds = dict(stats.get("seconds") or {})
    for name in ("creator_stats", "candles"):
        v = extra.get(name) or {}
        if isinstance(v, dict) and v.get("seconds") is not None:
            seconds[name] = v["seconds"]
    seconds["total"] = round(total_s, 2)
    rec = {"at": now, "seconds": seconds, "run_budget_s": run_budget_s, "over_budget": total_s > run_budget_s, "subjects": stats.get("subjects"),
           "sampled": {k: sampled.get(k) for k in ("requested", "stored", "no_pair", "calls", "blocked", "errors", "skipped", "time_budget")} if sampled else None,
           "skipped": stats.get("skipped"), "outcomes": stats.get("outcomes"), "paper": stats.get("paper"), "sol_usd_src": stats.get("sol_usd_src"),
           "creator_stats": {k: (extra.get("creator_stats") or {}).get(k) for k in ("creators_updated", "stopped_early", "backlog_sources", "error")} if isinstance(extra.get("creator_stats"), dict) else None,
           "candles": {k: (extra.get("candles") or {}).get(k) for k in ("due", "fetched", "candles", "errors", "blocked", "time_budget", "error")} if isinstance(extra.get("candles"), dict) else None}
    try:
        db.write_tx(conn, lambda c: db.kv_set(c, KV_LAST, json.dumps(rec, default=str), now), what="measure.track.last")
    except Exception:  # noqa: BLE001  a status record must never fail the run
        log.error("track.record_failed", exc_info=True)
    if rec["over_budget"]:
        log.warning("track.over_budget", total_s=round(total_s, 1), run_budget_s=run_budget_s, seconds=json.dumps(seconds))
    else:
        log.info("track.steps", **{k: v for k, v in seconds.items()})
    return rec


def last_run(conn: sqlite3.Connection) -> dict[str, Any] | None:
    raw = db.kv_get(conn, KV_LAST)
    if not raw:
        return None
    try:
        v = json.loads(raw)
    except ValueError:
        return None
    return v if isinstance(v, dict) else None


def graduation_coverage_24h(conn: sqlite3.Connection, now: float, max_gap_s: float, min_age_s: float = 900.0, lag_s: float = 600.0) -> dict[str, Any]:
    """The share of the graduations of the last 24 hours with a tracker sample at least every `max_gap_s` from the graduation
    until now (minus one tracker interval, `lag_s`, so the gap that is still running does not count) or until the end of
    their 24 h window: the coverage rule of the day and week reports, read live. Graduations younger than `min_age_s` are
    left out (nothing to cover yet)."""
    from .report import tracker_coverage

    rows = conn.execute(f"SELECT mint, {ET} AS t FROM migrations WHERE received_at >= ? AND {ET} >= ? AND {ET} <= ?",
                        (now - 86400 - 3600, now - 86400, now - min_age_s)).fetchall()
    covered = 0
    for r in rows:
        t = float(r["t"])
        end = min(t + 86400.0, now - lag_s)
        if end <= t:
            continue
        if tracker_coverage(conn, str(r["mint"]), t, end, max_gap_s)["covered"]:
            covered += 1
    return {"graduations": len(rows), "covered": covered, "share": (covered / len(rows)) if rows else None, "max_gap_s": max_gap_s}
