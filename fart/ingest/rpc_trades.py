"""Per-mint trade capture over the keyed RPC websocket, BEFORE graduation only.

One connection, many `logsSubscribe` subscriptions: for every active watchlist mint whose reason is
in `rpc_trades.reasons` (probation, early_buyers, meta_burst) and that has not graduated we subscribe
on the bonding curve PDA (config `rpc_trades.mention`: 'bonding_curve' | 'mint'). Never on a pump-amm
pool: one hot pool produced 79,604 notifications in two hours (VPS, 26 Oct 2026); graduated coins are
measured by the tracker's 5-minute DexScreener samples instead.

Every notification is decoded (`TradeEvent` from the curve; pump-amm `BuyEvent`/`SellEvent` only when
the pool maps to a known base mint, e.g. the first pool buy inside a migration transaction) and stored
in `trades` with source `rpc_trades` / `pump_amm`. Costs are booked in the ledger as
`ws_trade_notification`, `ws_trade_subscribe`, `ws_trade_unsubscribe`: since milestone 4 as the bytes
of the frames received and sent times `cu_per_byte` (config/budget.json; Alchemy bills Solana
websocket subscriptions per byte), aggregated per minute with the byte count kept, so
`fart status` -> rpc CU today by method shows bytes and CU per stream.

Three limits, in this order:
  * the hourly envelope: the stream's own CU in the current UTC hour may not exceed
    `daily_cu_share` / 24 of the daily cap (0.35 / 24 by default, so the other RPC users always keep
    the remaining 65% of the day); above it every subscription is dropped until the next hour
    (`rpc_trades.envelope_paused` counter, `policy.envelope_paused`);
  * the per-subscription cap: `max_notifications_per_subscription_per_hour` (2000); above it the
    subscription is dropped for the rest of its hour (`rpc_trades.subscription_capped` counter);
  * the milestone-2 rules: at most `rpc_trades_cap` subscriptions, probation dropped above
    `rpc_trades_cu_drop_fraction` of the daily cap, the stream closed at the daily cap.

Hysteresis (milestone 4, after 5,298 subscriptions for 238 mints in 52 minutes on the VPS): a
subscription is only sent for a mint that was desired in `subscribe_after_syncs` consecutive syncs
and only removed after the mint has been undesired for `unsubscribe_after_s`; a watchlist flicker
of one engine tick therefore never reaches the websocket. Every closed `ws_subscriptions` row carries
its `close_reason` (envelope, capped, unwatched, graduated, cu_policy, stream_cap, address_changed,
error, disconnect, shutdown). The `rpc_trades.subscriptions` log line is written when the set
changes and otherwise once per `log_interval_s`.

Every write is one short `BEGIN IMMEDIATE` transaction with retries (`db.write_tx`); a handler that
still fails is counted (`ingest.handle_failed.rpc_trades`) and its raw line can be replayed
(`fart ingest replay --source rpc_trades`): `ws_subscriptions.sub_id` maps a raw notification back
to its mint.
"""
from __future__ import annotations

import asyncio
import json
import sqlite3
import time
from dataclasses import dataclass
from typing import Any, Callable

from .. import db
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
REASONS_DEFAULT = ("probation", "early_buyers", "meta_burst")
M_NOTIFICATION = "ws_trade_notification"
M_SUBSCRIBE = "ws_trade_subscribe"
M_UNSUBSCRIBE = "ws_trade_unsubscribe"
STREAM_METHODS = (M_NOTIFICATION, M_SUBSCRIBE, M_UNSUBSCRIBE)
DAILY_CU_SHARE_DEFAULT = 0.35
UNSUBSCRIBE_AFTER_S_DEFAULT = 60.0
SUBSCRIBE_AFTER_SYNCS_DEFAULT = 2
# why a subscribed mint is no longer desired, in the order the filters of desired() apply
CLOSE_REASONS = ("unwatched", "graduated", "capped", "cu_policy", "envelope", "stream_cap", "address_changed", "error",
                 "disconnect", "shutdown")


@dataclass
class Sub:
    mint: str
    address: str
    kind: str              # 'curve' | 'mint'
    reason: str | None
    req_id: int
    since: float
    sub_id: int | None = None
    remove: bool = False   # unsubscribe as soon as the server confirms the id
    notifications: int = 0
    trades: int = 0
    row_id: int | None = None   # ws_subscriptions.id
    hour_start: float = 0.0     # per-subscription hourly cap window (UTC hour)
    hour_notifications: int = 0


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
        self.muted_until: dict[str, float] = {}  # mint -> end of the hour it was capped in
        self._req_id = 0
        self._pda_cache: dict[str, str] = {}
        self.connects = 0
        self.notifications = 0
        self.failures = 0
        self._pending_failures = 0
        self.counts: dict[str, int] = {}
        self._gap_id: int | None = None
        self._connected = False
        self._envelope_paused = False
        # unflushed frames per ledger method: bytes and count (booked about once a minute)
        self._pending_bytes: dict[str, int] = {m: 0 for m in STREAM_METHODS}
        self._pending_frames: dict[str, int] = {m: 0 for m in STREAM_METHODS}
        # hysteresis state (milestone 4)
        self._desired_streak: dict[str, int] = {}       # mint -> consecutive syncs it was desired in
        self._undesired_since: dict[str, float] = {}    # subscribed mint -> first sync it was not desired
        self._undesired_reason: dict[str, str] = {}     # subscribed mint -> latest reason it is not desired
        self.undesired_reasons: dict[str, str] = {}     # set by desired(): subscribed mints it left out, with why
        self._throttle = Throttle(3600)
        self.policy: dict[str, Any] = {}
        self.replay = False  # `fart ingest replay`: data writes only, no raw copy, no costs, no counters

    # ----- subscription policy -------------------------------------------------------------------
    def cu_fraction(self) -> float | None:
        u = self.budget.daily_usage(self.budget_source)
        cap = u.get("daily_cost_cap")
        if not cap:
            return None
        return float(u["cost"]) / float(cap)

    def daily_cu_share(self) -> float:
        """The share of the daily CU cap the stream may use over a whole day (config `daily_cu_share`;
        the milestone-3 `hourly_cu_fraction` is still read as 24 x that fraction)."""
        share = self.cfg.get("daily_cu_share")
        if share is None:
            frac = self.cfg.get("hourly_cu_fraction")
            share = 24.0 * float(frac) if frac is not None else DAILY_CU_SHARE_DEFAULT
        return max(0.0, min(1.0, float(share)))

    def hourly_allowance(self) -> float | None:
        """CU the trade stream itself may spend per UTC hour: daily_cu_share / 24 x the daily cap."""
        sc = self.budget.source(self.budget_source)
        if not sc.daily_cost:
            return None
        return float(sc.daily_cost) * self.daily_cu_share() / 24.0

    def stream_cost_this_hour(self, now: float) -> float:
        """Ledgered ws_trade_* cost since the start of the current UTC hour plus the unflushed pending frames."""
        hour_start = now - (now % 3600.0)
        row = self.conn.execute(
            "SELECT COALESCE(SUM(cost), 0) FROM api_ledger WHERE source=? AND method IN (?,?,?) AND ts>=? AND blocked_reason IS NULL",
            (self.budget_source, *STREAM_METHODS, hour_start),
        ).fetchone()
        sc = self.budget.source(self.budget_source)
        pending = sum(sc.cost_of_frames(m, self._pending_bytes[m], self._pending_frames[m]) for m in STREAM_METHODS)
        return float(row[0]) + pending

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
        if self.cfg.get("mention", "bonding_curve") == "bonding_curve":
            return self._curve_address(mint, row["bonding_curve"]), "curve"
        return mint, "mint"

    def _set_envelope_paused(self, paused: bool, now: float, spent: float, allowance: float | None) -> None:
        if paused == self._envelope_paused:
            return
        self._envelope_paused = paused
        if paused:
            log.warning("rpc_trades.envelope_paused", stream_cu_this_hour=round(spent), hourly_allowance=allowance,
                        note="every trade subscription dropped until the next UTC hour")
            try:
                db.write_tx(self.conn, lambda c: (db.counter_bump_daily(c, "rpc_trades.envelope_paused", 1, now),
                                                  kv_set(c, "rpc_trades.envelope_paused_at", str(now), now)),
                            what="rpc_trades.envelope", attempts=2)
            except sqlite3.OperationalError as e:
                log.warning("rpc_trades.envelope_counter_failed", error=str(e)[:100])
        else:
            log.info("rpc_trades.envelope_resumed", stream_cu_this_hour=round(spent), hourly_allowance=allowance)

    def desired(self, now: float) -> dict[str, tuple[str, str, str | None]]:
        """mint -> (address, kind, reason) for the subscriptions we want right now. Also fills
        `self.undesired_reasons`: for every currently subscribed mint that is left out, the first filter
        that removed it (the future `close_reason` of its row)."""
        watch.expire(self.conn, now)
        rows = self.conn.execute(
            "SELECT w.mint, w.reason, w.since, c.bonding_curve, (m.mint IS NOT NULL) AS migrated"
            " FROM watchlist w LEFT JOIN coins c ON c.mint = w.mint LEFT JOIN migrations m ON m.mint = w.mint"
            " WHERE w.active = 1"
        ).fetchall()
        for mint, until in list(self.muted_until.items()):
            if until <= now:
                del self.muted_until[mint]
        reasons = set(self.cfg.get("reasons", REASONS_DEFAULT))
        graduated = sum(1 for r in rows if r["migrated"])
        cands = [r for r in rows if not r["migrated"] and (r["reason"] or "probation") in reasons and r["mint"] not in self.muted_until]
        frac = self.cu_fraction()
        drop_at = float(self.watch_cfg.get("rpc_trades_cu_drop_fraction", 0.8))
        dropping = frac is not None and frac >= drop_at
        keep = set(self.watch_cfg.get("rpc_trades_keep_reasons_above_cu_fraction", KEEP_DEFAULT))
        if dropping:
            cands = [r for r in cands if (r["reason"] or "probation") in keep]
        allowance = self.hourly_allowance()
        spent = self.stream_cost_this_hour(now)
        paused = allowance is not None and spent >= allowance
        self._set_envelope_paused(paused, now, spent, allowance)
        if paused:
            cands = []
        cap = int(self.watch_cfg.get("rpc_trades_cap", 150))
        cands.sort(key=lambda r: (watch.PRIORITY.get(r["reason"], 0), r["since"]), reverse=True)
        out: dict[str, tuple[str, str, str | None]] = {}
        for r in cands[:cap]:
            address, kind = self._address_for(r)
            out[r["mint"]] = (address, kind, r["reason"])
        by_mint = {r["mint"]: r for r in rows}
        self.undesired_reasons = {}
        for mint in self.subs:
            if mint in out:
                continue
            r = by_mint.get(mint)
            if r is None or (r["reason"] or "probation") not in reasons:
                why = "unwatched"
            elif r["migrated"]:
                why = "graduated"
            elif mint in self.muted_until:
                why = "capped"
            elif dropping and (r["reason"] or "probation") not in keep:
                why = "cu_policy"
            elif paused:
                why = "envelope"
            else:
                why = "stream_cap"
            self.undesired_reasons[mint] = why
        self.policy = {"active_watchlist": len(rows), "eligible": len(cands), "subscribed": len(out), "cap": cap,
                       "cu_fraction": None if frac is None else round(frac, 3), "dropping_low_priority": dropping,
                       "graduated_excluded": graduated, "muted": len(self.muted_until),
                       "daily_cu_share": self.daily_cu_share(),
                       "hourly_cu_allowance": None if allowance is None else round(allowance),
                       "stream_cu_this_hour": round(spent), "envelope_paused": paused}
        if dropping and self._throttle.allow("dropping", now):
            log.warning("rpc_trades.cu_policy_dropping_probation", **self.policy)
        return out

    # ----- protocol messages ----------------------------------------------------------------------
    def _next_id(self) -> int:
        self._req_id += 1
        return self._req_id

    def _book_frame(self, method: str, text: str) -> None:
        """Count one websocket frame (sent or received) for the minute's cost flush: bytes and frames."""
        if self.replay:
            return
        self._pending_bytes[method] += len(text.encode("utf-8"))
        self._pending_frames[method] += 1

    def _unsubscribe_msg(self, sub: Sub) -> dict[str, Any]:
        rid = self._next_id()
        self.unsub_req[rid] = int(sub.sub_id)  # type: ignore[arg-type]
        msg = {"jsonrpc": "2.0", "id": rid, "method": "logsUnsubscribe", "params": [sub.sub_id]}
        self._book_frame(M_UNSUBSCRIBE, json.dumps(msg))
        return msg

    @staticmethod
    def _close_row(conn: sqlite3.Connection, sub: Sub, now: float, reason: str) -> None:
        if sub.row_id is not None:
            conn.execute("UPDATE ws_subscriptions SET until=?, close_reason=? WHERE id=? AND until IS NULL", (now, reason, sub.row_id))

    def sync_messages(self, now: float) -> list[dict[str, Any]]:
        """Diff the desired set against the current subscriptions with hysteresis; returns JSON-RPC
        messages to send. A mint is subscribed once it was desired in `subscribe_after_syncs`
        consecutive syncs and unsubscribed once it has been undesired for `unsubscribe_after_s`.
        The ws_subscriptions rows are written first (one short transaction); the in-memory state and
        the messages follow only when that succeeded, so a failed write changes nothing and the next
        sync simply tries again."""
        desired = self.desired(now)
        commitment = self.cfg.get("commitment", "confirmed")
        unsub_after = float(self.cfg.get("unsubscribe_after_s", UNSUBSCRIBE_AFTER_S_DEFAULT))
        sub_after = max(1, int(self.cfg.get("subscribe_after_syncs", SUBSCRIBE_AFTER_SYNCS_DEFAULT)))
        for mint in list(self._desired_streak):
            if mint not in desired:
                del self._desired_streak[mint]
        for mint in desired:
            self._desired_streak[mint] = self._desired_streak.get(mint, 0) + 1
        remove: list[tuple[Sub, str]] = []
        for mint, sub in list(self.subs.items()):
            want = desired.get(mint)
            if want is not None and want[0] == sub.address:
                self._undesired_since.pop(mint, None)
                self._undesired_reason.pop(mint, None)
                continue
            first = self._undesired_since.setdefault(mint, now)
            self._undesired_reason[mint] = "address_changed" if want is not None else self.undesired_reasons.get(mint, "unwatched")
            if now - first >= unsub_after:
                remove.append((sub, self._undesired_reason[mint]))
        add: list[tuple[str, str, str, str | None]] = []
        for mint, (address, kind, reason) in desired.items():
            if mint not in self.subs and self._desired_streak.get(mint, 0) >= sub_after:
                add.append((mint, address, kind, reason))
        if not remove and not add:
            return []

        def _write(c: sqlite3.Connection) -> list[int]:
            for sub, why in remove:
                self._close_row(c, sub, now, why)
            ids = []
            for mint, address, kind, reason in add:
                cur = c.execute(
                    "INSERT INTO ws_subscriptions(mint, address, kind, reason, since) VALUES(?,?,?,?,?)", (mint, address, kind, reason, now)
                )
                ids.append(int(cur.lastrowid))
            return ids

        row_ids = db.write_tx(self.conn, _write, what="rpc_trades.sync")
        msgs: list[dict[str, Any]] = []
        for sub, why in remove:
            if sub.sub_id is None:
                sub.remove = True
            else:
                self.by_sub.pop(sub.sub_id, None)
                msgs.append(self._unsubscribe_msg(sub))
            del self.subs[sub.mint]
            self._undesired_since.pop(sub.mint, None)
            self._undesired_reason.pop(sub.mint, None)
            self._bump(f"closed_{why}")
        for (mint, address, kind, reason), row_id in zip(add, row_ids):
            rid = self._next_id()
            sub = Sub(mint=mint, address=address, kind=kind, reason=reason, req_id=rid, since=now, row_id=row_id)
            self.subs[mint] = sub
            self.by_req[rid] = sub
            msg = {"jsonrpc": "2.0", "id": rid, "method": "logsSubscribe",
                   "params": [{"mentions": [address]}, {"commitment": commitment}]}
            self._book_frame(M_SUBSCRIBE, json.dumps(msg))
            msgs.append(msg)
        return msgs

    # ----- notification handling (sync, testable) ---------------------------------------------------
    def _sub_for(self, params: dict[str, Any], received_at: float) -> Sub | None:
        sid = params.get("subscription")
        sub = self.by_sub.get(sid)
        if sub is None and self.replay and isinstance(sid, int):
            row = self.conn.execute(
                "SELECT mint, address, kind, reason, since FROM ws_subscriptions WHERE sub_id=? AND since<=?"
                " AND (until IS NULL OR until>=?) ORDER BY since DESC LIMIT 1", (sid, received_at, received_at),
            ).fetchone()
            if row is not None:
                sub = Sub(mint=row["mint"], address=row["address"], kind=row["kind"], reason=row["reason"], req_id=0,
                          since=float(row["since"]), sub_id=sid)
        return sub

    def _cap_subscription(self, sub: Sub, now: float) -> None:
        """The per-subscription hourly cap was hit: drop it for the rest of its hour and count it."""
        until = sub.hour_start + 3600.0
        self.muted_until[sub.mint] = until
        if sub.sub_id is not None:
            self.by_sub.pop(sub.sub_id, None)
            self.outgoing.append(self._unsubscribe_msg(sub))
        else:
            sub.remove = True
        if self.subs.get(sub.mint) is sub:
            del self.subs[sub.mint]
        self._undesired_since.pop(sub.mint, None)
        self._undesired_reason.pop(sub.mint, None)
        detail = {"mint": sub.mint, "kind": sub.kind, "reason": sub.reason, "notifications_this_hour": sub.hour_notifications, "muted_until": until}
        log.warning("rpc_trades.subscription_capped", **detail)
        try:
            db.write_tx(self.conn, lambda c: (self._close_row(c, sub, now, "capped"),
                                              db.counter_bump_daily(c, "rpc_trades.subscription_capped", 1, now),
                                              kv_set(c, "rpc_trades.last_capped", json.dumps(detail), now)),
                        what="rpc_trades.cap", attempts=2)
        except sqlite3.OperationalError as e:
            log.warning("rpc_trades.cap_counter_failed", error=str(e)[:100])

    def handle_text(self, text: str, received_at: float) -> str:
        try:
            msg = json.loads(text)
        except json.JSONDecodeError:
            msg = None
        ref = None if self.replay else self.raw.write(msg if msg is not None else text, received_at)
        if not isinstance(msg, dict):
            self._bump("unparseable")
            return "unparseable"
        if msg.get("method") != "logsNotification":
            return self._handle_response(msg, text, received_at)
        self.notifications += 1
        self._book_frame(M_NOTIFICATION, text)
        params = msg.get("params") or {}
        result = params.get("result") or {}
        value = result.get("value") or {}
        slot = (result.get("context") or {}).get("slot")
        sub = self._sub_for(params, received_at)
        if sub is None:
            self._bump("orphan")
            return "orphan"
        sub.notifications += 1
        if not self.replay:
            if sub.hour_start == 0.0 or received_at - sub.hour_start >= 3600.0:
                sub.hour_start = received_at - (received_at % 3600.0)
                sub.hour_notifications = 0
            sub.hour_notifications += 1
            cap = int(self.cfg.get("max_notifications_per_subscription_per_hour", 2000))
            if cap > 0 and sub.hour_notifications > cap:
                self._cap_subscription(sub, received_at)
                self._bump("capped")
                return "capped"
        signature = value.get("signature")
        if value.get("err"):
            self._bump("failed_tx")
            self._touch(sub, received_at, 0)
            return "failed_tx"
        events, undecoded = decode_logs(value.get("logs"))
        recs = store.records_from_events(events, signature, slot)
        amm, rejected = store.pump_amm_trade_records(events, signature, slot, self._mint_of_pool, self.pf.get("wsol_mint"))
        pool = store.pool_from_create_pool_event(events)
        mig = recs["migration"]
        counts = {"other_mint": 0, "amm_unwatched": 0}
        n_trades = {"n": 0}

        def _apply(c: sqlite3.Connection) -> None:
            n_trades["n"] = 0
            counts["other_mint"] = counts["amm_unwatched"] = 0
            if undecoded and not self.replay:
                store.store_undecoded(c, undecoded, SOURCE, signature, slot, received_at)
                for reason, n in summarize_undecoded(undecoded).items():
                    store.counter_bump(c, f"decode.{reason}", n, received_at)
            for t in recs["trades"]:
                if t.get("mint") != sub.mint:
                    counts["other_mint"] += 1
                    continue
                if store.insert_trade(c, t, SOURCE, received_at):
                    n_trades["n"] += 1
            for t in amm:
                if not watch.is_watched(c, t["mint"]):
                    counts["amm_unwatched"] += 1
                    continue
                if store.insert_trade(c, t, SOURCE_AMM, received_at):
                    n_trades["n"] += 1
            if not self.replay:
                for r in rejected:
                    store.counter_bump(c, f"decode.pump_amm_{r}", 1, received_at)
            if mig and mig.get("mint") == sub.mint and store.migration_rejection(c, mig.get("mint"), self.pf) is None:
                store.upsert_migration(c, mig, SOURCE, received_at)
            if pool and pool["mint"] == sub.mint and store.migration_rejection(c, pool["mint"], self.pf) is None:
                store.upsert_migration(c, dict(pool, signature=signature, slot=slot, block_time=None), SOURCE, received_at)
            if not self.replay:
                self._touch_row(c, sub, received_at, n_trades["n"])
                kv_set(c, "ingest.rpc_trades.last_msg_at", str(received_at), received_at)
                if self._pending_failures:
                    db.counter_bump(c, f"ingest.handle_failed.{SOURCE}", self._pending_failures, received_at)
                    db.counter_bump(c, f"ingest.handle_failed.{SOURCE}.{db.day_of(received_at)}", self._pending_failures, received_at)

        try:
            db.write_tx(self.conn, _apply, what="rpc_trades.handle")
        except sqlite3.OperationalError as e:
            self._handle_failed(received_at, ref, e)
            raise
        self._pending_failures = 0
        sub.trades += n_trades["n"]
        for r in rejected:
            self._bump(f"amm_{r}")
        if counts["other_mint"]:
            self._bump("trade_other_mint", counts["other_mint"])
        if counts["amm_unwatched"]:
            self._bump("amm_unwatched", counts["amm_unwatched"])
        kind = "trade" if n_trades["n"] else "notification"
        self._bump(kind)
        return kind

    def _handle_failed(self, received_at: float, ref: str | None, exc: BaseException) -> None:
        self.failures += 1
        self._pending_failures += 1
        detail = f"{ref or '-'} {type(exc).__name__}: {str(exc)[:120]}"
        if db.record_failure(self.conn, f"ingest.handle_failed.{SOURCE}", received_at, self._pending_failures, detail=detail):
            self._pending_failures = 0

    def _handle_response(self, msg: dict[str, Any], text: str, received_at: float) -> str:
        rid = msg.get("id")
        # the frame's bytes belong to the request it answers (subscribe / unsubscribe), else to the stream
        self._book_frame(M_SUBSCRIBE if rid in self.by_req else M_UNSUBSCRIBE if rid in self.unsub_req else M_NOTIFICATION, text)
        if "error" in msg:
            sub = self.by_req.pop(rid, None)
            if sub is not None:
                log.warning("rpc_trades.subscribe_error", mint=sub.mint, kind=sub.kind, error=str(msg.get("error"))[:200])
                if self.subs.get(sub.mint) is sub:
                    del self.subs[sub.mint]
                self._close_row(self.conn, sub, received_at, "error")
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
                if sub.row_id is not None:
                    self.conn.execute("UPDATE ws_subscriptions SET sub_id=? WHERE id=?", (result, sub.row_id))
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

    def _mint_of_pool(self, pool: str) -> str | None:
        return store.mint_of_pool(self.conn, pool)

    def _touch(self, sub: Sub, received_at: float, n_trades: int) -> None:
        sub.trades += n_trades
        if sub.row_id is not None and not self.replay:
            self._touch_row(self.conn, sub, received_at, n_trades)

    @staticmethod
    def _touch_row(conn: sqlite3.Connection, sub: Sub, received_at: float, n_trades: int) -> None:
        if sub.row_id is not None:
            conn.execute(
                "UPDATE ws_subscriptions SET notifications=notifications+1, trades=trades+?, last_notification_at=? WHERE id=?",
                (n_trades, received_at, sub.row_id),
            )

    def _bump(self, k: str, n: int = 1) -> None:
        self.counts[k] = self.counts.get(k, 0) + n

    # ----- budget ------------------------------------------------------------------------------------
    def flush_cost(self) -> None:
        """Book the frames of the last minute per ledger method: bytes x cu_per_byte (the byte count is
        kept in the row), or n x the table estimate when the method is not byte-priced."""
        sc = self.budget.source(self.budget_source)
        for method in STREAM_METHODS:
            n, nbytes = self._pending_frames[method], self._pending_bytes[method]
            if n:
                self.budget.record(self.budget_source, method, sc.cost_of_frames(method, nbytes, n), True, None, None, nbytes=nbytes)
            self._pending_frames[method] = self._pending_bytes[method] = 0

    def cap_reached(self) -> bool:
        """Within one CU of the daily cap (or past it: the streams book after the fact)."""
        v = self.budget.peek(self.budget_source, M_NOTIFICATION, cost=1.0)
        return (not v.allowed) and v.reason in ("daily_cost_cap", "daily_requests_cap")

    # ----- connection loop -------------------------------------------------------------------------------
    async def run(self, stop: asyncio.Event) -> None:
        t0 = self.clock()
        db.kv_setdefault(self.conn, "ingest.rpc_trades.first_started", str(t0), t0)
        kv_set(self.conn, "ingest.rpc_trades.started", str(t0), t0)
        self._open_gap(t0, "startup")
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
        self._reset_subscriptions(self.clock(), "shutdown")
        self.flush_cost()
        self.raw.flush()

    def _reset_subscriptions(self, now: float, reason: str = "disconnect") -> None:
        """Forget every subscription of a session that ended (the rows are closed with `reason`). The
        hysteresis counters stay: a mint that was desired before the drop is re-subscribed at once."""
        subs = list(self.subs.values())
        if subs:
            try:
                db.write_tx(self.conn, lambda c: [self._close_row(c, s, now, reason) for s in subs], what="rpc_trades.reset", attempts=2)
            except sqlite3.OperationalError as e:
                log.warning("rpc_trades.reset_rows_failed", error=str(e)[:100])
        self.subs.clear()
        self.by_req.clear()
        self.by_sub.clear()
        self.unsub_req.clear()
        self.outgoing.clear()
        self._undesired_since.clear()
        self._undesired_reason.clear()

    async def _send_all(self, ws, msgs: list[dict[str, Any]]) -> None:
        for m in msgs:
            await ws.send(json.dumps(m))

    def _log_subscriptions(self, sent: int) -> None:
        log.info("rpc_trades.subscriptions", sent=sent, subscribed=len(self.subs), confirmed=len(self.by_sub),
                 pending_unsubscribe=len(self._undesired_since), eligible=self.policy.get("eligible"),
                 envelope_paused=self.policy.get("envelope_paused"), stream_cu_this_hour=self.policy.get("stream_cu_this_hour"),
                 hourly_cu_allowance=self.policy.get("hourly_cu_allowance"))

    async def _session(self, stop: asyncio.Event) -> None:
        idle = float(self.cfg.get("watchdog_idle_s", 900))
        idle_min_subs = int(self.cfg.get("watchdog_min_subscriptions", 20))
        sync_every = float(self.cfg.get("sync_interval_s", 5))
        log_every = float(self.cfg.get("log_interval_s", 60))
        async with self._connect(self.url, ping_interval=float(self.cfg.get("ping_interval_s", 30)), ping_timeout=30,
                                 max_size=8 * 1024 * 1024, open_timeout=15, close_timeout=5) as ws:
            self.connects += 1
            self._connected = True
            now = self.clock()
            self._reset_subscriptions(now, "disconnect")
            self._close_gap(now)
            msgs = self.sync_messages(now)
            await self._send_all(ws, msgs)
            log.info("rpc_trades.connected", connects=self.connects, source=self.budget_source, subscriptions=len(msgs), **self.policy)
            last_msg = time.monotonic()
            last_sync = time.monotonic()
            last_cost = time.monotonic()
            last_log = time.monotonic()
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
                    try:
                        msgs = self.sync_messages(self.clock())
                    except sqlite3.OperationalError as e:
                        log.warning("rpc_trades.sync_failed", error=str(e)[:100])
                        msgs = []
                    if msgs:
                        await self._send_all(ws, msgs)
                    if msgs or time.monotonic() - last_log >= log_every:
                        last_log = time.monotonic()
                        self._log_subscriptions(len(msgs))
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
                "confirmed": len(self.by_sub), "pending_unsubscribe": len(self._undesired_since), "failures": self.failures,
                "muted": len(self.muted_until), "counts": dict(self.counts), "policy": dict(self.policy)}


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
