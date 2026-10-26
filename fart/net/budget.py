"""The ONE gate for every external call.

Per source: token-bucket rate gate (persisted in SQLite, shared between processes via a file
lock), daily budget (requests and cost units; the RPC cost is estimated compute units from a
configurable table), health row (last_ok/last_err/counts/latency) and explicit backoff on
429/5xx honoring Retry-After. A blocked call is written to api_ledger with a reason and
logged; it is never silently skipped.
"""
from __future__ import annotations

import fcntl
import math
import os
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from ..log import Throttle, get_logger
from ..util import day_start, now as _now

log = get_logger("budget")


class Blocked(Exception):
    """Raised by callers that cannot proceed; carries the reason and a retry hint."""

    def __init__(self, source: str, method: str, reason: str, retry_in_s: float):
        super().__init__(f"{source}.{method} blocked: {reason} (retry in {retry_in_s:.1f}s)")
        self.source = source
        self.method = method
        self.reason = reason
        self.retry_in_s = retry_in_s


@dataclass(frozen=True)
class SourceConfig:
    name: str
    rate_per_s: float
    burst: float
    daily_requests: int | None
    daily_cost: float | None
    cost_default: float
    cost_table: dict[str, float]
    backoff_base_s: float
    backoff_max_s: float
    cu_per_byte: float | None = None            # milestone 4: websocket frames are priced per byte
    byte_priced: frozenset[str] = frozenset()   # the methods booked as bytes x cu_per_byte

    def cost_of(self, method: str, override: float | None = None) -> float:
        """Cost of one call from the table (byte-priced methods have no per-call cost: 0)."""
        if override is not None:
            return float(override)
        if self.is_byte_priced(method):
            return 0.0
        return float(self.cost_table.get(method, self.cost_default))

    def is_byte_priced(self, method: str) -> bool:
        return self.cu_per_byte is not None and method in self.byte_priced

    def cost_of_frames(self, method: str, nbytes: int, n: int) -> float:
        """Cost of `n` websocket frames totalling `nbytes`: bytes x cu_per_byte when the method is
        byte-priced, else n x the table cost (the milestone-3 per-notification estimate)."""
        if self.is_byte_priced(method):
            return float(nbytes) * float(self.cu_per_byte)
        return float(self.cost_table.get(method, self.cost_default)) * float(n)


@dataclass(frozen=True)
class Verdict:
    allowed: bool
    reason: str | None = None
    retry_in_s: float = 0.0
    cost: float = 0.0


def _resolve_daily_cost(value: Any, env: dict[str, str]) -> float | None:
    if value is None:
        return None
    if isinstance(value, str) and value.startswith("env:"):
        raw = env.get(value[4:], "")
        try:
            return float(raw) if raw else None
        except ValueError:
            return None
    return float(value)


def load_source_configs(budget_cfg: dict[str, Any], env: dict[str, str] | None = None) -> dict[str, SourceConfig]:
    env = dict(os.environ if env is None else env)
    out: dict[str, SourceConfig] = {}
    for name, c in budget_cfg["sources"].items():
        out[name] = SourceConfig(
            name=name,
            rate_per_s=float(c["rate_per_s"]),
            burst=float(c["burst"]),
            daily_requests=(int(c["daily_requests"]) if c.get("daily_requests") is not None else None),
            daily_cost=_resolve_daily_cost(c.get("daily_cost"), env),
            cost_default=float(c.get("cost_default", 1)),
            cost_table={k: float(v) for k, v in (c.get("cost_table") or {}).items()},
            backoff_base_s=float(c.get("backoff_base_s", 2)),
            backoff_max_s=float(c.get("backoff_max_s", 300)),
            cu_per_byte=(float(c["cu_per_byte"]) if c.get("cu_per_byte") is not None else None),
            byte_priced=frozenset(str(m) for m in (c.get("byte_priced") or [])),
        )
    return out


class Budget:
    """Shared by every process; cheap to construct. `clock` is injectable for tests."""

    def __init__(
        self,
        conn: sqlite3.Connection,
        sources: dict[str, SourceConfig],
        lock_path: Path | str,
        clock: Callable[[], float] = _now,
        warning_period_s: float = 3600.0,
    ):
        self.conn = conn
        self.sources = sources
        self.lock_path = Path(lock_path)
        self.clock = clock
        self._throttle = Throttle(warning_period_s)

    # ----- construction helpers -------------------------------------------------
    @classmethod
    def from_settings(cls, conn: sqlite3.Connection, settings, clock: Callable[[], float] = _now) -> "Budget":
        cfg = settings.config("budget")
        env = {"RPC_DAILY_BUDGET_CU": str(settings.rpc_daily_budget_cu),
               "X_DAILY_BUDGET_USD": str(getattr(settings, "x_daily_budget_usd", 3.0))}
        srcs = load_source_configs(cfg, env)
        return cls(conn, srcs, settings.lock_path, clock=clock,
                   warning_period_s=float(cfg.get("blocked_warning_period_s", 3600)))

    # ----- locking --------------------------------------------------------------
    @contextmanager
    def _lock(self):
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(self.lock_path), os.O_RDWR | os.O_CREAT, 0o640)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    # ----- public API -------------------------------------------------------------
    def source(self, name: str) -> SourceConfig:
        try:
            return self.sources[name]
        except KeyError:
            raise KeyError(f"unknown budget source {name!r}; add it to config/budget.json") from None

    def peek(self, source: str, method: str = "*", cost: float | None = None) -> Verdict:
        """Would a call be allowed right now? Does not consume a token or write anything."""
        return self._evaluate(self.source(source), method, cost, consume=False)

    def acquire(self, source: str, method: str, cost: float | None = None) -> Verdict:
        """Consume one rate token if allowed. Blocked verdicts are ledgered and logged."""
        sc = self.source(source)
        with self._lock():
            v = self._evaluate(sc, method, cost, consume=True)
        if not v.allowed:
            self._record_blocked(sc, method, v)
        return v

    def acquire_or_raise(self, source: str, method: str, cost: float | None = None) -> Verdict:
        v = self.acquire(source, method, cost)
        if not v.allowed:
            raise Blocked(source, method, v.reason or "blocked", v.retry_in_s)
        return v

    def block(self, source: str, method: str, reason: str, retry_in_s: float = 0.0) -> Verdict:
        """Record a call refused by a caller-side policy (e.g. the X hourly envelope) exactly like a
        gate refusal: ledger row with blocked_reason, health counter, log line."""
        v = Verdict(False, reason, retry_in_s, 0.0)
        self._record_blocked(self.source(source), method, v)
        return v

    def record(
        self,
        source: str,
        method: str,
        cost: float,
        ok: bool,
        status: int | None,
        latency_ms: float | None,
        err: str | None = None,
        retry_after_s: float | None = None,
        nbytes: int | None = None,
    ) -> None:
        """Record the result of a call that was previously acquired. `nbytes` is the byte count a
        per-byte priced row (websocket frames) was booked for; it is shown next to the CU in `fart status`."""
        sc = self.source(source)
        t = self.clock()
        with self._lock():
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                self.conn.execute(
                    "INSERT INTO api_ledger(ts, source, method, cost, ok, status, latency_ms, bytes) VALUES(?,?,?,?,?,?,?,?)",
                    (t, source, method, float(cost), 1 if ok else 0, status, latency_ms, None if nbytes is None else int(nbytes)),
                )
                h = self._health_row(source)
                if ok:
                    ewma = latency_ms if h["latency_ms_ewma"] is None or latency_ms is None else (
                        0.8 * h["latency_ms_ewma"] + 0.2 * latency_ms
                    )
                    self.conn.execute(
                        "UPDATE health SET last_ok=?, ok_count=ok_count+1, consecutive_err=0, backoff_until=NULL,"
                        " latency_ms_ewma=?, updated_at=? WHERE source=?",
                        (t, ewma, t, source),
                    )
                else:
                    consecutive = int(h["consecutive_err"]) + 1
                    backoff_until = h["backoff_until"]
                    if status is None or status == 429 or status >= 500:  # network error, throttled, server error
                        wait = min(sc.backoff_max_s, sc.backoff_base_s * (2 ** (consecutive - 1)))
                        if retry_after_s is not None:
                            wait = max(wait, min(float(retry_after_s), sc.backoff_max_s * 4))
                        backoff_until = t + wait
                    self.conn.execute(
                        "UPDATE health SET last_err=?, last_err_msg=?, err_count=err_count+1, consecutive_err=?,"
                        " backoff_until=?, updated_at=? WHERE source=?",
                        (t, (err or "")[:300], consecutive, backoff_until, t, source),
                    )
                self.conn.execute("COMMIT")
            except Exception:
                self.conn.execute("ROLLBACK")
                raise
        if not ok:
            log.warning("api.error", source=source, method=method, status=status, err=(err or "")[:200])

    def daily_usage(self, source: str, ts: float | None = None) -> dict[str, float]:
        t = self.clock() if ts is None else ts
        ds = day_start(t)
        row = self.conn.execute(
            "SELECT COUNT(*) FILTER (WHERE blocked_reason IS NULL) AS requests,"
            " COALESCE(SUM(cost) FILTER (WHERE blocked_reason IS NULL), 0) AS cost,"
            " COUNT(*) FILTER (WHERE blocked_reason IS NOT NULL) AS blocked,"
            " COUNT(*) FILTER (WHERE ok = 0) AS errors"
            " FROM api_ledger WHERE source=? AND ts>=? AND ts<?",
            (source, ds, ds + 86400.0),
        ).fetchone()
        sc = self.source(source)
        return {
            "requests": int(row["requests"]),
            "cost": float(row["cost"]),
            "blocked": int(row["blocked"]),
            "errors": int(row["errors"]),
            "daily_requests_cap": sc.daily_requests,
            "daily_cost_cap": sc.daily_cost,
        }

    def health(self) -> list[dict[str, Any]]:
        rows = self.conn.execute("SELECT * FROM health ORDER BY source").fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["usage_today"] = self.daily_usage(d["source"])
            out.append(d)
        return out

    def snapshot(self) -> dict[str, Any]:
        """For `fart status`: every configured source, even untouched ones."""
        health = {h["source"]: h for h in self.health()}
        out: dict[str, Any] = {}
        t = self.clock()
        for name, sc in self.sources.items():
            h = health.get(name) or {"usage_today": self.daily_usage(name)}
            bucket = self.conn.execute("SELECT tokens, updated_at FROM rate_buckets WHERE source=?", (name,)).fetchone()
            tokens = sc.burst if bucket is None else min(sc.burst, bucket["tokens"] + (t - bucket["updated_at"]) * sc.rate_per_s)
            out[name] = {
                "tokens_available": round(tokens, 2),
                "burst": sc.burst,
                "rate_per_s": sc.rate_per_s,
                "backoff_until": h.get("backoff_until"),
                "last_ok": h.get("last_ok"),
                "last_err": h.get("last_err"),
                "last_err_msg": h.get("last_err_msg"),
                "ok_count": h.get("ok_count", 0),
                "err_count": h.get("err_count", 0),
                "blocked_count": h.get("blocked_count", 0),
                "latency_ms_ewma": h.get("latency_ms_ewma"),
                "usage_today": h["usage_today"],
            }
        return out

    # ----- internals ----------------------------------------------------------------
    def _health_row(self, source: str) -> sqlite3.Row:
        row = self.conn.execute("SELECT * FROM health WHERE source=?", (source,)).fetchone()
        if row is None:
            self.conn.execute("INSERT INTO health(source, updated_at) VALUES(?, ?)", (source, self.clock()))
            row = self.conn.execute("SELECT * FROM health WHERE source=?", (source,)).fetchone()
        return row

    def _evaluate(self, sc: SourceConfig, method: str, cost: float | None, consume: bool) -> Verdict:
        t = self.clock()
        c = sc.cost_of(method, cost)
        in_tx = False
        if consume:
            self.conn.execute("BEGIN IMMEDIATE")
            in_tx = True
        try:
            h = self._health_row(sc.name) if consume else self.conn.execute(
                "SELECT * FROM health WHERE source=?", (sc.name,)
            ).fetchone()
            backoff_until = h["backoff_until"] if h is not None else None
            if backoff_until is not None and backoff_until > t:
                return self._finish(in_tx, Verdict(False, "backoff", backoff_until - t, c))

            usage = self.daily_usage(sc.name, t)
            next_day_in = day_start(t) + 86400.0 - t
            if sc.daily_requests is not None and usage["requests"] + 1 > sc.daily_requests:
                return self._finish(in_tx, Verdict(False, "daily_requests_cap", next_day_in, c))
            if sc.daily_cost is not None and usage["cost"] + c > sc.daily_cost:
                return self._finish(in_tx, Verdict(False, "daily_cost_cap", next_day_in, c))

            row = self.conn.execute("SELECT tokens, updated_at FROM rate_buckets WHERE source=?", (sc.name,)).fetchone()
            if row is None:
                tokens, updated = sc.burst, t
            else:
                tokens = min(sc.burst, float(row["tokens"]) + max(0.0, t - float(row["updated_at"])) * sc.rate_per_s)
                updated = t
            if tokens < 1.0:
                retry_in = (1.0 - tokens) / sc.rate_per_s if sc.rate_per_s > 0 else math.inf
                if consume:
                    self.conn.execute(
                        "INSERT INTO rate_buckets(source, tokens, updated_at) VALUES(?,?,?)"
                        " ON CONFLICT(source) DO UPDATE SET tokens=excluded.tokens, updated_at=excluded.updated_at",
                        (sc.name, tokens, updated),
                    )
                return self._finish(in_tx, Verdict(False, "rate_limit", retry_in, c))
            if consume:
                self.conn.execute(
                    "INSERT INTO rate_buckets(source, tokens, updated_at) VALUES(?,?,?)"
                    " ON CONFLICT(source) DO UPDATE SET tokens=excluded.tokens, updated_at=excluded.updated_at",
                    (sc.name, tokens - 1.0, updated),
                )
            return self._finish(in_tx, Verdict(True, None, 0.0, c))
        except Exception:
            if in_tx:
                self.conn.execute("ROLLBACK")
            raise

    def _finish(self, in_tx: bool, v: Verdict) -> Verdict:
        if in_tx:
            self.conn.execute("COMMIT")
        return v

    def _record_blocked(self, sc: SourceConfig, method: str, v: Verdict) -> None:
        t = self.clock()
        with self._lock():
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                self.conn.execute(
                    "INSERT INTO api_ledger(ts, source, method, cost, ok, status, latency_ms, blocked_reason)"
                    " VALUES(?,?,?,?,NULL,NULL,NULL,?)",
                    (t, sc.name, method, 0.0, v.reason),
                )
                self._health_row(sc.name)
                self.conn.execute(
                    "UPDATE health SET blocked_count=blocked_count+1, updated_at=? WHERE source=?", (t, sc.name)
                )
                self.conn.execute("COMMIT")
            except Exception:
                self.conn.execute("ROLLBACK")
                raise
        fields = dict(source=sc.name, method=method, reason=v.reason, retry_in_s=round(v.retry_in_s, 1))
        log.info("api.blocked", **fields)
        if v.reason in ("daily_cost_cap", "daily_requests_cap") and self._throttle.allow(f"{sc.name}:{v.reason}", t):
            log.warning("api.budget_cap_reached", **fields, note="degrading: calls to this source blocked until the next UTC day")
