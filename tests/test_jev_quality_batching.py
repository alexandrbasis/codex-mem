"""Refinement batching preserves evidence, coverage and per-call accounting."""
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from codex_mem import jev_client
from codex_mem.jev_quality import MAX_QUALITY_PAYLOAD_BYTES, JevQualityError, quality_gate
from codex_mem.store import Store
from tests.test_jev_quality import claim, note, response


def many_fields():
    return note(observation={
        "type": "discovery", "subtitle": "Local result", "narrative": "Local results only.",
        "facts": [f"Fact {i}" for i in range(8)],
        "concepts": [f"Custom assertion {i}" for i in range(8)],
        "files_read": [f"read-{i}" for i in range(8)],
        "files_modified": [f"modified-{i}" for i in range(8)],
    })


class QualityBatchingTests(unittest.TestCase):
    def test_native_37_fields_keep_full_state_and_global_field_indexes(self):
        candidate = many_fields()
        current = claim(context=[{"id": "history", "source": "hook:Stop", "body": "Historical caveat"}],
                        project_context="Reference only")
        original = copy.deepcopy((candidate, current))
        calls = []
        def evaluate(payload):
            calls.append(payload)
            self.assertLessEqual(jev_client.payload_bytes(payload["state"], payload["questions"]),
                                 MAX_QUALITY_PAYLOAD_BYTES)
            self.assertLessEqual(len(payload["questions"]), 64)
            return response(payload, .5, .5) if len(calls) == 1 else response(payload)
        audit = quality_gate([candidate], None, current, project="/quality", evaluator=evaluate)
        self.assertGreater(len(calls), 2)
        self.assertTrue(all(payload["state"] == calls[0]["state"] for payload in calls))
        self.assertEqual(original, (candidate, current))
        keys = [key for payload in calls[1:] for key in payload["questions"]]
        self.assertEqual({f"item_0_field_{i}_{kind}" for i in range(37)
                          for kind in ("grounded", "overclaim")}, set(keys))
        self.assertEqual(74, len(keys))
        self.assertEqual(37, audit["counts"]["fields_evaluated"])
        self.assertEqual(1, audit["counts"]["accepted"])

    def test_v10_instruction_overhead_fits_after_question_batching(self):
        candidate = note(observation={"type": "discovery", "subtitle": "Result", "narrative": "Local result",
            "facts": [f"Result {i}" for i in range(8)], "concepts": ["how-it-works"],
            "files_read": ["a", "b", "c"]})
        calls = []
        def evaluate(payload):
            calls.append(payload)
            return response(payload, .5, .5) if len(calls) == 1 else response(payload)
        audit = quality_gate([candidate], None, claim({"id": "source", "source": "hook:PostToolUse",
            "body": "Local regression passed."}), project="/quality", evaluator=evaluate)
        self.assertEqual("accept", audit["route"])
        self.assertEqual(16, audit["counts"]["fields_evaluated"])
        self.assertGreater(len(calls), 2)

    def test_later_batch_rejection_cannot_accept_candidate(self):
        calls = []
        def evaluate(payload):
            calls.append(payload)
            return (response(payload, .5, .5) if len(calls) == 1 else
                    response(payload, .01, .99) if len(calls) == 3 else response(payload))
        with self.assertRaises(JevQualityError) as raised:
            quality_gate([many_fields()], None, claim(), project="/quality", evaluator=evaluate)
        self.assertEqual("jev_quality_rejected", raised.exception.code)
        self.assertEqual(0, raised.exception.audit["counts"]["accepted"])

    def test_each_paid_and_cached_batch_has_receipt_before_next_call(self):
        with tempfile.TemporaryDirectory() as directory, Store(Path(directory) / "memory") as store:
            calls = []
            def evaluate(payload):
                if calls:
                    rows = store._connection.execute("SELECT audit_json FROM jev_judgment_audits").fetchall()
                    self.assertEqual(len(calls), len(rows))
                calls.append(payload)
                return response(payload, .5, .5) if len(calls) == 1 else response(payload)
            first = quality_gate([many_fields()], None, claim(), project=directory, store=store, evaluator=evaluate)
            cached_calls = []
            original_evaluate = jev_client.evaluate
            def replay(**kwargs):
                rows = store._connection.execute("SELECT audit_json FROM jev_judgment_audits").fetchall()
                self.assertEqual(len(calls) + len(cached_calls), len(rows))
                cached_calls.append(kwargs)
                return original_evaluate(**kwargs)
            with mock.patch("codex_mem.jev_client.evaluate", side_effect=replay):
                second = quality_gate([many_fields()], None, claim(), project=directory, store=store, evaluator=evaluate)
            rows = store._connection.execute("SELECT audit_json FROM jev_judgment_audits ORDER BY id").fetchall()
            receipts = [json.loads(row[0]) for row in rows]
        self.assertGreater(len(calls), 2)
        self.assertEqual(len(calls) * 2, len(receipts))
        self.assertEqual(len(calls), first["counts"]["requests"])
        self.assertEqual(len(calls) * 200, first["usage"]["input_tokens"])
        self.assertEqual(len(calls), second["counts"]["cache_hits"])
        self.assertEqual(0, second["counts"]["requests"])
        self.assertEqual(0, second["usage"]["input_tokens"])

    def test_between_batches_source_revocation_or_deadline_preserves_paid_audit(self):
        for expired in (False, True):
            with self.subTest(expired=expired):
                available = True
                now = 0
                calls = []
                def evaluate(payload):
                    nonlocal available, now
                    calls.append(payload)
                    if len(calls) == 2:
                        if expired:
                            now = 20
                        else:
                            available = False
                    return response(payload, .5, .5) if len(calls) == 1 else response(payload)
                with mock.patch("codex_mem.jev_quality.monotonic", side_effect=lambda: now):
                    with self.assertRaises(JevQualityError) as raised:
                        quality_gate([many_fields()], None, claim(), project="/quality", evaluator=evaluate,
                                     source_guard=lambda: available)
                audit = raised.exception.audit
                self.assertEqual("jev_quality_unavailable", raised.exception.code)
                self.assertEqual(2, len(calls))
                self.assertEqual(2, audit["counts"]["requests"])
                self.assertEqual(400, audit["usage"]["input_tokens"])
                self.assertEqual(0, audit["counts"]["accepted"])
                self.assertEqual("jev_timeout" if expired else "jev_source_unavailable",
                                 audit["evaluations"][-1]["error_code"])

    def test_later_transport_failure_preserves_previous_calls_and_usage(self):
        calls = []
        def evaluate(payload):
            calls.append(payload)
            if len(calls) == 3:
                raise RuntimeError("transport failed")
            return response(payload, .5, .5) if len(calls) == 1 else response(payload)
        with self.assertRaises(JevQualityError) as raised:
            quality_gate([many_fields()], None, claim(), project="/quality", evaluator=evaluate)
        audit = raised.exception.audit
        self.assertEqual("jev_quality_unavailable", raised.exception.code)
        self.assertEqual(3, audit["counts"]["requests"])
        self.assertEqual(400, audit["usage"]["input_tokens"])
        self.assertEqual("partial", audit["usage_status"])
        self.assertEqual(0, audit["counts"]["accepted"])
        self.assertGreater(audit["counts"]["fields_evaluated"], 0)
        self.assertLess(audit["counts"]["fields_evaluated"], 37)
