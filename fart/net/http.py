"""Minimal JSON-over-HTTPS client (urllib) that goes through the budget gate."""
from __future__ import annotations

import json
import socket
import time
import urllib.error
import urllib.request
from typing import Any, Callable

from ..log import get_logger
from .budget import Blocked, Budget

log = get_logger("http")

USER_AGENT = "fart/0.1 (+read-only public data consumer)"


class HttpError(Exception):
    def __init__(self, status: int | None, message: str, retry_after_s: float | None = None, body: str | None = None):
        super().__init__(f"HTTP {status}: {message}")
        self.status = status
        self.retry_after_s = retry_after_s
        self.body = body  # first 2000 bytes of an error body (Telegram puts retry_after in it); never logged raw


def _parse_retry_after(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return float(value)
    except ValueError:
        return None  # HTTP-date form is rare on these APIs; backoff still applies


def retry_after_from_headers(headers: Any, now: float | None = None) -> float | None:
    """Retry-After in seconds, else X's x-rate-limit-reset (epoch seconds) converted to a delay."""
    if not headers:
        return None
    ra = _parse_retry_after(headers.get("Retry-After"))
    if ra is not None:
        return ra
    reset = _parse_retry_after(headers.get("x-rate-limit-reset"))
    if reset is not None:
        return max(1.0, reset - (time.time() if now is None else now))
    return None


def request_json(
    budget: Budget,
    source: str,
    method: str,
    url: str,
    body: Any | None = None,
    headers: dict[str, str] | None = None,
    timeout_s: float = 10.0,
    cost: float | None = None,
    cost_from_response: Callable[[Any], float] | None = None,
) -> Any:
    """Acquire -> call -> record. Raises Blocked when the gate says no, HttpError on failure.

    `method` is the budget method name (e.g. 'getTransaction', 'tokens'), not the HTTP verb.
    `cost` is the estimate the gate checks; `cost_from_response(parsed)` may replace it with the
    actual cost once the body is known (X charges per post returned). The URL and the headers are
    never logged (RPC URLs carry the API key, X and Telegram use bearer tokens).
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
        actual = verdict.cost
        if cost_from_response is not None:
            try:
                actual = float(cost_from_response(parsed))
            except Exception:  # a costing bug must never lose the call record
                actual = verdict.cost
        budget.record(source, method, actual, True, status, latency)
        return parsed
    except urllib.error.HTTPError as e:
        latency = (time.monotonic() - t0) * 1000.0
        retry_after = retry_after_from_headers(e.headers, budget.clock())
        body: str | None = None
        try:
            body = e.read(2000).decode("utf-8", errors="replace")
        except Exception:
            body = None
        # a response-priced call (X: per post returned) that failed returned nothing and is not billed
        budget.record(source, method, 0.0 if cost_from_response is not None else verdict.cost, False, e.code, latency,
                      err=str(e.reason), retry_after_s=retry_after)
        raise HttpError(e.code, str(e.reason), retry_after, body=body) from None
    except (urllib.error.URLError, socket.timeout, TimeoutError, ConnectionError, OSError) as e:
        latency = (time.monotonic() - t0) * 1000.0
        budget.record(source, method, 0.0 if cost_from_response is not None else verdict.cost, False, None, latency,
                      err=f"{type(e).__name__}: {e}")
        raise HttpError(None, f"{type(e).__name__}: {e}") from None


__all__ = ["request_json", "HttpError", "Blocked"]
