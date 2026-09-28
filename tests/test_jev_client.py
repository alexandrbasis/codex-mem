"""Typed contracts, privacy, cache invalidation and bounded provider failures."""
from copy import deepcopy
import json
from pathlib import Path
import sqlite3
import tempfile
import time
import unittest
from unittest.mock import patch

from codex_mem import jev_client as client
from codex_mem.store import Store


QUESTIONS = {
    "support": {"type": "noul", "instructions": "Does the evidence support the statement?"},
    "relation": {"type": "choice", "instructions": "How does the evidence relate to the statement?",
                 "criteria": {"supports": "Supported", "contradicts": "Contradicted", "unknown": "Absent"}},
    "relevance": {"type": "score", "instructions": "How directly does the record answer the query?",
                  "criteria": ["Unrelated", "Partial context", "Direct answer"]},
}


def response(payload):
    return {"model": client.MODEL, "answers": {
        "support": {"type": "noul", "noul": .9},
        "relation": {"type": "choice", "choice": "supports", "confidence": .8,
                     "probabilities": {"supports": .9, "contradicts": .05, "unknown": .05}},
        "relevance": {"type": "score", "score": 1.8, "confidence": .7,
                      "legend": {str(i): value for i, value in enumerate(payload["questions"]["relevance"]["criteria"])},
                      "probabilities": {"0": 0, "1": .2, "2": .8}}},
        "usage": {"input_tokens": 123, "output_tokens": 9}}


class JevClientTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Store(self.tmp.name)
        self.addCleanup(self.store.close)
        self.project = str(Path(self.tmp.name) / "project")

    def evaluate(self, **overrides):
        arguments = dict(state={"query": "How did we resolve duplicate charges?"},
                         questions=deepcopy(QUESTIONS), policy_version="test-judgment-v1",
                         project=self.project, store=self.store, evaluator=response)
        arguments.update(overrides)
        return client.evaluate(**arguments)

    def test_exact_hit_is_free_and_content_change_invalidates(self):
        with patch.object(client, "_post", side_effect=AssertionError("No external request")):
            answers, cold = self.evaluate()
            self.assertEqual(123, cold["usage"]["input_tokens"])
            self.assertNotIn("legend", answers["relevance"])
            _, warm = self.evaluate(evaluator=lambda _: self.fail("Cache miss"))
            self.assertEqual({"requests": 0, "cache_hits": 1}, warm["counts"])
            self.assertEqual({"input_tokens": 0, "output_tokens": 0}, warm["usage"])
            for changes in ({"state": {"query": "A revised question"}},
                            {"policy_version": "test-judgment-v2"},
                            {"project": self.project + "-other"}):
                self.assertEqual(1, self.evaluate(**changes)[1]["counts"]["requests"])
            changed = deepcopy(QUESTIONS)
            changed["support"]["instructions"] += " Explicitly."
            self.assertEqual(1, self.evaluate(questions=changed)[1]["counts"]["requests"])

    def test_credentials_and_private_data_redacted_before_provider_and_cache(self):
        captured = []
        def provider(payload):
            captured.append(payload)
            result = response(payload)
            result["debug"] = "remote secret text"
            result["answers"]["support"]["explanation"] = "remote secret text"
            return result
        state = {"api_key": "privatecredentialvalue", "text": "<private>hidden words</private>"}
        _, audit = self.evaluate(state=state, evaluator=provider)
        self.assertEqual("[REDACTED]", captured[0]["state"]["api_key"])
        self.assertNotIn("hidden words", json.dumps(captured))
        audit["raw_content"] = "unwanted source"
        self.assertTrue(client.record_audit(self.store, self.project, audit, route="quality_accept", owner_id="job_1"))
        cached = self.store._connection.execute("SELECT response_json FROM jev_judgment_cache").fetchone()[0]
        saved = self.store._connection.execute("SELECT audit_json FROM jev_judgment_audits").fetchone()[0]
        for secret in ("privatecredentialvalue", "hidden words", "remote secret text", "unwanted source", "How did"):
            self.assertNotIn(secret, cached + saved)

    def test_invalid_responses_keep_paid_usage_and_never_cache(self):
        mutations = [
            lambda r: r.update(model="jev-latest"),
            lambda r: r["answers"].pop("support"),
            lambda r: r["answers"]["support"].update(noul=True),
            lambda r: r["answers"]["support"].update(noul=float("nan")),
            lambda r: r["answers"]["relation"].update(choice="unknown"),
            lambda r: r["answers"]["relation"]["probabilities"].update(supports=.1),
            lambda r: r["answers"]["relevance"].update(score=.2),
            lambda r: r["answers"]["relevance"]["legend"].update({"2": "Wrong rubric"}),
        ]
        for mutate in mutations:
            with self.subTest(mutation=mutate):
                def provider(payload):
                    value = response(payload)
                    mutate(value)
                    return value
                with self.assertRaises(client.JevError) as failed:
                    self.evaluate(evaluator=provider)
                self.assertEqual("jev_invalid_response", failed.exception.code)
                self.assertEqual(123, failed.exception.audit["usage"]["input_tokens"])
                self.assertEqual("partial", failed.exception.audit["usage_status"])
        self.assertIsNone(self.store._connection.execute(
            "SELECT name FROM sqlite_master WHERE name='jev_judgment_cache'").fetchone())

    def test_numeric_and_nested_sensitive_fields_remain_valid_json(self):
        for value in (123456, {"algorithm": "sha256"}, ["secret"]):
            with self.subTest(value=value):
                seen = []
                def provider(payload):
                    seen.append(payload)
                    return response(payload)
                self.evaluate(state={"api_key": value, "ordinary_count": 15}, evaluator=provider, store=None)
                self.assertEqual({"api_key": "[REDACTED]", "ordinary_count": 15}, seen[0]["state"])

    def test_corrupt_cached_answer_is_recomputed(self):
        self.evaluate()
        with self.store._connection:
            self.store._connection.execute("UPDATE jev_judgment_cache SET response_json='{}'")
        self.assertEqual(1, self.evaluate()[1]["counts"]["requests"])

    def test_oversize_input_does_not_call_provider(self):
        with self.assertRaises(client.JevError) as failed:
            self.evaluate(state="x" * client.MAX_PAYLOAD_BYTES, evaluator=lambda _: self.fail("Oversize request"))
        self.assertEqual("jev_input_limit", failed.exception.code)
        self.assertEqual(0, failed.exception.audit["counts"]["requests"])
        self.assertTrue(client.record_audit(self.store, self.project, failed.exception.audit, route="quality_input_limit"))

    def test_failure_is_sanitized_and_missing_usage_is_unknown(self):
        def failing(_):
            raise RuntimeError("Bearer private-provider-detail")
        with self.assertRaises(client.JevError) as failure:
            self.evaluate(evaluator=failing)
        self.assertEqual("jev_transport", str(failure.exception))
        self.assertEqual("unavailable", failure.exception.audit["usage_status"])
        self.assertNotIn("private-provider-detail", json.dumps(failure.exception.audit))

    def test_quality_budget_preserves_full_evidence_without_widening_default(self):
        state = {"evidence": "x" * 30_000 + "COMPLETE_EVIDENCE_END"}
        seen = []
        def provider(payload):
            seen.append(payload["state"])
            return response(payload)
        _, audit = self.evaluate(state=state, evaluator=provider,
                                 max_payload_bytes=client.MAX_QUALITY_PAYLOAD_BYTES)
        self.assertEqual([state], seen)
        self.assertEqual(1, audit["counts"]["requests"])
        # A larger cached answer cannot bypass a narrower caller's admission budget.
        with self.assertRaises(client.JevError) as failure:
            self.evaluate(state=state, evaluator=lambda _: self.fail("Default budget widened"))
        self.assertEqual("jev_input_limit", failure.exception.code)
        self.assertEqual({"requests": 0, "cache_hits": 0}, failure.exception.audit["counts"])
        _, warm = self.evaluate(state=state, max_payload_bytes=client.MAX_QUALITY_PAYLOAD_BYTES,
                                evaluator=lambda _: self.fail("Expected exact-input cache hit"))
        self.assertEqual({"requests": 0, "cache_hits": 1}, warm["counts"])

    def test_quality_budget_has_a_hard_cap_and_strict_validation(self):
        for value in (True, 0, 1.5, "96000", client.MAX_QUALITY_PAYLOAD_BYTES + 1):
            with self.subTest(limit=value), self.assertRaises(client.JevError) as failure:
                self.evaluate(max_payload_bytes=value,
                              evaluator=lambda _: self.fail("Invalid budget dispatched"))
            self.assertEqual("jev_invalid_input", failure.exception.code)
            self.assertEqual(0, failure.exception.audit["counts"]["requests"])
        with self.assertRaises(client.JevError) as failure:
            self.evaluate(state="x" * client.MAX_QUALITY_PAYLOAD_BYTES,
                          max_payload_bytes=client.MAX_QUALITY_PAYLOAD_BYTES,
                          evaluator=lambda _: self.fail("Hard limit exceeded"))
        self.assertEqual("jev_input_limit", failure.exception.code)
        self.assertEqual(0, failure.exception.audit["counts"]["requests"])

    def test_over_deadline_answer_records_usage_but_cannot_authorize(self):
        def delayed(payload):
            time.sleep(.025)
            return response(payload)
        with self.assertRaises(client.JevError) as failure:
            self.evaluate(evaluator=delayed, timeout=.01)
        self.assertEqual("jev_timeout", failure.exception.code)
        self.assertEqual(123, failure.exception.audit["usage"]["input_tokens"])

    def test_cache_and_audit_writer_contention_are_bounded(self):
        # A busy DB must not convert the short hook path into a multi-second wait.
        lock = sqlite3.connect(self.store.db_path)
        self.addCleanup(lock.close)
        lock.execute("BEGIN IMMEDIATE")
        before = time.monotonic()
        _, audit = self.evaluate()
        self.assertFalse(client.record_audit(self.store, self.project, audit, route="search"))
        self.assertLess(time.monotonic() - before, .4)
        lock.rollback()

    def test_cache_write_cannot_extend_success_past_deadline(self):
        with patch.object(client, "_cache_put", side_effect=lambda *args: time.sleep(.025)):
            with self.assertRaises(client.JevError) as failure:
                self.evaluate(timeout=.01)
        self.assertEqual("jev_timeout", failure.exception.code)
        self.assertEqual(123, failure.exception.audit["usage"]["input_tokens"])

    def test_revocation_after_cache_lookup_blocks_dispatch_and_cached_decision(self):
        for warm in (False, True):
            with self.subTest(warm=warm):
                if warm:
                    self.evaluate()
                current = {"allowed": True}
                original = client._cache_get
                def revoking_lookup(*args):
                    answer = original(*args)
                    current["allowed"] = False
                    return answer
                with patch.object(client, "_cache_get", side_effect=revoking_lookup):
                    with self.assertRaises(client.JevError) as failure:
                        self.evaluate(before_dispatch=lambda: current["allowed"],
                                      evaluator=lambda _: self.fail("Revoked source dispatched"))
                self.assertEqual("jev_source_unavailable", failure.exception.code)
                self.assertEqual({"requests": 0, "cache_hits": 0}, failure.exception.audit["counts"])
                self.assertEqual(0, failure.exception.audit["usage"]["input_tokens"])

    def test_guard_errors_fail_closed_without_exposing_details(self):
        def unavailable():
            raise RuntimeError("private-source-details")
        with self.assertRaises(client.JevError) as failure:
            self.evaluate(before_dispatch=unavailable,
                          evaluator=lambda _: self.fail("Unavailable source dispatched"))
        self.assertEqual("jev_source_unavailable", failure.exception.code)
        self.assertNotIn("private-source-details", json.dumps(failure.exception.audit))
        self.assertTrue(client.record_audit(self.store, self.project, failure.exception.audit,
                                          route="quality_unavailable"))

    def test_independent_scopes_and_exclusions(self):
        settings = {"jev_quality_enabled": True, "jev_retrieval_enabled": False,
                    "jev_filter_projects": [self.project], "jev_retrieval_projects": []}
        self.assertTrue(client.enabled(settings, "jev_quality_enabled", self.project + "/child"))
        self.assertFalse(client.enabled(settings, "jev_quality_enabled", self.project + "-neighbor"))
        self.assertFalse(client.enabled(settings, "jev_retrieval_enabled", self.project))
        settings["jev_quality_projects"] = [self.project + "/child"]
        self.assertFalse(client.enabled(settings, "jev_quality_enabled", self.project))
        self.assertTrue(client.enabled(settings, "jev_quality_enabled", self.project + "/child"))
        settings["jev_quality_projects"] = [self.project + "-neighbor"]
        self.assertFalse(client.enabled(settings, "jev_quality_enabled", self.project + "-neighbor"))
        settings["jev_quality_projects"] = []
        settings.update(jev_retrieval_enabled=True, excluded_projects=[self.project])
        self.assertFalse(client.enabled(settings, "jev_quality_enabled", self.project))
        self.assertFalse(client.enabled(settings, "jev_retrieval_enabled", self.project))


if __name__ == "__main__":
    unittest.main()
