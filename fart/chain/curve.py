"""pump.fun bonding curve math: constant product on virtual reserves.

All state is in base units (lamports; tokens with 6 decimals) as integers, like the program.
Parameters come from config/pumpfun.json; reserves, supply and the 0.95% protocol fee were
checked against the pump.fun Global account on the VPS (`fart chain global`). Quotes charge
`trade_fee_bps` = total_curve_fee_bps (protocol 95 + creator 30 = 125, creator part TO VERIFY).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

LAMPORTS = 1_000_000_000


@dataclass(frozen=True)
class CurveParams:
    initial_virtual_sol: int
    initial_virtual_token: int
    initial_real_token: int
    token_total_supply: int
    fee_bps: int                   # protocol fee (Global.fee_basis_points, 95 on mainnet)
    graduation_real_sol: int
    token_decimals: int = 6
    sol_decimals: int = 9
    creator_fee_bps: int = 0       # creator fee on curve trades (TO VERIFY, default 30)
    total_fee_bps: int | None = None  # what a trader pays in total; the paper trade uses this

    @classmethod
    def from_config(cls, cfg: dict[str, Any]) -> "CurveParams":
        fee = int(cfg["fee_bps"])
        creator = int(cfg.get("creator_fee_bps", 0))
        return cls(
            initial_virtual_sol=int(cfg["initial_virtual_sol_reserves"]),
            initial_virtual_token=int(cfg["initial_virtual_token_reserves"]),
            initial_real_token=int(cfg["initial_real_token_reserves"]),
            token_total_supply=int(cfg["token_total_supply"]),
            fee_bps=fee,
            graduation_real_sol=int(cfg["graduation_real_sol"]),
            token_decimals=int(cfg.get("token_decimals", 6)),
            sol_decimals=int(cfg.get("sol_decimals", 9)),
            creator_fee_bps=creator,
            total_fee_bps=int(cfg.get("total_curve_fee_bps", fee + creator)),
        )

    @property
    def trade_fee_bps(self) -> int:
        """Fee basis points applied to quotes: the configured total, else protocol + creator."""
        return self.total_fee_bps if self.total_fee_bps is not None else self.fee_bps + self.creator_fee_bps

    @property
    def token_unit(self) -> int:
        return 10 ** self.token_decimals

    @property
    def sol_unit(self) -> int:
        return 10 ** self.sol_decimals


@dataclass(frozen=True)
class CurveState:
    virtual_sol: int
    virtual_token: int
    real_sol: int | None = None
    real_token: int | None = None

    @classmethod
    def initial(cls, p: CurveParams) -> "CurveState":
        return cls(p.initial_virtual_sol, p.initial_virtual_token, 0, p.initial_real_token)

    @classmethod
    def from_sol_floats(cls, v_sol: float, v_tokens: float, p: CurveParams) -> "CurveState":
        """From PumpPortal-style floats (SOL and whole tokens)."""
        return cls(int(round(v_sol * p.sol_unit)), int(round(v_tokens * p.token_unit)))

    @property
    def k(self) -> int:
        return self.virtual_sol * self.virtual_token

    def price_sol(self, p: CurveParams) -> float:
        """SOL per whole token."""
        if self.virtual_token <= 0:
            return float("inf")
        return (self.virtual_sol / p.sol_unit) / (self.virtual_token / p.token_unit)

    def mcap_sol(self, p: CurveParams) -> float:
        return self.price_sol(p) * (p.token_total_supply / p.token_unit)

    def real_sol_estimate(self, p: CurveParams) -> int:
        """Real SOL in the curve: the tracked value, else virtual minus initial virtual."""
        if self.real_sol is not None:
            return self.real_sol
        return max(0, self.virtual_sol - p.initial_virtual_sol)


@dataclass(frozen=True)
class BuyQuote:
    sol_in_total: int        # what the buyer pays (curve + fee)
    sol_to_curve: int
    fee: int
    tokens_out: int
    spot_price: float        # SOL per token before
    fill_price: float        # SOL per token actually paid (incl. fee)
    new_spot_price: float
    price_impact: float      # new_spot / spot - 1
    slippage_bps: float      # (fill / spot - 1) * 1e4
    partial: bool            # capped by remaining real tokens
    new_state: CurveState


@dataclass(frozen=True)
class SellQuote:
    tokens_in: int
    sol_out_gross: int
    fee: int
    sol_out_net: int
    spot_price: float
    fill_price: float
    new_spot_price: float
    price_impact: float
    slippage_bps: float
    new_state: CurveState


def split_fee(sol_total: int, fee_bps: int) -> tuple[int, int]:
    """Gross SOL budget -> (sol_to_curve, fee) with fee = sol_to_curve * fee_bps / 10000."""
    sol_to_curve = sol_total * 10_000 // (10_000 + fee_bps)
    return sol_to_curve, sol_total - sol_to_curve


def buy_quote(state: CurveState, p: CurveParams, sol_in_total: int) -> BuyQuote:
    """Spend `sol_in_total` lamports (fee included). tokens = vT - k/(vS + x)."""
    if sol_in_total <= 0:
        raise ValueError("sol_in_total must be positive")
    sol_to_curve, fee = split_fee(sol_in_total, p.trade_fee_bps)
    spot = state.price_sol(p)
    new_vsol = state.virtual_sol + sol_to_curve
    new_vtok = state.k // new_vsol + 1
    tokens_out = max(0, state.virtual_token - new_vtok)
    partial = False
    remaining = state.real_token if state.real_token is not None else None
    if remaining is not None and tokens_out > remaining:
        tokens_out = remaining
        partial = True
        # recompute the SOL actually needed for the capped amount
        new_vtok = state.virtual_token - tokens_out
        new_vsol = -(-state.k // new_vtok) if new_vtok > 0 else new_vsol
        sol_to_curve = new_vsol - state.virtual_sol
        fee = sol_to_curve * p.trade_fee_bps // 10_000
        sol_in_total = sol_to_curve + fee
    new_state = CurveState(
        new_vsol,
        state.virtual_token - tokens_out,
        None if state.real_sol is None else state.real_sol + sol_to_curve,
        None if state.real_token is None else state.real_token - tokens_out,
    )
    new_spot = new_state.price_sol(p)
    fill = (sol_in_total / p.sol_unit) / (tokens_out / p.token_unit) if tokens_out > 0 else float("inf")
    return BuyQuote(
        sol_in_total=sol_in_total, sol_to_curve=sol_to_curve, fee=fee, tokens_out=tokens_out,
        spot_price=spot, fill_price=fill, new_spot_price=new_spot,
        price_impact=(new_spot / spot - 1.0) if spot > 0 else float("inf"),
        slippage_bps=((fill / spot) - 1.0) * 10_000 if spot > 0 and tokens_out > 0 else float("inf"),
        partial=partial, new_state=new_state,
    )


def sell_quote(state: CurveState, p: CurveParams, tokens_in: int) -> SellQuote:
    """Sell `tokens_in` base units. sol = vS - k/(vT + t), minus fee."""
    if tokens_in <= 0:
        raise ValueError("tokens_in must be positive")
    spot = state.price_sol(p)
    new_vtok = state.virtual_token + tokens_in
    gross = state.virtual_sol * tokens_in // new_vtok
    fee = gross * p.trade_fee_bps // 10_000
    net = gross - fee
    new_state = CurveState(
        state.virtual_sol - gross,
        new_vtok,
        None if state.real_sol is None else max(0, state.real_sol - gross),
        None if state.real_token is None else state.real_token + tokens_in,
    )
    new_spot = new_state.price_sol(p)
    fill = (net / p.sol_unit) / (tokens_in / p.token_unit)
    return SellQuote(
        tokens_in=tokens_in, sol_out_gross=gross, fee=fee, sol_out_net=net,
        spot_price=spot, fill_price=fill, new_spot_price=new_spot,
        price_impact=(new_spot / spot - 1.0) if spot > 0 else 0.0,
        slippage_bps=(1.0 - fill / spot) * 10_000 if spot > 0 else 0.0,
        new_state=new_state,
    )


def graduation_progress(state: CurveState, p: CurveParams) -> float:
    """real SOL / ~85 SOL, clamped to [0, 1]."""
    if p.graduation_real_sol <= 0:
        return 0.0
    return max(0.0, min(1.0, state.real_sol_estimate(p) / p.graduation_real_sol))


def sol_to_graduate(state: CurveState, p: CurveParams) -> float:
    """Remaining SOL (whole SOL) of real reserves before graduation."""
    return max(0, p.graduation_real_sol - state.real_sol_estimate(p)) / p.sol_unit


def sol_for_tokens(state: CurveState, p: CurveParams, tokens_out: int) -> int:
    """Lamports to the curve needed to receive `tokens_out` (no fee)."""
    if tokens_out <= 0:
        return 0
    if tokens_out >= state.virtual_token:
        raise ValueError("cannot buy the whole virtual reserve")
    new_vtok = state.virtual_token - tokens_out
    new_vsol = -(-state.k // new_vtok)
    return new_vsol - state.virtual_sol


def dev_buy_fraction_of_curve(dev_buy_sol: float | None, p: CurveParams) -> float | None:
    """Dev buy as a fraction of the SOL needed to graduate (bundled heuristic input)."""
    if dev_buy_sol is None:
        return None
    return dev_buy_sol / (p.graduation_real_sol / p.sol_unit)
