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
    closed = coverage.close_dangling_gaps(conn, now, relabel="restart")
    if closed:
        log.info("gaps.closed_at_startup", n=closed, reason="restart")
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
                          trades_enabled=settings.pumpportal_trades_enabled)
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
