"""The social daemon: `fart social` (15 s loop) and `fart social --once`.

Without X_BEARER_TOKEN (or with X_MODE=off) it idles with a heartbeat and says so once. Every tick runs
the due jobs (mega 60 s, refresh 120 s, KOL 180 s, confirmation 15 min per key) and the fast-post matching.
Milestone 6: with a token the default mode is `shadow` (`Settings.x_mode`): the same calls and money, but
fast posts go to `shadow_decisions` and confirmation counts to `shadow_confirmations`; `live` needs
`X_MODE=live` and the `x_live` rules key (fart/social/shadow.py). The mode runs in kv `social.x_mode`.
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
    return XClient(conn, budget, xb, xcfg, kols, settings.x_bearer_token, load_stopwords(settings.config("stopwords")), rules_version(),
                   shadow=settings.x_shadow)


def disabled_reason(settings: Settings) -> str:
    if not settings.x_configured:
        return "X_BEARER_TOKEN not configured"
    return "X_MODE=off"


def run_once(settings: Settings) -> dict[str, Any]:
    conn = db.connect(settings.db_path)
    try:
        now = wall_now()
        db.kv_set(conn, "social.heartbeat", str(now), now)
        db.kv_set(conn, "social.x_mode", settings.x_mode, now)
        if not settings.x_active:
            return {"disabled": disabled_reason(settings), "x_mode": settings.x_mode}
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
    client = build_client(conn, settings) if settings.x_active else None
    log.info("social.started", enabled=client is not None, x_mode=settings.x_mode, x_mode_requested=settings.x_mode_requested or "unset",
             interval_s=interval, x_daily_budget_usd=settings.x_daily_budget_usd, kols=len(settings.config("x_kols").get("accounts", [])))
    if settings.x_mode_note:
        log.warning("social.x_mode_note", note=settings.x_mode_note)
    if client is None:
        log.warning("social.disabled", reason=f"{disabled_reason(settings)}; idling with a heartbeat")
    elif client.shadow:
        log.info("social.shadow", note="X runs in shadow mode: fast posts -> shadow_decisions, counts -> shadow_confirmations; decisions untouched")
    db.kv_set(conn, "social.x_mode", settings.x_mode, wall_now())
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
