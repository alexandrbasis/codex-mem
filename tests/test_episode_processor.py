"""Offline checks for bounded episode generation and background receipts."""
from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from codex_mem import episode_processor as ep


def receipt(output=None, **changes):
    value = {"output": output if output is not None else {"episodes": [], "claims": [], "relations": []},
             "evidence": {"thread_start": {"thread_id": "worker-1", "model": ep.MODEL,
                                            "reasoning_effort": ep.REASONING_EFFORT, "model_provider": "openai"},
                          "turn_started": {"thread_id": "worker-1", "turn_id": "turn-1"},
                          "turn_completed": True, "no_tools": True, "rerouted": False},
             "metrics": {"duration_ms": 12, "usage": {"tokens": {"input_tokens": 100, "output_tokens": 10}}}}
    value.update(changes)
    return value


def snapshot(project):
    return {"job_id": "job-1", "token": "token-1", "project": str(project), "session_id": "session-1",
            "revision": 0, "deletion_generation": 0, "input_fingerprint": "test",
            "sources": [{"id": "event-1", "seq": 1, "body": "Use local import. Deployment remains open.",
                         "title": "Request", "source": "hook:UserPromptSubmit", "source_role": "user_intent",
                         "raw_source_link": "memory://event/event-1"}],
            "state": {"claims": [{"id": "old-claim", "text": "Earlier requirement"}], "episodes": []}}


def episode():
    return {"key": "episode-1", "summary": "Use local import.", "goal": "", "rationale": "", "outcome": "", "open_work": "",
            "evidence": {"summary": [{"source_id": "event-1", "start": 0, "end": 17, "quote": "Use local import."}],
                         "goal": [], "rationale": [], "outcome": [], "open_work": []}}


class EpisodeProcessorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.project = Path(self.tmp.name) / "project"
        self.project.mkdir()
        self.settings = {"project_memory_mode": "shadow", "project_memory_projects": [str(self.project)],
                         "capture_enabled": True, "capture_scope": "all", "excluded_projects": [],
                         "jev_quality_enabled": False}
        self.config_patch = mock.patch.object(ep, "load_config", return_value=self.settings)
        self.config_patch.start()
        self.store_patch = mock.patch.object(ep, "Store")
        self.store_patch.start()
        self.memory_patch = mock.patch("codex_mem.project_memory.ProjectMemory")
        self.memory = self.memory_patch.start().return_value
        self.memory.claim.return_value = snapshot(self.project)
        self.memory.commit.return_value = {"status": "committed"}
        self.memory.snapshot_current.return_value = True
        self.memory.begin_attempt.return_value = {"attempt_id": "attempt-1"}

    def tearDown(self):
        mock.patch.stopall()
        self.tmp.cleanup()

    def run_job(self, runner=None):
        return ep.process_project_memory(self.project, self.tmp.name, runner=runner or (lambda request: receipt()))

    def test_native_contract_bounded_request_usage_receipt_and_commit(self):
        requests = []
        output = {"episodes": [episode()], "claims": [], "relations": []}
        def run(request):
            requests.append(request)
            return receipt(output)
        result = self.run_job(run)
        self.assertEqual(result["status"], "processed")
        self.assertEqual(result["tokens"], {"input_tokens": 100, "output_tokens": 10})
        self.assertEqual(result["output_schema_version"], ep.OUTPUT_SCHEMA_VERSION)
        self.assertEqual(requests[0]["model"], ep.MODEL)
        self.assertEqual(requests[0]["reasoning_effort"], ep.REASONING_EFFORT)
        self.assertNotIn("result", requests[0]["output_schema"]["properties"])
        self.memory.commit.assert_called_once()
        self.memory.fail.assert_not_called()
        prompt = requests[0]["prompt"]
        for expected in ("user_intent", "memory://event/event-1", "Earlier requirement", "complete_transcript", "Proposed, accepted"):
            self.assertIn(expected, prompt)
        self.assertNotIn("Use local import", json.dumps(result))
        self.assertEqual(self.memory.claim.call_args.kwargs["max_events"], ep.MAX_EVENTS)

    def test_off_empty_selection_private_and_excluded_do_not_claim(self):
        for change in ({"project_memory_mode": "off"}, {"project_memory_projects": []},
                       {"capture_scope": "manual"}, {"excluded_projects": [str(self.project)]}):
            with self.subTest(change=change), mock.patch.object(ep, "load_config", return_value={**self.settings, **change}):
                self.assertEqual(self.run_job()["status"], "disabled")
        self.memory.claim.assert_not_called()

    def test_background_claim_uses_durable_readiness_and_explicit_call_is_immediate(self):
        self.assertEqual(self.run_job()["status"], "processed")
        self.assertIs(self.memory.claim.call_args.kwargs["ready_only"], False)
        ep.process_project_memory(self.project, self.tmp.name, background=True, runner=lambda request: receipt())
        self.assertIs(self.memory.claim.call_args.kwargs["ready_only"], True)
        with self.assertRaises(ValueError):
            ep.process_project_memory(self.project, self.tmp.name, background="true", runner=lambda request: receipt())

    def test_idle_does_not_launch_runner(self):
        self.memory.claim.return_value = None
        runner = mock.Mock(spec=[])
        self.assertEqual(self.run_job(runner)["status"], "idle")
        runner.assert_not_called()

    def test_timeout_retains_source_events_with_fixed_failure(self):
        def runner(request):
            raise ep.ProcessorFailure("timeout")
        result = self.run_job(runner)
        self.assertEqual((result["status"], result["code"]), ("failed", "timeout"))
        self.assertEqual(self.memory.fail.call_args.args, (self.memory.claim.return_value, "timeout"))
        self.assertEqual(self.memory.fail.call_args.kwargs["receipt"]["code"], "timeout")
        self.memory.commit.assert_not_called()

    def test_runner_failure_does_not_leak_exception_or_retry_in_call(self):
        runner = mock.Mock(spec=[], side_effect=RuntimeError("secret raw evidence"))
        result = self.run_job(runner)
        self.assertEqual(result["code"], "runner_failure")
        self.assertNotIn("secret", json.dumps(result))
        self.assertEqual(runner.call_count, 1)

    def test_invalid_model_tool_and_schema_are_quarantined_once(self):
        variants = []
        for field, value in (("no_tools", False), ("rerouted", True), ("turn_completed", False)):
            value_receipt = receipt()
            value_receipt["evidence"][field] = value
            variants.append(value_receipt)
        bad = receipt()
        bad["evidence"]["thread_start"]["model"] = "other"
        variants.extend([bad, receipt({"notes": []})])
        for value in variants:
            with self.subTest(value=value):
                self.memory.reset_mock()
                runner = mock.Mock(spec=[], return_value=value)
                self.assertEqual(self.run_job(runner)["code"], "invalid_response")
                self.assertEqual(self.memory.fail.call_args.args, (self.memory.claim.return_value, "invalid_response"))
                self.memory.commit.assert_not_called()
                self.assertEqual(runner.call_count, 1)

    def test_false_quote_rejected_before_optional_quality_call(self):
        output = {"episodes": [episode()], "claims": [], "relations": []}
        output["episodes"][0]["evidence"]["summary"][0]["quote"] = "Deployment completed"
        with mock.patch.object(ep, "_quality") as quality:
            result = self.run_job(lambda request: receipt(output))
        self.assertEqual(result["code"], "invalid_response")
        quality.assert_not_called()
        self.memory.commit.assert_not_called()

    def test_storage_validator_rejection_is_quarantined(self):
        self.memory.commit.side_effect = ValueError("unsupported closure")
        self.assertEqual(self.run_job()["code"], "invalid_response")
        self.memory.fail.assert_called_once()

    def test_stale_commit_defers_without_claiming_success(self):
        self.memory.commit.return_value = {"status": "stale"}
        self.assertEqual(self.run_job()["status"], "deferred")
        self.memory.fail.assert_not_called()

    def test_configuration_disabled_during_generation_blocks_commit(self):
        def run(request):
            self.settings["project_memory_mode"] = "off"
            return receipt()
        self.assertEqual(self.run_job(run)["status"], "disabled")
        self.memory.commit.assert_not_called()
        self.memory.fail.assert_called_once()

    def test_configuration_disabled_after_claim_blocks_dispatch(self):
        with mock.patch.object(ep, "load_config", side_effect=[self.settings, {**self.settings, "project_memory_mode": "off"}]):
            runner = mock.Mock(spec=[])
            self.assertEqual(self.run_job(runner)["status"], "disabled")
            runner.assert_not_called()

    def test_prompt_and_output_limits_do_not_truncate_evidence(self):
        raw = snapshot(self.project)
        raw["sources"][0]["body"] = "x" * ep.MAX_PROMPT_CHARS
        with self.assertRaises(ep.ProcessorFailure):
            ep.runner_request(raw, 10)
        oversized = {"episodes": [episode()] * (ep.MAX_RECORDS + 1), "claims": [], "relations": []}
        self.assertEqual(self.run_job(lambda request: receipt(oversized))["code"], "invalid_response")

    def test_optional_gate_receives_original_roles_and_cited_fields(self):
        output = {"episodes": [episode()], "claims": [], "relations": []}
        with mock.patch("codex_mem.jev_quality.quality_gate", return_value={"route": "accept", "model": "fixed"}) as gate:
            audits = ep._quality(output, snapshot(self.project), self.settings, None)
        self.assertEqual(audits, [{"route": "accept", "model": "fixed"}])
        notes, _, supplied = gate.call_args.args
        self.assertEqual(notes[0]["source_ids"], ["event-1"])
        self.assertEqual(supplied["sources"][0]["source_role"], "user_intent")
        self.assertNotIn("verified", json.dumps(audits))

    def test_quote_resolution_computes_unicode_offsets_without_mutating_output(self):
        raw = snapshot(self.project)
        raw["sources"][0]["body"] = "Привет 🌿. Use local import."
        item = episode()
        item["evidence"]["summary"] = [{"source_id": "event-1", "quote": "Use local import."}]
        output = {"episodes": [item], "claims": [], "relations": []}
        resolved = ep._resolve_quotes(output, raw)
        span = resolved["episodes"][0]["evidence"]["summary"][0]
        self.assertEqual((span["start"], span["end"]), (10, 27))
        self.assertNotIn("start", output["episodes"][0]["evidence"]["summary"][0])
        ep._validate_output(resolved, raw)
        wire_span = ep.output_schema()["properties"]["episodes"]["items"]["properties"]["evidence"]["properties"]["summary"]["items"]
        self.assertEqual(wire_span, {"$ref": "#/$defs/evidence_ref"})

    def test_quote_resolution_rejects_repeated_overlapping_and_forged_quotes(self):
        for body, quote in (("pass then pass", "pass"), ("aaaa", "aa"), ("actual result", "invented result")):
            raw = snapshot(self.project)
            raw["sources"][0]["body"] = body
            item = episode()
            item["evidence"]["summary"] = [{"source_id": "event-1", "quote": quote}]
            with self.subTest(body=body), self.assertRaises(ValueError):
                ep._resolve_quotes({"episodes": [item], "claims": [], "relations": []}, raw)

    def test_canonical_offsets_are_never_repaired(self):
        raw = snapshot(self.project)
        item = episode()
        item["evidence"]["summary"][0]["end"] = 72
        resolved = ep._resolve_quotes({"episodes": [item], "claims": [], "relations": []}, raw)
        self.assertEqual(resolved["episodes"][0]["evidence"]["summary"][0]["end"], 72)
        with self.assertRaises(ValueError):
            ep._validate_output(resolved, raw)

    def test_usage_attempt_begins_before_native_and_records_partial_checkpoint_then_final(self):
        calls = []
        self.memory.begin_attempt.side_effect = lambda *a, **k: (calls.append("begin") or {"attempt_id": "attempt-1"})
        def factory(**kwargs):
            def run(request):
                calls.append("native")
                kwargs["usage_checkpoint"]({"worker_thread_id": "worker-1", "worker_turn_id": "turn-1",
                    "metrics": {"duration_ms": 5, "usage": {"status": "partial", "tokens": {"input_tokens": 50}}},
                    "prompt": "must never persist"})
                return receipt()
            return run
        with mock.patch.object(ep, "NativeProcessorRunner", side_effect=factory):
            result = ep.process_project_memory(self.project, self.tmp.name)
        self.assertEqual(calls, ["begin", "native"])
        checkpoint = self.memory.checkpoint_attempt.call_args.args[2]
        self.assertEqual(checkpoint["usage_status"], "partial")
        self.assertEqual(checkpoint["input_tokens"], 50)
        self.assertNotIn("prompt", checkpoint)
        self.assertEqual(self.memory.finalize_attempt.call_args.args[2], "processed")
        self.assertEqual(result["usage_recording_status"], "recorded")

    def test_usage_finalizes_failed_validation_timeout_stale_and_configuration_cancel(self):
        def timeout(request):
            raise ep.ProcessorFailure("timeout", worker_thread_id="worker-1", metrics={
                "usage": {"status": "partial", "tokens": {"input_tokens": 17}}})
        result = self.run_job(timeout)
        final = self.memory.finalize_attempt.call_args
        self.assertEqual(final.args[2], "failed")
        self.assertEqual(final.kwargs["receipt"]["usage_status"], "partial")
        self.assertEqual(final.kwargs["receipt"]["input_tokens"], 17)
        self.assertEqual(result["worker_thread_id"], "worker-1")
        self.memory.reset_mock()
        self.run_job(lambda request: receipt({"episodes": [], "claims": [], "relations": [], "forged": True}))
        self.assertEqual(self.memory.finalize_attempt.call_args.kwargs["receipt"]["input_tokens"], 100)
        self.memory.reset_mock()
        def stale(request):
            self.memory.snapshot_current.return_value = False
            return receipt()
        self.assertEqual(self.run_job(stale)["status"], "deferred")
        self.assertEqual(self.memory.finalize_attempt.call_args.args[2], "deferred")
        self.memory.snapshot_current.return_value = True
        self.memory.reset_mock()
        def cancel(request):
            self.settings["project_memory_mode"] = "off"
            return receipt()
        self.assertEqual(self.run_job(cancel)["status"], "disabled")
        self.assertEqual(self.memory.finalize_attempt.call_args.args[2], "disabled")

    def test_usage_begin_failure_prevents_native_and_finalize_failure_preserves_commit(self):
        self.memory.begin_attempt.side_effect = ep.StoreError("private storage error")
        native = mock.Mock(spec=[])
        self.assertEqual(self.run_job(native)["code"], "storage_failure")
        native.assert_not_called()
        self.memory.finalize_attempt.assert_not_called()
        self.memory.begin_attempt.side_effect = None
        self.memory.finalize_attempt.side_effect = ep.StoreError("private storage error")
        result = self.run_job()
        self.assertEqual(result["status"], "processed")
        self.assertEqual(result["usage_recording_status"], "failed")
        self.assertNotIn("private", json.dumps(result))

    def test_v7_native_continuity_schema_lists_only_supplied_current_project_heads(self):
        raw = snapshot(self.project)
        root = {"id": "head-current", "project": raw["project"], "summary": "ORCH-218 import.",
                "goal": "Repair ORCH-218 importer", "logical_episode_id": "root", "episode_version": 2,
                "is_current_head": True}
        foil = {**root, "id": "head-foil", "logical_episode_id": "foil-root", "goal": "Review TASK-999 environment",
                "summary": "Review TASK-999 environment. This is unrelated to ORCH-218.",
                "evidence": {"summary": [{"source_id": "older-foil", "quote": "Review TASK-999 environment. This is unrelated to ORCH-218."}]}}
        raw["sources"][0]["body"] = "Correction for ORCH-218: the revised importer check still fails locally."
        raw["state"]["episodes"] = [root, foil, {**root, "id": "historical", "is_current_head": False},
                                       {**root, "id": "foreign", "project": "/foreign"}]
        raw["state"]["claims"].append({"id": "old-open", "text": "Keep the early requirement.", "workflow_state": "open"})
        request = ep.runner_request(raw, 10)
        supplied = json.loads(request["prompt"].split("\n", 1)[1])
        visible = {episode["id"]: episode for episode in supplied["current_state"]["episodes"]}
        self.assertEqual(set(visible), {"head-current", "head-foil"})
        self.assertEqual(visible["head-foil"]["goal"], "Review TASK-999 environment")
        self.assertIn("unrelated to ORCH-218", visible["head-foil"]["evidence"]["summary"][0]["quote"])
        self.assertEqual(supplied["events"][0]["source_role"], "user_intent")
        self.assertIn("Correction for ORCH-218", supplied["events"][0]["spans"][0]["text"])
        self.assertIn("old-open", [claim["id"] for claim in supplied["current_state"]["claims"]])
        branches = request["output_schema"]["properties"]["episodes"]["items"]["anyOf"]
        root_schema, continuation_schema = branches
        self.assertEqual(root_schema["properties"]["continuation_of"], {"type": "null"})
        self.assertEqual(root_schema["properties"]["evidence"]["properties"]["continuation_of"]["maxItems"], 0)
        self.assertEqual(set(continuation_schema["properties"]["continuation_of"]["enum"]), {"head-current", "head-foil"})
        self.assertEqual(continuation_schema["properties"]["evidence"]["properties"]["continuation_of"]["minItems"], 1)
        for branch in branches:
            self.assertIn("continuation_of", branch["required"])
            self.assertIn("continuation_of", branch["properties"]["evidence"]["required"])
        self.assertIn("shared stable task ID or the whole prior goal phrase", request["prompt"])
        self.assertIn("never merges decisions", request["prompt"])
        try:
            import jsonschema
        except ImportError:
            jsonschema = None
        if jsonschema is not None:
            validator = jsonschema.Draft202012Validator(request["output_schema"])
            item = episode()
            del item["key"]
            ref = next(iter(ep._span_catalog(raw)[1]))
            item["continuation_of"] = "head-current"
            item["consolidation_of"] = []
            item["evidence"]["consolidation_of"] = []
            item["evidence"]["summary"] = [ref]
            item["evidence"]["continuation_of"] = [ref]
            wire = {"episodes": [item], "claims": []}
            self.assertTrue(validator.is_valid(wire))
            item["evidence"]["continuation_of"] = []
            self.assertFalse(validator.is_valid(wire))
            item["continuation_of"] = None
            self.assertTrue(validator.is_valid(wire))
            item["evidence"]["continuation_of"] = [ref]
            self.assertFalse(validator.is_valid(wire))
            del item["continuation_of"]
            self.assertFalse(validator.is_valid(wire))

    def test_v7_canonical_legacy_roots_and_exact_known_continuity_refs_remain_compatible(self):
        raw = snapshot(self.project)
        raw["sources"][0].update(body="Continue ORCH-218 importer repair.", body_offset=731)
        raw["state"]["episodes"] = [{"id": "head", "project": raw["project"], "is_current_head": True,
                                      "summary": "Repair ORCH-218 importer", "goal": "Repair ORCH-218 importer"}]
        refs = list(ep._span_catalog(raw)[1])
        item = episode()
        item["summary"] = "Continue ORCH-218 importer repair."
        item["evidence"]["summary"] = [refs[0]]
        del item["key"]
        item["continuation_of"] = "head"
        item["evidence"]["continuation_of"] = [refs[0]]
        wire = {"episodes": [item], "claims": [], "relations": []}
        canonical = ep._resolve_object_references(wire, raw)
        canonical = ep._resolve_evidence_refs(canonical, raw)
        ep._validate_output(canonical, raw)
        self.assertEqual(canonical["episodes"][0]["continuation_of"], "head")
        span = canonical["episodes"][0]["evidence"]["continuation_of"][0]
        self.assertEqual(span["start"], 0)
        self.assertEqual(span["end"], len(raw["sources"][0]["body"]))
        self.assertEqual(span["source_id"], "event-1")
        legacy = deepcopy(canonical)
        del legacy["episodes"][0]["continuation_of"]
        del legacy["episodes"][0]["evidence"]["continuation_of"]
        ep._validate_output(legacy, raw)
        unsupported = deepcopy(canonical)
        unsupported["episodes"][0]["evidence"]["continuation_of"] = []
        ep._validate_output(unsupported, raw)

    def test_v7_unknown_historical_foreign_and_forged_continuity_are_strict(self):
        raw = snapshot(self.project)
        raw["state"]["episodes"] = [{"id": "head", "project": raw["project"], "is_current_head": True},
                                      {"id": "historical", "project": raw["project"], "is_current_head": False},
                                      {"id": "foreign", "project": "/foreign", "is_current_head": True}]
        item = episode()
        item["evidence"]["continuation_of"] = list(item["evidence"]["summary"])
        for target in ("unknown", "event-1", "historical", "foreign", None):
            output = {"episodes": [{**deepcopy(item), "continuation_of": target}], "claims": [], "relations": []}
            with self.subTest(target=target), self.assertRaises(ValueError):
                ep._validate_output(output, raw)
        item["continuation_of"] = "head"
        item["evidence"]["continuation_of"] = ["r-forged"]
        with self.assertRaises(ValueError):
            ep._resolve_evidence_refs({"episodes": [item], "claims": [], "relations": []}, raw)
        item["evidence"]["continuation_of"] = [{"source_id": "event-1", "start": 0, "end": 17, "quote": "Falsified quote."}]
        with self.assertRaises(ValueError):
            ep._validate_output({"episodes": [item], "claims": [], "relations": []}, raw)

    def test_v7_quality_adapter_has_resolved_prior_head_and_current_continuity_evidence(self):
        raw = snapshot(self.project)
        raw["state"]["episodes"] = [{"id": "head", "project": raw["project"], "is_current_head": True,
            "summary": "Earlier ORCH-218 importer repair", "goal": "Repair ORCH-218 importer", "open_work": "Finish repair",
            "logical_episode_id": "root", "episode_version": 2}]
        item = episode()
        item["continuation_of"] = "head"
        item["evidence"]["continuation_of"] = deepcopy(item["evidence"]["summary"])
        output = {"episodes": [item], "claims": [], "relations": []}
        ep._validate_output(output, raw)
        with mock.patch("codex_mem.jev_quality.quality_gate", return_value={"route": "accept"}) as gate:
            ep._quality(output, raw, self.settings, None)
        notes, _, provided = gate.call_args.args
        continuation = next(note for note in notes if note["title"] == "Project memory continuation")
        candidate = json.loads(continuation["body"])
        prior = continuation["reference_only_record"]["prior_episode"]
        self.assertEqual(prior["goal"], "Repair ORCH-218 importer")
        self.assertEqual(prior["logical_episode_id"], "root")
        self.assertEqual(continuation["field_evidence"][0]["source_id"], "event-1")
        self.assertIn("Same chat, lexical similarity", candidate["assertion_to_check"])
        self.assertEqual(provided["context"][0]["source"], "processor:project-memory-reference")
        self.assertIn("Finish repair", provided["context"][0]["body"])

    def test_v7_omitted_continuity_receipt_keeps_reconciliation_visible(self):
        self.memory.commit.return_value = {"status": "committed", "omitted_continuities_count": 1,
                                          "omitted_continuities_reason": "unproved_continuity", "pending_reconciliation": True}
        result = self.run_job()
        self.assertEqual(result["status"], "processed")
        self.assertEqual(result["omitted_continuities_count"], 1)
        self.assertEqual(result["omitted_continuities_reason"], "unproved_continuity")
        self.assertTrue(result["pending_reconciliation"])

    def test_generation_omissions_include_heads_removed_by_upstream_budget(self):
        raw = snapshot(self.project)
        raw["state"]["relations"] = [{"id": "relation-1"}, {"id": "relation-2"}]
        raw["state"]["context_selection"] = {
            "method": "reserved_heads_and_open_obligations", "budget_ascii_chars": 24000,
            "serialized_ascii_chars": 23675, "candidate_window_bounded": True,
            "considered_records": {"claims": 9, "episodes": 5, "relations": 3},
            "omitted_records": {"claims": 8, "episodes": 5, "relations": 1},
        }
        original = deepcopy(raw)
        supplied = json.loads(ep.runner_request(raw, 10)["prompt"].split("\n", 1)[1])
        metadata = supplied["current_state"]["context_selection"]
        self.assertEqual(metadata["upstream_omitted_records"], {"claims": 8, "episodes": 5, "relations": 1})
        self.assertEqual(metadata["local_omitted_records"], {"claims": 0, "episodes": 0, "relations": 2})
        self.assertEqual(metadata["omitted_records"], {"claims": 8, "episodes": 5, "relations": 3})
        self.assertTrue(metadata["upstream_omissions_reported"])
        self.assertEqual(metadata["upstream_context_selection"], original["state"]["context_selection"])
        self.assertEqual(supplied["events"][0]["id"], "event-1")
        self.assertEqual("".join(span["text"] for span in supplied["events"][0]["spans"]), raw["sources"][0]["body"])
        self.assertEqual(raw, original)

    def test_generation_combines_local_ascii_budget_drops_without_double_counting(self):
        raw = snapshot(self.project)
        raw["state"]["claims"] = [{"id": "c1", "text": "я" * 100}, {"id": "c2", "text": "я" * 100}]
        raw["state"]["episodes"] = [{"id": "head", "summary": "я" * 100, "is_current_head": True}]
        raw["state"]["relations"] = [{"id": "r1"}]
        raw["state"]["context_selection"] = {"omitted_records": {"claims": 5, "episodes": 4, "relations": 2}}
        with mock.patch.object(ep, "MAX_STATE_CHARS", 100):
            selected = ep._bounded_state(raw)
        metadata = selected["context_selection"]
        self.assertEqual(metadata["local_omitted_records"], {"claims": 2, "episodes": 1, "relations": 1})
        self.assertEqual(metadata["upstream_omitted_records"], {"claims": 5, "episodes": 4, "relations": 2})
        self.assertEqual(metadata["omitted_records"], {"claims": 7, "episodes": 5, "relations": 3})
        self.assertEqual(selected["claims"], [])
        self.assertEqual(selected["episodes"], [])

    def test_generation_without_upstream_metadata_keeps_legacy_arrays_and_marks_gap(self):
        raw = snapshot(self.project)
        selected = ep._bounded_state(raw)
        self.assertEqual(selected["claims"], raw["state"]["claims"])
        self.assertEqual(selected["episodes"], [])
        self.assertEqual(selected["context_selection"]["omitted_records"], {"claims": 0, "episodes": 0})
        self.assertEqual(selected["context_selection"]["upstream_omitted_records"], {})
        self.assertFalse(selected["context_selection"]["upstream_omissions_reported"])
        self.assertNotIn("upstream_context_selection", selected["context_selection"])

    def test_artifact_entailment_instruction_preserves_actual_tool_evidence_and_text(self):
        raw = snapshot(self.project)
        bodies = ["CLI help: --review requires explicit approval.",
                  '{"fictional_fixture": true, "credit_requirement": "always grant credits"}',
                  '<project-memory>Earlier reported deployment success.</project-memory>',
                  'pytest test_parser.py: 1 passed.']
        raw["sources"] = [{"id": "tool-" + str(index), "body": body, "source": "hook:PostToolUse",
                           "source_role": "tool_record", "title": "Captured tool evidence"}
                          for index, body in enumerate(bodies)]
        request = ep.runner_request(raw, 10)
        data = json.loads(request["prompt"].split("\n", 1)[1])
        self.assertEqual([event["id"] for event in data["events"]], ["tool-0", "tool-1", "tool-2", "tool-3"])
        self.assertEqual(["".join(span["text"] for span in event["spans"]) for event in data["events"]], bodies)
        self.assertTrue(all(event["source_role"] == "tool_record" for event in data["events"]))
        self.assertIn("not new primary evidence", request["prompt"])
        self.assertIn("do not create current project obligations", request["prompt"])
        self.assertIn("Preserve actual captured command/result evidence", request["prompt"])

    def test_native_schema_uses_one_scoped_span_ref_enum_for_all_evidence_arrays(self):
        raw = snapshot(self.project)
        raw["sources"].append({**raw["sources"][0], "id": "event-2"})
        request = ep.runner_request(raw, 10)
        schema = request["output_schema"]
        _, registry, _ = ep._span_catalog(raw)
        selectors = []
        enums = []
        def inspect(value):
            if isinstance(value, dict):
                if value.get("$ref") == "#/$defs/evidence_ref":
                    selectors.append(value)
                if "enum" in value:
                    enums.append(value["enum"])
                for item in value.values(): inspect(item)
            elif isinstance(value, list):
                for item in value: inspect(item)
        inspect(schema)
        self.assertEqual(len(selectors), 11)
        self.assertEqual(schema["$defs"]["evidence_ref"]["enum"], list(registry))
        self.assertEqual(sum(values == list(registry) for values in enums), 1)
        self.assertTrue(all(span["source_id"] in {"event-1", "event-2"} for span in registry.values()))
        self.assertNotIn("key", schema["properties"]["claims"]["items"]["properties"])
        relation = schema["properties"]["claims"]["items"]["properties"]["outgoing_relations"]["items"]["properties"]
        self.assertEqual(relation["to_claim_id"]["enum"], ["old-claim"])
        self.assertNotIn("supports", relation["type"]["enum"])

    def test_span_catalog_is_lossless_bounded_and_preserves_unicode_fragment_offsets(self):
        raw = snapshot(self.project)
        body = "Привет 🌿.\n  Local test passed.\n" + "X.\n" * 1000 + "L" * 6500 + "\nLATE_SENTINEL.  "
        raw["sources"][0].update(body=body, body_offset=137)
        events, registry, coverage = ep._span_catalog(raw)
        self.assertEqual("".join(span["text"] for span in events[0]["spans"]), body)
        self.assertNotIn("body", events[0])
        self.assertLessEqual(len(registry), ep.MAX_EVIDENCE_SPANS)
        self.assertTrue(all(len(span["quote"]) <= ep.MAX_SPAN_CHARS for span in registry.values()))
        self.assertEqual(coverage["omitted_source_chars"], 0)
        self.assertEqual(coverage["provided_source_chars"], len(body))
        self.assertGreater(coverage["merged_span_count"], 0)
        self.assertGreater(coverage["fragment_span_count"], 0)
        self.assertIn("LATE_SENTINEL", events[0]["spans"][-1]["text"])
        cursor = 0
        for span in registry.values():
            self.assertEqual(span["start"], cursor)
            self.assertEqual(body[span["start"]:span["end"]], span["quote"])
            cursor = span["end"]
        self.assertEqual(cursor, len(body))
        self.assertEqual(ep._span_catalog(raw), (events, registry, coverage))
        shifted = deepcopy(raw)
        shifted["sources"][0]["body_offset"] += 1
        self.assertNotEqual(list(ep._span_catalog(shifted)[1]), list(registry))

    def test_span_refs_preserve_repeated_sentences_and_punctuation_without_transcription(self):
        raw = snapshot(self.project)
        raw["sources"][0]["body"] = "The check passed locally. The check passed locally."
        events, registry, _ = ep._span_catalog(raw)
        self.assertEqual(len(registry), 2)
        refs = list(registry)
        item = episode()
        item["evidence"]["summary"] = [refs[1]]
        wire = {"episodes": [item], "claims": [], "relations": []}
        canonical = ep._resolve_evidence_refs(wire, raw)
        span = canonical["episodes"][0]["evidence"]["summary"][0]
        self.assertEqual(span, registry[refs[1]])
        self.assertTrue(span["quote"].endswith("."))
        self.assertGreater(span["start"], 0)
        ep._validate_output(ep._resolve_quotes(canonical, raw), raw)
        self.assertEqual(wire["episodes"][0]["evidence"]["summary"], [refs[1]])
        wire["episodes"][0]["evidence"]["summary"] = ["r-forged"]
        with self.assertRaises(ValueError):
            ep._resolve_evidence_refs(wire, raw)

    def test_native_runner_passes_shared_ref_definition_without_observation_unwrapping(self):
        from codex_mem import processor
        request = ep.runner_request(snapshot(self.project), 10)
        try:
            import jsonschema
        except ImportError:
            jsonschema = None
        if jsonschema is not None:
            jsonschema.Draft202012Validator.check_schema(request["output_schema"])
        client = mock.Mock()
        responses = {"initialize": {}, "account/read": {"account": {"type": "apiKey"}, "requiresOpenaiAuth": True}, "config/read": {"config": {}},
                     "model/list": {"data": [{"id": ep.MODEL, "model": ep.MODEL, "supportedReasoningEfforts": [{"reasoningEffort": ep.REASONING_EFFORT}]}]},
                     "thread/start": {"thread": {"id": "worker"}, "model": ep.MODEL,
                                      "reasoningEffort": ep.REASONING_EFFORT, "modelProvider": "openai"},
                     "mcpServerStatus/list": {"data": []}, "turn/start": {"turn": {"id": "turn"}}}
        client.request.side_effect = lambda method, *args, **kwargs: responses[method]
        with mock.patch.object(processor, "_AppServer", return_value=client), \
                mock.patch.object(processor.shutil, "which", return_value="codex"), \
                mock.patch.object(processor, "_wait_for_turn_completion"), \
                mock.patch.object(processor, "_read_valid_final_output", return_value={"episodes": [], "claims": [], "relations": []}):
            returned = ep.NativeProcessorRunner().run(request)
        self.assertEqual(returned["output"]["episodes"], [])
        sent_schema = next(call.args[1]["outputSchema"] for call in client.request.call_args_list if call.args[0] == "turn/start")
        self.assertEqual(sent_schema, request["output_schema"])
        self.assertIn("evidence_ref", sent_schema["$defs"])

    def test_native_schema_has_no_object_keys_and_no_initial_relation_endpoints(self):
        raw = snapshot(self.project)
        raw["state"]["claims"] = []
        schema = ep.runner_request(raw, 10)["output_schema"]["properties"]
        self.assertNotIn("key", schema["episodes"]["items"]["properties"])
        self.assertNotIn("key", schema["claims"]["items"]["properties"])
        self.assertNotIn("relations", schema)
        self.assertEqual(schema["claims"]["items"]["properties"]["outgoing_relations"],
                         {"type": "array", "items": {"type": "null"}, "maxItems": 0})
        self.assertIn("raw source IDs in relation endpoints", ep.runner_request(raw, 10)["prompt"])

    def test_native_claim_ownership_binds_only_returned_claims_and_existing_targets(self):
        raw = snapshot(self.project)
        item = episode()
        del item["key"]
        span = {"source_id": "event-1", "quote": "Use local import."}
        claim = {"text": "Use local import.", "kind": "task", "workflow_state": "open",
                 "scope": {"environment": "", "version": "", "worktree": ""}, "rationale": "",
                 "evidence": {"text": [span], "workflow_state": [span], "rationale": []}}
        claim["outgoing_relations"] = [{"type": "contradicts", "to_claim_id": "old-claim", "evidence": [span]}]
        wire = {"episodes": [item], "claims": [claim]}
        resolved = ep._resolve_object_references(wire, raw)
        self.assertEqual(resolved["episodes"][0]["key"], "episode_0")
        self.assertEqual(resolved["claims"][0]["key"], "claim_0")
        self.assertEqual(resolved["claims"][0]["text"], "Use local import.")
        self.assertEqual(resolved["relations"][0]["from"], "claim_0")
        self.assertEqual(resolved["relations"][0]["to"], "old-claim")
        ep._validate_output(ep._resolve_quotes(resolved, raw), raw)
        self.assertNotIn("key", wire["claims"][0])
        self.assertNotIn("from", wire["claims"][0]["outgoing_relations"][0])
        self.assertNotIn("outgoing_relations", resolved["claims"][0])

    def test_native_relation_rejects_source_ids_absent_indices_and_foreign_targets(self):
        raw = snapshot(self.project)
        base = {"episodes": [], "claims": [{"key": "kept-canonical-key"}], "relations": []}
        for source_index, target, kind in (("event-1", "old-claim", "contradicts"),
                                          (True, "old-claim", "closes"), (1, "old-claim", "closes"),
                                          (0, "event-1", "closes"), (0, "foreign-claim", "closes"),
                                          (0, "old-claim", "supports")):
            value = deepcopy(base)
            value["relations"] = [{"type": kind, "from_claim_index": source_index, "to_claim_id": target, "evidence": []}]
            with self.subTest(source_index=source_index,target=target,kind=kind), self.assertRaises(ValueError):
                ep._resolve_object_references(value, raw)
        canonical = deepcopy(base)
        canonical["relations"] = [{"type": "supports", "from": "event-1", "to": "kept-canonical-key", "evidence": []}]
        unchanged = ep._resolve_object_references(canonical, raw)
        self.assertEqual(unchanged, canonical)

    def test_foreign_source_id_is_rejected_without_repairs(self):
        raw = snapshot(self.project)
        item = episode()
        item["evidence"]["summary"][0]["source_id"] = "foreign-event"
        output = {"episodes": [item], "claims": [], "relations": []}
        resolved = ep._resolve_quotes(output, raw)
        self.assertEqual(resolved["episodes"][0]["evidence"]["summary"][0]["source_id"], "foreign-event")
        with self.assertRaises(ValueError):
            ep._validate_output(resolved, raw)
        item["evidence"]["summary"][0] = {"source_id": "foreign-event", "quote": "Use local import."}
        with self.assertRaises(ValueError):
            ep._resolve_quotes(output, raw)

    def test_unsupported_optional_goal_is_omitted_and_recorded_without_losing_summary(self):
        raw = snapshot(self.project)
        item = episode()
        item["goal"] = "Invented investigation goal."
        output = {"episodes": [item], "claims": [], "relations": []}
        normalized, omitted = ep._omit_unsupported_optional_fields(output)
        self.assertEqual(normalized["episodes"][0]["goal"], "")
        self.assertEqual(output["episodes"][0]["goal"], "Invented investigation goal.")
        self.assertEqual(omitted, {"omitted_fields_count": 1, "omitted_fields_reason": "missing_optional_evidence", "omitted_fields": "episode.goal"})
        ep._validate_output(normalized, raw)
        result = self.run_job(lambda request: receipt(output))
        self.assertEqual(result["status"], "processed")
        self.assertEqual(result["omitted_fields_count"], 1)
        saved_output = self.memory.commit.call_args.args[1]
        self.assertEqual(saved_output["episodes"][0]["goal"], "")
        self.assertTrue(self.memory.commit.call_args.kwargs["conservative_transitions"])

    def test_optional_forged_quote_is_rejected_instead_of_omitted(self):
        item = episode()
        item["goal"] = "Invented investigation goal."
        item["evidence"]["goal"] = [{"source_id": "event-1", "start": 0, "end": 17, "quote": "Invented quotation"}]
        output = {"episodes": [item], "claims": [], "relations": []}
        result = self.run_job(lambda request: receipt(output))
        self.assertEqual(result["code"], "invalid_response")
        self.assertNotIn("omitted_fields_count", result)
        self.memory.commit.assert_not_called()

    def test_omitted_transition_receipt_preserves_pending_reconciliation(self):
        self.memory.commit.return_value = {"status": "committed", "omitted_transitions_count": 1,
                                          "omitted_transitions_reason": "unproved_transition", "pending_reconciliation": True}
        result = self.run_job()
        self.assertEqual(result["status"], "processed")
        self.assertEqual(result["omitted_transitions_count"], 1)
        self.assertTrue(result["pending_reconciliation"])

    def test_downgraded_state_receipt_is_visible_without_claiming_completion(self):
        self.memory.commit.return_value = {"status": "committed", "downgraded_states_count": 1,
                                          "downgraded_states_reason": "unproved_completion", "pending_reconciliation": True}
        result = self.run_job()
        self.assertEqual(result["status"], "processed")
        self.assertEqual(result["downgraded_states_count"], 1)
        self.assertEqual(result["downgraded_states_reason"], "unproved_completion")
        self.assertTrue(result["pending_reconciliation"])
        self.assertIn("are proposed facts, not completed actions", ep.runner_request(snapshot(self.project), 10)["prompt"])

    def test_required_evidence_arrays_cannot_be_empty_for_open_or_proposed_claims(self):
        raw = snapshot(self.project)
        span = {"source_id": "event-1", "start": 0, "end": 17, "quote": "Use local import."}
        for workflow_state in ("open", "proposed", "accepted", "completed"):
            claim = {"key": "task", "text": "Use local import.", "kind": "task", "workflow_state": workflow_state,
                     "scope": {"environment": "", "version": "", "worktree": ""}, "rationale": "",
                     "evidence": {"text": [span], "workflow_state": [], "rationale": []}}
            output = {"episodes": [], "claims": [claim], "relations": []}
            with self.subTest(workflow_state=workflow_state), self.assertRaises(ValueError):
                ep._validate_output(ep._resolve_quotes(output, raw), raw)
            self.assertEqual(output["claims"][0]["evidence"]["workflow_state"], [])
        item = episode()
        item["evidence"]["summary"] = []
        with self.assertRaises(ValueError):
            ep._validate_output({"episodes": [item], "claims": [], "relations": []}, raw)
        schema = ep.output_schema()
        claim_evidence = schema["properties"]["claims"]["items"]["properties"]["evidence"]["properties"]
        self.assertEqual(claim_evidence["text"]["minItems"], 1)
        self.assertEqual(claim_evidence["workflow_state"]["minItems"], 1)
        self.assertNotIn("minItems", claim_evidence["rationale"])

    def test_quality_resolves_deployment_target_against_generic_test_success(self):
        from codex_mem.jev_quality import JevQualityError, MODEL
        raw = snapshot(self.project)
        raw["sources"][0].update(body="Tests passed.", source="hook:PostToolUse", source_role="tool_record")
        scope = {"environment": "production", "version": "v2", "worktree": str(self.project)}
        raw["state"]["claims"] = [{"id": "deployment-target", "text": "Deploy importer to production.",
                                     "workflow_state": "open", "kind": "task", "scope": scope}]
        span = {"source_id": "event-1", "start": 0, "end": 13, "quote": "Tests passed."}
        output = {"episodes": [], "claims": [{"key": "local-pass", "text": "Tests passed.",
                    "kind": "fact", "workflow_state": "completed", "scope": scope, "rationale": "",
                    "evidence": {"text": [span], "workflow_state": [span], "rationale": []}}],
                  "relations": [{"from": "local-pass", "to": "deployment-target", "type": "closes", "evidence": [span]}]}
        ep._validate_output(output, raw)
        inspected = []
        def evaluate(payload):
            relations = [item for item in payload["state"]["items"]
                         if item["candidate"]["title"] == "Project memory relations"]
            self.assertEqual(len(relations), 1)
            item = relations[0]
            candidate = json.loads(item["candidate"]["body"])
            self.assertEqual(candidate["reference_only_endpoints"]["target"]["text"], "Deploy importer to production.")
            self.assertEqual(candidate["reference_only_endpoints"]["target"]["scope"], scope)
            self.assertEqual(candidate["reference_only_endpoints"]["origin"]["text"], "Tests passed.")
            self.assertEqual(item["cited_sources"][0]["body"], "Tests passed.")
            self.assertEqual(item["history_use"], "reference_resolution_only")
            self.assertTrue(any("Deploy importer" in source["body"] for source in item["session_history"]))
            inspected.append(True)
            return {"model": MODEL, "answers": {key: {"type": "noul", "noul": .01 if key.endswith("_grounded") else .99}
                                                  for key in payload["questions"]}}
        with self.assertRaises(JevQualityError):
            ep._quality(output, raw, self.settings, evaluate)
        self.assertEqual(inspected, [True])

    def test_quality_unknown_relation_identity_fails_before_remote_check(self):
        raw = snapshot(self.project)
        output = {"episodes": [], "claims": [], "relations": [{"from": "unknown", "to": "old-claim", "type": "closes", "evidence": []}]}
        evaluate = mock.Mock(spec=[])
        with self.assertRaises(ValueError):
            ep._quality(output, raw, self.settings, evaluate)
        evaluate.assert_not_called()


class EpisodeStorageIntegrationTests(unittest.TestCase):
    def test_native_claim_binds_trusted_project_scope_without_model_worktree(self):
        from codex_mem.config import configure
        from codex_mem.project_memory import ProjectMemory
        from codex_mem.store import Store
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary) / "project"
            project.mkdir()
            data = Path(temporary) / "memory"
            configure(data, capture_scope="all", project_memory_mode="shadow",
                      project_memory_projects=[str(project)])
            with Store(data) as store:
                store.remember(project, "Request", "Keep deployment open.",
                               source="hook:UserPromptSubmit", session_id="session")
            def run(request):
                supplied = json.loads(request["prompt"].split("\n", 1)[1])
                scope_schema = request["output_schema"]["properties"]["claims"]["items"]["properties"]["scope"]
                self.assertNotIn("worktree", scope_schema["properties"])
                refs = [supplied["events"][0]["spans"][0]["ref"]]
                claim = {"text": "Keep deployment open.", "kind": "requirement", "workflow_state": "open",
                         "scope": {"environment": "production", "version": ""}, "rationale": "",
                         "evidence": {"text": refs, "workflow_state": refs, "rationale": []}}
                return receipt({"episodes": [], "claims": [claim], "relations": []})
            result = ep.process_project_memory(project, data, runner=run)
            self.assertEqual(result["status"], "processed", result)
            with Store(data) as store:
                claim = ProjectMemory(store).snapshot(project)["claims"][0]
                self.assertEqual(claim["scope"], {"environment": "production", "version": "", "worktree": str(project.resolve())})

    def test_canonical_runner_scope_keys_and_states_obey_store_contract(self):
        from codex_mem.config import configure
        from codex_mem.project_memory import ProjectMemory
        from codex_mem.store import Store
        cases = (("canonical", "processed"), ("empty", "processed"),
                 ("foreign", "failed"), ("basename", "failed"),
                 ("bad-key", "failed"), ("bad-state", "failed"), ("duplicate-key", "failed"))
        for case, status in cases:
            with self.subTest(case=case), tempfile.TemporaryDirectory() as temporary:
                project = Path(temporary) / "project"
                project.mkdir()
                data = Path(temporary) / "memory"
                configure(data, capture_scope="all", project_memory_mode="shadow",
                          project_memory_projects=[str(project)])
                with Store(data) as store:
                    store.remember(project, "Request", "Keep deployment open.",
                                   source="hook:UserPromptSubmit", session_id="session")
                def run(request):
                    supplied = json.loads(request["prompt"].split("\n", 1)[1])
                    span = {"source_id": supplied["events"][0]["id"], "quote": "Keep deployment open.",
                            "start": 0, "end": 21}
                    worktree = {"empty": "", "foreign": "/foreign/project", "basename": "project"}.get(case, supplied["project"])
                    claim = {"key": "bad key" if case == "bad-key" else "canonical-claim",
                             "text": "Keep deployment open.", "kind": "requirement",
                             "workflow_state": "pending_review" if case == "bad-state" else "open",
                             "scope": {"environment": "production", "version": "", "worktree": worktree},
                             "rationale": "", "evidence": {"text": [span], "workflow_state": [span], "rationale": []}}
                    return receipt({"episodes": [], "claims": [claim, deepcopy(claim)] if case == "duplicate-key" else [claim],
                                    "relations": []})
                result = ep.process_project_memory(project, data, runner=run)
                self.assertEqual(result["status"], status, result)
                with Store(data) as store:
                    claims = ProjectMemory(store).snapshot(project)["claims"]
                    if status == "processed":
                        expected = "" if case == "empty" else str(project.resolve())
                        self.assertEqual(claims[0]["scope"]["worktree"], expected)
                    else:
                        self.assertEqual(result["code"], "invalid_response")
                        self.assertEqual(claims, [])

    def test_temp_store_roundtrip_and_invalid_output_do_not_lose_events(self):
        from codex_mem.config import configure
        from codex_mem.project_memory import ProjectMemory
        from codex_mem.store import Store
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary) / "project"
            project.mkdir()
            data = Path(temporary) / "memory"
            configure(data, capture_scope="all", project_memory_mode="shadow",
                      project_memory_projects=[str(project)])
            with Store(data) as store:
                source = store.remember(project, "Request", "Use local import.", source="hook:UserPromptSubmit", session_id="session")
            def run(request):
                supplied = json.loads(request["prompt"].split("\n", 1)[1])
                item = episode()
                item["goal"] = "Invented investigation goal."
                del item["key"]
                item["evidence"]["summary"][0]["source_id"] = supplied["events"][0]["id"]
                return receipt({"episodes": [item], "claims": [], "relations": []})
            result = ep.process_project_memory(project, data, runner=run)
            self.assertEqual(result["status"], "processed", result)
            with Store(data) as store:
                state = ProjectMemory(store).snapshot(project)
                self.assertEqual(state["episodes"][0]["summary"], "User intent: Use local import.")
                self.assertEqual(state["episodes"][0]["goal"], "")
                self.assertNotIn("Invented investigation", json.dumps(state))
                saved = json.loads(store._connection.execute("SELECT receipt_json FROM pm_jobs").fetchone()[0])
                self.assertEqual(saved["processing"]["input_tokens"], 100)
                self.assertEqual(saved["processing"]["omitted_fields_count"], 1)
                self.assertEqual(saved["processing"]["omitted_fields"], "episode.goal")
                self.assertEqual(saved["processing"]["output_schema_version"], ep.OUTPUT_SCHEMA_VERSION)
                store.remember(project, "Open work", "Deployment remains open.", source="hook:UserPromptSubmit", session_id="session")
            rejected = ep.process_project_memory(project, data, runner=lambda request: receipt({"episodes": [], "claims": [], "relations": [], "forged": True}))
            self.assertEqual(rejected["code"], "invalid_response")
            self.assertEqual(ep.process_project_memory(project, data, runner=lambda request: self.fail("quarantined output retried"))["status"], "idle")
            with Store(data) as store:
                self.assertEqual(store._connection.execute("SELECT COUNT(*) FROM pm_events WHERE done=0").fetchone()[0], 1)
                self.assertEqual(store._connection.execute("SELECT COUNT(*) FROM pm_jobs WHERE status='quarantined'").fetchone()[0], 1)

    def test_generator_cross_chat_continuation_and_named_correction_preserve_task_identity(self):
        from codex_mem.config import configure
        from codex_mem.project_memory import ProjectMemory
        from codex_mem.store import Store
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary) / "project"
            project.mkdir()
            data = Path(temporary) / "memory"
            configure(data, capture_scope="all", project_memory_mode="shadow", project_memory_projects=[str(project)])
            def ingest(text, session, goal, target=None, keep_requirement=False):
                with Store(data) as store:
                    store.remember(project, "User task", text, source="hook:UserPromptSubmit", session_id=session)
                def run(request):
                    supplied = json.loads(request["prompt"].split("\n", 1)[1])
                    refs = [span["ref"] for event in supplied["events"] for span in event["spans"]]
                    heads = {episode["id"]: episode for episode in supplied["current_state"]["episodes"]}
                    if target is not None:
                        self.assertIn(target, heads)
                    item = {"summary": text, "goal": goal, "rationale": "", "outcome": "", "open_work": "",
                            "continuation_of": target, "evidence": {"summary": refs, "goal": refs[:1], "rationale": [],
                            "outcome": [], "open_work": [], "continuation_of": refs[:1] if target else []}}
                    claims = []
                    if keep_requirement:
                        claims = [{"text": "Keep diagnostic redaction open.", "kind": "requirement", "workflow_state": "open",
                                   "scope": {"environment": "", "version": "", "worktree": ""}, "rationale": "",
                                   "evidence": {"text": refs[-1:], "workflow_state": refs[-1:], "rationale": []}}]
                    return receipt({"episodes": [item], "claims": claims, "relations": []})
                result = ep.process_project_memory(project, data, runner=run)
                self.assertEqual(result["status"], "processed", result)
                with Store(data) as store:
                    state = ProjectMemory(store).snapshot(project)
                current = next(episode for episode in state["episodes"] if episode["session_id"] == session)
                return result, current, state
            _, root, _ = ingest("TASK-123: Implement scheduled importer. Keep diagnostic redaction open.",
                                  "chat-one", "TASK-123 importer", keep_requirement=True)
            _, foil, _ = ingest("TASK-999: Review environment. This is unrelated to TASK-123.",
                                  "chat-foil", "TASK-999 environment")
            _, child, _ = ingest("Continue TASK-123: check revised importer implementation.",
                                   "chat-two", "TASK-123 importer", target=root["id"])
            self.assertEqual(child["logical_episode_id"], root["id"])
            self.assertEqual(child["parent_episode_id"], root["id"])
            self.assertEqual(child["episode_version"], 2)
            self.assertEqual(child["continuity_basis"], "model_inference")
            corrected, latest, state = ingest("Correction for TASK-123: the revised check still fails locally.",
                                               "chat-three", "TASK-123 importer", target=child["id"])
            self.assertEqual(corrected["omitted_continuities_count"], 0)
            self.assertEqual(latest["logical_episode_id"], root["id"])
            self.assertEqual(latest["parent_episode_id"], child["id"])
            self.assertEqual(latest["episode_version"], 3)
            self.assertEqual(len(state["episodes"]), 2)
            self.assertEqual(state["claims"][0]["workflow_state"], "open")
            self.assertEqual(next(episode for episode in state["episodes"] if episode["id"] == foil["id"])["episode_version"], 1)
            rejected, standalone, state = ingest("Correction for TASK-123: inspect another local check.",
                                                  "chat-four", "TASK-123 importer", target=foil["id"])
            self.assertEqual(rejected["omitted_continuities_count"], 1)
            self.assertIsNone(standalone["parent_episode_id"])
            self.assertEqual(standalone["episode_version"], 1)
            with Store(data) as store:
                old = ProjectMemory(store).get(project, [root["id"]])[0]
                self.assertEqual(old["summary"], "User intent: TASK-123: Implement scheduled importer.\nUser intent: Keep diagnostic redaction open.")
                self.assertFalse(old["is_current_head"])
                history = ProjectMemory(store).snapshot(project, include_episode_history=True)["episodes"]
                self.assertEqual(len(history), 5)

    def test_attempt_ledger_retains_failed_partial_cost_across_retry_and_source_deletion(self):
        from codex_mem.config import configure
        from codex_mem.project_memory import ProjectMemory
        from codex_mem.store import Store
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary) / "project"
            project.mkdir()
            data = Path(temporary) / "memory"
            configure(data, capture_scope="all", project_memory_mode="shadow", project_memory_projects=[str(project)])
            with Store(data) as store:
                source = store.remember(project, "Request", "Private fixture source body.", source="hook:UserPromptSubmit", session_id="session")
            def factory(**kwargs):
                def run(request):
                    kwargs["usage_checkpoint"]({"worker_thread_id": "worker-1", "worker_turn_id": "turn-1",
                        "metrics": {"duration_ms": 5, "usage": {"status": "partial", "source": "app_server_thread_total",
                                    "updates": 1, "tokens": {"input_tokens": 17}}}})
                    raise ep.ProcessorFailure("timeout")
                return run
            with mock.patch.object(ep, "NativeProcessorRunner", side_effect=factory):
                failed = ep.process_project_memory(project, data)
            self.assertEqual(failed["code"], "timeout")
            with Store(data) as store:
                memory = ProjectMemory(store)
                first = memory.attempts(project)
                self.assertEqual(len(first), 1)
                self.assertEqual(first[0]["outcome"], "failed")
                self.assertEqual(first[0]["usage_status"], "partial")
                self.assertEqual(first[0]["usage_source"], "app_server_thread_total")
                self.assertEqual(first[0]["input_tokens"], 17)
                self.assertIsNone(first[0]["output_tokens"])
                self.assertEqual(memory.retry_failed(project), 1)
            processed = ep.process_project_memory(project, data, runner=lambda request: receipt())
            self.assertEqual(processed["status"], "processed", processed)
            with Store(data) as store:
                memory = ProjectMemory(store)
                attempts = memory.attempts(project)
                self.assertEqual(len(attempts), 2)
                self.assertEqual({item["outcome"] for item in attempts}, {"processed", "failed"})
                self.assertEqual(sum(item["input_tokens"] for item in attempts), 117)
                self.assertEqual(len({item["attempt_id"] for item in attempts}), 2)
                self.assertEqual(len({item["claim_token"] for item in attempts}), 2)
                self.assertNotIn("Private fixture", json.dumps(attempts))
                store.forget(project, [source["id"]])
                self.assertEqual(len(memory.attempts(project)), 2)

    def test_native_span_ref_fragment_commits_absolute_original_source_offsets(self):
        from codex_mem.config import configure
        from codex_mem.project_memory import ProjectMemory
        from codex_mem.store import Store
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary) / "project"
            project.mkdir()
            data = Path(temporary) / "memory"
            configure(data, capture_scope="all", project_memory_mode="shadow", project_memory_projects=[str(project)])
            prefix, text = "Earlier context. " * 10, "The local check passed."
            with Store(data) as store:
                source = store.remember(project, "Check", prefix + text, source="hook:PostToolUse", session_id="session")
                store._connection.execute("UPDATE pm_events SET offset=? WHERE source_id=?", (len(prefix), source["id"]))
                store._connection.commit()
            def run(request):
                supplied = json.loads(request["prompt"].split("\n", 1)[1])
                self.assertNotIn("body", supplied["events"][0])
                span = supplied["events"][0]["spans"][0]
                self.assertEqual(span["text"], text)
                item = episode()
                del item["key"]
                item["summary"] = text
                item["evidence"]["summary"] = [span["ref"]]
                return receipt({"episodes": [item], "claims": [], "relations": []})
            result = ep.process_project_memory(project, data, runner=run)
            self.assertEqual(result["status"], "processed", result)
            with Store(data) as store:
                span = ProjectMemory(store).snapshot(project)["episodes"][0]["evidence"]["summary"][0]
                self.assertEqual((span["start"], span["end"]), (len(prefix), len(prefix) + len(text)))
                self.assertEqual(span["quote"], text)

    def test_unproved_completed_version_fact_is_saved_as_proposal_with_receipt(self):
        from codex_mem.config import configure
        from codex_mem.project_memory import ProjectMemory
        from codex_mem.store import Store
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary) / "project"
            project.mkdir()
            data = Path(temporary) / "memory"
            configure(data, capture_scope="all", project_memory_mode="shadow", project_memory_projects=[str(project)])
            text = "Version 2.1 is checked locally."
            with Store(data) as store:
                store.remember(project, "Version", text, source="hook:PostToolUse", session_id="session")
            def run(request):
                supplied = json.loads(request["prompt"].split("\n", 1)[1])
                span = {"source_id": supplied["events"][0]["id"], "quote": text}
                claim = {"key": "version", "text": text, "kind": "fact", "workflow_state": "completed",
                         "scope": {"environment": "local", "version": "2.1", "worktree": supplied["project"]}, "rationale": "",
                         "evidence": {"text": [span], "workflow_state": [span], "rationale": []}}
                return receipt({"episodes": [], "claims": [claim], "relations": []})
            result = ep.process_project_memory(project, data, runner=run)
            self.assertEqual(result["status"], "processed", result)
            self.assertEqual(result["downgraded_states_count"], 1)
            self.assertTrue(result["pending_reconciliation"])
            with Store(data) as store:
                claim = ProjectMemory(store).snapshot(project)["claims"][0]
                self.assertEqual(claim["workflow_state"], "proposed")
                self.assertEqual(claim["requested_state"], "completed")
                self.assertEqual(claim["source_role"], "model_inference")
                self.assertEqual(claim["evidence_basis"], "model_inference")

    def test_deleted_during_generation_is_not_sent_to_quality_or_committed(self):
        from codex_mem.config import configure
        from codex_mem.store import Store
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary) / "project"
            project.mkdir()
            data = Path(temporary) / "memory"
            configure(data, capture_scope="all", project_memory_mode="shadow", project_memory_projects=[str(project)])
            with Store(data) as store:
                source = store.remember(project, "Request", "Use local import.", source="hook:UserPromptSubmit", session_id="session")
            def run(request):
                with Store(data) as store:
                    store.forget(project, [source["id"]])
                return receipt()
            with mock.patch.object(ep, "_quality") as quality:
                result = ep.process_project_memory(project, data, runner=run)
            self.assertEqual(result["status"], "deferred", result)
            quality.assert_not_called()


if __name__ == "__main__":
    unittest.main()
