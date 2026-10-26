"""rules_version: the git commit of the running code.

Resolution order: $FART_RULES_VERSION, `git rev-parse` in the repo root,
a VERSION file written by deploy/install.sh, else 'unknown'.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

_CACHE: dict[str, str] = {}

REPO_ROOT = Path(__file__).resolve().parent.parent


def rules_version() -> str:
    if "v" in _CACHE:
        return _CACHE["v"]
    v = os.environ.get("FART_RULES_VERSION", "").strip()
    if not v and (REPO_ROOT / ".git").exists():
        try:
            out = subprocess.run(
                ["git", "-C", str(REPO_ROOT), "rev-parse", "--short=12", "HEAD"],
                capture_output=True, text=True, timeout=5, check=False,
            )
            if out.returncode == 0:
                v = out.stdout.strip()
        except (OSError, subprocess.SubprocessError):
            v = ""
    if not v:
        vf = REPO_ROOT / "VERSION"
        if vf.exists():
            v = vf.read_text(encoding="utf-8").strip()
    _CACHE["v"] = v or "unknown"
    return _CACHE["v"]
