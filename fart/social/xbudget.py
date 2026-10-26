"""USD budget for the X API: daily ledger (`x_spend`), per-day dedupe (`x_dedupe`), hourly envelope,
account-watch reserve and the KOL sub-cap. The hard daily cap is also enforced by the generic budget
gate (source `x`, `daily_cost` from X_DAILY_BUDGET_USD); this class adds the pacing policy and the
per-kind view for `fart status`. A refused call is recorded like any gate refusal.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Any, Callable, Iterable

from ..log import get_logger
from ..net.budget import Budget
from ..util import day_key, now as wall_now

log = get_logger("xbudget")

SOURCE = "x"
ACCOUNT_WATCH_KINDS = ("mega_search", "kol_search", "refresh")
CONFIRM_KINDS = ("ca_counts", "ca_search")
KOL_KINDS = ("kol_search",)


@dataclass(frozen=True)
class XVerdict:
    allowed: bool
    reason: str | None = None
    spend_today: float = 0.0


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

    # ----- prices ------------------------------------------------------------------------------------
    def price(self, name: str) -> float:
        return float(self.cfg.get("prices", {}).get(name, 0.0))

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

    def spend_by_kind(self, now: float | None = None) -> dict[str, dict[str, float]]:
        t = self.clock() if now is None else now
        return {r["kind"]: {"n": int(r["n"]), "usd": float(r["usd"])} for r in self.conn.execute(
            "SELECT kind, SUM(n) AS n, SUM(usd) AS usd FROM x_spend WHERE day=? GROUP BY kind", (day_key(t),))}

    def book(self, kind: str, n: int, usd: float, now: float | None = None) -> None:
        t = self.clock() if now is None else now
        self.conn.execute("INSERT INTO x_spend(day, kind, n, usd, ts) VALUES(?,?,?,?,?)", (day_key(t), kind, int(n), float(usd), t))

    def dedupe_new(self, kind: str, keys: Iterable[str], now: float | None = None) -> list[str]:
        """Keys not yet seen today for this kind (posts are billed once per UTC day, author expansions
        once per user per UTC day). Marks them seen."""
        t = self.clock() if now is None else now
        day = day_key(t)
        new: list[str] = []
        for k in keys:
            cur = self.conn.execute("INSERT OR IGNORE INTO x_dedupe(day, kind, key) VALUES(?,?,?)", (day, kind, str(k)))
            if cur.rowcount:
                new.append(str(k))
        return new

    # ----- pacing policy ------------------------------------------------------------------------------
    def envelope_fraction(self, now: float) -> float:
        """Share of the daily cap available through the end of the current UTC hour."""
        import datetime as _dt

        hour = _dt.datetime.fromtimestamp(now, tz=_dt.timezone.utc).hour
        weights = [self.peak_w if h in self.peak_hours else self.off_w for h in range(24)]
        total = sum(weights) or 1.0
        return sum(weights[: hour + 1]) / total

    def allow(self, kind: str, est_usd: float, now: float | None = None) -> XVerdict:
        """Policy check before a call; a refusal is ledgered (api_ledger) and logged, never silent."""
        t = self.clock() if now is None else now
        spend = self.spend_today(t)
        reason = None
        if self.cap <= 0:
            reason = "daily_cost_cap"
        elif spend + est_usd > self.cap:
            reason = "daily_cost_cap"
        elif spend + est_usd > self.cap * self.envelope_fraction(t):
            reason = "hourly_envelope"
        elif kind in CONFIRM_KINDS and self.spend_today(t, CONFIRM_KINDS) + est_usd > self.cap * (1.0 - self.reserve):
            reason = "account_watch_reserve"
        elif kind in KOL_KINDS and self.spend_today(t, KOL_KINDS) + est_usd > self.kol_cap:
            reason = "kol_daily_cap"
        if reason is not None:
            self.budget.block(SOURCE, kind, reason)
            return XVerdict(False, reason, spend)
        return XVerdict(True, None, spend)
