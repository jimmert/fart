"""Executable paper trade per `observe` decision, from recorded trades only.

Entry: `size_sol` at the first trade at/after as_of + entry_delay_s, filled on the curve state of
that trade (reserves after it; conservative) with fee, impact and slippage from curve math.
Exit: trailing stop from the running peak spot price, or at max_hold_s, on the recorded trades;
no later trade -> no exit (pnl stays None, reason recorded). Costs are explicit. Pure function.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
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
