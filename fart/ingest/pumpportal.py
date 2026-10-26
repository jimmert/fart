"""PumpPortal public data websocket: ONE connection, watchdog, bounded reconnects.

Subscriptions: subscribeNewToken, subscribeMigration, subscribeTokenTrade for the active
watchlist (synced every few seconds, capped). Every raw message is written to the hourly gzip
store before normalization. Connects go through the budget gate (max 12 per hour).
"""
from __future__ import annotations

import asyncio
import json
import random
import sqlite3
import time
from typing import Any, Callable

from ..db import kv_set
from ..log import get_logger
from ..net.budget import Budget
from ..util import now as wall_now
from . import coverage, store, watch
from .raw import RawWriter

log = get_logger("pumpportal")

SOURCE = "pumpportal"


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
    ):
        self.conn = conn
        self.budget = budget
        self.raw = raw
        self.url = url
        self.cfg = cfg
        self.watch_cfg = watch_cfg
        self.clock = clock
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
        ref = self.raw.write(msg if msg is not None else text, received_at)
        kind = store.classify_pumpportal(msg) if msg is not None else "unparseable"
        self.counts[kind] = self.counts.get(kind, 0) + 1
        self.messages += 1
        cap = int(self.watch_cfg.get("cap", 200))
        self.conn.execute("BEGIN")
        try:
            if kind == "create":
                rec = store.pumpportal_create_record(msg)
                new = store.upsert_coin(self.conn, rec, SOURCE, received_at, raw_ref=ref)
                if new and rec.get("mint") and (rec.get("platform") or "pump") == "pump" and self.watch_cfg.get("probation", True):
                    watch.add(self.conn, rec["mint"], "probation", received_at, float(self.watch_cfg.get("early_window_s", 300)), cap)
            elif kind == "trade":
                rec = store.pumpportal_trade_record(msg)
                if rec.get("mint") and watch.is_watched(self.conn, rec["mint"]):
                    store.insert_trade(self.conn, rec, SOURCE, received_at)
                else:
                    self.counts["trade_unwatched"] = self.counts.get("trade_unwatched", 0) + 1
            elif kind == "migrate":
                rec = store.pumpportal_migration_record(msg)
                store.upsert_migration(self.conn, rec, SOURCE, received_at)
                if rec.get("mint") and self.watch_cfg.get("subscribe_graduations", True):
                    watch.add(self.conn, rec["mint"], "graduation", received_at, float(self.watch_cfg.get("ttl_s", 86400)), cap)
            elif kind == "info":
                log.info("pp.info", text=str(msg)[:200])
            else:
                store.counter_bump(self.conn, "pp.unknown_messages", 1, received_at)
            kv_set(self.conn, "ingest.pumpportal.last_msg_at", str(received_at), received_at)
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise
        return kind

    def desired_subscriptions(self, now: float) -> set[str]:
        watch.expire(self.conn, now)
        cap = int(self.watch_cfg.get("cap", 200))
        return set(watch.active_mints(self.conn)[:cap])

    def subscription_messages(self, now: float) -> list[dict[str, Any]]:
        """Diff the active watchlist against what the connection is subscribed to."""
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
        kv_set(self.conn, "ingest.pumpportal.started", str(self.clock()), self.clock())
        self._open_gap(self.clock(), "startup")
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
                log.warning("pp.disconnected", connected=self._connected, error=f"{type(e).__name__}: {str(e)[:200]}")
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
            log.info("pp.connected", connects=self.connects)
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
                "subscribed": len(self.subscribed), "counts": dict(self.counts)}
