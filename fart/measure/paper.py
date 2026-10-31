"""Executable paper trade per `observe` decision, from recorded trades only.

Entry: `size_sol` at the first trade at/after as_of + entry_delay_s, filled on the curve state of
that trade (reserves after it; conservative) with fee, impact and slippage from curve math.
Exit: trailing stop from the running peak spot price, or at max_hold_s, on the recorded trades;
no later trade -> no exit (pnl stays None, reason recorded). Costs are explicit. Pure function.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, fields
from typing import Any

from ..chain.curve import CurveParams, CurveState, buy_quote, sell_quote


@dataclass
class TradePoint:
    t: float
    virtual_sol: int
    virtual_token: int


@dataclass
class PaperResult:
    entry_as_of: float
    entry_time: float | None
    entry_price: float | None       # spot before our buy (SOL/token)
    fill_price: float | None        # effective price paid incl. fee
    size_sol: float
    tokens: float | None
    slippage_bps: float | None
    price_impact: float | None
    partial: bool
    fees_sol: float
    exit_time: float | None
    exit_price: float | None        # spot at exit
    exit_sol_net: float | None
    peak_price: float | None
    exit_reason: str
    pnl_sol: float | None
    pnl_pct: float | None
    trades_seen: int

    def to_json(self) -> dict[str, Any]:
        return asdict(self)


def simulate(points: list[TradePoint], params: CurveParams, cfg: dict[str, Any], as_of: float) -> PaperResult:
    size_sol = float(cfg.get("size_sol", 0.5))
    delay = float(cfg.get("entry_delay_s", 20))
    stop = float(cfg.get("trailing_stop_frac", 0.30))
    max_hold = float(cfg.get("max_hold_s", 86400))
    net_fee = float(cfg.get("network_fee_sol", 0.0001))
    entry_as_of = as_of + delay
    pts = sorted((p for p in points if p.virtual_sol > 0 and p.virtual_token > 0), key=lambda p: p.t)
    entry_idx = next((i for i, p in enumerate(pts) if p.t >= entry_as_of), None)
    base = dict(entry_as_of=entry_as_of, size_sol=size_sol, partial=False, trades_seen=len(pts))
    if entry_idx is None:
        return PaperResult(entry_time=None, entry_price=None, fill_price=None, tokens=None, slippage_bps=None, price_impact=None,
                           fees_sol=0.0, exit_time=None, exit_price=None, exit_sol_net=None, peak_price=None,
                           exit_reason="no_trade_after_entry", pnl_sol=None, pnl_pct=None, **base)
    ep = pts[entry_idx]
    state = CurveState(ep.virtual_sol, ep.virtual_token)
    q = buy_quote(state, params, int(round(size_sol * params.sol_unit)))
    fees = q.fee / params.sol_unit + net_fee
    held_tokens = q.tokens_out
    peak = q.new_spot_price
    exit_reason = "no_exit_data"
    exit_time = exit_price = exit_net = None
    for p in pts[entry_idx + 1:]:
        st = CurveState(p.virtual_sol, p.virtual_token)
        spot = st.price_sol(params)
        if p.t - ep.t > max_hold:
            sq = sell_quote(st, params, held_tokens)
            exit_time, exit_price, exit_net, exit_reason = p.t, spot, sq.sol_out_net / params.sol_unit, "max_hold"
            fees += sq.fee / params.sol_unit + net_fee
            break
        peak = max(peak, spot)
        if spot <= peak * (1.0 - stop):
            sq = sell_quote(st, params, held_tokens)
            exit_time, exit_price, exit_net, exit_reason = p.t, spot, sq.sol_out_net / params.sol_unit, "trailing_stop"
            fees += sq.fee / params.sol_unit + net_fee
            break
    # curve fees are inside size_sol (buy) and exit_net (sell); network fees are paid on both legs
    pnl = None if exit_net is None else exit_net - size_sol - 2 * net_fee
    return PaperResult(
        entry_time=ep.t, entry_price=q.spot_price, fill_price=q.fill_price, tokens=held_tokens / params.token_unit,
        slippage_bps=q.slippage_bps, price_impact=q.price_impact, fees_sol=round(fees, 9), exit_time=exit_time,
        exit_price=exit_price, exit_sol_net=exit_net, peak_price=peak, exit_reason=exit_reason, pnl_sol=pnl,
        pnl_pct=None if pnl is None else pnl / size_sol, **dict(base, partial=q.partial),
    )


def points_from_rows(rows) -> list[TradePoint]:
    out = []
    for r in rows:
        if r["virtual_sol"] is None or r["virtual_token"] is None:
            continue
        t = r["block_time"] if r["block_time"] is not None else r["received_at"]
        out.append(TradePoint(float(t), int(r["virtual_sol"]), int(r["virtual_token"])))
    return out


# =====================================================================================================================
# Milestone 12: the shared execution model of the research harness (fart/research) and the plays layer (fart/plays).
#
# `simulate()` above is the milestone-1 paper trade of the measurement (method_version m1.0) and stays what it is. The
# `Position` below is the one implementation of "what would a human have got": the harness feeds it the reconstructed
# price path of a coin in one go, the plays daemon feeds it the new trades of every tick and keeps its state in the
# `plays` row between ticks. Both get the same fills, the same costs and the same exits by construction.
#
# Price path: a list of `PricePoint`s in time order. On the curve a point carries the virtual reserves after a trade (kind
# curve) or as the BondingCurve account showed them between the trades (kind poll, milestone 13) and the fill is `buy_quote`
# / `sell_quote` (the curve fee of config/pumpfun.json, 1%, and the curve's own impact are inside the quotes). After graduation a point carries a price in SOL per token (a DexScreener sample or a GeckoTerminal candle)
# and the SOL side of the pool's liquidity when the sample had it; the fill then pays `pumpswap_fee_bps` and the
# constant-product impact V / (1 + V / Y) with Y the SOL side of the pool (an assumed 85 SOL, flagged, when the sample had
# no liquidity). Every transaction pays `priority_fee_sol`. The human delay: a signal at t fills at the first point at or
# after t + entry_delay_s; an exit rule that triggers at a point fills at the first point at or after that point's time
# plus exit_delay_s (the same delay, unless given). No point there: no fill, or an unknown outcome; never a number.
#
# Exit rules (`ExitRules`), checked in this order at every point after the fill: a sell by the creator wallet when
# `sell_at_dev_sell`; the graduation point when `sell_at_graduation`; the hard stop (spot at or under the fill price
# minus `hard_stop_frac`); the target (spot at or above the fill price plus `target_pct`: `target_fraction` of the tokens
# is sold once, the rest keeps the trailing stop); the trailing stop (spot at or under the running peak minus
# `trailing_frac`); the time stop (`time_stop_s` after the fill); the outer bound `max_hold_s`. A path that ends before
# any exit filled leaves the position open: the outcome is UNKNOWN and is counted apart, never as a result.
# =====================================================================================================================
LAMPORTS = 1_000_000_000
TOKEN_UNIT = 1_000_000
ASSUMED_POOL_SOL = 85.0        # the SOL side of a fresh PumpSwap pool; used (and flagged) when a sample carries no liquidity


CURVE_KINDS = ("curve", "poll")   # the kinds that carry virtual reserves: a fill on them is a quote against the curve


@dataclass
class PricePoint:
    """One point of a coin's price path. `kind`: curve (virtual reserves after a trade), poll (milestone 13: the virtual
    reserves read from the BondingCurve account between the trades; the curve is always the counterparty, so an entry or an
    exit may fill on it, but a poll sees no trade: `dev_sell` is never set on one), sample (a DexScreener market cap sample),
    candle (a GeckoTerminal 1-minute close). `price_sol` is SOL per whole token (derived for curve and poll points).
    `liquidity_sol` is the SOL side of the pool for the post-graduation impact; `dev_sell` marks a sell by the creator;
    `graduation` marks the graduation (the last curve point before the migration)."""
    t: float
    kind: str = "curve"
    virtual_sol: int | None = None
    virtual_token: int | None = None
    price_sol: float | None = None
    liquidity_sol: float | None = None
    dev_sell: bool = False
    graduation: bool = False

    def state(self) -> CurveState | None:
        if self.kind in CURVE_KINDS and self.virtual_sol and self.virtual_token:
            return CurveState(int(self.virtual_sol), int(self.virtual_token))
        return None

    def spot(self, params: CurveParams) -> float | None:
        st = self.state()
        if st is not None:
            return st.price_sol(params)
        return self.price_sol if self.price_sol and self.price_sol > 0 else None


@dataclass(frozen=True)
class ExitRules:
    target_pct: float | None = None       # +0.60: sell `target_fraction` once the spot is 60% above the fill price
    target_fraction: float = 0.5
    trailing_frac: float | None = 0.30    # sell what is left when the spot falls 30% from the running peak
    time_stop_s: float | None = None      # sell what is left this many seconds after the fill
    hard_stop_frac: float | None = 0.35   # sell everything when the spot is 35% under the fill price
    sell_at_graduation: bool = False
    sell_at_dev_sell: bool = False
    max_hold_s: float = 86400.0

    @classmethod
    def from_dict(cls, d: dict[str, Any] | None) -> "ExitRules":
        d = dict(d or {})
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in d.items() if k in known})

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def describe_nl(self) -> str:
        """The exit rule in plain Dutch (the Plays panel and the reports)."""
        parts = []
        if self.sell_at_dev_sell:
            parts.append("verkoop zodra de maker verkoopt")
        if self.sell_at_graduation:
            parts.append("verkoop op het moment van graduatie")
        if self.hard_stop_frac is not None:
            parts.append(f"harde stop op −{round(100 * self.hard_stop_frac)}%")
        if self.target_pct is not None:
            frac = "alles" if self.target_fraction >= 0.999 else f"{round(100 * self.target_fraction)}%"
            parts.append(f"verkoop {frac} bij +{round(100 * self.target_pct)}%")
        if self.trailing_frac is not None:
            parts.append(f"trailing stop {round(100 * self.trailing_frac)}% onder de piek" + (" voor de rest" if self.target_pct is not None and self.target_fraction < 0.999 else ""))
        if self.time_stop_s is not None:
            parts.append(f"tijdstop na {_minutes_nl(self.time_stop_s)}")
        parts.append(f"uiterlijk na {_minutes_nl(self.max_hold_s)}")
        return "; ".join(parts)


def _minutes_nl(seconds: float) -> str:
    s = float(seconds)
    if s < 3600:
        return f"{round(s / 60)} minuten" if round(s / 60) != 1 else "1 minuut"
    h = s / 3600
    return f"{round(h)} uur" if abs(h - round(h)) < 0.01 else f"{h:.1f} uur".replace(".", ",")


@dataclass(frozen=True)
class Costs:
    priority_fee_sol: float = 0.0005
    pumpswap_fee_bps: int = 25

    @classmethod
    def from_config(cls, rcfg: dict[str, Any] | None) -> "Costs":
        rcfg = rcfg or {}
        return cls(priority_fee_sol=float(rcfg.get("priority_fee_sol", 0.0005)), pumpswap_fee_bps=int(rcfg.get("pumpswap_fee_bps", 25)))


@dataclass
class Fill:
    t: float
    kind: str                 # buy | sell
    reason: str               # entry | target | trailing_stop | hard_stop | time_stop | max_hold | graduation | dev_sell
    tokens: int               # base units
    sol: float                # SOL paid (buy, fee included) or received net (sell)
    spot: float               # spot price at the fill
    fill_price: float         # effective SOL per token
    fee_sol: float            # curve / pool fee in SOL
    impact: float | None      # price impact fraction of this fill
    venue: str                # curve | pool
    impact_src: str | None = None   # for pool fills: liquidity | assumed_85_sol
    point_kind: str | None = None   # milestone 13: the kind of the point the fill happened on (curve | poll | sample | candle)


def pool_sell(tokens: int, price_sol: float, liquidity_sol: float | None, fee_bps: int) -> tuple[float, float, float, str]:
    """Selling `tokens` base units on the pool at `price_sol`: (net SOL, fee SOL, impact fraction, impact source)."""
    value = tokens / TOKEN_UNIT * price_sol
    y = float(liquidity_sol) if liquidity_sol and liquidity_sol > 0 else ASSUMED_POOL_SOL
    src = "liquidity" if liquidity_sol and liquidity_sol > 0 else "assumed_85_sol"
    gross = value / (1.0 + value / y)
    fee = gross * fee_bps / 10_000
    return gross - fee, fee, (1.0 - gross / value) if value > 0 else 0.0, src


def pool_buy(sol_total: float, price_sol: float, liquidity_sol: float | None, fee_bps: int) -> tuple[int, float, float, str]:
    """Buying with `sol_total` SOL (fee included) on the pool: (tokens base units, fee SOL, impact fraction, impact source)."""
    y = float(liquidity_sol) if liquidity_sol and liquidity_sol > 0 else ASSUMED_POOL_SOL
    src = "liquidity" if liquidity_sol and liquidity_sol > 0 else "assumed_85_sol"
    fee = sol_total * fee_bps / (10_000 + fee_bps)
    to_pool = sol_total - fee
    # constant product: tokens out = X * dy / (Y + dy) = (value at spot) / (1 + dy / Y)
    tokens = (to_pool / price_sol) / (1.0 + to_pool / y) * TOKEN_UNIT
    return int(tokens), fee, (1.0 - 1.0 / (1.0 + to_pool / y)), src


class Position:
    """A paper position driven point by point (see the module notes above). `signal_t` is the time of the signal; the fill
    waits `entry_delay_s`; `size_sol` is what the human spends (fee included). `feed(point)` returns a list of events
    ("filled", "partial_exit", "closed", "no_fill") and `done` says whether anything is left to follow."""

    def __init__(self, signal_t: float, size_sol: float, params: CurveParams, rules: ExitRules, costs: Costs | None = None,
                 entry_delay_s: float = 20.0, exit_delay_s: float | None = None, max_entry_wait_s: float = 600.0):
        self.signal_t = float(signal_t)
        self.size_sol = float(size_sol)
        self.params = params
        self.rules = rules
        self.costs = costs or Costs()
        self.entry_delay_s = float(entry_delay_s)
        self.exit_delay_s = self.entry_delay_s if exit_delay_s is None else float(exit_delay_s)
        self.max_entry_wait_s = float(max_entry_wait_s)
        self.status = "signal"            # signal | open | closed | no_fill
        self.fills: list[Fill] = []
        self.entry: Fill | None = None
        self.held: int = 0                # tokens held (base units)
        self.peak: float | None = None    # running peak spot since the fill
        self.target_taken = False
        self.pending_exit: tuple[str, float] | None = None   # (reason, trigger time): fills at the first point >= trigger + exit_delay
        self.last_point: PricePoint | None = None
        self.points_seen = 0
        self.fees_sol = 0.0
        self.sold_sol = 0.0
        self.exit_reason: str | None = None
        self.exit_t: float | None = None

    # ----- state ---------------------------------------------------------------------------------------------------
    @property
    def done(self) -> bool:
        return self.status in ("closed", "no_fill")

    @property
    def pnl_sol(self) -> float | None:
        if self.status != "closed" or self.entry is None:
            return None
        return self.sold_sol - self.size_sol - self.costs.priority_fee_sol * len(self.fills)

    @property
    def pnl_pct(self) -> float | None:
        p = self.pnl_sol
        return None if p is None else p / self.size_sol

    def mark(self, point: PricePoint | None = None) -> float | None:
        """The unrealised result at `point` (the last point by default): what a sell of what is held would net, net of all
        costs so far; None before the fill or without a price."""
        if self.entry is None or self.status != "open":
            return None
        pt = point or self.last_point
        if pt is None:
            return None
        spot = pt.spot(self.params)
        if spot is None:
            return None
        net = self._sell_value(pt, self.held, spot)[0] if self.held > 0 else 0.0
        return self.sold_sol + net - self.size_sol - self.costs.priority_fee_sol * (len(self.fills) + (1 if self.held > 0 else 0))

    def to_json(self) -> dict[str, Any]:
        return {"signal_t": self.signal_t, "size_sol": self.size_sol, "rules": self.rules.to_dict(), "costs": asdict(self.costs),
                "entry_delay_s": self.entry_delay_s, "exit_delay_s": self.exit_delay_s, "max_entry_wait_s": self.max_entry_wait_s,
                "status": self.status, "fills": [asdict(f) for f in self.fills], "held": self.held, "peak": self.peak,
                "target_taken": self.target_taken, "pending_exit": list(self.pending_exit) if self.pending_exit else None,
                "points_seen": self.points_seen, "fees_sol": self.fees_sol, "sold_sol": self.sold_sol,
                "exit_reason": self.exit_reason, "exit_t": self.exit_t, "last_t": None if self.last_point is None else self.last_point.t,
                "last_spot": None if self.last_point is None else self.last_point.spot(self.params)}

    @classmethod
    def from_json(cls, d: dict[str, Any], params: CurveParams) -> "Position":
        p = cls(float(d["signal_t"]), float(d["size_sol"]), params, ExitRules.from_dict(d.get("rules")), Costs(**(d.get("costs") or {})),
                float(d.get("entry_delay_s", 20.0)), d.get("exit_delay_s"), float(d.get("max_entry_wait_s", 600.0)))
        p.status = str(d.get("status", "signal"))
        p.fills = [Fill(**{k: v for k, v in f.items() if k in Fill.__dataclass_fields__}) for f in d.get("fills") or []]  # type: ignore[attr-defined]
        p.entry = next((f for f in p.fills if f.kind == "buy"), None)
        p.held = int(d.get("held") or 0)
        p.peak = d.get("peak")
        p.target_taken = bool(d.get("target_taken"))
        pe = d.get("pending_exit")
        p.pending_exit = (str(pe[0]), float(pe[1])) if pe else None
        p.points_seen = int(d.get("points_seen") or 0)
        p.fees_sol = float(d.get("fees_sol") or 0.0)
        p.sold_sol = float(d.get("sold_sol") or 0.0)
        p.exit_reason = d.get("exit_reason")
        p.exit_t = d.get("exit_t")
        if d.get("last_t") is not None:
            p.last_point = PricePoint(float(d["last_t"]), "sample", price_sol=d.get("last_spot"))
        return p

    # ----- feeding -------------------------------------------------------------------------------------------------
    def feed_all(self, points: list[PricePoint]) -> list[str]:
        events: list[str] = []
        for pt in points:
            events.extend(self.feed(pt))
            if self.done:
                break
        return events

    def feed(self, pt: PricePoint) -> list[str]:
        if self.done:
            return []
        self.points_seen += 1
        events: list[str] = []
        spot = pt.spot(self.params)
        if self.status == "signal":
            if pt.t < self.signal_t + self.entry_delay_s:
                self.last_point = pt
                return []
            if pt.t > self.signal_t + self.entry_delay_s + self.max_entry_wait_s:
                self.status = "no_fill"
                self.exit_reason = "no_trade_after_entry"
                self.last_point = pt
                return ["no_fill"]
            if spot is None:
                self.last_point = pt
                return []
            self._buy(pt, spot)
            events.append("filled")
            self.last_point = pt
            return events
        # open: a pending exit fills first
        if self.pending_exit is not None:
            reason, trigger_t = self.pending_exit
            if pt.t >= trigger_t + self.exit_delay_s and spot is not None:
                fraction = self.rules.target_fraction if reason == "target" else 1.0
                self._sell(pt, spot, reason, fraction)
                self.pending_exit = None
                events.append("closed" if self.status == "closed" else "partial_exit")
                self.last_point = pt
                return events
        if spot is None:
            self.last_point = pt
            return events
        if self.peak is None or spot > self.peak:
            self.peak = spot
        reason = self._trigger(pt, spot)
        if reason is not None and self.pending_exit is None:
            if self.exit_delay_s <= 0:
                fraction = self.rules.target_fraction if reason == "target" else 1.0
                self._sell(pt, spot, reason, fraction)
                events.append("closed" if self.status == "closed" else "partial_exit")
            else:
                self.pending_exit = (reason, pt.t)
                events.append("exit_pending")
        self.last_point = pt
        return events

    def _trigger(self, pt: PricePoint, spot: float) -> str | None:
        assert self.entry is not None
        r = self.rules
        fill = self.entry.fill_price
        if r.sell_at_dev_sell and pt.dev_sell:
            return "dev_sell"
        if r.sell_at_graduation and pt.graduation:
            return "graduation"
        if r.hard_stop_frac is not None and spot <= fill * (1.0 - r.hard_stop_frac):
            return "hard_stop"
        if r.target_pct is not None and not self.target_taken and spot >= fill * (1.0 + r.target_pct):
            return "target"
        if r.trailing_frac is not None and self.peak is not None and spot <= self.peak * (1.0 - r.trailing_frac):
            return "trailing_stop"
        if r.time_stop_s is not None and pt.t - self.entry.t >= r.time_stop_s:
            return "time_stop"
        if pt.t - self.entry.t >= r.max_hold_s:
            return "max_hold"
        return None

    # ----- fills ---------------------------------------------------------------------------------------------------
    def _buy(self, pt: PricePoint, spot: float) -> None:
        st = pt.state()
        if st is not None:
            q = buy_quote(st, self.params, int(round(self.size_sol * self.params.sol_unit)))
            tokens, fee, impact, venue, src = q.tokens_out, q.fee / self.params.sol_unit, q.price_impact, "curve", None
            fill_price = q.fill_price
        else:
            tokens, fee, impact, src = pool_buy(self.size_sol, spot, pt.liquidity_sol, self.costs.pumpswap_fee_bps)
            venue = "pool"
            fill_price = (self.size_sol / (tokens / TOKEN_UNIT)) if tokens > 0 else float("inf")
        if tokens <= 0:
            self.status = "no_fill"
            self.exit_reason = "no_tokens"
            return
        f = Fill(pt.t, "buy", "entry", int(tokens), self.size_sol, spot, fill_price, fee, impact, venue, src, pt.kind)
        self.fills.append(f)
        self.entry = f
        self.held = int(tokens)
        self.peak = spot
        self.fees_sol += fee + self.costs.priority_fee_sol
        self.status = "open"

    def _sell_value(self, pt: PricePoint, tokens: int, spot: float) -> tuple[float, float, float | None, str, str | None]:
        st = pt.state()
        if st is not None:
            q = sell_quote(st, self.params, int(tokens))
            return q.sol_out_net / self.params.sol_unit, q.fee / self.params.sol_unit, q.price_impact, "curve", None
        net, fee, impact, src = pool_sell(int(tokens), spot, pt.liquidity_sol, self.costs.pumpswap_fee_bps)
        return net, fee, impact, "pool", src

    def _sell(self, pt: PricePoint, spot: float, reason: str, fraction: float) -> None:
        tokens = self.held if fraction >= 0.999 else int(self.held * fraction)
        if tokens <= 0:
            return
        net, fee, impact, venue, src = self._sell_value(pt, tokens, spot)
        self.fills.append(Fill(pt.t, "sell", reason, int(tokens), net, spot, (net / (tokens / TOKEN_UNIT)) if tokens else 0.0, fee, impact, venue, src, pt.kind))
        self.held -= int(tokens)
        self.sold_sol += net
        self.fees_sol += fee + self.costs.priority_fee_sol
        if reason == "target":
            self.target_taken = True
        if self.held <= 0:
            self.status = "closed"
            self.exit_reason = reason
            self.exit_t = pt.t


def capacity_sol(state: CurveState, params: CurveParams, max_impact: float = 0.05) -> float:
    """The SOL a buy may spend (fee included) before the spot moves `max_impact` (5%): for the constant product the spot
    after a buy of x to the curve is (vS + x)^2 / k, so x = vS (sqrt(1 + max_impact) - 1)."""
    x = state.virtual_sol * ((1.0 + max_impact) ** 0.5 - 1.0)
    return x * (1.0 + params.trade_fee_bps / 10_000) / params.sol_unit
