"""Minimal JSON-over-HTTPS client (urllib) that goes through the budget gate."""
from __future__ import annotations

import json
import socket
import time
import urllib.error
import urllib.request
from typing import Any

from ..log import get_logger
from .budget import Blocked, Budget

log = get_logger("http")

USER_AGENT = "fart/0.1 (+read-only public data consumer)"


class HttpError(Exception):
    def __init__(self, status: int | None, message: str, retry_after_s: float | None = None):
        super().__init__(f"HTTP {status}: {message}")
        self.status = status
        self.retry_after_s = retry_after_s


def _parse_retry_after(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return float(value)
    except ValueError:
        return None  # HTTP-date form is rare on these APIs; backoff still applies


def request_json(
    budget: Budget,
    source: str,
    method: str,
    url: str,
    body: Any | None = None,
    headers: dict[str, str] | None = None,
    timeout_s: float = 10.0,
    cost: float | None = None,
) -> Any:
    """Acquire -> call -> record. Raises Blocked when the gate says no, HttpError on failure.

    `method` is the budget method name (e.g. 'getTransaction', 'tokens'), not the HTTP verb.
    The URL is never logged (RPC URLs carry the API key).
    """
    verdict = budget.acquire_or_raise(source, method, cost)
    data = None
    hdrs = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    if headers:
        hdrs.update(headers)
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        hdrs["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=hdrs, method="POST" if body is not None else "GET")
    t0 = time.monotonic()
    status: int | None = None
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            status = resp.status
            raw = resp.read()
        latency = (time.monotonic() - t0) * 1000.0
        try:
            parsed = json.loads(raw.decode("utf-8")) if raw else None
        except (UnicodeDecodeError, json.JSONDecodeError) as e:
            budget.record(source, method, verdict.cost, False, status, latency, err=f"bad json: {e}")
            raise HttpError(status, f"invalid JSON body: {e}") from None
        budget.record(source, method, verdict.cost, True, status, latency)
        return parsed
    except urllib.error.HTTPError as e:
        latency = (time.monotonic() - t0) * 1000.0
        retry_after = _parse_retry_after(e.headers.get("Retry-After") if e.headers else None)
        budget.record(source, method, verdict.cost, False, e.code, latency, err=str(e.reason), retry_after_s=retry_after)
        raise HttpError(e.code, str(e.reason), retry_after) from None
    except (urllib.error.URLError, socket.timeout, TimeoutError, ConnectionError, OSError) as e:
        latency = (time.monotonic() - t0) * 1000.0
        budget.record(source, method, verdict.cost, False, None, latency, err=f"{type(e).__name__}: {e}")
        raise HttpError(None, f"{type(e).__name__}: {e}") from None


__all__ = ["request_json", "HttpError", "Blocked"]
