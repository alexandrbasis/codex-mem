"""Dashboard CLI wiring without starting a listener or touching memory."""
from contextlib import redirect_stdout
import io
import json
import unittest
from unittest.mock import patch

from codex_mem.cli import main
from codex_mem.dashboard import DashboardServerError


class DashboardCLITests(unittest.TestCase):
    def test_ui_forwards_options_without_opening_the_store(self):
        with patch("codex_mem.dashboard.run_dashboard", return_value=0) as run, \
                patch("codex_mem.cli.Store", side_effect=AssertionError("must be read only")):
            self.assertEqual(0, main(["ui", "--data-dir", "/missing", "--port", "9876", "--open"]))
        run.assert_called_once_with("/missing", port=9876, open_browser=True)

    def test_bind_failure_has_actionable_cli_error(self):
        output = io.StringIO()
        with patch("codex_mem.dashboard.run_dashboard", side_effect=DashboardServerError("Port is in use")), \
                redirect_stdout(output):
            self.assertEqual(2, main(["ui"]))
        self.assertEqual("dashboard_unavailable", json.loads(output.getvalue())["error"]["code"])

    def test_invalid_ports_are_rejected_before_start(self):
        for value in ("0", "-1", "65536", "oops"):
            with self.subTest(port=value), patch("codex_mem.dashboard.run_dashboard") as run, redirect_stdout(io.StringIO()):
                self.assertEqual(2, main(["ui", "--port", value]))
                run.assert_not_called()
