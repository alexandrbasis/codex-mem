"""Persistent local credentials and browser access after server restarts."""
from concurrent.futures import ThreadPoolExecutor
import http.client
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from codex_mem.dashboard import DashboardServerError, TOKEN_FILENAME, _persistent_token, make_server


class DashboardAuthenticationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)

    def test_concurrent_starts_share_one_private_token_without_store(self):
        with patch("codex_mem.store.Store", side_effect=AssertionError("no Store writes")), ThreadPoolExecutor(max_workers=4) as pool:
            tokens = list(pool.map(lambda _: _persistent_token(self.base), range(8)))
        self.assertEqual(1, len(set(tokens)))
        self.assertEqual(43, len(tokens[0]))
        self.assertEqual(0o600, (self.base / TOKEN_FILENAME).stat().st_mode & 0o777)
        self.assertEqual([TOKEN_FILENAME], sorted(p.name for p in self.base.iterdir()))

    def test_symlink_and_hardlink_targets_are_unchanged(self):
        target = self.base / "unrelated"
        target.write_text("private existing value")
        target.chmod(0o644)
        path = self.base / TOKEN_FILENAME
        for link in (lambda: path.symlink_to(target), lambda: os.link(target, path)):
            link()
            with self.assertRaises(DashboardServerError):
                _persistent_token(self.base)
            self.assertEqual("private existing value", target.read_text())
            self.assertEqual(0o644, target.stat().st_mode & 0o777)
            path.unlink()

    def test_corrupt_token_is_preserved(self):
        path = self.base / TOKEN_FILENAME
        path.write_text("malformed credential")
        with self.assertRaises(DashboardServerError):
            _persistent_token(self.base)
        self.assertEqual("malformed credential", path.read_text())

    def test_cookie_still_authenticates_after_restart_on_same_port(self):
        class Reader:
            def overview(self, **kwargs):
                return {"status": "ok"}
        port = 0
        cookie = None
        for _ in range(2):
            with make_server(port=port, reader=Reader(), token=_persistent_token(self.base)) as server:
                port = server.server_port
                thread = threading.Thread(target=server.serve_forever, daemon=True)
                thread.start()
                try:
                    client = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
                    if cookie is None:
                        client.request("GET", "/?token=" + server.token)
                        response = client.getresponse()
                        self.assertEqual(303, response.status)
                        cookie = response.getheader("Set-Cookie").split(";", 1)[0]
                        response.read()
                        client.close()
                        client = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
                    client.request("GET", "/api/overview", headers={"Cookie": cookie})
                    response = client.getresponse()
                    self.assertEqual(200, response.status)
                    response.read()
                    client.close()
                    with self.assertRaises(DashboardServerError):
                        make_server(port=port, reader=Reader())
                finally:
                    server.shutdown()
                    thread.join(2)
