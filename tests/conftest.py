"""Shared fixtures: an isolated FART_ROOT per test with the real config files."""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

FIXTURES = Path(__file__).resolve().parent / "fixtures"


@pytest.fixture
def settings(tmp_path, monkeypatch):
    from fart.config import load_settings

    monkeypatch.setenv("FART_ROOT", str(tmp_path))
    for k in ("RPC_HTTP_URL", "RPC_WSS_URL", "RPC_DAILY_BUDGET_CU", "PUMPPORTAL_WSS_URL"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("FART_RULES_VERSION", "test-rules-v1")
    s = load_settings(root=tmp_path)
    s.ensure_dirs()
    return s


@pytest.fixture
def conn(settings):
    from fart import db

    c = db.connect(settings.db_path)
    yield c
    c.close()


@pytest.fixture
def fixtures_dir() -> Path:
    return FIXTURES
