"""`fart backup verify` (milestone 6): does the newest archive restore?

Takes the newest `data/backup/fart-*.db.gz`, unpacks it into a temporary directory under `data/`
(`data/backup/.verify-<pid>/`), runs `PRAGMA quick_check` on the copy, compares the row counts of
`coins`, `migrations`, `decisions` and `outcomes` with the live database (live >= archive: the archive is
older, and those tables only grow), removes the temporary directory and prints the result. Exit 0 when
everything holds, 1 on any failure (bad gzip, quick_check not ok, a live count below the archive's), 2
without an archive. `deploy/backup.sh` runs it after every backup, so a failed check fails the unit. The
result is also kept in kv `backup.verify.last` for `fart status` (best effort).
"""
from __future__ import annotations

import gzip
import json
import os
import shutil
import sqlite3
import time
from pathlib import Path
from typing import Any

from . import db
from .config import Settings
from .util import now as wall_now, utc_iso

TABLES = ("coins", "migrations", "decisions", "outcomes")


def newest_archive(backup_dir: Path) -> Path | None:
    files = sorted(backup_dir.glob("fart-*.db.gz"))  # the stamp sorts chronologically
    return files[-1] if files else None


def _tables(conn: sqlite3.Connection) -> set[str]:
    return {str(r[0]) for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def _count(conn: sqlite3.Connection, table: str) -> int:
    return int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])


def verify(settings: Settings, now: float | None = None, tables: tuple[str, ...] = TABLES) -> dict[str, Any]:
    t = wall_now() if now is None else now
    backup_dir = settings.data_dir / "backup"
    res: dict[str, Any] = {"ok": False, "archive": None, "quick_check": None, "counts": {}, "errors": [], "checked_at": utc_iso(t)}
    arch = newest_archive(backup_dir) if backup_dir.exists() else None
    if arch is None:
        res["errors"].append(f"no archive fart-*.db.gz under {backup_dir}")
        res["status"] = "no_archive"
        return res
    res["archive"] = str(arch)
    res["archive_bytes"] = arch.stat().st_size
    tmp = backup_dir / f".verify-{os.getpid()}"
    try:
        shutil.rmtree(tmp, ignore_errors=True)
        tmp.mkdir(parents=True, exist_ok=False)
        copy = tmp / "fart.db"
        try:
            with gzip.open(arch, "rb") as src, open(copy, "wb") as dst:
                shutil.copyfileobj(src, dst)
        except (OSError, EOFError, gzip.BadGzipFile) as e:
            res["errors"].append(f"cannot unpack {arch.name}: {type(e).__name__}: {e}")
            res["status"] = "bad_archive"
            return res
        res["unpacked_bytes"] = copy.stat().st_size
        try:
            ac = sqlite3.connect(f"file:{copy.as_posix()}?mode=ro", uri=True, timeout=30)
            ac.row_factory = sqlite3.Row
            try:
                res["quick_check"] = str(ac.execute("PRAGMA quick_check").fetchone()[0])
                res["archive_schema_version"] = int(ac.execute("PRAGMA user_version").fetchone()[0])
                arch_tables = _tables(ac)
                arch_counts = {tb: (_count(ac, tb) if tb in arch_tables else None) for tb in tables}
            finally:
                ac.close()
        except sqlite3.DatabaseError as e:
            res["errors"].append(f"archive is not a readable SQLite database: {e}")
            res["status"] = "bad_archive"
            return res
        if res["quick_check"] != "ok":
            res["errors"].append(f"quick_check on the archive: {res['quick_check']}")
        live_counts: dict[str, int | None] = {tb: None for tb in tables}
        if settings.db_path.exists():
            lc = db.connect_ro(settings.db_path)
            try:
                live_tables = _tables(lc)
                live_counts = {tb: (_count(lc, tb) if tb in live_tables else None) for tb in tables}
                res["live_schema_version"] = int(lc.execute("PRAGMA user_version").fetchone()[0])
            finally:
                lc.close()
        else:
            res["errors"].append(f"no live database at {settings.db_path}")
        for tb in tables:
            a, lv = arch_counts.get(tb), live_counts.get(tb)
            entry: dict[str, Any] = {"archive": a, "live": lv}
            if a is None and lv is None:
                entry["status"] = "absent"
            elif a is None:
                entry["status"] = "absent_in_archive"
                res["errors"].append(f"table {tb} is missing in the archive")
            elif lv is None:
                entry["status"] = "absent_live"
                res["errors"].append(f"table {tb} is missing in the live database")
            elif lv >= a:
                entry["status"] = "ok"
            else:
                entry["status"] = "live_below_archive"
                res["errors"].append(f"live {tb} has {lv} rows, the archive {a}: the live database lost rows or the archive is not ours")
            res["counts"][tb] = entry
        res["ok"] = not res["errors"]
        res["status"] = "ok" if res["ok"] else "failed"
        return res
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
        res["temp_removed"] = not tmp.exists()


def remember(settings: Settings, res: dict[str, Any]) -> None:
    """kv `backup.verify.last` for `fart status`; best effort, never raises."""
    if not settings.db_path.exists():
        return
    try:
        conn = db.connect(settings.db_path)
        try:
            payload = {k: v for k, v in res.items() if k in ("ok", "status", "archive", "quick_check", "counts", "errors", "checked_at", "archive_bytes")}
            db.write_tx(conn, lambda c: db.kv_set(c, "backup.verify.last", json.dumps(payload, default=str), time.time()), what="backup.verify", attempts=2)
        finally:
            conn.close()
    except Exception:  # a status line is not worth a failed verification
        pass


def render_text(res: dict[str, Any]) -> str:
    lines = [f"backup verify: {res.get('status', '?').upper()}" + (f"  archive={res['archive']}" if res.get("archive") else "")]
    if res.get("archive"):
        lines.append(f"  quick_check={res.get('quick_check')}  archive {res.get('archive_bytes', 0):,} bytes gz -> {res.get('unpacked_bytes', 0):,} bytes"
                     f"  schema archive {res.get('archive_schema_version')} / live {res.get('live_schema_version')}")
        for tb, c in res.get("counts", {}).items():
            lines.append(f"  {tb:<11} archive={c['archive']!s:<8} live={c['live']!s:<8} {c['status']}")
    for e in res.get("errors", []):
        lines.append(f"  ERROR: {e}")
    lines.append(f"  temporary copy removed: {'yes' if res.get('temp_removed', True) else 'NO'}")
    return "\n".join(lines)


def cmd_verify(settings: Settings, as_json: bool) -> int:
    res = verify(settings)
    remember(settings, res)
    if as_json:
        print(json.dumps(res, indent=2, default=str))
    else:
        print(render_text(res))
    return 0 if res["ok"] else (2 if res.get("status") == "no_archive" else 1)
