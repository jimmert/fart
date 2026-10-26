"""The notify daemon: build messages into the outbox, send what is due. `fart notify` (20 s loop),
`fart notify --once`, `fart notify test`, `fart notify outbox`.

Builders (only while enabled):
  observe   every 5 min: new observe / observe_meta decisions since the last batch, max 6 batches per hour
  fast_post immediately, one message per decision
  digest    once a day at 06:10 UTC from the recall report of the previous day (+ X spend, CU spend)
  health    stream silent > 5 min, CU/X budget at 80% and at the cap, PumpPortal 403/429; 1 h cooldown per kind
Sender: pending rows due, max 20 per minute, 429 retry_after honored, retry ladder then gave_up.
"""
from __future__ import annotations

import datetime as _dt
import json
import signal
import time
from pathlib import Path
from typing import Any

from .. import db
from ..config import Settings
from ..log import get_logger
from ..net.budget import Budget
from ..util import day_key, now as wall_now, utc_iso
from ..version import rules_version
from . import format as fmt
from . import outbox
from .telegram import SendResult, TelegramSender

log = get_logger("notify")

SILENT_SOURCES = ("pumpportal", "rpc_logs")


class Notifier:
    def __init__(self, conn, settings: Settings, budget: Budget, sender: TelegramSender | None, clock=wall_now):
        self.conn = conn
        self.settings = settings
        self.cfg = settings.config("notify")
        self.budget = budget
        self.sender = sender
        self.clock = clock
        self.chat = settings.telegram_owner_chat_id
        self.footer = self.cfg.get("footer", "Observatie, geen advies. Alles wordt achteraf gemeten.")
        self.enabled = settings.telegram_enabled and sender is not None
        self.stats: dict[str, int] = {}

    def _bump(self, k: str, n: int = 1) -> None:
        self.stats[k] = self.stats.get(k, 0) + n

    # ----- builders ----------------------------------------------------------------------------------------
    def _pointer(self, key: str, table_max: int) -> int | None:
        """Last processed decision id; on the first run start at the current maximum (no history dump)."""
        v = db.kv_get(self.conn, key)
        if v is None:
            db.kv_set(self.conn, key, str(table_max), self.clock())
            return None
        return int(v)

    def build_observe(self, now: float) -> int:
        ocfg = self.cfg.get("observe", {})
        last_batch = float(db.kv_get(self.conn, "notify.observe.last_batch_at", "0") or 0)
        if now - last_batch < float(ocfg.get("batch_interval_s", 300)):
            return 0
        max_id = int(self.conn.execute("SELECT COALESCE(MAX(id), 0) FROM decisions").fetchone()[0])
        last_id = self._pointer("notify.observe.last_id", max_id)
        if last_id is None:
            db.kv_set(self.conn, "notify.observe.last_batch_at", str(now), now)
            return 0
        rows = self.conn.execute(
            "SELECT * FROM decisions WHERE id > ? AND kind IN ('observe', 'observe_meta') AND verdict='observe' ORDER BY id", (last_id,)
        ).fetchall()
        if not rows:
            db.kv_set(self.conn, "notify.observe.last_batch_at", str(now), now)
            return 0
        n_hour = int(self.conn.execute("SELECT COUNT(*) FROM outbox WHERE kind='observe' AND created_at > ?", (now - 3600,)).fetchone()[0])
        if n_hour >= int(ocfg.get("max_batches_per_hour", 6)):
            self._bump("observe_deferred")
            return 0  # keep the pointer: these decisions ride the next batch
        from ..measure.outcomes import sol_usd_at

        decisions = [fmt.decision_to_dict(r) for r in rows]
        sol_usd, _ = sol_usd_at(self.conn, now)
        text = fmt.observe_batch(decisions, sol_usd, self.footer, int(ocfg.get("max_items_per_message", 8)))
        key = f"observe:{decisions[0]['id']}-{decisions[-1]['id']}"
        queued = outbox.enqueue(self.conn, "observe", key, self.chat, text, now)
        db.kv_set(self.conn, "notify.observe.last_id", str(decisions[-1]["id"]), now)
        db.kv_set(self.conn, "notify.observe.last_batch_at", str(now), now)
        return 1 if queued else 0

    def build_fast_posts(self, now: float) -> int:
        max_id = int(self.conn.execute("SELECT COALESCE(MAX(id), 0) FROM decisions").fetchone()[0])
        last_id = self._pointer("notify.fast_post.last_id", max_id)
        if last_id is None:
            return 0
        n = 0
        for r in self.conn.execute("SELECT * FROM decisions WHERE id > ? AND kind='fast_post' ORDER BY id", (last_id,)).fetchall():
            d = fmt.decision_to_dict(r)
            if outbox.enqueue(self.conn, "fast_post", f"fast_post:{d['id']}", self.chat, fmt.fast_post(d, self.footer), now):
                n += 1
            db.kv_set(self.conn, "notify.fast_post.last_id", str(d["id"]), now)
        return n

    def build_digest(self, now: float) -> int:
        hhmm = str(self.cfg.get("digest", {}).get("time_utc", "06:10"))
        hh, mm = (int(x) for x in hhmm.split(":"))
        t = _dt.datetime.fromtimestamp(now, tz=_dt.timezone.utc)
        if (t.hour, t.minute) < (hh, mm):
            return 0
        day = day_key(now - 86400)
        if db.kv_get(self.conn, "notify.digest.last_day") == day:
            return 0
        rep = self._report_for(day, now)
        start = now - 86400 - (now % 86400)  # UTC midnight of `day`
        x_spend = float(self.conn.execute("SELECT COALESCE(SUM(usd), 0) FROM x_spend WHERE day=?", (day,)).fetchone()[0])
        rpc_source = "rpc_public" if self.settings.rpc_is_public else "rpc"
        cu = float(self.conn.execute("SELECT COALESCE(SUM(cost), 0) FROM api_ledger WHERE source=? AND blocked_reason IS NULL AND ts >= ? AND ts < ?",
                                     (rpc_source, start, start + 86400)).fetchone()[0])
        cu_cap = None if self.settings.rpc_is_public else float(self.settings.rpc_daily_budget_cu)
        text = fmt.digest(day, rep, x_spend if self.settings.x_configured else None, self.settings.x_daily_budget_usd if self.settings.x_configured else None,
                          cu, cu_cap, self.footer)
        queued = outbox.enqueue(self.conn, "digest", f"digest:{day}", self.chat, text, now)
        db.kv_set(self.conn, "notify.digest.last_day", day, now)
        return 1 if queued else 0

    def _report_for(self, day: str, now: float) -> dict[str, Any]:
        path = Path(self.settings.out_dir) / "recall" / f"{day}.json"
        if path.exists():
            try:
                return json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                pass
        from ..measure.report import build_report

        return build_report(self.conn, self.settings, day, now)

    def build_health(self, now: float) -> int:
        hcfg = self.cfg.get("health", {})
        cooldown = float(hcfg.get("cooldown_s", 3600))
        silent_s = float(hcfg.get("stream_silent_s", 300))
        warn_frac = float(hcfg.get("budget_warn_fraction", 0.8))
        kv = {r["key"]: r["value"] for r in self.conn.execute("SELECT key, value FROM kv WHERE key LIKE 'ingest.%'")}
        alerts: list[tuple[str, dict[str, Any]]] = []
        hb = kv.get("ingest.heartbeat")
        ingest_alive = hb is not None and now - float(hb) < 120
        if ingest_alive:
            for src in SILENT_SOURCES:
                if kv.get(f"ingest.{src}.started") is None:
                    continue
                last = kv.get(f"ingest.{src}.last_msg_at")
                ref = float(last) if last is not None else float(kv.get(f"ingest.{src}.started") or now)
                if now - ref > silent_s:
                    alerts.append((f"stream_silent:{src}", {"source": src, "age": fmt.age(now - ref), "last": fmt.fmt_time(float(last)) if last else "nooit"}))
        for src in ("rpc_public" if self.settings.rpc_is_public else "rpc", "x"):
            try:
                u = self.budget.daily_usage(src, now)
            except KeyError:
                continue
            cap = u.get("daily_cost_cap")
            if not cap:
                continue
            frac = float(u["cost"]) / float(cap)
            unit = "CU" if src.startswith("rpc") else "USD"
            params = {"source": src, "pct": f"{100 * frac:.0f}%", "used": f"{u['cost']:.2f} {unit}" if unit == "USD" else f"{u['cost']:.0f} CU",
                      "cap": f"{cap:.2f} {unit}" if unit == "USD" else f"{cap:.0f} CU"}
            if frac >= 1.0 or u.get("blocked", 0) and frac >= 0.99:
                alerts.append((f"budget_cap:{src}", params))
            elif frac >= warn_frac:
                alerts.append((f"budget_80:{src}", params))
        at = kv.get("ingest.pumpportal.last_http_status_at")
        if at is not None and now - float(at) < 3600:
            alerts.append(("pumpportal_refused", {"status": kv.get("ingest.pumpportal.last_http_status"), "at": fmt.fmt_time(float(at))}))
        n = 0
        for kind, params in alerts:
            last = db.kv_get(self.conn, f"notify.health.{kind}.last")
            if last is not None and now - float(last) < cooldown:
                continue
            text = fmt.health(kind.split(":")[0], params, now, self.footer)
            if outbox.enqueue(self.conn, "health", f"health:{kind}:{int(now)}", self.chat, text, now):
                n += 1
            db.kv_set(self.conn, f"notify.health.{kind}.last", str(now), now)
        return n

    def build(self, now: float) -> dict[str, int]:
        out = {"observe": 0, "fast_post": 0, "digest": 0, "health": 0}
        for name, fn in (("observe", self.build_observe), ("fast_post", self.build_fast_posts), ("digest", self.build_digest), ("health", self.build_health)):
            self.conn.execute("BEGIN")
            try:
                out[name] = fn(now)
                self.conn.execute("COMMIT")
            except Exception:
                self.conn.execute("ROLLBACK")
                log.error("notify.build_failed", builder=name, exc_info=True)
        return out

    # ----- sender ---------------------------------------------------------------------------------------------
    def send_due(self, now: float) -> dict[str, int]:
        scfg = self.cfg.get("send", {})
        schedule = [float(x) for x in scfg.get("retry_schedule_s", [15, 60, 300, 900, 3600])]
        per_min = int(scfg.get("max_per_minute", 20))
        out = {"sent": 0, "retry": 0, "gave_up": 0, "blocked": 0}
        if self.sender is None:
            return out
        room = per_min - outbox.sent_in_last(self.conn, now)
        if room <= 0:
            return out
        for row in outbox.due(self.conn, now, limit=room):
            res: SendResult = self.sender.send(row["chat"], row["text"])
            if res.ok:
                outbox.mark_sent(self.conn, row["id"], res.message_id, now)
                out["sent"] += 1
                continue
            if res.blocked:
                outbox.mark_retry(self.conn, row["id"], now + max(5.0, res.retry_after_s or 5.0), res.error or "blocked", count_attempt=False)
                out["blocked"] += 1
                break
            attempts = int(row["attempts"]) + 1
            if res.permanent or attempts > len(schedule):
                outbox.mark_gave_up(self.conn, row["id"], res.error or "failed")
                out["gave_up"] += 1
                log.warning("notify.gave_up", outbox_id=row["id"], kind=row["kind"], status=res.status, error=res.error)
                continue
            wait = schedule[attempts - 1]
            if res.status == 429 and res.retry_after_s:
                wait = max(float(res.retry_after_s), 1.0)
            outbox.mark_retry(self.conn, row["id"], now + wait, res.error or "failed")
            out["retry"] += 1
            if res.status == 429:
                break  # the whole chat is throttled: stop this round
        return out

    def tick(self, now: float) -> dict[str, Any]:
        summary: dict[str, Any] = {"enabled": self.enabled}
        db.kv_set(self.conn, "notify.heartbeat", str(now), now)
        if not self.enabled:
            return summary
        summary["built"] = self.build(now)
        summary["send"] = self.send_due(now)
        summary["outbox"] = outbox.counts(self.conn)
        return summary


def build_notifier(conn, settings: Settings) -> Notifier:
    budget = Budget.from_settings(conn, settings)
    sender = TelegramSender(budget, settings.telegram_bot_token) if settings.telegram_enabled else None
    return Notifier(conn, settings, budget, sender)


def run_notify(settings: Settings, once: bool = False) -> int:
    settings.ensure_dirs()
    conn = db.connect(settings.db_path)
    if once:
        try:
            print(json.dumps(build_notifier(conn, settings).tick(wall_now()), indent=2, default=str))
        finally:
            conn.close()
        return 0
    interval = float(settings.config("notify").get("loop_interval_s", 20))
    stopping = {"v": False}

    def _stop(*_: Any) -> None:
        stopping["v"] = True

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    notifier = build_notifier(conn, settings)
    log.info("notify.started", enabled=notifier.enabled, interval_s=interval, telegram=settings.describe()["telegram"],
             notify_enabled_flag=settings.telegram_notify_enabled)
    if not notifier.enabled:
        log.warning("notify.disabled", reason="TELEGRAM_NOTIFY_ENABLED is not 1 or token/chat id missing; idling with a heartbeat")
    last_log = 0.0
    while not stopping["v"]:
        t0 = time.monotonic()
        try:
            s = notifier.tick(wall_now())
            if any(s.get("built", {}).values()) or any(s.get("send", {}).values()) or time.monotonic() - last_log > 300:
                last_log = time.monotonic()
                log.info("notify.tick", **s)
        except Exception:
            log.error("notify.tick_failed", exc_info=True)
        elapsed = time.monotonic() - t0
        for _ in range(int(max(0.0, interval - elapsed) * 10)):
            if stopping["v"]:
                break
            time.sleep(0.1)
    conn.close()
    log.info("notify.stopped")
    return 0


def cmd_test(settings: Settings) -> int:
    """Send one test message to the owner chat (only when enabled)."""
    if not settings.telegram_enabled:
        print("notify is disabled: set TELEGRAM_BOT_TOKEN, TELEGRAM_OWNER_CHAT_ID and TELEGRAM_NOTIFY_ENABLED=1 in .env")
        return 2
    settings.ensure_dirs()
    conn = db.connect(settings.db_path)
    try:
        notifier = build_notifier(conn, settings)
        now = wall_now()
        text = fmt.test_message(now, rules_version(), notifier.footer)
        row_id = outbox.enqueue(conn, "test", f"test:{int(now)}", settings.telegram_owner_chat_id, text, now)
        res = notifier.send_due(now)
        row = conn.execute("SELECT status, message_id, error FROM outbox WHERE id=?", (row_id,)).fetchone()
        print(f"test message outbox id {row_id}: status={row['status']} message_id={row['message_id']} error={row['error']}  ({res})")
        return 0 if row["status"] == "sent" else 1
    finally:
        conn.close()


def cmd_outbox(settings: Settings, as_json: bool, n: int = 20) -> int:
    if not settings.db_path.exists():
        print("no database yet")
        return 0
    conn = db.connect_ro(settings.db_path)
    try:
        rows = outbox.last_rows(conn, n)
        total = outbox.counts(conn)
    finally:
        conn.close()
    if as_json:
        print(json.dumps({"counts": total, "rows": rows}, indent=2, default=str))
        return 0
    print("outbox: " + (", ".join(f"{k}={v}" for k, v in sorted(total.items())) or "empty"))
    for r in rows:
        first = (r["text"] or "").splitlines()[0][:60] if r["text"] else ""
        print(f"  #{r['id']:<5} {r['kind']:<9} {r['status']:<8} attempts={r['attempts']} created={utc_iso(r['created_at'])}"
              f" sent={utc_iso(r['sent_at']) if r['sent_at'] else '-'} msg={r['message_id'] or '-'} {first!s}" + (f"  error={r['error']}" if r["error"] else ""))
    return 0
