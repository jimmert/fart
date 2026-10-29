"""rules_version: the git commit of the running code; rules_fingerprint: a hash of the files that
define the engine's rules (milestone 5).

Resolution order of rules_version: $FART_RULES_VERSION, `git rev-parse` in the repo root,
a VERSION file written by deploy/install.sh, else 'unknown'.

Every deploy is a new commit and therefore a new `rules_version`, even when the rules did not change
(milestone 5 itself: a bug fix in the watchlist, the week report and the community preparation, no
threshold touched). The week report therefore groups decisions by the fingerprint of the rule-defining
files (`RULE_FILES`): two rules_versions with the same fingerprint ran the same rules. The engine writes
its fingerprint next to its rules_version in kv (`engine.rules_fingerprint.<rules_version>`), and
`config/measure.json` -> `community_gate.frozen_rules` carries the fingerprint of the frozen measurement
week; a test pins the working tree to it, so changing a rule means changing that config entry on purpose.

Milestone 11: a change of a rule file that provably computes the same outcomes (the meta baseline once per tick instead
of once per term) gets a new fingerprint but not a new segment: `community_gate.frozen_rules_equivalents` lists such
fingerprints with the date, the PR and the test that proves byte-equal outcomes on a fixture database. The week report
pools them with the frozen fingerprint (`canonical_fingerprint`) and says so under the segment title; the website and
`fart status` call the running tree frozen when it is the frozen fingerprint or a listed equivalent (`runs_frozen_rules`).
"""
from __future__ import annotations

import hashlib
import os
import subprocess
from pathlib import Path

_CACHE: dict[str, str] = {}

REPO_ROOT = Path(__file__).resolve().parent.parent

# The files that decide what becomes an observation: thresholds, clustering, features, curve math.
# Orchestration (watchlist mechanics, the daemon loop, ingestion) is deliberately not part of it.
RULE_FILES = (
    "config/engine.json",
    "config/pumpfun.json",
    "config/stopwords.json",
    "fart/chain/curve.py",
    "fart/engine/decide.py",
    "fart/engine/features.py",
    "fart/engine/meta.py",
)


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


def rules_fingerprint(root: Path | None = None) -> str:
    """`sha256:<hex>` over the sorted RULE_FILES (path, newline, bytes, newline). A missing file is
    hashed as absent (so the value is still defined); the result is cached per root."""
    base = REPO_ROOT if root is None else Path(root)
    key = f"fp:{base}"
    if key in _CACHE:
        return _CACHE[key]
    h = hashlib.sha256()
    for rel in sorted(RULE_FILES):
        h.update(rel.encode("utf-8"))
        h.update(b"\n")
        p = base / rel
        h.update(p.read_bytes() if p.exists() else b"<absent>")
        h.update(b"\n")
    _CACHE[key] = "sha256:" + h.hexdigest()
    return _CACHE[key]


def frozen_fingerprint(gate_cfg: dict | None) -> str | None:
    """The fingerprint of `community_gate.frozen_rules`, or None when there is none."""
    fr = (gate_cfg or {}).get("frozen_rules") or {}
    return str(fr["rules_fingerprint"]) if fr.get("rules_fingerprint") else None


def frozen_rules_equivalents(gate_cfg: dict | None) -> dict[str, dict]:
    """fingerprint -> entry of `community_gate.frozen_rules_equivalents` (milestone 11): fingerprints of rule files that
    compute exactly what the frozen rules compute, each with the date, the PR number and the test that proves it."""
    out: dict[str, dict] = {}
    for e in (gate_cfg or {}).get("frozen_rules_equivalents") or []:
        if isinstance(e, dict) and e.get("rules_fingerprint"):
            out[str(e["rules_fingerprint"])] = e
    return out


def canonical_fingerprint(fp: str | None, gate_cfg: dict | None) -> str | None:
    """The frozen fingerprint for the frozen one and for every proven equivalent; any other fingerprint stays itself."""
    frozen = frozen_fingerprint(gate_cfg)
    if fp is not None and frozen is not None and fp in frozen_rules_equivalents(gate_cfg):
        return frozen
    return fp


def frozen_equivalent(fp: str | None, gate_cfg: dict | None) -> dict | None:
    """The equivalence entry of `fp`, or None when `fp` is the frozen fingerprint itself or no equivalent at all."""
    return frozen_rules_equivalents(gate_cfg).get(fp) if fp else None


def runs_frozen_rules(gate_cfg: dict | None, fp: str | None = None) -> bool:
    """Whether `fp` (default: the running tree) is the frozen fingerprint of the measurement week or a proven equivalent."""
    frozen = frozen_fingerprint(gate_cfg)
    if frozen is None:
        return False
    return canonical_fingerprint(rules_fingerprint() if fp is None else fp, gate_cfg) == frozen
