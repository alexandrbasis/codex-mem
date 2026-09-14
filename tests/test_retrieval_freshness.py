"""Freshness survives empty retrieval and the compact transport contracts."""
import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import xml.etree.ElementTree as ET

from codex_mem.cli import main
from codex_mem.config import configure
from codex_mem.hooks import handle_hook
from codex_mem.mcp import MemoryMCPServer
from codex_mem.store import Store


BLOCKED = {
    "status": "blocked", "knowledge_incomplete": True,
    "latest_capture_at": "2026-09-14T09:00:00Z",
    "last_successful_processing_at": "2026-09-12T08:00:00Z",
    "last_ready_at": "2026-09-12T08:01:00Z",
    "pending_capture_count": 849, "ready_count": 399,
    "blocked_reason": "runner_failure", "index": {"indexed": 399, "pending": 0, "stale": 0},
    "summary": "Memory blocked; knowledge incomplete. Pending captures: 849; "
               "last successful processing: 2026-09-12T08:00:00Z; last ready note: 2026-09-12T08:01:00Z; "
               "latest capture: 2026-09-14T09:00:00Z; reason: runner_failure; "
               "index: 399 indexed, 0 pending. Index coverage does not establish memory freshness.",
}


class FreshnessDeliveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.project = self.root / "project"
        self.data = self.root / "data"
        configure(self.data, capture_scope="all", semantic_enabled=False)
        self.store = Store(self.data)
        self.addCleanup(self.store.close)
        self.telemetry = patch("codex_mem.freshness.freshness_snapshot", return_value=BLOCKED)
        self.telemetry.start()
        self.addCleanup(self.telemetry.stop)

    def test_mcp_empty_search_keeps_list_and_exposes_visible_freshness(self):
        server = MemoryMCPServer(store=self.store)
        result = server._call_tool({"name": "memory_search", "arguments": {
            "project": str(self.project), "query": "release 1.8.0",
        }})
        self.assertEqual([], json.loads(result["content"][0]["text"]))
        self.assertEqual({"result": []}, result["structuredContent"])
        self.assertEqual(BLOCKED, result["_meta"]["codexMemRetrieval"]["freshness"])
        self.assertIn("runner_failure", result["content"][1]["text"])

    def test_cli_legacy_list_and_extended_json_both_deliver_freshness(self):
        for extra in ([], ["--mode", "lexical"]):
            stdout, stderr = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                code = main(["--data-dir", str(self.data), "search", "--project", str(self.project),
                             "--query", "missing", *extra])
            self.assertEqual(0, code, stdout.getvalue())
            value = json.loads(stdout.getvalue())
            if extra:
                self.assertEqual([], value["results"])
                self.assertEqual(BLOCKED, value["freshness"])
            else:
                self.assertEqual([], value)
                self.assertEqual(BLOCKED, json.loads(stderr.getvalue())["freshness"])

    def test_context_empty_or_stale_always_warns_and_remains_bounded(self):
        for query in ("missing", ""):
            self.store.remember(self.project, "Release 1.5.0", "Historical release evidence")
            context = self.store.context(self.project, query=query)
            self.assertIn("849", ET.fromstring(context).find("freshness").text)
            self.assertIn("runner_failure", context)
        for budget in (128, 180, 256, 380, 600, 1200, 6000):
            context = self.store.context(self.project, budget=budget)
            self.assertLessEqual(len(context), budget)
            self.assertIn("blocked", ET.fromstring(context).find("freshness").text)

    def test_session_start_empty_memory_injects_full_warning(self):
        response = handle_hook({"hook_event_name": "SessionStart", "cwd": str(self.project),
                                "session_id": "new-session", "source": "startup"}, store=self.store)
        text = response["hookSpecificOutput"]["additionalContext"]
        self.assertIn("runner_failure", text)
        self.assertIn("849", text)
        self.assertIn("2026-09-12T08:00:00Z", text)
        self.assertLessEqual(len(text), 6000)

    def test_session_start_tiny_budget_explicitly_reports_missing_context(self):
        configure(self.data, context_chars=256)
        response = handle_hook({"hook_event_name": "SessionStart", "cwd": str(self.project),
                                "session_id": "tiny-session", "source": "startup"}, store=self.store)
        text = response["hookSpecificOutput"]["additionalContext"]
        self.assertIn("freshness unknown", text)
        self.assertIn("omitted by budget", text)
        self.assertLessEqual(len(text), 256)
