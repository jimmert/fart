"""The ingest daemon: PumpPortal stream + RPC log stream + RPC per-mint trade stream + periodic
sweep/coverage/retention, plus a Global-account check at startup (logged, never fatal)."""
from __future__ import annotations

import asyncio
import json
import signal
import time
from typing import Any

from .. import db
from ..chain.global_account import fetch_and_check
from ..chain.rpc import RpcClient
from ..config import Settings
from ..log import get_logger
from ..net.budget import Budget
from ..util import now as wall_now
from . import coverage
from .pumpportal import PumpPortalClient
from .raw import RawWriter
from .retention import maybe_run_retention
from .rpc_logs import RpcLogsClient
from .rpc_trades import RpcTradesClient, fill_block_times
from .sweep import process_enrich_queue, sweep_signatures

log = get_logger("ingest")


def _sweep_job(settings: Settings) -> dict[str, Any]:
    """Runs in a worker thread with its own connection and budget handle."""
    conn = db.connect(settings.db_path)
    try:
        budget = Budget.from_settings(conn, settings)
        rpc = RpcClient(budget, settings.rpc_http_url, settings.rpc_is_public)
        icfg = settings.config("ingest")
        pf = settings.config("pumpfun")
        now = wall_now()
        out: dict[str, Any] = {}
        if icfg["backfill"].get("enabled", True):
            out["sweep"] = sweep_signatures(conn, rpc, {"create": pf["mint_authority"], "migrate": pf["migration_account"]},
                                            icfg["backfill"], now)
        if icfg["enrich"].get("enabled", True):
            cfg = dict(icfg["backfill"])
            cfg.update(icfg["enrich"])
            out["enrich"] = process_enrich_queue(conn, rpc, cfg, now)
        tcfg = icfg.get("rpc_trades", {})
        if tcfg.get("enabled", True):
            out["block_times"] = fill_block_times(conn, rpc, int(tcfg.get("max_block_time_lookups_per_run", 20)), now)
        return out
    finally:
        conn.close()


def global_check_job(settings: Settings) -> dict[str, Any]:
    """Fetch the pump.fun Global account and compare it with config/pumpfun.json. Logs a mismatch,
    never raises; the result is kept in kv for `fart status`."""
    conn = db.connect(settings.db_path)
    try:
        budget = Budget.from_settings(conn, settings)
        rpc = RpcClient(budget, settings.rpc_http_url, settings.rpc_is_public)
        res = fetch_and_check(rpc, settings.config("pumpfun"))
        now = wall_now()
        db.kv_set(conn, "chain.global.checked_at", str(now), now)
        db.kv_set(conn, "chain.global.status", res["status"], now)
        db.kv_set(conn, "chain.global.mismatches", json.dumps(res.get("mismatches", []), default=str), now)
        if res["status"] == "ok":
            log.info("global.check_ok", address=res["address"], to_verify=(res.get("decoded") or {}).get("to_verify"))
        elif res["status"] == "mismatch":
            log.warning("global.check_mismatch", mismatches=res["mismatches"], note="config/pumpfun.json differs from the chain; see `fart chain global`")
        else:
            log.warning("global.check_unavailable", status=res["status"], error=res.get("error"))
        return res
    finally:
        conn.close()


async def _periodic(settings: Settings, conn, stop: asyncio.Event, sweep_interval_s: float) -> None:
    icfg = settings.config("ingest")
    last_sweep = 0.0
    last_coverage = 0.0
    last_retention_check = 0.0
    sweep_running = False
    while not stop.is_set():
        now = wall_now()
        try:
            db.kv_set(conn, "ingest.heartbeat", str(now), now)
            if now - last_coverage >= 60:
                last_coverage = now
                coverage.recompute_recent(conn, now, hours=3)
            if not sweep_running and now - last_sweep >= sweep_interval_s:
                last_sweep = now
                sweep_running = True
                try:
                    res = await asyncio.to_thread(_sweep_job, settings)
                    log.debug("sweep.result", **{k: str(v)[:300] for k, v in res.items()})
                finally:
                    sweep_running = False
            if now - last_retention_check >= 3600:
                last_retention_check = now
                maybe_run_retention(conn, settings.raw_dir, settings.archive_dir, icfg["retention"], now)
        except Exception:
            log.error("periodic.failed", exc_info=True)
        try:
            await asyncio.wait_for(stop.wait(), timeout=5.0)
        except asyncio.TimeoutError:
            pass


async def _global_check(settings: Settings) -> None:
    try:
        await asyncio.to_thread(global_check_job, settings)
    except Exception:
        log.error("global.check_failed", exc_info=True)


async def _main(settings: Settings) -> None:
    conn = db.connect(settings.db_path)
    budget = Budget.from_settings(conn, settings)
    now = wall_now()
    # a crash or a host reboot leaves no shutdown gap: the previous heartbeat tells how long we were down
    last_hb = db.kv_get(conn, "ingest.heartbeat")
    down = coverage.record_downtime(conn, float(last_hb) if last_hb else None, now)
    if down:
        log.warning("gaps.downtime_recorded", n=down, since=last_hb, seconds=round(now - float(last_hb)))
    closed = coverage.close_dangling_gaps(conn, now, relabel="restart")
    if closed:
        log.info("gaps.closed_at_startup", n=closed, reason="restart")
    db.kv_setdefault(conn, "ingest.first_started", str(now), now)
    icfg = settings.config("ingest")
    wcfg = settings.config("engine")["watchlist"]
    pf = settings.config("pumpfun")
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, stop.set)
        except (NotImplementedError, RuntimeError):
            pass

    log.info("pp.mode", trades="pumpportal (PUMPPORTAL_API_KEY configured)" if settings.pumpportal_trades_enabled
             else "creates+migrations only (no PUMPPORTAL_API_KEY); per-mint trades via RPC")
    pp_raw = RawWriter(settings.raw_dir, "pumpportal", icfg["pumpportal"].get("raw_flush_interval_s", 5), icfg["pumpportal"].get("raw_flush_lines", 200))
    pp = PumpPortalClient(conn, budget, pp_raw, settings.pumpportal_connect_url, icfg["pumpportal"], wcfg,
                          trades_enabled=settings.pumpportal_trades_enabled, pumpfun_cfg=pf)
    tasks = [asyncio.create_task(pp.run(stop), name="pumpportal")]
    rpc_source = "rpc_public" if settings.rpc_is_public else "rpc"
    rl = None
    if icfg["rpc_logs"].get("enabled", True):
        rl_raw = RawWriter(settings.raw_dir, "rpc_logs", 5, 200)
        rl = RpcLogsClient(conn, budget, rl_raw, settings.rpc_wss_url, rpc_source, icfg["rpc_logs"], pf, wcfg)
        tasks.append(asyncio.create_task(rl.run(stop), name="rpc_logs"))
    rt = None
    tcfg = icfg.get("rpc_trades", {})
    if tcfg.get("enabled", True):
        if settings.rpc_is_public and tcfg.get("require_keyed_rpc", True):
            log.warning("rpc_trades.disabled", reason="public RPC fallback (no RPC_HTTP_URL/RPC_WSS_URL): per-mint subscriptions need a keyed endpoint")
        else:
            rt_raw = RawWriter(settings.raw_dir, "rpc_trades", 5, 200)
            rt = RpcTradesClient(conn, budget, rt_raw, settings.rpc_wss_url, rpc_source, tcfg, wcfg, pf)
            tasks.append(asyncio.create_task(rt.run(stop), name="rpc_trades"))
            log.info("rpc_trades.limits", reasons=tcfg.get("reasons"), daily_cu_share=rt.daily_cu_share(),
                     hourly_cu_allowance=rt.hourly_allowance(),
                     max_notifications_per_subscription_per_hour=tcfg.get("max_notifications_per_subscription_per_hour"),
                     cap=wcfg.get("rpc_trades_cap"), unsubscribe_after_s=tcfg.get("unsubscribe_after_s"),
                     subscribe_after_syncs=tcfg.get("subscribe_after_syncs"))
    else:
        log.info("rpc_trades.disabled", reason="config/ingest.json rpc_trades.enabled is false")
    tasks.append(asyncio.create_task(_periodic(settings, conn, stop, float(icfg["backfill"].get("sweep_interval_s", 300))), name="periodic"))
    check_task = asyncio.create_task(_global_check(settings), name="global_check")
    log.info("ingest.started", **settings.describe(), rpc_logs=rl is not None, rpc_trades=rt is not None)
    t_stats = time.monotonic()
    while not stop.is_set():
        try:
            await asyncio.wait_for(stop.wait(), timeout=60.0)
        except asyncio.TimeoutError:
            pass
        if time.monotonic() - t_stats >= 300:
            t_stats = time.monotonic()
            log.info("ingest.stats", pumpportal=pp.stats(), rpc_logs=rl.stats() if rl else None, rpc_trades=rt.stats() if rt else None)
    log.info("ingest.stopping")
    done, pending = await asyncio.wait(tasks, timeout=15.0)
    for t in pending:
        t.cancel()
    check_task.cancel()
    await asyncio.gather(*tasks, check_task, return_exceptions=True)
    pp_raw.close()
    conn.close()
    log.info("ingest.stopped")


def run_ingest(settings: Settings) -> int:
    settings.ensure_dirs()
    asyncio.run(_main(settings))
    return 0


# ----- replay of raw hour files (milestone 3) --------------------------------------------------------
REPLAY_SOURCES = ("pumpportal", "rpc_logs", "rpc_trades")


def replay_raw(settings: Settings, source: str, since: float, until: float | None = None, log_every: int = 5000) -> dict[str, Any]:
    """Re-feed the raw hour files of one source to its handler with the recorded received_at, data
    writes only (no raw copy, no costs, no counters). Idempotent: coins/migrations merge, trades
    de-duplicate on (signature, mint). Use after `ingest.handle_failed.<source>` counted failures:
    `fart ingest replay --source rpc_logs --since 2026-10-26T08:00Z`."""
    from .raw import RawWriter, iter_raw

    if source not in REPLAY_SOURCES:
        raise ValueError(f"unknown source {source!r}; one of {REPLAY_SOURCES}")
    until = wall_now() if until is None else until
    conn = db.connect(settings.db_path)
    try:
        budget = Budget.from_settings(conn, settings)
        icfg = settings.config("ingest")
        wcfg = settings.config("engine")["watchlist"]
        pf = settings.config("pumpfun")
        raw = RawWriter(settings.raw_dir, source)
        rpc_source = "rpc_public" if settings.rpc_is_public else "rpc"
        if source == "pumpportal":
            client: Any = PumpPortalClient(conn, budget, raw, "wss://replay.invalid", icfg["pumpportal"], wcfg,
                                           connect=lambda *a, **k: None, trades_enabled=settings.pumpportal_trades_enabled, pumpfun_cfg=pf)
        elif source == "rpc_logs":
            client = RpcLogsClient(conn, budget, raw, "wss://replay.invalid", rpc_source, icfg["rpc_logs"], pf, wcfg, connect=lambda *a, **k: None)
        else:
            client = RpcTradesClient(conn, budget, raw, "wss://replay.invalid", rpc_source, icfg.get("rpc_trades", {}), wcfg, pf,
                                     connect=lambda *a, **k: None)
        client.replay = True
        stats: dict[str, Any] = {"source": source, "files": 0, "records": 0, "replayed": 0, "skipped": 0, "errors": 0, "kinds": {}}
        hour = since - (since % 3600.0)
        while hour < until:
            path = raw.path_for(hour)
            hour += 3600.0
            if not path.exists():
                continue
            stats["files"] += 1
            for rec in iter_raw(path):
                stats["records"] += 1
                t = rec.get("received_at")
                msg = rec.get("msg")
                if not isinstance(t, (int, float)) or t < since or t > until:
                    stats["skipped"] += 1
                    continue
                text = msg if isinstance(msg, str) else json.dumps(msg, separators=(",", ":"), ensure_ascii=False)
                try:
                    kind = client.handle_text(text, float(t))
                    stats["replayed"] += 1
                    stats["kinds"][kind] = stats["kinds"].get(kind, 0) + 1
                except Exception as e:  # keep going: one bad line must not stop the replay
                    stats["errors"] += 1
                    if stats["errors"] <= 5:
                        log.error("replay.record_failed", error=f"{type(e).__name__}: {str(e)[:160]}", received_at=t)
                if stats["replayed"] % log_every == 0 and stats["replayed"]:
                    log.info("replay.progress", **{k: v for k, v in stats.items() if k != "kinds"})
        log.info("replay.done", **{k: v for k, v in stats.items() if k != "kinds"}, kinds=json.dumps(stats["kinds"]))
        return stats
    finally:
        conn.close()
