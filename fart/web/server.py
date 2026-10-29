"""`fart web`: the HTTP server of the website (milestones 8 and 9).

`App.handle()` turns a `Request` into a `Response` without touching sockets (the tests call it directly and through a
live server); `Handler` adapts `http.server.BaseHTTPRequestHandler` to it and enforces what only the socket layer can:
the request-size limit (64 KB for the request line, the headers and the body together: 431 or 413, the connection
closed without reading the body), a socket timeout, HTTP/1.1 with an explicit Content-Length on every response.

Every response carries the security headers: a strict Content-Security-Policy (`default-src 'self'`, no inline
scripts or styles: the files under /static/ are the only code and style), `X-Content-Type-Options: nosniff`,
`Referrer-Policy: no-referrer`, `X-Frame-Options: DENY`; `Cache-Control: no-store` unless a route says otherwise.

Milestone 9 splits the site in two:

* the **public section** (`FART_WEB_PUBLIC=1`, the default): `/` (the dashboard), `/observaties`, `/rapporten`,
  `/rapporten/<day>`, `/rapporten/week-<day>`, `/methode`, `/tools` and the JSON under `/api/public/*` need no session.
  Every view behind them is computed by one background thread (`ViewStore`, milestone 10: at start and then every
  `LIVE_TTL_S` (15 s) for the live views and every `CACHE_TTL_S` (30 s) for the rest, with its own read-only connection
  and short transactions); a request only reads the last value and never waits for a computation. A view that was never
  computed shows "wordt berekend" on the page and `{"state": "computing"}` in the JSON (200, `Cache-Control` of a few
  seconds). The public answers carry `Cache-Control: public, max-age=15` or `30` and an `ETag` (a matching
  `If-None-Match` gets 304), and a client without a session may make at most `RATE_LIMIT` requests per minute to the
  public routes (then 429 with `Retry-After`). With `FART_WEB_PUBLIC=0` the whole site is behind the login, as in
  milestone 8;
* the **owner section**: `/eigenaar` (health, budgets, the X planner, settings, the log summary), the raw Markdown of
  every report under `/rapporten/<file>.md` and the milestone-8 JSON under `/api/*`, always behind the login (scrypt
  password, signed session cookie, CSRF, lockout: `fart/web/auth.py`). `/instellingen` redirects to `/eigenaar`.

The log (`logs/web.log` and the journal) carries the method, the path without its query, the status, the duration
and the client IP (the last `X-Forwarded-For` entry, which the shared Caddy appends, when the peer is the loopback);
never a cookie, a password, a token or a request body. Failed logins are logged as `web.login_failed` with the IP so
the login guard can replay them after a restart. A view computation longer than `SLOW_VIEW_S` is logged as
`web.view_slow` with the view's name and its duration; the last duration per view is on `/eigenaar` and, through
`logs/web-views.json` (the one file next to the log the service writes), in `fart status`.
"""
from __future__ import annotations

import collections
import hashlib
import hmac
import json
import math
import re
import signal
import select
import socket
import sys
import threading
import time
import urllib.parse
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable

from .. import db as _db  # noqa: F401  (the read-only connection is opened in data.open_db)
from ..config import Settings
from ..log import Throttle, get_logger
from ..notify.format import fmt_time
from ..util import atomic_write_json, now as wall_now, utc_iso
from . import auth, data, markdown, pages, public, terminal, theme

log = get_logger("web")

MAX_REQUEST_BYTES = 64 * 1024
SOCKET_TIMEOUT_S = 30
CACHE_TTL_S = 30
LIVE_TTL_S = 15            # the live dashboard views
CACHE_MAX_ITEMS = 128      # on-demand entries (a day filter of the observations) kept at most
SLOW_VIEW_S = 10.0         # a computation longer than this is logged with the view's name and its duration
VIEWS_POLL_S = 1.0         # how often the background thread looks for a view that is due
VIEWS_STOP_S = 10.0        # how long a stop waits for a running computation before giving up on it
WANTED_S = 600.0           # an on-demand entry nobody asked for this long is dropped
COMPUTING_MAX_AGE_S = 2    # Cache-Control of an answer that says "wordt berekend"
VIEWS_FILE = "web-views.json"   # under logs/: the last duration per view, read by `fart status`
VIEWS_LOG_EVERY_S = 300.0  # the durations of a cycle are logged at most this often
RATE_LIMIT = 120           # public requests per IP per window
RATE_WINDOW_S = 60
STATIC_DIR = Path(__file__).resolve().parent / "static"
STATIC_TYPES = {"app.css": "text/css; charset=utf-8", "app.js": "text/javascript; charset=utf-8", "terminal.js": "text/javascript; charset=utf-8", "icon.svg": "image/svg+xml"}
CSP = ("default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self'; connect-src 'self'; font-src 'self'; "
       "object-src 'none'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'")
SECURITY_HEADERS = (
    ("Content-Security-Policy", CSP),
    ("X-Content-Type-Options", "nosniff"),
    ("Referrer-Policy", "no-referrer"),
    ("X-Frame-Options", "DENY"),
    ("Cache-Control", "no-store"),
)
LOOPBACK = ("127.0.0.1", "::1", "::ffff:127.0.0.1")
PUBLIC_CACHE_CONTROL = f"public, max-age={CACHE_TTL_S}"
REPORT_PAGE = re.compile(r"^(week-)?\d{4}-\d{2}-\d{2}$")
DAY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


@dataclass
class Request:
    method: str
    path: str
    query: dict[str, str]
    headers: dict[str, str]        # lower-case names
    body: bytes
    ip: str

    @property
    def cookies(self) -> dict[str, str]:
        return auth.parse_cookies(self.headers.get("cookie"))

    def form(self) -> dict[str, str]:
        ctype = (self.headers.get("content-type") or "").split(";")[0].strip().lower()
        if ctype != "application/x-www-form-urlencoded":
            return {}
        parsed = urllib.parse.parse_qs(self.body.decode("utf-8", "replace"), keep_blank_values=True, max_num_fields=20)
        return {k: v[0] for k, v in parsed.items() if v}


@dataclass
class Response:
    status: int
    body: bytes = b""
    content_type: str = "text/html; charset=utf-8"
    headers: list[tuple[str, str]] = field(default_factory=list)
    cookies: list[str] = field(default_factory=list)
    close: bool = False

    def all_headers(self) -> list[tuple[str, str]]:
        merged: dict[str, str] = {k: v for k, v in SECURITY_HEADERS}
        merged["Content-Type"] = self.content_type
        for k, v in self.headers:
            merged[k] = v
        out = list(merged.items())
        out += [("Set-Cookie", c) for c in self.cookies]
        return out


@dataclass
class StreamResponse(Response):
    """Milestone 12: a server-sent-events answer. `events` yields the encoded events (the socket layer writes them chunked
    as they come and keeps the connection open); `on_close` runs when the client went away or the server stops."""
    events: Any = None
    on_close: Callable[[], None] | None = None


STREAM_WAIT_S = 1.0   # milestone 12: how long a stream waits for an event before it checks that the client is still connected
SSE_HEADERS = (("Cache-Control", "no-cache, no-store"), ("X-Accel-Buffering", "no"), ("Connection", "close"))


def html_response(status: int, body: str, **kw: Any) -> Response:
    return Response(status, body.encode("utf-8"), "text/html; charset=utf-8", **kw)


def json_response(status: int, obj: Any, **kw: Any) -> Response:
    return Response(status, json.dumps(obj, ensure_ascii=False, default=str).encode("utf-8"), "application/json; charset=utf-8", **kw)


def redirect(location: str, cookies: list[str] | None = None) -> Response:
    return Response(303, b"", "text/plain; charset=utf-8", headers=[("Location", location)], cookies=cookies or [])


def etag_of(body: bytes) -> str:
    return '"' + hashlib.sha256(body).hexdigest()[:20] + '"'


# ----- the precomputed public views (milestone 10) ----------------------------------------------------------------------
@dataclass
class Entry:
    value: Any                 # the last value that was computed (None: never)
    computed_at: float         # the application clock at the last successful computation
    next_at: float             # when the view is due again
    seconds: float             # the duration of the last computation (also of a failed one)
    ttl_s: float
    ok: bool = True
    errors: int = 0
    last_error: str | None = None
    last_error_at: float | None = None


class ViewStore:
    """The precomputed public views: one background thread computes every view on its schedule (the scheduled keys in
    their order, each when its TTL is over; at start everything once) with its own read-only connection and short
    transactions (autocommit: every statement is its own read); a request only reads (`get`) and never computes or
    waits. A view that was never computed reads as None (the page says "wordt berekend"); a view whose computation
    failed keeps its previous value and the time it was made (the pages show when their data was made) and is retried
    when it is due again. On-demand keys (`request`: a day filter of the observations) are computed by the thread after
    the first request, refreshed while they keep being asked for and dropped after `WANTED_S` of silence, at most
    `max_items` of them. The duration of every computation is kept (`stats`), a slow one is logged (`web.view_slow`
    with the name and the seconds), and the durations go to `status_path` (logs/web-views.json) for `fart status`."""

    def __init__(self, compute: Callable[[str, Any], Any], schedule: Callable[[], list[tuple[str, float]]], clock: Callable[[], float] = wall_now,
                 open_db: Callable[[], Any] | None = None, children: Callable[[str, Any], list[str]] | None = None, max_items: int = CACHE_MAX_ITEMS,
                 slow_s: float = SLOW_VIEW_S, status_path: Path | None = None):
        self.compute = compute
        self.schedule = schedule
        self.clock = clock
        self.open_db = open_db
        self.children = children or (lambda key, value: [])
        self.max_items = int(max_items)
        self.slow_s = float(slow_s)
        self.status_path = status_path
        self._lock = threading.Lock()           # the entries and the wanted keys
        self._cycle = threading.Lock()          # one refresh at a time
        self._entries: dict[str, Entry] = {}
        self._wanted: dict[str, tuple[float, float]] = {}   # on-demand key -> (last asked, ttl)
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None
        self._conn: Any = None                  # the connection of the running cycle (interrupted by stop)
        self._log_throttle = Throttle(VIEWS_LOG_EVERY_S)
        self._status_failed = False
        self.computes = 0
        self.failures = 0
        self.hits = 0
        self.misses = 0

    # ----- reading (the request threads) ---------------------------------------------------------------------------
    def get(self, key: str) -> Entry | None:
        """The entry of a key, at once; None when it was never computed. Never computes."""
        with self._lock:
            e = self._entries.get(key)
            if e is None or e.value is None:
                self.misses += 1
            else:
                self.hits += 1
            return e

    def request(self, key: str, ttl_s: float = CACHE_TTL_S) -> Entry | None:
        """An on-demand key: remembered as wanted (the thread computes it next), its entry returned at once."""
        with self._lock:
            self._wanted[key] = (self.clock(), float(ttl_s))
        self._wake.set()
        return self.get(key)

    def stats(self, now: float | None = None) -> list[dict[str, Any]]:
        """Per key: the last duration, when the value was made, the errors; the scheduled keys first, then the rest."""
        t = self.clock() if now is None else now
        with self._lock:
            items = list(self._entries.items())
        order = {k: i for i, (k, _) in enumerate(self.schedule())}
        items.sort(key=lambda kv: (order.get(kv[0], len(order)), kv[0]))
        out = []
        for key, e in items:
            age = None if e.value is None else max(0.0, t - e.computed_at)
            out.append({"view": key, "seconds": round(e.seconds, 3), "computed_at": utc_iso(e.computed_at) if e.value is not None else None, "age_s": None if age is None else round(age),
                        "ok": e.ok, "errors": e.errors, "last_error": e.last_error, "ttl_s": e.ttl_s, "state": "computed" if e.value is not None else ("failed" if e.errors else "computing")})
        return out

    # ----- computing (the background thread, or a test) ------------------------------------------------------------
    def due(self, now: float | None = None) -> list[tuple[str, float]]:
        t = self.clock() if now is None else now
        out = []
        with self._lock:
            for key, ttl in self.schedule():
                if self._due(key, t):
                    out.append((key, float(ttl)))
            for key, (asked, ttl) in list(self._wanted.items()):
                if t - asked > WANTED_S or asked > t + WANTED_S:
                    self._wanted.pop(key, None)
                    self._entries.pop(key, None)
                elif self._due(key, t):
                    out.append((key, float(ttl)))
        return out

    def _due(self, key: str, now: float) -> bool:
        e = self._entries.get(key)
        return e is None or now >= e.next_at or e.next_at > now + e.ttl_s   # the last case: the clock moved back

    def refresh(self, now: float | None = None, force: bool = False) -> list[str]:
        """Compute every view that is due (every scheduled view with `force`) and return their keys. One at a time:
        a second caller while a cycle runs computes nothing."""
        if not self._cycle.acquire(blocking=False):
            return []
        try:
            t = self.clock() if now is None else now
            todo = [(k, float(ttl)) for k, ttl in self.schedule()] if force else self.due(t)
            if not todo:
                return []
            conn = None
            if self.open_db is not None:
                try:
                    conn = self.open_db()
                except Exception:  # noqa: BLE001  no database: every view says so
                    log.error("web.views_db_failed", exc_info=True)
                    conn = None
            with self._lock:
                self._conn = conn
            done: list[str] = []
            try:
                for key, ttl in todo:
                    if self._stop.is_set():
                        break
                    value = self._compute_one(key, ttl, conn)
                    done.append(key)
                    if value is not None:
                        kids = self.children(key, value)
                        for kid in kids:
                            if self._stop.is_set():
                                break
                            self._compute_one(kid, ttl, conn)
                            done.append(kid)
                        self._prune_children(key, kids)
            finally:
                with self._lock:
                    self._conn = None
                if conn is not None:
                    try:
                        conn.close()
                    except Exception:  # noqa: BLE001
                        pass
            self._trim()
            if done:
                self._write_status()
                if self._log_throttle.allow("cycle", t):
                    log.info("web.views", **{k: round(self._entries[k].seconds, 3) for k in done if k in self._entries and ":" not in k})
            return done
        finally:
            self._cycle.release()

    def _compute_one(self, key: str, ttl: float, conn: Any) -> Any:
        t0 = time.perf_counter()
        started = self.clock()
        try:
            value = self.compute(key, conn)
        except Exception as e:  # noqa: BLE001  the previous value stays; the log has the traceback
            dt = time.perf_counter() - t0
            self.failures += 1
            log.error("web.view_failed", view=key, seconds=round(dt, 3), exc_info=True)
            with self._lock:
                prev = self._entries.get(key)
                self._entries[key] = Entry(prev.value if prev else None, prev.computed_at if prev else started, self.clock() + ttl, dt, ttl, ok=False,
                                           errors=(prev.errors if prev else 0) + 1, last_error=f"{type(e).__name__}: {str(e)[:120]}", last_error_at=started)
            return None
        dt = time.perf_counter() - t0
        self.computes += 1
        if dt > self.slow_s:
            log.warning("web.view_slow", view=key, seconds=round(dt, 2), limit_s=self.slow_s)
        with self._lock:
            prev = self._entries.get(key)
            self._entries[key] = Entry(value, started, self.clock() + ttl, dt, ttl, ok=True, errors=prev.errors if prev else 0,
                                       last_error=prev.last_error if prev else None, last_error_at=prev.last_error_at if prev else None)
        return value

    def _prune_children(self, key: str, kids: list[str]) -> None:
        prefix = f"{key}:"
        with self._lock:
            for k in [k for k in self._entries if k.startswith(prefix) and k not in kids]:
                self._entries.pop(k, None)

    def _trim(self) -> None:
        """The on-demand entries are bounded: the ones asked for the longest ago go first."""
        with self._lock:
            if len(self._wanted) <= self.max_items:
                return
            for k in sorted(self._wanted, key=lambda k: self._wanted[k][0])[: len(self._wanted) - self.max_items]:
                self._wanted.pop(k, None)
                self._entries.pop(k, None)

    def _write_status(self) -> None:
        if self.status_path is None:
            return
        try:
            views = {row["view"]: {"seconds": row["seconds"], "computed_at": row["computed_at"], "age_s": row["age_s"], "ok": row["ok"], "errors": row["errors"],
                                   "state": row["state"]} for row in self.stats()}
            atomic_write_json(self.status_path, {"written_at": self.clock(), "written_label": utc_iso(self.clock()), "views": views})
            self._status_failed = False
        except OSError as e:
            if not self._status_failed:
                log.warning("web.views_status_not_written", path=str(self.status_path), error=str(e)[:120])
            self._status_failed = True

    # ----- the thread ---------------------------------------------------------------------------------------------------
    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="fart-web-views", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.refresh()
            except Exception:  # noqa: BLE001  the thread never dies on one cycle
                log.error("web.views_cycle_failed", exc_info=True)
            self._wake.wait(VIEWS_POLL_S)
            self._wake.clear()

    def stop(self, timeout: float = VIEWS_STOP_S) -> bool:
        """Stop the thread: a running query is interrupted, the cycle ends, the thread exits. True when it did in time."""
        self._stop.set()
        self._wake.set()
        with self._lock:
            conn = self._conn
        if conn is not None:
            try:
                conn.interrupt()
            except Exception:  # noqa: BLE001
                pass
        t = self._thread
        if t is None:
            return True
        t.join(timeout)
        return not t.is_alive()

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()


# ----- the per-IP limit of the public routes ---------------------------------------------------------------------------
class RateLimiter:
    """A sliding window per client: at most `limit` requests in the last `window_s` seconds; bounded memory."""

    def __init__(self, limit: int = RATE_LIMIT, window_s: float = RATE_WINDOW_S, max_clients: int = 10000):
        self.limit = int(limit)
        self.window_s = float(window_s)
        self.max_clients = int(max_clients)
        self._lock = threading.Lock()
        self._hits: dict[str, collections.deque] = {}

    def check(self, ip: str, now: float) -> float | None:
        """None when the request may pass (and it is counted); else the seconds to wait."""
        key = ip or "?"
        with self._lock:
            dq = self._hits.get(key)
            if dq is None:
                if len(self._hits) >= self.max_clients:
                    self._evict(now)
                dq = self._hits[key] = collections.deque()
            while dq and now - dq[0] >= self.window_s:
                dq.popleft()
            if len(dq) >= self.limit:
                return max(1.0, self.window_s - (now - dq[0]))
            dq.append(now)
            return None

    def _evict(self, now: float) -> None:
        for k in [k for k, dq in self._hits.items() if not dq or now - dq[-1] >= self.window_s]:
            del self._hits[k]
        if len(self._hits) >= self.max_clients:
            for k in sorted(self._hits, key=lambda k: self._hits[k][-1] if self._hits[k] else 0)[: len(self._hits) // 2]:
                del self._hits[k]


class App:
    """The routes, the session check and the page assembly. The public views come from the `ViewStore` (computed by its
    thread, read by the requests); the owner section opens one read-only database connection per request."""

    PUBLIC_API = {"/api/public/status": "status", "/api/public/coverage": "coverage", "/api/public/observations": "observations",
                  "/api/public/reports": "reports", "/api/public/week": "week", "/api/public/live": "live", "/api/public/hot": "hot",
                  "/api/public/new": "new", "/api/public/themes": "themes", "/api/public/graduations": "graduations", "/api/public/social": "social",
                  "/api/public/plays": "plays"}
    LIVE_VIEWS = ("live", "hot", "new", "themes", "graduations", "social", "today")
    REST_VIEWS = ("status", "coverage", "week", "reports", "observations", "method", "plays")     # every 30 s; "reports" also computes every report page
    DASHBOARD_VIEWS = ("live", "hot", "new", "themes", "graduations", "social", "today", "week", "coverage")
    OWNER_API = {"/api/status": "status", "/api/coverage": "coverage", "/api/decisions": "decisions", "/api/x": "x",
                 "/api/report/latest": "report", "/api/week/latest": "week"}
    PUBLIC_PAGES = ("/", "/observaties", "/meting", "/rapporten", "/methode", "/tools", "/terminal", "/plays")
    STREAM_PATH = "/api/public/stream"          # milestone 12: the Terminal's server-sent events
    SNAPSHOT_PATH = "/api/public/terminal"      # milestone 12: the feed's last snapshot as JSON

    def __init__(self, settings: Settings, clock: Callable[[], float] = wall_now):
        problems = auth.check_config(settings.web_password_hash, settings.web_secret)
        if problems:
            raise auth.WebConfigError("; ".join(problems))
        self.settings = settings
        self.clock = clock
        self.signer = auth.Signer(settings.web_secret)
        self.guard = auth.LoginGuard(clock)
        self.revoked = auth.Revocations()
        ws = settings.web_site
        self.site = {"name": str(ws["site_name"]), "product": str(ws["product"]), "tagline": str(ws["tagline"]), "domain": str(ws["domain"]),
                     "accent": str(ws.get("accent") or ""), "refresh_s": int(ws.get("refresh_s") or 30), "live_refresh_s": int(ws.get("live_refresh_s") or LIVE_TTL_S)}
        self.store = ViewStore(self.compute_public, self.view_schedule, clock, open_db=self.db, children=self.view_children,
                               status_path=settings.logs_dir / VIEWS_FILE)
        # milestone 12: the Terminal's feed (one incremental computation per second, shared by every viewer)
        self.feed = terminal.TerminalFeed(settings, self.db, clock)
        self.terminal_cfg = terminal.terminal_config(settings)
        self.limiter = RateLimiter()
        self.limit_log = Throttle(RATE_WINDOW_S)
        self.static: dict[str, bytes] = {}
        for name in STATIC_TYPES:
            body = (STATIC_DIR / name).read_bytes()
            if name == "app.css":
                body += theme.accent_override(str(self.site.get("accent") or "")).encode("utf-8")
            self.static[name] = body
        self.static_etags = {name: etag_of(body) for name, body in self.static.items()}

    # ----- helpers ----------------------------------------------------------------------------------------------------
    def db(self):
        """A read-only connection, or None when the database does not exist yet."""
        try:
            return data.open_db(self.settings.db_path)
        except FileNotFoundError:
            return None

    def session(self, req: Request, now: float) -> dict[str, Any] | None:
        sess = self.signer.verify_session(req.cookies.get(auth.SESSION_COOKIE), now)
        if sess is None or self.revoked.is_revoked(sess["nonce"], now):
            return None
        return sess

    def footer(self, now: float) -> str:
        return (f"regels {pages.esc(data.rules_fingerprint()[:19])} (versie {pages.esc(data.rules_version())}) · alleen-lezen · "
                f"pagina gemaakt {pages.esc(fmt_time(now))}")

    def ctx(self, sess: dict[str, Any] | None, now: float) -> dict[str, Any]:
        return {"site": self.site, "public": bool(self.settings.web_public), "logged_in": sess is not None,
                "logout_csrf": self.signer.logout_token(sess["nonce"]) if sess else "", "footer": self.footer(now),
                "banner": public.banner_text(self.settings), "warning": public.warning_text(self.settings),
                "refresh_label": f"Ververst elke {int(self.site.get('refresh_s', 30))} s zolang dit tabblad open staat."}

    def page(self, title: str, body: str, sess: dict[str, Any] | None, now: float, active: str, *, script: bool = True, status: int = 200,
             robots: str | None = None, cacheable: bool = False, req: Request | None = None, refresh_s: int | None = None, computing: bool = False,
             terminal_script: bool = False) -> Response:
        html = pages.layout(self.ctx(sess, now), title, body, active=active, script=script, robots=robots, refresh_s=refresh_s, terminal_script=terminal_script)
        resp = html_response(status, html)
        if cacheable and sess is None and status == 200:
            self.public_headers(resp, req, max_age=COMPUTING_MAX_AGE_S if computing else refresh_s)
        return resp

    def public_headers(self, resp: Response, req: Request | None, max_age: int | None = None) -> Response:
        """ETag plus a short public cache on an anonymous public answer; 304 when the client already has it."""
        tag = etag_of(resp.body)
        resp.headers += [("Cache-Control", f"public, max-age={int(max_age)}" if max_age else PUBLIC_CACHE_CONTROL), ("ETag", tag), ("Vary", "Cookie")]
        if req is not None and _etag_matches(req.headers.get("if-none-match"), tag):
            resp.status = 304
            resp.body = b""
        return resp

    def login_response(self, now: float, status: int = 200, error: str | None = None, retry_after: float | None = None) -> Response:
        csrf = self.signer.csrf_token(now)
        html = pages.layout(self.ctx(None, now), "Inloggen", pages.login_page(self.ctx(None, now), csrf, error), robots="noindex, nofollow", script=False)
        headers = [("Retry-After", str(int(math.ceil(retry_after))))] if retry_after else []
        return html_response(status, html, cookies=[auth.set_cookie(auth.CSRF_COOKIE, csrf, auth.CSRF_TTL_S, path="/login")], headers=headers)

    def error_response(self, status: int, message: str, sess: dict[str, Any] | None = None, now: float | None = None) -> Response:
        t = self.clock() if now is None else now
        try:
            return self.page("Fout", pages.error_page(status, message), sess, t, "", script=False, status=status, robots="noindex, nofollow")
        except Exception:  # noqa: BLE001  the error page must never fail
            return Response(status, f"{status} {message}".encode("utf-8"), "text/plain; charset=utf-8")

    # ----- routing ----------------------------------------------------------------------------------------------------
    def handle(self, req: Request) -> Response:
        now = self.clock()
        path = req.path
        if req.method not in ("GET", "HEAD", "POST"):
            return Response(405, b"method not allowed", "text/plain; charset=utf-8", headers=[("Allow", "GET, HEAD, POST")])
        if path.startswith("/static/"):
            return self.static_file(path[len("/static/"):], req)
        if path == "/login":
            if req.method == "POST":
                return self.login_post(req, now)
            if self.session(req, now) is not None:
                return redirect(OWNER_PATH)
            return self.login_response(now)
        sess = self.session(req, now)
        if path == "/logout":
            if req.method != "POST":
                return Response(405, b"method not allowed", "text/plain; charset=utf-8", headers=[("Allow", "POST")])
            if sess is None:
                return self.login_response(now, status=401)
            if not self.signer.verify_logout(req.form().get("csrf"), sess["nonce"]):
                return self.page("Uitloggen", pages.error_page(403, "Het uitlogformulier was ongeldig; gebruik de knop bovenaan."), sess, now, "", status=403, script=False)
            self.revoked.revoke(sess["nonce"], sess["expires"])
            log.info("web.logout", ip=req.ip)
            return redirect("/" if self.settings.web_public else "/login", cookies=[auth.clear_cookie(auth.SESSION_COOKIE)])
        is_public_route = self.is_public_route(path)
        if sess is None and not (self.settings.web_public and is_public_route):
            if self.settings.web_public and not self.is_owner_route(path):
                return self.page("Niet gevonden", pages.error_page(404, "Deze pagina bestaat niet."), None, now, "", status=404, script=False, robots="noindex, nofollow")
            if path.startswith("/api/"):
                return json_response(401, {"error": "login required", "login": "/login"})
            return self.login_response(now, status=401)
        if req.method == "POST":
            return self.page("Niet toegestaan", pages.error_page(405, "Deze site is alleen-lezen: er is niets om te versturen."), sess, now, "", status=405, script=False)
        if sess is None and is_public_route:
            wait = self.limiter.check(req.ip, now)
            if wait is not None:
                if self.limit_log.allow(req.ip, now):
                    log.warning("web.rate_limited", ip=req.ip, path=path[:80], retry_after_s=round(wait))
                headers = [("Retry-After", str(int(math.ceil(wait))))]
                if path.startswith("/api/"):
                    return json_response(429, {"error": "too many requests", "retry_after_s": int(math.ceil(wait))}, headers=headers)
                r = self.page("Even wachten", pages.error_page(429, "Te veel verzoeken van dit adres; probeer het over een minuut opnieuw."), None, now, "", status=429, script=False)
                r.headers += headers
                return r
        if path == self.STREAM_PATH:
            return self.stream_response(req)
        if path == self.SNAPSHOT_PATH:
            return self.snapshot_response(req, sess)
        if path.startswith("/api/public/"):
            return self.public_api(path, req, now, sess)
        if path.startswith("/api/"):
            return self.owner_api(path, now)
        if path == "/":
            v = self.public_views(now, self.DASHBOARD_VIEWS)
            return self.page("Dashboard", pages.dashboard(self.ctx(sess, now), v), sess, now, "/", cacheable=True, req=req,
                             refresh_s=self.site["live_refresh_s"], computing=_any_computing(v))
        if path == "/meting":
            v = self.public_views(now, ("week", "coverage", "reports"))
            v["warning"] = public.warning_text(self.settings)
            return self.page("Meting", pages.measurement_page(self.ctx(sess, now), v), sess, now, "/meting", cacheable=True, req=req, computing=_any_computing(v))
        if path == "/observaties":
            v = self.public_view("observations", now)
            return self.page("Observaties", pages.observations_page(self.ctx(sess, now), v), sess, now, "/observaties", cacheable=True, req=req, computing=public.is_computing(v))
        if path == "/rapporten":
            v = self.public_view("reports", now)
            return self.page("Rapporten", pages.reports_page(self.ctx(sess, now), v), sess, now, "/rapporten", cacheable=True, req=req, computing=public.is_computing(v))
        if path.startswith("/rapporten/"):
            return self.report_page(path[len("/rapporten/"):], req, sess, now)
        if path == "/methode":
            v = self.public_view("method", now)
            return self.page("Methode", pages.method_page(self.ctx(sess, now), v), sess, now, "/methode", script=False, cacheable=True, req=req, computing=public.is_computing(v))
        if path == "/tools":
            return self.page("Tools", pages.tools_page(self.ctx(sess, now)), sess, now, "/tools", script=False, cacheable=True, req=req)
        if path == "/terminal":
            v = self.public_view("plays", now)
            return self.page("Terminal", pages.terminal_page(self.ctx(sess, now), self.terminal_cfg, v), sess, now, "/terminal", script=False, cacheable=True, req=req,
                             terminal_script=True)
        if path == "/plays":
            v = self.public_view("plays", now)
            return self.page("Plays", pages.plays_page(self.ctx(sess, now), v), sess, now, "/plays", cacheable=True, req=req, computing=public.is_computing(v))
        if path == "/instellingen":
            return redirect(OWNER_PATH + "#instellingen")
        if path == OWNER_PATH:
            return self.page("Eigenaar", pages.owner_page(self.ctx(sess, now), self.owner_views(now)), sess, now, OWNER_PATH, robots="noindex, nofollow")
        return self.page("Niet gevonden", pages.error_page(404, "Deze pagina bestaat niet."), sess, now, "", status=404, script=False, robots="noindex, nofollow")

    def is_public_route(self, path: str) -> bool:
        if path in self.PUBLIC_PAGES or path.startswith("/api/public/"):
            return True
        if path.startswith("/rapporten/"):
            return not path.endswith(".md")   # the raw Markdown stays with the owner
        return False

    def is_owner_route(self, path: str) -> bool:
        """The routes that exist only behind the login (401 for a visitor); anything else unknown is 404."""
        return path in (OWNER_PATH, "/instellingen") or path.startswith("/api/") or (path.startswith("/rapporten/") and path.endswith(".md"))

    # ----- the public side (precomputed by the store's thread; a request only reads) -----------------------------------
    def view_schedule(self) -> list[tuple[str, float]]:
        """The scheduled views in computation order with their TTL: the live views first (the dashboard), then the rest."""
        live = float(self.site["live_refresh_s"])
        return [(n, live) for n in self.LIVE_VIEWS] + [(n, float(CACHE_TTL_S)) for n in self.REST_VIEWS]

    def view_children(self, key: str, value: Any) -> list[str]:
        """The report pages ride along with the reports list (same cycle, same TTL): one key per report file."""
        if key == "reports" and isinstance(value, dict):
            return [f"report:{r['name']}" for r in value.get("rows") or [] if REPORT_PAGE.match(str(r.get("name") or ""))]
        return []

    def refresh_views(self, force: bool = False) -> list[str]:
        """Compute the due views now, in this thread (the tests; the thread does the same every second)."""
        return self.store.refresh(force=force)

    def start_views(self) -> None:
        self.store.start()
        self.feed.start()

    def stop_views(self, timeout: float = VIEWS_STOP_S) -> bool:
        ok = self.store.stop(timeout)
        return self.feed.stop(timeout) and ok

    # ----- milestone 12: the Terminal's stream and snapshot --------------------------------------------------------------
    def stream_response(self, req: Request) -> Response:
        """`/api/public/stream`: subscribe the viewer to the feed; the socket layer writes the events as they come."""
        follow = terminal.parse_follow(req.query.get("follow"))
        last_raw = req.headers.get("last-event-id") or req.query.get("last")
        try:
            last_id = int(last_raw) if last_raw not in (None, "") else None
        except ValueError:
            last_id = None
        got = self.feed.subscribe(follow, last_id)
        if got is None:
            return json_response(503, {"error": "too many live connections", "retry_after_s": 30}, headers=[("Retry-After", "30")])
        sub, initial = got
        feed = self.feed

        def events():
            idle = 0.0
            try:
                yield b"retry: 3000\n\n"
                for ev in initial:
                    yield ev.encode()
                while not feed._stop.is_set():
                    ev = sub.get(timeout=STREAM_WAIT_S)
                    if sub.dropped:
                        yield b"event: dropped\ndata: {\"reason\": \"client too slow\"}\n\n"
                        return
                    if ev is None:
                        idle += STREAM_WAIT_S
                        if idle >= feed.heartbeat_s + 5.0:
                            idle = 0.0
                            yield b": keep-alive\n\n"
                        else:
                            yield b""          # nothing to send: the socket layer checks whether the client is still there
                        continue
                    idle = 0.0
                    yield ev.encode()
            finally:
                feed.unsubscribe(sub)

        return StreamResponse(200, b"", "text/event-stream; charset=utf-8", headers=list(SSE_HEADERS), close=True, events=events(), on_close=lambda: feed.unsubscribe(sub))

    def snapshot_response(self, req: Request, sess: dict[str, Any] | None) -> Response:
        snap = self.feed.snapshot()
        if snap is None:
            resp = json_response(200, {**public.computing("terminal"), "rows": [], "n": 0})
            if sess is None:
                self.public_headers(resp, req, max_age=COMPUTING_MAX_AGE_S)
            return resp
        resp = json_response(200, snap)
        if sess is None:
            self.public_headers(resp, req, max_age=1)
        return resp

    def feed_status(self) -> dict[str, Any]:
        f = self.feed
        return {"running": f.running, "cycles": f.cycles, "errors": f.errors, "viewers": f.connections, "coins": len(f.coins), "last_cycle_s": round(f.last_cycle_s, 3),
                "max_connections": f.max_connections, "heartbeat_s": f.heartbeat_s}

    def public_view(self, name: str, now: float, arg: str | None = None) -> Any:
        """The last value of a view, at once; the placeholder "wordt berekend" while it was never computed; None for a
        report that does not exist. A day filter of the observations is an on-demand key the thread computes next."""
        key = f"{name}:{arg}" if arg else name
        if name == "report":
            entry = self.store.get(key)
            if entry is not None and entry.value is not None:
                return entry.value
            if entry is not None and entry.ok:
                return None            # computed and found nothing: the file is unreadable, not "still computing"
            return public.computing(name) if public.report_exists(self.settings, arg or "") else None
        entry = self.store.request(key) if arg else self.store.get(key)
        if entry is None or entry.value is None:
            v = public.computing(name)
            if entry is not None and entry.errors:
                v["note"] = "De berekening van dit onderdeel mislukte; de volgende poging volgt binnen een halve minuut (het logboek zegt wat)."
            return v
        return entry.value

    def public_views(self, now: float, names: tuple[str, ...]) -> dict[str, Any]:
        return {n: self.public_view(n, now) for n in names}

    def compute_public(self, key: str, conn: Any) -> Any:
        """One view from scratch, on the store's connection (None: no database yet). Called by the store's thread."""
        now = self.clock()
        name, _, arg = key.partition(":")
        if name == "method":
            return public.method_view(self.settings)
        if name == "reports":
            return public.reports_view(self.settings, now)
        if name == "week":
            return public.week_view(self.settings, now)
        if name == "report":
            return public.week_report_view(self.settings, arg, now) if arg.startswith("week-") else public.day_report_view(self.settings, arg, now)
        if name == "status":
            return public.status_view(conn, self.settings, now)
        if name == "coverage":
            return public.coverage_view(conn, now)
        if name == "today":
            return public.today_view(conn, self.settings, now)
        if name == "observations":
            return public.observations_view(conn, self.settings, now, day=arg or None)
        if name == "live":
            return public.live_view(conn, self.settings, now)
        if name == "hot":
            return public.hot_coins_view(conn, self.settings, now)
        if name == "new":
            return public.new_coins_view(conn, self.settings, now)
        if name == "themes":
            return public.themes_view(conn, self.settings, now)
        if name == "graduations":
            return public.graduations_view(conn, self.settings, now)
        if name == "social":
            return public.social_view(conn, self.settings, now)
        if name == "plays":
            return public.plays_view(conn, self.settings, now)
        raise KeyError(key)

    def public_api(self, path: str, req: Request, now: float, sess: dict[str, Any] | None) -> Response:
        if path.startswith("/api/public/reports/"):
            name = path[len("/api/public/reports/"):]
            if not REPORT_PAGE.match(name):
                return json_response(404, {"error": "unknown report"})
            v = self.public_view("report", now, name)
            if v is None:
                return json_response(404, {"error": "unknown report"})
            return self.public_json(v, req, sess)
        name = self.PUBLIC_API.get(path)
        if name is None:
            return json_response(404, {"error": "unknown endpoint", "endpoints": sorted(self.PUBLIC_API) + ["/api/public/reports/<day>", "/api/public/reports/week-<day>"]})
        arg = None
        if name == "observations":
            day = req.query.get("day")
            if day:
                if not DAY_RE.match(day):
                    return json_response(400, {"error": "day must be YYYY-MM-DD"})
                arg = day
        return self.public_json(self.public_view(name, now, arg), req, sess, live=name in self.LIVE_VIEWS)

    def public_json(self, obj: Any, req: Request, sess: dict[str, Any] | None, live: bool = False) -> Response:
        resp = json_response(200, obj)
        if sess is None:
            self.public_headers(resp, req, max_age=COMPUTING_MAX_AGE_S if public.is_computing(obj) else (self.site["live_refresh_s"] if live else None))
        return resp

    def report_page(self, name: str, req: Request, sess: dict[str, Any] | None, now: float) -> Response:
        if name.endswith(".md"):
            # the raw report for the owner (the guard above already required a session)
            text = data.read_report(self.settings, name)
            if text is None:
                return self.page("Niet gevonden", pages.error_page(404, "Dat rapport bestaat niet."), sess, now, "/rapporten", script=False, status=404, robots="noindex, nofollow")
            body = pages.raw_report_page(self.ctx(sess, now), markdown.render(text), name, f"/rapporten/{name[:-3]}")
            return self.page(name, body, sess, now, "/rapporten", script=False, robots="noindex, nofollow")
        if not REPORT_PAGE.match(name):
            return self.page("Niet gevonden", pages.error_page(404, "Dat rapport bestaat niet."), sess, now, "/rapporten", script=False, status=404, robots="noindex, nofollow")
        v = self.public_view("report", now, name)
        if v is None:
            return self.page("Niet gevonden", pages.error_page(404, "Dat rapport bestaat niet."), sess, now, "/rapporten", script=False, status=404, robots="noindex, nofollow")
        if public.is_computing(v):
            return self.page("Rapport", pages.computing_page("Rapport", name), sess, now, "/rapporten", script=False, cacheable=True, req=req, computing=True)
        if v["kind"] == "week":
            body = pages.week_report_page(self.ctx(sess, now), v)
            title = f"Weekrapport {v['window_label']}"
        else:
            body = pages.day_report_page(self.ctx(sess, now), v)
            title = f"Dagrapport {v['day_label']}"
        return self.page(title, body, sess, now, "/rapporten", script=False, cacheable=True, req=req)

    # ----- the owner side (never cached, never without a session) -------------------------------------------------------
    def owner_views(self, now: float) -> dict[str, Any]:
        conn = self.db()
        try:
            return {"status": data.status_view(conn, self.settings, now), "coverage": data.coverage_view(conn, now), "x": data.x_view(conn, self.settings, now),
                    "report": data.report_latest_view(self.settings, now), "week": data.week_latest_view(self.settings, now), "settings": data.settings_view(self.settings),
                    "logs": data.logs_view(self.settings, now), "report_files": data.report_files(self.settings), "views": self.views_status(now)}
        finally:
            if conn is not None:
                conn.close()

    def views_status(self, now: float) -> dict[str, Any]:
        """The background computation of the public views, for /eigenaar and /api/status (milestone 10); the Terminal feed (milestone 12)."""
        rows = self.store.stats(now)
        return {"rows": rows, "running": self.store.running, "computes": self.store.computes, "failures": self.store.failures, "slow_s": SLOW_VIEW_S,
                "live_ttl_s": self.site["live_refresh_s"], "ttl_s": CACHE_TTL_S, "file": VIEWS_FILE, "terminal": self.feed_status()}

    def views(self, now: float, names: tuple[str, ...]) -> dict[str, Any]:
        conn = self.db()
        try:
            out: dict[str, Any] = {}
            for name in names:
                out[name] = self.view(name, conn, now)
            return out
        finally:
            if conn is not None:
                conn.close()

    def view(self, name: str, conn, now: float) -> dict[str, Any]:
        if name == "status":
            return {**data.status_view(conn, self.settings, now), "web_views": self.views_status(now)}
        if name == "coverage":
            return data.coverage_view(conn, now)
        if name == "decisions":
            return data.decisions_view(conn, self.settings, now)
        if name == "x":
            return data.x_view(conn, self.settings, now)
        if name == "report":
            return data.report_latest_view(self.settings, now)
        if name == "week":
            return data.week_latest_view(self.settings, now)
        raise KeyError(name)

    def owner_api(self, path: str, now: float) -> Response:
        name = self.OWNER_API.get(path)
        if name is None:
            return json_response(404, {"error": "unknown endpoint", "endpoints": sorted(self.OWNER_API) + sorted(self.PUBLIC_API)})
        return json_response(200, self.views(now, (name,))[name])

    def static_file(self, name: str, req: Request | None = None) -> Response:
        if name not in self.static:
            return Response(404, b"not found", "text/plain; charset=utf-8")
        tag = self.static_etags[name]
        headers = [("Cache-Control", "public, max-age=300"), ("ETag", tag)]
        if req is not None and _etag_matches(req.headers.get("if-none-match"), tag):
            return Response(304, b"", STATIC_TYPES[name], headers=headers)
        return Response(200, self.static[name], STATIC_TYPES[name], headers=headers)

    # ----- login --------------------------------------------------------------------------------------------------------
    def login_post(self, req: Request, now: float) -> Response:
        form = req.form()
        csrf_cookie = req.cookies.get(auth.CSRF_COOKIE, "")
        csrf_form = form.get("csrf", "")
        if not csrf_cookie or not csrf_form or not hmac.compare_digest(csrf_cookie, csrf_form) or not self.signer.verify_csrf(csrf_form, now):
            log.warning("web.login_csrf_failed", ip=req.ip)
            return self.login_response(now, status=403, error="Het formulier is verlopen of ongeldig. Probeer het opnieuw.")
        wait = self.guard.wait_s(req.ip, now)
        if wait is not None:
            log.warning("web.login_locked", ip=req.ip, wait_s=round(wait))
            return self.login_response(now, status=429, error=f"Te veel mislukte pogingen. Wacht {max(1, math.ceil(wait / 60))} minuten.", retry_after=wait)
        if auth.verify_password(form.get("password", ""), self.settings.web_password_hash):
            self.guard.record_success(req.ip)
            token = self.signer.session_token(now)
            log.info("web.login_ok", ip=req.ip)
            return redirect(OWNER_PATH, cookies=[auth.set_cookie(auth.SESSION_COOKIE, token, auth.SESSION_TTL_S), auth.clear_cookie(auth.CSRF_COOKIE, path="/login")])
        locked = self.guard.record_failure(req.ip, now)
        remaining = self.guard.remaining(req.ip, now)
        log.warning(auth.LOG_EVENT_FAILED, ip=req.ip, remaining=remaining, locked_s=None if locked is None else round(locked))
        if locked is not None:
            return self.login_response(now, status=429, error=f"Onjuist wachtwoord. Te veel mislukte pogingen: wacht {math.ceil(locked / 60)} minuten.", retry_after=locked)
        return self.login_response(now, status=401, error=f"Onjuist wachtwoord ({remaining} {'poging' if remaining == 1 else 'pogingen'} over in dit venster).")


OWNER_PATH = pages.OWNER_PATH


def _any_computing(views: dict[str, Any]) -> bool:
    return any(public.is_computing(v) for v in views.values())


def _etag_matches(header: str | None, tag: str) -> bool:
    if not header:
        return False
    for part in header.split(","):
        p = part.strip()
        if p.startswith("W/"):
            p = p[2:]
        if p == "*" or p == tag:
            return True
    return False


# ----- the socket layer ----------------------------------------------------------------------------------------------------
class Handler(BaseHTTPRequestHandler):
    app: App  # set by make_server
    protocol_version = "HTTP/1.1"
    timeout = SOCKET_TIMEOUT_S

    def version_string(self) -> str:  # no Python or module version on the wire
        return "fart"

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002  the request log is written in _dispatch
        pass

    def log_error(self, format: str, *args: Any) -> None:  # noqa: A002
        log.warning("web.http_error", detail=(format % args)[:200])

    def send_error(self, code: int, message: str | None = None, explain: str | None = None) -> None:
        """Malformed requests the base class refuses (a line too long, a bad request line) get our headers too."""
        self.close_connection = True
        body = f"{code} {message or ''}".strip().encode("utf-8")
        self._write(Response(int(code), body, "text/plain; charset=utf-8", close=True), head=(getattr(self, "command", "") == "HEAD"))

    def do_GET(self) -> None:
        self._dispatch(head=False)

    def do_HEAD(self) -> None:
        self._dispatch(head=True)

    def do_POST(self) -> None:
        self._dispatch(head=False)

    def _not_allowed(self) -> None:
        self._write(Response(405, b"method not allowed", "text/plain; charset=utf-8", headers=[("Allow", "GET, HEAD, POST")], close=True), head=False)

    do_PUT = do_DELETE = do_PATCH = do_OPTIONS = do_TRACE = do_CONNECT = _not_allowed

    def _client_ip(self) -> str:
        peer = str(self.client_address[0])
        if peer in LOOPBACK:
            xff = self.headers.get("X-Forwarded-For")
            if xff:
                last = xff.split(",")[-1].strip()
                if last:
                    return last[:64]
        return peer

    def _request(self) -> Request | Response:
        header_bytes = len(self.raw_requestline) + sum(len(k) + len(v) + 4 for k, v in self.headers.items())
        if header_bytes > MAX_REQUEST_BYTES:
            return Response(431, b"request header fields too large", "text/plain; charset=utf-8", close=True)
        length_raw = self.headers.get("Content-Length")
        try:
            length = int(length_raw) if length_raw else 0
        except ValueError:
            return Response(400, b"bad content-length", "text/plain; charset=utf-8", close=True)
        if length < 0:
            return Response(400, b"bad content-length", "text/plain; charset=utf-8", close=True)
        if self.headers.get("Transfer-Encoding"):
            return Response(411, b"length required", "text/plain; charset=utf-8", close=True)
        if header_bytes + length > MAX_REQUEST_BYTES:
            return Response(413, b"request too large (64 KB)", "text/plain; charset=utf-8", close=True)
        body = b""
        if length:
            if self.command != "POST":
                return Response(400, b"no body allowed", "text/plain; charset=utf-8", close=True)
            body = self.rfile.read(length)
        parts = urllib.parse.urlsplit(self.path)
        path = urllib.parse.unquote(parts.path or "/")
        if "\x00" in path or ".." in path.split("/"):
            return Response(400, b"bad path", "text/plain; charset=utf-8", close=True)
        query = {k: v[0] for k, v in urllib.parse.parse_qs(parts.query, keep_blank_values=True, max_num_fields=20).items() if v}
        headers = {k.lower(): v for k, v in self.headers.items()}
        return Request(self.command, path, query, headers, body, self._client_ip())

    def _dispatch(self, head: bool) -> None:
        t0 = time.monotonic()
        ip = str(self.client_address[0])
        path = urllib.parse.urlsplit(self.path).path[:200]
        req: Request | Response | None = None
        try:
            req = self._request()
            resp = req if isinstance(req, Response) else self.app.handle(req)
            if isinstance(req, Request):
                ip = req.ip
        except Exception:  # noqa: BLE001  never leak a traceback to the client; the log has it (without the request)
            log.error("web.request_failed", exc_info=True, method=self.command, path=path)
            resp = self.app.error_response(500, "Er ging iets mis bij het maken van deze pagina. Het log zegt wat.")
        self._write(resp, head)
        log.info("web.request", method=self.command, path=path, status=resp.status, ms=round((time.monotonic() - t0) * 1000), ip=ip)

    def _write(self, resp: Response, head: bool) -> None:
        if isinstance(resp, StreamResponse):
            self._stream(resp, head)
            return
        if resp.close:
            self.close_connection = True
        try:
            self.send_response(resp.status)
            for k, v in resp.all_headers():
                self.send_header(k, v)
            self.send_header("Content-Length", str(len(resp.body)))
            if self.close_connection:
                self.send_header("Connection", "close")
            self.end_headers()
            if not head and resp.body:
                self.wfile.write(resp.body)
        except (BrokenPipeError, ConnectionResetError, socket.timeout):
            self.close_connection = True


    def _stream(self, resp: StreamResponse, head: bool) -> None:
        """Milestone 12: a server-sent-events answer: the headers, then every event as its own chunk (HTTP/1.1 chunked
        transfer, flushed at once), until the generator ends or the client goes away."""
        self.close_connection = True
        try:
            self.send_response(resp.status)
            for k, v in resp.all_headers():
                if k.lower() in ("content-length",):
                    continue
                self.send_header(k, v)
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            self.wfile.flush()
            if head or resp.events is None:
                self.wfile.write(b"0\r\n\r\n")
                return
            for chunk in resp.events:
                if not chunk:
                    if self._client_gone():
                        break
                    continue
                self.wfile.write(f"{len(chunk):x}\r\n".encode("ascii") + chunk + b"\r\n")
                self.wfile.flush()
            self.wfile.write(b"0\r\n\r\n")
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, socket.timeout, OSError):
            pass
        finally:
            try:
                close = getattr(resp.events, "close", None)
                if close is not None:
                    close()
            except Exception:  # noqa: BLE001
                pass
            if resp.on_close is not None:
                try:
                    resp.on_close()
                except Exception:  # noqa: BLE001
                    pass


    def _client_gone(self) -> bool:
        """A streaming client sends nothing after its request: a readable socket means it closed (EOF) or broke."""
        try:
            r, _, _ = select.select([self.connection], [], [], 0)
            if not r:
                return False
            data = self.connection.recv(1, socket.MSG_PEEK)
            return data == b""
        except (OSError, ValueError):
            return True


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def parse_bind(value: str) -> tuple[str, int]:
    """'127.0.0.1:8792' or '[::1]:8792' -> (host, port)."""
    v = (value or "").strip()
    if v.startswith("["):
        host, _, port = v[1:].partition("]")
        port = port.lstrip(":")
    else:
        host, _, port = v.rpartition(":")
    if not host or not port.isdigit():
        raise ValueError(f"FART_WEB_BIND must be host:port, got {value!r}")
    return host, int(port)


def make_server(app: App, host: str, port: int) -> Server:
    cls = type("FartHandler", (Handler,), {"app": app})
    if ":" in host:
        Server.address_family = socket.AF_INET6
    return Server((host, port), cls)


def run_web(settings: Settings) -> int:
    problems = auth.check_config(settings.web_password_hash, settings.web_secret)
    if problems:
        for p in problems:
            print(f"fart web: {p}", file=sys.stderr)
        print("fart web: not started (both FART_WEB_PASSWORD_HASH and FART_WEB_SECRET are required in .env)", file=sys.stderr)
        log.error("web.config_invalid", problems=problems)
        return 2
    try:
        host, port = parse_bind(settings.web_bind)
    except ValueError as e:
        print(f"fart web: {e}", file=sys.stderr)
        log.error("web.config_invalid", problems=[str(e)])
        return 2
    app = App(settings)
    replayed = app.guard.load_from_log(settings.logs_dir / "web.log")
    try:
        server = make_server(app, host, port)
    except OSError as e:
        print(f"fart web: cannot listen on {host}:{port}: {e}", file=sys.stderr)
        log.error("web.listen_failed", bind=f"{host}:{port}", error=str(e)[:120])
        return 1
    stop = threading.Event()

    def _stop(signum, frame) -> None:  # noqa: ANN001
        stop.set()

    previous = (signal.signal(signal.SIGTERM, _stop), signal.signal(signal.SIGINT, _stop))
    bound = f"{server.server_address[0]}:{server.server_address[1]}"
    log.info("web.started", bind=bound, db=str(settings.db_path), readonly=True, login_failures_replayed=replayed, public=settings.web_public,
             site=str(app.site.get("site_name")), cache_ttl_s=CACHE_TTL_S, live_ttl_s=app.site["live_refresh_s"], rate_limit_per_min=RATE_LIMIT,
             session_ttl_s=auth.SESSION_TTL_S, max_request_bytes=MAX_REQUEST_BYTES, views_file=str(settings.logs_dir / VIEWS_FILE))
    app.start_views()   # milestone 10: the public views are computed here, from now on, never in a request
    t = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.5}, name="fart-web", daemon=True)
    t.start()
    try:
        while not stop.is_set():
            stop.wait(1.0)
    finally:
        server.shutdown()
        server.server_close()
        views_stopped = app.stop_views()
        signal.signal(signal.SIGTERM, previous[0] or signal.SIG_DFL)
        signal.signal(signal.SIGINT, previous[1] or signal.SIG_DFL)
        log.info("web.stopped", views_thread_stopped=views_stopped)
    return 0


# ----- the two helper commands --------------------------------------------------------------------------------------------
MIN_PASSWORD_CHARS = 12


def cmd_hash_password() -> int:
    """Ask for the password invisibly, twice; print only the hash (for FART_WEB_PASSWORD_HASH in .env)."""
    import getpass

    try:
        pw = getpass.getpass("Wachtwoord voor de website (onzichtbaar): ")
        pw2 = getpass.getpass("Nog een keer: ")
    except (EOFError, KeyboardInterrupt):
        print("fart web hash-password: cancelled, nothing printed", file=sys.stderr)
        return 2
    if pw != pw2:
        print("fart web hash-password: the two entries differ; nothing printed", file=sys.stderr)
        return 2
    if len(pw) < MIN_PASSWORD_CHARS:
        print(f"fart web hash-password: use at least {MIN_PASSWORD_CHARS} characters; nothing printed", file=sys.stderr)
        return 2
    print(auth.hash_password(pw))
    return 0


def cmd_new_secret() -> int:
    print(auth.new_secret())
    return 0
