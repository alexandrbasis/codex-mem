"""Ordinary hook questions recall scoped notes without running a model."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import xml.etree.ElementTree as ET

from codex_mem.config import configure
from codex_mem.hooks import handle_hook
from codex_mem.store import Store


class PromptContextTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.project = self.root / "project"
        self.data = self.root / "data"
        configure(self.data, capture_scope="all", semantic_enabled=False,
                  service_enabled=False, processor_enabled=False)
        self.store = Store(self.data)
        self.addCleanup(self.store.close)

    def ids(self, query, **kwargs):
        context = self.store.context(self.project, query=query, budget=6000, **kwargs)
        return [entry.attrib["id"] for entry in ET.fromstring(context).findall("entry")]

    def test_audited_russian_question_and_request_find_the_real_store_note(self):
        note = self.store.remember(
            self.project, "Плагин Codex Mem: карантин памяти",
            "Плагин сохраняет наблюдения, но карантин задерживает обработку памяти.",
            session_id="previous-session", observation={"type": "discovery"},
        )
        for query in ("Как работает плагин памяти?",
                      "проанализируй как там наш плагин, как он справляется со своей задачей"):
            with self.subTest(query=query):
                self.assertEqual([note["id"]], self.ids(query))
        # Explicit lexical lookup remains an AND query.
        self.assertEqual([], self.store.search(self.project, "Как работает плагин памяти?"))

    def test_english_question_preserves_topic_and_filters(self):
        note = self.store.remember(
            self.project, "Checkout retry key", "The checkout retry key prevents duplicate charges.",
            session_id="previous", observation={"type": "bugfix", "concepts": ["payments"],
                                                 "files_modified": ["checkout.py"]},
        )
        self.store.remember(self.project, "Checkout retry key", "Discovery only.",
                            observation={"type": "discovery"})
        self.assertEqual([note["id"]], self.ids(
            "Can you explain how the checkout retry key works?", types="bugfix",
            files="checkout.py", concepts="payments"))

    def test_prompt_fallback_never_relaxes_versions_or_code_identifiers(self):
        expected = self.store.remember(self.project, "memory_search 1.9.0", "Lookup behavior.")
        for title in ("memory_searcher 1.9.0", "memory_search 1.8.0", "memory_search 1 9 0"):
            self.store.remember(self.project, title, "Lookup behavior.")
        self.assertEqual([expected["id"]], self.ids("How does memory_search work in 1.9.0?"))
        self.assertEqual([expected["id"]], self.ids("memory_search 1.9.0"))
        self.assertEqual([], self.ids("memory_search 1.9.1"))
        issue = self.store.remember(self.project, "AIAN-700", "Merchant diagnostic.")
        self.store.remember(self.project, "AIAN-701", "Merchant diagnostic.")
        self.assertEqual([issue["id"]], self.ids("What happened with AIAN-700?"))

    def test_real_hook_preserves_project_session_raw_and_superseded_exclusions(self):
        expected = self.store.remember(self.project, "Memory quarantine", "Review the quarantine queue.",
                                        session_id="previous")
        excluded = [
            self.store.remember(self.project, "Memory quarantine", "CURRENT_PRIVATE", session_id="current"),
            self.store.remember(self.project / "other", "Memory quarantine", "FOREIGN_PRIVATE"),
            self.store.remember(self.project, "Memory quarantine", "RAW_PRIVATE", source="hook:Stop"),
            self.store.remember(self.project, "Memory quarantine", "SUPERSEDED_PRIVATE"),
        ]
        self.store.remember(self.project, "Archived unrelated", "Already consolidated.",
                            source_ids=[excluded[-1]["id"]])
        with patch("codex_mem.semantic._backend", side_effect=AssertionError("no hook model")):
            response = handle_hook({"hook_event_name": "UserPromptSubmit", "cwd": str(self.project),
                                    "session_id": "current", "turn_id": "test-turn",
                                    "prompt": "Can you explain how memory quarantine works?"}, self.store)
        context = response["hookSpecificOutput"]["additionalContext"]
        self.assertIn(expected["id"], context)
        for record in excluded:
            self.assertNotIn(record["id"], context)
        for marker in ("CURRENT_PRIVATE", "FOREIGN_PRIVATE", "RAW_PRIVATE", "SUPERSEDED_PRIVATE"):
            self.assertNotIn(marker, context)
        self.assertLessEqual(len(context), 6000)

    def test_empty_prompt_words_do_not_turn_into_unfiltered_recent_context(self):
        self.store.remember(self.project, "Unrelated billing", "Do not inject this.")
        self.assertEqual([], self.ids("Can you explain how this works?"))

    def test_prompt_words_do_not_consume_the_term_budget_before_topic(self):
        note = self.store.remember(self.project, "Checkout quarantine", "The checkout queue has a quarantine.")
        self.assertEqual([note["id"]], self.ids("Can you please " * 14 + "explain checkout quarantine?"))

    def test_prompt_uses_the_same_recent_observation_lane_before_candidate_cap(self):
        for index in range(105):
            self.store.remember(self.project, f"Checkout quarantine decision {index}",
                "Checkout quarantine policy was selected.", session_id="old-decisions",
                observation={"type": "decision"})
        recent = self.store.remember(self.project, "Checkout quarantine capture failed",
            "Production capture failed; verify this before release.", session_id="recent",
            observation={"type": "discovery"})
        for query in ("checkout quarantine", "How does checkout quarantine work?"):
            with self.subTest(query=query):
                self.assertEqual(recent["id"], self.ids(query)[0])

    def test_prompt_collapses_same_session_summaries_before_candidate_cap(self):
        for index in range(105):
            self.store.remember(self.project, f"Checkout quarantine handoff {index}",
                "Checkout quarantine verification pending.", session_id="same-chat",
                kind="session_summary", session_summary={"next_steps": "Check checkout quarantine."})
        recent = self.store.remember(self.project, "Checkout quarantine capture failed",
            "Production capture failed; verify this before release.", session_id="recent",
            observation={"type": "discovery"})
        self.assertIn(recent["id"], self.ids("How does checkout quarantine work?"))
