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

Milestone 4: the meta_burst watchlist entries are a stable, bounded choice (`decide.meta_burst_sets`:
the `meta_burst_per_meta` youngest coins of every bursting meta, at most `meta_burst_max_total` in
all, frozen while the meta bursts, released `meta_burst_release_s` after it stopped bursting) that
is persisted in kv `engine.meta_burst_sets`, so a restart continues with the same set. Before, every
tick re-added up to 20 coins of every bursting meta, evicted the oldest equal-priority entries and
made the trade stream re-subscribe the difference (5,298 subscriptions in 52 minutes on the VPS).

Milestone 5: step 1 also trims `meta_burst` rows above `meta_burst_max_total` that are not in the frozen
sets (oldest first, never a row that traded in the last 10 minutes): after the M4 upgrade 195 M3 rows kept
their slots until their TTL and left probation 5 of its 100 reserved slots. The engine also records the
fingerprint of the rule-defining files next to its rules_version (kv `engine.rules_fingerprint.<rv>`) so the
week report can pool rules_versions that ran identical rules.

Milestone 6: (a) X reaches the decisions only in `live` mode (`Settings.x_mode`: X_MODE=live plus the
`x_live` rules key). In `off` and `shadow` the engine passes no mega-post feature and overwrites the X fields
of every coin's features with the values of empty social tables (`X_NEUTRAL`, pinned to `features.x_features`
by a test), so the decisions of a shadow day are byte for byte the decisions of a day without X; the rule
files are untouched. (b) Every tick the engine records, per watched coin and per ranked meta, the highest
threshold features of the day in `engine_candidates` (`fart/engine/candidates.py`), after the verdicts and
inside the same short write; `fart engine explain --day` reads them.

Milestone 10: the ranked metas of step 5 (the top `max_terms`, with the fields the website's themes card shows) are
written as JSON in kv `engine.metas_latest` in the same decision transaction (`fart/engine/themes.py`), so the website
reads the engine's themes instead of repeating the clustering per visit. Nothing else of the tick changes.

Milestone 11: the tick is seconds again instead of minutes (166 to 337 s on the VPS on 26 Oct 2026, all of it in step 5:
the baseline of every term was fetched and tokenized again per term; `features.meta_baseline` now does it once per tick,
with the outcomes proven byte-equal by a test). The tick has a budget and is visible: every step's duration (`expire_s`,
`features_s`, `promote_s`, `rpc_s`, `metas_s`, `decisions_s`) is logged per tick (`engine.tick_steps`) and kept in the
summary; a tick longer than the loop interval is counted (kv `engine.tick_slow` and `engine.tick_slow.<day>`, with the
last one in `engine.tick_slow.last`) and logged at most once per hour (`engine.tick_slow`, with the number of slow ticks
since the previous line). The durations of the last completed tick go to kv `engine.tick_last` inside the next tick's
decision transaction (no write outside the existing ones), where `fart status` and `/eigenaar` read them. No work is
skipped when a tick is slow: it becomes visible, never hidden.
"""
from __future__ import annotations

import json
import signal
import sqlite3
import time
from typing import Any

from .. import db
from ..chain.curve import CurveParams
from ..chain.rpc import RpcClient
from ..config import Settings
from ..ingest import watch
from ..log import Throttle, get_logger
from ..net.budget import Budget
from ..util import now as wall_now
from ..version import rules_fingerprint, rules_version
from . import candidates, decide, features, themes
from .enrich import creator_activity, holder_concentration
from .meta import load_stopwords

log = get_logger("engine")

# The X fields of `features.coin_features()` as `features.x_features()` returns them on empty social tables.
# In `off` and `shadow` mode the engine writes exactly these, whatever the social daemon stored (milestone 6).
X_NEUTRAL: dict[str, Any] = {"x_mentions_1h": 0, "x_distinct_authors_1h": 0, "x_follower_weighted_1h": 0.0, "x_first_mention_ts": None,
                             "x_count_1h": None, "x_count_24h": None, "x_count_ts": None}

# The six steps of a tick, in order; their durations are logged per tick and written to kv (milestone 11).
STEPS = ("expire_s", "features_s", "promote_s", "rpc_s", "metas_s", "decisions_s")
SLOW_LOG_PERIOD_S = 3600.0   # a slow tick (longer than the loop interval) is logged at most once per hour; every one is counted
KV_TICK_LAST = "engine.tick_last"
KV_TICK_SLOW = "engine.tick_slow"


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
        self.rules_fingerprint = rules_fingerprint()
        self.x_mode = settings.x_mode
        self.x_live = settings.x_live
        self.stats: dict[str, int] = {}
        self.burst_sets: dict[str, dict[str, Any]] = self._load_burst_sets()
        # milestone 11: the tick budget. The durations of the running tick (partial after a failure), the last completed
        # tick (written to kv in the next decision write), the slow ticks not yet counted in kv, the hourly log throttle.
        self.interval_s = float(self.cfg.get("loop_interval_s", 15))
        self.steps: dict[str, float] = {}
        self.last_tick: dict[str, Any] | None = None
        self.slow_pending = 0
        self.slow_total = 0
        self.slow_since_log = 0
        self.slow_last: dict[str, Any] | None = None
        self.slow_log = Throttle(SLOW_LOG_PERIOD_S)

    def _bump(self, k: str, n: int = 1) -> None:
        self.stats[k] = self.stats.get(k, 0) + n

    def _load_burst_sets(self) -> dict[str, dict[str, Any]]:
        """The meta_burst choice of the previous run (kv), so a restart keeps the same stable set."""
        raw = db.kv_get(self.conn, "engine.meta_burst_sets")
        if not raw:
            return {}
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return {}
        return {str(k): v for k, v in data.items() if isinstance(v, dict) and isinstance(v.get("mints"), list)} if isinstance(data, dict) else {}

    @staticmethod
    def _graduated(conn: sqlite3.Connection, mints: list[str]) -> set[str]:
        out: set[str] = set()
        for i in range(0, len(mints), 500):
            chunk = mints[i:i + 500]
            marks = ",".join("?" for _ in chunk)
            out.update(r[0] for r in conn.execute(f"SELECT mint FROM migrations WHERE mint IN ({marks})", chunk))
        return out

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
                                   "bursting_metas": 0, "rpc_lookups": 0, "meta_burst_sets": 0, "meta_burst_mints": 0,
                                   "meta_burst_released": 0, "meta_burst_trimmed": 0, "candidates_written": 0}
        cap, reserve = watch.limits(wcfg)
        t_start = time.monotonic()
        self.steps = {}

        # 1. expire and trim (short write): rows past their TTL or idle, then meta_burst rows above the bound
        #    that are not part of the frozen choice (milestone 5; a row that traded in the last 10 min stays)
        frozen_mints = [m for s_ in self.burst_sets.values() for m in s_.get("mints", [])]
        max_total = int(wcfg.get("meta_burst_max_total", 40))

        def _expire(c: sqlite3.Connection) -> tuple[int, int]:
            expired = watch.expire(c, now, float(wcfg.get("dead_after_idle_s", 7200)))
            trimmed = watch.trim(c, "meta_burst", max_total, frozen_mints, now)
            return expired, trimmed

        summary["expired"], summary["meta_burst_trimmed"] = db.write_tx(self.conn, _expire, what="engine.expire")
        self.steps["expire_s"] = time.monotonic() - t_start

        # 2. features (read snapshot, no writes). X reaches a decision only in live mode (milestone 6).
        t_step = time.monotonic()
        strong: list[dict[str, Any]] = []
        promotions: list[dict[str, Any]] = []
        coin_records: list[dict[str, Any]] = []
        with db.read_tx(self.conn):
            mints = watch.active_mints(self.conn)
            reasons = {r[0]: (r[1] or "probation") for r in self.conn.execute("SELECT mint, reason FROM watchlist WHERE active=1")}
            summary["coins"] = len(mints)
            mega = features.mega_post_feature(self.conn, as_of) if self.x_live else None
            for mint in mints:
                f = features.coin_features(self.conn, mint, as_of, self.params, ccfg)
                if f is None:
                    continue
                if not self.x_live:
                    f.update(X_NEUTRAL)
                f["mega_post"] = mega
                if decide.wants_promotion(f, wcfg):
                    promotions.append(f)
                passes, _ = decide.coin_verdict(f, ccfg)
                coin_records.append(candidates.coin_row(f, reasons.get(mint), ccfg, passes))
                if passes or f["unique_buyers_5m"] >= int(ccfg["observe_min_unique_buyers_5m"]) // 2:
                    strong.append(f)
        self.steps["features_s"] = time.monotonic() - t_step

        # 3. promotions (short write; promote_watchlist re-checks the probation reason)
        t_step = time.monotonic()
        if promotions:
            def _promote(c: sqlite3.Connection) -> int:
                return sum(1 for f in promotions if decide.promote_watchlist(c, f, now, wcfg))

            summary["promoted"] = db.write_tx(self.conn, _promote, what="engine.promote")
        self.steps["promote_s"] = time.monotonic() - t_step

        # 4. RPC enrichment for the strongest candidates only (network: outside every transaction)
        t_step = time.monotonic()
        strong.sort(key=lambda f: (f["net_sol_5m"], f["unique_buyers_5m"]), reverse=True)
        if self.rpc is not None:
            h_budget = int(ccfg.get("max_holder_lookups_per_loop", 3))
            c_budget = int(ccfg.get("max_creator_lookups_per_loop", 3))
            for f in strong:
                if h_budget > 0 and self.rpc.available("getTokenLargestAccounts"):
                    f["holders"] = holder_concentration(self.rpc, f["mint"], int(ccfg.get("holder_concentration_top_n", 10)))
                    h_budget -= 1
                    summary["rpc_lookups"] += 1
                if c_budget > 0 and f.get("creator") and self.rpc.available("getSignaturesForAddress"):
                    f["creator_activity"] = creator_activity(self.rpc, f["creator"], as_of)
                    c_budget -= 1
                    summary["rpc_lookups"] += 1
        self.steps["rpc_s"] = time.monotonic() - t_step

        # 5. metas (read snapshot, no writes); the baseline of the terms is computed once here (milestone 11)
        t_step = time.monotonic()
        with db.read_tx(self.conn):
            recent, clusters, metas, baseline = themes.compute_metas(self.conn, as_of, mcfg, self.stopwords)
            bursting = [m for m in metas if decide.is_bursting(m, mcfg)]
            meta_records = [candidates.meta_row(m, decide.is_bursting(m, mcfg), decide.meta_verdict(m, mcfg)[0]) for m in metas]
            # the stable meta_burst choice (pure, computed here so the write below stays milliseconds)
            burst_terms = [m["term"] for m in bursting] if wcfg.get("subscribe_bursting_meta", True) else []
            members: dict[str, list[str]] = {}
            for term in burst_terms:
                # youngest first: the coins a burst is made of are the ones that just appeared
                members[term] = [x["mint"] for x in sorted(clusters[term], key=lambda x: float(x.get("t") or 0.0), reverse=True)]
            chosen_before = [m for s_ in self.burst_sets.values() for m in s_.get("mints", [])]
            graduated = self._graduated(self.conn, sorted({m for ms in members.values() for m in ms} | set(chosen_before)))
        new_sets, released = decide.meta_burst_sets(
            self.burst_sets, burst_terms, lambda t: members.get(t, []), graduated, now,
            int(wcfg.get("meta_burst_per_meta", 5)), int(wcfg.get("meta_burst_max_total", 40)), float(wcfg.get("meta_burst_release_s", 600)))
        burst_mints = [m for s_ in new_sets.values() for m in s_["mints"]]
        summary["bursting_metas"] = len(bursting)
        summary["metas"] = len(metas)
        summary["top_metas"] = [(m["term"], m["sol_inflow"], m["unique_buyers"], m["n_coins"]) for m in metas[:5]]
        summary["meta_burst_sets"], summary["meta_burst_mints"], summary["meta_burst_released"] = len(new_sets), len(burst_mints), len(released)
        summary["recent_coins"], summary["baseline_coins"] = len(recent), baseline.n_coins
        self.steps["metas_s"] = time.monotonic() - t_step

        # 6. decisions, meta_burst watchlist entries, heartbeat (short write)
        t_step = time.monotonic()

        def _write(c: sqlite3.Connection) -> tuple[int, int, int]:
            n_coin = n_meta = n_added = 0
            for f in strong:
                if decide.observe_coin(c, f, as_of, now, ccfg, cooldown, self.rules_version) is not None:
                    n_coin += 1
            if released:
                watch.release(c, released, "meta_burst", now)
            for mint in burst_mints:
                # a no-op for an active row (keeps its `since`), a reactivation for an expired one
                if watch.add(c, mint, "meta_burst", now, float(wcfg.get("ttl_s", 86400)), cap, reserve):
                    n_added += 1
            for m in metas:
                if decide.observe_meta(c, m, as_of, now, mcfg, cooldown, self.rules_version) is not None:
                    n_meta += 1
            if new_sets != self.burst_sets or released:
                db.kv_set(c, "engine.meta_burst_sets", json.dumps(new_sets, sort_keys=True), now)
            # milestone 10: the ranked metas for the website (the same transaction, the same as_of)
            themes.write(c, themes.snapshot(metas, clusters, as_of, mcfg, len(recent), set(new_sets), themes.observed_terms_today(c, as_of), self.rules_version), now)
            # milestone 6: the day's candidate maxima (after the verdicts; never read by the engine)
            summary["candidates_written"] = (candidates.write_coins(c, coin_records, as_of, self.rules_version)
                                             + candidates.write_metas(c, meta_records, as_of, self.rules_version))
            db.kv_set(c, "engine.heartbeat", str(now), now)
            db.kv_set(c, "engine.rules_version", self.rules_version, now)
            db.kv_set(c, f"engine.rules_fingerprint.{self.rules_version}", self.rules_fingerprint, now)
            db.kv_set(c, "engine.x_mode", self.x_mode, now)
            # milestone 11: the previous tick's durations and the slow ticks since the last write, in this same transaction
            self._write_tick_stats(c, now)
            return n_coin, n_meta, n_added

        summary["coin_decisions"], summary["meta_decisions"], summary["meta_burst_watched"] = db.write_tx(self.conn, _write, what="engine.decisions")
        self.slow_pending = 0
        self.burst_sets = new_sets
        self.steps["decisions_s"] = time.monotonic() - t_step
        tick_s = time.monotonic() - t_start
        summary.update({k: round(self.steps.get(k, 0.0), 3) for k in STEPS})
        summary["tick_s"] = round(tick_s, 3)
        summary["slow"] = tick_s > self.interval_s
        self.record_tick(as_of, tick_s, True, summary)
        return summary

    # ----- the tick budget (milestone 11) ---------------------------------------------------------------------------
    def record_tick(self, as_of: float, tick_s: float, ok: bool, summary: dict[str, Any] | None = None) -> dict[str, Any]:
        """The tick that just ended: its durations are logged (`engine.tick_steps`), kept for the next decision write
        (`engine.tick_last`) and, when the tick took longer than the loop interval, counted as slow (kv in the next
        decision write; `engine.tick_slow` logged at most once per hour with the number of slow ticks since the previous
        line). A failed tick is recorded too, with the durations of the steps it completed."""
        s = summary or {}
        rec: dict[str, Any] = {"as_of": float(as_of), "tick_s": round(float(tick_s), 3), "ok": bool(ok), "interval_s": self.interval_s,
                               "slow": float(tick_s) > self.interval_s, **{k: round(self.steps.get(k, 0.0), 3) for k in STEPS},
                               "coins": s.get("coins"), "metas": s.get("metas"), "recent_coins": s.get("recent_coins"), "baseline_coins": s.get("baseline_coins"),
                               "coin_decisions": s.get("coin_decisions"), "meta_decisions": s.get("meta_decisions")}
        self.last_tick = rec
        log.info("engine.tick_steps", **rec)
        if rec["slow"]:
            self.slow_pending += 1
            self.slow_total += 1
            self.slow_since_log += 1
            self.slow_last = rec
            if self.slow_log.allow("tick_slow"):
                log.warning("engine.tick_slow", slow_since_last_log=self.slow_since_log, slow_total=self.slow_total, **rec)
                self.slow_since_log = 0
        return rec

    def _write_tick_stats(self, c: sqlite3.Connection, now: float) -> None:
        """Inside the decision transaction: the last completed tick and the slow ticks not yet counted."""
        if self.last_tick is not None:
            db.kv_set(c, KV_TICK_LAST, json.dumps(self.last_tick, sort_keys=True), now)
        if self.slow_pending and self.slow_last is not None:
            db.counter_bump_daily(c, KV_TICK_SLOW, self.slow_pending, now)
            db.kv_set(c, f"{KV_TICK_SLOW}.last", json.dumps(self.slow_last, sort_keys=True), now)
            db.kv_set(c, f"{KV_TICK_SLOW}.last_at", str(self.slow_last["as_of"]), now)


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
    if settings.x_mode_note:
        log.warning("engine.x_mode_note", note=settings.x_mode_note)
    last_log = 0.0
    failures = 0
    while not stopping["v"]:
        t0 = time.monotonic()
        as_of = wall_now()
        try:
            s = eng.tick(as_of)
            if s["coin_decisions"] or s["meta_decisions"] or time.monotonic() - last_log > 300:
                last_log = time.monotonic()
                log.info("engine.tick", **s)
        except Exception as e:
            failures += 1
            log.error("engine.tick_failed", exc_info=True, failures=failures)
            eng.record_tick(as_of, time.monotonic() - t0, False)   # the steps it completed; counted as slow when it was
            db.record_failure(conn, "engine.tick_failed", wall_now(), detail=f"{type(e).__name__}: {str(e)[:150]}")
        elapsed = time.monotonic() - t0
        for _ in range(int(max(0.0, interval - elapsed) * 10)):
            if stopping["v"]:
                break
            time.sleep(0.1)
    conn.close()
    log.info("engine.stopped", failures=failures)
    return 0
