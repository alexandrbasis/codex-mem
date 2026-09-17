"""Source chronology and continuation links stay scoped and read-only."""
import json
from pathlib import Path
import tempfile
import unittest

from codex_mem.config import configure
from codex_mem.mcp import MemoryMCPServer
from codex_mem.retrieval import preview_records
from codex_mem.store import Store


class ResumeMetadataTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.project = Path(self.temp.name) / "project"
        self.data = Path(self.temp.name) / "data"
        configure(self.data, semantic_enabled=False, service_enabled=False, processor_enabled=False)
        self.store = Store(self.data)
        self.addCleanup(self.store.close)

    def entry(self, title, at, *, session="release", kind="session_summary", source="manual", sources=()):
        row = self.store.remember(self.project, title, title, session_id=session, kind=kind,
                                  source=source, source_ids=list(sources))
        self.store._connection.execute("UPDATE entries SET created_at=? WHERE id=?", (at, row["id"]))
        return row

    def test_delayed_summary_uses_stop_time_and_points_to_later_outcome(self):
        earlier = self.entry("Release delegated", "2026-09-14T09:00:00Z", kind="session", source="hook:Stop")
        later = self.entry("Release completed", "2026-09-14T10:00:00Z", kind="session", source="hook:Stop")
        new = self.entry("Release completed", "2026-09-14T10:01:00Z", sources=[later["id"]])
        delayed = self.entry("Release delegated", "2026-09-16T10:00:00Z", sources=[earlier["id"]])
        metadata = self.store.resume_metadata(self.project, [delayed["id"], new["id"]])
        self.assertEqual("2026-09-14T09:00:00Z", metadata[delayed["id"]]["event_at"])
        self.assertEqual("source_event", metadata[delayed["id"]]["event_time_basis"])
        self.assertTrue(metadata[delayed["id"]]["context_historical"])
        self.assertEqual(new["id"], metadata[delayed["id"]]["later_summary_id"])
        self.assertNotIn("later_summary_id", metadata[new["id"]])

    def test_stop_is_preferred_to_later_tool_and_other_sessions_cannot_supply_time(self):
        stop = self.entry("Finished", "2026-09-14T09:00:00Z", kind="session", source="hook:Stop")
        tool = self.entry("Read later", "2026-09-14T10:00:00Z", kind="tool", source="hook:PostToolUse")
        foreign = self.entry("Other session", "2026-09-17T09:00:00Z", session="other", kind="session", source="hook:Stop")
        summary = self.entry("Release", "2026-09-16T09:00:00Z", sources=[stop["id"], tool["id"], foreign["id"]])
        row = self.store.resume_metadata(self.project, [summary["id"]])[summary["id"]]
        self.assertEqual(stop["id"], row["event_id"])
        self.assertEqual("2026-09-14T09:00:00Z", row["event_at"])

    def test_old_finding_links_to_followup_without_claiming_it_is_superseded(self):
        note = self.entry("Release needs verification", "2026-09-14T09:00:00Z", kind="note")
        latest = self.entry("Release verified", "2026-09-14T10:00:00Z")
        row = self.store.resume_metadata(self.project, [note["id"]])[note["id"]]
        self.assertEqual(latest["id"], row["later_summary_id"])
        self.assertNotIn("context_historical", row)
        self.assertEqual("recorded_at", row["event_time_basis"])
        self.assertIsNone(self.store.get(self.project, [note["id"]])[0]["superseded_by"])

    def test_scope_raw_exclusion_and_no_session_coalescing(self):
        old = self.entry("Independent one", "2026-09-14T09:00:00Z", session=None)
        self.entry("Independent two", "2026-09-14T10:00:00Z", session=None)
        raw = self.entry("Raw", "2026-09-14T11:00:00Z", kind="session", source="hook:Stop")
        foreign = self.store.remember(self.project / "other", "Foreign", "Private", kind="session_summary")
        self.store._connection.execute("PRAGMA query_only=ON")
        before = self.store._connection.total_changes
        rows = self.store.resume_metadata(self.project, [old["id"], raw["id"], foreign["id"]])
        self.assertEqual({old["id"]}, set(rows))
        self.assertNotIn("later_summary_id", rows[old["id"]])
        self.assertEqual(before, self.store._connection.total_changes)
        self.assertEqual({}, self.store.resume_metadata(self.project, []))

    def test_compact_preview_delivers_history_and_event_basis(self):
        row = {"id": "a" * 32, "title": "Earlier", "event_at": "2026-09-14T09:00:00Z",
               "event_id": "c" * 32, "event_time_basis": "source_event",
               "context_historical": True, "later_summary_id": "b" * 32}
        preview = preview_records([row])[0]
        for key in ("event_at", "event_time_basis", "context_historical", "later_summary_id"):
            self.assertEqual(row[key], preview[key])

    def test_mcp_historical_topic_exposes_followup_without_leaking_other_topic(self):
        old = self.entry("OAuth remains blocked", "2026-09-14T09:00:00Z")
        current = self.entry("Billing was reviewed", "2026-09-14T10:00:00Z")
        result = MemoryMCPServer(store=self.store)._call_tool({"name": "memory_search", "arguments": {
            "project": str(self.project), "query": "OAuth", "mode": "lexical", "intent": "resume"}})
        records = json.loads(result["content"][0]["text"])
        self.assertEqual([old["id"]], [r["id"] for r in records])
        self.assertTrue(records[0]["context_historical"])
        self.assertEqual(current["id"], records[0]["later_summary_id"])
        self.assertNotIn("Billing", result["content"][0]["text"])
