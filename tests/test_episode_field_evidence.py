"""Public processing checks for field-bound semantic evidence, without model calls."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest import mock

from codex_mem import episode_processor as ep
from codex_mem.config import configure
from codex_mem.jev_quality import MODEL
from codex_mem.project_memory import ProjectMemory
from codex_mem.store import Store
from test_episode_processor import receipt


class FieldEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.project = Path(self.temporary.name) / "project"
        self.project.mkdir()
        self.data = Path(self.temporary.name) / "memory"
        configure(self.data, capture_scope="all", project_memory_mode="shadow",
                  project_memory_projects=[str(self.project)], jev_quality_enabled=True)
        self.requests = []

    def remember(self, text, *, project=None):
        with Store(self.data) as store:
            return store.remember(project or self.project, "Task", text,
                                  source="hook:UserPromptSubmit", session_id="session")

    def state(self):
        with Store(self.data) as store:
            return ProjectMemory(store).snapshot(self.project)

    def runner(self, outcome_ref=0):
        def run(request):
            supplied = json.loads(request["prompt"].split("\n", 1)[1])
            spans = [span for event in supplied["events"] for span in event["spans"]]
            item = {"summary": "Plan importer.", "goal": "", "rationale": "", "outcome": "Importer remains open.",
                    "open_work": "", "continuation_of": None,
                    "evidence": {"summary": [spans[0]["ref"]], "goal": [], "rationale": [],
                                 "outcome": [spans[outcome_ref]["ref"]], "open_work": [], "continuation_of": []}}
            return receipt({"episodes": [item], "claims": [], "relations": []})
        return run

    def evaluate(self, payload):
        self.requests.append(deepcopy(payload))
        answers = {}
        for item in payload["state"]["items"]:
            assertion = json.loads(item["candidate"]["body"])
            supported = assertion["value"] in [span["quote"].strip() for span in item["field_evidence"]]
            for suffix in ("grounded", "overclaim"):
                answers[f"item_{item['item_index']}_{suffix}"] = {
                    "type": "noul", "noul": .99 if supported == (suffix == "grounded") else .01}
        return {"model": MODEL, "answers": answers, "usage": {"input_tokens": 7, "output_tokens": 3}}

    def test_field_support_does_not_leak_between_fields_of_one_episode(self):
        source = self.remember("Plan importer. Importer remains open.")
        result = ep.process_project_memory(self.project, self.data, runner=self.runner(),
                                           jev_quality_evaluator=self.evaluate)
        self.assertEqual(result["status"], "failed", result)
        self.assertTrue(self.requests)
        state = self.state()
        self.assertEqual(state["episodes"], [])
        self.assertEqual(state["claims"], [])
        self.assertEqual(state["coverage"]["pending_events"], 1)
        self.assertEqual(state["coverage"]["processed_events"], 0)
        with Store(self.data) as store:
            self.assertEqual(store.get(self.project, [source["id"]])[0]["body"],
                             "Plan importer. Importer remains open.")
        fields = [item for request in self.requests for item in request["state"]["items"]]
        self.assertEqual(len(fields), 2)
        self.assertEqual([json.loads(item["candidate"]["body"])["field"] for item in fields],
                         ["summary", "outcome"])

    def test_supported_fields_commit_with_exact_spans_and_reference_only_source_context(self):
        self.remember("Plan importer. Importer remains open.")
        result = ep.process_project_memory(self.project, self.data, runner=self.runner(1),
                                           jev_quality_evaluator=self.evaluate)
        self.assertEqual(result["status"], "processed", result)
        self.assertEqual(self.state()["episodes"][0]["outcome"], "User intent: Importer remains open.")
        self.assertEqual(result["quality"][0]["usage"], {"input_tokens": 7, "output_tokens": 3})
        items = self.requests[0]["state"]["items"]
        outcome = next(item for item in items if json.loads(item["candidate"]["body"])["field"] == "outcome")
        self.assertEqual(outcome["cited_sources"][0]["body"], " Importer remains open.")
        self.assertEqual(outcome["field_evidence"][0]["start"], 14)
        self.assertEqual(outcome["cited_sources"][0]["body_offset"], 14)
        self.assertEqual(outcome["source_context"][0]["body"], "Plan importer. Importer remains open.")
        self.assertEqual(outcome["source_context_use"], "qualify_or_negate_only")
        self.assertEqual(outcome["reference_only_record"]["project"], str(self.project.resolve()))
        for question in self.requests[0]["questions"].values():
            self.assertIn("uncited passage cannot", question["instructions"]["field_evidence_binding"])

    def test_disabled_quality_commits_without_evaluator_dispatch(self):
        configure(self.data, jev_quality_enabled=False)
        self.remember("Plan importer. Importer remains open.")
        result = ep.process_project_memory(self.project, self.data, runner=self.runner(1),
                                           jev_quality_evaluator=self.evaluate)
        self.assertEqual(result["status"], "processed", result)
        self.assertEqual(self.requests, [])
        self.assertNotIn("quality", result)

    def test_foreign_source_is_rejected_before_evaluator_and_preserves_raw_event(self):
        foreign_project = Path(self.temporary.name) / "foreign"
        foreign_project.mkdir()
        foreign = self.remember("Foreign deployment passed.", project=foreign_project)
        source = self.remember("Plan importer. Importer remains open.")
        base = self.runner(1)
        def run(request):
            result = base(request)
            result["output"]["episodes"][0]["evidence"]["outcome"] = [
                {"source_id": foreign["id"], "quote": "Foreign deployment passed.", "start": 0, "end": 26}]
            return result
        result = ep.process_project_memory(self.project, self.data, runner=run,
                                           jev_quality_evaluator=self.evaluate)
        self.assertEqual(result["code"], "invalid_response", result)
        self.assertEqual(self.requests, [])
        self.assertEqual(self.state()["episodes"], [])
        with Store(self.data) as store:
            self.assertTrue(store.get(self.project, [source["id"]]))

    def test_deleted_source_is_not_sent_to_evaluator(self):
        source = self.remember("Plan importer. Importer remains open.")
        base = self.runner(1)
        def run(request):
            result = base(request)
            with Store(self.data) as store:
                store.forget(self.project, [source["id"]])
            return result
        result = ep.process_project_memory(self.project, self.data, runner=run,
                                           jev_quality_evaluator=self.evaluate)
        self.assertEqual(result["code"], "source_changed", result)
        self.assertEqual(self.requests, [])
        self.assertEqual(self.state()["episodes"], [])

    def test_missing_optional_field_evidence_is_omitted_before_semantic_review(self):
        self.remember("Plan importer. Importer remains open.")
        base = self.runner(1)
        def run(request):
            result = base(request)
            result["output"]["episodes"][0]["evidence"]["outcome"] = []
            return result
        result = ep.process_project_memory(self.project, self.data, runner=run,
                                           jev_quality_evaluator=self.evaluate)
        self.assertEqual(result["status"], "processed", result)
        self.assertEqual(result["omitted_fields_count"], 1)
        self.assertEqual(self.state()["episodes"][0]["outcome"], "")
        self.assertEqual(len(self.requests[0]["state"]["items"]), 1)

    def test_claim_state_has_its_own_evidence_and_scope(self):
        self.remember("Use local importer. Deployment remains open.")
        def run(request):
            supplied = json.loads(request["prompt"].split("\n", 1)[1])
            spans = supplied["events"][0]["spans"]
            claim = {"text": "Use local importer.", "kind": "task", "workflow_state": "completed",
                     "scope": {"environment": "production", "version": "v2", "worktree": ""}, "rationale": "",
                     "evidence": {"text": [spans[0]["ref"]], "workflow_state": [spans[1]["ref"]], "rationale": []}}
            return receipt({"episodes": [], "claims": [claim], "relations": []})
        def evaluate(payload):
            self.requests.append(deepcopy(payload))
            answers = {}
            for item in payload["state"]["items"]:
                assertion = json.loads(item["candidate"]["body"])
                self.assertEqual(assertion["scope"]["environment"], "production")
                self.assertEqual(item["reference_only_record"]["text"], "Use local importer.")
                supported = assertion["field"] == "text"
                if not supported:
                    self.assertEqual(item["cited_sources"][0]["body"], " Deployment remains open.")
                for suffix in ("grounded", "overclaim"):
                    answers[f"item_{item['item_index']}_{suffix}"] = {
                        "type": "noul", "noul": .99 if supported == (suffix == "grounded") else .01}
            return {"model": MODEL, "answers": answers, "usage": {"input_tokens": 7, "output_tokens": 3}}
        result = ep.process_project_memory(self.project, self.data, runner=run, jev_quality_evaluator=evaluate)
        self.assertEqual(result["status"], "failed", result)
        self.assertEqual(self.state()["claims"], [])
        self.assertEqual(len(self.requests[0]["state"]["items"]), 2)

    def test_continuation_checks_only_its_refs_against_actual_prior_head(self):
        self.remember("Plan importer. Importer remains open.")
        first = ep.process_project_memory(self.project, self.data, runner=self.runner(1),
                                          jev_quality_evaluator=self.evaluate)
        self.assertEqual(first["status"], "processed", first)
        prior = self.state()["episodes"][0]
        self.remember("Plan importer. Continue importer.")
        self.requests.clear()
        def run(request):
            supplied = json.loads(request["prompt"].split("\n", 1)[1])
            spans = supplied["events"][0]["spans"]
            item = {"summary": "Plan importer.", "goal": "", "rationale": "", "outcome": "", "open_work": "",
                    "continuation_of": prior["id"], "evidence": {"summary": [spans[0]["ref"]], "goal": [],
                    "rationale": [], "outcome": [], "open_work": [], "continuation_of": [spans[1]["ref"]]}}
            return receipt({"episodes": [item], "claims": [], "relations": []})
        def evaluate(payload):
            self.requests.append(deepcopy(payload))
            continuation = next(item for item in payload["state"]["items"]
                                if item["candidate"]["title"] == "Project memory continuation")
            self.assertEqual(continuation["reference_only_record"]["prior_episode"]["id"], prior["id"])
            self.assertEqual(continuation["reference_only_record"]["summary"], "Plan importer.")
            self.assertEqual(continuation["cited_sources"][0]["body"], " Continue importer.")
            self.assertEqual(len(continuation["field_evidence"]), 1)
            return {"model": MODEL, "answers": {key: {"type": "noul", "noul": .99 if key.endswith("_grounded") else .01}
                                                   for key in payload["questions"]}, "usage": {"input_tokens": 7, "output_tokens": 3}}
        result = ep.process_project_memory(self.project, self.data, runner=run, jev_quality_evaluator=evaluate)
        self.assertEqual(result["status"], "processed", result)
        self.assertEqual(len(self.requests[0]["state"]["items"]), 2)

    def test_all_fields_are_checked_across_bounded_batches_and_usage_is_summed(self):
        self.remember("Plan importer. Importer remains open.")
        base = self.runner(1)
        def run(request):
            result = base(request)
            item = result["output"]["episodes"][0]
            for field in ("goal", "rationale", "open_work"):
                item[field] = item["summary"]
                item["evidence"][field] = item["evidence"]["summary"]
            result["output"]["episodes"] = [deepcopy(item), deepcopy(item)]
            return result
        result = ep.process_project_memory(self.project, self.data, runner=run,
                                           jev_quality_evaluator=self.evaluate)
        self.assertEqual(result["status"], "processed", result)
        self.assertEqual(sum(len(request["state"]["items"]) for request in self.requests), 10)
        self.assertTrue(all(len(request["state"]["items"]) <= 5 for request in self.requests))
        self.assertEqual(sum(audit["usage"]["input_tokens"] for audit in result["quality"]), 14)
        self.assertEqual(sum(audit["usage"]["output_tokens"] for audit in result["quality"]), 6)

    def test_later_unicode_source_fragment_keeps_absolute_evidence_offset(self):
        self.remember("λ" * 24000 + "Done.")
        first = ep.process_project_memory(self.project, self.data, runner=lambda request: receipt(),
                                          jev_quality_evaluator=self.evaluate)
        self.assertEqual(first["status"], "processed", first)
        def run(request):
            supplied = json.loads(request["prompt"].split("\n", 1)[1])
            span = supplied["events"][0]["spans"][0]["ref"]
            return receipt({"episodes": [{"summary": "Done.", "goal": "", "rationale": "", "outcome": "", "open_work": "",
                "continuation_of": None, "evidence": {"summary": [span], "goal": [], "rationale": [],
                "outcome": [], "open_work": [], "continuation_of": []}}], "claims": [], "relations": []})
        result = ep.process_project_memory(self.project, self.data, runner=run,
                                           jev_quality_evaluator=self.evaluate)
        self.assertEqual(result["status"], "processed", result)
        item = self.requests[0]["state"]["items"][0]
        self.assertEqual(item["field_evidence"][0], {"source_id": item["cited_sources"][0]["id"],
                         "start": 0, "end": 5, "quote": "Done."})
        self.assertEqual(item["cited_sources"][0]["body_offset"], 24000)
        self.assertEqual(self.state()["episodes"][0]["evidence"]["summary"][0]["start"], 24000)

    def test_oversized_later_field_is_rejected_before_any_evaluator_call(self):
        self.remember("A" * 23999 + ".")
        def run(request):
            supplied = json.loads(request["prompt"].split("\n", 1)[1])
            refs = [span["ref"] for span in supplied["events"][0]["spans"]]
            first = {field: "A" for field in ("summary", "goal", "rationale", "outcome", "open_work")}
            first.update(continuation_of=None,
                         evidence={field: refs[:1] for field in first})
            first["evidence"]["continuation_of"] = []
            second = {"summary": "A", "goal": "", "rationale": "", "outcome": "", "open_work": "",
                      "continuation_of": None, "evidence": {"summary": refs, "goal": [], "rationale": [],
                      "outcome": [], "open_work": [], "continuation_of": []}}
            return receipt({"episodes": [first, second], "claims": [], "relations": []})
        result = ep.process_project_memory(self.project, self.data, runner=run,
                                           jev_quality_evaluator=self.evaluate)
        self.assertEqual(result["status"], "failed", result)
        self.assertEqual(self.requests, [])
        self.assertEqual(self.state()["episodes"], [])

    def test_quality_deadline_stops_additional_field_batches(self):
        from codex_mem import jev_quality
        self.remember("Plan importer. Importer remains open.")
        base = self.runner(1)
        def run(request):
            result = base(request)
            result["output"]["episodes"] = [deepcopy(result["output"]["episodes"][0]) for _ in range(3)]
            return result
        def slow_evaluate(payload):
            result = self.evaluate(payload)
            time.sleep(.03)
            return result
        with mock.patch.object(jev_quality, "MAX_GATE_SECONDS", .02):
            result = ep.process_project_memory(self.project, self.data, runner=run,
                                               jev_quality_evaluator=slow_evaluate)
        self.assertEqual(result["status"], "failed", result)
        self.assertEqual(len(self.requests), 1)
        self.assertEqual(self.state()["episodes"], [])
