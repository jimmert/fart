"""Login for the website (milestone 8): one owner account, signed session cookies, a CSRF token, a login guard.

* The password lives in `.env` only as an scrypt hash (`FART_WEB_PASSWORD_HASH`, made with `fart web hash-password`;
  `hashlib.scrypt`, n=2^14, r=8, p=1, 16 random salt bytes, 32 bytes of key). The format `scrypt:n:r:p:<salt hex>:<key hex>`
  carries no `$` or `=`, so it survives the `.env` parser and a shell helper unchanged. Verification compares in
  constant time (`hmac.compare_digest`).
* A session is a stateless signed token `<issued>.<nonce>.<signature>` (HMAC-SHA256 under `FART_WEB_SECRET`, made with
  `fart web new-secret`), valid for `SESSION_TTL_S` (12 h) after it was issued, in a cookie that is HttpOnly, Secure and
  SameSite=Strict. A token signed under another secret, altered, expired or logged out is invalid. Logging out revokes the
  token's nonce in memory until it would have expired anyway; a restart of the service forgets the revocations but a new
  secret in `.env` invalidates everything at once.
* The login form carries a CSRF token: the same signed shape under the `csrf` domain, set as a cookie for `/login` and
  echoed in a hidden field; the POST must carry both and they must match (double submit plus a signature, at most
  `CSRF_TTL_S` old). The logout form carries a token derived from the session's nonce.
* The login guard counts failed attempts per client IP and per account (there is one account, `owner`): more than
  `MAX_FAILED` (5) failures within `FAIL_WINDOW_S` (10 minutes) lock the key for `LOCK_S` (15 minutes); a locked key
  is refused before the password is looked at. The counters live in memory; because the website's database connection is
  read-only (the `kv` table is off limits) they are also recorded in the service's own log (`web.login_failed`, with the
  IP and the time, never the password) and replayed from it at start, so a restart does not reset a running lock.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import math
import os
import secrets
import threading
import time
from pathlib import Path
from typing import Any, Callable

HASH_SCHEME = "scrypt"
SCRYPT_N = 2 ** 14
SCRYPT_R = 8
SCRYPT_P = 1
SCRYPT_DKLEN = 32
SALT_BYTES = 16
SECRET_MIN_CHARS = 32          # the generator gives 64 hex characters (32 bytes)
SESSION_TTL_S = 12 * 3600
CSRF_TTL_S = 3600
MAX_FAILED = 5
FAIL_WINDOW_S = 600
LOCK_S = 900
ACCOUNT = "owner"
SESSION_COOKIE = "fart_session"
CSRF_COOKIE = "fart_csrf"
LOG_EVENT_FAILED = "web.login_failed"


class WebConfigError(ValueError):
    """`.env` lacks or misshapes one of the two website values; `fart web` refuses to start with this message."""


# ----- passwords -----------------------------------------------------------------------------------------------
def hash_password(password: str, *, n: int = SCRYPT_N, r: int = SCRYPT_R, p: int = SCRYPT_P, salt: bytes | None = None) -> str:
    """`scrypt:n:r:p:<salt hex>:<key hex>` for `.env`. A fresh random salt every call."""
    if not password:
        raise ValueError("empty password")
    salt = os.urandom(SALT_BYTES) if salt is None else bytes(salt)
    key = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=int(n), r=int(r), p=int(p), dklen=SCRYPT_DKLEN)
    return f"{HASH_SCHEME}:{int(n)}:{int(r)}:{int(p)}:{salt.hex()}:{key.hex()}"


def parse_hash(value: str | None) -> tuple[int, int, int, bytes, bytes] | None:
    """(n, r, p, salt, key) of a well-formed hash, else None. Bounds keep a garbled value from costing seconds or gigabytes."""
    if not value:
        return None
    parts = str(value).strip().split(":")
    if len(parts) != 6 or parts[0] != HASH_SCHEME:
        return None
    try:
        n, r, p = int(parts[1]), int(parts[2]), int(parts[3])
        salt, key = bytes.fromhex(parts[4]), bytes.fromhex(parts[5])
    except ValueError:
        return None
    if n < 2 ** 12 or n > 2 ** 20 or (n & (n - 1)) != 0 or r < 1 or r > 32 or p < 1 or p > 16:
        return None
    if len(salt) < 8 or len(key) < 16:
        return None
    return n, r, p, salt, key


def verify_password(password: str, stored: str | None) -> bool:
    parsed = parse_hash(stored)
    if parsed is None or not password:
        return False
    n, r, p, salt, key = parsed
    try:
        cand = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=n, r=r, p=p, dklen=len(key))
    except (ValueError, MemoryError):
        return False
    return hmac.compare_digest(cand, key)


def new_secret() -> str:
    return secrets.token_hex(32)


def check_config(password_hash: str | None, secret: str | None) -> list[str]:
    """The problems with the two `.env` values, in plain words; empty when both are usable."""
    problems = []
    if not (password_hash or "").strip():
        problems.append("FART_WEB_PASSWORD_HASH is empty: run `fart web hash-password` and put the printed hash in .env")
    elif parse_hash(password_hash) is None:
        problems.append("FART_WEB_PASSWORD_HASH is not a hash made by `fart web hash-password` (scrypt:n:r:p:salt:key)")
    s = (secret or "").strip()
    if not s:
        problems.append("FART_WEB_SECRET is empty: run `fart web new-secret` and put the printed value in .env")
    elif len(s) < SECRET_MIN_CHARS:
        problems.append(f"FART_WEB_SECRET is too short (at least {SECRET_MIN_CHARS} characters; `fart web new-secret` prints 64)")
    return problems


# ----- signed tokens -------------------------------------------------------------------------------------------
class Signer:
    """HMAC-SHA256 tokens `<issued>.<nonce>.<signature>` with a domain prefix so a CSRF token can never pass as a session."""

    def __init__(self, secret: str):
        if not secret or len(secret) < SECRET_MIN_CHARS:
            raise WebConfigError("FART_WEB_SECRET is missing or too short")
        self._key = secret.encode("utf-8")

    def _sig(self, domain: str, issued: int, nonce: str) -> str:
        return hmac.new(self._key, f"{domain}.{issued}.{nonce}".encode("ascii"), hashlib.sha256).hexdigest()

    def issue(self, domain: str, now: float) -> str:
        issued = int(now)
        nonce = secrets.token_hex(16)
        return f"{issued}.{nonce}.{self._sig(domain, issued, nonce)}"

    def verify(self, domain: str, token: str | None, now: float, ttl_s: float) -> dict[str, Any] | None:
        """The token's parts when its signature holds and it is not older than `ttl_s`; None otherwise."""
        if not token or len(token) > 200:
            return None
        parts = str(token).split(".")
        if len(parts) != 3:
            return None
        try:
            issued = int(parts[0])
        except ValueError:
            return None
        nonce, sig = parts[1], parts[2]
        if len(nonce) != 32 or not all(c in "0123456789abcdef" for c in nonce):
            return None
        if not hmac.compare_digest(sig, self._sig(domain, issued, nonce)):
            return None
        if issued > now + 60 or now - issued > ttl_s:
            return None  # from the future (a clock moved) or expired
        return {"issued": issued, "nonce": nonce, "expires": issued + ttl_s}

    # the three uses
    def session_token(self, now: float) -> str:
        return self.issue("session", now)

    def verify_session(self, token: str | None, now: float) -> dict[str, Any] | None:
        return self.verify("session", token, now, SESSION_TTL_S)

    def csrf_token(self, now: float) -> str:
        return self.issue("csrf", now)

    def verify_csrf(self, token: str | None, now: float) -> bool:
        return self.verify("csrf", token, now, CSRF_TTL_S) is not None

    def logout_token(self, session_nonce: str) -> str:
        return hmac.new(self._key, f"logout.{session_nonce}".encode("ascii"), hashlib.sha256).hexdigest()[:32]

    def verify_logout(self, token: str | None, session_nonce: str) -> bool:
        return bool(token) and hmac.compare_digest(str(token), self.logout_token(session_nonce))


class Revocations:
    """Nonces of sessions that logged out, kept until the session would have expired anyway."""

    def __init__(self):
        self._lock = threading.Lock()
        self._until: dict[str, float] = {}

    def revoke(self, nonce: str, expires: float) -> None:
        with self._lock:
            self._until[nonce] = float(expires)

    def is_revoked(self, nonce: str, now: float) -> bool:
        with self._lock:
            for k in [k for k, v in self._until.items() if v <= now]:
                del self._until[k]
            return nonce in self._until


# ----- cookies ---------------------------------------------------------------------------------------------------
def parse_cookies(header: str | None) -> dict[str, str]:
    out: dict[str, str] = {}
    for part in (header or "").split(";"):
        if "=" not in part:
            continue
        k, v = part.split("=", 1)
        k, v = k.strip(), v.strip()
        if k and k not in out:
            out[k] = v
    return out


def set_cookie(name: str, value: str, max_age: int, path: str = "/") -> str:
    """HttpOnly, Secure, SameSite=Strict; `max_age` 0 deletes the cookie."""
    return f"{name}={value}; Path={path}; Max-Age={int(max_age)}; HttpOnly; Secure; SameSite=Strict"


def clear_cookie(name: str, path: str = "/") -> str:
    return set_cookie(name, "", 0, path)


# ----- the login guard ------------------------------------------------------------------------------------------
class LoginGuard:
    """Failed attempts per client IP and per account; a key with MAX_FAILED failures inside FAIL_WINDOW_S is locked for LOCK_S."""

    def __init__(self, clock: Callable[[], float] = time.time, max_failed: int = MAX_FAILED, window_s: float = FAIL_WINDOW_S,
                 lock_s: float = LOCK_S):
        self.clock = clock
        self.max_failed = int(max_failed)
        self.window_s = float(window_s)
        self.lock_s = float(lock_s)
        self._lock = threading.Lock()
        self._failures: dict[str, list[float]] = {}
        self._locked_until: dict[str, float] = {}

    @staticmethod
    def keys(ip: str) -> tuple[str, str]:
        return f"ip:{ip or '?'}", f"account:{ACCOUNT}"

    def _prune(self, key: str, now: float) -> list[float]:
        lst = [t for t in self._failures.get(key, []) if now - t < self.window_s]
        if lst:
            self._failures[key] = lst
        else:
            self._failures.pop(key, None)
        return lst

    def wait_s(self, ip: str, now: float | None = None) -> float | None:
        """Seconds to wait when the IP or the account is locked, else None."""
        t = self.clock() if now is None else now
        with self._lock:
            waits = []
            for k in self.keys(ip):
                until = self._locked_until.get(k)
                if until is not None:
                    if until > t:
                        waits.append(until - t)
                    else:
                        del self._locked_until[k]
            return max(waits) if waits else None

    def record_failure(self, ip: str, now: float | None = None) -> float | None:
        """Count a failure for both keys; returns the wait in seconds when this failure locked a key, else None."""
        t = self.clock() if now is None else now
        locked = None
        with self._lock:
            for k in self.keys(ip):
                lst = self._prune(k, t)
                lst.append(t)
                self._failures[k] = lst
                if len(lst) >= self.max_failed:
                    self._locked_until[k] = max(self._locked_until.get(k, 0.0), t + self.lock_s)
                    self._failures.pop(k, None)
                    locked = self.lock_s
        return locked

    def record_success(self, ip: str) -> None:
        with self._lock:
            for k in self.keys(ip):
                self._failures.pop(k, None)

    def remaining(self, ip: str, now: float | None = None) -> int:
        """Attempts left before the IP (or the account, whichever is closer) locks."""
        t = self.clock() if now is None else now
        with self._lock:
            return max(0, min(self.max_failed - len(self._prune(k, t)) for k in self.keys(ip)))

    def load_from_log(self, log_path: Path | str, now: float | None = None, max_bytes: int = 512 * 1024) -> int:
        """Replay the `web.login_failed` lines of the service's own log (JSON lines) that fall inside the window that can
        still matter (window + lock), oldest first. Returns the number of failures replayed. A missing or unreadable log
        replays nothing; a malformed line is skipped."""
        t = self.clock() if now is None else now
        p = Path(log_path)
        try:
            size = p.stat().st_size
            with open(p, "rb") as fh:
                if size > max_bytes:
                    fh.seek(size - max_bytes)
                    fh.readline()  # the rest of a cut line
                data = fh.read()
        except OSError:
            return 0
        horizon = t - (self.window_s + self.lock_s)
        events: list[tuple[float, str]] = []
        for raw in data.splitlines():
            if LOG_EVENT_FAILED.encode("ascii") not in raw:
                continue
            try:
                d = json.loads(raw.decode("utf-8", "replace"))
            except ValueError:
                continue
            if not isinstance(d, dict) or d.get("event") != LOG_EVENT_FAILED:
                continue
            try:
                ts = float(d.get("ts"))
            except (TypeError, ValueError):
                continue
            if not math.isfinite(ts) or ts < horizon or ts > t + 60:
                continue
            events.append((ts, str(d.get("ip") or "?")))
        events.sort()
        for ts, ip in events:
            self.record_failure(ip, ts)
        return len(events)
