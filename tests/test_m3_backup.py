"""Milestone 3, item 3: deploy/backup.sh compresses without gzip's fchown(2) and verifies the archive."""
from __future__ import annotations

import os
import shutil
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "deploy" / "backup.sh"


def test_backup_script_never_lets_gzip_own_the_output():
    code = "\n".join(line for line in SCRIPT.read_text().splitlines() if not line.lstrip().startswith("#"))
    assert "gzip -f" not in code and "chown" not in code and "chmod" not in code  # fchown(2) is what SIGSYS'd the unit
    assert "gzip -n -c" in code and "gzip -t" in code
    assert "find \"$DEST\" -name 'fart-*.db.gz'" in code  # the retention step


@pytest.mark.skipif(shutil.which("bash") is None or shutil.which("gzip") is None, reason="bash and gzip needed")
def test_backup_script_end_to_end(tmp_path):
    root = tmp_path / "fart"
    (root / "data").mkdir(parents=True)
    db_path = root / "data" / "fart.db"
    c = sqlite3.connect(str(db_path))
    c.executescript("CREATE TABLE coins(mint TEXT); CREATE TABLE decisions(id INTEGER); INSERT INTO coins VALUES('M1'); INSERT INTO coins VALUES('M2');")
    c.commit()
    c.close()
    dest = root / "data" / "backup"
    dest.mkdir()
    old = dest / "fart-20200101-000000.db.gz"
    old.write_bytes(b"old")
    t = time.time() - 40 * 86400
    os.utime(old, (t, t))
    env = dict(os.environ, FART_ROOT=str(root), PY=sys.executable, KEEP_DAYS="14")
    res = subprocess.run(["bash", str(SCRIPT)], env=env, capture_output=True, text=True, timeout=120)
    assert res.returncode == 0, res.stdout + res.stderr
    out = res.stdout
    assert "quick_check=ok coins=2 decisions=0" in out and "gzip -t ok" in out
    assert "backup: removed" in out and not old.exists()  # the retention step ran this time
    archives = sorted(dest.glob("fart-*.db.gz"))
    assert len(archives) == 1 and not list(dest.glob("fart-*.db")) and not list(dest.glob("*.tmp"))
    assert subprocess.run(["gzip", "-t", str(archives[0])], capture_output=True).returncode == 0
    assert "1 file(s) kept" in out
    # no database: a clean no-op
    shutil.rmtree(root / "data")
    (root / "data").mkdir()
    res = subprocess.run(["bash", str(SCRIPT)], env=env, capture_output=True, text=True, timeout=60)
    assert res.returncode == 0 and "nothing to do" in res.stdout
