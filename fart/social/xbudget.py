"""USD planner for the X API (milestone 7): the daily ledger (`x_spend`, with the billed unit per row), the per-day
dedupe (`x_dedupe`) and the admission policy that replaces the milestone-2 refusals with pacing.

Two numbers describe a call: its *ceiling* (what it can cost at most at the configured prices: every post new, every
author new) and its *expected* cost (what a call of that kind cost on average today, bounded by the ceiling; the
ceiling itself for a one-off call such as a confirmation count or search). The hard rules are checked against the
ceiling, so the daily cap can never be exceeded whatever the response contains: the cap itself (`daily_cost_cap`),
the share of the cap reserved for the mega + KOL watches (`account_watch_reserve`: confirmation calls may use at most
1 - reserve) and the KOL sub-cap (`kol_daily_cap`). A call that fails one of them is refused for the rest of the UTC
day and ledgered as blocked, as before. The hourly envelope (the cumulative share of the cap through the current hour,
peak hours weighted 1.0 and the rest 0.4) is checked against the expected cost and DEFERS instead of refusing: the
verdict says when to try again (the next UTC hour), nothing is ledgered, the deferral is counted in kv
(`x.planner.deferred.<kind>.<day>`) and logged once per hour per kind; the caller keeps the request in its plan (a key
stays due, `since_id` picks the posts up later). The generic gate (source `x`, `daily_cost` from X_DAILY_BUDGET_USD)
still enforces the cap on the booked costs behind this class; with the ceiling check in front of it, it never has to.
"""
from __future__ import annotations

import datetime as _dt
import sqlite3
from dataclasses import dataclass
from typing import Any, Callable, Iterable

from .. import db
from ..log import Throttle, get_logger
from ..net.budget import Budget
from ..util import day_key, day_start, now as wall_now

log = get_logger("xbudget")

SOURCE = "x"
ACCOUNT_WATCH_KINDS = ("mega_search", "kol_search", "refresh")
CONFIRM_KINDS = ("ca_counts", "ca_search", "ca_authors")
KOL_KINDS = ("kol_search",)
KINDS = ("mega_search", "refresh", "kol_search", "ca_counts", "ca_search", "ca_authors")
HARD_REASONS = ("daily_cost_cap", "account_watch_reserve", "kol_daily_cap")
DEFER_REASON = "hourly_envelope"
DEFERRED_PREFIX = "x.planner.deferred."
# the unit of `x_spend.n` per kind (what a row counts); the mega/KOL searches book posts and authors in separate rows
UNITS = {"mega_search": "post", "kol_search": "post", "refresh": "post", "ca_counts": "request", "ca_search": "post", "ca_authors": "author"}


@dataclass(frozen=True)
class XVerdict:
    allowed: bool
    reason: str | None = None
    spend_today: float = 0.0
    deferred: bool = False          # not now, later today (the envelope); allowed=False without deferred = refused for the day
    retry_at: float | None = None   # when a deferred call may be tried again (the start of the next UTC hour)


def next_hour(now: float) -> float:
    return (int(now) // 3600 + 1) * 3600.0


def hour_of(now: float) -> int:
    return _dt.datetime.fromtimestamp(now, tz=_dt.timezone.utc).hour


class XBudget:
    def __init__(self, conn: sqlite3.Connection, budget: Budget, cfg: dict[str, Any], daily_cap_usd: float,
                 clock: Callable[[], float] = wall_now):
        self.conn = conn
        self.budget = budget
        self.cfg = cfg
        self.cap = float(daily_cap_usd)
        self.clock = clock
        env = cfg.get("envelope", {})
        self.peak_hours = {int(h) for h in env.get("peak_hours_utc", [])}
        self.peak_w = float(env.get("peak_weight", 1.0))
        self.off_w = float(env.get("off_weight", 0.4))
        self.reserve = float(env.get("account_watch_reserve_fraction", 0.30))
        self.kol_cap = float(env.get("kol_daily_cap_usd", 1.0))
        self._throttle = Throttle(3600)

    # ----- prices ------------------------------------------------------------------------------------
    def price(self, name: str) -> float:
        return float(self.cfg.get("prices", {}).get(name, 0.0))

    def search_ceiling(self, max_results: int, authors: int) -> float:
        """The most a search can cost: every post and every author new today."""
        return int(max_results) * self.price("post_usd") + int(authors) * self.price("author_expansion_usd")

    # ----- ledger ------------------------------------------------------------------------------------
    def spend_today(self, now: float | None = None, kinds: Iterable[str] | None = None) -> float:
        t = self.clock() if now is None else now
        if kinds is None:
            row = self.conn.execute("SELECT COALESCE(SUM(usd), 0) FROM x_spend WHERE day=?", (day_key(t),)).fetchone()
        else:
            ks = list(kinds)
            marks = ",".join("?" for _ in ks)
            row = self.conn.execute(f"SELECT COALESCE(SUM(usd), 0) FROM x_spend WHERE day=? AND kind IN ({marks})", (day_key(t), *ks)).fetchone()
        return float(row[0])

    def spend_by_kind(self, now: float | None = None) -> dict[str, dict[str, Any]]:
        """Per kind: the booked units (`n`, all units together), the USD and the units apart (`units`: post / request / author;
        rows written before milestone 7 have no unit and count under `mixed`)."""
        t = self.clock() if now is None else now
        out: dict[str, dict[str, Any]] = {}
        for r in self.conn.execute("SELECT kind, COALESCE(unit, 'mixed') AS unit, SUM(n) AS n, SUM(usd) AS usd FROM x_spend WHERE day=? GROUP BY kind, unit",
                                   (day_key(t),)):
            d = out.setdefault(str(r["kind"]), {"n": 0, "usd": 0.0, "units": {}})
            d["n"] += int(r["n"])
            d["usd"] += float(r["usd"])
            d["units"][str(r["unit"])] = d["units"].get(str(r["unit"]), 0) + int(r["n"])
        return out

    def calls_today(self, now: float | None = None) -> dict[str, dict[str, float]]:
        """Successful calls per kind today from the gate's ledger (api_ledger: one row per call, the actual cost)."""
        t = self.clock() if now is None else now
        t0 = day_start(t)
        return {str(r["method"]): {"calls": int(r["calls"]), "usd": float(r["usd"])} for r in self.conn.execute(
            "SELECT method, COUNT(*) AS calls, COALESCE(SUM(cost), 0) AS usd FROM api_ledger"
            " WHERE source=? AND ok=1 AND blocked_reason IS NULL AND ts >= ? AND ts < ? GROUP BY method", (SOURCE, t0, t0 + 86400.0))}

    def book(self, kind: str, n: int, usd: float, now: float | None = None, unit: str | None = None) -> None:
        t = self.clock() if now is None else now
        self.conn.execute("INSERT INTO x_spend(day, kind, n, usd, ts, unit) VALUES(?,?,?,?,?,?)",
                          (day_key(t), kind, int(n), float(usd), t, unit or UNITS.get(kind)))

    def dedupe_new(self, kind: str, keys: Iterable[str], now: float | None = None) -> list[str]:
        """Keys not yet seen today for this kind (posts are billed once per UTC day, author expansions once per
        user per UTC day). Marks them seen."""
        t = self.clock() if now is None else now
        day = day_key(t)
        new: list[str] = []
        for k in keys:
            cur = self.conn.execute("INSERT OR IGNORE INTO x_dedupe(day, kind, key) VALUES(?,?,?)", (day, kind, str(k)))
            if cur.rowcount:
                new.append(str(k))
        return new

    def seen_today(self, kind: str, key: str, now: float | None = None) -> bool:
        t = self.clock() if now is None else now
        return self.conn.execute("SELECT 1 FROM x_dedupe WHERE day=? AND kind=? AND key=?", (day_key(t), kind, str(key))).fetchone() is not None

    def deferred_today(self, now: float | None = None) -> dict[str, int]:
        """Deferrals per kind today (kv counters x.planner.deferred.<kind>.<day>)."""
        t = self.clock() if now is None else now
        suffix = f".{day_key(t)}"
        out: dict[str, int] = {}
        for r in self.conn.execute("SELECT key, value FROM kv WHERE key LIKE ?", (DEFERRED_PREFIX + "%" + suffix,)):
            k = str(r["key"])[len(DEFERRED_PREFIX):-len(suffix)]
            out[k] = int(r["value"] or 0)
        return out

    # ----- pacing policy ------------------------------------------------------------------------------
    def is_peak(self, now: float) -> bool:
        return hour_of(now) in self.peak_hours

    def envelope_fraction(self, now: float) -> float:
        """Share of the daily cap available through the end of the current UTC hour."""
        hour = hour_of(now)
        weights = [self.peak_w if h in self.peak_hours else self.off_w for h in range(24)]
        total = sum(weights) or 1.0
        return sum(weights[: hour + 1]) / total

    def expected_cost(self, kind: str, ceiling: float, now: float | None = None) -> float:
        """What a call of this kind is expected to cost: the average actual cost of today's calls of the kind (the
        dedupe makes an incremental mega/KOL search nearly free), bounded by the ceiling; without calls today the
        milestone-2 prior of one new post plus one new author for the account watches, the ceiling for the rest."""
        t = self.clock() if now is None else now
        calls = self.calls_today(t).get(kind)
        if calls and calls["calls"] >= 3:
            return max(0.0, min(float(ceiling), calls["usd"] / calls["calls"]))
        if kind in ("mega_search", "kol_search"):
            return min(float(ceiling), self.price("post_usd") + self.price("author_expansion_usd"))
        return float(ceiling)

    def admit(self, kind: str, ceiling_usd: float, expected_usd: float | None = None, now: float | None = None) -> XVerdict:
        """The planner's verdict on one call. Hard rules against the ceiling (refused for the day, ledgered and
        logged, never silent); the hourly envelope against the expected cost (deferred to the next hour, counted)."""
        t = self.clock() if now is None else now
        ceiling = max(0.0, float(ceiling_usd))
        expected = ceiling if expected_usd is None else max(0.0, min(ceiling, float(expected_usd)))
        spend = self.spend_today(t)
        reason = None
        if self.cap <= 0:
            reason = "daily_cost_cap"
        elif spend + ceiling > self.cap:
            reason = "daily_cost_cap"
        elif kind in CONFIRM_KINDS and self.spend_today(t, CONFIRM_KINDS) + ceiling > self.cap * (1.0 - self.reserve):
            reason = "account_watch_reserve"
        elif kind in KOL_KINDS and self.spend_today(t, KOL_KINDS) + ceiling > self.kol_cap:
            reason = "kol_daily_cap"
        if reason is not None:
            self.budget.block(SOURCE, kind, reason)
            return XVerdict(False, reason, spend)
        if spend + expected > self.cap * self.envelope_fraction(t):
            retry = next_hour(t)
            db.counter_bump(self.conn, f"{DEFERRED_PREFIX}{kind}.{day_key(t)}", 1, t)
            if self._throttle.allow(f"deferred:{kind}", t):
                log.info("x.deferred", kind=kind, reason=DEFER_REASON, spend_today=round(spend, 4), expected=round(expected, 4),
                         allowance=round(self.cap * self.envelope_fraction(t), 4), retry_at=_dt.datetime.fromtimestamp(retry, tz=_dt.timezone.utc).strftime("%H:%M"))
            return XVerdict(False, DEFER_REASON, spend, deferred=True, retry_at=retry)
        return XVerdict(True, None, spend)

    def allow(self, kind: str, est_usd: float, now: float | None = None) -> XVerdict:
        """The milestone-2 entry point: one estimate, used as ceiling and expected cost alike."""
        return self.admit(kind, est_usd, est_usd, now)

    # ----- the per-kind view for `fart status` and the dry run -------------------------------------------
    def plan_view(self, now: float, planned: dict[str, float] | None = None) -> list[dict[str, Any]]:
        """Per kind: the planned USD for the day (when given), the actual USD so far, the share of the cap of each,
        the calls and the deferrals."""
        spend = self.spend_by_kind(now)
        calls = self.calls_today(now)
        deferred = self.deferred_today(now)
        out = []
        for kind in KINDS:
            actual = float((spend.get(kind) or {}).get("usd", 0.0))
            plan = None if planned is None else float(planned.get(kind, 0.0))
            out.append({"kind": kind, "planned_usd": plan, "planned_share": (plan / self.cap) if (plan is not None and self.cap) else None,
                        "actual_usd": actual, "actual_share": (actual / self.cap) if self.cap else None,
                        "units": (spend.get(kind) or {}).get("units", {}), "calls": int((calls.get(kind) or {}).get("calls", 0)),
                        "deferred": int(deferred.get(kind, 0))})
        return out
