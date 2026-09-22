"""Skill routing metadata must not become mandatory memory search terms."""
from pathlib import Path
import tempfile
import unittest
import xml.etree.ElementTree as ET

from codex_mem.config import configure
from codex_mem.hooks import _prior_context, handle_hook
from codex_mem.query import normalize_retrieval_query
from codex_mem.store import Store


class SkillPromptContextTests(unittest.TestCase):
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

    def context_ids(self, query):
        context = _prior_context(
            self.store, str(self.project), {"context_chars": 6000},
            active_session_id="current", exclude_session="current", query=query,
        )
        return [entry.attrib["id"] for entry in ET.fromstring(context).findall("entry")]

    def test_routed_skill_does_not_hide_the_topic_in_the_real_hook_builder(self):
        note = self.store.remember(
            self.project, "Плагин памяти", "Плагин сохраняет решения и результаты проверок.",
            session_id="previous", observation={"type": "discovery"},
        )
        mention = "[$codex-mem:maintenance](/tmp/plugins/codex-mem/1.9.0/skills/maintenance/SKILL.md)"
        query = f"проанализируй как там наш плагин {mention}\nкак справляется с задачей своей"
        self.assertEqual([note["id"]], self.context_ids(query))

    def test_routing_is_removed_before_the_hook_query_length_bound(self):
        note = self.store.remember(
            self.project, "memory_search", "The lookup returns useful project findings.",
            session_id="previous", observation={"type": "discovery"},
        )
        mention = "[$maintenance](/tmp/" + "x" * 1100 + "/skills/maintenance/SKILL.md)"
        self.assertEqual([note["id"]], self.context_ids(mention + " How does memory_search work?"))

    def test_skill_routing_does_not_relax_explicit_version_or_symbol(self):
        note = self.store.remember(self.project, "memory_search 1.9.0", "Lookup behavior.")
        self.store.remember(self.project, "memory_searcher 1.9.0", "Other symbol.")
        self.store.remember(self.project, "memory_search 1.8.0", "Older behavior.")
        mention = "[$maintenance](/tmp/skills/maintenance/SKILL.md)"
        self.assertEqual([note["id"]], self.context_ids(mention + " How does memory_search work in 1.9.0?"))

    def test_normalization_preserves_the_original_captured_evidence(self):
        note = self.store.remember(self.project, "Memory quarantine", "The quarantine has been checked.",
                                   session_id="previous")
        prompt = "[$maintenance](/tmp/skills/maintenance/SKILL.md) How does memory quarantine work?"
        response = handle_hook({"hook_event_name": "UserPromptSubmit", "cwd": str(self.project),
                                "session_id": "current", "turn_id": "turn-one", "prompt": prompt},
                               self.store)
        self.assertIn(note["id"], response["hookSpecificOutput"]["additionalContext"])
        captured = [r for r in self.store.timeline(self.project)
                    if r.get("source") == "hook:UserPromptSubmit"]
        self.assertEqual(1, len(captured))
        full = self.store.get(self.project, [captured[0]["id"]])
        self.assertIn(prompt, full[0]["body"])

    def test_routing_only_prompt_does_not_inject_unrelated_recent_memory(self):
        self.store.remember(self.project, "Unrelated billing", "Different subject.", session_id="previous")
        context = _prior_context(
            self.store, str(self.project), {"context_chars": 6000},
            active_session_id="current", exclude_session="current",
            query="[$maintenance](/tmp/skills/maintenance/SKILL.md)",
        )
        self.assertEqual("", context)

    def test_only_explicit_local_skill_routing_is_removed(self):
        for query in (
            "Explain [maintenance](/tmp/skills/maintenance/SKILL.md)",
            "Explain [$maintenance](https://example.test/skills/maintenance/SKILL.md)",
            "Inspect [$symbol](/tmp/source/worker.py)",
            "How does /tmp/skills/maintenance/SKILL.md work?",
            "Find $HOME and memory_search in v1.9.0 and AIAN-700",
            "[$maintenance](/tmp/skills/maintenance/SKILL.md.bak)",
        ):
            with self.subTest(query=query):
                self.assertEqual(query, normalize_retrieval_query(query))
        for target in (
            "/tmp/skills/maintenance/SKILL.md",
            "</tmp/My Skills/maintenance/SKILL.md>",
            "~/skills/maintenance/SKILL.md",
            r"C:\Users\alex\skills\maintenance\SKILL.md",
        ):
            with self.subTest(target=target):
                query = f"[$codex-mem:maintenance]({target}) memory_search 1.9.0"
                normalized = normalize_retrieval_query(query)
                self.assertEqual("memory_search 1.9.0", normalized)
                self.assertEqual(normalized, normalize_retrieval_query(normalized))


if __name__ == "__main__":
    unittest.main()
