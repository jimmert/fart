"""Solana RPC logsSubscribe on the pump.fun mint authority (every create) and the migration
account (every graduation). Decodes Anchor events from `Program data:` lines, stores raw
notifications, and degrades (disconnects) when the daily compute-unit cap is reached.
"""
from __future__ import annotations

import asyncio
import json
import sqlite3
import time
from typing import Any, Callable

from ..chain.events import decode_logs, summarize_undecoded
from ..db import kv_set
from ..log import get_logger
from ..net.budget import Budget
from ..util import now as wall_now
from . import coverage, store, watch
from .raw import RawWriter

log = get_logger("rpc_logs")

SOURCE = "rpc_logs"


class RpcLogsClient:
    def __init__(
        self,
        conn: sqlite3.Connection,
        budget: Budget,
        raw: RawWriter,
        wss_url: str,
        budget_source: str,
        cfg: dict[str, Any],
        pumpfun_cfg: dict[str, Any],
        watch_cfg: dict[str, Any],
        connect: Callable[..., Any] | None = None,
        clock: Callable[[], float] = wall_now,
    ):
        self.conn = conn
        self.budget = budget
        self.raw = raw
        self.url = wss_url
        self.budget_source = budget_source
        self.cfg = cfg
        self.watch_cfg = watch_cfg
        self.addresses = {"create": pumpfun_cfg["mint_authority"], "migrate": pumpfun_cfg["migration_account"]}
        self.clock = clock
        if connect is None:
            import websockets

            connect = websockets.connect
        self._connect = connect
        self.connects = 0
        self.notifications = 0
        self.counts: dict[str, int] = {}
        self._gap_id: int | None = None
        self._connected = False
        self._pending_cost_n = 0

    # ----- notification handling (sync, testable) ---------------------------------------------
    def handle_text(self, text: str, received_at: float) -> str:
        try:
            msg = json.loads(text)
        except json.JSONDecodeError:
            msg = None
        self.raw.write(msg if msg is not None else text, received_at)
        if not isinstance(msg, dict):
            self._bump("unparseable")
            return "unparseable"
        if msg.get("method") != "logsNotification":
            if "error" in msg:
                log.warning("rpc_logs.error_message", error=str(msg.get("error"))[:200])
                self._bump("error")
                return "error"
            self._bump("other")
            return "other"
        self.notifications += 1
        self._pending_cost_n += 1
        result = (msg.get("params") or {}).get("result") or {}
        value = result.get("value") or {}
        slot = (result.get("context") or {}).get("slot")
        signature = value.get("signature")
        if value.get("err"):
            self._bump("failed_tx")
            return "failed_tx"
        events, undecoded = decode_logs(value.get("logs"))
        recs = store.records_from_events(events, signature, slot)
        kind = "other"
        cap = int(self.watch_cfg.get("cap", 200))
        self.conn.execute("BEGIN")
        try:
            if undecoded:
                store.store_undecoded(self.conn, undecoded, SOURCE, signature, slot, received_at)
                for reason, n in summarize_undecoded(undecoded).items():
                    store.counter_bump(self.conn, f"decode.{reason}", n, received_at)
            if recs["create"]:
                kind = "create"
                c = recs["create"]
                new = store.upsert_coin(self.conn, c, SOURCE, received_at)
                if new and c.get("mint") and self.watch_cfg.get("probation", True):
                    watch.add(self.conn, c["mint"], "probation", received_at, float(self.watch_cfg.get("early_window_s", 300)), cap)
                if signature:
                    kv_set(self.conn, "ingest.rpc_logs.last_create_sig", signature, received_at)
                for t in recs["trades"]:  # the dev buy; stored when the mint is watched (probation makes it so)
                    if t.get("mint") and watch.is_watched(self.conn, t["mint"]):
                        store.insert_trade(self.conn, t, SOURCE, received_at)
            if recs["migration"]:
                kind = "migrate"
                m = recs["migration"]
                store.upsert_migration(self.conn, m, SOURCE, received_at)
                if m.get("mint") and self.watch_cfg.get("subscribe_graduations", True):
                    watch.add(self.conn, m["mint"], "graduation", received_at, float(self.watch_cfg.get("ttl_s", 86400)), cap)
                if signature:
                    kv_set(self.conn, "ingest.rpc_logs.last_migrate_sig", signature, received_at)
            kv_set(self.conn, "ingest.rpc_logs.last_msg_at", str(received_at), received_at)
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise
        self._bump(kind)
        return kind

    def _bump(self, k: str) -> None:
        self.counts[k] = self.counts.get(k, 0) + 1

    def flush_cost(self) -> None:
        """Account notifications as estimated compute units (aggregated, about once a minute)."""
        if self._pending_cost_n:
            unit = self.budget.source(self.budget_source).cost_of("ws_notification")
            self.budget.record(self.budget_source, "ws_notification", unit * self._pending_cost_n, True, None, None)
            self._pending_cost_n = 0

    def cap_reached(self) -> bool:
        v = self.budget.peek(self.budget_source, "ws_notification")
        return (not v.allowed) and v.reason in ("daily_cost_cap", "daily_requests_cap")

    # ----- connection loop ---------------------------------------------------------------------
    async def run(self, stop: asyncio.Event) -> None:
        kv_set(self.conn, "ingest.rpc_logs.started", str(self.clock()), self.clock())
        self._open_gap(self.clock(), "startup")
        backoff_min = float(self.cfg.get("backoff_min_s", 10))
        backoff_max = float(self.cfg.get("backoff_max_s", 300))
        attempt = 0
        while not stop.is_set():
            if self.cap_reached():
                self._open_gap(self.clock(), "budget_cap")
                log.warning("rpc_logs.paused_budget_cap", source=self.budget_source)
                await self._sleep(stop, 300)
                continue
            verdict = self.budget.acquire(self.budget_source, "ws_connect")
            if not verdict.allowed:
                wait = min(max(verdict.retry_in_s, 5.0), 300.0)
                log.warning("rpc_logs.connect_gated", reason=verdict.reason, wait_s=round(wait, 1))
                await self._sleep(stop, wait)
                continue
            t0 = time.monotonic()
            self._connected = False
            try:
                await self._session(stop)
                self.budget.record(self.budget_source, "ws_connect", verdict.cost, True, None, (time.monotonic() - t0) * 1000.0)
                attempt = 0
            except asyncio.CancelledError:
                raise
            except Exception as e:
                # connected-then-dropped is a successful connect; a failed handshake arms the budget backoff
                self.budget.record(self.budget_source, "ws_connect", verdict.cost, self._connected, None,
                                   (time.monotonic() - t0) * 1000.0, err=None if self._connected else f"{type(e).__name__}: {e}")
                log.warning("rpc_logs.disconnected", connected=self._connected, error=f"{type(e).__name__}: {str(e)[:200]}")
                if self._connected and time.monotonic() - t0 > 60:  # a stable session resets the backoff ladder
                    attempt = 0
            self.flush_cost()
            if stop.is_set():
                break
            self._open_gap(self.clock(), "disconnect")
            attempt += 1
            wait = min(backoff_max, backoff_min * (2 ** (attempt - 1)))
            await self._sleep(stop, wait)
        self._open_gap(self.clock(), "shutdown")
        self.flush_cost()
        self.raw.flush()

    async def _session(self, stop: asyncio.Event) -> None:
        idle = float(self.cfg.get("watchdog_idle_s", 180))
        commitment = self.cfg.get("commitment", "confirmed")
        async with self._connect(self.url, ping_interval=float(self.cfg.get("ping_interval_s", 30)), ping_timeout=30,
                                 max_size=8 * 1024 * 1024, open_timeout=15, close_timeout=5) as ws:
            self.connects += 1
            self._connected = True
            log.info("rpc_logs.connected", connects=self.connects, source=self.budget_source)
            self._close_gap(self.clock())
            for req_id, kind in ((1, "create"), (2, "migrate")):
                self.budget.acquire(self.budget_source, "ws_subscribe")
                await ws.send(json.dumps({
                    "jsonrpc": "2.0", "id": req_id, "method": "logsSubscribe",
                    "params": [{"mentions": [self.addresses[kind]]}, {"commitment": commitment}],
                }))
            last_msg = time.monotonic()
            last_cost = time.monotonic()
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
                        log.error("rpc_logs.handle_failed", exc_info=True, sample=text[:200])
                elif time.monotonic() - last_msg > idle:
                    raise TimeoutError(f"watchdog: no notification for {idle:.0f}s")
                self.raw.maybe_flush()
                if time.monotonic() - last_cost >= 60:
                    last_cost = time.monotonic()
                    self.flush_cost()
                    if self.cap_reached():
                        log.warning("rpc_logs.cap_reached_closing")
                        break
            try:
                await ws.close()
            except Exception:
                pass

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
        return {"connects": self.connects, "notifications": self.notifications, "counts": dict(self.counts)}
