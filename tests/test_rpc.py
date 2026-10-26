"""RPC client through the budget gate, with the network stubbed at urllib level."""
import io
import json
import urllib.error

import pytest

from fart.chain.rpc import RpcClient, RpcError
from fart.net import http as nh
from fart.net.budget import Blocked, Budget, load_source_configs


class FakeResp(io.BytesIO):
    def __init__(self, body: bytes, status: int = 200):
        super().__init__(body)
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()


def make(settings, conn, responses, monkeypatch):
    calls = []

    def fake_urlopen(req, timeout=None):
        calls.append(json.loads(req.data.decode()))
        r = responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return FakeResp(json.dumps(r).encode())

    monkeypatch.setattr(nh.urllib.request, "urlopen", fake_urlopen)
    srcs = load_source_configs(settings.config("budget"), {"RPC_DAILY_BUDGET_CU": "100000"})
    budget = Budget(conn, srcs, settings.lock_path)
    return RpcClient(budget, "https://rpc.invalid/v2/KEY", is_public=False), budget, calls


def test_get_transaction_records_cost(settings, conn, monkeypatch, fixtures_dir):
    tx = json.loads((fixtures_dir / "tx_create.json").read_text())
    rpc, budget, calls = make(settings, conn, [tx], monkeypatch)
    res = rpc.get_transaction("sig")
    assert res["slot"] == 370_000_001
    assert calls[0]["method"] == "getTransaction"
    assert calls[0]["params"][1]["maxSupportedTransactionVersion"] == 0
    usage = budget.daily_usage("rpc")
    assert usage["requests"] == 1 and usage["cost"] == 40  # cost table: the published Alchemy figure (milestone 4)


def test_rpc_error_is_raised_and_recorded_ok(settings, conn, monkeypatch):
    rpc, budget, _ = make(settings, conn, [{"jsonrpc": "2.0", "id": 1, "error": {"code": -32602, "message": "bad params"}}], monkeypatch)
    with pytest.raises(RpcError):
        rpc.get_slot()
    # transport was fine: the ledger says ok; the JSON-RPC error is the caller's problem
    assert conn.execute("SELECT ok FROM api_ledger").fetchone()[0] == 1


def test_http_429_triggers_backoff_then_blocked(settings, conn, monkeypatch):
    err = urllib.error.HTTPError("u", 429, "Too Many", {"Retry-After": "30"}, io.BytesIO(b""))
    rpc, budget, _ = make(settings, conn, [err], monkeypatch)
    with pytest.raises(nh.HttpError) as ei:
        rpc.get_slot()
    assert ei.value.status == 429 and ei.value.retry_after_s == 30
    with pytest.raises(Blocked) as bi:
        rpc.get_slot()
    assert bi.value.reason == "backoff"
    assert not rpc.available("getSlot")


def test_public_fallback_uses_public_source(settings, conn, monkeypatch):
    srcs = load_source_configs(settings.config("budget"), {})
    budget = Budget(conn, srcs, settings.lock_path)
    rpc = RpcClient(budget, settings.rpc_http_url, is_public=settings.rpc_is_public)
    assert rpc.source == "rpc_public"
