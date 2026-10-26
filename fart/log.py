"""Structured logging: JSON lines to a rotating file (5 MB x 5) plus plain stderr.

Usage: log = get_logger("ingest"); log.info("pp.connect", attempt=3)
Never pass secret values as fields.
"""
from __future__ import annotations

import json
import logging
import logging.handlers
import sys
import time
from pathlib import Path
from typing import Any

_CONFIGURED = False


class _JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        d: dict[str, Any] = {
            "ts": round(record.created, 3),
            "t": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(record.created)),
            "level": record.levelname,
            "logger": record.name,
            "event": record.getMessage(),
        }
        fields = getattr(record, "fields", None)
        if fields:
            d.update(fields)
        if record.exc_info:
            d["exc"] = self.formatException(record.exc_info)
        return json.dumps(d, default=str, ensure_ascii=False)


class _PlainFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        fields = getattr(record, "fields", None) or {}
        kv = " ".join(f"{k}={json.dumps(v, default=str, ensure_ascii=False)}" for k, v in fields.items())
        base = f"{time.strftime('%H:%M:%S', time.gmtime(record.created))} {record.levelname[:4]} {record.name} {record.getMessage()}"
        if kv:
            base += " " + kv
        if record.exc_info:
            base += "\n" + self.formatException(record.exc_info)
        return base


class Log:
    """Thin wrapper so call sites read `log.info("event", key=value)`."""

    def __init__(self, logger: logging.Logger):
        self._l = logger

    def _emit(self, level: int, event: str, exc_info: bool = False, **fields: Any) -> None:
        self._l.log(level, event, extra={"fields": fields}, exc_info=exc_info)

    def debug(self, event: str, **f: Any) -> None:
        self._emit(logging.DEBUG, event, **f)

    def info(self, event: str, **f: Any) -> None:
        self._emit(logging.INFO, event, **f)

    def warning(self, event: str, **f: Any) -> None:
        self._emit(logging.WARNING, event, **f)

    def error(self, event: str, exc_info: bool = False, **f: Any) -> None:
        self._emit(logging.ERROR, event, exc_info=exc_info, **f)


def setup_logging(logs_dir: Path | None, name: str, level: int = logging.INFO, stderr: bool = True) -> None:
    global _CONFIGURED
    root = logging.getLogger()
    if _CONFIGURED:
        return
    root.setLevel(level)
    for h in list(root.handlers):
        root.removeHandler(h)
    if logs_dir is not None:
        logs_dir.mkdir(parents=True, exist_ok=True)
        fh = logging.handlers.RotatingFileHandler(
            logs_dir / f"{name}.log", maxBytes=5 * 1024 * 1024, backupCount=5, encoding="utf-8"
        )
        fh.setFormatter(_JsonFormatter())
        root.addHandler(fh)
    if stderr:
        sh = logging.StreamHandler(sys.stderr)
        sh.setFormatter(_PlainFormatter())
        root.addHandler(sh)
    logging.getLogger("websockets").setLevel(logging.WARNING)
    _CONFIGURED = True


def get_logger(name: str) -> Log:
    return Log(logging.getLogger(name))


class Throttle:
    """Emit at most once per `period` seconds per key (e.g. 'once per hour' warnings)."""

    def __init__(self, period_s: float):
        self.period_s = period_s
        self._last: dict[str, float] = {}

    def allow(self, key: str, now: float | None = None) -> bool:
        t = time.time() if now is None else now
        last = self._last.get(key)
        if last is None or t - last >= self.period_s:
            self._last[key] = t
            return True
        return False
