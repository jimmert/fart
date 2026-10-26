"""Solana JSON-RPC client (HTTP) through the budget gate. Read-only methods only."""
from __future__ import annotations

from typing import Any

from ..log import get_logger
from ..net.budget import Blocked, Budget
from ..net.http import HttpError, request_json

log = get_logger("rpc")


class RpcError(Exception):
    def __init__(self, code: int | None, message: str):
        super().__init__(f"rpc error {code}: {message}")
        self.code = code


class RpcClient:
    def __init__(self, budget: Budget, http_url: str, is_public: bool, timeout_s: float = 10.0):
        self.budget = budget
        self.url = http_url
        self.source = "rpc_public" if is_public else "rpc"
        self.timeout_s = timeout_s
        self._id = 0

    def available(self, method: str = "*") -> bool:
        return self.budget.peek(self.source, method).allowed

    def call(self, method: str, params: list[Any] | None = None) -> Any:
        """Raises Blocked (budget), HttpError (transport) or RpcError (JSON-RPC error)."""
        self._id += 1
        body = {"jsonrpc": "2.0", "id": self._id, "method": method, "params": params or []}
        resp = request_json(self.budget, self.source, method, self.url, body=body, timeout_s=self.timeout_s)
        if not isinstance(resp, dict):
            raise RpcError(None, "non-object response")
        if "error" in resp and resp["error"]:
            e = resp["error"]
            raise RpcError(e.get("code") if isinstance(e, dict) else None, str(e.get("message") if isinstance(e, dict) else e))
        return resp.get("result")

    # ----- read-only helpers ------------------------------------------------------------
    def get_transaction(self, signature: str) -> dict[str, Any] | None:
        return self.call(
            "getTransaction",
            [signature, {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0, "commitment": "confirmed"}],
        )

    def get_signatures_for_address(
        self, address: str, limit: int = 1000, before: str | None = None, until: str | None = None
    ) -> list[dict[str, Any]]:
        opts: dict[str, Any] = {"limit": max(1, min(1000, int(limit))), "commitment": "confirmed"}
        if before:
            opts["before"] = before
        if until:
            opts["until"] = until
        res = self.call("getSignaturesForAddress", [address, opts])
        return res if isinstance(res, list) else []

    def get_token_largest_accounts(self, mint: str) -> list[dict[str, Any]]:
        res = self.call("getTokenLargestAccounts", [mint, {"commitment": "confirmed"}])
        if isinstance(res, dict) and isinstance(res.get("value"), list):
            return res["value"]
        return []

    def get_account_info_base64(self, address: str) -> dict[str, Any] | None:
        res = self.call("getAccountInfo", [address, {"encoding": "base64", "commitment": "confirmed"}])
        if isinstance(res, dict):
            return res.get("value")
        return None

    def get_slot(self) -> int | None:
        res = self.call("getSlot", [{"commitment": "confirmed"}])
        return int(res) if isinstance(res, int) else None


__all__ = ["RpcClient", "RpcError", "Blocked", "HttpError"]
