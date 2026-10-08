"""Read-only dashboard HTTP server, restricted to the local loopback address."""
from __future__ import annotations

import hmac
from http.cookies import SimpleCookie, CookieError
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import secrets
from urllib.parse import parse_qs, urlencode, urlsplit
import webbrowser

STATIC_FILES = {"/": ("index.html", "text/html; charset=utf-8"),
                "/app.js": ("app.js", "text/javascript; charset=utf-8"),
                "/styles.css": ("styles.css", "text/css; charset=utf-8")}
MAX_RESPONSE_BYTES = 4 * 1024 * 1024
COOKIE_NAME = "codex_mem_token"
PERIODS = {"all", "today", "7d", "30d"}


def _token_matches(candidate, expected):
    return hmac.compare_digest(candidate.encode("utf-8"), expected.encode("utf-8"))


class DashboardServerError(RuntimeError):
    """The local dashboard could not start."""


class _RequestError(Exception):
    def __init__(self, status: int, code: str, message: str):
        self.status, self.code, self.message = status, code, message


class DashboardServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server_port}/?{urlencode({'token': self.token})}"

    def handle_error(self, request, client_address):
        # Request exceptions must never print private paths, tokens, or data.
        pass


class _Handler(BaseHTTPRequestHandler):
    server_version = "CodexMemDashboard"
    sys_version = ""

    def log_message(self, format, *args):
        # The bootstrap query contains a credential. Do not log request URLs.
        pass

    def _headers(self, status, content_type="application/json; charset=utf-8", length=0, extra=None):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(length))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Content-Security-Policy", "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; font-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'")
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()

    def _json(self, status, value):
        body = json.dumps(value, ensure_ascii=True, allow_nan=False, separators=(",", ":")).encode("utf-8")
        if len(body) > MAX_RESPONSE_BYTES:
            raise _RequestError(413, "response_too_large", "The result is too large. Use a smaller page.")
        self._headers(status, length=len(body))
        self.wfile.write(body)

    def _validate_request(self):
        hosts = self.headers.get_all("Host", [])
        allowed = {f"127.0.0.1:{self.server.server_port}", f"localhost:{self.server.server_port}"}
        if len(hosts) != 1 or hosts[0] not in allowed:
            raise _RequestError(403, "invalid_host", "This dashboard accepts local requests only.")
        origins = self.headers.get_all("Origin", [])
        if origins and (len(origins) != 1 or origins[0] != f"http://{hosts[0]}"):
            raise _RequestError(403, "invalid_origin", "The request must come from this dashboard.")
        if self.headers.get("Sec-Fetch-Site", "") == "cross-site":
            raise _RequestError(403, "invalid_origin", "The request must come from this dashboard.")
        if len(self.path) > 8192:
            raise _RequestError(400, "invalid_query", "The request URL is too long.")
        parsed = urlsplit(self.path)
        if parsed.scheme or parsed.netloc or parsed.fragment:
            raise _RequestError(400, "invalid_query", "Use a local dashboard path.")
        try:
            # Python 3.10 strict parsing rejects even an empty query.
            query = parse_qs(parsed.query, keep_blank_values=True, max_num_fields=8, strict_parsing=True) if parsed.query else {}
        except ValueError:
            raise _RequestError(400, "invalid_query", "The query is invalid.") from None
        if any(len(values) != 1 for values in query.values()):
            raise _RequestError(400, "invalid_query", "Each query parameter must occur once.")
        return parsed.path, {key: value[0] for key, value in query.items()}

    def _authenticated(self):
        tokens = self.headers.get_all("X-Codex-Mem-Token", [])
        if len(tokens) == 1 and _token_matches(tokens[0], self.server.token):
            return True
        try:
            cookie = SimpleCookie(self.headers.get("Cookie", ""))
            token = cookie.get(COOKIE_NAME)
            return token is not None and _token_matches(token.value, self.server.token)
        except (CookieError, TypeError):
            return False

    def do_GET(self):
        try:
            path, query = self._validate_request()
            if path == "/" and "token" in query:
                if set(query) != {"token"} or not _token_matches(query["token"], self.server.token):
                    raise _RequestError(401, "unauthorized", "Open the dashboard URL printed by codex-mem.")
                self._headers(303, extra={"Location": "/", "Set-Cookie": f"{COOKIE_NAME}={self.server.token}; HttpOnly; SameSite=Strict; Path=/"})
                return
            if not self._authenticated():
                raise _RequestError(401, "unauthorized", "Open the dashboard URL printed by codex-mem.")
            if path in STATIC_FILES:
                if query:
                    raise _RequestError(400, "invalid_query", "Static files do not accept query parameters.")
                name, content_type = STATIC_FILES[path]
                try:
                    body = (self.server.static_dir / name).read_bytes()
                except OSError:
                    raise _RequestError(503, "assets_unavailable", "Dashboard files are unavailable. Reinstall codex-mem.") from None
                if len(body) > MAX_RESPONSE_BYTES:
                    raise _RequestError(503, "assets_unavailable", "Dashboard files are unavailable.")
                self._headers(200, content_type, len(body))
                self.wfile.write(body)
                return
            self._api(path, query)
        except _RequestError as exc:
            self._json(exc.status, {"error": {"code": exc.code, "message": exc.message}})
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception:
            self._json(503, {"error": {"code": "data_unavailable", "message": "Dashboard data is unavailable or invalid."}})

    def _api(self, path, query):
        routes = {"/api/overview": "overview", "/api/projects": "projects", "/api/project": "project", "/api/session": "session"}
        if path not in routes:
            raise _RequestError(404, "not_found", "The dashboard route does not exist.")
        allowed = {"period"}
        if path in {"/api/projects", "/api/project"}:
            allowed |= {"page", "limit"}
        if path == "/api/projects":
            allowed.add("query")
        if path in {"/api/project", "/api/session"}:
            allowed.add("project")
        if path == "/api/session":
            allowed |= {"session", "session_id"}
        if set(query) - allowed:
            raise _RequestError(400, "invalid_query", "The query contains an unsupported parameter.")
        period = query.get("period", "all")
        if period not in PERIODS:
            raise _RequestError(400, "invalid_period", "Period must be all, today, 7d, or 30d.")
        kwargs = {"period": period}
        if path in {"/api/projects", "/api/project"}:
            for key, default, maximum in (("page", 1, 100000), ("limit", 25, 100)):
                raw = query.get(key, str(default))
                if not raw.isascii() or not raw.isdigit() or len(raw) > 6 or not 1 <= int(raw) <= maximum:
                    raise _RequestError(400, "invalid_pagination", f"{key.capitalize()} must be between 1 and {maximum}.")
                kwargs[key] = int(raw)
        if "query" in query:
            if len(query["query"]) > 256 or any(ord(c) < 32 for c in query["query"]):
                raise _RequestError(400, "invalid_query", "The search query is invalid.")
            kwargs["query"] = query["query"]
        if "session" in query:
            if "session_id" in query:
                raise _RequestError(400, "invalid_query", "Use one session parameter.")
            query["session_id"] = query.pop("session")
        for key in ("project", "session_id"):
            if key in allowed and key in query:
                value = query[key]
                maximum = 4096 if key == "project" else 256
                if not value or len(value) > maximum or any(ord(c) < 32 or ord(c) == 127 for c in value):
                    raise _RequestError(400, "invalid_query", f"The {key} parameter is invalid.")
                kwargs[key] = value
        required = ("project", "session_id") if path == "/api/session" else ("project",) if path == "/api/project" else ()
        for key in required:
            if key not in kwargs:
                raise _RequestError(400, "invalid_query", f"The {key} parameter is required.")
        result = getattr(self.server.reader, routes[path])(**kwargs)
        self._json(200, result)

    def _reject_method(self):
        try:
            self._validate_request()
            self._json(405, {"error": {"code": "read_only", "message": "The dashboard accepts GET requests only."}})
        except _RequestError as exc:
            self._json(exc.status, {"error": {"code": exc.code, "message": exc.message}})

    do_POST = do_PUT = do_PATCH = do_DELETE = do_OPTIONS = do_HEAD = _reject_method


def make_server(data_dir=None, port=8765, *, reader=None, static_dir=None):
    """Create a server without starting it or writing dashboard data."""
    if isinstance(port, bool) or not isinstance(port, int) or not 0 <= port <= 65535:
        raise DashboardServerError("Dashboard port must be between 0 and 65535.")
    if reader is None:
        from .dashboard_data import DashboardReader
        reader = DashboardReader(data_dir=data_dir)
    try:
        server = DashboardServer(("127.0.0.1", port), _Handler)
    except OSError as exc:
        raise DashboardServerError(f"Cannot start the local dashboard on port {port}. The port may be in use.") from exc
    server.reader = reader
    server.token = secrets.token_urlsafe(32)
    server.static_dir = Path(static_dir) if static_dir is not None else Path(__file__).parent / "ui_static"
    return server


def run_dashboard(data_dir=None, port=8765, open_browser=False):
    """Run the local dashboard until Ctrl+C closes its listener."""
    with make_server(data_dir=data_dir, port=port) as server:
        print(f"Codex Mem dashboard: {server.url}", flush=True)
        print("Read-only local dashboard. Press Ctrl+C to stop.", flush=True)
        if open_browser:
            webbrowser.open(server.url)
        try:
            server.serve_forever(poll_interval=0.25)
        except KeyboardInterrupt:
            pass
    return 0
