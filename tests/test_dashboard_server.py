"""HTTP boundary tests for the read-only local dashboard."""
import http.client
import json
from pathlib import Path
import tempfile
import threading
import unittest
from urllib.parse import urlsplit

from codex_mem.dashboard import DashboardServerError, make_server


class Reader:
    def __init__(self):
        self.calls = []

    def overview(self, **kwargs):
        self.calls.append(("overview", kwargs))
        return {"status": "ok"}

    def projects(self, **kwargs):
        self.calls.append(("projects", kwargs))
        return {"projects": []}

    project = projects
    session = projects


class DashboardHTTPTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.assets = Path(self.temp.name) / "assets"
        self.assets.mkdir()
        for name in ("index.html", "app.js", "styles.css"):
            (self.assets / name).write_text("static " + name)
        self.reader = Reader()
        self.server = make_server(port=0, reader=self.reader, static_dir=self.assets)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.temp.cleanup)
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.thread.join, 2)
        self.addCleanup(self.server.shutdown)

    def request(self, path, *, method="GET", headers=None, authenticated=True):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=3)
        headers = dict(headers or {})
        if authenticated:
            headers.setdefault("X-Codex-Mem-Token", self.server.token)
        connection.request(method, path, headers=headers)
        response = connection.getresponse()
        result = response.status, dict(response.getheaders()), response.read()
        connection.close()
        return result

    def test_token_required_and_browser_cookie_bootstrap(self):
        self.assertEqual(401, self.request("/api/overview", authenticated=False)[0])
        path = urlsplit(self.server.url)
        status, headers, _ = self.request(path.path + "?" + path.query, authenticated=False)
        self.assertEqual(303, status)
        self.assertEqual("/", headers["Location"])
        self.assertIn("HttpOnly", headers["Set-Cookie"])
        self.assertIn("SameSite=Strict", headers["Set-Cookie"])
        cookie = headers["Set-Cookie"].split(";", 1)[0]
        self.assertEqual(200, self.request("/api/overview", headers={"Cookie": cookie}, authenticated=False)[0])
        self.assertEqual(401, self.request("/?token=wrong", authenticated=False)[0])
        self.assertEqual(401, self.request("/?token=%E2%98%83", authenticated=False)[0])

    def test_host_and_origin_refused(self):
        port = self.server.server_port
        for headers in ({"Host": "attacker.example"}, {"Host": f"127.0.0.1:{port}.evil"},
                        {"Origin": "http://attacker.example"}, {"Origin": "null"},
                        {"Origin": f"http://localhost:{port}"}, {"Sec-Fetch-Site": "cross-site"}):
            with self.subTest(headers=headers):
                self.assertEqual(403, self.request("/api/overview", headers=headers)[0])
        self.assertEqual(200, self.request("/api/overview", headers={"Origin": f"http://127.0.0.1:{port}"})[0])

    def test_static_map_and_security_headers(self):
        for path in ("/", "/app.js", "/styles.css"):
            status, headers, body = self.request(path)
            self.assertEqual(200, status)
            self.assertTrue(body.startswith(b"static "))
            self.assertEqual("no-store", headers["Cache-Control"])
            self.assertEqual("no-referrer", headers["Referrer-Policy"])
            self.assertIn("frame-ancestors 'none'", headers["Content-Security-Policy"])
            self.assertNotIn("Access-Control-Allow-Origin", headers)
        for path in ("/../config.json", "/%2e%2e/config.json", "/assets/index.html", "/api/start", "/api/refresh"):
            self.assertEqual(404, self.request(path)[0])

    def test_plugin_assets_serve_english_ui_without_a_frontend_build(self):
        import codex_mem.dashboard as dashboard
        self.server.static_dir = Path(dashboard.__file__).parent / "ui_static"
        status, _, page = self.request("/")
        self.assertEqual(200, status)
        self.assertIn(b'lang="en"', page)
        self.assertIn(b"Overview", page)
        self.assertIn(b"Projects", page)
        status, _, script = self.request("/app.js")
        self.assertEqual(200, status)
        self.assertIn(b"en-US", script)
        self.assertNotRegex((page + script).decode("utf-8"), r"[\u0400-\u04ff]")
        self.assertEqual(200, self.request("/styles.css")[0])

    def test_filters_are_bounded_and_validated(self):
        bad = ("/api/projects?period=year", "/api/projects?page=0", "/api/projects?limit=101",
               "/api/projects?page=-1", "/api/projects?page=1&page=2", "/api/projects?other=x",
               "/api/project", "/api/session", "/api/session?session=%00", "/api/overview?limit=5")
        for path in bad:
            with self.subTest(path=path):
                self.assertEqual(400, self.request(path)[0])
        self.assertEqual([], self.reader.calls)
        self.assertEqual(200, self.request("/api/projects?period=7d&page=2&limit=5")[0])
        self.assertEqual(("projects", {"period": "7d", "page": 2, "limit": 5}), self.reader.calls[-1])

    def test_mutating_methods_are_refused(self):
        for method in ("POST", "PUT", "PATCH", "DELETE", "OPTIONS"):
            self.assertEqual(405, self.request("/api/overview", method=method)[0])
        self.assertEqual([], self.reader.calls)

    def test_data_failure_does_not_leak_exception(self):
        def fail(**kwargs):
            raise RuntimeError("secret private /path token")
        self.reader.overview = fail
        status, _, body = self.request("/api/overview")
        self.assertEqual(503, status)
        self.assertNotIn(b"secret", body)
        self.assertEqual("data_unavailable", json.loads(body)["error"]["code"])

    def test_loopback_binding_and_address_in_use_error(self):
        self.assertEqual("127.0.0.1", self.server.server_address[0])
        with self.assertRaises(DashboardServerError):
            make_server(port=self.server.server_port, reader=self.reader)
        for value in (-1, 65536, True, "8765"):
            with self.assertRaises(DashboardServerError):
                make_server(port=value, reader=self.reader)

    def test_missing_assets_clear_error(self):
        (self.assets / "app.js").unlink()
        status, _, body = self.request("/app.js")
        self.assertEqual(503, status)
        self.assertEqual("assets_unavailable", json.loads(body)["error"]["code"])

    def test_real_reader_missing_home_creates_no_files(self):
        from codex_mem.dashboard_data import DashboardReader
        missing = Path(self.temp.name) / "missing-home"
        self.server.reader = DashboardReader(data_dir=missing)
        status, _, body = self.request("/api/overview")
        self.assertEqual(200, status)
        self.assertEqual("unavailable", json.loads(body)["status"])
        self.assertFalse(missing.exists())

    def test_session_reader_contract(self):
        self.assertEqual(200, self.request("/api/session?project=%2Fproject&session_id=task&period=today")[0])
        self.assertEqual({"project": "/project", "session_id": "task", "period": "today"}, self.reader.calls[-1][1])
        self.assertEqual(400, self.request("/api/session?project=x&session=a&session_id=b")[0])

    def test_corrupt_data_is_read_only(self):
        from codex_mem.dashboard_data import DashboardReader
        home = Path(self.temp.name) / "corrupt-home"
        home.mkdir()
        database = home / "memory.sqlite3"
        database.write_bytes(b"invalid sqlite data")
        self.server.reader = DashboardReader(data_dir=home)
        before = {path.name: path.read_bytes() for path in home.iterdir()}
        status, _, body = self.request("/api/overview")
        self.assertEqual(200, status)
        self.assertEqual("unavailable", json.loads(body)["status"])
        self.assertEqual(before, {path.name: path.read_bytes() for path in home.iterdir()})


if __name__ == "__main__":
    unittest.main()
