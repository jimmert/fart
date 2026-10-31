"""The backtest harness of milestone 12 (B.1): entries at the engine cadence, execution through the shared `Position`,
metrics with block-bootstrap intervals per day, a walk-forward split and one definition of "proven".

What a run does, for one hypothesis over a window of UTC days:

1. **Candidates**: the coins the hypothesis is about (new coins with trades, coins that crossed 75 SOL, the coins of a
   theme observation, the graduations), loaded one at a time: the coin row, its trades (all of them, with `received_at`
   so the as-of filter can be applied per tick), its migration, its tracker samples and its pool candles.
2. **Signal**: the rule of every variant is evaluated at the engine cadence (`tick_s`, 15 s) on the features knowable at
   that tick (`features.knowable` -> `features.replay_features`, the bundle flags, the creator's sells, the creator's
   history when the hypothesis needs it). The first tick at which a variant fires is its signal for that coin; one signal
   per coin per variant.
3. **Execution**: `paper.Position` on the coin's price path from the signal on (curve points from the trades, since
   milestone 13 the polls of the BondingCurve account between the trades (`path_points`, kind "poll": the curve is always
   the counterparty, so an entry or an exit may fill on one; a dev sell is only visible on a trade point), the graduation
   point, DexScreener samples and GeckoTerminal candles after it): the fill `entry_delay_s` after the signal on the first
   point then, the exits of the variant, every cost. A path that ends without an exit is an UNKNOWN outcome; a signal
   without a point to fill on within ten minutes is NO FILL. Both are counted apart, never as a result; their meaning did
   not change with the polls, only the paths got longer.
4. **Metrics** per variant: expected value per trade (SOL and % of the size), hit rate, median, p10, p90, maximum drawdown of
   the equity curve, cases, the 95% block-bootstrap interval of the mean with the UTC days as blocks (fixed seed), the
   lead time (signal to the peak within 24 h), the capacity (SOL until 5% impact at the signal), the unknown share.
5. **Walk-forward**: the days are split in two halves; the variant with the highest expected value on the first half (the
   selection) is the candidate; it is PROVEN only when, on the second half (the test), the lower bound of its interval is
   above 0 after costs, with at least `min_cases` cases, an unknown share under `max_unknown_share` and a median lead time
   of at least `min_lead_s`. Every variant's metrics are reported on both halves and on the whole window, so nothing is
   hidden; the number of variants tried is printed next to every verdict (with K variants, about K x 2.5% of them would
   clear a one-sided 2.5% bound by luck).
6. **Sensitivity**: the selected variant is re-run with the other entry delays (5/20/60/120 s) and sizes (0.1/0.5/1 SOL).
7. **Conditioning**: the cases of the selected variant are split by the hour of the signal (peak 13:00 to 02:00 UTC or
   not), the SOL/USD trend over the previous 24 h and the number of new coins in the signal's hour (terciles); splits,
   not separate rules.

Nothing here writes to the database; the report module writes the files.
"""
from __future__ import annotations

import json
import math
import random
import sqlite3
import statistics
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Callable, Iterable

from ..chain.curve import CurveParams, CurveState
from ..config import Settings
from ..db import asof_clause
from ..log import get_logger
from ..measure.paper import Costs, ExitRules, Position, PricePoint, capacity_sol
from ..util import day_bounds, day_key, hour_key
from . import features as rf
from . import hypotheses as hyps

log = get_logger("research")

ASOF = asof_clause()
ET = "COALESCE(block_time, received_at)"
TOTAL_TOKENS = 1_000_000_000          # whole tokens of a pump.fun coin (token_total_supply / token_unit)
SAMPLE_TOLERANCE_S = 150.0            # H6: a sample counts as "30 minutes after graduation" within this
PEAK_HORIZON_S = 86400.0              # the lead time looks for the peak within 24 h of the signal
MAX_ENTRY_WAIT_S = 600.0              # a signal without a point to fill on within ten minutes is no fill
LOG_EVERY = 5000
MAX_CASES_IN_JSON = 5000             # the JSON keeps at most this many cases (the oldest first); the metrics use all of them


@dataclass
class RunConfig:
    hypothesis: str
    days: list[str]
    entry_delay_s: float = 20.0
    exit_delay_s: float | None = None
    size_sol: float = 0.5
    tick_s: float = 15.0
    costs: Costs = field(default_factory=Costs)
    draws: int = 9999
    seed: int = 20261029
    alpha: float = 0.05
    min_cases: int = 100
    max_unknown_share: float = 0.2
    min_lead_s: float = 120.0
    min_select_cases: int = 20
    peak_hours_utc: tuple[int, ...] = (13, 14, 15, 16, 17, 18, 19, 20, 21, 22, 23, 0, 1)
    limit_coins: int | None = None
    entry_delays_s: tuple[float, ...] = (5.0, 20.0, 60.0, 120.0)
    sizes_sol: tuple[float, ...] = (0.1, 0.5, 1.0)
    creator_prior_cap: int = 50
    variants: list[str] | None = None     # restrict the grid (tests, sensitivity runs)
    days_requested: int | None = None     # milestone 13: the weekly run asks for 30 days and gets what the database has; the report says so
    window_note: str | None = None

    @classmethod
    def from_settings(cls, settings: Settings, hypothesis: str, days: list[str], **overrides: Any) -> "RunConfig":
        r = (settings.config("measure") or {}).get("research") or {}
        bs = r.get("bootstrap") or {}
        pv = r.get("proven") or {}
        kw: dict[str, Any] = dict(
            entry_delay_s=float(r.get("entry_delay_s", 20)), size_sol=float(r.get("size_sol", 0.5)), tick_s=float(r.get("tick_s", 15)),
            costs=Costs.from_config(r), draws=int(bs.get("draws", 9999)), seed=int(bs.get("seed", 20261029)), alpha=float(bs.get("alpha", 0.05)),
            min_cases=int(pv.get("min_cases", 100)), max_unknown_share=float(pv.get("max_unknown_share", 0.2)), min_lead_s=float(pv.get("min_lead_s", 120)),
            peak_hours_utc=tuple(int(h) for h in r.get("peak_hours_utc") or cls.peak_hours_utc),
            entry_delays_s=tuple(float(x) for x in r.get("entry_delays_s") or cls.entry_delays_s),
            sizes_sol=tuple(float(x) for x in r.get("sizes_sol") or cls.sizes_sol),
        )
        kw.update({k: v for k, v in overrides.items() if v is not None})
        return cls(hypothesis=str(hypothesis).upper(), days=list(days), **kw)


@dataclass
class Coin:
    mint: str
    name: str | None
    symbol: str | None
    creator: str | None
    created_t: float
    dev_buy_sol: float | None
    slot: int | None
    trades: list[rf.Trade]
    grad_t: float | None = None
    pool: str | None = None
    samples: list[dict[str, Any]] = field(default_factory=list)
    candles: list[dict[str, Any]] = field(default_factory=list)
    points: list[rf.PollPoint] = field(default_factory=list)                        # milestone 13: the polls of the curve account (path_points)
    paths: dict[Any, list[PricePoint]] = field(default_factory=dict, repr=False)      # price_path per (signal time, with polls) (the exits of one entry share it)


@dataclass
class Case:
    """One signal of one variant on one coin, with its execution."""
    variant: str
    group: str
    mint: str
    day: str
    signal_t: float
    hour_utc: int
    status: str                       # closed | unknown | no_fill
    pnl_sol: float | None
    pnl_pct: float | None
    exit_reason: str | None
    fees_sol: float
    entry_spot: float | None
    fill_price: float | None
    entry_impact: float | None
    venue: str | None
    lead_s: float | None
    peak_gain: float | None           # peak spot / entry spot - 1 within 24 h (what was there to take)
    capacity_sol: float | None
    exit_t: float | None
    hold_s: float | None
    sol_trend: str                    # up | down | unknown
    coins_in_hour: int | None
    name: str | None = None
    symbol: str | None = None
    features: dict[str, Any] = field(default_factory=dict)
    entry_kind: str | None = None     # milestone 13: the kind of the point the entry filled on (curve | poll | sample | candle)
    exit_kind: str | None = None      # ... and the final exit
    curve_source: str | None = None   # H2: the state the crossing was seen on (trade | poll)
    post_follow: bool | None = None   # the signal fell after the follow step started (its path could have polls); None when unknown

    def to_json(self) -> dict[str, Any]:
        return asdict(self)


# ----- statistics -----------------------------------------------------------------------------------------------------------
def bootstrap_mean(blocks: list[tuple[float, int]], draws: int, seed: int, alpha: float) -> dict[str, Any] | None:
    """Block bootstrap of a pooled mean sum / n with the blocks (days) resampled with replacement; deterministic for a seed;
    a draw with n == 0 is undefined and skipped. None without a defined draw."""
    n = len(blocks)
    if n == 0:
        return None
    rng = random.Random(int(seed))
    vals: list[float] = []
    for _ in range(int(draws)):
        s = 0.0
        k = 0
        for _ in range(n):
            b = blocks[rng.randrange(n)]
            s += float(b[0])
            k += int(b[1])
        if k > 0:
            vals.append(s / k)
    if not vals:
        return None
    vals.sort()
    k = len(vals) - 1
    lo = vals[int(math.floor(alpha / 2.0 * k))]
    hi = vals[int(math.ceil((1.0 - alpha / 2.0) * k))]
    tot_s = sum(float(b[0]) for b in blocks)
    tot_n = sum(int(b[1]) for b in blocks)
    return {"low": lo, "high": hi, "point": (tot_s / tot_n) if tot_n else None, "draws": int(draws), "defined_draws": len(vals), "seed": int(seed),
            "alpha": float(alpha), "blocks": n, "method": "blokbootstrap, UTC-dagen als blokken, percentielen van het gemiddelde per trade"}


def _quantile(sorted_vals: list[float], q: float) -> float | None:
    if not sorted_vals:
        return None
    if len(sorted_vals) == 1:
        return sorted_vals[0]
    pos = q * (len(sorted_vals) - 1)
    lo = int(math.floor(pos))
    hi = min(lo + 1, len(sorted_vals) - 1)
    return sorted_vals[lo] + (sorted_vals[hi] - sorted_vals[lo]) * (pos - lo)


def max_drawdown(pnls_in_time_order: list[float]) -> float:
    peak = 0.0
    eq = 0.0
    dd = 0.0
    for p in pnls_in_time_order:
        eq += p
        peak = max(peak, eq)
        dd = max(dd, peak - eq)
    return dd


def metrics(cases: list[Case], cfg: RunConfig, with_ci: bool = True) -> dict[str, Any]:
    """The figures of one variant over a set of cases (a half, or the whole window)."""
    known = [c for c in cases if c.status == "closed" and c.pnl_sol is not None]
    unknown = [c for c in cases if c.status == "unknown"]
    no_fill = [c for c in cases if c.status == "no_fill"]
    pnls = [float(c.pnl_sol) for c in sorted(known, key=lambda c: c.signal_t)]
    pcts = sorted(float(c.pnl_pct) for c in known if c.pnl_pct is not None)
    leads = sorted(float(c.lead_s) for c in cases if c.lead_s is not None and c.status != "no_fill")
    caps = sorted(float(c.capacity_sol) for c in cases if c.capacity_sol is not None)
    holds = sorted(float(c.hold_s) for c in known if c.hold_s is not None)
    n = len(known)
    denom = n + len(unknown)
    out: dict[str, Any] = {
        "signals": len(cases), "cases": n, "unknown": len(unknown), "no_fill": len(no_fill),
        "unknown_share": (len(unknown) / denom) if denom else None,
        "ev_sol": (sum(pnls) / n) if n else None, "ev_pct": (sum(pcts) / n) if n else None,
        "sum_sol": sum(pnls) if n else None,
        "hit_rate": (sum(1 for p in pnls if p > 0) / n) if n else None,
        "median_pct": _quantile(pcts, 0.5), "p10_pct": _quantile(pcts, 0.1), "p90_pct": _quantile(pcts, 0.9),
        "max_drawdown_sol": max_drawdown(pnls) if n else None,
        "lead_median_s": _quantile(leads, 0.5), "lead_n": len(leads),
        "capacity_median_sol": _quantile(caps, 0.5),
        "hold_median_s": _quantile(holds, 0.5),
        "fees_mean_sol": (sum(c.fees_sol for c in known) / n) if n else None,
        "exit_reasons": _count(c.exit_reason for c in known),
        "fill_sources": {"entry": _count(c.entry_kind for c in cases if c.entry_kind is not None), "exit": _count(c.exit_kind for c in known if c.exit_kind is not None)},
        "days": sorted({c.day for c in cases}),
    }
    if with_ci:
        by_day: dict[str, list[float]] = {}
        for c in known:
            by_day.setdefault(c.day, []).append(float(c.pnl_sol))
        blocks = [(sum(v), len(v)) for d, v in sorted(by_day.items())]
        out["ci"] = bootstrap_mean(blocks, cfg.draws, cfg.seed, cfg.alpha) if blocks else None
    return out


def _count(items: Iterable[Any]) -> dict[str, int]:
    out: dict[str, int] = {}
    for it in items:
        k = str(it)
        out[k] = out.get(k, 0) + 1
    return dict(sorted(out.items(), key=lambda kv: -kv[1]))


def proven_verdict(m: dict[str, Any], cfg: RunConfig) -> dict[str, Any]:
    """The definition of "bewezen" on a set of metrics (the test half): every criterion with its value and verdict."""
    ci = m.get("ci") or {}
    low = ci.get("low")
    crit = [
        {"key": "ci_low_positive", "label": "ondergrens 95%-interval verwachte waarde na kosten > 0 (toetshelft)", "value": low, "ok": low is not None and low > 0},
        {"key": "min_cases", "label": f"minstens {cfg.min_cases} gevallen (toetshelft)", "value": m.get("cases"), "ok": int(m.get("cases") or 0) >= cfg.min_cases},
        {"key": "max_unknown", "label": f"aandeel onbekende uitkomsten onder {round(100 * cfg.max_unknown_share)}%", "value": m.get("unknown_share"),
         "ok": m.get("unknown_share") is not None and float(m["unknown_share"]) < cfg.max_unknown_share},
        {"key": "min_lead", "label": f"mediane lead time (signaal tot piek) minstens {round(cfg.min_lead_s / 60)} minuten", "value": m.get("lead_median_s"),
         "ok": m.get("lead_median_s") is not None and float(m["lead_median_s"]) >= cfg.min_lead_s},
    ]
    return {"proven": all(c["ok"] for c in crit), "criteria": crit}


# ----- data loading ----------------------------------------------------------------------------------------------------------
def _coin_row(conn: sqlite3.Connection, mint: str) -> sqlite3.Row | None:
    return conn.execute(f"SELECT mint, name, symbol, creator, {ET} AS t, dev_buy_sol, slot FROM coins WHERE mint=?", (mint,)).fetchone()


def load_coin(conn: sqlite3.Connection, mint: str, with_path: bool = True) -> Coin | None:
    r = _coin_row(conn, mint)
    if r is None:
        return None
    c = Coin(str(r["mint"]), r["name"], r["symbol"], r["creator"], float(r["t"]), r["dev_buy_sol"], None if r["slot"] is None else int(r["slot"]),
             rf.load_trades(conn, mint))
    m = conn.execute(f"SELECT {ET} AS t, pool_address FROM migrations WHERE mint=?", (mint,)).fetchone()
    if m is not None:
        c.grad_t = float(m["t"])
        c.pool = m["pool_address"]
    if with_path:
        c.samples = [dict(s) for s in conn.execute("SELECT ts, mcap_usd, fdv_usd, liquidity_usd, sol_usd, json FROM mcap_samples WHERE mint=? ORDER BY ts", (mint,))]
        if c.pool and _table_exists(conn, "pool_candles"):
            c.candles = [dict(x) for x in conn.execute("SELECT minute, open, high, low, close, volume_usd FROM pool_candles WHERE pool=? ORDER BY minute", (c.pool,))]
    c.points = rf.load_points(conn, mint)   # milestone 13: the polls are part of the state a live tick sees, so they load with or without the path
    return c


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone() is not None


def sol_usd_near(conn: sqlite3.Connection, t: float, tolerance_s: float = 3600.0) -> float | None:
    r = conn.execute("SELECT sol_usd FROM mcap_samples WHERE sol_usd IS NOT NULL AND ts BETWEEN ? AND ? ORDER BY ABS(ts-?) LIMIT 1",
                     (t - tolerance_s, t + tolerance_s, t)).fetchone()
    return None if r is None else float(r[0])


def sample_price_sol(s: dict[str, Any]) -> float | None:
    """SOL per token from a DexScreener sample: market cap (fdv as the fallback) over the total supply, in SOL."""
    cap = s.get("mcap_usd") if s.get("mcap_usd") is not None else s.get("fdv_usd")
    su = s.get("sol_usd")
    if cap is None or not su or float(su) <= 0 or float(cap) <= 0:
        return None
    return float(cap) / float(su) / TOTAL_TOKENS


def sample_liquidity_sol(s: dict[str, Any]) -> float | None:
    liq, su = s.get("liquidity_usd"), s.get("sol_usd")
    if liq is None or not su or float(su) <= 0:
        return None
    return float(liq) / float(su) / 2.0


def sample_volume_h1(s: dict[str, Any]) -> float | None:
    try:
        js = json.loads(s.get("json") or "{}")
    except (TypeError, ValueError):
        return None
    v = js.get("volume_h1_usd") if isinstance(js, dict) else None
    return None if v is None else float(v)


def graduation_time(coin: Coin) -> float | None:
    """The graduation as the path knows it: the migration when the stream saw it, else (milestone 13) the first poll of the
    BondingCurve account that found `complete` (the stream missed the migration; the poll's time is an upper bound)."""
    if coin.grad_t is not None:
        return coin.grad_t
    for p in coin.points:
        if p.complete:
            return p.t
    return None


def price_path(coin: Coin, from_t: float, include_polls: bool = True) -> list[PricePoint]:
    """The realised price path of a coin from `from_t` on: curve points from the trades with reserves (the creator's sells
    flagged), the polls of the curve account between them (kind "poll", milestone 13; `include_polls=False` gives the path
    the stream alone would have given), the graduation point (the last curve state before the migration, or before the poll
    that found the curve complete), then the tracker samples and the candles after the graduation. Sorted by time; the
    future relative to the signal is what a position lives through."""
    pts: list[PricePoint] = []
    grad_t = graduation_time(coin)
    last_state: tuple[float, int, int] | None = None   # (t, virtual_sol, virtual_token) of the last curve state before the graduation
    for x in coin.trades:
        if not (x.virtual_sol and x.virtual_token):
            continue
        if grad_t is not None and x.t > grad_t:
            continue
        if last_state is None or x.t >= last_state[0]:
            last_state = (x.t, int(x.virtual_sol), int(x.virtual_token))
        if x.t >= from_t:
            pts.append(PricePoint(x.t, "curve", x.virtual_sol, x.virtual_token, dev_sell=(not x.is_buy and x.buyer is not None and x.buyer == coin.creator)))
    if include_polls:
        for p in coin.points:
            if not p.has_state or (grad_t is not None and p.t > grad_t):
                continue
            if last_state is None or p.t >= last_state[0]:
                last_state = (p.t, int(p.virtual_sol), int(p.virtual_token))
            if p.t >= from_t:
                pts.append(PricePoint(p.t, "poll", p.virtual_sol, p.virtual_token))
    if grad_t is not None and last_state is not None and grad_t >= from_t:
        pts.append(PricePoint(grad_t, "curve", last_state[1], last_state[2], graduation=True))
    if coin.grad_t is not None:
        sol_usd_ref: float | None = None
        liq_ref: float | None = None
        for s in coin.samples:
            ts = float(s["ts"])
            if ts < coin.grad_t or ts < from_t:
                continue
            p = sample_price_sol(s)
            if p is None:
                continue
            sol_usd_ref = float(s["sol_usd"])
            liq_ref = sample_liquidity_sol(s) or liq_ref
            pts.append(PricePoint(ts, "sample", price_sol=p, liquidity_sol=sample_liquidity_sol(s)))
        if coin.candles and sol_usd_ref:
            for cd in coin.candles:
                t = float(cd["minute"]) + 60.0
                if t < coin.grad_t or t < from_t or cd.get("close") is None:
                    continue
                pts.append(PricePoint(t, "candle", price_sol=float(cd["close"]) / sol_usd_ref, liquidity_sol=liq_ref))
    pts.sort(key=lambda p: (p.t, {"curve": 0, "poll": 1}.get(p.kind, 2)))
    return pts


def peak_after(path: list[PricePoint], params: CurveParams, from_t: float, horizon_s: float = PEAK_HORIZON_S) -> tuple[float | None, float | None]:
    """(time of the highest spot within the horizon, that spot)."""
    best_t = best = None
    for p in path:
        if p.t < from_t or p.t > from_t + horizon_s:
            continue
        s = p.spot(params)
        if s is not None and (best is None or s > best):
            best, best_t = s, p.t
    return best_t, best


# ----- the run ---------------------------------------------------------------------------------------------------------------
class Harness:
    def __init__(self, conn: sqlite3.Connection, settings: Settings, cfg: RunConfig, clock: Callable[[], float] = time.time):
        self.conn = conn
        self.settings = settings
        self.cfg = cfg
        self.clock = clock
        self.params = CurveParams.from_config(settings.config("pumpfun"))
        self.hyp = hyps.get(cfg.hypothesis)
        self.variants = [v for v in self.hyp.variants if cfg.variants is None or v.id in cfg.variants]
        mcfg = settings.config("measure") or {}
        self.method_version = str(mcfg.get("method_version", "m1.0"))
        self.h24 = max(int(h) for h in mcfg.get("horizons_s") or [86400])
        self.dev_sell_window_s = float((mcfg.get("creator_stats") or {}).get("dev_sell_window_s", rf.DEV_SELL_WINDOW_S))
        self.start = day_bounds(cfg.days[0])[0]
        self.end = day_bounds(cfg.days[-1])[1]
        self.creator_cache: dict[tuple[str, str], dict[str, Any]] = {}
        self.coins_per_hour: dict[str, int] = {}
        self.sol_trend_cache: dict[str, str] = {}
        self.stats: dict[str, Any] = {"coins_considered": 0, "coins_with_trades": 0, "coins_prefiltered": 0, "coins_evaluated": 0, "ticks": 0,
                                      "signals": 0, "seconds": None, "theme_decisions": 0, "graduations": 0, "signals_with_polls": 0}
        self.follow_started_at = follow_started_at(conn)
        self.has_points = _table_exists(conn, "path_points")

    # ----- candidates -----------------------------------------------------------------------------------------------------
    def candidate_mints(self) -> list[str]:
        h = self.hyp
        if h.id == "H3":
            return sorted(self.theme_roles().keys())
        if h.id == "H6" or h.post_graduation:
            return [str(r[0]) for r in self.conn.execute(f"SELECT mint FROM migrations WHERE {ET} >= ? AND {ET} < ? ORDER BY {ET}", (self.start, self.end))]
        if h.id == "H2":
            # coins whose curve passed 75 SOL (virtual 105 SOL) inside the window, seen on a trade or (milestone 13) on a poll
            rows = self.conn.execute("SELECT DISTINCT mint FROM trades WHERE received_at >= ? AND received_at < ? AND virtual_sol >= ?",
                                     (self.start - 60, self.end + 60, int((30.0 + hyps.H2_CROSS_SOL) * 1e9))).fetchall()
            mints = {str(r[0]) for r in rows}
            if self.has_points:
                mints.update(str(r[0]) for r in self.conn.execute("SELECT DISTINCT mint FROM path_points WHERE t >= ? AND t < ? AND virtual_sol >= ?",
                                                                  (self.start - 60, self.end + 60, int((30.0 + hyps.H2_CROSS_SOL) * 1e9))))
            return sorted(mints)
        # new coins of the window that have trades
        with_trades = {str(r[0]) for r in self.conn.execute("SELECT DISTINCT mint FROM trades WHERE received_at >= ? AND received_at < ?", (self.start - 60, self.end + 3600))}
        rows = self.conn.execute(f"SELECT mint FROM coins WHERE {ET} >= ? AND {ET} < ? ORDER BY {ET}", (self.start, self.end)).fetchall()
        self.stats["coins_considered"] = len(rows)
        return [str(r[0]) for r in rows if str(r[0]) in with_trades]

    def theme_roles(self) -> dict[str, dict[str, Any]]:
        """H3: mint -> {role, term, decision_as_of, signal_t} from the theme observations of the window."""
        if getattr(self, "_roles", None) is not None:
            return self._roles
        from ..engine.meta import load_stopwords, tokenize

        ecfg = self.settings.config("engine")
        mcfg = ecfg.get("meta") or {}
        window_s = float(mcfg.get("window_s", 10800))
        min_len = int(mcfg.get("min_token_len", 4))
        stop = load_stopwords(self.settings.config("stopwords"))
        roles: dict[str, dict[str, Any]] = {}
        decs = self.conn.execute("SELECT term, as_of FROM decisions WHERE kind='observe_meta' AND verdict='observe' AND as_of >= ? AND as_of < ? ORDER BY as_of",
                                 (self.start, self.end)).fetchall()
        self.stats["theme_decisions"] = len(decs)
        seen_terms: set[str] = set()
        for d in decs:
            term, as_of = str(d["term"]), float(d["as_of"])
            if term in seen_terms:
                continue   # the first observation of a theme in the window defines its leader; later cooldown repeats add nothing
            seen_terms.add(term)
            cands = []
            for r in self.conn.execute(f"SELECT mint, name, symbol, {ET} AS t FROM coins WHERE {ASOF} AND {ET} > ? AND {ET} <= ? ORDER BY t",
                                       (as_of, as_of, as_of - window_s, as_of)):
                if term in tokenize(r["name"], r["symbol"], stop, min_len):
                    cands.append((str(r["mint"]), float(r["t"])))
            if not cands:
                continue
            scored = []
            for mint, t in cands:
                tr = rf.knowable(rf.load_trades(self.conn, mint), as_of)
                if not tr:
                    continue
                first3 = rf.window_stats([x for x in tr if x.t <= t + 180], t + 180, 180.0)
                scored.append((mint, t, first3.unique_buyers))
            if not scored:
                continue
            strongest = max(scored, key=lambda s: (s[2], -s[1]))
            first = min(scored, key=lambda s: s[1])
            for mint, t, _ in scored:
                role = "leader_strongest" if mint == strongest[0] else ("leader_first" if mint == first[0] else None)
                if role is not None and mint not in roles:
                    roles[mint] = {"role": role, "term": term, "decision_as_of": as_of, "signal_t": as_of, "leader_created_t": t}
            # copies: the coins of the term created more than ten minutes after the first leader, up to two hours after the observation
            for r in self.conn.execute(f"SELECT mint, name, symbol, {ET} AS t FROM coins WHERE {ET} > ? AND {ET} <= ? ORDER BY t",
                                       (first[1] + 600.0, as_of + 7200.0)):
                mint = str(r["mint"])
                if mint in roles or term not in tokenize(r["name"], r["symbol"], stop, min_len):
                    continue
                roles[mint] = {"role": "copy", "term": term, "decision_as_of": as_of, "signal_t": float(r["t"]) + 120.0, "leader_created_t": first[1]}
        self._roles = roles
        return roles

    # ----- conditioning ---------------------------------------------------------------------------------------------------
    def _coins_in_hour(self, t: float) -> int:
        hk = hour_key(t)
        if hk not in self.coins_per_hour:
            lo = t - (t % 3600)
            self.coins_per_hour[hk] = int(self.conn.execute(f"SELECT COUNT(*) FROM coins WHERE received_at >= ? AND received_at < ? AND {ET} >= ? AND {ET} < ?",
                                                            (lo - 60, lo + 3660, lo, lo + 3600)).fetchone()[0])
        return self.coins_per_hour[hk]

    def _sol_trend(self, t: float) -> str:
        hk = hour_key(t)
        if hk not in self.sol_trend_cache:
            now_p = sol_usd_near(self.conn, t)
            then_p = sol_usd_near(self.conn, t - 86400)
            self.sol_trend_cache[hk] = "unknown" if now_p is None or then_p is None else ("up" if now_p >= then_p else "down")
        return self.sol_trend_cache[hk]

    def _creator(self, coin: Coin, as_of: float) -> dict[str, Any]:
        key = (coin.creator or "", day_key(as_of))
        if key not in self.creator_cache:
            self.creator_cache[key] = creator_history_capped(self.conn, coin, as_of, self.method_version, self.h24, self.dev_sell_window_s, self.cfg.creator_prior_cap)
        return self.creator_cache[key]

    # ----- the replay of one coin -----------------------------------------------------------------------------------------
    def signals_for(self, coin: Coin) -> dict[str, tuple[float, dict[str, Any]]]:
        """variant id -> (signal time, the features at the signal): the first tick at which the variant fires."""
        h = self.hyp
        out: dict[str, tuple[float, dict[str, Any]]] = {}
        pending = {v.id: v for v in self.variants}
        if h.id == "H6":
            return self._signals_h6(coin, pending)
        if h.post_graduation:
            return self._signals_post_grad(coin, pending)
        if h.id == "H3":
            role = self.theme_roles().get(coin.mint)
            if role is None:
                return out
            ticks = [float(role["signal_t"])]
        elif h.id == "H2":
            hi = int((30.0 + 70.0) * 1e9)
            first_hi = min([x.t for x in coin.trades if x.virtual_sol and x.virtual_sol >= hi] + [p.t for p in coin.points if p.has_state and p.virtual_sol >= hi], default=None)
            if first_hi is None:
                return out
            last_seen = max([coin.trades[-1].t if coin.trades else first_hi] + [p.t for p in coin.points if p.has_state])   # the polls extend what the stream saw
            until = coin.grad_t if coin.grad_t is not None else last_seen
            ticks = rf.tick_times(first_hi - self.cfg.tick_s, min(until + self.cfg.tick_s, first_hi + 6 * 3600), self.cfg.tick_s, self.cfg.tick_s)
        else:
            until = coin.created_t + h.horizon_s
            if coin.grad_t is not None:
                until = min(until, coin.grad_t)
            ticks = rf.tick_times(coin.created_t, until, self.cfg.tick_s, h.first_tick_s)
        f_prev: dict[str, Any] | None = None
        prev_t: float | None = None
        for t in ticks:
            if t < self.start or t >= self.end:
                f_prev, prev_t = None, t
                continue
            self.stats["ticks"] += 1
            kn = rf.knowable(coin.trades, t)
            if h.id != "H3" and not kn:
                f_prev, prev_t = None, t
                continue
            f = self._features_at(coin, kn, t)
            if prev_t is not None and f_prev is None and h.id in ("H1", "H2"):
                kn_prev = rf.knowable(coin.trades, prev_t)
                f_prev = self._features_at(coin, kn_prev, prev_t) if kn_prev else None
            bundle = rf.bundle_flags(kn, t, coin.created_t, coin.dev_buy_sol, coin.slot, graduation_sol=self.params.graduation_real_sol / self.params.sol_unit, creator=coin.creator)
            dev = rf.dev_sell(kn, t, coin.created_t, coin.creator, self.dev_sell_window_s)
            graduated = coin.grad_t is not None and coin.grad_t <= t
            ctx = hyps.Ctx(coin.mint, coin.created_t, t, f, f_prev, bundle, dev, graduated, coin.grad_t, None,
                           self.theme_roles().get(coin.mint) if h.needs_theme else None, None, None, int(time.gmtime(t).tm_hour))
            if h.needs_creator and int(f.get("trades_total") or 0) >= 3:
                ctx.creator = self._creator(coin, t)
            for vid in list(pending):
                v = pending[vid]
                try:
                    fired = bool(h.fires(v, ctx))
                except Exception:  # noqa: BLE001  a predicate bug must never turn into a silent miss
                    log.error("research.predicate_failed", hypothesis=h.id, variant=vid, mint=coin.mint, exc_info=True)
                    fired = False
                if fired:
                    out[vid] = (t, {**f, **bundle, **dev, **(ctx.creator or {}), "theme": ctx.theme})
                    del pending[vid]
            if not pending:
                break
            f_prev, prev_t = f, t
        return out

    def _features_at(self, coin: Coin, kn: list[rf.Trade], t: float) -> dict[str, Any]:
        """The replay features at `t`; with polls of the curve account (milestone 13) the newer of the last trade and the last
        poll carries the curve state (`curve_source` says which), so H2 may see its crossing on a poll."""
        f = rf.replay_features(kn, t, coin.created_t, self.params)
        if coin.points:
            rf.apply_poll_state(f, coin.points, t, self.params)
        return f

    def _signals_h6(self, coin: Coin, pending: dict[str, hyps.Variant]) -> dict[str, tuple[float, dict[str, Any]]]:
        out: dict[str, tuple[float, dict[str, Any]]] = {}
        if coin.grad_t is None or not (self.start <= coin.grad_t < self.end):
            return out
        last = rf.last_state(coin.trades, coin.grad_t)
        grad_price = None if last is None else CurveState(int(last.virtual_sol), int(last.virtual_token)).price_sol(self.params)
        target = coin.grad_t + hyps.H6_AFTER_S
        near = [s for s in coin.samples if abs(float(s["ts"]) - target) <= SAMPLE_TOLERANCE_S and sample_price_sol(s) is not None]
        if not near or grad_price is None:
            return out
        s = min(near, key=lambda s: abs(float(s["ts"]) - target))
        sample = {"ts": float(s["ts"]), "price_sol": sample_price_sol(s), "volume_h1_usd": sample_volume_h1(s), "liquidity_sol": sample_liquidity_sol(s)}
        t = float(s["ts"])
        f = {"as_of": t, "age_s": round(t - coin.created_t, 1), "price_sol": sample["price_sol"], "grad_price_sol": grad_price, "volume_h1_usd": sample["volume_h1_usd"]}
        ctx = hyps.Ctx(coin.mint, coin.created_t, t, f, None, {}, {}, True, coin.grad_t, None, None, sample, grad_price, int(time.gmtime(t).tm_hour))
        for vid, v in list(pending.items()):
            if self.hyp.fires(v, ctx):
                out[vid] = (t, {**f, "sample": sample})
        return out

    def _signals_post_grad(self, coin: Coin, pending: dict[str, hyps.Variant]) -> dict[str, tuple[float, dict[str, Any]]]:
        """Milestone 13 (H8-H10): one tick per minute after the graduation on the post-graduation path (candles, samples), with the
        price as the last point at or before the tick, the peak since the graduation, the drawdown from it, the volume since the
        graduation (candles, else the sample's pool volume) and of the last five minutes (candles only), the creator's sells on
        the curve up to the graduation and, for H10, the theme role by graduation order. Nothing is interpolated."""
        out: dict[str, tuple[float, dict[str, Any]]] = {}
        h = self.hyp
        if coin.grad_t is None or not (self.start <= coin.grad_t < self.end):
            return out
        path = price_path(coin, coin.grad_t)
        grad_pt = next((p for p in path if p.graduation), None)
        grad_price = grad_pt.spot(self.params) if grad_pt is not None else None
        if grad_price is None:
            return out
        post = [(p.t, p.spot(self.params)) for p in path if p.t > coin.grad_t and p.spot(self.params) is not None]
        candles = sorted(((float(c["minute"]), c.get("volume_usd")) for c in coin.candles if c.get("minute") is not None and float(c["minute"]) >= coin.grad_t - 60), key=lambda x: x[0])
        samples = sorted(((float(s["ts"]), sample_volume_h1(s)) for s in coin.samples if float(s["ts"]) >= coin.grad_t), key=lambda x: x[0])
        dev_sold = any(x.t <= coin.grad_t and not x.is_buy and x.buyer is not None and x.buyer == coin.creator for x in coin.trades)
        role = self.theme_grad_roles().get(coin.mint) if h.needs_theme else None
        if h.needs_theme and role is None:
            return out
        self.stats["graduations"] += 1
        minutes = int(h.post_grad_horizon_s // 60)
        for k in range(1, minutes + 1):
            t = coin.grad_t + 60.0 * k
            if t < self.start or t >= self.end + 86400:
                continue
            self.stats["ticks"] += 1
            seen = [(pt, sp) for pt, sp in post if pt <= t]
            price = seen[-1][1] if seen else grad_price
            peak = max([grad_price] + [sp for _, sp in seen])
            vol_c = [v for m, v in candles if m + 60.0 <= t and v is not None] if candles else None
            vol_since = sum(vol_c) if vol_c else None
            if vol_since is None and samples:
                last_s = [v for ts, v in samples if ts <= t and v is not None]
                vol_since = last_s[-1] if last_s else None
            vol_5m = sum(v for m, v in candles if m + 60.0 <= t and m + 60.0 > t - 300.0 and v is not None) if candles and any(m + 60.0 <= t for m, _ in candles) else None
            f = {"as_of": t, "age_s": round(t - coin.created_t, 1), "minutes_after_graduation": round((t - coin.grad_t) / 60.0, 3), "price_sol": price, "grad_price_sol": grad_price,
                 "peak_since_graduation_sol": peak, "drawdown_from_peak": None if not peak else round(1.0 - price / peak, 4), "points_since_graduation": len(seen),
                 "volume_since_graduation_usd": vol_since, "volume_5m_usd": vol_5m, "dev_sold_before_graduation": dev_sold, "theme_role": role.get("role") if role else None,
                 "has_candles": bool(candles)}
            ctx = hyps.Ctx(coin.mint, coin.created_t, t, f, None, {}, {}, True, coin.grad_t, None, role, None, grad_price, int(time.gmtime(t).tm_hour))
            for vid in list(pending):
                try:
                    fired = bool(h.fires(pending[vid], ctx))
                except Exception:  # noqa: BLE001
                    log.error("research.predicate_failed", hypothesis=h.id, variant=vid, mint=coin.mint, exc_info=True)
                    fired = False
                if fired:
                    out[vid] = (t, {**f, "theme": role})
                    del pending[vid]
            if not pending:
                break
        return out

    def theme_grad_roles(self) -> dict[str, dict[str, Any]]:
        """H10: mint -> {role: leader | copy, term, decision_as_of} by graduation order: of the coins that carry a theme's word (the
        coins of the window before the observation and the copies up to two hours after it) the first to graduate is the leader,
        every later graduation of the same theme a copy. The first theme a coin belongs to wins."""
        if getattr(self, "_grad_roles", None) is not None:
            return self._grad_roles
        from ..engine.meta import load_stopwords, tokenize

        ecfg = self.settings.config("engine")
        mcfg = ecfg.get("meta") or {}
        window_s = float(mcfg.get("window_s", 10800))
        min_len = int(mcfg.get("min_token_len", 4))
        stop = load_stopwords(self.settings.config("stopwords"))
        roles: dict[str, dict[str, Any]] = {}
        decs = self.conn.execute("SELECT term, as_of FROM decisions WHERE kind='observe_meta' AND verdict='observe' AND as_of >= ? AND as_of < ? ORDER BY as_of",
                                 (self.start - window_s, self.end)).fetchall()
        seen_terms: set[str] = set()
        for d in decs:
            term, as_of = str(d["term"]), float(d["as_of"])
            if term in seen_terms:
                continue
            seen_terms.add(term)
            grads = []
            for r in self.conn.execute(f"SELECT c.mint, c.name, c.symbol, {ET.replace('block_time', 'm.block_time').replace('received_at', 'm.received_at')} AS gt FROM coins c"
                                       f" JOIN migrations m ON m.mint = c.mint WHERE COALESCE(c.block_time, c.received_at) > ? AND COALESCE(c.block_time, c.received_at) <= ? ORDER BY gt",
                                       (as_of - window_s, as_of + 7200.0)):
                if term in tokenize(r["name"], r["symbol"], stop, min_len):
                    grads.append((str(r["mint"]), float(r["gt"])))
            for i, (mint, gt) in enumerate(grads):
                if mint not in roles:
                    roles[mint] = {"role": "leader" if i == 0 else "copy", "term": term, "decision_as_of": as_of, "graduated_t": gt}
        self._grad_roles = roles
        return roles

    def execute(self, coin: Coin, variant: hyps.Variant, signal_t: float, feats: dict[str, Any], entry_delay_s: float | None = None,
                size_sol: float | None = None, include_polls: bool = True) -> Case:
        delay = self.cfg.entry_delay_s if entry_delay_s is None else float(entry_delay_s)
        size = self.cfg.size_sol if size_sol is None else float(size_sol)
        key = (signal_t, bool(include_polls))
        path = coin.paths.get(key)
        if path is None:
            path = coin.paths[key] = price_path(coin, signal_t, include_polls=include_polls)
        pos = Position(signal_t, size, self.params, variant.rules, self.cfg.costs, delay, self.cfg.exit_delay_s if self.cfg.exit_delay_s is not None else delay, MAX_ENTRY_WAIT_S)
        pos.feed_all(path)
        status = "closed" if pos.status == "closed" else ("no_fill" if pos.status == "no_fill" else "unknown")
        entry = pos.entry
        peak_t, peak = peak_after(path, self.params, signal_t)
        entry_spot = entry.spot if entry is not None else None
        vs, vt = feats.get("virtual_sol"), feats.get("virtual_token")
        cap = capacity_sol(CurveState(int(vs), int(vt)), self.params) if vs and vt else None
        compact = {k: feats.get(k) for k in ("unique_buyers_5m", "net_sol_5m", "largest_buyer_share_15m", "dev_buy_frac_of_curve", "bundled_any", "bundle_reasons",
                                             "dev_first_sell_t", "mcap_sol", "graduation_progress", "curve_sol", "trades_total", "creator_track_record",
                                             "creator_graduations", "creator_winners", "theme", "volume_h1_usd", "curve_source", "minutes_after_graduation",
                                             "drawdown_from_peak", "volume_since_graduation_usd", "volume_5m_usd", "dev_sold_before_graduation", "theme_role") if k in feats}
        last_sell = next((f for f in reversed(pos.fills) if f.kind == "sell"), None) if status == "closed" else None
        return Case(variant.id, variant.group, coin.mint, day_key(signal_t), signal_t, int(time.gmtime(signal_t).tm_hour), status, pos.pnl_sol, pos.pnl_pct,
                    pos.exit_reason, round(pos.fees_sol, 9), entry_spot, entry.fill_price if entry else None, entry.impact if entry else None,
                    entry.venue if entry else None, None if peak_t is None else round(peak_t - signal_t, 1),
                    None if peak is None or not entry_spot else round(peak / entry_spot - 1.0, 4), None if cap is None else round(cap, 4),
                    pos.exit_t, None if pos.exit_t is None or entry is None else round(pos.exit_t - entry.t, 1), self._sol_trend(signal_t), self._coins_in_hour(signal_t),
                    coin.name, coin.symbol, compact, entry.point_kind if entry else None, last_sell.point_kind if last_sell else None, feats.get("curve_source"),
                    None if self.follow_started_at is None else signal_t >= self.follow_started_at)

    def prefilter(self, coin: Coin) -> bool:
        """Cheap necessary conditions per hypothesis so most coins skip the tick replay (a coin that never meets them cannot fire)."""
        h = self.hyp
        if h.id in ("H1", "H5", "H7", "H4"):
            early = [x for x in coin.trades if x.t <= coin.created_t + h.horizon_s]
            if h.id == "H4":
                return len(early) >= 3
            buyers = {x.buyer for x in early if x.is_buy and x.buyer}
            need = min(int(v.entry.get("min_buyers", 0)) for v in self.variants) if self.variants else 0
            return len(buyers) >= need
        if h.post_graduation:
            return coin.grad_t is not None
        return bool(coin.trades) or h.id == "H6"

    def run(self) -> dict[str, Any]:
        t0 = time.perf_counter()
        mints = self.candidate_mints()
        if self.cfg.limit_coins:
            mints = mints[: int(self.cfg.limit_coins)]
        self.stats["coins_with_trades"] = len(mints)
        cases: list[Case] = []
        signals: list[tuple[Coin, str, float, dict[str, Any]]] = []
        for i, mint in enumerate(mints):
            coin = load_coin(self.conn, mint)
            if coin is None:
                continue
            if not self.prefilter(coin):
                self.stats["coins_prefiltered"] += 1
                continue
            self.stats["coins_evaluated"] += 1
            sigs = self.signals_for(coin)
            for vid, (t, feats) in sigs.items():
                v = next(x for x in self.variants if x.id == vid)
                cases.append(self.execute(coin, v, t, feats))
                signals.append((coin, vid, t, feats))
            if (i + 1) % LOG_EVERY == 0:
                log.info("research.progress", hypothesis=self.hyp.id, coins=i + 1, of=len(mints), signals=len(cases), seconds=round(time.perf_counter() - t0, 1))
        self.stats["signals"] = len(cases)
        self._signals = signals
        result = self.evaluate(cases)
        self.stats["seconds"] = round(time.perf_counter() - t0, 2)
        result["stats"] = dict(self.stats)
        return result

    # ----- evaluation -----------------------------------------------------------------------------------------------------
    def split_days(self) -> tuple[list[str], list[str]]:
        days = list(self.cfg.days)
        k = (len(days) + 1) // 2
        return days[:k], days[k:]

    def evaluate(self, cases: list[Case]) -> dict[str, Any]:
        sel_days, test_days = self.split_days()
        sel_set, test_set = set(sel_days), set(test_days)
        by_var: dict[str, list[Case]] = {v.id: [] for v in self.variants}
        for c in cases:
            by_var.setdefault(c.variant, []).append(c)
        rows = []
        for v in self.variants:
            cs = by_var.get(v.id, [])
            full = metrics(cs, self.cfg)
            sel = metrics([c for c in cs if c.day in sel_set], self.cfg)
            test = metrics([c for c in cs if c.day in test_set], self.cfg)
            verdict = proven_verdict(test, self.cfg) if test_days else {"proven": False, "criteria": [], "note": "geen toetshelft: te weinig dagen voor een walk-forward"}
            rows.append({"variant": v.id, "label": v.label_nl, "group": v.group, "exit": v.exit, "exit_label": hyps.EXIT_LABELS_NL.get(v.exit, v.exit),
                         "exit_rules": v.rules.to_dict(), "exit_text": v.rules.describe_nl(), "entry": dict(v.entry), "full": full, "selection": sel, "test": test, "verdict": verdict,
                         "follow_split": self.follow_split(cs)})
        # the walk-forward choice: the best expected value on the selection half among variants with enough cases there
        eligible = [r for r in rows if int(r["selection"].get("cases") or 0) >= self.cfg.min_select_cases and r["selection"].get("ev_sol") is not None]
        selected = max(eligible, key=lambda r: float(r["selection"]["ev_sol"])) if eligible else None
        proven = [r["variant"] for r in rows if r["verdict"].get("proven")]
        by_group: dict[str, list[Case]] = {}
        for c in cases:
            by_group.setdefault(c.group, []).append(c)
        groups = []
        for g in sorted(by_group):
            m = metrics(by_group[g], self.cfg, with_ci=False)
            groups.append({"group": g, **{k: m.get(k) for k in ("signals", "cases", "unknown", "no_fill", "ev_sol", "hit_rate", "median_pct", "lead_median_s", "capacity_median_sol")}})
        exit_reasons = _count(c.exit_reason for c in cases if c.status == "closed" and c.exit_reason)
        kept = sorted(cases, key=lambda c: (c.signal_t, c.variant))[:MAX_CASES_IN_JSON]
        out = {
            "hypothesis": {"id": self.hyp.id, "title": self.hyp.title_nl, "statement": self.hyp.statement_nl, "scope": self.hyp.scope_nl,
                           "expectation": self.hyp.expectation_nl, "notes": self.hyp.notes_nl},
            "config": {"days": self.cfg.days, "selection_days": sel_days, "test_days": test_days, "entry_delay_s": self.cfg.entry_delay_s,
                       "exit_delay_s": self.cfg.exit_delay_s if self.cfg.exit_delay_s is not None else self.cfg.entry_delay_s, "size_sol": self.cfg.size_sol,
                       "tick_s": self.cfg.tick_s, "costs": asdict(self.cfg.costs), "curve_fee_bps": self.params.trade_fee_bps,
                       "bootstrap": {"draws": self.cfg.draws, "seed": self.cfg.seed, "alpha": self.cfg.alpha},
                       "proven": {"min_cases": self.cfg.min_cases, "max_unknown_share": self.cfg.max_unknown_share, "min_lead_s": self.cfg.min_lead_s},
                       "min_select_cases": self.cfg.min_select_cases, "variants_tested": len(self.variants), "variants_in_grid": len(self.hyp.variants),
                       "limit_coins": self.cfg.limit_coins, "days_requested": self.cfg.days_requested, "window_note": self.cfg.window_note,
                       "follow_started_at": self.follow_started_at, "poll_points_in_window": self.poll_points_in_window()},
            "variants": rows,
            "selected": None if selected is None else selected["variant"],
            "selected_proven": bool(selected and selected["verdict"].get("proven")),
            "proven_variants": proven,
            "multiplicity_note": (f"{len(self.variants)} varianten getoetst; bij louter toeval zou ongeveer {len(self.variants) * self.cfg.alpha / 2:.1f} daarvan "
                                  f"een ondergrens boven 0 laten zien. Alleen de vooraf gekozen variant (beste verwachte waarde op de keuzehelft) telt als toets; "
                                  f"de andere kolommen staan er om te zien, niet om uit te kiezen."),
            "groups": groups,
            "exit_reasons": exit_reasons,
            "cases": [c.to_json() for c in kept],
            "cases_total": len(cases),
            "cases_truncated": len(cases) > len(kept),
        }
        if selected is not None:
            sel_cases = by_var.get(selected["variant"], [])
            out["conditioning"] = self.conditioning(sel_cases)
            out["sensitivity"] = self.sensitivity(selected["variant"])
        return out

    def follow_split(self, cases: list[Case]) -> dict[str, Any] | None:
        """Milestone 13: the signals before and after the follow step started (kv `follow.started_at`), with their unknown shares;
        older signals never get a path with hindsight, so the two halves are the honest comparison. None before the first run."""
        if self.follow_started_at is None:
            return None
        out: dict[str, Any] = {"since": self.follow_started_at}
        for key, sel in (("before", [c for c in cases if c.post_follow is False]), ("after", [c for c in cases if c.post_follow])):
            m = metrics(sel, self.cfg, with_ci=False)
            out[key] = {k: m.get(k) for k in ("signals", "cases", "unknown", "no_fill", "unknown_share", "ev_sol", "hit_rate", "fill_sources")}
        return out

    def poll_points_in_window(self) -> int | None:
        if not self.has_points:
            return None
        return int(self.conn.execute("SELECT COUNT(*) FROM path_points WHERE t >= ? AND t < ?", (self.start, self.end + 86400)).fetchone()[0])

    def conditioning(self, cases: list[Case]) -> dict[str, Any]:
        peak = set(self.cfg.peak_hours_utc)
        counts = sorted(c.coins_in_hour for c in cases if c.coins_in_hour is not None)
        t1, t2 = _quantile(counts, 1 / 3), _quantile(counts, 2 / 3)

        def tercile(n: int | None) -> str:
            if n is None or t1 is None or t2 is None:
                return "unknown"
            return "low" if n <= t1 else ("high" if n > t2 else "mid")

        groups = {
            "hour": {"label": "tijd van de dag", "splits": {"peak": "piekuren 13:00 tot 02:00 UTC", "off": "buiten de piekuren"},
                     "assign": lambda c: "peak" if c.hour_utc in peak else "off"},
            "sol_trend": {"label": "SOL/USD-trend over de vorige 24 uur", "splits": {"up": "SOL hoger dan 24 u eerder", "down": "SOL lager", "unknown": "onbekend"},
                          "assign": lambda c: c.sol_trend},
            "coins_per_hour": {"label": "nieuwe munten in het uur van het signaal (tercielen)", "splits": {"low": f"laag (≤ {t1:.0f})" if t1 is not None else "laag", "mid": "midden", "high": f"hoog (> {t2:.0f})" if t2 is not None else "hoog", "unknown": "onbekend"},
                               "assign": lambda c: tercile(c.coins_in_hour)},
        }
        out = {}
        for key, g in groups.items():
            rows = []
            for split, label in g["splits"].items():
                cs = [c for c in cases if g["assign"](c) == split]
                if not cs:
                    continue
                m = metrics(cs, self.cfg, with_ci=False)
                rows.append({"split": split, "label": label, **{k: m.get(k) for k in ("signals", "cases", "unknown", "ev_sol", "ev_pct", "hit_rate", "median_pct")}})
            out[key] = {"label": g["label"], "rows": rows}
        return out

    def sensitivity(self, variant_id: str) -> dict[str, Any]:
        v = next(x for x in self.variants if x.id == variant_id)
        sigs = [(coin, vid, t, f) for coin, vid, t, f in getattr(self, "_signals", []) if vid == variant_id]
        delays = []
        for d in self.cfg.entry_delays_s:
            cs = [self.execute(coin, v, t, f, entry_delay_s=d) for coin, _, t, f in sigs]
            m = metrics(cs, self.cfg, with_ci=False)
            delays.append({"entry_delay_s": d, **{k: m.get(k) for k in ("cases", "unknown", "no_fill", "ev_sol", "ev_pct", "hit_rate", "median_pct")}})
        sizes = []
        for s in self.cfg.sizes_sol:
            cs = [self.execute(coin, v, t, f, size_sol=s) for coin, _, t, f in sigs]
            m = metrics(cs, self.cfg, with_ci=False)
            impacts = sorted(float(c.entry_impact) for c in cs if c.entry_impact is not None)
            sizes.append({"size_sol": s, **{k: m.get(k) for k in ("cases", "ev_sol", "ev_pct", "hit_rate")}, "entry_impact_median": _quantile(impacts, 0.5)})
        # milestone 13: the same signals on the path the stream alone gives (no poll points), so what the polls change is visible
        sources = []
        for label, with_polls in (("alle punten (trades, polls, samples, candles)", True), ("alleen trade-punten (zonder polls)", False)):
            cs = [self.execute(coin, v, t, f, include_polls=with_polls) for coin, _, t, f in sigs]
            m = metrics(cs, self.cfg, with_ci=False)
            sources.append({"label": label, "include_polls": with_polls, **{k: m.get(k) for k in ("signals", "cases", "unknown", "no_fill", "unknown_share", "ev_sol", "ev_pct", "hit_rate", "median_pct", "fill_sources")}})
        return {"entry_delay": delays, "size": sizes, "sources": sources}


def creator_history_capped(conn: sqlite3.Connection, coin: Coin, as_of: float, method_version: str, h24: int, dev_sell_window_s: float, cap: int) -> dict[str, Any]:
    """`features.creator_history` with the bundle / dump scan bounded to the creator's last `cap` earlier coins (a serial
    deployer with hundreds of coins would otherwise cost hundreds of trade loads per signal); the counts of coins,
    graduations and winners are over all earlier coins."""
    base = rf.creator_history.__wrapped__ if hasattr(rf.creator_history, "__wrapped__") else None  # pragma: no cover
    out: dict[str, Any] = {"creator_coins": None, "creator_coins_24h": None, "creator_graduations": None, "creator_winners": None,
                           "creator_not_winners": None, "creator_bundled": None, "creator_dev_sold_early": None, "creator_prev_coin_age_s": None,
                           "creator_track_record": None, "creator_scan_capped": False}
    if not coin.creator:
        return out
    rows = conn.execute(f"SELECT mint, {ET} AS t, dev_buy_sol, slot FROM coins WHERE creator=? AND mint<>? AND {ASOF} AND {ET} < ? ORDER BY t",
                        (coin.creator, coin.mint, as_of, as_of, coin.created_t)).fetchall()
    out["creator_coins"] = len(rows)
    out["creator_coins_24h"] = sum(1 for r in rows if float(r["t"]) >= coin.created_t - 86400)
    out["creator_prev_coin_age_s"] = None if not rows else round(coin.created_t - float(rows[-1]["t"]))
    mints = [str(r["mint"]) for r in rows]
    grads = winners = not_winners = bundled = dumped = 0
    if mints:
        for chunk in rf.iter_chunks(mints, 400):
            marks = ",".join("?" for _ in chunk)
            grads += int(conn.execute(f"SELECT COUNT(*) FROM migrations WHERE mint IN ({marks}) AND {ASOF}", (*chunk, as_of, as_of)).fetchone()[0])
            for r in conn.execute(f"SELECT hit_100k FROM outcomes WHERE subject='graduation' AND horizon_s=? AND method_version=? AND measured_at <= ? AND mint IN ({marks})",
                                  (int(h24), method_version, as_of, *chunk)):
                if r["hit_100k"] == 1:
                    winners += 1
                elif r["hit_100k"] == 0:
                    not_winners += 1
        scan = rows[-cap:] if cap and len(rows) > cap else rows
        out["creator_scan_capped"] = len(scan) < len(rows)
        for r in scan:
            tr = rf.load_trades(conn, str(r["mint"]), as_of=as_of)
            bf = rf.bundle_flags(tr, as_of, float(r["t"]), r["dev_buy_sol"], r["slot"], creator=coin.creator)
            if bf["bundled_any"] is True:
                bundled += 1
            if rf.dev_sell(tr, as_of, float(r["t"]), coin.creator, dev_sell_window_s)["dev_sold_within_window"]:
                dumped += 1
    out.update({"creator_graduations": grads, "creator_winners": winners, "creator_not_winners": not_winners, "creator_bundled": bundled, "creator_dev_sold_early": dumped})
    out["creator_track_record"] = ("good" if (grads > 0 or winners > 0) and bundled == 0 and dumped == 0 else
                                   ("bad" if (bundled > 0 or dumped > 0) else ("none" if not rows else "neutral")))
    return out


def follow_started_at(conn: sqlite3.Connection) -> float | None:
    """When the follow step first ran (kv `follow.started_at`, milestone 13), else the first poll point; None without either."""
    try:
        raw = conn.execute("SELECT value FROM kv WHERE key='follow.started_at'").fetchone()
        if raw is not None and raw[0]:
            return float(raw[0])
        if _table_exists(conn, "path_points"):
            r = conn.execute("SELECT MIN(t) FROM path_points").fetchone()
            return None if r is None or r[0] is None else float(r[0])
    except (sqlite3.OperationalError, TypeError, ValueError):
        return None
    return None


def run_hypothesis(conn: sqlite3.Connection, settings: Settings, hypothesis: str, days: list[str], **overrides: Any) -> dict[str, Any]:
    cfg = RunConfig.from_settings(settings, hypothesis, days, **overrides)
    return Harness(conn, settings, cfg).run()


def window_days(end_day: str, days: int) -> list[str]:
    from ..measure.week import window_days as wd

    return wd(end_day, days)


__all__ = ["RunConfig", "Harness", "Case", "Coin", "metrics", "bootstrap_mean", "proven_verdict", "price_path", "graduation_time", "run_hypothesis", "window_days",
           "max_drawdown", "load_coin", "follow_started_at"]
