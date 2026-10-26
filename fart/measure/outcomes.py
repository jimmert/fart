"""Outcome tracker: for every `observe` decision AND every graduation (a universe independent of
decisions) measure at +1 h, +6 h, +24 h: peak market cap (own trades for watched mints,
DexScreener samples otherwise), volume, hit_100k, bundled. Missing data stays unknown (NULL),
never 0. Rows are append-only; a new method_version writes new rows.

Milestone 5 (fidelity, not a method change): a run that cannot sample every subject (more mints than
`max_mints_per_run`, DexScreener blocked by the gate or failing) counts the mints it skipped in kv
`measure.track.skipped` (all time) and `measure.track.skipped.<day>`, with the reasons in
`measure.track.skipped.last`; `fart status` shows them. The reports count a graduation without samples at
least every 10 minutes as "uitkomst onbekend", never as "geen winnaar" (`fart/measure/report.py`).
"""
from __future__ import annotations

import json
import sqlite3
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

    def run(self, sample: bool = True) -> dict[str, Any]:
        now = self.clock()
        window = float(self.cfg["sampling"].get("track_window_s", 90000))
        subs = subjects(self.conn, now, window)
        stats: dict[str, Any] = {"subjects": len(subs), "sampled": None, "outcomes": 0, "paper": 0, "sol_usd": None}
        if sample and subs:
            sol_usd, src = self.feed.sol_usd()
            stats["sol_usd"] = sol_usd
            stats["sol_usd_src"] = src
            all_mints = sorted({s["mint"] for s in subs})
            mints = all_mints[: int(self.cfg["sampling"].get("max_mints_per_run", 600))]
            stats["sampled"] = self.feed.sample_mints(mints, sol_usd)
            skipped = {"over_cap": len(all_mints) - len(mints), "dexscreener": int(stats["sampled"].get("skipped", 0))}
            stats["skipped"] = skipped
            n_skipped = skipped["over_cap"] + skipped["dexscreener"]
            if n_skipped:
                def _count(c, n=n_skipped, detail=dict(skipped)) -> None:
                    db.counter_bump_daily(c, "measure.track.skipped", n, now)
                    db.kv_set(c, "measure.track.skipped.last", json.dumps({"at": now, **detail}), now)

                db.write_tx(self.conn, _count, what="measure.skipped")
                log.warning("track.skipped", **skipped, subjects=len(all_mints), max_mints_per_run=len(mints))
        stats["outcomes"], stats["paper"] = self.measure_due(now)
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
        Tracker(conn, settings, budget).run()
    finally:
        conn.close()
    return 0
