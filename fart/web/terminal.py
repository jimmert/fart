"""The Terminal's live feed (milestone 12, A.1, A.5, A.6): one incremental computation per second, shared by every viewer,
delivered as server-sent events (`/api/public/stream`) and as a snapshot (`/api/public/terminal`).

The feed (`TerminalFeed`, one background thread with its own read-only connection) keeps, in memory, the coins in view:
the active watchlist, the coins created in the last `creates_window_s` (30 minutes) and the coins the connected viewers
follow. Every cycle it reads the trades received since its cursor (an index range on `trades(received_at)`, never a
scan), appends them to the per-coin windows, recomputes the rows of the coins that got a trade (and every row every
`RECOMPUTE_S` seconds, so the sliding windows age honestly), reads the cheap facts the daemons leave behind (the watchlist
reasons, the observations, the graduations, kv `engine.metas_latest` for the themes, `x_keys` for the X mentions,
`creator_stats` for the makers, `plays` for the Plays column) and emits what changed. Nothing is computed per request: a
request reads the last snapshot or subscribes to the events.

Events: `snapshot` (every row, on connect and at most every `SNAPSHOT_EVERY_S`), `rows` (changed rows and removed mints),
`plays` (the open plays), `heartbeat` (every `heartbeat_s`, with the age of the engine's last tick). Every event has an
increasing id; the last `RING` delta events are kept, so a client that reconnects with `Last-Event-ID` gets what it missed
and otherwise a fresh snapshot. A connection may name up to `follow_max` mints to keep live (the follow panel); the feed
tracks a followed coin even when it left the watchlist, as long as a viewer follows it.

Rows carry the engine's momentum definitions (unique buyers, net SOL, buys and sells, the largest buyer's share, the market
cap and the graduation progress from the last curve state; `fart/research/features.py::replay_features`), the bundle flags
and the creator's sells (the same module), the creator's statistics (`creator_stats`), the theme (a bursting term of the
engine's snapshot in the coin's name or ticker), the X mentions of the last hour when the X layer counts them, the last
trade, the status and the plays firing on the coin. Unknown stays null; the page shows a dash, never a zero.
"""
from __future__ import annotations

import collections
import json
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable

from ..chain.curve import CurveParams
from ..config import Settings
from ..engine import themes as engine_themes
from ..log import Throttle, get_logger
from ..research import features as rf
from ..util import now as wall_now, utc_iso
from . import data

log = get_logger("terminal")

RECOMPUTE_S = 5.0            # every row is recomputed at least this often (the sliding windows age)
SNAPSHOT_EVERY_S = 300.0     # a full snapshot event at most this often (a client keeps itself current with the deltas)
RING = 400                   # delta events kept for a resume
WINDOW_KEEP_S = 960.0        # trades kept per coin in memory (the 15-minute window plus a margin)
MAX_TRADES_PER_CYCLE = 20000
QUEUE_MAX = 200              # events a slow client may lag before it is dropped
CURSOR_LAG_S = 5.0           # trades are read from this far behind the cursor: a row committed after a cycle passed its received_at still arrives
EVENT_SNAPSHOT, EVENT_ROWS, EVENT_PLAYS, EVENT_HEARTBEAT = "snapshot", "rows", "plays", "heartbeat"
ET = "COALESCE(block_time, received_at)"
STATUS_NL = {"probation": "nieuw (eerste 5 min)", "early_buyers": "vroege kopers", "meta_burst": "thema", "observe": "observatie", "graduation": "gegradueerd",
             "graduated": "gegradueerd", "unwatched": "niet gevolgd"}


@dataclass
class CoinState:
    mint: str
    name: str | None
    symbol: str | None
    creator: str | None
    created_t: float
    dev_buy_sol: float | None
    slot: int | None
    trades: collections.deque = field(default_factory=collections.deque)   # rf.Trade, event-time order, the last WINDOW_KEEP_S
    first: list = field(default_factory=list)                              # the first 10 trades (the bundle flags)
    trades_total: int = 0
    bundle: dict[str, Any] | None = None                                    # frozen once it can be evaluated
    dev_first_sell_t: float | None = None
    dev_bought: float = 0.0
    dev_sold: float = 0.0
    last_state: tuple[int, int, float] | None = None                        # (virtual_sol, virtual_token, t)
    history: collections.deque = field(default_factory=lambda: collections.deque(maxlen=12))   # (t, buyers_5m, net_5m) for the momentum delta
    row: dict[str, Any] | None = None
    dirty: bool = True
    seen_at: float = 0.0
    computed_at: float = 0.0


@dataclass
class Event:
    id: int
    name: str
    data: str

    def encode(self) -> bytes:
        return f"id: {self.id}\nevent: {self.name}\ndata: {self.data}\n\n".encode("utf-8")


class Subscriber:
    def __init__(self, follow: list[str]):
        self.queue: collections.deque = collections.deque()
        self.cond = threading.Condition()
        self.follow = list(follow)
        self.dropped = False

    def put(self, ev: Event) -> None:
        with self.cond:
            if len(self.queue) >= QUEUE_MAX:
                self.dropped = True
            else:
                self.queue.append(ev)
            self.cond.notify()

    def get(self, timeout: float) -> Event | None:
        with self.cond:
            if not self.queue:
                self.cond.wait(timeout)
            return self.queue.popleft() if self.queue else None


class TerminalFeed:
    def __init__(self, settings: Settings, open_db: Callable[[], Any], clock: Callable[[], float] = wall_now, cfg: dict[str, Any] | None = None):
        self.settings = settings
        self.open_db = open_db
        self.clock = clock
        c = dict(cfg or (settings.web_site.get("terminal") or {}))
        self.creates_window_s = float(c.get("creates_window_s", 1800))
        self.max_rows = int(c.get("max_rows", 800))
        self.heartbeat_s = float(c.get("stream_heartbeat_s", 15))
        self.max_connections = int(c.get("stream_max_connections", 64))
        self.follow_max = int(c.get("follow_max", 50))
        self.poll_s = float(c.get("stream_poll_s", 1.0))
        self.params = CurveParams.from_config(settings.config("pumpfun"))
        mcfg = settings.config("measure") or {}
        self.dev_sell_window_s = float((mcfg.get("creator_stats") or {}).get("dev_sell_window_s", rf.DEV_SELL_WINDOW_S))
        self.graduation_sol = self.params.graduation_real_sol / self.params.sol_unit
        self._lock = threading.Lock()
        self._subs: dict[int, Subscriber] = {}
        self._sub_seq = 0
        self._seq = 0
        self._ring: collections.deque[Event] = collections.deque(maxlen=RING)
        self._snapshot: dict[str, Any] | None = None
        self._snapshot_json: str = ""
        self._plays_json: str = ""
        self.coins: dict[str, CoinState] = {}
        self.cursor: float | None = None
        self._seen: dict[tuple[str, str], float] = {}   # (mint, signature) -> received_at, for the lagged cursor window
        self.last_full = 0.0
        self.last_snapshot_event = 0.0
        self.last_heartbeat = 0.0
        self.last_engine_hb: float | None = None
        self.creators: dict[str, dict[str, Any] | None] = {}
        self.terms: dict[str, dict[str, Any]] = {}
        self.metas_as_of: float | None = None
        self.x_counts: dict[str, int] = {}
        self.plays_rows: list[dict[str, Any]] = []
        self.plays_by_mint: dict[str, list[str]] = {}
        self.rules_meta: dict[str, dict[str, Any]] = {}
        self.sol_usd: dict[str, Any] = {"price": None, "known": False}
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._conn: Any = None
        self.cycles = 0
        self.errors = 0
        self.last_cycle_s = 0.0
        self.slow_log = Throttle(300.0)

    # ----- subscribers --------------------------------------------------------------------------------------------------
    @property
    def connections(self) -> int:
        with self._lock:
            return len(self._subs)

    def subscribe(self, follow: Iterable[str] = (), last_event_id: int | None = None) -> tuple[Subscriber, list[Event]] | None:
        """A new viewer: None when the connection bound is reached; else the subscriber and the events to send first (the
        missed deltas when `last_event_id` is in the ring, otherwise a fresh snapshot)."""
        follow_list = [m for m in follow if isinstance(m, str) and 30 <= len(m) <= 50][: self.follow_max]
        with self._lock:
            if len(self._subs) >= self.max_connections:
                return None
            self._sub_seq += 1
            sub = Subscriber(follow_list)
            self._subs[self._sub_seq] = sub
            sub.key = self._sub_seq  # type: ignore[attr-defined]
            initial: list[Event] = []
            if last_event_id is not None and self._ring and self._ring[0].id <= last_event_id + 1 <= self._ring[-1].id + 1 and last_event_id < self._seq:
                initial = [e for e in self._ring if e.id > last_event_id]
            elif last_event_id is not None and last_event_id == self._seq:
                initial = []
            else:
                if self._snapshot_json:
                    initial = [Event(self._seq, EVENT_SNAPSHOT, self._snapshot_json)]
                if self._plays_json:
                    initial.append(Event(self._seq, EVENT_PLAYS, self._plays_json))
        return sub, initial

    def unsubscribe(self, sub: Subscriber) -> None:
        with self._lock:
            self._subs.pop(getattr(sub, "key", -1), None)

    def _followed(self) -> set[str]:
        with self._lock:
            return {m for s in self._subs.values() for m in s.follow}

    def _emit(self, name: str, data: str, keep: bool = True) -> Event:
        with self._lock:
            self._seq += 1
            ev = Event(self._seq, name, data)
            if keep:
                self._ring.append(ev)
            subs = list(self._subs.values())
        for s in subs:
            s.put(ev)
        return ev

    # ----- the thread -----------------------------------------------------------------------------------------------------
    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="fart-web-terminal", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 10.0) -> bool:
        self._stop.set()
        conn = self._conn
        if conn is not None:
            try:
                conn.interrupt()
            except Exception:  # noqa: BLE001
                pass
        t = self._thread
        if t is None:
            return True
        t.join(timeout)
        return not t.is_alive()

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def _run(self) -> None:
        while not self._stop.is_set():
            t0 = time.perf_counter()
            try:
                self.cycle()
            except Exception:  # noqa: BLE001  the feed never dies on one cycle; the connection is reopened on the next
                self.errors += 1
                log.error("terminal.cycle_failed", exc_info=True)
                self._close()
            dt = time.perf_counter() - t0
            self._stop.wait(max(0.05, self.poll_s - dt))

    def _close(self) -> None:
        if self._conn is not None:
            try:
                self._conn.close()
            except Exception:  # noqa: BLE001
                pass
            self._conn = None

    def _db(self) -> Any:
        if self._conn is None:
            try:
                self._conn = self.open_db()
            except FileNotFoundError:
                self._conn = None
        return self._conn

    # ----- one cycle --------------------------------------------------------------------------------------------------------
    def cycle(self, now: float | None = None) -> dict[str, Any]:
        t_start = time.perf_counter()
        now = self.clock() if now is None else now
        conn = self._db()
        self.cycles += 1
        if conn is None:
            self._set_snapshot(now, [], note="Er is nog geen database.")
            self._maybe_heartbeat(now, None)
            return {"rows": 0, "db": False}
        kv = data._kv(conn, ("engine.heartbeat", "sol_usd.cache"))
        engine_hb = data._float(kv.get("engine.heartbeat"))
        engine_ticked = engine_hb is not None and engine_hb != self.last_engine_hb
        self.last_engine_hb = engine_hb
        self._sol_usd(kv.get("sol_usd.cache"), now)
        # the universe
        reasons = {str(r["mint"]): str(r["reason"] or "probation") for r in conn.execute("SELECT mint, reason FROM watchlist WHERE active=1")}
        recent = conn.execute(f"SELECT mint FROM coins WHERE received_at >= ? AND {ET} >= ? ORDER BY received_at DESC LIMIT ?",
                              (now - self.creates_window_s - 60, now - self.creates_window_s, self.max_rows)).fetchall()
        universe = set(reasons) | {str(r[0]) for r in recent} | self._followed()
        new_mints = [m for m in universe if m not in self.coins]
        self._load_coins(conn, new_mints, now)
        changed = self._ingest_trades(conn, now)
        full = engine_ticked or now - self.last_full >= RECOMPUTE_S
        if full:
            self.last_full = now
            observed = self._observed(conn, list(universe))
            graduated = self._graduated(conn, list(universe))
            self._refresh_metas(conn, now)
            self._refresh_x(conn)
            self._refresh_plays(conn, now)
            self._refresh_creators(conn, [c.creator for c in self.coins.values() if c.creator])
        else:
            observed = graduated = None
        rows: list[dict[str, Any]] = []
        updates: list[dict[str, Any]] = []
        removed: list[str] = []
        for mint in list(self.coins):
            c = self.coins[mint]
            if mint not in universe:
                removed.append(mint)
                del self.coins[mint]
                continue
            if full or c.dirty or c.row is None:
                before = c.row
                c.row = self._row(c, now, reasons, observed, graduated)
                c.dirty = False
                c.computed_at = now
                if before != c.row:
                    updates.append(c.row)
            rows.append(c.row)
        rows.sort(key=lambda r: -float(r["created_t"]))
        rows = rows[: self.max_rows]
        if full or updates or removed or self._snapshot is None:
            self._set_snapshot(now, rows)
        if self._snapshot_json and (now - self.last_snapshot_event >= SNAPSHOT_EVERY_S or self.last_snapshot_event == 0.0):
            self.last_snapshot_event = now
            self._emit(EVENT_SNAPSHOT, self._snapshot_json, keep=False)
        elif updates or removed:
            self._emit(EVENT_ROWS, json.dumps({"t": utc_iso(now), "upd": updates, "del": removed, "n": len(rows)}, separators=(",", ":"), default=str))
        self._maybe_heartbeat(now, engine_hb)
        self.last_cycle_s = time.perf_counter() - t_start
        if self.last_cycle_s > 2.0 and self.slow_log.allow("slow", now):
            log.warning("terminal.cycle_slow", seconds=round(self.last_cycle_s, 2), coins=len(self.coins), updates=len(updates))
        return {"rows": len(rows), "updates": len(updates), "removed": len(removed), "full": full, "seconds": round(self.last_cycle_s, 3), "db": True}

    def _maybe_heartbeat(self, now: float, engine_hb: float | None) -> None:
        if now - self.last_heartbeat >= self.heartbeat_s:
            self.last_heartbeat = now
            self._emit(EVENT_HEARTBEAT, json.dumps({"t": utc_iso(now), "engine_age_s": None if engine_hb is None else round(max(0.0, now - engine_hb)),
                                                     "coins": len(self.coins), "viewers": self.connections}, separators=(",", ":")), keep=False)

    def _set_snapshot(self, now: float, rows: list[dict[str, Any]], note: str | None = None) -> None:
        snap = {"t": utc_iso(now), "generated_at": utc_iso(now), "generated_label": data.ts_label(now), "rows": rows, "n": len(rows),
                "engine_age_s": None if self.last_engine_hb is None else round(max(0.0, now - self.last_engine_hb)), "sol_usd": self.sol_usd,
                "creates_window_s": self.creates_window_s, "themes_as_of": None if self.metas_as_of is None else utc_iso(self.metas_as_of),
                "plays_rules": list(self.rules_meta.values()), "note": note}
        with self._lock:
            self._snapshot = snap
            self._snapshot_json = json.dumps(snap, separators=(",", ":"), default=str)

    def snapshot(self) -> dict[str, Any] | None:
        with self._lock:
            return self._snapshot

    def plays(self) -> dict[str, Any]:
        return {"rows": self.plays_rows, "rules": list(self.rules_meta.values())}

    # ----- loading ------------------------------------------------------------------------------------------------------------
    def _load_coins(self, conn: Any, mints: list[str], now: float) -> None:
        for chunk in rf.iter_chunks(mints, 400):
            marks = ",".join("?" for _ in chunk)
            for r in conn.execute(f"SELECT mint, name, symbol, creator, {ET} AS t, dev_buy_sol, slot FROM coins WHERE mint IN ({marks})", chunk):
                c = CoinState(str(r["mint"]), r["name"], r["symbol"], r["creator"], float(r["t"]), r["dev_buy_sol"], None if r["slot"] is None else int(r["slot"]))
                c.seen_at = now
                # backfill: the first ten trades (the bundle flags, the creator's buys) and the trades of the window
                first = conn.execute(f"SELECT {rf.TRADE_COLUMNS} FROM trades WHERE mint=? AND received_at <= ? ORDER BY {ET}, received_at LIMIT 10", (c.mint, now)).fetchall()
                c.first = [rf.Trade.from_row(x) for x in first]
                c.trades_total = int(conn.execute("SELECT COUNT(*) FROM trades WHERE mint=? AND received_at <= ?", (c.mint, now)).fetchone()[0])
                recent = conn.execute(f"SELECT {rf.TRADE_COLUMNS}, signature FROM trades WHERE mint=? AND received_at > ? AND received_at <= ? ORDER BY {ET}, received_at",
                                      (c.mint, now - WINDOW_KEEP_S, now)).fetchall()
                for x in recent:
                    self._add_trade(c, rf.Trade.from_row(x), backfill=True)
                    if float(x["received_at"]) > now - CURSOR_LAG_S:
                        self._seen[(c.mint, str(x["signature"]))] = float(x["received_at"])
                last = conn.execute(f"SELECT virtual_sol, virtual_token, {ET} AS t FROM trades WHERE mint=? AND received_at <= ? AND virtual_sol IS NOT NULL ORDER BY {ET} DESC, received_at DESC LIMIT 1",
                                    (c.mint, now)).fetchone()
                if last is not None:
                    c.last_state = (int(last["virtual_sol"]), int(last["virtual_token"]), float(last["t"]))
                dev = conn.execute(f"SELECT COALESCE(SUM(CASE WHEN is_buy=1 THEN tokens END),0) AS b, COALESCE(SUM(CASE WHEN is_buy=0 THEN tokens END),0) AS s,"
                                   f" MIN(CASE WHEN is_buy=0 THEN {ET} END) AS first_sell FROM trades WHERE mint=? AND buyer=? AND received_at <= ?", (c.mint, c.creator or "", now)).fetchone()
                c.dev_bought, c.dev_sold = float(dev["b"] or 0.0), float(dev["s"] or 0.0)
                c.dev_first_sell_t = None if dev["first_sell"] is None else float(dev["first_sell"])
                self.coins[c.mint] = c

    def _add_trade(self, c: CoinState, x: rf.Trade, backfill: bool = False) -> None:
        if not backfill:
            c.trades_total += 1
            if len(c.first) < 10:
                c.first.append(x)
            if c.creator and x.buyer == c.creator:
                if x.is_buy:
                    c.dev_bought += x.tokens
                else:
                    c.dev_sold += x.tokens
                    if c.dev_first_sell_t is None or x.t < c.dev_first_sell_t:
                        c.dev_first_sell_t = x.t
        if x.virtual_sol and x.virtual_token and (c.last_state is None or x.t >= c.last_state[2]):
            c.last_state = (int(x.virtual_sol), int(x.virtual_token), x.t)
        c.trades.append(x)
        c.dirty = True

    def _ingest_trades(self, conn: Any, now: float) -> int:
        if self.cursor is None:
            self.cursor = now   # the backfill of _load_coins covered the past; from here on only new rows
            return 0
        # the window starts CURSOR_LAG_S behind the cursor: a trade whose transaction committed after a cycle had already passed
        # its received_at is still read; the seen set keeps it from counting twice
        rows = conn.execute(f"SELECT {rf.TRADE_COLUMNS}, signature FROM trades WHERE received_at > ? AND received_at <= ? ORDER BY received_at LIMIT ?",
                            (self.cursor - CURSOR_LAG_S, now, MAX_TRADES_PER_CYCLE)).fetchall()
        n = 0
        last_received = self.cursor
        for r in rows:
            received = float(r["received_at"])
            last_received = max(last_received, received)
            key = (str(r["mint"]), str(r["signature"]))
            if key in self._seen:
                continue
            self._seen[key] = received
            c = self.coins.get(key[0])
            if c is None:
                continue
            self._add_trade(c, rf.Trade.from_row(r))
            n += 1
        self.cursor = min(now, last_received) if len(rows) >= MAX_TRADES_PER_CYCLE else now
        cut_seen = self.cursor - 2 * CURSOR_LAG_S
        if len(self._seen) > 2000 or (self._seen and self.cycles % 50 == 0):
            self._seen = {k: v for k, v in self._seen.items() if v >= cut_seen}
        # trim the windows
        cut = now - WINDOW_KEEP_S
        for c in self.coins.values():
            while c.trades and c.trades[0].t < cut:
                c.trades.popleft()
                c.dirty = True
        return n

    def _observed(self, conn: Any, mints: list[str]) -> dict[str, float]:
        out: dict[str, float] = {}
        for chunk in rf.iter_chunks(mints, 400):
            marks = ",".join("?" for _ in chunk)
            for r in conn.execute(f"SELECT mint, MIN(as_of) FROM decisions WHERE kind='observe' AND mint IN ({marks}) GROUP BY mint", chunk):
                out[str(r[0])] = float(r[1])
        self._observed_cache = out
        return out

    def _graduated(self, conn: Any, mints: list[str]) -> dict[str, float]:
        out: dict[str, float] = {}
        for chunk in rf.iter_chunks(mints, 400):
            marks = ",".join("?" for _ in chunk)
            for r in conn.execute(f"SELECT mint, {ET} FROM migrations WHERE mint IN ({marks})", chunk):
                out[str(r[0])] = float(r[1])
        self._graduated_cache = out
        return out

    def _refresh_metas(self, conn: Any, now: float) -> None:
        snap = engine_themes.read(conn)
        self.terms = {}
        self.metas_as_of = None
        if snap is None or now - float(snap["as_of"]) > engine_themes.MAX_AGE_S:
            return
        self.metas_as_of = float(snap["as_of"])
        for m in snap.get("metas") or []:
            if isinstance(m, dict) and m.get("term"):
                self.terms[str(m["term"])] = {"term": str(m["term"]), "bursting": bool(m.get("bursting")), "n_coins": m.get("n_coins"), "burst_ratio": m.get("burst_ratio"),
                                              "burst_is_new": bool(m.get("burst_ratio_is_inf"))}
        from ..engine.meta import load_stopwords

        self._stopwords = load_stopwords(self.settings.config("stopwords"))
        self._min_len = int((self.settings.config("engine").get("meta") or {}).get("min_token_len", 4))

    def _refresh_x(self, conn: Any) -> None:
        self.x_counts = {}
        if data.table_exists(conn, "x_keys"):
            for r in conn.execute("SELECT mint, MAX(COALESCE(last_n_1h, 0)) FROM x_keys WHERE mint IS NOT NULL AND stopped_at IS NULL GROUP BY mint"):
                self.x_counts[str(r[0])] = int(r[1] or 0)

    def _refresh_plays(self, conn: Any, now: float) -> None:
        from ..plays import rules as plays_rules

        try:
            rules = plays_rules.load_rules(self.settings)
        except Exception:  # noqa: BLE001
            rules = []
        self.rules_meta = {r.id: r.to_public() for r in rules}
        rows: list[dict[str, Any]] = []
        by: dict[str, list[str]] = {}
        if data.table_exists(conn, "plays"):
            for r in conn.execute("SELECT id, rule, version, hypothesis, mint, as_of, status, entry_json, entry_price, fill_price, last_price, peak_price FROM plays"
                                  " WHERE status IN ('signal', 'open') AND as_of >= ? ORDER BY as_of DESC LIMIT 100", (now - 2 * 86400,)):
                mint = str(r["mint"])
                by.setdefault(mint, []).append(str(r["rule"]))
                try:
                    entry = json.loads(r["entry_json"] or "{}")
                except ValueError:
                    entry = {}
                c = self.coins.get(mint)
                cur = None
                if c is not None and c.last_state is not None:
                    cur = _curve(c.last_state, self.params)
                rule = self.rules_meta.get(str(r["rule"]), {})
                rows.append({"id": int(r["id"]), "rule": str(r["rule"]), "name": rule.get("name"), "version": int(r["version"]), "mint": mint, "mint_short": _short(mint),
                             "coin": (c.name if c else None), "symbol": (c.symbol if c else None), "as_of": utc_iso(float(r["as_of"])), "since_s": round(max(0.0, now - float(r["as_of"]))),
                             "status": str(r["status"]), "status_label": {"signal": "signaal, wacht op vulling", "open": "open"}.get(str(r["status"]), str(r["status"])),
                             "hit_rate": rule.get("hit_rate"), "ev_sol": rule.get("ev_sol"), "ev_pct": rule.get("ev_pct"), "exit_text": rule.get("exit_text"),
                             "entry_curve_sol": entry.get("curve_sol"), "entry_mcap_sol": entry.get("mcap_sol"), "entry_price": r["entry_price"], "fill_price": r["fill_price"],
                             "now_curve_sol": None if cur is None else cur["curve_sol"], "now_mcap_sol": None if cur is None else cur["mcap_sol"],
                             "change_pct": (None if not r["fill_price"] or cur is None or not cur.get("price_sol") else round(cur["price_sol"] / float(r["fill_price"]) - 1.0, 4))})
        self.plays_rows = rows
        self.plays_by_mint = by
        js = json.dumps({"t": utc_iso(now), "rows": rows, "rules": list(self.rules_meta.values())}, separators=(",", ":"), default=str)
        if js != self._plays_json:
            self._plays_json = js
            self._emit(EVENT_PLAYS, js)

    def _refresh_creators(self, conn: Any, creators: list[str]) -> None:
        from ..measure import creators as creators_mod

        missing = sorted({c for c in creators if c not in self.creators})
        if not missing or not data.table_exists(conn, "creator_stats"):
            for c in missing:
                self.creators[c] = None
            return
        found = creators_mod.lookup(conn, missing)
        for c in missing:
            self.creators[c] = found.get(c)
        if len(self.creators) > 20000:
            self.creators = {k: v for k, v in list(self.creators.items())[-10000:]}

    def _sol_usd(self, raw: str | None, now: float) -> None:
        out: dict[str, Any] = {"price": None, "known": False}
        if raw:
            try:
                d = json.loads(raw)
                price, ts = float(d["price"]), float(d["ts"])
                if now - ts <= 3600 and price > 0:
                    out = {"price": price, "known": True, "age_s": round(now - ts)}
            except (ValueError, TypeError, KeyError):
                pass
        self.sol_usd = out

    # ----- the row ----------------------------------------------------------------------------------------------------------
    def _row(self, c: CoinState, now: float, reasons: dict[str, str], observed: dict[str, float] | None, graduated: dict[str, float] | None) -> dict[str, Any]:
        obs = observed if observed is not None else getattr(self, "_observed_cache", {})
        grads = graduated if graduated is not None else getattr(self, "_graduated_cache", {})
        trades = [x for x in c.trades if x.t <= now]
        f = rf.replay_features(trades, now, c.created_t, self.params) if trades else None
        if c.bundle is None:
            b = rf.bundle_flags(c.first, now, c.created_t, c.dev_buy_sol, c.slot, graduation_sol=self.graduation_sol, creator=c.creator)
            if b["bundled_any"] is not None or len(c.first) >= rf.FIRST_N:
                c.bundle = b   # frozen once it can be judged; an unknown stays open until more trades arrive
        else:
            b = c.bundle
        st = _curve(c.last_state, self.params) if c.last_state is not None else {"price_sol": None, "mcap_sol": None, "prog": None, "curve_sol": None}
        grad_t = grads.get(c.mint)
        reason = reasons.get(c.mint)
        if grad_t is not None:
            status = "graduated"
        elif c.mint in obs:
            status = "observe"
        elif reason:
            status = reason
        else:
            status = "unwatched"
        buyers = f["unique_buyers_5m"] if f else 0
        net = f["net_sol_5m"] if f else 0.0
        c.history.append((now, buyers, net))
        past = next((h for h in c.history if now - h[0] >= 55.0), None)
        maker = self.creators.get(c.creator or "") if c.creator else None
        theme = self._theme(c)
        mcap_sol = st["mcap_sol"]
        return {
            "mint": c.mint, "mint_short": _short(c.mint), "name": c.name or "naam onbekend", "symbol": c.symbol, "maker": c.creator,
            "created_t": c.created_t, "created_at": utc_iso(c.created_t), "age_s": round(now - c.created_t),
            "mcap_sol": mcap_sol, "mcap_usd": None if mcap_sol is None or not self.sol_usd.get("known") else round(mcap_sol * float(self.sol_usd["price"])),
            "prog": st["prog"], "curve_sol": st["curve_sol"], "price_sol": st["price_sol"],
            "buyers_5m": buyers, "net_sol_5m": round(net, 3), "buys_5m": f["buys_5m"] if f else 0, "sells_5m": f["sells_5m"] if f else 0,
            "trades_5m": f["trades_5m"] if f else 0, "trades_total": c.trades_total,
            "share": f["largest_buyer_share_15m"] if f else None, "dev_frac": b.get("dev_buy_frac_of_curve"), "dev_buy_sol": c.dev_buy_sol,
            "bundled": b.get("bundled_any"), "bundle_reasons": b.get("bundle_reasons") or [],
            "dev_sold": c.dev_first_sell_t is not None, "dev_sold_frac": (round(min(1.0, c.dev_sold / c.dev_bought), 3) if c.dev_bought > 0 else (1.0 if c.dev_sold > 0 else None)),
            "maker_stats": None if not maker else {"coins": maker.get("coins"), "grads": maker.get("graduations"), "winners": maker.get("winners"), "bundled": maker.get("bundled"),
                                                   "dumped": maker.get("dev_sold_early"), "track": maker.get("track_record")},
            "theme": theme, "x_1h": self.x_counts.get(c.mint), "last_trade_age_s": None if c.last_state is None else round(max(0.0, now - c.last_state[2])),
            "status": status, "status_label": STATUS_NL.get(status, status), "observed": c.mint in obs, "graduated": grad_t is not None,
            "d_buyers": None if past is None else buyers - past[1], "d_net": None if past is None else round(net - past[2], 3),
            "plays": self.plays_by_mint.get(c.mint, []),
        }

    def _theme(self, c: CoinState) -> dict[str, Any] | None:
        if not self.terms:
            return None
        from ..engine.meta import tokenize

        toks = tokenize(c.name, c.symbol, getattr(self, "_stopwords", set()), getattr(self, "_min_len", 4))
        hits = [self.terms[t] for t in toks if t in self.terms]
        if not hits:
            return None
        best = max(hits, key=lambda h: (bool(h["bursting"]), h.get("n_coins") or 0))
        return {"term": best["term"], "bursting": bool(best["bursting"]), "burst_is_new": bool(best.get("burst_is_new")), "burst_ratio": best.get("burst_ratio"), "n_coins": best.get("n_coins")}


def _curve(state: tuple[int, int, float], params: CurveParams) -> dict[str, Any]:
    from ..chain.curve import CurveState, graduation_progress

    try:
        st = CurveState(int(state[0]), int(state[1]))
        return {"price_sol": st.price_sol(params), "mcap_sol": round(st.mcap_sol(params), 2), "prog": round(graduation_progress(st, params), 4),
                "curve_sol": round(st.real_sol_estimate(params) / params.sol_unit, 2)}
    except (TypeError, ValueError, ZeroDivisionError):
        return {"price_sol": None, "mcap_sol": None, "prog": None, "curve_sol": None}


def _short(mint: str) -> str:
    return mint if len(mint) <= 12 else f"{mint[:4]}…{mint[-4:]}"


# ----- the page's configuration and the presets (A.2, A.3) ------------------------------------------------------------------
PRESETS = [
    {"id": "nieuw", "label": "nieuw < 5 min", "text": "munten jonger dan 5 minuten", "filter": {"max_age_s": 300}},
    {"id": "momentum", "label": "momentum", "text": "unieke kopers én netto SOL stijgen (tegenover een minuut eerder)", "filter": {"rising": True, "min_buyers": 5}},
    {"id": "bijna", "label": "bijna gegradueerd", "text": "minstens 70 SOL in de curve, nog niet gegradueerd", "filter": {"min_curve_sol": 70, "status": ["probation", "early_buyers", "meta_burst", "observe", "unwatched"]}},
    {"id": "organisch", "label": "organisch", "text": "niet gebundeld, dev-buy hoogstens 2% van de curve, grootste koper hoogstens 15%", "filter": {"bundled": "no", "max_dev_frac": 0.02, "max_share": 0.15}},
    {"id": "thema", "label": "thema brandt", "text": "de naam of ticker draagt een thema dat volgens de engine nu opkomt", "filter": {"theme": "burning"}},
    {"id": "makers", "label": "makers met track record", "text": "de maker had eerder een graduatie of winnaar in onze data", "filter": {"maker": "track"}},
]

COLUMNS = [
    ("age", "leeftijd", True), ("coin", "naam / ticker", True), ("mcap", "marktwaarde", True), ("prog", "graduatie (SOL van 85)", True), ("buyers", "kopers 5 min", True),
    ("net", "netto SOL 5 min", True), ("bs", "kopers / verkopers", True), ("share", "grootste koper", True), ("dev", "dev-buy", True), ("bundle", "bundel", True),
    ("maker", "maker", True), ("theme", "thema", True), ("x", "X (1 u)", False), ("last", "laatste trade", True), ("status", "status", True), ("plays", "plays", True),
    ("links", "acties", True),
]


def terminal_config(settings: Settings) -> dict[str, Any]:
    """What the page hands the script (as a data attribute, JSON): the terminals, the presets, the columns, the limits."""
    site = settings.web_site
    terms = []
    for t in site.get("terminals") or []:
        if isinstance(t, dict) and t.get("name") and isinstance(t.get("url"), str) and t["url"].startswith("https://") and "{mint}" in t["url"]:
            terms.append({"name": str(t["name"])[:24], "url": t["url"], "verified": bool(t.get("verified", False))})
    tc = site.get("terminal") or {}
    return {"terminals": terms[:5], "presets": PRESETS, "columns": [{"id": k, "label": l, "default": d} for k, l, d in COLUMNS],
            "links": {"pumpfun": "https://pump.fun/coin/{mint}", "dexscreener": "https://dexscreener.com/solana/{mint}", "solscan": "https://solscan.io/token/{mint}"},
            "creates_window_s": int(tc.get("creates_window_s", 1800)), "follow_max": int(tc.get("follow_max", 50)), "pulse_s": int(tc.get("stream_heartbeat_s", 15)),
            "stream": "/api/public/stream", "snapshot": "/api/public/terminal"}


def parse_follow(value: str | None) -> list[str]:
    if not value:
        return []
    out = []
    for part in str(value).split(","):
        p = part.strip()
        if 30 <= len(p) <= 50 and all(ch.isalnum() for ch in p):
            out.append(p)
    return out[:200]
