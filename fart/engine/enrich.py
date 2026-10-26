"""Candidate-only RPC enrichment (budget-bounded): holder concentration and creator activity."""
from __future__ import annotations

from typing import Any

from ..chain.rpc import RpcClient, RpcError
from ..net.budget import Blocked
from ..net.http import HttpError

TOTAL_SUPPLY_TOKENS = 1_000_000_000.0


def holder_concentration(rpc: RpcClient, mint: str, top_n: int = 10) -> dict[str, Any] | None:
    """Top-N share of total supply from getTokenLargestAccounts. The largest account is usually
    the bonding curve before graduation (or the AMM pool after), so both including and excluding
    it are reported. None when the call is unavailable (budget/transport)."""
    try:
        accounts = rpc.get_token_largest_accounts(mint)
    except (Blocked, HttpError, RpcError):
        return None
    amounts = []
    for a in accounts:
        try:
            amounts.append(float(a.get("uiAmount") if a.get("uiAmount") is not None else a.get("uiAmountString")))
        except (TypeError, ValueError):
            continue
    amounts.sort(reverse=True)
    if not amounts:
        return {"accounts": 0, "top_n_share": None, "top_n_share_excl_largest": None, "largest_share": None}
    top = amounts[:top_n]
    return {
        "accounts": len(amounts),
        "largest_share": round(top[0] / TOTAL_SUPPLY_TOKENS, 4),
        "top_n_share": round(sum(top) / TOTAL_SUPPLY_TOKENS, 4),
        "top_n_share_excl_largest": round(sum(amounts[1:top_n + 1]) / TOTAL_SUPPLY_TOKENS, 4),
    }


def creator_activity(rpc: RpcClient, creator: str, as_of: float, window_s: float = 86400.0, limit: int = 100) -> dict[str, Any] | None:
    """Transactions signed/mentioning the creator wallet in the window (activity proxy; a prior-coin
    count would need one getTransaction per signature). None when unavailable."""
    try:
        sigs = rpc.get_signatures_for_address(creator, limit=limit)
    except (Blocked, HttpError, RpcError):
        return None
    n = 0
    oldest = None
    for s in sigs:
        bt = s.get("blockTime")
        if bt is None:
            continue
        if as_of - window_s <= bt <= as_of:
            n += 1
        oldest = bt if oldest is None else min(oldest, bt)
    return {"tx_in_window": n, "fetched": len(sigs), "truncated": len(sigs) >= limit,
            "wallet_age_s_min": None if oldest is None else round(as_of - oldest)}
