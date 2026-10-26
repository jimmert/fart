"""Engine daemon: every 15 s compute features as_of now, promote watchlist entries, and write
`observe` decisions for coins and metas that pass the thresholds. Observations only."""
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
        cooldown = float(self.cfg.get("decision_cooldown_s", 1800))
        summary: dict[str, Any] = {"as_of": as_of, "coins": 0, "promoted": 0, "coin_decisions": 0, "meta_decisions": 0,
                                   "bursting_metas": 0, "rpc_lookups": 0}
        self.conn.execute("BEGIN")
        try:
            watch.expire(self.conn, now, float(wcfg.get("dead_after_idle_s", 7200)))
            mints = watch.active_mints(self.conn)
            summary["coins"] = len(mints)
            candidates: list[dict[str, Any]] = []
            for mint in mints:
                f = features.coin_features(self.conn, mint, as_of, self.params, ccfg)
                if f is None:
                    continue
                if decide.promote_watchlist(self.conn, f, now, wcfg):
                    summary["promoted"] += 1
                passes, _ = decide.coin_verdict(f, ccfg)
                if passes or f["unique_buyers_5m"] >= int(ccfg["observe_min_unique_buyers_5m"]) // 2:
                    candidates.append(f)
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise

        # RPC enrichment for the strongest candidates only (outside the transaction: network)
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

        self.conn.execute("BEGIN")
        try:
            for f in candidates:
                if decide.observe_coin(self.conn, f, as_of, now, ccfg, cooldown, self.rules_version) is not None:
                    summary["coin_decisions"] += 1
            # metas
            mcfg = self.cfg["meta"]
            recent = features.recent_coins(self.conn, as_of, float(mcfg.get("window_s", 10800)))
            clusters = cluster(recent, self.stopwords, int(mcfg.get("min_token_len", 4)))
            metas = []
            for term, members in clusters.items():
                if len(members) < int(mcfg.get("min_coins", 3)):
                    continue
                metas.append(features.meta_features(self.conn, term, members, as_of, mcfg, self.stopwords))
            metas = features.rank_metas(metas)[: int(mcfg.get("max_terms", 50))]
            for m in metas:
                if decide.is_bursting(m, mcfg):
                    summary["bursting_metas"] += 1
                    if wcfg.get("subscribe_bursting_meta", True):
                        for mint in [c["mint"] for c in clusters[m["term"]]][:20]:
                            watch.add(self.conn, mint, "meta_burst", now, float(wcfg.get("ttl_s", 86400)), int(wcfg.get("cap", 200)))
                if decide.observe_meta(self.conn, m, as_of, now, mcfg, cooldown, self.rules_version) is not None:
                    summary["meta_decisions"] += 1
            summary["metas"] = len(metas)
            summary["top_metas"] = [(m["term"], m["sol_inflow"], m["unique_buyers"], m["n_coins"]) for m in metas[:5]]
            db.kv_set(self.conn, "engine.heartbeat", str(now), now)
            db.kv_set(self.conn, "engine.rules_version", self.rules_version, now)
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise
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
    while not stopping["v"]:
        t0 = time.monotonic()
        try:
            s = eng.tick(wall_now())
            if s["coin_decisions"] or s["meta_decisions"] or time.monotonic() - last_log > 300:
                last_log = time.monotonic()
                log.info("engine.tick", **s)
        except Exception:
            log.error("engine.tick_failed", exc_info=True)
        elapsed = time.monotonic() - t0
        for _ in range(int(max(0.0, interval - elapsed) * 10)):
            if stopping["v"]:
                break
            time.sleep(0.1)
    conn.close()
    log.info("engine.stopped")
    return 0
