"""config/plays.json as the plays layer reads it: the active rules with their entry parameters, exit rules and figures.

A rule is written by `fart research promote` and never edited in place (a change is a new version); the layer evaluates
the pre-registered predicate of the rule's hypothesis (fart/research/hypotheses.py) with the entry parameters stored in
the rule, and exits with the `exit_rules` stored in the rule, so a later change of the code's grid never changes a
running play. The file is not a rule file of the engine.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ..measure.paper import ExitRules
from ..research import hypotheses as hyps
from ..version import REPO_ROOT


def plays_path(settings: Any) -> Path:
    p = Path(settings.root) / "config" / "plays.json"
    return p if p.exists() else REPO_ROOT / "config" / "plays.json"


def load_config(settings: Any) -> dict[str, Any]:
    cfg = settings.configs.get("plays") if getattr(settings, "configs", None) else None
    if not isinstance(cfg, dict):
        try:
            cfg = json.loads(plays_path(settings).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            cfg = {}
    return cfg if isinstance(cfg, dict) else {}


class Rule:
    def __init__(self, d: dict[str, Any]):
        self.id = str(d.get("id") or f"{d.get('hypothesis')}-v{d.get('version')}")
        self.name = str(d.get("name") or d.get("hypothesis") or self.id)
        self.hypothesis = str(d.get("hypothesis") or "").upper()
        self.variant_id = str(d.get("variant") or "")
        self.version = int(d.get("version") or 1)
        self.label = str(d.get("label") or "")
        self.entry = dict(d.get("entry") or {})
        self.exit_key = str(d.get("exit") or "")
        self.exit_rules = ExitRules.from_dict(d.get("exit_rules")) if d.get("exit_rules") else hyps.EXITS.get(self.exit_key, ExitRules())
        self.exit_text = str(d.get("exit_text") or self.exit_rules.describe_nl())
        self.figures = dict(d.get("figures") or {})
        self.report = d.get("report")
        self.promoted_at = d.get("promoted_at")
        self.active = bool(d.get("active", True))
        self.raw = d

    @property
    def hyp(self) -> hyps.Hypothesis:
        return hyps.get(self.hypothesis)

    @property
    def variant(self) -> hyps.Variant:
        return hyps.Variant(self.variant_id, self.label, self.entry, self.exit_key or "tr30", group=self.variant_id)

    def fires(self, ctx: hyps.Ctx) -> bool:
        return bool(self.hyp.fires(self.variant, ctx))

    def to_public(self) -> dict[str, Any]:
        f = self.figures
        return {"id": self.id, "name": self.name, "hypothesis": self.hypothesis, "variant": self.variant_id, "version": self.version, "label": self.label,
                "entry": self.entry, "exit_text": self.exit_text, "report": self.report, "promoted_at": self.promoted_at, "active": self.active,
                "hit_rate": f.get("hit_rate"), "ev_sol": f.get("ev_sol"), "ev_pct": f.get("ev_pct"), "ci_low_sol": f.get("ci_low_sol"), "ci_high_sol": f.get("ci_high_sol"),
                "cases": f.get("cases"), "unknown_share": f.get("unknown_share"), "lead_median_s": f.get("lead_median_s"), "size_sol": f.get("size_sol")}


def load_rules(settings: Any, include_inactive: bool = False) -> list[Rule]:
    cfg = load_config(settings)
    rules = []
    for d in cfg.get("rules") or []:
        if not isinstance(d, dict):
            continue
        r = Rule(d)
        if not r.hypothesis or r.hypothesis not in hyps.HYPOTHESES:
            continue
        if r.active or include_inactive:
            rules.append(r)
    return rules


def settings_of(settings: Any) -> dict[str, Any]:
    cfg = load_config(settings)
    return {"size_sol": float(cfg.get("size_sol", 0.5)), "entry_delay_s": float(cfg.get("entry_delay_s", 20)),
            "alerts_per_hour": int((cfg.get("alerts") or {}).get("max_per_hour", 6)), "loop_interval_s": float(cfg.get("loop_interval_s", 15)),
            "version": cfg.get("version")}
