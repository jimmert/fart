"""Price sources for measurement: SOL/USD (cached 60 s) and per-mint market cap samples.

DexScreener: GET https://api.dexscreener.com/tokens/v1/solana/{mint,...} (<= 30 mints) -> list
of pairs with priceUsd, marketCap, fdv, liquidity.usd, volume.{h24,h6,h1,m5}, txns, pairCreatedAt,
dexId. GeckoTerminal is the fallback for SOL/USD. Response shapes are UNVERIFIED in the sandbox
(docs/UNVERIFIED.md); parsing is tolerant and returns None for anything missing.
"""
from __future__ import annotations

import json
import sqlite3
from typing import Any

from ..db import kv_get, kv_set
from ..log import get_logger
from ..net.budget import Blocked, Budget
from ..net.http import HttpError, request_json
from ..util import fnum

log = get_logger("prices")

DEXSCREENER_TOKENS = "https://api.dexscreener.com/tokens/v1/solana/"
GECKO_TOKEN = "https://api.geckoterminal.com/api/v2/networks/solana/tokens/"
USD_QUOTES = {"USDC", "USDT", "USD1", "PYUSD"}


def best_pair_per_token(pairs: Any) -> dict[str, dict[str, Any]]:
    """Highest-liquidity pair per base token address."""
    out: dict[str, dict[str, Any]] = {}
    if not isinstance(pairs, list):
        return out
    for p in pairs:
        if not isinstance(p, dict):
            continue
        base = (p.get("baseToken") or {}).get("address")
        if not base:
            continue
        liq = fnum((p.get("liquidity") or {}).get("usd")) or 0.0
        cur = out.get(base)
        if cur is None or liq > (fnum((cur.get("liquidity") or {}).get("usd")) or 0.0):
            out[base] = p
    return out


def pair_to_sample(p: dict[str, Any]) -> dict[str, Any]:
    vol = p.get("volume") or {}
    txns = p.get("txns") or {}
    return {
        "price_usd": fnum(p.get("priceUsd")),
        "mcap_usd": fnum(p.get("marketCap")),
        "fdv_usd": fnum(p.get("fdv")),
        "liquidity_usd": fnum((p.get("liquidity") or {}).get("usd")),
        "volume_h24_usd": fnum(vol.get("h24")),
        "volume_h6_usd": fnum(vol.get("h6")),
        "volume_h1_usd": fnum(vol.get("h1")),
        "txns_h24": ((txns.get("h24") or {}).get("buys") or 0) + ((txns.get("h24") or {}).get("sells") or 0) if txns.get("h24") else None,
        "pair_created_at": fnum(p.get("pairCreatedAt")),
        "dex_id": p.get("dexId"),
        "pair_address": p.get("pairAddress"),
    }


def sol_usd_from_dexscreener(pairs: Any, wsol: str) -> float | None:
    """Price of WSOL from its deepest USD-quoted pair."""
    best = None
    best_liq = -1.0
    if not isinstance(pairs, list):
        return None
    for p in pairs:
        if not isinstance(p, dict):
            continue
        if (p.get("baseToken") or {}).get("address") != wsol:
            continue
        if str((p.get("quoteToken") or {}).get("symbol", "")).upper() not in USD_QUOTES:
            continue
        liq = fnum((p.get("liquidity") or {}).get("usd")) or 0.0
        price = fnum(p.get("priceUsd"))
        if price and liq > best_liq:
            best, best_liq = price, liq
    return best


class PriceFeed:
    def __init__(self, conn: sqlite3.Connection, budget: Budget, cfg: dict[str, Any], clock):
        self.conn = conn
        self.budget = budget
        self.cfg = cfg
        self.clock = clock

    # ----- SOL/USD ---------------------------------------------------------------------------
    def sol_usd(self) -> tuple[float | None, str]:
        """(price, source). Cached in kv for cache_s seconds; stale cache is returned (flagged)
        when every live source fails; None when nothing is known."""
        now = self.clock()
        cache_s = float(self.cfg["sol_usd"].get("cache_s", 60))
        raw = kv_get(self.conn, "sol_usd.cache")
        cached = json.loads(raw) if raw else None
        if cached and now - cached["ts"] <= cache_s:
            return cached["price"], f"cache:{cached['src']}"
        wsol = self.cfg["sol_usd"]["wsol_mint"]
        price, src = None, "none"
        try:
            pairs = request_json(self.budget, "dexscreener", "tokens", DEXSCREENER_TOKENS + wsol)
            price = sol_usd_from_dexscreener(pairs, wsol)
            src = "dexscreener"
        except (Blocked, HttpError) as e:
            log.info("sol_usd.dexscreener_unavailable", error=str(e)[:160])
        if price is None:
            try:
                data = request_json(self.budget, "geckoterminal", "token", GECKO_TOKEN + wsol)
                price = fnum((((data or {}).get("data") or {}).get("attributes") or {}).get("price_usd"))
                src = "geckoterminal"
            except (Blocked, HttpError) as e:
                log.info("sol_usd.geckoterminal_unavailable", error=str(e)[:160])
        if price is not None:
            kv_set(self.conn, "sol_usd.cache", json.dumps({"ts": now, "price": price, "src": src}), now)
            return price, src
        if cached:
            return cached["price"], f"stale:{cached['src']}:{int(now - cached['ts'])}s"
        return None, "none"

    # ----- market cap samples -------------------------------------------------------------------
    def sample_mints(self, mints: list[str], sol_usd: float | None) -> dict[str, int]:
        """Fetch DexScreener pairs for up to N mints per call and store one mcap_samples row per mint
        that has a pair. Mints without a pair are counted (not stored: unknown stays unknown)."""
        batch = int(self.cfg["sampling"].get("dexscreener_batch", 30))
        stats = {"requested": len(mints), "stored": 0, "no_pair": 0, "calls": 0, "blocked": 0, "errors": 0}
        for i in range(0, len(mints), batch):
            chunk = mints[i:i + batch]
            try:
                pairs = request_json(self.budget, "dexscreener", "tokens", DEXSCREENER_TOKENS + ",".join(chunk))
            except Blocked:
                stats["blocked"] += 1
                break
            except HttpError as e:
                stats["errors"] += 1
                log.warning("sample.dexscreener_error", error=str(e)[:160])
                continue
            stats["calls"] += 1
            best = best_pair_per_token(pairs)
            now = self.clock()
            self.conn.execute("BEGIN")
            try:
                for mint in chunk:
                    p = best.get(mint)
                    if p is None:
                        stats["no_pair"] += 1
                        continue
                    s = pair_to_sample(p)
                    self.conn.execute(
                        "INSERT OR REPLACE INTO mcap_samples(mint, ts, src, mcap_usd, fdv_usd, liquidity_usd, volume_h24_usd, price_usd, sol_usd, json)"
                        " VALUES(?,?,?,?,?,?,?,?,?,?)",
                        (mint, now, "dexscreener", s["mcap_usd"], s["fdv_usd"], s["liquidity_usd"], s["volume_h24_usd"], s["price_usd"],
                         sol_usd, json.dumps(s)),
                    )
                    stats["stored"] += 1
                self.conn.execute("COMMIT")
            except Exception:
                self.conn.execute("ROLLBACK")
                raise
        return stats
