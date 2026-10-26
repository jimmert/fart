"""Milestone 6, point 4: `fart backup verify` and the hook in deploy/backup.sh (a failed check fails the unit)."""
from __future__ import annotations

import gzip
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import pytest

from fart import backup as bk
from fart import db
from fart.cli import main
from fart.config import load_settings

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "deploy" / "backup.sh"


def _settings(tmp_path, monkeypatch):
    monkeypatch.setenv("FART_RULES_VERSION", "test-rules-v1")
    for k in ("RPC_HTTP_URL", "RPC_WSS_URL", "X_BEARER_TOKEN"):
        monkeypatch.delenv(k, raising=False)
    s = load_settings(root=tmp_path)
    s.ensure_dirs()
    return s


def _populate(path):
    c = db.connect(path)
    c.execute("INSERT INTO coins(mint, received_at, source) VALUES('M1', 1, 'pumpportal'), ('M2', 2, 'pumpportal')")
    c.execute("INSERT INTO migrations(mint, received_at, source) VALUES('M1', 3, 'rpc_logs')")
    c.execute("INSERT INTO decisions(as_of, created_at, kind, mint, features_json, rules_version, verdict, reasons_json) VALUES(1, 1, 'observe', 'M1', '{}', 'r', 'observe', '[]')")
    c.close()


def _archive(settings, stamp="20261026-053000"):
    """A real archive: the database copied through the backup API, then gzipped."""
    dest = settings.data_dir / "backup"
    dest.mkdir(exist_ok=True)
    src = sqlite3.connect(f"file:{settings.db_path.as_posix()}?mode=ro", uri=True)
    plain = dest / f"fart-{stamp}.db"
    dst = sqlite3.connect(str(plain))
    with dst:
        src.backup(dst)
    src.close()
    dst.close()
    with open(plain, "rb") as fh, gzip.open(dest / f"fart-{stamp}.db.gz", "wb") as gz:
        shutil.copyfileobj(fh, gz)
    plain.unlink()
    return dest / f"fart-{stamp}.db.gz"


def test_verify_ok_counts_cleanup_and_status(tmp_path, monkeypatch, capsys):
    s = _settings(tmp_path, monkeypatch)
    _populate(s.db_path)
    arch = _archive(s)
    res = bk.verify(s, now=1_793_000_000.0)
    assert res["ok"] and res["status"] == "ok" and res["archive"] == str(arch) and res["quick_check"] == "ok" and res["temp_removed"]
    assert res["counts"] == {"coins": {"archive": 2, "live": 2, "status": "ok"}, "migrations": {"archive": 1, "live": 1, "status": "ok"},
                             "decisions": {"archive": 1, "live": 1, "status": "ok"}, "outcomes": {"archive": 0, "live": 0, "status": "ok"}}
    assert res["archive_schema_version"] == res["live_schema_version"] == db.SCHEMA_VERSION and not list((s.data_dir / "backup").glob(".verify-*"))
    # the live database may be ahead of the archive (it only grows)
    c = db.connect(s.db_path)
    c.execute("INSERT INTO coins(mint, received_at, source) VALUES('M3', 4, 'pumpportal')")
    c.close()
    assert main(["--root", str(tmp_path), "--quiet", "backup", "verify"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("backup verify: OK") and "coins       archive=2        live=3        ok" in out and "temporary copy removed: yes" in out
    c = db.connect(s.db_path)
    last = json.loads(db.kv_get(c, "backup.verify.last"))
    c.close()
    assert last["ok"] and last["counts"]["coins"]["live"] == 3
    assert main(["--root", str(tmp_path), "--quiet", "status"]) == 0
    assert "backup: last verify ok at" in capsys.readouterr().out
    assert main(["--root", str(tmp_path), "--quiet", "--json", "backup", "verify"]) == 0
    assert json.loads(capsys.readouterr().out)["ok"] is True


def test_verify_fails_on_lost_rows_bad_archive_and_reports_no_archive(tmp_path, monkeypatch, capsys):
    s = _settings(tmp_path, monkeypatch)
    assert main(["--root", str(tmp_path), "--quiet", "backup", "verify"]) == 2
    assert "NO_ARCHIVE" in capsys.readouterr().out
    _populate(s.db_path)
    _archive(s)
    c = db.connect(s.db_path)
    c.execute("DELETE FROM coins WHERE mint='M2'")  # the live database lost a row the archive still has
    c.close()
    assert main(["--root", str(tmp_path), "--quiet", "backup", "verify"]) == 1
    out = capsys.readouterr().out
    assert "FAILED" in out and "live_below_archive" in out and "live coins has 1 rows, the archive 2" in out
    # a newer archive that is not a gzip file, then one that is a gzip of garbage: both fail, nothing is left behind
    bad = s.data_dir / "backup" / "fart-20261027-053000.db.gz"
    bad.write_bytes(b"not a gzip file")
    res = bk.verify(s)
    assert not res["ok"] and res["status"] == "bad_archive" and "cannot unpack" in res["errors"][0] and res["temp_removed"]
    with gzip.open(bad, "wb") as gz:
        gz.write(b"garbage" * 100)
    res = bk.verify(s)
    assert not res["ok"] and res["status"] == "bad_archive" and "not a readable SQLite database" in res["errors"][0]
    assert not list((s.data_dir / "backup").glob(".verify-*"))
    assert main(["--root", str(tmp_path), "--quiet", "backup", "verify"]) == 1
    capsys.readouterr()


@pytest.mark.skipif(shutil.which("bash") is None or shutil.which("gzip") is None, reason="bash and gzip needed")
def test_backup_script_verifies_and_fails_the_unit_on_a_bad_archive(tmp_path, monkeypatch):
    s = _settings(tmp_path, monkeypatch)
    _populate(s.db_path)
    env = dict(os.environ, FART_ROOT=str(tmp_path), PY=sys.executable, KEEP_DAYS="14", FART_RULES_VERSION="t")
    res = subprocess.run(["bash", str(SCRIPT)], env=env, capture_output=True, text=True, timeout=120)
    assert res.returncode == 0, res.stdout + res.stderr
    assert "quick_check=ok coins=2 decisions=1" in res.stdout and "backup verify: OK" in res.stdout and "backup: verify ok" in res.stdout
    assert len(list((s.data_dir / "backup").glob("fart-*.db.gz"))) == 1 and not list((s.data_dir / "backup").glob(".verify-*"))
    # a corrupt archive with the newest stamp makes the script (and so the unit) fail after the backup itself succeeded
    (s.data_dir / "backup" / "fart-29991231-235959.db.gz").write_bytes(b"corrupt")
    time.sleep(1.1)  # the archive stamp has one-second resolution
    res = subprocess.run(["bash", str(SCRIPT)], env=env, capture_output=True, text=True, timeout=120)
    assert res.returncode == 1 and "VERIFY FAILED" in res.stderr and "cannot unpack fart-29991231-235959.db.gz" in res.stdout
    assert len(list((s.data_dir / "backup").glob("fart-*.db.gz"))) == 3  # the new backup was still written before the check
