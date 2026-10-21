"""Final prepared fields reach assessment before the atomic commit."""
import json
from pathlib import Path
import tempfile
import unittest

from codex_mem.config import configure
from codex_mem.episode_processor import process_project_memory
from codex_mem.project_memory import ProjectMemory
from codex_mem.store import Store
from test_episode_processor import receipt
from tests.test_jev_quality import response


class FinalQualityTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.project = Path(temporary.name) / "project"
        self.project.mkdir()
        self.data = Path(temporary.name) / "data"
        configure(self.data, capture_scope="all", project_memory_mode="shadow",
                  project_memory_projects=[str(self.project)], jev_quality_enabled=False)

    def remember(self, text, session, source="hook:UserPromptSubmit"):
        with Store(self.data) as store:
            return store.remember(self.project, "Task FINAL-17", text, source=source, session_id=session)

    def state(self):
        with Store(self.data) as store:
            return ProjectMemory(store).snapshot(self.project)

    @staticmethod
    def generate(request, target=None, help_open=False):
        supplied = json.loads(request["prompt"].split("\n", 1)[1])
        events = supplied["events"]
        user = next(event for event in events if event["source_role"] == "user_intent" and not event.get("historical"))
        ref = user["spans"][0]["ref"]
        item = {"summary": "Task FINAL-17", "goal": "Task FINAL-17", "rationale": "", "outcome": "",
                "open_work": "", "continuation_of": target, "consolidation_of": [],
                "evidence": {"summary": [ref], "goal": [ref], "rationale": [], "outcome": [],
                             "open_work": [], "continuation_of": [ref] if target else [], "consolidation_of": []}}
        if help_open:
            tool = next(event for event in events if event["source_role"] == "tool_record")
            item["open_work"] = "Command help"
            item["evidence"]["open_work"] = [span["ref"] for span in tool["spans"]]
        return receipt({"episodes": [item], "claims": [], "relations": []})

    def test_carried_final_field_is_assessed_and_rejection_preserves_head(self):
        self.remember("Task FINAL-17: retain the exporter.", "first")
        self.remember("Command: export --help\nUsage: export --verify. This is help, not an unfinished user task.",
                      "first", "hook:PostToolUse")
        first = process_project_memory(self.project, self.data,
                    runner=lambda request: self.generate(request, help_open=True))
        self.assertEqual(first["status"], "processed", first)
        parent = self.state()["episodes"][0]["id"]
        self.remember("Continue task FINAL-17. Preserve the existing acceptance requirements.", "later")
        configure(self.data, jev_quality_enabled=True)
        seen = []
        def evaluate(payload):
            assertions = [json.loads(item["candidate_claims"]["body"]) for item in payload["state"]["items"]]
            seen.extend(assertions)
            reject = any(item.get("field") == "prior_open_work" for item in assertions)
            return response(payload, .01 if reject else .99, .01)
        result = process_project_memory(self.project, self.data,
                    runner=lambda request: self.generate(request, target=parent), jev_quality_evaluator=evaluate)
        self.assertTrue(any(item.get("field") == "prior_open_work" for item in seen), seen)
        self.assertEqual(result["status"], "failed", result)
        self.assertEqual([item["id"] for item in self.state()["episodes"]], [parent])

    def test_prepared_digest_cannot_authorize_a_different_commit(self):
        source = self.remember("Task FINAL-17: preserve the signed receipt.", "first")
        with Store(self.data) as store:
            memory = ProjectMemory(store)
            job = memory.claim(self.project)
            span = {"source_id": source["id"], "start": 0, "end": len(source["body"]), "quote": source["body"]}
            output = {"episodes": [{"key": "e", "summary": source["body"], "evidence": {"summary": [span]}}],
                      "claims": [], "relations": []}
            prepared = memory.prepare(job, output)
            self.assertEqual(memory.snapshot(self.project)["episodes"], [])
            with self.assertRaisesRegex(ValueError, "changed after assessment"):
                memory.commit(job, {"episodes": [], "claims": [], "relations": []},
                              expected_prepared_digest=prepared["digest"])
            self.assertEqual(memory.snapshot(self.project)["episodes"], [])
            self.assertEqual(memory.commit(job, output, expected_prepared_digest=prepared["digest"])["status"], "committed")
