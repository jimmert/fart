"""Bundle, dev-sell and creator-history features from `trades` and `coins` (milestone 12, B.4), outside the rule files.

Everything here is as-of: a function that takes `as_of` reads only rows knowable then (`db.asof_clause`), so the harness
never sees the future and the plays layer computes what a human could have known at that moment. The engine's own
features (`fart/engine/features.py`, a frozen rule file) are imported where the plays layer needs them and never changed;
these functions add what the hypotheses of B.2 need and what the engine does not compute:

* `bundle_flags`: three bundle definitions, each a plain yes/no/unknown:
    - `dev`: the creator's own buy at the create is more than `dev_frac` (5%) of the SOL the curve needs to graduate;
    - `slot`: at least `slot_min_buyers` (3) distinct buyers in the same slot as the create whose buys together are more
      than `slot_frac` (15%) of the curve (the classic bundle: a block of wallets filled in the create transaction's slot);
    - `top2`: in the first `first_n` (10) trades two wallets bought more than `top2_frac` (40%) of the buy SOL.
  `any` is the OR of the three; a definition that cannot be evaluated yet (no slot known, fewer than ten trades and the
  coin younger than two minutes) is None, never False.
* `dev_sell`: the first sell by the creator wallet (time, tokens, share of what the creator held), whether it fell within
  `window_s` (600) of the create, and the creator's sold fraction as of the moment.
* `creator_history`: what our own data says about the creator before this coin: earlier coins (all time and in the last
  24 h), their graduations, their winners (24 h outcome of the graduation), their bundled coins (the `any` flag), their
  early dev sells, and the time since the previous coin. Winners are counted only from outcomes measured before `as_of`
  (an outcome written later is hindsight).
* `replay_windows`: the pure, in-memory version of the engine's window features over a list of trades, for the harness
  (which walks a coin's trades once instead of querying per tick) and the terminal feed. Its numbers are the engine's
  definitions (distinct buying wallets, buy minus sell SOL, counts over the window) computed in Python.
"""
from __future__ import annotations

import bisect
import sqlite3
from dataclasses import dataclass
from typing import Any, Iterable

from ..db import asof_clause

ASOF = asof_clause()
ET = "COALESCE(block_time, received_at)"

DEV_FRAC = 0.05            # bundle definition 1: dev buy > 5% of the curve's graduation SOL
SLOT_MIN_BUYERS = 3        # bundle definition 2: >= 3 buyers in the create's slot ...
SLOT_FRAC = 0.15           # ... buying together > 15% of the curve
FIRST_N = 10               # bundle definition 3: the first 10 trades ...
TOP2_FRAC = 0.40           # ... with > 40% of the buy SOL by 2 wallets
DEV_SELL_WINDOW_S = 600.0  # a creator sell within 10 minutes of the create is an early dev sell
YOUNG_S = 120.0            # under two minutes with fewer than ten trades the top2 flag is still unknown
TOP2_MIN_BUYS = 5          # the top2 flag is judged only once the first trades hold at least this many buys (two buys are always > 40%)


@dataclass
class Trade:
    """One trade as the replay helpers want it (a `trades` row without the SQL)."""
    t: float
    buyer: str | None
    is_buy: bool
    sol: float
    tokens: float
    virtual_sol: int | None
    virtual_token: int | None
    slot: int | None = None
    received_at: float | None = None

    @classmethod
    def from_row(cls, r: Any) -> "Trade":
        t = r["block_time"] if r["block_time"] is not None else r["received_at"]
        return cls(float(t), r["buyer"], bool(r["is_buy"]), float(r["sol"] or 0.0), float(r["tokens"] or 0.0),
                   None if r["virtual_sol"] is None else int(r["virtual_sol"]), None if r["virtual_token"] is None else int(r["virtual_token"]),
                   None if r["slot"] is None else int(r["slot"]), float(r["received_at"]))


TRADE_COLUMNS = "mint, slot, block_time, received_at, buyer, is_buy, sol, tokens, virtual_sol, virtual_token"


def load_trades(conn: sqlite3.Connection, mint: str, as_of: float | None = None, until_received: float | None = None) -> list[Trade]:
    """A coin's trades in event-time order; with `as_of` only the rows knowable then (the harness replays a coin once
    with every trade and applies the as-of itself per tick, so it passes None and `until_received`)."""
    if as_of is not None:
        rows = conn.execute(f"SELECT {TRADE_COLUMNS} FROM trades WHERE mint=? AND {ASOF} ORDER BY {ET}, received_at", (mint, as_of, as_of)).fetchall()
    elif until_received is not None:
        rows = conn.execute(f"SELECT {TRADE_COLUMNS} FROM trades WHERE mint=? AND received_at <= ? ORDER BY {ET}, received_at", (mint, until_received)).fetchall()
    else:
        rows = conn.execute(f"SELECT {TRADE_COLUMNS} FROM trades WHERE mint=? ORDER BY {ET}, received_at", (mint,)).fetchall()
    return [Trade.from_row(r) for r in rows]


def knowable(trades: list[Trade], as_of: float) -> list[Trade]:
    """The trades of a replay knowable at `as_of`: received by then and with an event time at or before it."""
    return [x for x in trades if (x.received_at is None or x.received_at <= as_of) and x.t <= as_of]


# ----- the three bundle definitions ----------------------------------------------------------------------------------------
def bundle_flags(trades: list[Trade], as_of: float, created_t: float, dev_buy_sol: float | None, create_slot: int | None,
                 graduation_sol: float = 85.0, dev_frac: float = DEV_FRAC, slot_min_buyers: int = SLOT_MIN_BUYERS, slot_frac: float = SLOT_FRAC,
                 first_n: int = FIRST_N, top2_frac: float = TOP2_FRAC, creator: str | None = None) -> dict[str, Any]:
    """The bundle flags of a coin as of `as_of` from the trades knowable then (pass `knowable(trades, as_of)` or a full list
    the caller already bounded). Each flag is True / False / None (cannot be evaluated yet)."""
    xs = [x for x in trades if x.t <= as_of]
    dev = None if dev_buy_sol is None else (float(dev_buy_sol) / graduation_sol > dev_frac)
    dev_frac_value = None if dev_buy_sol is None else float(dev_buy_sol) / graduation_sol
    # slot bundle: buyers in the create's slot (the creator's own buy excluded), together above the fraction
    slot = None
    slot_buyers = 0
    slot_sol = 0.0
    if create_slot is not None:
        same = [x for x in xs if x.is_buy and x.slot == create_slot and x.buyer and x.buyer != creator]
        wallets = {x.buyer for x in same}
        slot_buyers = len(wallets)
        slot_sol = sum(x.sol for x in same)
        slot = slot_buyers >= slot_min_buyers and slot_sol / graduation_sol > slot_frac
    # top2: the first N trades, two wallets with more than the fraction of the buy SOL
    first = xs[:first_n]
    top2 = None
    top2_share = None
    if len(first) >= first_n or as_of - created_t >= YOUNG_S:
        buys: dict[str, float] = {}
        total = 0.0
        n_buys = 0
        for x in first:
            if x.is_buy and x.sol > 0:
                buys[x.buyer or "?"] = buys.get(x.buyer or "?", 0.0) + x.sol
                total += x.sol
                n_buys += 1
        if total > 0 and buys and n_buys >= TOP2_MIN_BUYS:
            top = sorted(buys.values(), reverse=True)[:2]
            top2_share = sum(top) / total
            top2 = top2_share > top2_frac
        # fewer buys than that: the share of two wallets says nothing yet, the flag stays unknown (a dead coin stays unknown)
    flags = [dev, slot, top2]
    if any(f is True for f in flags):
        any_flag: bool | None = True
    elif top2 is False and dev is not None:
        any_flag = False     # the slot flag needs the create's slot; without one the verdict rests on the dev buy and the top-2 share
    else:
        any_flag = None
    return {"bundled_dev": dev, "dev_buy_frac_of_curve": None if dev_frac_value is None else round(dev_frac_value, 4),
            "bundled_slot": slot, "slot_buyers": slot_buyers, "slot_sol": round(slot_sol, 4), "bundled_top2": top2,
            "top2_share": None if top2_share is None else round(top2_share, 4), "bundled_any": any_flag,
            "bundle_reasons": [r for r, f in (("dev_buy_gt_5pct", dev), ("same_slot_buyers", slot), ("top2_gt_40pct", top2)) if f]}


# ----- the creator's sells ------------------------------------------------------------------------------------------------
def dev_sell(trades: list[Trade], as_of: float, created_t: float, creator: str | None, window_s: float = DEV_SELL_WINDOW_S) -> dict[str, Any]:
    """The creator wallet's first sell as of `as_of`, whether it fell within `window_s` of the create, and the fraction of
    what the creator bought that was sold. Without a creator: everything None."""
    out: dict[str, Any] = {"dev_first_sell_t": None, "dev_sold_within_window": None, "dev_sold_frac": None, "dev_sell_window_s": window_s}
    if not creator:
        return out
    bought = sold = 0.0
    first_sell = None
    for x in trades:
        if x.t > as_of or x.buyer != creator:
            continue
        if x.is_buy:
            bought += x.tokens
        else:
            sold += x.tokens
            if first_sell is None:
                first_sell = x.t
    out["dev_first_sell_t"] = first_sell
    out["dev_sold_within_window"] = (first_sell is not None and first_sell - created_t <= window_s) if first_sell is not None else False
    if bought > 0:
        out["dev_sold_frac"] = round(min(1.0, sold / bought), 4)
    elif sold > 0:
        out["dev_sold_frac"] = 1.0
    return out


# ----- the creator's history, as-of ----------------------------------------------------------------------------------------
def creator_history(conn: sqlite3.Connection, creator: str | None, as_of: float, exclude_mint: str | None, created_t: float | None = None,
                    method_version: str = "m1.0", h24: int = 86400, params: Any = None, dev_sell_window_s: float = DEV_SELL_WINDOW_S) -> dict[str, Any]:
    """What our own data says about `creator` before `as_of`: earlier coins (all, last 24 h before the coin's create),
    graduations, winners (a graduation whose 24 h outcome, measured before `as_of`, says hit), bundled coins (`bundle_flags`
    any == True on their first trades), early dev sells, the time since the previous coin. None for an unknown creator."""
    out: dict[str, Any] = {"creator_coins": None, "creator_coins_24h": None, "creator_graduations": None, "creator_winners": None,
                           "creator_not_winners": None, "creator_bundled": None, "creator_dev_sold_early": None, "creator_prev_coin_age_s": None,
                           "creator_track_record": None}
    if not creator:
        return out
    ref = created_t if created_t is not None else as_of
    rows = conn.execute(f"SELECT mint, {ET} AS t, dev_buy_sol, slot FROM coins WHERE creator=? AND mint<>? AND {ASOF} AND {ET} < ? ORDER BY t",
                        (creator, exclude_mint or "", as_of, as_of, ref)).fetchall()
    out["creator_coins"] = len(rows)
    out["creator_coins_24h"] = sum(1 for r in rows if float(r["t"]) >= ref - 86400)
    out["creator_prev_coin_age_s"] = None if not rows else round(ref - float(rows[-1]["t"]))
    mints = [str(r["mint"]) for r in rows]
    grads = winners = not_winners = bundled = dumped = 0
    if mints:
        marks = ",".join("?" for _ in mints)
        grads = int(conn.execute(f"SELECT COUNT(*) FROM migrations WHERE mint IN ({marks}) AND {ASOF}", (*mints, as_of, as_of)).fetchone()[0])
        for r in conn.execute(f"SELECT mint, hit_100k FROM outcomes WHERE subject='graduation' AND horizon_s=? AND method_version=? AND measured_at <= ?"
                              f" AND mint IN ({marks})", (int(h24), method_version, as_of, *mints)):
            if r["hit_100k"] == 1:
                winners += 1
            elif r["hit_100k"] == 0:
                not_winners += 1
        for r in rows:
            tr = load_trades(conn, str(r["mint"]), as_of=as_of)
            bf = bundle_flags(tr, as_of, float(r["t"]), r["dev_buy_sol"], r["slot"], creator=creator)
            if bf["bundled_any"] is True:
                bundled += 1
            ds = dev_sell(tr, as_of, float(r["t"]), creator, dev_sell_window_s)
            if ds["dev_sold_within_window"]:
                dumped += 1
    out.update({"creator_graduations": grads, "creator_winners": winners, "creator_not_winners": not_winners, "creator_bundled": bundled,
                "creator_dev_sold_early": dumped})
    out["creator_track_record"] = "good" if (grads > 0 or winners > 0) and bundled == 0 and dumped == 0 else ("bad" if (bundled > 0 or dumped > 0) else ("none" if not rows else "neutral"))
    return out


# ----- the engine's window features, replayed in memory -------------------------------------------------------------------
@dataclass
class WindowStats:
    unique_buyers: int
    buy_sol: float
    sell_sol: float
    buys: int
    sells: int
    trades: int

    @property
    def net_sol(self) -> float:
        return self.buy_sol - self.sell_sol


def window_stats(trades: list[Trade], as_of: float, window_s: float) -> WindowStats:
    """Distinct buying wallets, buy and sell SOL and the counts over (as_of - window_s, as_of] (the engine's `_window_stats`)."""
    lo, hi = as_of - window_s, as_of
    buyers: set[str] = set()
    buy_sol = sell_sol = 0.0
    buys = sells = 0
    for x in trades:
        if x.t <= lo or x.t > hi:
            continue
        if x.is_buy:
            buys += 1
            buy_sol += x.sol
            if x.buyer:
                buyers.add(x.buyer)
        else:
            sells += 1
            sell_sol += x.sol
    return WindowStats(len(buyers), buy_sol, sell_sol, buys, sells, buys + sells)


def largest_buyer_share(trades: list[Trade], as_of: float, window_s: float = 900.0) -> tuple[float | None, str | None]:
    """The largest buyer's share of the buy SOL over the window (the engine's 15-minute definition)."""
    lo, hi = as_of - window_s, as_of
    by: dict[str, float] = {}
    total = 0.0
    for x in trades:
        if x.is_buy and lo < x.t <= hi and x.sol > 0:
            by[x.buyer or "?"] = by.get(x.buyer or "?", 0.0) + x.sol
            total += x.sol
    if total <= 0 or not by:
        return None, None
    buyer, s = max(by.items(), key=lambda kv: kv[1])
    return s / total, buyer


def last_state(trades: list[Trade], as_of: float) -> Trade | None:
    """The last trade with reserves knowable at `as_of` (the curve state then)."""
    for x in reversed(trades):
        if x.t <= as_of and x.virtual_sol and x.virtual_token:
            return x
    return None


def replay_features(trades: list[Trade], as_of: float, created_t: float, params: Any) -> dict[str, Any]:
    """The engine's momentum features of one coin at `as_of` from a replay (unique buyers and net SOL over 1/5/15 minutes,
    the counts, the largest buyer's share over 15 minutes, the curve state), computed in memory with the engine's
    definitions. `trades` must already be the knowable set (see `knowable`)."""
    from ..chain.curve import CurveState, graduation_progress

    f: dict[str, Any] = {"as_of": as_of, "age_s": round(as_of - created_t, 1)}
    for label, w in (("1m", 60.0), ("5m", 300.0), ("15m", 900.0)):
        st = window_stats(trades, as_of, w)
        f[f"unique_buyers_{label}"] = st.unique_buyers
        f[f"net_sol_{label}"] = round(st.net_sol, 4)
        f[f"buy_sol_{label}"] = round(st.buy_sol, 4)
        f[f"buys_{label}"] = st.buys
        f[f"sells_{label}"] = st.sells
        f[f"trades_{label}"] = st.trades
    share, buyer = largest_buyer_share(trades, as_of)
    f["largest_buyer_share_15m"] = None if share is None else round(share, 4)
    f["largest_buyer_15m"] = buyer
    f["trades_total"] = sum(1 for x in trades if x.t <= as_of)
    last = last_state(trades, as_of)
    if last is not None:
        st = CurveState(int(last.virtual_sol), int(last.virtual_token))
        f["price_sol"] = st.price_sol(params)
        f["mcap_sol"] = round(st.mcap_sol(params), 4)
        f["graduation_progress"] = round(graduation_progress(st, params), 4)
        f["curve_sol"] = round(st.real_sol_estimate(params) / params.sol_unit, 3)
        f["last_trade_age_s"] = round(as_of - last.t, 1)
        f["virtual_sol"] = int(last.virtual_sol)
        f["virtual_token"] = int(last.virtual_token)
    else:
        f.update({"price_sol": None, "mcap_sol": None, "graduation_progress": None, "curve_sol": None, "last_trade_age_s": None,
                  "virtual_sol": None, "virtual_token": None})
    return f


def tick_times(created_t: float, until_t: float, tick_s: float = 15.0, first_s: float | None = None) -> list[float]:
    """The engine cadence as the harness evaluates the rules: created + k * tick_s for k >= 1 up to `until_t`
    (from `first_s` after the create when given)."""
    start = created_t + (tick_s if first_s is None else first_s)
    out = []
    t = start
    while t <= until_t:
        out.append(t)
        t += tick_s
    return out


def index_times(trades: list[Trade]) -> list[float]:
    return [x.t for x in trades]


def count_until(times: list[float], t: float) -> int:
    """How many trades have an event time at or before `t` (bisect over the sorted times)."""
    return bisect.bisect_right(times, t)


def iter_chunks(items: Iterable[Any], n: int) -> Iterable[list[Any]]:
    buf: list[Any] = []
    for it in items:
        buf.append(it)
        if len(buf) >= n:
            yield buf
            buf = []
    if buf:
        yield buf
