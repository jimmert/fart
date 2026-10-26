"""`fart web`: the HTTP server of the website (milestone 8).

`App.handle()` turns a `Request` into a `Response` without touching sockets (the tests call it directly and through a
live server); `Handler` adapts `http.server.BaseHTTPRequestHandler` to it and enforces what only the socket layer can:
the request-size limit (64 KB for the request line, the headers and the body together: 431 or 413, the connection
closed without reading the body), a socket timeout, HTTP/1.1 with an explicit Content-Length on every response.

Every response carries the security headers: a strict Content-Security-Policy (`default-src 'self'`, no inline
scripts or styles: the two files under /static/ are the only code and style), `X-Content-Type-Options: nosniff`,
`Referrer-Policy: no-referrer`, `X-Frame-Options: DENY` and `Cache-Control: no-store` (static files: a short cache).

Routes: `/login` (GET the form, POST the password with the CSRF token), `/logout` (POST), `/` (dashboard),
`/observaties`, `/rapporten`, `/rapporten/<file>.md`, `/instellingen`, `/api/status`, `/api/coverage`, `/api/decisions`,
`/api/x`, `/api/report/latest`, `/api/week/latest`, `/static/app.css`, `/static/app.js`. Without a valid session every
route but `/login` and the two static files answers 401: the login page for a page, `{"error": ...}` for `/api/*`.

The log (`logs/web.log` and the journal) carries the method, the path without its query, the status, the duration
and the client IP (the last `X-Forwarded-For` entry, which the shared Caddy appends, when the peer is the loopback);
never a cookie, a password, a token or a request body. Failed logins are logged as `web.login_failed` with the IP so
the login guard can replay them after a restart (`fart/web/auth.py`).
"""
from __future__ import annotations

import hmac
import math
import signal
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
from ..log import get_logger
from ..util import now as wall_now
from . import auth, data, markdown, pages

log = get_logger("web")

MAX_REQUEST_BYTES = 64 * 1024
SOCKET_TIMEOUT_S = 30
STATIC_DIR = Path(__file__).resolve().parent / "static"
STATIC_TYPES = {"app.css": "text/css; charset=utf-8", "app.js": "text/javascript; charset=utf-8"}
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


def html_response(status: int, body: str, **kw: Any) -> Response:
    return Response(status, body.encode("utf-8"), "text/html; charset=utf-8", **kw)


def json_response(status: int, obj: Any, **kw: Any) -> Response:
    import json

    return Response(status, json.dumps(obj, ensure_ascii=False, default=str).encode("utf-8"), "application/json; charset=utf-8", **kw)


def redirect(location: str, cookies: list[str] | None = None) -> Response:
    return Response(303, b"", "text/plain; charset=utf-8", headers=[("Location", location)], cookies=cookies or [])


class App:
    """The routes, the session check and the page assembly; one read-only database connection per request."""

    def __init__(self, settings: Settings, clock: Callable[[], float] = wall_now):
        problems = auth.check_config(settings.web_password_hash, settings.web_secret)
        if problems:
            raise auth.WebConfigError("; ".join(problems))
        self.settings = settings
        self.clock = clock
        self.signer = auth.Signer(settings.web_secret)
        self.guard = auth.LoginGuard(clock)
        self.revoked = auth.Revocations()
        self.static = {name: (STATIC_DIR / name).read_bytes() for name in STATIC_TYPES}

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
        return (f"rules_version {pages.esc(data.rules_version())} · vingerafdruk regels {pages.esc(data.rules_fingerprint()[:19])} · "
                f"pagina gemaakt {pages.esc(data.fmt_time(now))} · alleen-lezen")

    def page(self, title: str, body: str, sess: dict[str, Any], now: float, active: str, script: bool = True, status: int = 200) -> Response:
        html = pages.layout(title, body, active=active, logged_in=True, logout_csrf=self.signer.logout_token(sess["nonce"]),
                            footer=self.footer(now), script=script)
        return html_response(status, html)

    def login_response(self, now: float, status: int = 200, error: str | None = None, retry_after: float | None = None) -> Response:
        csrf = self.signer.csrf_token(now)
        html = pages.layout("Inloggen", pages.login_page(csrf, error), footer=self.footer(now))
        headers = [("Retry-After", str(int(math.ceil(retry_after))))] if retry_after else []
        return html_response(status, html, cookies=[auth.set_cookie(auth.CSRF_COOKIE, csrf, auth.CSRF_TTL_S, path="/login")], headers=headers)

    # ----- routing ----------------------------------------------------------------------------------------------------
    def handle(self, req: Request) -> Response:
        now = self.clock()
        path = req.path
        if req.method not in ("GET", "HEAD", "POST"):
            return Response(405, b"method not allowed", "text/plain; charset=utf-8", headers=[("Allow", "GET, HEAD, POST")])
        if path.startswith("/static/"):
            return self.static_file(path[len("/static/"):])
        if path == "/login":
            if req.method == "POST":
                return self.login_post(req, now)
            if self.session(req, now) is not None:
                return redirect("/")
            return self.login_response(now)
        sess = self.session(req, now)
        if path == "/logout":
            if req.method != "POST":
                return Response(405, b"method not allowed", "text/plain; charset=utf-8", headers=[("Allow", "POST")])
            if sess is None:
                return self.login_response(now, status=401)
            if not self.signer.verify_logout(req.form().get("csrf"), sess["nonce"]):
                return self.page("Uitloggen", pages.error_page(403, "Het uitlogformulier was ongeldig; gebruik de knop bovenaan."), sess, now, "", status=403)
            self.revoked.revoke(sess["nonce"], sess["expires"])
            log.info("web.logout", ip=req.ip)
            return redirect("/login", cookies=[auth.clear_cookie(auth.SESSION_COOKIE)])
        if sess is None:
            if path.startswith("/api/"):
                return json_response(401, {"error": "login required", "login": "/login"})
            return self.login_response(now, status=401)
        if req.method == "POST":
            return self.page("Niet toegestaan", pages.error_page(405, "Deze site is alleen-lezen: er is niets om te versturen."), sess, now, "", status=405)
        if path.startswith("/api/"):
            return self.api(path, now)
        if path == "/":
            return self.page("Dashboard", pages.dashboard(self.views(now, ("status", "coverage", "x", "report", "week"))), sess, now, "/")
        if path == "/observaties":
            return self.page("Observaties", pages.observations_page(self.views(now, ("decisions",))), sess, now, "/observaties")
        if path == "/rapporten":
            return self.page("Rapporten", pages.reports_page(data.report_files(self.settings)), sess, now, "/rapporten", script=False)
        if path.startswith("/rapporten/"):
            name = path[len("/rapporten/"):]
            text = data.read_report(self.settings, name)
            if text is None:
                return self.page("Niet gevonden", pages.error_page(404, "Dat rapport bestaat niet."), sess, now, "/rapporten", script=False, status=404)
            body = pages.reports_page(data.report_files(self.settings), markdown.render(text), name)
            return self.page(name, body, sess, now, "/rapporten", script=False)
        if path == "/instellingen":
            return self.page("Instellingen", pages.settings_page(data.settings_view(self.settings)), sess, now, "/instellingen", script=False)
        return self.page("Niet gevonden", pages.error_page(404, "Deze pagina bestaat niet."), sess, now, "", script=False, status=404)

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
            return data.status_view(conn, self.settings, now)
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

    API = {"/api/status": "status", "/api/coverage": "coverage", "/api/decisions": "decisions", "/api/x": "x",
           "/api/report/latest": "report", "/api/week/latest": "week"}

    def api(self, path: str, now: float) -> Response:
        name = self.API.get(path)
        if name is None:
            return json_response(404, {"error": "unknown endpoint", "endpoints": sorted(self.API)})
        return json_response(200, self.views(now, (name,))[name])

    def static_file(self, name: str) -> Response:
        if name not in self.static:
            return Response(404, b"not found", "text/plain; charset=utf-8")
        return Response(200, self.static[name], STATIC_TYPES[name], headers=[("Cache-Control", "public, max-age=300")])

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
            return redirect("/", cookies=[auth.set_cookie(auth.SESSION_COOKIE, token, auth.SESSION_TTL_S), auth.clear_cookie(auth.CSRF_COOKIE, path="/login")])
        locked = self.guard.record_failure(req.ip, now)
        remaining = self.guard.remaining(req.ip, now)
        log.warning(auth.LOG_EVENT_FAILED, ip=req.ip, remaining=remaining, locked_s=None if locked is None else round(locked))
        if locked is not None:
            return self.login_response(now, status=429, error=f"Onjuist wachtwoord. Te veel mislukte pogingen: wacht {math.ceil(locked / 60)} minuten.", retry_after=locked)
        return self.login_response(now, status=401, error=f"Onjuist wachtwoord ({remaining} {'poging' if remaining == 1 else 'pogingen'} over in dit venster).")


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
            resp = html_response(500, pages.layout("Fout", pages.error_page(500, "Er ging iets mis bij het maken van deze pagina. Het log zegt wat.")))
        self._write(resp, head)
        log.info("web.request", method=self.command, path=path, status=resp.status, ms=round((time.monotonic() - t0) * 1000), ip=ip)

    def _write(self, resp: Response, head: bool) -> None:
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

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    log.info("web.started", bind=f"{host}:{port}", db=str(settings.db_path), readonly=True, login_failures_replayed=replayed,
             session_ttl_s=auth.SESSION_TTL_S, max_request_bytes=MAX_REQUEST_BYTES)
    t = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.5}, name="fart-web", daemon=True)
    t.start()
    try:
        while not stop.is_set():
            stop.wait(1.0)
    finally:
        server.shutdown()
        server.server_close()
        log.info("web.stopped")
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
