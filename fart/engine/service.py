"""Engine daemon: every 15 s compute features as_of now, promote watchlist entries, and write
`observe` decisions for coins and metas that pass the thresholds. Observations only.

Transaction shape (milestone 3, after the VPS lock storm): every tick is
  1. one short IMMEDIATE write (expire the watchlist),
  2. a closed read snapshot (features of every watched coin: no writes inside),
  3. one short IMMEDIATE write (promotions),
  4. RPC enrichment of the strongest candidates (network, no transaction),
  5. a closed read snapshot (recent coins, clustering, meta features),
  6. one short IMMEDIATE write (decisions, meta_burst watchlist entries, heartbeat).
A tick that still fails is counted in kv (`engine.tick_failed`, `engine.tick_failed.<day>`) so
`fart status` and the daily report show it; the next tick runs 15 s later as before.
"""
from __future__ import annotations

import signal
import sqlite3
import time
from typing import Any

from .. import db
from ..chain.curve import CurveParams
from ..chain.rpc import RpcClient
from ..config import Settings
from ..ingest import watch
from ..log import get_logger
from ..net.budget import Budget
from ..util import now as wall_now
from ..version import rules_version
from . import decide, features
from .enrich import creator_activity, holder_concentration
from .meta import cluster, load_stopwords

log = get_logger("engine")


class Engine:
    def __init__(self, conn: sqlite3.Connection, settings: Settings, rpc: RpcClient | None = None):
        self.conn = conn
        self.settings = settings
        self.cfg = settings.config("engine")
        self.pf = settings.config("pumpfun")
        self.params = CurveParams.from_config(self.pf)
        self.stopwords = load_stopwords(settings.config("stopwords"))
        self.rpc = rpc
        self.rules_version = rules_version()
        self.stats: dict[str, int] = {}

    def _bump(self, k: str, n: int = 1) -> None:
        self.stats[k] = self.stats.get(k, 0) + n

    def tick(self, as_of: float) -> dict[str, Any]:
        """One loop iteration with as_of = our clock now. Returns a summary."""
        now = as_of
        wcfg = self.cfg["watchlist"]
        ccfg = dict(self.cfg["coin"])
        ccfg.setdefault("bundled_graduation_window_s", self.pf.get("bundled_graduation_window_s", 60))
        ccfg.setdefault("bundled_dev_buy_fraction", self.pf.get("bundled_dev_buy_fraction", 0.5))
        mcfg = self.cfg["meta"]
        cooldown = float(self.cfg.get("decision_cooldown_s", 1800))
        summary: dict[str, Any] = {"as_of": as_of, "coins": 0, "promoted": 0, "coin_decisions": 0, "meta_decisions": 0,
                                   "bursting_metas": 0, "rpc_lookups": 0}
        t_start = time.monotonic()

        # 1. expire (short write)
        db.write_tx(self.conn, lambda c: watch.expire(c, now, float(wcfg.get("dead_after_idle_s", 7200))), what="engine.expire")

        # 2. features (read snapshot, no writes)
        candidates: list[dict[str, Any]] = []
        promotions: list[dict[str, Any]] = []
        with db.read_tx(self.conn):
            mints = watch.active_mints(self.conn)
            summary["coins"] = len(mints)
            mega = features.mega_post_feature(self.conn, as_of)
            for mint in mints:
                f = features.coin_features(self.conn, mint, as_of, self.params, ccfg)
                if f is None:
                    continue
                f["mega_post"] = mega
                if decide.wants_promotion(f, wcfg):
                    promotions.append(f)
                passes, _ = decide.coin_verdict(f, ccfg)
                if passes or f["unique_buyers_5m"] >= int(ccfg["observe_min_unique_buyers_5m"]) // 2:
                    candidates.append(f)
        summary["features_s"] = round(time.monotonic() - t_start, 3)

        # 3. promotions (short write; promote_watchlist re-checks the probation reason)
        if promotions:
            def _promote(c: sqlite3.Connection) -> int:
                return sum(1 for f in promotions if decide.promote_watchlist(c, f, now, wcfg))

            summary["promoted"] = db.write_tx(self.conn, _promote, what="engine.promote")

        # 4. RPC enrichment for the strongest candidates only (network: outside every transaction)
        candidates.sort(key=lambda f: (f["net_sol_5m"], f["unique_buyers_5m"]), reverse=True)
        if self.rpc is not None:
            h_budget = int(ccfg.get("max_holder_lookups_per_loop", 3))
            c_budget = int(ccfg.get("max_creator_lookups_per_loop", 3))
            for f in candidates:
                if h_budget > 0 and self.rpc.available("getTokenLargestAccounts"):
                    f["holders"] = holder_concentration(self.rpc, f["mint"], int(ccfg.get("holder_concentration_top_n", 10)))
                    h_budget -= 1
                    summary["rpc_lookups"] += 1
                if c_budget > 0 and f.get("creator") and self.rpc.available("getSignaturesForAddress"):
                    f["creator_activity"] = creator_activity(self.rpc, f["creator"], as_of)
                    c_budget -= 1
                    summary["rpc_lookups"] += 1

        # 5. metas (read snapshot, no writes)
        with db.read_tx(self.conn):
            recent = features.recent_coins(self.conn, as_of, float(mcfg.get("window_s", 10800)))
            clusters = cluster(recent, self.stopwords, int(mcfg.get("min_token_len", 4)))
            metas = []
            for term, members in clusters.items():
                if len(members) < int(mcfg.get("min_coins", 3)):
                    continue
                metas.append(features.meta_features(self.conn, term, members, as_of, mcfg, self.stopwords))
            metas = features.rank_metas(metas)[: int(mcfg.get("max_terms", 50))]
        bursting = [m for m in metas if decide.is_bursting(m, mcfg)]
        summary["bursting_metas"] = len(bursting)
        summary["metas"] = len(metas)
        summary["top_metas"] = [(m["term"], m["sol_inflow"], m["unique_buyers"], m["n_coins"]) for m in metas[:5]]

        # 6. decisions, meta_burst watchlist entries, heartbeat (short write)
        def _write(c: sqlite3.Connection) -> tuple[int, int]:
            n_coin = n_meta = 0
            for f in candidates:
                if decide.observe_coin(c, f, as_of, now, ccfg, cooldown, self.rules_version) is not None:
                    n_coin += 1
            if wcfg.get("subscribe_bursting_meta", True):
                for m in bursting:
                    for mint in [x["mint"] for x in clusters[m["term"]]][:20]:
                        watch.add(c, mint, "meta_burst", now, float(wcfg.get("ttl_s", 86400)), int(wcfg.get("cap", 200)))
            for m in metas:
                if decide.observe_meta(c, m, as_of, now, mcfg, cooldown, self.rules_version) is not None:
                    n_meta += 1
            db.kv_set(c, "engine.heartbeat", str(now), now)
            db.kv_set(c, "engine.rules_version", self.rules_version, now)
            return n_coin, n_meta

        summary["coin_decisions"], summary["meta_decisions"] = db.write_tx(self.conn, _write, what="engine.decisions")
        summary["tick_s"] = round(time.monotonic() - t_start, 3)
        return summary


def run_engine(settings: Settings) -> int:
    settings.ensure_dirs()
    conn = db.connect(settings.db_path)
    budget = Budget.from_settings(conn, settings)
    rpc = RpcClient(budget, settings.rpc_http_url, settings.rpc_is_public)
    eng = Engine(conn, settings, rpc)
    interval = float(settings.config("engine").get("loop_interval_s", 15))
    stopping = {"v": False}

    def _stop(*_: Any) -> None:
        stopping["v"] = True

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    log.info("engine.started", rules_version=eng.rules_version, interval_s=interval, **settings.describe())
    last_log = 0.0
    failures = 0
    while not stopping["v"]:
        t0 = time.monotonic()
        try:
            s = eng.tick(wall_now())
            if s["coin_decisions"] or s["meta_decisions"] or time.monotonic() - last_log > 300:
                last_log = time.monotonic()
                log.info("engine.tick", **s)
        except Exception as e:
            failures += 1
            log.error("engine.tick_failed", exc_info=True, failures=failures)
            db.record_failure(conn, "engine.tick_failed", wall_now(), detail=f"{type(e).__name__}: {str(e)[:150]}")
        elapsed = time.monotonic() - t0
        for _ in range(int(max(0.0, interval - elapsed) * 10)):
            if stopping["v"]:
                break
            time.sleep(0.1)
    conn.close()
    log.info("engine.stopped", failures=failures)
    return 0
