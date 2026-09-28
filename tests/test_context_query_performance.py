"""Bounded context reads preserve linked event chronology on busy sessions."""

from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import xml.etree.ElementTree as ET

from codex_mem.config import configure
from codex_mem.store import Store


class ContextQueryPerformanceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.project = Path(self.temp.name) / "project"
        data = Path(self.temp.name) / "data"
        configure(data, semantic_enabled=False, service_enabled=False, processor_enabled=False)
        self.store = Store(data)
        self.addCleanup(self.store.close)

    def test_hook_mode_only_changes_freshness_source(self):
        entry = self.store.remember(self.project, "Open recovery", "The recovery still needs a check.")
        with patch("codex_mem.freshness.freshness_snapshot", return_value={"status": "full"}) as full, \
             patch("codex_mem.freshness.hook_freshness_snapshot", return_value={"status": "hook"}) as hook:
            regular = self.store.context(self.project, budget=1000)
            hooked = self.store.context(self.project, budget=1000, hook_mode=True)
        self.assertIn(entry["id"], regular)
        self.assertIn(entry["id"], hooked)
        self.assertEqual([item.attrib["id"] for item in ET.fromstring(regular).findall("entry")],
                         [item.attrib["id"] for item in ET.fromstring(hooked).findall("entry")])
        full.assert_called_once()
        hook.assert_called_once()

    def test_large_shared_session_uses_linked_events_without_quadratic_scan(self):
        session = "busy-session"
        raw = [self.store.remember(self.project, f"Private event {index}", "Private source text",
                                   source="hook:PostToolUse", session_id=session)
               for index in range(500)]
        for index, event in enumerate(raw):
            self.store._connection.execute(
                "UPDATE entries SET created_at=? WHERE id=?",
                (f"2026-09-{index // 24 + 1:02d}T{index % 24:02d}:00:00Z", event["id"]),
            )
        first = last = None
        for index in range(200):
            linked = raw[index * 2]
            note = self.store.remember(
                self.project, f"Recovery summary {index}", "Recovery is still being checked.",
                kind="session_summary", source="processor:test", session_id=session,
                source_ids=[linked["id"]],
                session_summary={"request": "Recovery", "next_steps": "Check the recovery."},
            )
            if index == 0:
                first = note
            if index == 199:
                last = note
        assert first is not None and last is not None
        # The first summary is written last. Its linked event still makes it
        # earlier than the later summary and cannot displace that handoff.
        self.store._connection.execute(
            "UPDATE entries SET created_at='2030-01-01T00:00:00Z' WHERE id=?", (first["id"],)
        )
        operations = 0

        def progress():
            nonlocal operations
            operations += 1
            return 0

        self.store._connection.set_progress_handler(progress, 1_000)
        try:
            metadata = self.store.resume_metadata(self.project, [first["id"], last["id"]])
            with patch("codex_mem.freshness.freshness_snapshot", return_value={"status": "unknown"}):
                context = self.store.context(self.project, budget=2000, allow_remote=False)
        finally:
            self.store._connection.set_progress_handler(None, 0)
        self.assertEqual(last["id"], metadata[first["id"]]["later_summary_id"])
        self.assertTrue(metadata[first["id"]]["context_historical"])
        self.assertIn(last["id"], context)
        self.assertNotIn("Private source text", context)
        # Deterministic SQLite VM work is more stable than a wall-clock limit.
        # A broad raw-session join would multiply 200 summaries by 500 events.
        self.assertLess(operations, 2_000)


if __name__ == "__main__":
    unittest.main()
