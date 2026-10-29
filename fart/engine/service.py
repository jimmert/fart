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

Milestone 11: on the VPS a tick took 590 to 598 s (29 Oct 2026, 1,494,055 trades, 145,299 coins): step 5 fetched and
tokenized the 88,736 baseline coins once per term, hundreds of times per tick. The baseline is now one fetch and one
tokenization per tick (`features.meta_baseline`, handed to every `meta_features` call by `themes.compute_metas`), with the
same outcomes byte for byte (a test proves it against the frozen old implementation). Every tick logs the duration of
every step (`expire_s`, `features_s`, `promote_s`, `rpc_s`, `metas_s`, `decisions_s`, `tick_s`; `engine.tick` is logged
every tick now), the previous tick's complete record goes to kv `engine.tick_last` inside the decision write, a tick
longer than the loop interval is counted (`engine.tick_slow`, per day too) and warned about at most once per hour
(`fart/engine/tickstats.py`). No work is skipped when a tick runs long: it is made visible.
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
from ..log import get_logger
from ..net.budget import Budget
from ..util import now as wall_now
from ..version import rules_fingerprint, rules_version
from . import candidates, decide, features, themes, tickstats
from .enrich import creator_activity, holder_concentration
from .meta import load_stopwords

log = get_logger("engine")

# The X fields of `features.coin_features()` as `features.x_features()` returns them on empty social tables.
# In `off` and `shadow` mode the engine writes exactly these, whatever the social daemon stored (milestone 6).
X_NEUTRAL: dict[str, Any] = {"x_mentions_1h": 0, "x_distinct_authors_1h": 0, "x_follower_weighted_1h": 0.0, "x_first_mention_ts": None,
                             "x_count_1h": None, "x_count_24h": None, "x_count_ts": None}


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
        self.interval = float(self.cfg.get("loop_interval_s", 15))
        self.last_tick: dict[str, Any] | None = None   # milestone 11: the previous tick's budget, written by the next tick's decision write

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

        # 1. expire and trim (short write): rows past their TTL or idle, then meta_burst rows above the bound
        #    that are not part of the frozen choice (milestone 5; a row that traded in the last 10 min stays)
        frozen_mints = [m for s_ in self.burst_sets.values() for m in s_.get("mints", [])]
        max_total = int(wcfg.get("meta_burst_max_total", 40))

        def _expire(c: sqlite3.Connection) -> tuple[int, int]:
            expired = watch.expire(c, now, float(wcfg.get("dead_after_idle_s", 7200)))
            trimmed = watch.trim(c, "meta_burst", max_total, frozen_mints, now)
            return expired, trimmed

        summary["expired"], summary["meta_burst_trimmed"] = db.write_tx(self.conn, _expire, what="engine.expire")
        summary["expire_s"] = round(time.monotonic() - t_start, 3)
        t_step = time.monotonic()

        # 2. features (read snapshot, no writes). X reaches a decision only in live mode (milestone 6).
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
        summary["features_s"] = round(time.monotonic() - t_step, 3)
        t_step = time.monotonic()

        # 3. promotions (short write; promote_watchlist re-checks the probation reason)
        if promotions:
            def _promote(c: sqlite3.Connection) -> int:
                return sum(1 for f in promotions if decide.promote_watchlist(c, f, now, wcfg))

            summary["promoted"] = db.write_tx(self.conn, _promote, what="engine.promote")
        summary["promote_s"] = round(time.monotonic() - t_step, 3)
        t_step = time.monotonic()

        # 4. RPC enrichment for the strongest candidates only (network: outside every transaction)
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

        summary["rpc_s"] = round(time.monotonic() - t_step, 3)
        t_step = time.monotonic()

        # 5. metas (read snapshot, no writes; the baseline once per tick, milestone 11)
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
        summary["baseline_coins"] = None if baseline is None else baseline.n_coins
        summary["metas_s"] = round(time.monotonic() - t_step, 3)
        t_step = time.monotonic()

        # 6. decisions, meta_burst watchlist entries, heartbeat (short write)
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
            # milestone 11: the complete record of the previous tick (its own duration is known only after this commit)
            tickstats.write_previous(c, self.last_tick, now)
            db.kv_set(c, "engine.heartbeat", str(now), now)
            db.kv_set(c, "engine.rules_version", self.rules_version, now)
            db.kv_set(c, f"engine.rules_fingerprint.{self.rules_version}", self.rules_fingerprint, now)
            db.kv_set(c, "engine.x_mode", self.x_mode, now)
            return n_coin, n_meta, n_added

        summary["coin_decisions"], summary["meta_decisions"], summary["meta_burst_watched"] = db.write_tx(self.conn, _write, what="engine.decisions")
        self.burst_sets = new_sets
        summary["decisions_s"] = round(time.monotonic() - t_step, 3)
        summary["tick_s"] = round(time.monotonic() - t_start, 3)
        summary["interval_s"] = self.interval
        summary["slow"] = summary["tick_s"] > self.interval
        self.last_tick = tickstats.record(summary, self.interval)
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
    if settings.x_mode_note:
        log.warning("engine.x_mode_note", note=settings.x_mode_note)
    slow = tickstats.SlowTickReporter()
    failures = 0
    while not stopping["v"]:
        t0 = time.monotonic()
        try:
            s = eng.tick(wall_now())
            log.info("engine.tick", **s)   # every tick (milestone 11): the duration of every step is the point of the line
            warn = slow.note(s)            # a tick longer than the interval: counted in kv by the next tick, warned at most once per hour
            if warn:
                log.warning("engine.tick_slow", **warn)
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
