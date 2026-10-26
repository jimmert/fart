"""Small helpers: clock, time keys, atomic file writes."""
from __future__ import annotations

import datetime as _dt
import json
import os
import tempfile
from pathlib import Path
from typing import Any


def now() -> float:
    """Our own wall clock (unix seconds). Never confuse with chain time."""
    import time

    return time.time()


def utc_iso(ts: float | None) -> str | None:
    if ts is None:
        return None
    return _dt.datetime.fromtimestamp(ts, tz=_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def day_key(ts: float) -> str:
    return _dt.datetime.fromtimestamp(ts, tz=_dt.timezone.utc).strftime("%Y-%m-%d")


def hour_key(ts: float) -> str:
    """UTC hour bucket key, e.g. '2026-10-26T13'."""
    return _dt.datetime.fromtimestamp(ts, tz=_dt.timezone.utc).strftime("%Y-%m-%dT%H")


def day_bounds(day: str) -> tuple[float, float]:
    """(start, end) unix seconds of a 'YYYY-MM-DD' UTC day."""
    d = _dt.datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=_dt.timezone.utc)
    start = d.timestamp()
    return start, start + 86400.0


def hour_bounds(hour: str) -> tuple[float, float]:
    d = _dt.datetime.strptime(hour, "%Y-%m-%dT%H").replace(tzinfo=_dt.timezone.utc)
    start = d.timestamp()
    return start, start + 3600.0


def day_start(ts: float) -> float:
    return day_bounds(day_key(ts))[0]


def atomic_write_bytes(path: Path | str, data: bytes) -> None:
    """Write via a temp file in the same directory, then os.replace."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def atomic_write_text(path: Path | str, text: str) -> None:
    atomic_write_bytes(path, text.encode("utf-8"))


def atomic_write_json(path: Path | str, obj: Any) -> None:
    atomic_write_text(path, json.dumps(obj, indent=2, sort_keys=True, ensure_ascii=False) + "\n")


def load_json(path: Path | str) -> Any:
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def fnum(x: Any) -> float | None:
    """Tolerant float conversion: None/garbage -> None (never 0)."""
    if x is None:
        return None
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    if v != v:  # NaN
        return None
    return v
