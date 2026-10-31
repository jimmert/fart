"""Following signals (milestone 13): every coin that meets a pre-registered follow trigger gets 24 hours of price points,
so every signal of every hypothesis has a measurable outcome instead of an "onbekend" the moment the trade stream lets go.

What is followed (`followed`, one row per coin, written by the plays daemon every tick on its candidates):
* every coin that meets a trigger of `fart/research/hypotheses.py::FOLLOW_TRIGGERS` on the widest level of a hypothesis
  family (at least 10 unique buyers in 5 minutes for H1 / H4 / H5 / H7, a theme coin with trades for H3, a curve of 60 SOL
  for H2), judged on the same `Ctx` the predicates see;
* every coin on which an active play rule fired (reason `play:<rule>`), and every open play;
* every coin with an observe decision of the engine (reason `observe`, from the `decisions` table since the daemon's cursor).
`since` is the signal time, `until` 24 hours later; `reason` lists the triggers in the order they fired.

Where the points come from, cheapest sufficient source first:
(a) the trade stream while the coin has a subscription (unchanged: every trade is a point; a coin with an open subscription
    gets only a watch poll every `slow_interval_s`, so a time-based exit can still fill when the trades stop);
(b) outside the stream and before graduation: polls of the BondingCurve account through `getMultipleAccounts` (at most
    `max_addresses_per_call` = 100 addresses per call, one ledger row per call, 20 CU each on the Alchemy page of 31 Oct 2026)
    every `fast_interval_s` (60 s) in the first `fast_window_s` (2 h) after the first signal and every `slow_interval_s` (300 s)
    after that, decoded against the IDL (`fart/chain/bonding_curve.py`) and stored in `path_points` with source 'poll'; a poll
    that finds `complete` marks the graduation when the stream missed the migration (the reserves are zero then: the point
    carries the time, the price comes from the last known state);
(c) after graduation: the tracker's samples and the candles (existing).

Bounds, each visible in `fart status` (`follow:` line) and in kv: a share of the daily CU cap (`daily_cu_share`, 0.10 of
`RPC_DAILY_BUDGET_CU`; above it every call is refused and booked as blocked with reason `follow_daily_share`), a maximum of
followed coins polled per tick (`max_followed`, 1,500, newest signal first; the rest waits and is counted as `over_cap`),
a degradation level that lengthens the fast interval 60 -> 120 -> 300 s when the share's hourly envelope is exceeded or the
gate refuses (and shortens it again when the usage falls under half of the envelope), the slow interval for a curve whose
reserves did not change for `unchanged_slow_after_s` (2 h), and `closed` for a coin whose account stays missing or
undecodable (`missing_closes_after` polls). A poll never fails silently: calls, refusals, errors and misses are counted per
day (kv `follow.*`), the last tick is in kv `follow.last`, and the first run is in kv `follow.started_at` so the research
reports can split signals from before and after the follow started.

The polls need the keyed RPC: with the public fallback (no `RPC_HTTP_URL`) the step says so and does nothing, like the trade
stream. Nothing here touches the engine, a rule file or a decision.
"""
from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass
from typing import Any

from .. import db
from ..chain.bonding_curve import decode_account_value
from ..chain.pda import bonding_curve_address
from ..chain.rpc import RpcClient, RpcError
from ..config import Settings
from ..log import Throttle, get_logger
from ..net.budget import Blocked, Budget
from ..net.http import HttpError
from ..research import features as rf
from ..util import day_key, now as wall_now

log = get_logger("follow")

ET = "COALESCE(block_time, received_at)"
METHOD = "getMultipleAccounts"
BLOCK_REASON_SHARE = "follow_daily_share"
KV_LAST = "follow.last"
KV_STARTED = "follow.started_at"
KV_DECISIONS_CURSOR = "follow.decisions_cursor"
KV_LEVEL = "follow.level"
KV_LEVEL_CHANGED = "follow.level_changed_at"
KV_POLLS = "follow.polls"          # addresses polled (daily counter)
KV_CALLS = "follow.calls"
KV_POINTS = "follow.points"
KV_ERRORS = "follow.errors"
KV_BLOCKED = "follow.blocked"
KV_MISSES = "follow.misses"
STATES = ("active", "graduated", "done", "closed")
LEVEL_UP_AFTER_S = 300.0           # at most one step up per 5 minutes ...
LEVEL_DOWN_AFTER_S = 900.0         # ... and one step down per 15 minutes (hysteresis)
LEVEL_DOWN_BELOW = 0.5             # step down when the hourly usage is under half of the hourly envelope


@dataclass(frozen=True)
class FollowConfig:
    enabled: bool = True
    require_keyed_rpc: bool = True
    follow_s: float = 86400.0
    max_followed: int = 1500
    fast_interval_s: float = 60.0
    fast_window_s: float = 7200.0
    slow_interval_s: float = 300.0
    degraded_intervals_s: tuple[float, ...] = (60.0, 120.0, 300.0)
    daily_cu_share: float = 0.10
    max_addresses_per_call: int = 100
    max_calls_per_tick: int = 10
    unchanged_slow_after_s: float = 7200.0
    missing_closes_after: int = 3
    stream_interval_s: float = 300.0

    @classmethod
    def from_settings(cls, settings: Settings) -> "FollowConfig":
        c = (settings.config("measure") or {}).get("follow") or {}
        kw: dict[str, Any] = {}
        for f in cls.__dataclass_fields__:  # type: ignore[attr-defined]
            if f in c and c[f] is not None:
                v = c[f]
                kw[f] = tuple(float(x) for x in v) if f == "degraded_intervals_s" else (bool(v) if f in ("enabled", "require_keyed_rpc") else (int(v) if f in ("max_followed", "max_addresses_per_call", "max_calls_per_tick", "missing_closes_after") else float(v)))
        kw["max_addresses_per_call"] = min(100, int(kw.get("max_addresses_per_call", 100)))
        return cls(**kw)

    @property
    def max_level(self) -> int:
        return max(0, len(self.degraded_intervals_s) - 1)


def table_exists(conn: sqlite3.Connection, name: str = "followed") -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone() is not None


class Follower:
    """The follow step of the plays daemon: the rows, the lifecycle and the polls. `rpc` is injectable (tests: a fake with
    `get_multiple_accounts_base64`); without one the keyed RPC client is built from `budget`, and with the public fallback the
    polls are disabled with a reason (the rows are still kept)."""

    def __init__(self, conn: sqlite3.Connection, settings: Settings, budget: Budget | None = None, rpc: Any = None, clock=wall_now, cfg: FollowConfig | None = None):
        self.conn = conn
        self.settings = settings
        self.clock = clock
        self.cfg = cfg or FollowConfig.from_settings(settings)
        self.pf = settings.config("pumpfun")
        self.cap_cu = float(settings.rpc_daily_budget_cu)
        self.source = "rpc_public" if settings.rpc_is_public else "rpc"
        self.budget = budget
        self.rpc = rpc
        self.disabled_reason: str | None = None
        if not self.cfg.enabled:
            self.disabled_reason = "follow.enabled is false in config/measure.json"
        elif rpc is None and settings.rpc_is_public and self.cfg.require_keyed_rpc:
            self.disabled_reason = "the polls need the keyed RPC (RPC_HTTP_URL): the public endpoint has no CU accounting"
        elif rpc is None and budget is not None:
            self.rpc = RpcClient(budget, settings.rpc_http_url, settings.rpc_is_public)
        elif rpc is None:
            self.disabled_reason = "no RPC client"
        if self.budget is None and self.rpc is not None:
            self.budget = getattr(self.rpc, "budget", None)
        self.level = 0
        self.level_changed_at = 0.0
        self.last_stats: dict[str, Any] = {}
        self._pda_cache: dict[str, str] = {}
        self._throttle = Throttle(3600.0)
        if table_exists(conn):
            try:
                self.level = max(0, min(self.cfg.max_level, int(db.kv_get(conn, KV_LEVEL, "0") or 0)))
                self.level_changed_at = float(db.kv_get(conn, KV_LEVEL_CHANGED, "0") or 0)
            except (TypeError, ValueError):
                self.level = 0

    # ----- following --------------------------------------------------------------------------------------------------------
    @property
    def polls_enabled(self) -> bool:
        return self.disabled_reason is None and self.rpc is not None

    def _curve_of(self, c: sqlite3.Connection, mint: str) -> str | None:
        if mint in self._pda_cache:
            return self._pda_cache[mint]
        row = c.execute("SELECT bonding_curve FROM coins WHERE mint=?", (mint,)).fetchone()
        curve = str(row["bonding_curve"]) if row is not None and row["bonding_curve"] else None
        if curve is None:
            try:
                curve = bonding_curve_address(mint, self.pf["program_id"])
            except (ValueError, TypeError):
                curve = None
        if curve is not None:
            self._pda_cache[mint] = curve
        return curve

    def follow_many(self, items: list[tuple[str, float, str]], now: float) -> dict[str, int]:
        """Follow the coins of `items` ((mint, signal time, reason)); an existing row keeps its times and gets the reason appended
        once. Returns the counts of new and updated rows."""
        if not items or not table_exists(self.conn):
            return {"new": 0, "updated": 0}

        def _write(c: sqlite3.Connection) -> dict[str, int]:
            out = {"new": 0, "updated": 0}
            db.kv_setdefault(c, KV_STARTED, str(now), now)
            for mint, signal_t, reason in items:
                mint = str(mint)
                reason = str(reason)[:60]
                row = c.execute("SELECT reason, state FROM followed WHERE mint=?", (mint,)).fetchone()
                if row is not None:
                    reasons = [r for r in str(row["reason"] or "").split(",") if r]
                    if reason not in reasons:
                        reasons.append(reason)
                        c.execute("UPDATE followed SET reason=?, updated_at=? WHERE mint=?", (",".join(reasons), now, mint))
                        out["updated"] += 1
                    continue
                since = float(signal_t)
                until = since + self.cfg.follow_s
                curve = self._curve_of(c, mint)
                grad = c.execute(f"SELECT {ET} AS t FROM migrations WHERE mint=?", (mint,)).fetchone()
                state, closed_reason, graduated_at = "active", None, None
                if curve is None:
                    state, closed_reason = "closed", "bad_mint"
                elif grad is not None:
                    state, graduated_at = "graduated", float(grad["t"])
                elif until <= now:
                    state = "done"
                c.execute("INSERT INTO followed(mint, since, reason, until, state, curve, next_poll_at, interval_s, polls, errors, misses, graduated_at, closed_reason,"
                          " created_at, updated_at, json) VALUES(?,?,?,?,?,?,?,?,0,0,0,?,?,?,?,?)",
                          (mint, since, reason, until, state, curve, now if state == "active" else None, self.cfg.fast_interval_s, graduated_at, closed_reason, now, now,
                           json.dumps({"reasons": [reason]})))
                out["new"] += 1
            return out

        return db.write_tx(self.conn, _write, what="follow.rows")

    def add_observe_decisions(self, now: float) -> int:
        """Every coin with a new observe decision of the engine since the cursor (the cursor starts at the first run: nothing is
        followed with hindsight)."""
        if not table_exists(self.conn):
            return 0
        raw = db.kv_get(self.conn, KV_DECISIONS_CURSOR)
        if raw is None:
            db.write_tx(self.conn, lambda c: db.kv_set(c, KV_DECISIONS_CURSOR, str(now), now), what="follow.cursor")
            return 0
        cursor = float(raw)
        rows = self.conn.execute("SELECT mint, as_of, created_at FROM decisions WHERE kind='observe' AND verdict='observe' AND mint IS NOT NULL AND created_at > ?"
                                 " ORDER BY created_at LIMIT 500", (cursor,)).fetchall()
        if not rows:
            return 0
        res = self.follow_many([(str(r["mint"]), float(r["as_of"]), "observe") for r in rows], now)
        newest = max(float(r["created_at"]) for r in rows)
        db.write_tx(self.conn, lambda c: db.kv_set(c, KV_DECISIONS_CURSOR, str(newest), now), what="follow.cursor")
        return int(res["new"])

    def add_plays(self, now: float) -> int:
        """Every coin with an open play (signal or open) is followed for the play's 24 hours."""
        if not table_exists(self.conn) or not table_exists(self.conn, "plays"):
            return 0
        rows = self.conn.execute("SELECT mint, as_of, rule FROM plays WHERE status IN ('signal', 'open') AND as_of >= ?", (now - self.cfg.follow_s,)).fetchall()
        if not rows:
            return 0
        return int(self.follow_many([(str(r["mint"]), float(r["as_of"]), f"play:{r['rule']}") for r in rows], now)["new"])

    def refresh(self, now: float) -> dict[str, int]:
        """The lifecycle of the active rows: `done` after the 24 hours, `graduated` once a migration is known."""
        if not table_exists(self.conn):
            return {"done": 0, "graduated": 0}

        def _write(c: sqlite3.Connection) -> dict[str, int]:
            done = c.execute("UPDATE followed SET state='done', updated_at=? WHERE state='active' AND until <= ?", (now, now)).rowcount
            grad = c.execute(f"UPDATE followed SET state='graduated', graduated_at=(SELECT {ET} FROM migrations m WHERE m.mint = followed.mint), updated_at=?"
                             " WHERE state='active' AND mint IN (SELECT mint FROM migrations)", (now,)).rowcount
            return {"done": int(done), "graduated": int(grad)}

        return db.write_tx(self.conn, _write, what="follow.refresh")

    # ----- the budget share and the degradation level -----------------------------------------------------------------------
    def cu_since(self, since: float, now: float) -> float:
        row = self.conn.execute("SELECT COALESCE(SUM(cost), 0) FROM api_ledger WHERE source=? AND method=? AND blocked_reason IS NULL AND ts >= ? AND ts <= ?",
                                (self.source, METHOD, since, now)).fetchone()
        return float(row[0] or 0.0)

    @property
    def share_cu(self) -> float:
        return self.cfg.daily_cu_share * self.cap_cu

    def call_cost(self) -> float:
        if self.budget is None:
            return 0.0
        try:
            return float(self.budget.source(self.source).cost_of(METHOD))
        except KeyError:
            return 0.0

    def update_level(self, now: float, force_up: str | None = None) -> int:
        """Degrade when the last hour's CU exceed the share's hourly envelope (or the gate refused: `force_up`), recover when the
        usage falls under half of it; at most one step per 5 (up) / 15 (down) minutes."""
        envelope = self.share_cu / 24.0
        used = self.cu_since(now - 3600.0, now)
        before = self.level
        if force_up is not None or (envelope > 0 and used > envelope):
            if self.level < self.cfg.max_level and now - self.level_changed_at >= LEVEL_UP_AFTER_S:
                self.level += 1
                self.level_changed_at = now
                log.warning("follow.degraded", degradation_level=self.level, interval_s=self.interval_at_level(), cu_last_hour=round(used), envelope_cu=round(envelope), why=force_up or "hourly_envelope")
        elif envelope > 0 and used < LEVEL_DOWN_BELOW * envelope and self.level > 0 and now - self.level_changed_at >= LEVEL_DOWN_AFTER_S:
            self.level -= 1
            self.level_changed_at = now
            log.info("follow.recovered", degradation_level=self.level, interval_s=self.interval_at_level(), cu_last_hour=round(used), envelope_cu=round(envelope))
        if self.level != before:
            def _kv(c: sqlite3.Connection) -> None:
                db.kv_set(c, KV_LEVEL, str(self.level), now)
                db.kv_set(c, KV_LEVEL_CHANGED, str(now), now)

            db.write_tx(self.conn, _kv, what="follow.level")
        return self.level

    def interval_at_level(self) -> float:
        return float(self.cfg.degraded_intervals_s[min(self.level, self.cfg.max_level)])

    def interval_for(self, row: sqlite3.Row | dict[str, Any], now: float, has_stream: bool) -> float:
        since = float(row["since"])
        changed_at = row["changed_at"]
        base = self.cfg.fast_interval_s if now - since < self.cfg.fast_window_s else self.cfg.slow_interval_s
        if changed_at is not None and now - float(changed_at) >= self.cfg.unchanged_slow_after_s:
            base = self.cfg.slow_interval_s
        if has_stream:
            base = max(base, self.cfg.stream_interval_s)
        return max(base, self.interval_at_level())

    # ----- the poll step ----------------------------------------------------------------------------------------------------
    def due_rows(self, now: float) -> tuple[list[sqlite3.Row], int]:
        """(the active rows due for a poll, oldest due first, within the newest `max_followed`; the number of active rows beyond
        that bound)."""
        rows = self.conn.execute("SELECT * FROM followed WHERE state='active' ORDER BY since DESC, mint LIMIT ?", (int(self.cfg.max_followed),)).fetchall()
        active = int(self.conn.execute("SELECT COUNT(*) FROM followed WHERE state='active'").fetchone()[0])
        due = [r for r in rows if r["curve"] and (r["next_poll_at"] is None or float(r["next_poll_at"]) <= now)]
        due.sort(key=lambda r: (float(r["next_poll_at"] or 0.0), str(r["mint"])))
        return due, max(0, active - len(rows))

    def poll(self, now: float | None = None) -> dict[str, Any]:
        now = self.clock() if now is None else now
        t0 = time.perf_counter()
        stats: dict[str, Any] = {"at": now, "enabled": self.polls_enabled, "disabled_reason": self.disabled_reason, "active": 0, "due": 0, "over_cap": 0, "calls": 0,
                                 "polled": 0, "points": 0, "graduated": 0, "closed": 0, "misses": 0, "errors": 0, "blocked": 0, "level": self.level, "cu_today": 0.0,
                                 "share_cu": self.share_cu, "paused": False}
        if not table_exists(self.conn):
            stats["enabled"] = False
            stats["disabled_reason"] = "no followed table yet (schema v10)"
            return stats
        stats["active"] = int(self.conn.execute("SELECT COUNT(*) FROM followed WHERE state='active'").fetchone()[0])
        day0 = now - (now % 86400.0)
        stats["cu_today"] = self.cu_since(day0, now)
        if not self.polls_enabled:
            self._record(stats, t0, now)
            return stats
        due, over_cap = self.due_rows(now)
        stats["due"], stats["over_cap"] = len(due), over_cap
        self.update_level(now)
        stats["level"] = self.level
        if not due:
            self._record(stats, t0, now)
            return stats
        stream = self._open_subscriptions([str(r["mint"]) for r in due])
        cost = self.call_cost()
        for k in range(0, len(due), int(self.cfg.max_addresses_per_call)):
            if stats["calls"] >= int(self.cfg.max_calls_per_tick):
                break
            batch = due[k:k + int(self.cfg.max_addresses_per_call)]
            if self.share_cu > 0 and stats["cu_today"] + cost > self.share_cu:
                stats["paused"] = True
                stats["blocked"] += 1
                if self.budget is not None:
                    self.budget.block(self.source, METHOD, BLOCK_REASON_SHARE, retry_in_s=max(0.0, day0 + 86400.0 - now))
                if self._throttle.allow("share", now):
                    log.warning("follow.daily_share_spent", cu_today=round(stats["cu_today"]), share_cu=round(self.share_cu), note="polls paused until the next UTC day; the interval is already at its longest")
                break
            try:
                slot, values = self.rpc.get_multiple_accounts_base64([str(r["curve"]) for r in batch])
            except Blocked as e:
                stats["blocked"] += 1
                self.update_level(now, force_up=f"blocked:{e.reason}")
                log.info("follow.blocked", reason=e.reason, retry_in_s=round(e.retry_in_s, 1), addresses=len(batch))
                break
            except (HttpError, RpcError, ValueError) as e:
                stats["errors"] += 1
                log.warning("follow.call_failed", error=str(e)[:160], addresses=len(batch))
                db.write_tx(self.conn, lambda c, b=batch: c.execute(f"UPDATE followed SET errors=errors+1, updated_at=? WHERE mint IN ({','.join('?' for _ in b)})", (now, *[str(r['mint']) for r in b])), what="follow.errors")
                break
            stats["calls"] += 1
            stats["polled"] += len(batch)
            stats["cu_today"] += cost
            self._apply(batch, values, slot, now, stream, stats)
        self._record(stats, t0, now)
        return stats

    def _open_subscriptions(self, mints: list[str]) -> set[str]:
        if not table_exists(self.conn, "ws_subscriptions"):
            return set()
        out: set[str] = set()
        for chunk in rf.iter_chunks(mints, 400):
            marks = ",".join("?" for _ in chunk)
            for r in self.conn.execute(f"SELECT DISTINCT mint FROM ws_subscriptions WHERE until IS NULL AND mint IN ({marks})", chunk):
                out.add(str(r[0]))
        return out

    def _apply(self, batch: list[sqlite3.Row], values: list[Any], slot: int | None, now: float, stream: set[str], stats: dict[str, Any]) -> None:
        """Decode one call's answers and write the points and the row updates in one transaction."""
        points: list[tuple[Any, ...]] = []
        updates: list[tuple[Any, ...]] = []
        for row, value in zip(batch, values):
            mint = str(row["mint"])
            try:
                extra = json.loads(row["json"] or "{}")
            except ValueError:
                extra = {}
            polls = int(row["polls"] or 0) + 1
            errors, misses = int(row["errors"] or 0), int(row["misses"] or 0)
            state, closed_reason, graduated_at, changed_at = "active", row["closed_reason"], row["graduated_at"], row["changed_at"]
            last_point_t, last_source = row["last_point_t"], row["last_source"]
            if value is None:
                misses += 1
                stats["misses"] += 1
                if misses >= int(self.cfg.missing_closes_after):
                    state, closed_reason = "closed", "account_missing"
                    stats["closed"] += 1
            else:
                acct, info = decode_account_value(value)
                if acct is None or not acct.ok or not acct.quote_is_sol:
                    errors += 1
                    stats["errors"] += 1
                    why = (info.get("error") if acct is None else (acct.error or ("quote mint is not SOL" if not acct.quote_is_sol else "undecodable"))) or "undecodable"
                    if self._throttle.allow(f"decode:{why[:40]}", now):
                        log.warning("follow.undecodable", mint=mint, error=str(why)[:120], raw_len=None if acct is None else acct.raw_len)
                    if errors >= int(self.cfg.missing_closes_after):
                        state, closed_reason = "closed", f"undecodable: {str(why)[:60]}"
                        stats["closed"] += 1
                else:
                    misses = 0
                    f = acct.fields
                    if acct.complete:
                        # the graduation seen by the poll: the reserves are zero now, the time is what the point carries
                        points.append((mint, now, "poll", acct.virtual_sol, acct.virtual_token, acct.real_sol, acct.real_token, 1, slot))
                        state = "graduated"
                        graduated_at = graduated_at or now
                        stats["graduated"] += 1
                    else:
                        points.append((mint, now, "poll", acct.virtual_sol, acct.virtual_token, acct.real_sol, acct.real_token, 0, slot))
                        if extra.get("vs") != acct.virtual_sol or extra.get("vt") != acct.virtual_token or changed_at is None:
                            changed_at = now
                        extra["vs"], extra["vt"] = acct.virtual_sol, acct.virtual_token
                    stats["points"] += 1
                    last_point_t, last_source = now, "poll"
            interval = self.interval_for(row, now, mint in stream)
            next_at = now + interval if state == "active" else None
            updates.append((state, next_at, interval, now, polls, errors, misses, changed_at, last_point_t, last_source, graduated_at, closed_reason, now, json.dumps(extra), mint))

        def _write(c: sqlite3.Connection) -> None:
            for p in points:
                c.execute("INSERT OR REPLACE INTO path_points(mint, t, source, virtual_sol, virtual_token, real_sol, real_token, complete, slot) VALUES(?,?,?,?,?,?,?,?,?)", p)
            for u in updates:
                c.execute("UPDATE followed SET state=?, next_poll_at=?, interval_s=?, last_poll_at=?, polls=?, errors=?, misses=?, changed_at=?, last_point_t=?, last_source=?,"
                          " graduated_at=?, closed_reason=?, updated_at=?, json=? WHERE mint=?", u)

        db.write_tx(self.conn, _write, what="follow.points")

    def _record(self, stats: dict[str, Any], t0: float, now: float) -> None:
        stats["seconds"] = round(time.perf_counter() - t0, 3)
        self.last_stats = stats

        def _kv(c: sqlite3.Connection) -> None:
            db.kv_set(c, KV_LAST, json.dumps(stats, default=str), now)
            for key, n in ((KV_POLLS, stats["polled"]), (KV_CALLS, stats["calls"]), (KV_POINTS, stats["points"]), (KV_ERRORS, stats["errors"]),
                           (KV_BLOCKED, stats["blocked"]), (KV_MISSES, stats["misses"])):
                if n:
                    db.counter_bump_daily(c, key, int(n), now)

        try:
            db.write_tx(self.conn, _kv, what="follow.kv")
        except sqlite3.OperationalError:
            log.warning("follow.kv_not_written")


# ----- status -----------------------------------------------------------------------------------------------------------------
def last_point(conn: sqlite3.Connection, now: float) -> dict[str, Any] | None:
    """The newest price point of any followed coin, with its source (trade / poll / sample / candle): what `fart status` calls
    "bron van het laatste punt"."""
    if not table_exists(conn):
        return None
    cands: list[tuple[float, str]] = []
    r = conn.execute("SELECT MAX(t) FROM path_points WHERE source='poll'").fetchone()
    if r and r[0] is not None:
        cands.append((float(r[0]), "poll"))
    r = conn.execute("SELECT MAX(received_at) FROM trades WHERE mint IN (SELECT mint FROM followed WHERE state IN ('active', 'graduated'))").fetchone()
    if r and r[0] is not None:
        cands.append((float(r[0]), "trade"))
    r = conn.execute("SELECT MAX(ts) FROM mcap_samples WHERE mint IN (SELECT mint FROM followed WHERE state IN ('active', 'graduated'))").fetchone()
    if r and r[0] is not None:
        cands.append((float(r[0]), "sample"))
    if table_exists(conn, "pool_candles"):
        r = conn.execute("SELECT MAX(minute) FROM pool_candles WHERE pool IN (SELECT pool_address FROM migrations WHERE mint IN (SELECT mint FROM followed WHERE state='graduated'))").fetchone()
        if r and r[0] is not None:
            cands.append((float(r[0]) + 60.0, "candle"))
    if not cands:
        return None
    t, src = max(cands)
    return {"t": t, "source": src, "age_s": round(max(0.0, now - t))}


def status(conn: sqlite3.Connection, settings: Settings, now: float) -> dict[str, Any]:
    """For `fart status` (the `follow:` line) and the owner page."""
    cfg = FollowConfig.from_settings(settings)
    out: dict[str, Any] = {"exists": table_exists(conn), "enabled": cfg.enabled, "config": {"max_followed": cfg.max_followed, "daily_cu_share": cfg.daily_cu_share, "follow_s": cfg.follow_s,
                                                                                        "fast_interval_s": cfg.fast_interval_s, "slow_interval_s": cfg.slow_interval_s,
                                                                                        "degraded_intervals_s": list(cfg.degraded_intervals_s)}}
    if not out["exists"]:
        return out
    by = {str(r[0]): int(r[1]) for r in conn.execute("SELECT state, COUNT(*) FROM followed GROUP BY state")}
    out["by_state"] = by
    out["total"] = sum(by.values())
    out["active"] = by.get("active", 0)
    day = day_key(now)
    for key, name in ((KV_POLLS, "polls_today"), (KV_CALLS, "calls_today"), (KV_POINTS, "points_today"), (KV_ERRORS, "errors_today"), (KV_BLOCKED, "blocked_today"), (KV_MISSES, "misses_today")):
        out[name] = int(db.kv_get(conn, f"{key}.{day}", "0") or 0)
    source = "rpc_public" if settings.rpc_is_public else "rpc"
    day0 = now - (now % 86400.0)
    out["cu_today"] = float(conn.execute("SELECT COALESCE(SUM(cost), 0) FROM api_ledger WHERE source=? AND method=? AND blocked_reason IS NULL AND ts >= ?", (source, METHOD, day0)).fetchone()[0])
    out["share_cu"] = cfg.daily_cu_share * float(settings.rpc_daily_budget_cu)
    out["level"] = max(0, min(cfg.max_level, int(db.kv_get(conn, KV_LEVEL, "0") or 0)))
    out["interval_s"] = float(cfg.degraded_intervals_s[out["level"]])
    out["due_now"] = int(conn.execute("SELECT COUNT(*) FROM followed WHERE state='active' AND (next_poll_at IS NULL OR next_poll_at <= ?)", (now,)).fetchone()[0])
    out["over_cap"] = max(0, out["active"] - cfg.max_followed)
    raw = db.kv_get(conn, KV_LAST)
    try:
        out["last"] = json.loads(raw) if raw else None
    except ValueError:
        out["last"] = None
    out["last_age_s"] = None if not isinstance(out["last"], dict) or out["last"].get("at") is None else round(max(0.0, now - float(out["last"]["at"])))
    out["disabled_reason"] = (out["last"] or {}).get("disabled_reason") if isinstance(out["last"], dict) else None
    out["paused"] = bool((out["last"] or {}).get("paused")) if isinstance(out["last"], dict) else False
    started = db.kv_get(conn, KV_STARTED)
    out["started_at"] = None if started is None else float(started)
    out["last_point"] = last_point(conn, now)
    out["points_total"] = int(conn.execute("SELECT COUNT(*) FROM path_points").fetchone()[0])
    out["reasons"] = {}
    for r in conn.execute("SELECT reason FROM followed WHERE state IN ('active', 'graduated')"):
        for rs in str(r[0] or "").split(","):
            key = rs.split(":")[0] if rs.startswith("play:") else rs
            if key:
                out["reasons"][key] = out["reasons"].get(key, 0) + 1
    return out


def status_line(st: dict[str, Any]) -> str:
    """`follow: 1,234 followed (active 1,100, graduated 120, done 14, closed 0; early_buyers 900, curve60 ...); polls today 12,345 addresses in
    234 calls = 4,680 CU of the 300,000 share; level 0 (60 s); due now 12, over cap 0; last point via poll 12 s ago; errors today 0, blocked 0`."""
    if not st.get("exists"):
        return "follow: no followed table yet (schema v10: start the plays daemon once)"
    by = st.get("by_state") or {}
    reasons = ", ".join(f"{k} {v:,}" for k, v in sorted((st.get("reasons") or {}).items(), key=lambda kv: -kv[1]))
    lp = st.get("last_point")
    last_point_txt = "no point yet" if not lp else f"last point via {lp['source']} {lp['age_s']} s ago"
    level_txt = f"level {st.get('level', 0)} ({st.get('interval_s', 60):.0f} s fast interval)" + (" PAUSED: daily share spent" if st.get("paused") else "")
    head = (f"follow: {st.get('total', 0):,} followed (" + ", ".join(f"{k} {by.get(k, 0):,}" for k in STATES) + (f"; {reasons}" if reasons else "") + ")")
    if st.get("disabled_reason"):
        return head + f"; polls DISABLED: {st['disabled_reason']}; {last_point_txt}"
    return (head + f"; polls today {st.get('polls_today', 0):,} addresses in {st.get('calls_today', 0):,} calls = {st.get('cu_today', 0):,.0f} CU of the {st.get('share_cu', 0):,.0f} share"
            f" ({st.get('points_today', 0):,} points); {level_txt}; due now {st.get('due_now', 0):,}, over cap {st.get('over_cap', 0):,}; {last_point_txt}; "
            f"errors today {st.get('errors_today', 0)}, blocked {st.get('blocked_today', 0)}, misses {st.get('misses_today', 0)}"
            + (f"; last tick {st['last_age_s']} s ago" if st.get("last_age_s") is not None else "; never ticked"))


__all__ = ["FollowConfig", "Follower", "status", "status_line", "last_point", "METHOD", "KV_STARTED", "KV_LAST", "BLOCK_REASON_SHARE"]
