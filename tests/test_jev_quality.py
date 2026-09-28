"""Deterministic quality policy and accounting tests; no live model claims."""
from __future__ import annotations

import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from codex_mem import jev_client
from codex_mem.jev_quality import JevQualityError, MAX_QUALITY_PAYLOAD_BYTES, MODEL, POLICY_VERSION, quality_gate
from codex_mem.store import Store


def response(payload, grounded=0.99, overclaim=0.01):
    return {"model": MODEL, "answers": {
        key: {"type": "noul", "noul": grounded if key.endswith("_grounded") else overclaim}
        for key in payload["questions"]
    }, "usage": {"input_tokens": 200, "output_tokens": 20}}


def note(source_id="source", **changes):
    return {"title": "Reported result", "body": "The assistant reported a local test pass.",
            "tags": ["reported"], "source_ids": [source_id], **changes}


def claim(*sources, **changes):
    return {"job_id": "job", "sources": list(sources) or [
        {"id": "source", "source": "hook:Stop", "title": "Result", "body": "Tests passed."}],
        "context": [], "project_context": "", **changes}


class JevQualityTests(unittest.TestCase):
    def test_complete_candidates_and_canonical_roles_share_one_request(self):
        sources = [
            {"id": "assistant", "source": "hook:Stop", "body": "PRIVATE_ASSISTANT reported a pass.",
             "evidence_role": "verified_fact"},
            {"id": "intent", "source": "hook:UserPromptSubmit", "body": "Please deploy."},
            {"id": "tool", "source": "hook:PostToolUse", "body": "Synthetic test: 3 passed.",
             "tool_io": {"input": "pytest", "response": "3 passed", "truncated": False}},
            {"id": "marker", "source": "hook:Stop", "body": "Session ended.", "_jev_lifecycle_only": True},
        ]
        history = [{"id": "old", "source": "processor:example", "body": "Reported prior result."}]
        current = claim(*sources, context=history, project_context="REFERENCE_ONLY_MAY_POSTDATE")
        candidate = note("tool", observation={"facts": ["The synthetic test passed."], "files_modified": []})
        summary = {"title": "Session", "request": "Please deploy.", "completed": "Reported local pass.",
                   "source_ids": ["assistant", "intent", "tool", "marker"]}
        before = copy.deepcopy((current, candidate, summary))
        calls = []

        def evaluate(payload):
            calls.append(payload)
            self.assertEqual(4, len(payload["questions"]))
            first, second = payload["state"]["items"]
            self.assertEqual(candidate, first["candidate"])
            self.assertEqual(candidate["title"], first["candidate_claims"]["title"])
            self.assertEqual(candidate["observation"], first["candidate_claims"]["observation"])
            self.assertNotIn("source_ids", first["candidate_claims"])
            self.assertNotIn("tags", first["candidate_claims"])
            self.assertNotIn("policy", payload["state"])
            self.assertEqual("reference_resolution_only", first["history_use"])
            self.assertEqual("session_summary_evidence", second["history_use"])
            self.assertEqual(first["session_history"], second["session_history"])
            self.assertEqual("PRIVATE_ASSISTANT reported a pass.", second["cited_sources"][0]["body"])
            self.assertEqual(["assistant_report", "user_intent", "tool_record", "lifecycle_marker"],
                             [source["evidence_role"] for source in second["cited_sources"]])
            self.assertEqual("derived_note", second["session_history"][0]["evidence_role"])
            self.assertEqual("reference_only", payload["state"]["project_reference"]["evidence_role"])
            self.assertEqual(sources[2]["tool_io"], first["cited_sources"][0]["tool_io"])
            for question in payload["questions"].values():
                criteria = json.dumps(question["criteria"])
                self.assertIn("assistant_report", criteria)
                self.assertIn("user_intent", criteria)
                self.assertIn("Check factual title text too", question["instructions"]["claim_scope"])
                self.assertIn("generic headings", question["instructions"]["claim_scope"])
            self.assertIn("synthetic", json.dumps(payload["questions"]))
            self.assertIn("future plan", json.dumps(payload["questions"]))
            return response(payload)

        audit = quality_gate([candidate], summary, current, project="/quality", evaluator=evaluate)
        self.assertEqual("accept", audit["route"])
        self.assertEqual(2, audit["counts"]["accepted"])
        self.assertEqual(1, len(calls))
        self.assertEqual(before, (current, candidate, summary))
        self.assertNotIn("PRIVATE_ASSISTANT", json.dumps(audit))
        self.assertNotIn("REFERENCE_ONLY_MAY_POSTDATE", json.dumps(audit))

    def test_refinement_checks_all_nonempty_fields_without_omitting_evidence(self):
        candidate = note(
            title="Decision: use chosen_key for future retries",
            body="The user chose chosen_key; implementation remains unverified.",
            tags=["STORAGE_TAG_ONLY"],
            observation={
                "type": "decision", "subtitle": "Chosen identifier; not implemented",
                "facts": ["The accepted identifier is chosen_key."],
                "narrative": "The choice is intended to avoid duplicate requests.",
                "concepts": ["trade-off"], "files_read": ["reviewed.py"],
                "files_modified": ["modified.py"],
            },
        )
        summary = {"title": "Reported session result", "request": None, "investigated": "",
                   "learned": "", "completed": "The assistant reported a local check pass.",
                   "next_steps": "", "notes": "Independent execution remains unverified.",
                   "source_ids": ["source"]}
        originals = copy.deepcopy((candidate, summary))
        seen = []
        def evaluate(payload):
            seen.append(payload)
            if len(seen) == 1:
                self.assertEqual(2, len(payload["state"]["items"]))
                return response(payload, .5, .5)
            item = payload["state"]["items"][0]
            original = candidate if item["candidate_kind"] == "note" else summary
            self.assertEqual(original, item["candidate"])
            self.assertEqual("Tests passed.", item["cited_sources"][0]["body"])
            self.assertEqual([], item["session_history"])
            self.assertEqual(18 if item["candidate_kind"] == "note" else 6, len(payload["questions"]))
            questions = json.dumps(payload["questions"])
            self.assertNotIn("candidate_claims.request", questions)
            self.assertNotIn("candidate_claims.next_steps", questions)
            for question in payload["questions"].values():
                self.assertIn("another field's attribution cannot weaken", question["instructions"]["field_scope"])
                self.assertIn("files_modified means edits; files_read means reads", question["instructions"]["field_scope"])
            return response(payload)
        audit = quality_gate([candidate], summary, claim(), project="/quality", evaluator=evaluate)
        self.assertEqual("accept", audit["route"])
        self.assertEqual(3, len(seen))
        self.assertEqual(2, audit["counts"]["initial_uncertain"])
        self.assertEqual(2, audit["counts"]["refined"])
        self.assertEqual(12, audit["counts"]["fields_evaluated"])
        self.assertEqual(2, audit["counts"]["accepted"])
        self.assertEqual(0, audit["counts"]["uncertain"])
        for decision in audit["decisions"]:
            self.assertEqual("uncertain", decision["initial"]["route"])
            self.assertEqual("refinement", decision["decision_source"])
        self.assertEqual({"title", "body", "observation.type", "observation.subtitle", "observation.facts",
                          "observation.narrative", "observation.concepts", "observation.files_read", "observation.files_modified"},
                         {field["field_path"] for field in audit["decisions"][0]["fields"]})
        self.assertEqual({"title", "completed", "notes"}, {field["field_path"] for field in audit["decisions"][1]["fields"]})
        self.assertEqual(originals, (candidate, summary))
        self.assertNotIn("chosen_key", json.dumps(audit))

    def test_note_can_resolve_that_key_from_history_but_cites_only_current_result(self):
        history = [{"id": "decision", "source": "hook:UserPromptSubmit",
                    "body": "Use checkout_id as the retry idempotency key."}]
        current = claim({"id": "run", "source": "hook:PostToolUse",
                         "body": "The local retry test passed with that key."}, context=history)
        candidate = note("run", title="Local retry test", body="The local retry test passed with checkout_id.")
        seen = []

        def evaluate(payload):
            item = payload["state"]["items"][0]
            seen.append(item)
            self.assertEqual(["run"], [row["id"] for row in item["cited_sources"]])
            self.assertEqual("The local retry test passed with that key.", item["cited_sources"][0]["body"])
            self.assertEqual("Use checkout_id as the retry idempotency key.", item["session_history"][0]["body"])
            self.assertEqual("user_intent", item["session_history"][0]["evidence_role"])
            self.assertEqual("reference_resolution_only", item["history_use"])
            for question in payload["questions"].values():
                self.assertIn("interpreted using `items[0].session_history` only to resolve references",
                              question["instructions"]["question"])
                self.assertIn("may only resolve unambiguous references", question["instructions"]["reference_resolution"])
                self.assertIn("current cited sources must support each new finding or completion",
                              question["instructions"]["reference_resolution"])
                self.assertEqual({"tool_record", "user_intent"}, set(question["instructions"]["source_role_rules"]))
            self.assertIn("history alone", payload["questions"]["item_0_grounded"]["criteria"]["false"])
            self.assertIn("without current result evidence", payload["questions"]["item_0_overclaim"]["criteria"]["true"])
            return response(payload)

        audit = quality_gate([candidate], None, current, project="/quality", evaluator=evaluate)
        self.assertEqual("accept", audit["route"])
        self.assertEqual(1, len(seen))
        with self.assertRaises(JevQualityError) as raised:
            quality_gate([note("decision")], None, current, project="/quality", evaluator=mock.Mock())
        self.assertEqual("jev_quality_unavailable", raised.exception.code)
        self.assertEqual(0, raised.exception.audit["counts"]["requests"])

    def test_probability_boundaries_are_independent(self):
        for grounded, overclaim, route in (
            (0.8, 0.2, "accept"), (0.2, 0.0, "rejected"), (1.0, 0.8, "rejected"),
            (0.79, 0.01, "uncertain"), (0.99, 0.21, "uncertain"), (0.5, 0.5, "uncertain"),
        ):
            with self.subTest(grounded=grounded, overclaim=overclaim):
                evaluator = lambda payload: response(payload, grounded, overclaim)
                if route == "accept":
                    audit = quality_gate([note()], None, claim(), project="/quality", evaluator=evaluator)
                else:
                    with self.assertRaises(JevQualityError) as raised:
                        quality_gate([note()], None, claim(), project="/quality", evaluator=evaluator)
                    audit = raised.exception.audit
                    self.assertEqual("jev_quality_" + route, raised.exception.code)
                self.assertEqual(route, audit["decisions"][0]["route"])
                self.assertEqual(2 if route == "uncertain" else 1, audit["counts"]["requests"])

    def test_clear_rejection_prevents_refinement_of_any_candidate(self):
        calls = []
        def evaluate(payload):
            calls.append(payload)
            result = response(payload, .5, .5)
            result["answers"]["item_1_overclaim"]["noul"] = .99
            return result
        with self.assertRaises(JevQualityError) as raised:
            quality_gate([note(), note(title="Unsupported completion")], None, claim(),
                         project="/quality", evaluator=evaluate)
        self.assertEqual("jev_quality_rejected", raised.exception.code)
        self.assertEqual(1, len(calls))
        self.assertEqual(0, raised.exception.audit["counts"]["refined"])

    def test_a_rejected_title_field_blocks_supported_body_without_another_stage(self):
        candidate = note(title="Production deployment completed", body="The user requested a deployment; none was verified.")
        calls = []
        def evaluate(payload):
            calls.append(payload)
            if len(calls) == 1:
                return response(payload, .5, .5)
            result = response(payload)
            for key, question in payload["questions"].items():
                if "candidate_claims.title`" in question["instructions"]["question"]:
                    result["answers"][key]["noul"] = .01 if key.endswith("_grounded") else .99
            return result
        with self.assertRaises(JevQualityError) as raised:
            quality_gate([candidate], None, claim(), project="/quality", evaluator=evaluate)
        audit = raised.exception.audit
        self.assertEqual("jev_quality_rejected", raised.exception.code)
        self.assertEqual(2, len(calls))
        self.assertEqual("uncertain", audit["decisions"][0]["initial"]["route"])
        self.assertEqual({"title": "rejected", "body": "accept"},
                         {field["field_path"]: field["route"] for field in audit["decisions"][0]["fields"]})
        self.assertEqual(["aggregate", "refinement"], [entry["stage"] for entry in audit["evaluations"]])

    def test_refinement_preflight_rejects_full_payload_and_question_overflow(self):
        candidates = (
            (note(observation={"type": "decision", "subtitle": "Choice", "facts": ["Choice was stated."],
                              "narrative": "Reason", "concepts": ["trade-off"], "files_read": ["a"], "files_modified": ["b"]}),
             claim({"id": "source", "source": "hook:Stop", "body": "x" * 80_000})),
            (note(**{f"field_{index}": "value" for index in range(33)}), claim()),
        )
        for candidate, current in candidates:
            with self.subTest(fields=len(candidate)):
                evaluator = mock.Mock(side_effect=lambda payload: response(payload, .5, .5))
                with self.assertRaises(JevQualityError) as raised:
                    quality_gate([candidate], None, current, project="/quality", evaluator=evaluator)
                self.assertEqual("jev_quality_input_limit", raised.exception.code)
                evaluator.assert_called_once()
                self.assertEqual(1, raised.exception.audit["counts"]["requests"])
                self.assertEqual(0, raised.exception.audit["counts"]["refined"])
                self.assertEqual("refinement", raised.exception.audit["evaluations"][-1]["stage"])

    def test_failed_refinement_preserves_aggregate_usage_and_initial_uncertainty(self):
        calls = []
        def evaluate(payload):
            calls.append(payload)
            if len(calls) == 2:
                raise RuntimeError("PRIVATE_REFINEMENT_ERROR")
            return response(payload, .5, .5)
        with tempfile.TemporaryDirectory() as directory, Store(Path(directory) / "memory") as store:
            with self.assertRaises(JevQualityError) as raised:
                quality_gate([note()], None, claim(), project=directory, store=store, evaluator=evaluate)
            rows = store._connection.execute("SELECT audit_json FROM jev_judgment_audits ORDER BY id").fetchall()
        audit = raised.exception.audit
        self.assertEqual("jev_quality_unavailable", raised.exception.code)
        self.assertEqual(2, audit["counts"]["requests"])
        self.assertEqual(200, audit["usage"]["input_tokens"])
        self.assertEqual("partial", audit["usage_status"])
        self.assertEqual("uncertain", audit["decisions"][0]["initial"]["route"])
        self.assertEqual(["quality_uncertain", "quality_refinement_unavailable"],
                         [json.loads(row[0])["route"] for row in rows])
        self.assertNotIn("PRIVATE_REFINEMENT_ERROR", json.dumps(audit))

    def test_refinement_cannot_extend_deadline_or_bypass_revocation(self):
        for expired in (True, False):
            with self.subTest(expired=expired):
                available = True
                def evaluate(payload):
                    nonlocal available
                    if not expired:
                        available = False
                    return response(payload, .5, .5)
                evaluator = mock.Mock(side_effect=evaluate)
                with mock.patch("codex_mem.jev_quality.monotonic", side_effect=[0, 0, 19, 20]):
                    with self.assertRaises(JevQualityError) as raised:
                        quality_gate([note()], None, claim(), project="/quality", evaluator=evaluator,
                                     source_guard=lambda: available)
                evaluator.assert_called_once()
                self.assertEqual("jev_quality_unavailable", raised.exception.code)
                self.assertEqual(1, raised.exception.audit["counts"]["requests"])
                self.assertEqual(0, raised.exception.audit["counts"]["refined"])

    def test_cached_refinement_replay_has_two_receipts_and_no_new_usage(self):
        def evaluate(payload):
            return response(payload, .99, .01) if any("_field_" in key for key in payload["questions"]) else response(payload, .5, .5)
        evaluator = mock.Mock(side_effect=evaluate)
        with tempfile.TemporaryDirectory() as directory, Store(Path(directory) / "memory") as store:
            first = quality_gate([note()], None, claim(), project=directory, store=store, evaluator=evaluator)
            second = quality_gate([note()], None, claim(), project=directory, store=store, evaluator=evaluator)
        self.assertEqual(2, evaluator.call_count)
        self.assertEqual(2, first["counts"]["requests"])
        self.assertEqual({"input_tokens": 400, "output_tokens": 40}, first["usage"])
        self.assertEqual(0, second["counts"]["requests"])
        self.assertEqual(2, second["counts"]["cache_hits"])
        self.assertEqual({"input_tokens": 0, "output_tokens": 0}, second["usage"])
        self.assertEqual("uncertain", second["decisions"][0]["initial"]["route"])
        self.assertEqual("accept", second["decisions"][0]["route"])

    def test_assistant_only_evidence_gets_explicit_independent_tool_boundary(self):
        candidate = note(body="Independent tool output confirmed the cache fix and passing tests.")
        inspected = []
        def evaluate(payload):
            for question in payload["questions"].values():
                roles = question["instructions"]["source_role_rules"]
                inspected.append(roles)
                self.assertEqual({"assistant_report"}, set(roles))
                self.assertIn("not independent tool output", roles["assistant_report"])
                self.assertIn("unless an actual tool_record also supports that result", roles["assistant_report"])
            return response(payload, grounded=.01, overclaim=.99)
        with self.assertRaises(JevQualityError) as raised:
            quality_gate([candidate], None, claim(), project="/quality", evaluator=evaluate)
        self.assertEqual("jev_quality_rejected", raised.exception.code)
        self.assertEqual(2, len(inspected))

    def test_refinement_preserves_summary_attribution_and_does_not_repair_another_field(self):
        for attributed in (True, False):
            with self.subTest(attributed=attributed):
                summary = {"title": "Session summary", "completed": (
                    "The assistant reported updating the service and passing checks."
                    if attributed else "The service update and passing checks are independently verified."
                ), "notes": "No independent command output is recorded.", "source_ids": ["source"]}
                seen = []
                def evaluate(payload):
                    item = payload["state"]["items"][0]
                    self.assertEqual(summary, item["candidate"])
                    self.assertEqual(summary["completed"], item["candidate_claims"]["completed"])
                    seen.append(payload)
                    for question in payload["questions"].values():
                        self.assertNotIn("summary_fields", question["instructions"])
                    if len(seen) == 1:
                        return response(payload, .5, .5)
                    return response(payload, grounded=.99 if attributed else .01, overclaim=.01 if attributed else .99)
                if attributed:
                    audit = quality_gate([], summary, claim(), project="/quality", evaluator=evaluate)
                    self.assertEqual("accept", audit["route"])
                else:
                    with self.assertRaises(JevQualityError) as raised:
                        quality_gate([], summary, claim(), project="/quality", evaluator=evaluate)
                    self.assertEqual("jev_quality_rejected", raised.exception.code)
                self.assertEqual(2, len(seen))

    def test_uncertain_summary_rejects_whole_result_with_accepted_note(self):
        def evaluate(payload):
            result = response(payload)
            for key, answer in result["answers"].items():
                if key.startswith("item_1_") and key.endswith("_grounded"):
                    answer["noul"] = 0.5
            return result
        with self.assertRaises(JevQualityError) as raised:
            quality_gate([note()], {"title": "Summary", "source_ids": ["source"]}, claim(),
                         project="/quality", evaluator=evaluate)
        self.assertEqual("jev_quality_uncertain", raised.exception.code)
        self.assertEqual(1, raised.exception.audit["counts"]["accepted"])
        self.assertEqual(1, raised.exception.audit["counts"]["uncertain"])
        self.assertEqual(2, raised.exception.audit["counts"]["requests"])

    def test_missing_cited_evidence_is_unavailable_without_model_call(self):
        evaluator = mock.Mock()
        with self.assertRaises(JevQualityError) as raised:
            quality_gate([note("missing")], None, claim(), project="/quality", evaluator=evaluator)
        self.assertEqual("jev_quality_unavailable", raised.exception.code)
        self.assertEqual(0, raised.exception.audit["counts"]["requests"])
        evaluator.assert_not_called()

    def test_large_complete_quality_and_refinement_requests_use_explicit_higher_cap(self):
        retained_body = "FULL_START_" + "я" * 20_000 + "_FULL_END"
        current = claim({"id": "source", "source": "hook:Stop", "body": retained_body},
                        context=[{"id": "history", "source": "hook:UserPromptSubmit", "body": "Earlier plan remains a plan."}])
        original = copy.deepcopy(current)
        sizes = []
        def evaluate(payload):
            item = payload["state"]["items"][0]
            self.assertEqual(retained_body, item["cited_sources"][0]["body"])
            self.assertEqual(current["context"][0]["body"], item["session_history"][0]["body"])
            size = jev_client.payload_bytes(payload["state"], payload["questions"])
            sizes.append(size)
            self.assertGreater(size, jev_client.MAX_PAYLOAD_BYTES)
            self.assertLessEqual(size, MAX_QUALITY_PAYLOAD_BYTES)
            return response(payload, .5, .1) if len(sizes) == 1 else response(payload)
        with mock.patch.object(jev_client, "evaluate", wraps=jev_client.evaluate) as client:
            audit = quality_gate([note()], None, current, project="/quality", evaluator=evaluate)
        self.assertEqual("accept", audit["route"])
        self.assertEqual(2, audit["counts"]["requests"])
        self.assertEqual(1, audit["counts"]["refined"])
        self.assertEqual([MAX_QUALITY_PAYLOAD_BYTES, MAX_QUALITY_PAYLOAD_BYTES],
                         [call.kwargs["max_payload_bytes"] for call in client.call_args_list])
        self.assertEqual(24_000, jev_client.MAX_PAYLOAD_BYTES)
        self.assertEqual(96_000, MAX_QUALITY_PAYLOAD_BYTES)
        self.assertEqual(original, current)

    def test_quality_payload_limit_counts_utf8_bytes_without_clipping(self):
        current = claim({"id": "source", "body": "я" * (MAX_QUALITY_PAYLOAD_BYTES // 2)})
        evaluator = mock.Mock()
        with self.assertRaises(JevQualityError) as raised:
            quality_gate([note()], None, current, project="/quality", evaluator=evaluator)
        self.assertEqual("jev_quality_input_limit", raised.exception.code)
        self.assertEqual(0, raised.exception.audit["counts"]["requests"])
        self.assertEqual(MAX_QUALITY_PAYLOAD_BYTES, len(current["sources"][0]["body"].encode("utf-8")))
        evaluator.assert_not_called()

    def test_oversized_later_item_fails_before_any_call_without_clipping(self):
        current = claim({"id": "small", "body": "Small evidence."},
                        {"id": "big", "body": "FULL_START" + "x" * MAX_QUALITY_PAYLOAD_BYTES + "FULL_END"})
        original = copy.deepcopy(current)
        evaluator = mock.Mock()
        with self.assertRaises(JevQualityError) as raised:
            quality_gate([note("small"), note("big")], None, current, project="/quality", evaluator=evaluator)
        self.assertEqual("jev_quality_input_limit", raised.exception.code)
        self.assertEqual(0, raised.exception.audit["counts"]["requests"])
        self.assertEqual(original, current)
        evaluator.assert_not_called()

    def test_oversized_summary_history_is_quarantined_in_full(self):
        history = [{"id": "history", "source": "processor:example",
                    "body": "BEGIN_RETAINED_HISTORY" + "h" * MAX_QUALITY_PAYLOAD_BYTES + "END_RETAINED_HISTORY"}]
        current = claim(context=history)
        original = copy.deepcopy(current)
        evaluator = mock.Mock()
        with self.assertRaises(JevQualityError) as raised:
            quality_gate([], {"title": "Summary", "source_ids": ["source"]}, current,
                         project="/quality", evaluator=evaluator)
        self.assertEqual("jev_quality_input_limit", raised.exception.code)
        self.assertEqual(original, current)
        evaluator.assert_not_called()

    @staticmethod
    def split_items():
        current = claim({"id": "one", "body": "one" + "x" * (MAX_QUALITY_PAYLOAD_BYTES // 2)},
                        {"id": "two", "body": "two" + "y" * (MAX_QUALITY_PAYLOAD_BYTES // 2)})
        return [note("one"), note("two")], current

    def test_later_transport_failure_preserves_earlier_usage_and_both_receipts(self):
        notes, current = self.split_items()
        calls = []
        def evaluate(payload):
            calls.append(payload)
            if len(calls) == 2:
                raise RuntimeError("PRIVATE_TRANSPORT_DETAIL")
            return response(payload)
        with tempfile.TemporaryDirectory() as directory, Store(Path(directory) / "memory") as store:
            with mock.patch.object(jev_client, "record_audit") as record:
                with self.assertRaises(JevQualityError) as raised:
                    quality_gate(notes, None, current, project=directory, store=store, evaluator=evaluate)
        audit = raised.exception.audit
        self.assertEqual("jev_quality_unavailable", raised.exception.code)
        self.assertEqual(2, audit["counts"]["requests"])
        self.assertEqual({"input_tokens": 200, "output_tokens": 20}, audit["usage"])
        self.assertEqual("partial", audit["usage_status"])
        self.assertEqual(2, record.call_count)
        self.assertEqual(["quality_accept", "quality_unavailable"], [call.kwargs["route"] for call in record.call_args_list])
        self.assertNotIn("PRIVATE_TRANSPORT_DETAIL", json.dumps(audit))
        self.assertEqual("item_1_grounded", next(iter(calls[1]["questions"])))
        self.assertIn("items[0]", calls[1]["questions"]["item_1_grounded"]["instructions"]["question"])

    def test_gate_uses_remaining_deadline_across_calls_and_refuses_late_success(self):
        notes, current = self.split_items()
        timeouts = []
        def evaluate(**kwargs):
            timeouts.append(kwargs["timeout"])
            answers = response({"questions": kwargs["questions"]})["answers"]
            return answers, {"model": MODEL, "policy_version": POLICY_VERSION, "status": "success",
                             "evaluation_source": "live", "counts": {"requests": 1, "cache_hits": 0},
                             "usage": {"input_tokens": 200, "output_tokens": 20}, "usage_status": "reported",
                             "duration_ms": 1, "answers": answers}
        with mock.patch("codex_mem.jev_quality.monotonic", side_effect=[0, 0, 7, 8, 21]), \
                mock.patch.object(jev_client, "evaluate", side_effect=evaluate):
            with self.assertRaises(JevQualityError) as raised:
                quality_gate(notes, None, current, project="/quality", timeout=999)
        self.assertEqual([20, 12], timeouts)
        self.assertEqual("jev_quality_unavailable", raised.exception.code)
        self.assertEqual(2, raised.exception.audit["counts"]["requests"])
        self.assertEqual(400, raised.exception.audit["usage"]["input_tokens"])

    def test_empty_output_skips_without_calls_or_receipts(self):
        evaluator = mock.Mock()
        with mock.patch.object(jev_client, "record_audit") as record:
            audit = quality_gate([], None, claim(), project="/quality", evaluator=evaluator)
        self.assertEqual("skipped", audit["status"])
        self.assertEqual(0, audit["counts"]["requests"])
        evaluator.assert_not_called()
        record.assert_not_called()

    def test_revoked_sources_stop_before_any_call(self):
        evaluator = mock.Mock()
        with self.assertRaises(JevQualityError) as raised:
            quality_gate([note()], None, claim(), project="/quality", evaluator=evaluator,
                         source_guard=lambda: False)
        self.assertEqual("jev_quality_unavailable", raised.exception.code)
        self.assertEqual(0, raised.exception.audit["counts"]["requests"])
        self.assertEqual("jev_source_unavailable", raised.exception.audit["evaluations"][0]["error_code"])
        evaluator.assert_not_called()

    def test_revoked_during_call_blocks_remaining_items_and_preserves_usage(self):
        notes, current = self.split_items()
        available = True
        calls = []
        def evaluate(payload):
            nonlocal available
            calls.append(payload)
            available = False
            return response(payload)
        with self.assertRaises(JevQualityError) as raised:
            quality_gate(notes, None, current, project="/quality", evaluator=evaluate,
                         source_guard=lambda: available)
        self.assertEqual("jev_quality_unavailable", raised.exception.code)
        self.assertEqual(1, len(calls))
        self.assertEqual(1, raised.exception.audit["counts"]["requests"])
        self.assertEqual(200, raised.exception.audit["usage"]["input_tokens"])
        self.assertEqual(2, len(raised.exception.audit["evaluations"]))

    def test_expired_deadline_before_first_call_is_unavailable(self):
        evaluator = mock.Mock()
        with mock.patch("codex_mem.jev_quality.monotonic", side_effect=[0, 20]):
            with self.assertRaises(JevQualityError) as raised:
                quality_gate([note()], None, claim(), project="/quality", evaluator=evaluator)
        self.assertEqual("jev_quality_unavailable", raised.exception.code)
        self.assertEqual(0, raised.exception.audit["counts"]["requests"])
        evaluator.assert_not_called()

    def test_exact_cached_replay_records_no_new_usage(self):
        evaluator = mock.Mock(side_effect=response)
        with tempfile.TemporaryDirectory() as directory, Store(Path(directory) / "memory") as store:
            with mock.patch.object(jev_client, "record_audit"):
                first = quality_gate([note()], None, claim(), project=directory, store=store, evaluator=evaluator)
                second = quality_gate([note()], None, claim(), project=directory, store=store, evaluator=evaluator)
        self.assertEqual(1, first["counts"]["requests"])
        self.assertEqual(1, second["counts"]["cache_hits"])
        self.assertEqual(0, second["counts"]["requests"])
        self.assertEqual({"input_tokens": 0, "output_tokens": 0}, second["usage"])
        evaluator.assert_called_once()


if __name__ == "__main__":
    unittest.main()
