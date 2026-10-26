"""Hourly gzip JSONL raw store: data/raw/<source>/YYYYMMDD/HH.jsonl.gz.

Every message is written raw (with our received_at) before normalization. Each flush appends
one gzip member, so a crash mid-flush loses at most the unflushed buffer and never corrupts
earlier members. Readers must tolerate a truncated final member.
"""
from __future__ import annotations

import gzip
import json
import time
from pathlib import Path
from typing import Any, Iterator


class RawWriter:
    def __init__(self, raw_dir: Path, source: str, flush_interval_s: float = 5.0, flush_lines: int = 200):
        self.dir = Path(raw_dir) / source
        self.source = source
        self.flush_interval_s = flush_interval_s
        self.flush_lines = flush_lines
        self._buf: list[tuple[str, bytes]] = []
        self._last_flush = time.monotonic()
        self.lines_written = 0
        self.bytes_written = 0

    def path_for(self, ts: float) -> Path:
        t = time.gmtime(ts)
        return self.dir / time.strftime("%Y%m%d", t) / (time.strftime("%H", t) + ".jsonl.gz")

    def write(self, obj: Any, received_at: float, raw_text: str | None = None) -> str:
        """Buffer one record. Returns the raw_ref 'relative/path.jsonl.gz' for the record."""
        rec = {"received_at": round(received_at, 3), "msg": obj}
        if raw_text is not None and not isinstance(obj, (dict, list)):
            rec["raw"] = raw_text
        line = (json.dumps(rec, separators=(",", ":"), ensure_ascii=False) + "\n").encode("utf-8")
        p = self.path_for(received_at)
        self._buf.append((str(p), line))
        if len(self._buf) >= self.flush_lines:
            self.flush()
        return str(p.relative_to(self.dir.parent))

    def maybe_flush(self) -> None:
        if self._buf and time.monotonic() - self._last_flush >= self.flush_interval_s:
            self.flush()

    def flush(self) -> None:
        if not self._buf:
            self._last_flush = time.monotonic()
            return
        by_path: dict[str, list[bytes]] = {}
        for p, line in self._buf:
            by_path.setdefault(p, []).append(line)
        self._buf = []
        for p, lines in by_path.items():
            path = Path(p)
            path.parent.mkdir(parents=True, exist_ok=True)
            data = b"".join(lines)
            with gzip.open(path, "ab", compresslevel=6) as fh:
                fh.write(data)
            self.lines_written += len(lines)
            self.bytes_written += len(data)
        self._last_flush = time.monotonic()

    def close(self) -> None:
        self.flush()


def iter_raw(path: Path) -> Iterator[dict[str, Any]]:
    """Read a raw file, tolerating a truncated final gzip member."""
    try:
        with gzip.open(path, "rb") as fh:
            for line in fh:
                try:
                    yield json.loads(line)
                except json.JSONDecodeError:
                    continue
    except (EOFError, OSError, gzip.BadGzipFile):
        return


def prune_raw(raw_dir: Path, keep_days: int, now_ts: float | None = None) -> int:
    """Delete day directories older than keep_days. Returns the number of files removed."""
    now_ts = time.time() if now_ts is None else now_ts
    cutoff = time.strftime("%Y%m%d", time.gmtime(now_ts - keep_days * 86400))
    removed = 0
    if not raw_dir.exists():
        return 0
    for source_dir in raw_dir.iterdir():
        if not source_dir.is_dir():
            continue
        for day_dir in source_dir.iterdir():
            if day_dir.is_dir() and len(day_dir.name) == 8 and day_dir.name.isdigit() and day_dir.name < cutoff:
                for f in day_dir.iterdir():
                    f.unlink()
                    removed += 1
                day_dir.rmdir()
    return removed
