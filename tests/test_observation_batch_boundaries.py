"""Whole retained evidence and known turn boundaries in observer claims."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from codex_mem.store import MAX_OBSERVATION_CHARS, Store, project_key
from codex_mem.tool_io import MAX_TOOL_PAYLOAD_BYTES, get_tool_capture_for_entry, normalize_capture


class ObservationBatchBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.project = Path(self.temp.name) / "project"
        self.project.mkdir()
        self.store = Store(Path(self.temp.name) / "memory")
        self.addCleanup(self.store.close)

    def claim(self, **kwargs):
        return self.store.claim_observation_batch(
            self.project, "boundary-tests", "gpt-6-luna", "medium", **kwargs
        )

    def event(self, turn=None, session="session", source="hook:PostToolUse"):
        return self.store.remember(
            self.project, "Event", "Complete evidence", session_id=session,
            turn_id=turn, source=source,
        )["id"]

    def test_combined_quote_heavy_captures_are_claimed_whole_without_double_encoding(self):
        input_value = {"text": '"' * 32_000 + "input-tail"}
        response_value = {"text": '"' * 32_000 + "response-tail"}
        capture = normalize_capture({
            "tool_name": "Bash", "session_id": "session", "tool_use_id": "large",
            "tool_input": input_value,
            "tool_response": response_value,
        }, project=str(self.project))
        self.assertIsNotNone(capture)
        entry = self.store.remember(
            self.project, "Large", "Short excerpt", source="hook:PostToolUse", tool_capture=capture
        )
        original = get_tool_capture_for_entry(
            self.store._connection, entry["id"], project_key(self.project)
        )
        for field in ("tool_input", "tool_response"):
            self.assertLessEqual(len(original[field].encode("utf-8")), MAX_TOOL_PAYLOAD_BYTES)
            self.assertGreater(len(original[field]), 60_000)
        batch = self.claim()
        self.assertEqual([source["id"] for source in batch["sources"]], [entry["id"]])
        source = batch["sources"][0]
        self.assertEqual(source["tool_io"]["tool_input"], input_value)
        self.assertEqual(source["tool_io"]["tool_response"], response_value)
        for field in ("tool_input", "tool_response"):
            self.assertEqual(source["tool_io"][field], json.loads(original[field]))
        self.assertLessEqual(self.store._observation_source_chars(source), MAX_OBSERVATION_CHARS)
        self.assertEqual(original, get_tool_capture_for_entry(
            self.store._connection, entry["id"], project_key(self.project)
        ))
        self.assertEqual(self.store.get(self.project, [entry["id"]])[0]["body"], "Short excerpt")

    def test_prompt_decoding_preserves_legacy_text_null_and_inert_values(self):
        for payload in (None, "not-json", '{broken', '"plain string"', 'null', '[1, false]',
                        '{"command":"$(touch /tmp/should-never-run)"}'):
            with self.subTest(payload=payload):
                hydrated = {"id": "source", "tool_io": {"tool_input": payload, "tool_response": None}}
                with mock.patch("codex_mem.tool_io.hydrate_source_tool_io", return_value=hydrated):
                    result = self.store._hydrate_observation_source(project_key(self.project), {"id": "source"})
                try:
                    expected = json.loads(payload) if isinstance(payload, str) else payload
                except ValueError:
                    expected = payload
                self.assertEqual(result["tool_io"]["tool_input"], expected)
                self.assertIsNone(result["tool_io"]["tool_response"])
                self.assertEqual(hydrated["tool_io"]["tool_input"], payload)

    def test_known_turn_change_without_stop_ends_batch(self):
        first = self.event("turn-a")
        self.event("turn-b")
        self.assertEqual([source["id"] for source in self.claim()["sources"]], [first])

    def test_unknown_turn_events_keep_legacy_grouping_but_do_not_bridge_known_turns(self):
        expected = [self.event(), self.event("turn-a"), self.event()]
        self.event("turn-b")
        self.assertEqual([source["id"] for source in self.claim()["sources"]], expected)

    def test_all_unknown_turns_end_at_first_stop(self):
        expected = [self.event(), self.event(source="hook:Stop")]
        self.event()
        self.assertEqual([source["id"] for source in self.claim()["sources"]], expected)

    def test_other_session_turn_does_not_end_selected_session(self):
        first = self.event("turn-a")
        self.event("turn-b", session="other")
        last = self.event("turn-a", source="hook:Stop")
        self.assertEqual([source["id"] for source in self.claim()["sources"]], [first, last])

    def test_expired_lease_retains_exact_sources_after_later_turn_arrives(self):
        first = self.event("turn-a")
        batch = self.claim()
        self.event("turn-b")
        self.assertIsNone(self.claim())
        self.store._connection.execute(
            "UPDATE observation_jobs SET lease_expires_at = '2000-01-01T00:00:00Z' WHERE id = ?",
            (batch["job_id"],),
        )
        retry = self.claim()
        self.assertEqual(retry["job_id"], batch["job_id"])
        self.assertEqual([source["id"] for source in retry["sources"]], [first])

    def test_failed_retry_retains_exact_sources_after_later_turn_arrives(self):
        first = self.event("turn-a")
        batch = self.claim()
        self.store.fail_observation_batch(self.project, batch["job_id"], batch["lease_token"])
        self.event("turn-b")
        retry = self.claim(retry_failed=True)
        self.assertEqual(retry["job_id"], batch["job_id"])
        self.assertEqual([source["id"] for source in retry["sources"]], [first])
