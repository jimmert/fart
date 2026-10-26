"""The notify daemon: build messages into the outbox, send what is due. `fart notify` (20 s loop),
`fart notify --once`, `fart notify test`, `fart notify outbox`.

Builders (only while enabled):
  observe   every 5 min: new observe / observe_meta decisions since the last batch, max 6 batches per hour
  fast_post immediately, one message per decision
  digest    once a day at 06:10 UTC from the recall report of the previous day (+ X spend, CU spend)
  health    stream silent > 5 min, CU/X budget at 80% and at the cap, PumpPortal 403/429; 1 h cooldown per kind
  community (milestone 5, OFF by default) the community text per observe / observe_meta decision: to the
            community chat only with TELEGRAM_COMMUNITY_CHAT_ID set AND TELEGRAM_COMMUNITY_ENABLED=1; with
            TELEGRAM_COMMUNITY_PREVIEW=1 the same text goes to the OWNER as an extra message headed
            "VOORBEELD community-bericht" (kind community_preview), within the owner limits. Rules for both:
            one message per coin and per theme per 24 h, at most 6 per hour, nothing in the quiet hours;
            skipped decisions are counted, never sent late.
  community_weekly (milestone 5) on the configured weekday/time (Monday 06:45 UTC) the go/no-go section of
            the week report in plain words; community chat only when enabled, owner as preview when PREVIEW=1.
Sender: pending rows due, max 20 per minute, 429 retry_after honored, retry ladder then gave_up.
Every builder reads first and writes in one short IMMEDIATE transaction with retries (milestone 3);
the digest's report build (heavy, no network) happens before its write.

Chat guard (milestone 5): the only chats a row may be queued for or sent to are the owner chat and, while
the community channel is enabled, the community chat (`Notifier.allowed_chats`). `_enqueue()` refuses any
other chat and the sender marks a pending row for a chat that is not allowed `gave_up` without calling the
API, so switching the channel off also stops what was already queued.
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
from ..util import day_bounds, day_key, now as wall_now, utc_iso
from ..version import rules_version
from . import format as fmt
from . import outbox
from .telegram import SendResult, TelegramSender

log = get_logger("notify")

SILENT_SOURCES = ("pumpportal", "rpc_logs")
COMMUNITY_KINDS = ("community", "community_preview")
OWNER_HOURLY_KINDS = ("observe", "community_preview")  # both count towards observe.max_batches_per_hour (owner limits)


class Notifier:
    def __init__(self, conn, settings: Settings, budget: Budget, sender: TelegramSender | None, clock=wall_now):
        self.conn = conn
        self.settings = settings
        self.cfg = settings.config("notify")
        self.ccfg = self.cfg.get("community", {})
        self.budget = budget
        self.sender = sender
        self.clock = clock
        self.chat = settings.telegram_owner_chat_id
        self.community_chat = settings.telegram_community_chat_id
        self.footer = self.cfg.get("footer", "Observatie, geen advies. Alles wordt achteraf gemeten.")
        self.enabled = settings.telegram_enabled and sender is not None
        self.community_enabled = settings.community_enabled and sender is not None
        self.community_preview = settings.community_preview and sender is not None
        self.stats: dict[str, int] = {}

    def _bump(self, k: str, n: int = 1) -> None:
        self.stats[k] = self.stats.get(k, 0) + n

    # ----- the chat guard -----------------------------------------------------------------------------------
    @property
    def allowed_chats(self) -> set[str]:
        """The owner chat and, only while the community channel is enabled, the community chat."""
        chats = {str(self.chat)} if self.chat else set()
        if self.community_enabled and self.community_chat:
            chats.add(str(self.community_chat))
        return chats

    def _enqueue(self, c, kind: str, dedupe_key: str, chat: str, text: str, now: float) -> int | None:
        if str(chat) not in self.allowed_chats:
            self._bump("refused_chat")
            log.warning("notify.refused_chat", kind=kind, reason="not the owner chat and the community channel is off")
            return None
        return outbox.enqueue(c, kind, dedupe_key, str(chat), text, now)

    # ----- builders ----------------------------------------------------------------------------------------
    def _pointer(self, key: str, table_max: int, now: float) -> int | None:
        """Last processed decision id; on the first run start at the current maximum (no history dump)."""
        v = db.kv_get(self.conn, key)
        if v is None:
            db.write_tx(self.conn, lambda c: db.kv_set(c, key, str(table_max), now), what="notify.pointer")
            return None
        return int(v)

    def build_observe(self, now: float) -> int:
        ocfg = self.cfg.get("observe", {})
        last_batch = float(db.kv_get(self.conn, "notify.observe.last_batch_at", "0") or 0)
        if now - last_batch < float(ocfg.get("batch_interval_s", 300)):
            return 0
        max_id = int(self.conn.execute("SELECT COALESCE(MAX(id), 0) FROM decisions").fetchone()[0])
        last_id = self._pointer("notify.observe.last_id", max_id, now)
        if last_id is None:
            db.write_tx(self.conn, lambda c: db.kv_set(c, "notify.observe.last_batch_at", str(now), now), what="notify.observe")
            return 0
        rows = self.conn.execute(
            "SELECT * FROM decisions WHERE id > ? AND kind IN ('observe', 'observe_meta') AND verdict='observe' ORDER BY id", (last_id,)
        ).fetchall()
        if not rows:
            db.write_tx(self.conn, lambda c: db.kv_set(c, "notify.observe.last_batch_at", str(now), now), what="notify.observe")
            return 0
        marks = ",".join("?" for _ in OWNER_HOURLY_KINDS)
        n_hour = int(self.conn.execute(f"SELECT COUNT(*) FROM outbox WHERE kind IN ({marks}) AND created_at > ?", (*OWNER_HOURLY_KINDS, now - 3600)).fetchone()[0])
        if n_hour >= int(ocfg.get("max_batches_per_hour", 6)):
            self._bump("observe_deferred")
            return 0  # keep the pointer: these decisions ride the next batch
        from ..measure.outcomes import sol_usd_at

        decisions = [fmt.decision_to_dict(r) for r in rows]
        sol_usd, _ = sol_usd_at(self.conn, now)
        text = fmt.observe_batch(decisions, sol_usd, self.footer, int(ocfg.get("max_items_per_message", 8)))
        key = f"observe:{decisions[0]['id']}-{decisions[-1]['id']}"

        def _write(c) -> int:
            queued = self._enqueue(c, "observe", key, self.chat, text, now)
            db.kv_set(c, "notify.observe.last_id", str(decisions[-1]["id"]), now)
            db.kv_set(c, "notify.observe.last_batch_at", str(now), now)
            return 1 if queued else 0

        return db.write_tx(self.conn, _write, what="notify.observe")

    def build_fast_posts(self, now: float) -> int:
        max_id = int(self.conn.execute("SELECT COALESCE(MAX(id), 0) FROM decisions").fetchone()[0])
        last_id = self._pointer("notify.fast_post.last_id", max_id, now)
        if last_id is None:
            return 0
        rows = self.conn.execute("SELECT * FROM decisions WHERE id > ? AND kind='fast_post' ORDER BY id", (last_id,)).fetchall()
        if not rows:
            return 0
        items = [(d, fmt.fast_post(d, self.footer)) for d in (fmt.decision_to_dict(r) for r in rows)]

        def _write(c) -> int:
            n = 0
            for d, text in items:
                if self._enqueue(c, "fast_post", f"fast_post:{d['id']}", self.chat, text, now):
                    n += 1
            db.kv_set(c, "notify.fast_post.last_id", str(items[-1][0]["id"]), now)
            return n

        return db.write_tx(self.conn, _write, what="notify.fast_post")

    def build_digest(self, now: float) -> int:
        hhmm = str(self.cfg.get("digest", {}).get("time_utc", "06:10"))
        hh, mm = (int(x) for x in hhmm.split(":"))
        t = _dt.datetime.fromtimestamp(now, tz=_dt.timezone.utc)
        if (t.hour, t.minute) < (hh, mm):
            return 0
        day = day_key(now - 86400)
        if db.kv_get(self.conn, "notify.digest.last_day") == day:
            return 0
        rep = self._report_for(day, now)  # heavy (no network): outside every transaction
        start = now - 86400 - (now % 86400)  # UTC midnight of `day`
        x_spend = float(self.conn.execute("SELECT COALESCE(SUM(usd), 0) FROM x_spend WHERE day=?", (day,)).fetchone()[0])
        rpc_source = "rpc_public" if self.settings.rpc_is_public else "rpc"
        cu = float(self.conn.execute("SELECT COALESCE(SUM(cost), 0) FROM api_ledger WHERE source=? AND blocked_reason IS NULL AND ts >= ? AND ts < ?",
                                     (rpc_source, start, start + 86400)).fetchone()[0])
        cu_cap = None if self.settings.rpc_is_public else float(self.settings.rpc_daily_budget_cu)
        text = fmt.digest(day, rep, x_spend if self.settings.x_configured else None, self.settings.x_daily_budget_usd if self.settings.x_configured else None,
                          cu, cu_cap, self.footer)

        def _write(c) -> int:
            queued = self._enqueue(c, "digest", f"digest:{day}", self.chat, text, now)
            db.kv_set(c, "notify.digest.last_day", day, now)
            return 1 if queued else 0

        return db.write_tx(self.conn, _write, what="notify.digest")

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
        due = []
        for kind, params in alerts:
            last = db.kv_get(self.conn, f"notify.health.{kind}.last")
            if last is not None and now - float(last) < cooldown:
                continue
            due.append((kind, fmt.health(kind.split(":")[0], params, now, self.footer)))
        if not due:
            return 0

        def _write(c) -> int:
            n = 0
            for kind, text in due:
                if self._enqueue(c, "health", f"health:{kind}:{int(now)}", self.chat, text, now):
                    n += 1
                db.kv_set(c, f"notify.health.{kind}.last", str(now), now)
            return n

        return db.write_tx(self.conn, _write, what="notify.health")

    # ----- milestone 5: the community channel --------------------------------------------------------------
    def in_quiet_hours(self, now: float) -> bool:
        """Local quiet period (config/notify.json -> community.quiet_hours, HH:MM in `timezone`); may wrap
        midnight; start == end (or missing) disables it."""
        q = self.ccfg.get("quiet_hours") or {}
        start, end = str(q.get("start") or ""), str(q.get("end") or "")
        if not start or not end or start == end:
            return False
        tz = fmt._tz(str(self.cfg.get("timezone", "Europe/Amsterdam")))
        local = _dt.datetime.fromtimestamp(now, tz=tz)
        t = local.hour * 60 + local.minute
        sh, sm = (int(x) for x in start.split(":"))
        eh, em = (int(x) for x in end.split(":"))
        s, e = sh * 60 + sm, eh * 60 + em
        return (s <= t < e) if s < e else (t >= s or t < e)

    def _theme_for(self, d: dict[str, Any], now: float) -> str | None:
        """The most recent bursting theme (observe_meta decision in the meta window) whose term is in the
        coin's name or ticker; None when the coin belongs to no observed theme."""
        from ..engine.meta import load_stopwords, tokenize

        f = d.get("features") or {}
        ecfg = self.settings.config("engine")
        window = float(ecfg.get("meta", {}).get("window_s", 10800))
        terms = tokenize(f.get("name"), f.get("symbol"), load_stopwords(self.settings.config("stopwords")), int(ecfg.get("meta", {}).get("min_token_len", 4)))
        if not terms:
            return None
        for r in self.conn.execute("SELECT term FROM decisions WHERE kind='observe_meta' AND verdict='observe' AND as_of >= ? AND as_of <= ? ORDER BY as_of DESC",
                                   (now - window, now)):
            if r["term"] in terms:
                return str(r["term"])
        return None

    def _community_sent_in_last_hour(self, now: float) -> int:
        """Distinct decisions that got a community message (real or preview) in the last hour."""
        marks = ",".join("?" for _ in COMMUNITY_KINDS)
        return int(self.conn.execute(
            f"SELECT COUNT(DISTINCT substr(dedupe_key, instr(dedupe_key, ':') + 1)) FROM outbox WHERE kind IN ({marks}) AND created_at > ?",
            (*COMMUNITY_KINDS, now - 3600)).fetchone()[0])

    def build_community(self, now: float) -> int:
        """One community message per new observe / observe_meta decision, under the community rules, to the
        community chat (enabled) and/or as a preview to the owner (preview). Decisions the rules skip are
        counted and never sent later (an alert hours late is noise)."""
        if not (self.community_enabled or self.community_preview):
            return 0
        max_id = int(self.conn.execute("SELECT COALESCE(MAX(id), 0) FROM decisions").fetchone()[0])
        last_id = self._pointer("notify.community.last_id", max_id, now)
        if last_id is None:
            return 0
        rows = self.conn.execute(
            "SELECT * FROM decisions WHERE id > ? AND kind IN ('observe', 'observe_meta') AND verdict='observe' ORDER BY id", (last_id,)
        ).fetchall()
        if not rows:
            return 0
        from ..measure.outcomes import sol_usd_at

        quiet = self.in_quiet_hours(now)
        per_hour = int(self.ccfg.get("max_per_hour", 6))
        cooldown = float(self.ccfg.get("per_key_cooldown_s", 86400))
        used_hour = self._community_sent_in_last_hour(now)
        sol_usd, _ = sol_usd_at(self.conn, now)
        engine_cfg = self.settings.config("engine")
        items: list[tuple[dict[str, Any], str, str, str]] = []
        skipped = {"quiet": 0, "hour_cap": 0, "key_cooldown": 0}
        seen_keys: set[str] = set()
        for r in rows:
            d = fmt.decision_to_dict(r)
            key = f"coin:{d.get('mint')}" if d["kind"] == "observe" else f"theme:{d.get('term')}"
            if quiet:
                skipped["quiet"] += 1
                continue
            last = db.kv_get(self.conn, f"notify.community.key.{key}")
            if (last is not None and now - float(last) < cooldown) or key in seen_keys:
                skipped["key_cooldown"] += 1
                continue
            if used_hour + len(items) >= per_hour:
                skipped["hour_cap"] += 1
                continue
            theme = self._theme_for(d, now) if d["kind"] == "observe" else None
            rv = str(d.get("rules_version") or "")
            real = fmt.community_message(d, sol_usd, rv, engine_cfg, self.ccfg, self.footer, theme=theme, preview=False)
            prev = fmt.community_message(d, sol_usd, rv, engine_cfg, self.ccfg, self.footer, theme=theme, preview=True)
            items.append((d, key, real, prev))
            seen_keys.add(key)
        for k, v in skipped.items():
            if v:
                self._bump(f"community_skipped_{k}", v)

        def _write(c) -> int:
            n = 0
            for d, key, real, prev in items:
                if self.community_enabled and self._enqueue(c, "community", f"community:{d['id']}", self.community_chat, real, now):
                    n += 1
                if self.community_preview and self._enqueue(c, "community_preview", f"community_preview:{d['id']}", self.chat, prev, now):
                    n += 1
                db.kv_set(c, f"notify.community.key.{key}", str(now), now)
            db.kv_set(c, "notify.community.last_id", str(rows[-1]["id"]), now)
            return n

        return db.write_tx(self.conn, _write, what="notify.community")

    def _week_report_for(self, end_day: str, now: float) -> dict[str, Any]:
        path = Path(self.settings.out_dir) / "recall" / f"week-{end_day}.json"
        if path.exists():
            try:
                return json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                pass
        from ..measure.week import build_week_report

        days = int((self.settings.config("measure").get("week") or {}).get("days", 7))
        return build_week_report(self.conn, self.settings, end_day, days, now)

    def build_community_weekly(self, now: float) -> int:
        """The weekly accountability message (the go/no-go section of the week report in plain words) on the
        configured weekday and time; community chat only when enabled, owner as preview when PREVIEW=1."""
        if not (self.community_enabled or self.community_preview):
            return 0
        wcfg = self.ccfg.get("weekly") or {}
        weekday = int(wcfg.get("weekday", 1))
        hh, mm = (int(x) for x in str(wcfg.get("time_utc", "06:45")).split(":"))
        t = _dt.datetime.fromtimestamp(now, tz=_dt.timezone.utc)
        if t.isoweekday() != weekday or (t.hour, t.minute) < (hh, mm):
            return 0
        end_day = day_key(now - 86400)
        if db.kv_get(self.conn, "notify.community.weekly.last") == end_day:
            return 0
        rep = self._week_report_for(end_day, now)  # heavy (no network): outside every transaction
        week_start = day_bounds(str(rep.get("start_day") or end_day))[0]
        sent = int(self.conn.execute("SELECT COUNT(*) FROM outbox WHERE kind='community' AND status='sent' AND created_at >= ?", (week_start,)).fetchone()[0])
        real = fmt.community_weekly(rep, self.footer, self.ccfg, sent, self.community_enabled, preview=False)
        prev = fmt.community_weekly(rep, self.footer, self.ccfg, sent, self.community_enabled, preview=True)

        def _write(c) -> int:
            n = 0
            if self.community_enabled and self._enqueue(c, "community_weekly", f"community_weekly:{end_day}", self.community_chat, real, now):
                n += 1
            if self.community_preview and self._enqueue(c, "community_weekly_preview", f"community_weekly_preview:{end_day}", self.chat, prev, now):
                n += 1
            db.kv_set(c, "notify.community.weekly.last", end_day, now)
            return n

        return db.write_tx(self.conn, _write, what="notify.community_weekly")

    def build(self, now: float) -> dict[str, int]:
        out = {"observe": 0, "fast_post": 0, "digest": 0, "health": 0, "community": 0, "community_weekly": 0}
        for name, fn in (("observe", self.build_observe), ("fast_post", self.build_fast_posts), ("digest", self.build_digest), ("health", self.build_health),
                         ("community", self.build_community), ("community_weekly", self.build_community_weekly)):
            try:
                out[name] = fn(now)
            except Exception:
                log.error("notify.build_failed", builder=name, exc_info=True)
        return out

    # ----- sender ---------------------------------------------------------------------------------------------
    def send_due(self, now: float) -> dict[str, int]:
        scfg = self.cfg.get("send", {})
        schedule = [float(x) for x in scfg.get("retry_schedule_s", [15, 60, 300, 900, 3600])]
        per_min = int(scfg.get("max_per_minute", 20))
        out = {"sent": 0, "retry": 0, "gave_up": 0, "blocked": 0, "refused_chat": 0}
        if self.sender is None:
            return out
        room = per_min - outbox.sent_in_last(self.conn, now)
        if room <= 0:
            return out
        allowed = self.allowed_chats
        for row in outbox.due(self.conn, now, limit=room):
            if str(row["chat"]) not in allowed:
                # never call the API for a chat that is not allowed right now (the community channel is off, or an unknown chat)
                db.write_tx(self.conn, lambda c: outbox.mark_gave_up(c, row["id"], "chat not allowed: not the owner and the community channel is off"),
                            what="notify.refused_chat")
                out["refused_chat"] += 1
                self._bump("refused_chat")
                log.warning("notify.refused_chat", outbox_id=row["id"], kind=row["kind"])
                continue
            res: SendResult = self.sender.send(row["chat"], row["text"])  # network: outside every transaction
            if res.ok:
                db.write_tx(self.conn, lambda c: outbox.mark_sent(c, row["id"], res.message_id, now), what="notify.sent")
                out["sent"] += 1
                continue
            if res.blocked:
                db.write_tx(self.conn, lambda c: outbox.mark_retry(c, row["id"], now + max(5.0, res.retry_after_s or 5.0), res.error or "blocked", count_attempt=False),
                            what="notify.blocked")
                out["blocked"] += 1
                break
            attempts = int(row["attempts"]) + 1
            if res.permanent or attempts > len(schedule):
                db.write_tx(self.conn, lambda c: outbox.mark_gave_up(c, row["id"], res.error or "failed"), what="notify.gave_up")
                out["gave_up"] += 1
                log.warning("notify.gave_up", outbox_id=row["id"], kind=row["kind"], status=res.status, error=res.error)
                continue
            wait = schedule[attempts - 1]
            if res.status == 429 and res.retry_after_s:
                wait = max(float(res.retry_after_s), 1.0)
            db.write_tx(self.conn, lambda c: outbox.mark_retry(c, row["id"], now + wait, res.error or "failed"), what="notify.retry")
            out["retry"] += 1
            if res.status == 429:
                break  # the whole chat is throttled: stop this round
        return out

    def tick(self, now: float) -> dict[str, Any]:
        summary: dict[str, Any] = {"enabled": self.enabled}
        db.kv_set(self.conn, "notify.heartbeat", str(now), now)
        if not self.enabled:
            return summary
        summary["community"] = {"enabled": self.community_enabled, "preview": self.community_preview}
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
             notify_enabled_flag=settings.telegram_notify_enabled, community_enabled=notifier.community_enabled,
             community_preview=notifier.community_preview, allowed_chats=len(notifier.allowed_chats))
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
