"""The social daemon: `fart social` (15 s loop) and `fart social --once`.

Without X_BEARER_TOKEN it idles with a heartbeat and says so once. Every tick runs the due jobs
(mega 60 s, refresh 120 s, KOL 180 s, confirmation 15 min per key) and the fast-post matching.
"""
from __future__ import annotations

import json
import signal
import time
from typing import Any

from .. import db
from ..config import Settings
from ..engine.meta import load_stopwords
from ..log import get_logger
from ..net.budget import Budget
from ..util import now as wall_now
from ..version import rules_version
from .x import XClient
from .xbudget import XBudget

log = get_logger("social")


def build_client(conn, settings: Settings) -> XClient:
    budget = Budget.from_settings(conn, settings)
    xcfg = settings.config("x")
    xb = XBudget(conn, budget, xcfg, settings.x_daily_budget_usd)
    kols = list(settings.config("x_kols").get("accounts", []))
    return XClient(conn, budget, xb, xcfg, kols, settings.x_bearer_token, load_stopwords(settings.config("stopwords")), rules_version())


def run_once(settings: Settings) -> dict[str, Any]:
    conn = db.connect(settings.db_path)
    try:
        now = wall_now()
        db.kv_set(conn, "social.heartbeat", str(now), now)
        if not settings.x_configured:
            return {"disabled": "X_BEARER_TOKEN not configured"}
        return build_client(conn, settings).tick(now)
    finally:
        conn.close()


def run_social(settings: Settings, once: bool = False) -> int:
    settings.ensure_dirs()
    if once:
        print(json.dumps(run_once(settings), indent=2, default=str))
        return 0
    conn = db.connect(settings.db_path)
    interval = float(settings.config("x").get("loop_interval_s", 15))
    stopping = {"v": False}

    def _stop(*_: Any) -> None:
        stopping["v"] = True

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    client = build_client(conn, settings) if settings.x_configured else None
    log.info("social.started", enabled=client is not None, interval_s=interval, x_daily_budget_usd=settings.x_daily_budget_usd,
             kols=len(settings.config("x_kols").get("accounts", [])))
    if client is None:
        log.warning("social.disabled", reason="X_BEARER_TOKEN not configured; idling with a heartbeat")
    last_log = 0.0
    while not stopping["v"]:
        t0 = time.monotonic()
        now = wall_now()
        try:
            db.kv_set(conn, "social.heartbeat", str(now), now)
            if client is not None:
                s = client.tick(now)
                if any(isinstance(v, dict) and any(v.values()) for v in s.values()) or s.get("fast_post_decisions") or time.monotonic() - last_log > 300:
                    last_log = time.monotonic()
                    log.info("social.tick", **{k: v for k, v in s.items()}, stats=client.stats)
        except Exception:
            log.error("social.tick_failed", exc_info=True)
        elapsed = time.monotonic() - t0
        for _ in range(int(max(0.0, interval - elapsed) * 10)):
            if stopping["v"]:
                break
            time.sleep(0.1)
    conn.close()
    log.info("social.stopped")
    return 0
