"""Per-mint trade capture over the keyed RPC websocket (PumpPortal only serves trades to funded keys).

One connection, many `logsSubscribe` subscriptions: for every active watchlist mint we subscribe on
the bonding curve PDA before graduation (or the mint, config `rpc_trades.mention`) and on the
pump-amm pool after it. Every notification is decoded (`TradeEvent` from the curve, pump-amm
`BuyEvent`/`SellEvent` from the pool) and stored in `trades` with source `rpc_trades` /
`pump_amm`. Every notification, subscribe and unsubscribe is booked in the budget ledger as
estimated compute units; above `rpc_trades_cu_drop_fraction` of the daily CU cap only the
`rpc_trades_keep_reasons_above_cu_fraction` watchlist reasons keep a subscription (probation is
dropped first). Reconnects re-subscribe to the desired set. Per-subscription counters live in
`ws_subscriptions` so `fart status` can show the top subscriptions by notifications.
"""
from __future__ import annotations

import asyncio
import json
import sqlite3
import time
from dataclasses import dataclass
from typing import Any, Callable

from ..chain.events import decode_logs, summarize_undecoded
from ..chain.pda import bonding_curve_address
from ..db import kv_set
from ..log import Throttle, get_logger
from ..net.budget import Blocked, Budget
from ..util import now as wall_now
from . import coverage, store, watch
from .raw import RawWriter

log = get_logger("rpc_trades")

SOURCE = "rpc_trades"
SOURCE_AMM = "pump_amm"
KEEP_DEFAULT = ("observe", "early_buyers", "meta_burst")


@dataclass
class Sub:
    mint: str
    address: str
    kind: str              # 'curve' | 'pool' | 'mint'
    reason: str | None
    req_id: int
    since: float
    sub_id: int | None = None
    remove: bool = False   # unsubscribe as soon as the server confirms the id
    notifications: int = 0
    trades: int = 0
    row_id: int | None = None   # ws_subscriptions.id


class RpcTradesClient:
    def __init__(
        self,
        conn: sqlite3.Connection,
        budget: Budget,
        raw: RawWriter,
        wss_url: str,
        budget_source: str,
        cfg: dict[str, Any],
        watch_cfg: dict[str, Any],
        pumpfun_cfg: dict[str, Any],
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
        self.pf = pumpfun_cfg
        self.clock = clock
        if connect is None:
            import websockets

            connect = websockets.connect
        self._connect = connect
        self.subs: dict[str, Sub] = {}          # desired/active by mint
        self.by_req: dict[int, Sub] = {}        # pending logsSubscribe requests
        self.by_sub: dict[int, Sub] = {}        # confirmed, by server subscription id
        self.unsub_req: dict[int, int] = {}     # logsUnsubscribe request id -> subscription id
        self.outgoing: list[dict[str, Any]] = []
        self._req_id = 0
        self._pda_cache: dict[str, str] = {}
        self.connects = 0
        self.notifications = 0
        self.counts: dict[str, int] = {}
        self._gap_id: int | None = None
        self._connected = False
        self._pending_cost_n = 0
        self._pending_sub_n = 0
        self._pending_unsub_n = 0
        self._throttle = Throttle(3600)
        self.policy: dict[str, Any] = {}

    # ----- subscription policy -------------------------------------------------------------------
    def cu_fraction(self) -> float | None:
        u = self.budget.daily_usage(self.budget_source)
        cap = u.get("daily_cost_cap")
        if not cap:
            return None
        return float(u["cost"]) / float(cap)

    def _curve_address(self, mint: str, bonding_curve: str | None) -> str:
        if bonding_curve:
            return bonding_curve
        if mint not in self._pda_cache:
            try:
                self._pda_cache[mint] = bonding_curve_address(mint, self.pf["program_id"])
            except ValueError:
                self._pda_cache[mint] = mint
        return self._pda_cache[mint]

    def _address_for(self, row: sqlite3.Row) -> tuple[str, str]:
        mint = row["mint"]
        if row["migrated"]:
            if row["pool_address"]:
                return row["pool_address"], "pool"
            return mint, "mint"
        if self.cfg.get("mention", "bonding_curve") == "bonding_curve":
            return self._curve_address(mint, row["bonding_curve"]), "curve"
        return mint, "mint"

    def desired(self, now: float) -> dict[str, tuple[str, str, str | None]]:
        """mint -> (address, kind, reason) for the subscriptions we want right now."""
        watch.expire(self.conn, now)
        rows = self.conn.execute(
            "SELECT w.mint, w.reason, w.since, c.bonding_curve, m.pool_address, (m.mint IS NOT NULL) AS migrated"
            " FROM watchlist w LEFT JOIN coins c ON c.mint = w.mint LEFT JOIN migrations m ON m.mint = w.mint"
            " WHERE w.active = 1"
        ).fetchall()
        frac = self.cu_fraction()
        drop_at = float(self.watch_cfg.get("rpc_trades_cu_drop_fraction", 0.8))
        dropping = frac is not None and frac >= drop_at
        keep = set(self.watch_cfg.get("rpc_trades_keep_reasons_above_cu_fraction", KEEP_DEFAULT))
        cap = int(self.watch_cfg.get("rpc_trades_cap", 150))
        cands = [r for r in rows if not (dropping and (r["reason"] or "probation") not in keep)]
        cands.sort(key=lambda r: (watch.PRIORITY.get(r["reason"], 0), r["since"]), reverse=True)
        out: dict[str, tuple[str, str, str | None]] = {}
        for r in cands[:cap]:
            address, kind = self._address_for(r)
            out[r["mint"]] = (address, kind, r["reason"])
        self.policy = {"active_watchlist": len(rows), "eligible": len(cands), "subscribed": len(out), "cap": cap,
                       "cu_fraction": None if frac is None else round(frac, 3), "dropping_low_priority": dropping}
        if dropping and self._throttle.allow("dropping", now):
            log.warning("rpc_trades.cu_policy_dropping_probation", **self.policy)
        return out

    # ----- protocol messages ----------------------------------------------------------------------
    def _next_id(self) -> int:
        self._req_id += 1
        return self._req_id

    def _unsubscribe_msg(self, sub: Sub) -> dict[str, Any]:
        rid = self._next_id()
        self.unsub_req[rid] = int(sub.sub_id)  # type: ignore[arg-type]
        self._pending_unsub_n += 1
        return {"jsonrpc": "2.0", "id": rid, "method": "logsUnsubscribe", "params": [sub.sub_id]}

    def _close_row(self, sub: Sub, now: float) -> None:
        if sub.row_id is not None:
            self.conn.execute("UPDATE ws_subscriptions SET until=? WHERE id=? AND until IS NULL", (now, sub.row_id))

    def sync_messages(self, now: float) -> list[dict[str, Any]]:
        """Diff the desired set against the current subscriptions; returns JSON-RPC messages to send."""
        desired = self.desired(now)
        msgs: list[dict[str, Any]] = []
        commitment = self.cfg.get("commitment", "confirmed")
        self.conn.execute("BEGIN")
        try:
            for mint, sub in list(self.subs.items()):
                want = desired.get(mint)
                if want is not None and want[0] == sub.address:
                    continue
                if sub.sub_id is None:
                    sub.remove = True
                else:
                    self.by_sub.pop(sub.sub_id, None)
                    msgs.append(self._unsubscribe_msg(sub))
                self._close_row(sub, now)
                del self.subs[mint]
            for mint, (address, kind, reason) in desired.items():
                if mint in self.subs:
                    continue
                rid = self._next_id()
                sub = Sub(mint=mint, address=address, kind=kind, reason=reason, req_id=rid, since=now)
                self.subs[mint] = sub
                self.by_req[rid] = sub
                self._pending_sub_n += 1
                cur = self.conn.execute(
                    "INSERT INTO ws_subscriptions(mint, address, kind, reason, since) VALUES(?,?,?,?,?)",
                    (mint, address, kind, reason, now),
                )
                sub.row_id = int(cur.lastrowid)
                msgs.append({"jsonrpc": "2.0", "id": rid, "method": "logsSubscribe",
                             "params": [{"mentions": [address]}, {"commitment": commitment}]})
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise
        return msgs

    # ----- notification handling (sync, testable) ---------------------------------------------------
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
            return self._handle_response(msg, received_at)
        self.notifications += 1
        self._pending_cost_n += 1
        params = msg.get("params") or {}
        result = params.get("result") or {}
        value = result.get("value") or {}
        slot = (result.get("context") or {}).get("slot")
        sub = self.by_sub.get(params.get("subscription"))
        if sub is None:
            self._bump("orphan")
            return "orphan"
        sub.notifications += 1
        signature = value.get("signature")
        if value.get("err"):
            self._bump("failed_tx")
            self._touch(sub, received_at, 0)
            return "failed_tx"
        events, undecoded = decode_logs(value.get("logs"))
        recs = store.records_from_events(events, signature, slot)
        expected_pool = sub.address if sub.kind == "pool" else self._pool_of(sub.mint)
        amm, rejected = store.pump_amm_trade_records(events, signature, slot, sub.mint, expected_pool)
        n_trades = 0
        self.conn.execute("BEGIN")
        try:
            if undecoded:
                store.store_undecoded(self.conn, undecoded, SOURCE, signature, slot, received_at)
                for reason, n in summarize_undecoded(undecoded).items():
                    store.counter_bump(self.conn, f"decode.{reason}", n, received_at)
            for t in recs["trades"]:
                if t.get("mint") != sub.mint:
                    self._bump("trade_other_mint")
                    continue
                if store.insert_trade(self.conn, t, SOURCE, received_at):
                    n_trades += 1
            for t in amm:
                if store.insert_trade(self.conn, t, SOURCE_AMM, received_at):
                    n_trades += 1
            for r in rejected:
                store.counter_bump(self.conn, f"decode.pump_amm_{r}", 1, received_at)
                self._bump(f"amm_{r}")
            mig = recs["migration"]
            if mig and mig.get("mint") == sub.mint:
                store.upsert_migration(self.conn, mig, SOURCE, received_at)
            pool = store.pool_from_create_pool_event(events)
            if pool and pool["mint"] == sub.mint:
                store.upsert_migration(self.conn, dict(pool, signature=signature, slot=slot, block_time=None), SOURCE, received_at)
            self._touch(sub, received_at, n_trades)
            kv_set(self.conn, "ingest.rpc_trades.last_msg_at", str(received_at), received_at)
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise
        kind = "trade" if n_trades else "notification"
        self._bump(kind)
        return kind

    def _handle_response(self, msg: dict[str, Any], received_at: float) -> str:
        rid = msg.get("id")
        if "error" in msg:
            sub = self.by_req.pop(rid, None)
            if sub is not None:
                log.warning("rpc_trades.subscribe_error", mint=sub.mint, kind=sub.kind, error=str(msg.get("error"))[:200])
                if self.subs.get(sub.mint) is sub:
                    del self.subs[sub.mint]
                self._close_row(sub, received_at)
                self._bump("subscribe_error")
            elif rid in self.unsub_req:
                self.unsub_req.pop(rid)
                self._bump("unsubscribe_error")
            else:
                log.warning("rpc_trades.error_message", error=str(msg.get("error"))[:200])
                self._bump("error")
            return "error"
        if rid in self.by_req:
            sub = self.by_req.pop(rid)
            result = msg.get("result")
            if isinstance(result, int):
                sub.sub_id = result
                if sub.remove:
                    self.outgoing.append(self._unsubscribe_msg(sub))
                else:
                    self.by_sub[result] = sub
            self._bump("subscribed")
            return "subscribed"
        if rid in self.unsub_req:
            sid = self.unsub_req.pop(rid)
            self.by_sub.pop(sid, None)
            self._bump("unsubscribed")
            return "unsubscribed"
        self._bump("other")
        return "other"

    def _pool_of(self, mint: str) -> str | None:
        row = self.conn.execute("SELECT pool_address FROM migrations WHERE mint=?", (mint,)).fetchone()
        return row["pool_address"] if row is not None else None

    def _touch(self, sub: Sub, received_at: float, n_trades: int) -> None:
        sub.trades += n_trades
        if sub.row_id is not None:
            self.conn.execute(
                "UPDATE ws_subscriptions SET notifications=notifications+1, trades=trades+?, last_notification_at=? WHERE id=?",
                (n_trades, received_at, sub.row_id),
            )

    def _bump(self, k: str) -> None:
        self.counts[k] = self.counts.get(k, 0) + 1

    # ----- budget ------------------------------------------------------------------------------------
    def flush_cost(self) -> None:
        """Book notifications and (un)subscribes as estimated compute units (aggregated, about once a minute)."""
        sc = self.budget.source(self.budget_source)
        for method, n in (("ws_notification", self._pending_cost_n), ("ws_subscribe", self._pending_sub_n), ("ws_unsubscribe", self._pending_unsub_n)):
            if n:
                self.budget.record(self.budget_source, method, sc.cost_of(method) * n, True, None, None)
        self._pending_cost_n = self._pending_sub_n = self._pending_unsub_n = 0

    def cap_reached(self) -> bool:
        v = self.budget.peek(self.budget_source, "ws_notification")
        return (not v.allowed) and v.reason in ("daily_cost_cap", "daily_requests_cap")

    # ----- connection loop -------------------------------------------------------------------------------
    async def run(self, stop: asyncio.Event) -> None:
        kv_set(self.conn, "ingest.rpc_trades.started", str(self.clock()), self.clock())
        self._open_gap(self.clock(), "startup")
        backoff_min = float(self.cfg.get("backoff_min_s", 10))
        backoff_max = float(self.cfg.get("backoff_max_s", 300))
        attempt = 0
        while not stop.is_set():
            if self.cap_reached():
                self._open_gap(self.clock(), "budget_cap")
                log.warning("rpc_trades.paused_budget_cap", source=self.budget_source)
                await self._sleep(stop, 300)
                continue
            verdict = self.budget.acquire(self.budget_source, "ws_connect")
            if not verdict.allowed:
                wait = min(max(verdict.retry_in_s, 5.0), 300.0)
                log.warning("rpc_trades.connect_gated", reason=verdict.reason, wait_s=round(wait, 1))
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
                self.budget.record(self.budget_source, "ws_connect", verdict.cost, self._connected, None,
                                   (time.monotonic() - t0) * 1000.0, err=None if self._connected else f"{type(e).__name__}: {e}")
                log.warning("rpc_trades.disconnected", connected=self._connected, error=f"{type(e).__name__}: {str(e)[:200]}")
                if self._connected and time.monotonic() - t0 > 60:
                    attempt = 0
            self.flush_cost()
            if stop.is_set():
                break
            self._open_gap(self.clock(), "disconnect")
            attempt += 1
            await self._sleep(stop, min(backoff_max, backoff_min * (2 ** (attempt - 1))))
        self._open_gap(self.clock(), "shutdown")
        self._reset_subscriptions(self.clock())
        self.flush_cost()
        self.raw.flush()

    def _reset_subscriptions(self, now: float) -> None:
        for sub in self.subs.values():
            self._close_row(sub, now)
        self.subs.clear()
        self.by_req.clear()
        self.by_sub.clear()
        self.unsub_req.clear()
        self.outgoing.clear()

    async def _send_all(self, ws, msgs: list[dict[str, Any]]) -> None:
        for m in msgs:
            await ws.send(json.dumps(m))

    async def _session(self, stop: asyncio.Event) -> None:
        idle = float(self.cfg.get("watchdog_idle_s", 900))
        idle_min_subs = int(self.cfg.get("watchdog_min_subscriptions", 20))
        sync_every = float(self.cfg.get("sync_interval_s", 5))
        async with self._connect(self.url, ping_interval=float(self.cfg.get("ping_interval_s", 30)), ping_timeout=30,
                                 max_size=8 * 1024 * 1024, open_timeout=15, close_timeout=5) as ws:
            self.connects += 1
            self._connected = True
            now = self.clock()
            self._reset_subscriptions(now)
            self._close_gap(now)
            msgs = self.sync_messages(now)
            await self._send_all(ws, msgs)
            log.info("rpc_trades.connected", connects=self.connects, source=self.budget_source, subscriptions=len(msgs), **self.policy)
            last_msg = time.monotonic()
            last_sync = time.monotonic()
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
                        log.error("rpc_trades.handle_failed", exc_info=True, sample=text[:200])
                elif len(self.by_sub) >= idle_min_subs and time.monotonic() - last_msg > idle:
                    raise TimeoutError(f"watchdog: no notification for {idle:.0f}s with {len(self.by_sub)} subscriptions")
                if self.outgoing:
                    out, self.outgoing = self.outgoing, []
                    await self._send_all(ws, out)
                self.raw.maybe_flush()
                if time.monotonic() - last_sync >= sync_every:
                    last_sync = time.monotonic()
                    msgs = self.sync_messages(self.clock())
                    if msgs:
                        await self._send_all(ws, msgs)
                        log.info("rpc_trades.subscriptions", sent=len(msgs), active=len(self.subs), confirmed=len(self.by_sub))
                if time.monotonic() - last_cost >= 60:
                    last_cost = time.monotonic()
                    self.flush_cost()
                    if self.cap_reached():
                        log.warning("rpc_trades.cap_reached_closing")
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
        return {"connects": self.connects, "notifications": self.notifications, "subscriptions": len(self.subs),
                "confirmed": len(self.by_sub), "counts": dict(self.counts), "policy": dict(self.policy)}


def fill_block_times(conn: sqlite3.Connection, rpc, max_n: int, now: float) -> dict[str, int]:
    """Trades whose event carried no plausible timestamp get block_time from getBlockTime, once per
    slot, bounded per run and by the budget. Runs in the sweep thread."""
    from ..chain.rpc import RpcError
    from ..net.http import HttpError

    stats = {"slots": 0, "filled": 0, "skipped_budget": 0, "errors": 0}
    rows = conn.execute(
        "SELECT DISTINCT slot FROM trades WHERE block_time IS NULL AND slot IS NOT NULL AND source IN (?, ?)"
        " ORDER BY received_at DESC LIMIT ?",
        (SOURCE, SOURCE_AMM, max(0, int(max_n))),
    ).fetchall()
    for r in rows:
        stats["slots"] += 1
        if not rpc.available("getBlockTime"):
            stats["skipped_budget"] += 1
            continue
        try:
            t = rpc.get_block_time(int(r["slot"]))
        except Blocked:
            stats["skipped_budget"] += 1
            continue
        except (HttpError, RpcError):
            stats["errors"] += 1
            continue
        if isinstance(t, int):
            conn.execute("UPDATE trades SET block_time=? WHERE slot=? AND block_time IS NULL", (float(t), r["slot"]))
            stats["filled"] += 1
    return stats
