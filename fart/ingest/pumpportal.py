"""PumpPortal public data websocket: ONE connection, watchdog, bounded reconnects.

Subscriptions: subscribeNewToken, subscribeMigration always; subscribeTokenTrade for the active
watchlist only when the connection carries a funded API key (`PUMPPORTAL_API_KEY`): PumpPortal
answers every unkeyed subscribeTokenTrade with "only available when connecting with an API key
funded with at least some balance" and never sends a trade (VPS, 26 Oct 2026). Without a key the
client is creates + migrations only and trades come from the RPC (`rpc_trades.py`). Every raw
message is written to the hourly gzip store before normalization. Connects go through the budget
gate (max 12 per hour). The connection URL may carry the key and is never logged.

Milestone 3: a migration message is never accepted for a system mint (USDC, PUMP, ...: on 26 Oct
2026 PumpPortal put those in the `mint` field and they ended up on the watchlist); a refused message
lands in `rejected_messages`. Every write is one short `BEGIN IMMEDIATE` transaction with retries; a
handler that still fails is counted (`ingest.handle_failed.pumpportal`) and its raw hour file can be
replayed (`fart ingest replay`).

Milestone 4: a migration of a coin we never saw created (created before fart started, e.g.
Cv934yS1kxVtW3Y6vS8jhk6APKpdtWbzpMcs6VHHpump on 26 Oct 2026) is stored in `migrations` with
`coin_unknown=1` instead of being refused, so the universe of the daily report stays complete
(`grads_without_create`); such a coin is never put on the watchlist.
"""
from __future__ import annotations

import asyncio
import json
import random
import re
import sqlite3
import time
from typing import Any, Callable

from .. import db
from ..db import kv_set
from ..log import Throttle, get_logger
from ..net.budget import Budget
from ..util import now as wall_now
from . import coverage, store, watch
from .raw import RawWriter

log = get_logger("pumpportal")

SOURCE = "pumpportal"
_REFUSAL_RE = re.compile(r"api[ -]?key", re.IGNORECASE)
_HTTP_STATUS_RE = re.compile(r"\b(401|403|429)\b")


def http_status_of(exc: BaseException) -> int | None:
    """Best effort: the HTTP status behind a websocket handshake failure (403/429 from PumpPortal)."""
    for obj in (exc, getattr(exc, "response", None)):
        v = getattr(obj, "status_code", None)
        if isinstance(v, int):
            return v
    m = _HTTP_STATUS_RE.search(str(exc))
    return int(m.group(1)) if m else None


class PumpPortalClient:
    def __init__(
        self,
        conn: sqlite3.Connection,
        budget: Budget,
        raw: RawWriter,
        url: str,
        cfg: dict[str, Any],
        watch_cfg: dict[str, Any],
        connect: Callable[..., Any] | None = None,
        clock: Callable[[], float] = wall_now,
        trades_enabled: bool = False,
        pumpfun_cfg: dict[str, Any] | None = None,
    ):
        self.conn = conn
        self.budget = budget
        self.raw = raw
        self.url = url
        self.cfg = cfg
        self.watch_cfg = watch_cfg
        self.pf = pumpfun_cfg or {}
        self.clock = clock
        self.trades_enabled = trades_enabled
        self.replay = False  # `fart ingest replay`: data writes only (no raw copy, no counters, no kv)
        self.failures = 0
        self._pending_failures = 0
        self._throttle = Throttle(3600)
        if connect is None:
            import websockets  # imported lazily so tests can run without the package

            connect = websockets.connect
        self._connect = connect
        self.subscribed: set[str] = set()
        self.connects = 0
        self.failures = 0
        self.messages = 0
        self.counts: dict[str, int] = {}
        self._gap_id: int | None = None
        self._connected = False

    # ----- message handling (sync, testable) -------------------------------------------------
    def handle_text(self, text: str, received_at: float) -> str:
        try:
            msg = json.loads(text)
        except json.JSONDecodeError:
            msg = None
        ref = None if self.replay else self.raw.write(msg if msg is not None else text, received_at)
        kind = store.classify_pumpportal(msg) if msg is not None else "unparseable"
        self.counts[kind] = self.counts.get(kind, 0) + 1
        self.messages += 1
        cap, reserve = watch.limits(self.watch_cfg)
        notes: dict[str, Any] = {}

        def _apply(c: sqlite3.Connection) -> None:
            notes.clear()
            if kind == "create":
                rec = store.pumpportal_create_record(msg)
                new = store.upsert_coin(c, rec, SOURCE, received_at, raw_ref=ref, pumpfun_cfg=self.pf)
                if new and rec.get("mint") and (rec.get("platform") or "pump") == "pump" and self.watch_cfg.get("probation", True):
                    watch.add(c, rec["mint"], "probation", received_at, float(self.watch_cfg.get("early_window_s", 300)), cap, reserve)
            elif kind == "trade":
                rec = store.pumpportal_trade_record(msg)
                if rec.get("mint") and watch.is_watched(c, rec["mint"]):
                    store.insert_trade(c, rec, SOURCE, received_at)
                else:
                    notes["trade_unwatched"] = 1
            elif kind == "migrate":
                rec = store.pumpportal_migration_record(msg)
                why = store.migration_rejection(c, rec.get("mint"), self.pf)
                if why is not None:
                    notes["rejected"] = why
                    if not self.replay:
                        store.reject_message(c, SOURCE, "migrate", why, rec.get("mint"), msg, received_at, ref)
                else:
                    known = store.coin_known(c, rec["mint"])
                    store.upsert_migration(c, rec, SOURCE, received_at)  # coin_unknown=1 while the create is missing
                    if not known:
                        notes["coin_unknown"] = 1
                        if not self.replay:
                            store.counter_bump(c, "pp.migration_coin_unknown", 1, received_at)
                    # only a funded key follows graduated coins (subscribe_graduations); never an unknown coin
                    elif self.watch_cfg.get("subscribe_graduations", False):
                        watch.add(c, rec["mint"], "graduation", received_at, float(self.watch_cfg.get("ttl_s", 86400)), cap, reserve)
            elif kind == "info":
                text_l = str(msg)
                if _REFUSAL_RE.search(text_l) and "trade" in text_l.lower():
                    # the funded-key refusal of subscribeTokenTrade; without a key we never send it
                    notes["refusal"] = text_l[:200]
                    if not self.replay:
                        store.counter_bump(c, "pp.trade_sub_refused", 1, received_at)
                        kv_set(c, "ingest.pumpportal.trade_sub_refused_at", str(received_at), received_at)
                else:
                    notes["info"] = text_l[:200]
            elif not self.replay:
                store.counter_bump(c, "pp.unknown_messages", 1, received_at)
            if not self.replay:
                kv_set(c, "ingest.pumpportal.last_msg_at", str(received_at), received_at)
                if self._pending_failures:
                    db.counter_bump(c, f"ingest.handle_failed.{SOURCE}", self._pending_failures, received_at)
                    db.counter_bump(c, f"ingest.handle_failed.{SOURCE}.{db.day_of(received_at)}", self._pending_failures, received_at)

        try:
            db.write_tx(self.conn, _apply, what="pumpportal.handle")
        except sqlite3.OperationalError as e:
            self._handle_failed(received_at, ref, e)
            raise
        self._pending_failures = 0
        if "trade_unwatched" in notes:
            self.counts["trade_unwatched"] = self.counts.get("trade_unwatched", 0) + 1
        if "rejected" in notes:
            self.counts["migrate_rejected"] = self.counts.get("migrate_rejected", 0) + 1
            if self._throttle.allow(f"rejected:{notes['rejected']}", received_at):
                log.warning("pp.migration_rejected", reason=notes["rejected"], mint=(msg or {}).get("mint"), raw_ref=ref,
                            note="stored in rejected_messages; see docs/PUMPFUN-EVENTS.md")
        if "coin_unknown" in notes:
            self.counts["migrate_coin_unknown"] = self.counts.get("migrate_coin_unknown", 0) + 1
            if self._throttle.allow("coin_unknown", received_at):
                log.info("pp.migration_coin_unknown", mint=(msg or {}).get("mint"), raw_ref=ref,
                         note="graduation of a coin created before fart saw it: stored with coin_unknown=1, never watched")
        if "refusal" in notes and self._throttle.allow("refusal", received_at):
            log.warning("pp.trade_subscription_refused", trades_enabled=self.trades_enabled, text=notes["refusal"])
        if "info" in notes:
            log.info("pp.info", text=notes["info"])
        return kind

    def _handle_failed(self, received_at: float, ref: str | None, exc: BaseException) -> None:
        self.failures += 1
        self._pending_failures += 1
        detail = f"{ref or '-'} {type(exc).__name__}: {str(exc)[:120]}"
        if db.record_failure(self.conn, f"ingest.handle_failed.{SOURCE}", received_at, self._pending_failures, detail=detail):
            self._pending_failures = 0

    def desired_subscriptions(self, now: float) -> set[str]:
        watch.expire(self.conn, now)
        cap = int(self.watch_cfg.get("cap", 200))
        return set(watch.active_mints(self.conn)[:cap])

    def subscription_messages(self, now: float) -> list[dict[str, Any]]:
        """Diff the active watchlist against what the connection is subscribed to. Without a funded
        API key no trade subscription is ever sent (PumpPortal would refuse it anyway)."""
        if not self.trades_enabled:
            self.subscribed = set()
            return []
        desired = self.desired_subscriptions(now)
        msgs: list[dict[str, Any]] = []
        add = sorted(desired - self.subscribed)
        rem = sorted(self.subscribed - desired)
        if add:
            msgs.append({"method": "subscribeTokenTrade", "keys": add})
        if rem:
            msgs.append({"method": "unsubscribeTokenTrade", "keys": rem})
        self.subscribed = desired
        return msgs

    # ----- connection loop -------------------------------------------------------------------
    async def run(self, stop: asyncio.Event) -> None:
        t0 = self.clock()
        db.kv_setdefault(self.conn, "ingest.pumpportal.first_started", str(t0), t0)
        kv_set(self.conn, "ingest.pumpportal.started", str(t0), t0)
        self._open_gap(t0, "startup")
        backoff_min = float(self.cfg.get("backoff_min_s", 20))
        backoff_max = float(self.cfg.get("backoff_max_s", 180))
        attempt = 0
        while not stop.is_set():
            verdict = self.budget.acquire(SOURCE, "connect")
            if not verdict.allowed:
                wait = min(max(verdict.retry_in_s, 5.0), 300.0)
                log.warning("pp.connect_gated", reason=verdict.reason, wait_s=round(wait, 1))
                await self._sleep(stop, wait)
                continue
            t0 = time.monotonic()
            self._connected = False
            try:
                await self._session(stop)
                self.budget.record(SOURCE, "connect", verdict.cost, True, None, (time.monotonic() - t0) * 1000.0)
                attempt = 0
            except asyncio.CancelledError:
                raise
            except Exception as e:  # any transport/protocol error: reconnect with backoff
                self.failures += 1
                # a session that connected and later dropped is a successful connect (our own backoff
                # applies); a failed handshake is an error and also arms the budget backoff
                self.budget.record(SOURCE, "connect", verdict.cost, self._connected, None, (time.monotonic() - t0) * 1000.0,
                                   err=None if self._connected else f"{type(e).__name__}: {e}")
                status = http_status_of(e) if not self._connected else None
                if status in (401, 403, 429):
                    # the shared VPS IP was refused; recorded for `fart status` and the health alerts
                    t_now = self.clock()
                    kv_set(self.conn, "ingest.pumpportal.last_http_status", str(status), t_now)
                    kv_set(self.conn, "ingest.pumpportal.last_http_status_at", str(t_now), t_now)
                    store.counter_bump(self.conn, "pp.http_refusals", 1, t_now)
                log.warning("pp.disconnected", connected=self._connected, http_status=status, error=f"{type(e).__name__}: {str(e)[:200]}")
                if self._connected and time.monotonic() - t0 > 60:  # a stable session resets the backoff ladder
                    attempt = 0
            if stop.is_set():
                break
            self._open_gap(self.clock(), "disconnect")
            attempt += 1
            wait = min(backoff_max, backoff_min * (2 ** (attempt - 1))) * random.uniform(0.8, 1.2)
            log.info("pp.backoff", attempt=attempt, wait_s=round(wait, 1))
            await self._sleep(stop, wait)
        self._open_gap(self.clock(), "shutdown")
        self.raw.flush()

    async def _session(self, stop: asyncio.Event) -> None:
        idle = float(self.cfg.get("watchdog_idle_s", 120))
        sync_every = float(self.cfg.get("watchlist_sync_interval_s", 5))
        async with self._connect(self.url, ping_interval=20, ping_timeout=20, max_size=4 * 1024 * 1024,
                                 open_timeout=15, close_timeout=5) as ws:
            self.connects += 1
            self._connected = True
            now = self.clock()
            log.info("pp.connected", connects=self.connects, trades="api_key" if self.trades_enabled else "disabled (no PUMPPORTAL_API_KEY; trades via RPC)")
            self._close_gap(now)
            await ws.send(json.dumps({"method": "subscribeNewToken"}))
            await ws.send(json.dumps({"method": "subscribeMigration"}))
            self.subscribed = set()
            for m in self.subscription_messages(now):
                await ws.send(json.dumps(m))
            last_sync = time.monotonic()
            last_msg = time.monotonic()
            while not stop.is_set():
                try:
                    text = await asyncio.wait_for(ws.recv(), timeout=1.0)
                except asyncio.TimeoutError:
                    text = None
                if text is not None:
                    last_msg = time.monotonic()
                    if isinstance(text, bytes):
                        text = text.decode("utf-8", errors="replace")
                    try:
                        self.handle_text(text, self.clock())
                    except Exception:
                        log.error("pp.handle_failed", exc_info=True, sample=text[:200])
                elif time.monotonic() - last_msg > idle:
                    raise TimeoutError(f"watchdog: no message for {idle:.0f}s")
                self.raw.maybe_flush()
                if time.monotonic() - last_sync >= sync_every:
                    last_sync = time.monotonic()
                    for m in self.subscription_messages(self.clock()):
                        await ws.send(json.dumps(m))
                        log.info("pp.subscriptions", method=m["method"], n=len(m["keys"]), total=len(self.subscribed))
            try:
                await ws.close()
            except Exception:
                pass

    # ----- gaps ------------------------------------------------------------------------------
    def _open_gap(self, t: float, reason: str) -> None:
        if self._gap_id is None:
            self._gap_id = coverage.gap_open(self.conn, SOURCE, t, reason)

    def _close_gap(self, t: float) -> None:
        if self._gap_id is not None:
            coverage.gap_close(self.conn, self._gap_id, t)
            self._gap_id = None

    @staticmethod
    async def _sleep(stop: asyncio.Event, seconds: float) -> None:
        try:
            await asyncio.wait_for(stop.wait(), timeout=seconds)
        except asyncio.TimeoutError:
            pass

    def stats(self) -> dict[str, Any]:
        return {"connects": self.connects, "failures": self.failures, "messages": self.messages,
                "subscribed": len(self.subscribed), "trades_enabled": self.trades_enabled, "counts": dict(self.counts)}
