"""Telegram Bot API sendMessage through the budget gate (source `telegram`, 20 per minute).

The request URL carries the bot token: it is never logged, and errors are reported by status and
a short description only. 429 answers carry `parameters.retry_after` (and usually a Retry-After
header); both are honored.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Callable

from ..log import get_logger
from ..net.budget import Blocked, Budget
from ..net.http import HttpError, request_json

log = get_logger("telegram")

SOURCE = "telegram"
API = "https://api.telegram.org"


@dataclass
class SendResult:
    ok: bool
    message_id: int | None = None
    status: int | None = None
    retry_after_s: float | None = None
    error: str | None = None
    blocked: bool = False      # refused by the gate (rate/backoff), not an API failure
    permanent: bool = False    # 4xx other than 429: retrying will not help


def _retry_after_from_body(body: str | None) -> float | None:
    if not body:
        return None
    try:
        data = json.loads(body)
        ra = (data.get("parameters") or {}).get("retry_after")
        return float(ra) if ra is not None else None
    except (ValueError, AttributeError, TypeError):
        return None


def _description(body: str | None, fallback: str) -> str:
    if body:
        try:
            d = json.loads(body).get("description")
            if d:
                return str(d)[:200]
        except (ValueError, AttributeError):
            pass
    return fallback[:200]


class TelegramSender:
    def __init__(self, budget: Budget, token: str, request: Callable[..., Any] = request_json, timeout_s: float = 10.0, api: str = API):
        self.budget = budget
        self._token = token
        self._request = request
        self.timeout_s = timeout_s
        self.api = api.rstrip("/")

    def send(self, chat_id: str, text: str) -> SendResult:
        url = f"{self.api}/bot{self._token}/sendMessage"
        body = {"chat_id": chat_id, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True}
        try:
            resp = self._request(self.budget, SOURCE, "sendMessage", url, body=body, timeout_s=self.timeout_s)
        except Blocked as e:
            return SendResult(False, blocked=True, retry_after_s=e.retry_in_s, error=f"gate: {e.reason}")
        except HttpError as e:
            ra = e.retry_after_s if e.retry_after_s is not None else _retry_after_from_body(e.body)
            desc = _description(e.body, str(e))
            if e.status == 429:
                return SendResult(False, status=429, retry_after_s=ra, error=f"429 {desc}")
            permanent = e.status is not None and 400 <= e.status < 500
            return SendResult(False, status=e.status, error=f"{e.status} {desc}", permanent=permanent)
        if isinstance(resp, dict) and resp.get("ok"):
            mid = (resp.get("result") or {}).get("message_id")
            return SendResult(True, message_id=int(mid) if isinstance(mid, int) else None, status=200)
        desc = str((resp or {}).get("description") if isinstance(resp, dict) else resp)[:200]
        return SendResult(False, status=200, error=f"api not ok: {desc}", permanent=True)
