"""The budget of an engine tick (milestone 11): the duration of every step, the slow-tick counters, and their reading.

Measured on the VPS on 29 Oct 2026 (02:05 UTC): `tick_s` 590 to 598 s against a loop interval of 15 s, the engine at
100% CPU, the heartbeat up to 650 s old, decisions and the themes snapshot every ten minutes instead of every 15 s. The
tick summary carried only `features_s` and `tick_s`, so the slow step (the metas) was invisible. Now every tick logs the
duration of every step (`expire_s`, `features_s`, `promote_s`, `rpc_s`, `metas_s`, `decisions_s`, `tick_s`), the engine
writes the complete record of the previous tick in kv `engine.tick_last` inside its decision transaction (the previous
one: a tick's own duration is only known after its write committed; nothing is written outside the existing
transactions), counts every tick longer than the interval (`engine.tick_slow`, with a per-day twin for the report) and
warns at most once per hour (`engine.tick_slow` in the log). `fart status` and `/eigenaar` read the record and the
counters through `read()`. No work is skipped or shortened when a tick runs long: it becomes visible, not hidden.
"""
from __future__ import annotations

import json
import sqlite3
import time
from typing import Any, Callable

from .. import db
from ..log import Throttle
from ..notify.format import fmt_time, secs
from ..util import day_key, utc_iso

KV_LAST = "engine.tick_last"
KV_SLOW = "engine.tick_slow"
STEPS = ("expire", "features", "promote", "rpc", "metas", "decisions")
STEP_FIELDS = tuple(f"{s}_s" for s in STEPS)
RECORD_FIELDS = ("as_of", "tick_s", *STEP_FIELDS, "coins", "metas", "baseline_coins", "interval_s", "slow")


def record(summary: dict[str, Any], interval_s: float) -> dict[str, Any]:
    """The persisted shape of a finished tick's summary: the as_of, the durations, the sizes, whether it was slow."""
    out: dict[str, Any] = {k: summary.get(k) for k in RECORD_FIELDS if k in summary}
    out["interval_s"] = float(interval_s)
    out["slow"] = bool(float(summary.get("tick_s") or 0.0) > float(interval_s))
    return out


def write_previous(conn: sqlite3.Connection, previous: dict[str, Any] | None, now: float) -> None:
    """Inside the engine's decision transaction: the record of the previous tick and, when it was slow, the counters."""
    if not previous:
        return
    db.kv_set(conn, KV_LAST, json.dumps(previous, sort_keys=True, default=str), now)
    if previous.get("slow"):
        db.counter_bump_daily(conn, KV_SLOW, 1, now)


class SlowTickReporter:
    """The hourly warning of the daemon loop: every slow tick is counted, `note()` returns the fields of an
    `engine.tick_slow` warning at most once per `period_s` (the first slow tick at once, then the count since)."""

    def __init__(self, period_s: float = 3600.0, clock: Callable[[], float] = time.monotonic):
        self.throttle = Throttle(period_s)
        self.clock = clock
        self.unreported = 0
        self.total = 0

    def note(self, summary: dict[str, Any]) -> dict[str, Any] | None:
        if not summary.get("slow"):
            return None
        self.unreported += 1
        self.total += 1
        if not self.throttle.allow(KV_SLOW, self.clock()):
            return None
        fields = {"tick_s": summary.get("tick_s"), "interval_s": summary.get("interval_s", None), "slow_ticks_since_last_warning": self.unreported,
                  "slow_ticks_total": self.total, **{k: summary.get(k) for k in STEP_FIELDS}}
        self.unreported = 0
        return fields


def read(kv: dict[str, str], now: float, interval_s: float) -> dict[str, Any]:
    """What `fart status` and the website show: the last completed tick with its steps (raw seconds and labels) and the
    slow-tick counters (all time and today). `kv` is the `engine.*` slice of the kv table."""
    out: dict[str, Any] = {"interval_s": float(interval_s), "slow_total": _int(kv.get(KV_SLOW)), "slow_today": _int(kv.get(f"{KV_SLOW}.{day_key(now)}")),
                           "last": None, "note": "no completed tick recorded yet (the engine writes engine.tick_last from its second tick on; milestone 11)"}
    raw = kv.get(KV_LAST)
    if not raw:
        return out
    try:
        d = json.loads(raw)
        as_of = float(d["as_of"])
        tick_s = float(d["tick_s"])
    except (ValueError, TypeError, KeyError):
        out["note"] = "engine.tick_last is unreadable"
        return out
    interval = float(d["interval_s"]) if d.get("interval_s") is not None else float(interval_s)
    steps = {f: (None if d.get(f) is None else float(d[f])) for f in STEP_FIELDS}
    age = max(0.0, now - as_of)
    out["last"] = {
        "as_of": as_of, "as_of_iso": utc_iso(as_of), "as_of_label": fmt_time(as_of), "age_s": round(age),
        "tick_s": tick_s, "tick_label": secs(tick_s), "interval_s": interval,
        "slow": bool(d.get("slow", tick_s > interval)), "status": "slow" if d.get("slow", tick_s > interval) else "ok",
        "status_label": (f"langer dan het interval van {secs(interval)}" if d.get("slow", tick_s > interval) else f"binnen het interval van {secs(interval)}"),
        "steps": steps, "steps_label": " + ".join(f"{s} {secs(steps[f'{s}_s'])}" for s in STEPS),
        "coins": d.get("coins"), "metas": d.get("metas"), "baseline_coins": d.get("baseline_coins"),
    }
    out["note"] = None
    return out


def status_line(view: dict[str, Any]) -> str:
    """One line for `fart status`."""
    last = view.get("last")
    slow = f"slow ticks (> {secs(view.get('interval_s'))}): total {view.get('slow_total', 0)}, today {view.get('slow_today', 0)}"
    if not last:
        return f"engine tick: {view.get('note')}; {slow}"
    sizes = f"coins {last.get('coins')}, metas {last.get('metas')}, baseline coins {last.get('baseline_coins')}"
    return (f"engine tick (last completed, as_of {last['as_of_iso']}, {last['age_s']} s ago): {last['tick_label']} = {last['steps_label']}"
            f" [{sizes}; {'SLOW: ' if last['slow'] else ''}{last['status_label']}]; {slow}")


def _int(v: Any) -> int:
    try:
        return int(v or 0)
    except (TypeError, ValueError):
        return 0
