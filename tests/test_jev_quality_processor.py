"""Quality quarantine is atomic with the existing observation lifecycle."""
from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from codex_mem.config import configure
from codex_mem.jev_quality import MAX_QUALITY_PAYLOAD_BYTES
from codex_mem.processor import (
    MODEL, PROCESSOR_ID, REASONING_EFFORT, _effective_lease_seconds,
    _quality_sources_current, process_pending,
)
from codex_mem.store import Store, StoreError
from tests.test_jev_filter import answer as filter_answer
from tests.test_jev_quality import note, response
from tests import test_processor as processor_fixtures


class JevQualityProcessorTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.project = self.root / "project"
        self.project.mkdir()
        self.home = self.root / "memory"
        configure(self.home, jev_quality_enabled=True)

    def remember(self, body="The assistant reported a test pass.", source="hook:PostToolUse"):
        with Store(self.home) as store:
            return store.remember(self.project, "Raw event", body, source=source, session_id="quality-test")

    @staticmethod
    def runner(request):
        return processor_fixtures.ProcessorTests.receipt({"disposition": "processed", "notes": [note(request["sources"][0]["id"])]})

    @staticmethod
    def skipped(_):
        return processor_fixtures.ProcessorTests.receipt({"disposition": "skipped", "notes": [], "session_summary": None})

    def stored(self, result):
        with Store(self.home) as store:
            job = store.observation_job_status(self.project, result["job_id"])
            table = store._connection.execute("SELECT name FROM sqlite_master WHERE name='jev_judgment_audits'").fetchone()
            audits = store._connection.execute(
                "SELECT audit_json FROM jev_judgment_audits WHERE owner_id=? ORDER BY id", (result["job_id"],)
            ).fetchall() if table else []
        return job, [json.loads(row[0]) for row in audits]

    def assert_preserved(self, raw, result, reason):
        self.assertEqual("failed", result["status"], result)
        self.assertEqual("invalid_response", result["code"])
        self.assertEqual(reason, result["reason_code"])
        job, audits = self.stored(result)
        self.assertEqual("failed", job["status"])
        self.assertEqual([], job["output_ids"])
        with Store(self.home) as store:
            original = store.get(self.project, [raw["id"]])[0]
        self.assertEqual(raw["body"], original["body"])
        self.assertIsNone(original["superseded_by"])
        self.assertTrue(audits)
        self.assertNotIn("PRIVATE_", json.dumps([result, job, audits]))
        return job, audits

    def test_valid_shape_with_unsupported_claim_is_quarantined_before_finish(self):
        raw = self.remember("PRIVATE_INTENT Please deploy the change.", "hook:UserPromptSubmit")
        def runner(request):
            return processor_fixtures.ProcessorTests.receipt({"disposition": "processed", "notes": [
                note(request["sources"][0]["id"], title="Deployment completed", body="Production deployment succeeded.")
            ]})
        with mock.patch.object(Store, "finish_observation_batch", side_effect=AssertionError("Must not commit")) as finish:
            result = process_pending(self.project, self.home, runner=runner,
                                     jev_quality_evaluator=lambda payload: response(payload, 0.01, 0.99))
        _, audits = self.assert_preserved(raw, result, "jev_quality_rejected")
        finish.assert_not_called()
        self.assertEqual("quality_rejected", audits[0]["route"])
        self.assertEqual(1, result["jev_quality"]["counts"]["requests"])

    def test_uncertainty_transport_invalid_response_and_input_limit_preserve_raw(self):
        cases = (
            ("uncertain", lambda payload: response(payload, .5, .1), "PRIVATE_UNCERTAIN", 2),
            ("unavailable", mock.Mock(side_effect=RuntimeError("PRIVATE_TRANSPORT")), "PRIVATE_ERROR", 1),
            ("unavailable", lambda _: {}, "PRIVATE_MALFORMED", 1),
            ("input_limit", mock.Mock(), "PRIVATE_START" + "x" * MAX_QUALITY_PAYLOAD_BYTES + "PRIVATE_END", 0),
        )
        for index, (reason, evaluator, text, calls) in enumerate(cases):
            with self.subTest(reason=reason, index=index):
                self.home = self.root / f"memory-{index}"
                configure(self.home, jev_quality_enabled=True)
                raw = self.remember(text)
                runner = mock.Mock(side_effect=self.runner)
                result = process_pending(self.project, self.home, runner=runner, jev_quality_evaluator=evaluator)
                self.assert_preserved(raw, result, "jev_quality_" + reason)
                self.assertEqual(calls, result["jev_quality"]["counts"]["requests"])
                again = process_pending(self.project, self.home, runner=runner, jev_quality_evaluator=evaluator)
                self.assertEqual("idle", again["status"])
                runner.assert_called_once()

    def test_passed_note_and_summary_commit_together(self):
        raw = self.remember("The assistant reported a local pass; deployment remains unverified.", "hook:Stop")
        def runner(request):
            source_id = request["sources"][0]["id"]
            summary = processor_fixtures.ProcessorTests.structured_summary(source_id)
            return processor_fixtures.ProcessorTests.receipt({"disposition": "processed", "notes": [note(source_id)],
                                           "session_summary": summary})
        result = process_pending(self.project, self.home, runner=runner, jev_quality_evaluator=response)
        self.assertEqual("processed", result["status"], result)
        self.assertEqual(1, result["note_count"])
        self.assertEqual(1, result["session_summary_count"])
        self.assertEqual(2, result["jev_quality"]["counts"]["accepted"])
        job, audits = self.stored(result)
        self.assertEqual(2, len(job["output_ids"]))
        self.assertEqual(1, len(audits))
        self.assertTrue(result["jev_quality"]["audit_recorded"])
        with Store(self.home) as store:
            self.assertIsNotNone(store.get(self.project, [raw["id"]])[0]["superseded_by"])

    def test_required_summary_gets_the_same_vetted_history_as_generator(self):
        self.remember("Use checkout_id to avoid duplicate charges.")
        earlier = process_pending(self.project, self.home, runner=self.runner, jev_quality_evaluator=response)
        self.assertEqual("processed", earlier["status"], earlier)
        raw = self.remember("Done.", "hook:Stop")
        calls = []
        def runner(request):
            self.assertIn("Reported result", request["prompt"])
            return processor_fixtures.ProcessorTests.receipt({"disposition": "processed", "notes": [],
                "session_summary": processor_fixtures.ProcessorTests.structured_summary(request["sources"][0]["id"])})
        def evaluator(payload):
            calls.append(payload)
            item = payload["state"]["items"][0]
            self.assertEqual("session_summary", item["candidate_kind"])
            history = item["session_history"]
            self.assertTrue(any(row["title"] == "Reported result" for row in history))
            self.assertTrue(any(row["evidence_role"] == "derived_note" for row in history))
            self.assertEqual([raw["id"]], [source["id"] for source in item["cited_sources"]])
            return response(payload)
        result = process_pending(self.project, self.home, runner=runner, jev_quality_evaluator=evaluator)
        self.assertEqual("processed", result["status"], result)
        self.assertEqual(1, result["session_summary_count"])
        self.assertEqual(1, len(calls))

    def test_new_result_resolves_history_reference_without_citing_history(self):
        decision = self.remember("Use checkout_id as the retry idempotency key.", "hook:UserPromptSubmit")
        def decision_runner(request):
            return processor_fixtures.ProcessorTests.receipt({"disposition": "processed", "notes": [
                note(request["sources"][0]["id"], title="Idempotency key decision",
                     body="The user chose checkout_id as the retry idempotency key.")
            ]})
        earlier = process_pending(self.project, self.home, runner=decision_runner, jev_quality_evaluator=response)
        self.assertEqual("processed", earlier["status"], earlier)
        run = self.remember("The local retry test passed with that key.")
        inspected = []
        def runner(request):
            self.assertIn("checkout_id", request["prompt"])
            self.assertIn("The local retry test passed with that key.", request["prompt"])
            self.assertEqual(["s1"], [source["id"] for source in request["sources"]])
            return processor_fixtures.ProcessorTests.receipt({"disposition": "processed", "notes": [
                note(request["sources"][0]["id"], title="Local retry test passed",
                     body="The local retry test passed with checkout_id.")
            ]})
        def evaluator(payload):
            item = payload["state"]["items"][0]
            inspected.append(item)
            self.assertEqual("reference_resolution_only", item["history_use"])
            self.assertEqual([run["id"]], [source["id"] for source in item["cited_sources"]])
            self.assertTrue(any("checkout_id" in source["body"] for source in item["session_history"]))
            self.assertNotIn(decision["id"], [source["id"] for source in item["cited_sources"]])
            return response(payload)
        result = process_pending(self.project, self.home, runner=runner, jev_quality_evaluator=evaluator)
        self.assertEqual("processed", result["status"], result)
        self.assertEqual(1, len(inspected))
        job, _ = self.stored(result)
        with Store(self.home) as store:
            saved = store.get(self.project, job["output_ids"])[0]
        self.assertEqual([run["id"]], saved["source_ids"])
        self.assertEqual("The local retry test passed with checkout_id.", saved["body"])

    def test_uncertain_summary_prevents_saving_accepted_note(self):
        raw = self.remember("Reported a pass.", "hook:Stop")
        def runner(request):
            source_id = request["sources"][0]["id"]
            return processor_fixtures.ProcessorTests.receipt({"disposition": "processed", "notes": [note(source_id)],
                "session_summary": processor_fixtures.ProcessorTests.structured_summary(source_id)})
        def evaluator(payload):
            result = response(payload)
            for key, answer in result["answers"].items():
                if key.startswith("item_1_") and key.endswith("_grounded"):
                    answer["noul"] = .5
            return result
        result = process_pending(self.project, self.home, runner=runner, jev_quality_evaluator=evaluator)
        self.assert_preserved(raw, result, "jev_quality_uncertain")
        self.assertEqual(1, result["jev_quality"]["counts"]["accepted"])

    def test_missing_required_summary_is_rejected_before_quality(self):
        self.remember("Prior useful result.")
        earlier = process_pending(self.project, self.home, runner=self.runner, jev_quality_evaluator=response)
        self.assertEqual("processed", earlier["status"], earlier)
        self.remember("Done.", "hook:Stop")
        evaluator = mock.Mock()
        result = process_pending(self.project, self.home, runner=self.skipped, jev_quality_evaluator=evaluator)
        self.assertEqual("missing_required_summary", result["reason_code"])
        evaluator.assert_not_called()

    def test_lifecycle_attribution_check_precedes_quality(self):
        configure(self.home, jev_filter_enabled=True)
        self.remember("Keep substantive evidence.")
        marker = self.remember("PRIVATE_REJECTED_STOP", "hook:Stop")
        def filter_evaluator(payload):
            source = json.loads(payload["state"]["source_fragment"])
            return filter_answer(.01, "routine") if source["id"] == marker["id"] else filter_answer()
        def runner(request):
            source_id = next(row["id"] for row in request["sources"] if row["source"] == "hook:Stop")
            return processor_fixtures.ProcessorTests.receipt({"disposition": "processed", "notes": [note(source_id)],
                "session_summary": processor_fixtures.ProcessorTests.structured_summary(source_id)})
        evaluator = mock.Mock()
        result = process_pending(self.project, self.home, runner=runner, jev_evaluator=filter_evaluator,
                                 jev_quality_evaluator=evaluator)
        self.assertEqual("source_attribution_conflict", result["reason_code"])
        evaluator.assert_not_called()

    def test_disabled_outside_scope_and_excluded_projects_make_no_quality_calls(self):
        settings = (
            {"jev_quality_enabled": False},
            {"jev_quality_enabled": True, "jev_filter_projects": [str(self.root / "other")]},
            {"jev_quality_enabled": True, "excluded_projects": [str(self.project)]},
        )
        for index, value in enumerate(settings):
            with self.subTest(settings=value):
                self.home = self.root / f"scope-{index}"
                configure(self.home, **value)
                self.remember()
                evaluator = mock.Mock()
                result = process_pending(self.project, self.home, runner=self.runner, jev_quality_evaluator=evaluator)
                self.assertEqual("processed", result["status"], result)
                self.assertNotIn("jev_quality", result)
                self.assertEqual([], self.stored(result)[1])
                evaluator.assert_not_called()

    def test_skipped_output_makes_no_quality_calls(self):
        self.remember("Routine listing.")
        evaluator = mock.Mock()
        result = process_pending(self.project, self.home, runner=self.skipped, jev_quality_evaluator=evaluator)
        self.assertEqual("skipped", result["status"], result)
        self.assertNotIn("jev_quality", result)
        self.assertEqual([], self.stored(result)[1])
        evaluator.assert_not_called()

    def test_quality_budget_is_included_in_lease_with_and_without_filter(self):
        for filter_enabled, expected in ((False, 50), (True, 110)):
            with self.subTest(filter=filter_enabled):
                self.home = self.root / f"lease-{filter_enabled}"
                configure(self.home, jev_quality_enabled=True, jev_filter_enabled=filter_enabled)
                self.remember()
                with mock.patch("codex_mem.processor._effective_lease_seconds", wraps=_effective_lease_seconds) as lease:
                    result = process_pending(self.project, self.home, timeout=30, lease_seconds=1,
                        runner=self.runner, jev_evaluator=lambda _: filter_answer(), jev_quality_evaluator=response)
                self.assertEqual("processed", result["status"], result)
                lease.assert_called_once_with(1, expected)

    def test_failed_audit_persistence_is_visible(self):
        self.remember()
        with mock.patch("codex_mem.jev_client.record_audit", return_value=False):
            result = process_pending(self.project, self.home, runner=self.runner, jev_quality_evaluator=response)
        self.assertEqual("processed", result["status"], result)
        self.assertFalse(result["jev_quality"]["audit_recorded"])

    def test_forgotten_current_source_after_generation_is_never_sent(self):
        raw = self.remember("PRIVATE_FORGOTTEN_CURRENT")
        evaluator = mock.Mock()
        def runner(request):
            with Store(self.home) as store:
                store.forget(self.project, [raw["id"]])
            return self.runner(request)
        result = process_pending(self.project, self.home, runner=runner, jev_quality_evaluator=evaluator)
        self.assertEqual("invalid_response", result["code"], result)
        self.assertEqual("jev_quality_unavailable", result["reason_code"])
        self.assertEqual(0, result["jev_quality"]["counts"]["requests"])
        job, audits = self.stored(result)
        self.assertEqual("source_deleted", job["error_code"])
        self.assertEqual([], job["output_ids"])
        self.assertEqual("jev_source_unavailable", audits[-1]["error_code"])
        with Store(self.home) as store:
            self.assertEqual([], store.get(self.project, [raw["id"]]))
        evaluator.assert_not_called()

    def test_forgotten_session_history_blocks_current_result_without_deleting_it(self):
        history = self.remember("PRIVATE_FORGOTTEN_HISTORY")
        earlier = process_pending(self.project, self.home, runner=self.skipped)
        self.assertEqual("skipped", earlier["status"])
        raw = self.remember("Current result with an earlier reference.")
        evaluator = mock.Mock()
        def runner(request):
            self.assertIn("PRIVATE_FORGOTTEN_HISTORY", request["prompt"])
            with Store(self.home) as store:
                store.forget(self.project, [history["id"]])
            return self.runner(request)
        result = process_pending(self.project, self.home, runner=runner, jev_quality_evaluator=evaluator)
        self.assert_preserved(raw, result, "jev_quality_unavailable")
        self.assertEqual(0, result["jev_quality"]["counts"]["requests"])
        evaluator.assert_not_called()

    def test_forgotten_cross_session_reference_is_never_sent(self):
        with Store(self.home) as store:
            historical = store.remember(self.project, "Prior project decision", "PRIVATE_CROSS_SESSION_REFERENCE",
                                        kind="decision", session_id="older-session")
        raw = self.remember("Current result.")
        evaluator = mock.Mock()
        def runner(request):
            self.assertIn("PRIVATE_CROSS_SESSION_REFERENCE", request["prompt"])
            with Store(self.home) as store:
                store.forget(self.project, [historical["id"]])
            return self.runner(request)
        result = process_pending(self.project, self.home, runner=runner, jev_quality_evaluator=evaluator)
        self.assert_preserved(raw, result, "jev_quality_unavailable")
        evaluator.assert_not_called()

    def test_forget_during_cache_preparation_blocks_dispatch(self):
        raw = self.remember("PRIVATE_FORGOTTEN_BEFORE_DISPATCH")
        evaluator = mock.Mock()
        def cached(*_):
            with Store(self.home) as store:
                store.forget(self.project, [raw["id"]])
            return None
        with mock.patch("codex_mem.jev_client._cache_get", side_effect=cached):
            result = process_pending(self.project, self.home, runner=self.runner, jev_quality_evaluator=evaluator)
        self.assertEqual("invalid_response", result["code"], result)
        self.assertEqual("jev_quality_unavailable", result["reason_code"])
        self.assertEqual(0, result["jev_quality"]["counts"]["requests"])
        self.assertEqual("jev_source_unavailable", result["jev_quality"]["evaluations"][-1]["error_code"])
        evaluator.assert_not_called()

    def test_forget_between_split_requests_stops_egress_and_accounts_first_call(self):
        first = self.remember("FIRST_EVIDENCE " + "x" * (MAX_QUALITY_PAYLOAD_BYTES // 2))
        second = self.remember("SECOND_EVIDENCE " + "y" * (MAX_QUALITY_PAYLOAD_BYTES // 2))
        calls = []
        def runner(request):
            return processor_fixtures.ProcessorTests.receipt({"disposition": "processed",
                "notes": [note(source["id"]) for source in request["sources"]]})
        def evaluator(payload):
            calls.append(payload)
            with Store(self.home) as store:
                store.forget(self.project, [second["id"]])
            return response(payload)
        result = process_pending(self.project, self.home, runner=runner, jev_quality_evaluator=evaluator,
                                 max_chars=120_000)
        self.assertEqual("invalid_response", result["code"], result)
        self.assertEqual("jev_quality_unavailable", result["reason_code"])
        self.assertEqual(1, len(calls))
        self.assertEqual(1, result["jev_quality"]["counts"]["requests"])
        self.assertEqual({"input_tokens": 200, "output_tokens": 20}, result["jev_quality"]["usage"])
        job, audits = self.stored(result)
        self.assertEqual([], job["output_ids"])
        self.assertEqual(2, len(audits))
        self.assertEqual(1, sum(row["counts"]["requests"] for row in audits))
        with Store(self.home) as store:
            self.assertIsNone(store.get(self.project, [first["id"]])[0]["superseded_by"])
            self.assertEqual([], store.get(self.project, [second["id"]]))

    def test_replaced_lease_is_not_dispatched_or_mutated_by_old_worker(self):
        raw = self.remember()
        evaluator = mock.Mock()
        def runner(request):
            with Store(self.home) as store:
                store._connection.execute("UPDATE observation_jobs SET lease_token=?,attempt_count=attempt_count+1 WHERE id=?",
                                          ("replacement-token", request["job_id"]))
            return self.runner(request)
        result = process_pending(self.project, self.home, runner=runner, jev_quality_evaluator=evaluator)
        self.assertEqual("invalid_response", result["code"], result)
        self.assertEqual("jev_quality_unavailable", result["reason_code"])
        with Store(self.home) as store:
            job = store._connection.execute("SELECT status,lease_token,attempt_count FROM observation_jobs WHERE id=?",
                                            (result["job_id"],)).fetchone()
            self.assertEqual(("running", "replacement-token", 2), tuple(job))
            self.assertIsNone(store.get(self.project, [raw["id"]])[0]["superseded_by"])
        evaluator.assert_not_called()

    def test_current_source_membership_is_checked(self):
        for mutation in ("fingerprint", "superseded"):
            with self.subTest(mutation=mutation):
                self.home = self.root / ("guard-" + mutation)
                configure(self.home, jev_quality_enabled=True)
                raw = self.remember()
                evaluator = mock.Mock()
                def runner(request):
                    with Store(self.home) as store:
                        if mutation == "fingerprint":
                            store._connection.execute("UPDATE observation_jobs SET input_fingerprint='changed' WHERE id=?",
                                                      (request["job_id"],))
                        else:
                            store._connection.execute("UPDATE entries SET superseded_by=? WHERE id=?", (raw["id"], raw["id"]))
                    return self.runner(request)
                result = process_pending(self.project, self.home, runner=runner, jev_quality_evaluator=evaluator)
                self.assertEqual("invalid_response", result["code"], result)
                self.assertEqual("jev_quality_unavailable", result["reason_code"])
                self.assertEqual(0, result["jev_quality"]["counts"]["requests"])
                evaluator.assert_not_called()

    def test_expired_lease_blocks_dispatch_and_keeps_normal_recovery(self):
        raw = self.remember()
        evaluator = mock.Mock(side_effect=response)
        def expired_runner(request):
            with Store(self.home) as store:
                store._connection.execute("UPDATE observation_jobs SET lease_expires_at='2000-01-01T00:00:00Z' WHERE id=?",
                                          (request["job_id"],))
            return self.runner(request)
        expired = process_pending(self.project, self.home, runner=expired_runner, jev_quality_evaluator=evaluator)
        self.assertEqual("lease_expired", expired["code"], expired)
        self.assertEqual(0, expired["jev_quality"]["counts"]["requests"])
        evaluator.assert_not_called()
        with Store(self.home) as store:
            job = store.observation_job_status(self.project, expired["job_id"])
            self.assertEqual("running", job["status"])
            self.assertEqual([], job["output_ids"])
            self.assertIsNone(store.get(self.project, [raw["id"]])[0]["superseded_by"])
        recovered = process_pending(self.project, self.home, runner=self.runner, jev_quality_evaluator=evaluator)
        self.assertEqual("processed", recovered["status"], recovered)
        self.assertEqual(expired["job_id"], recovered["job_id"])
        evaluator.assert_called_once()
        with Store(self.home) as store:
            job = store.observation_job_status(self.project, expired["job_id"])
            self.assertEqual(2, job["attempt_count"])

    def test_known_semantic_failure_is_durably_quarantined_after_expiry(self):
        for reason, grounded, overclaim in (("rejected", .01, .99), ("uncertain", .5, .5)):
            with self.subTest(reason=reason):
                self.home = self.root / ("expired-" + reason)
                configure(self.home, jev_quality_enabled=True)
                raw = self.remember()
                runner = mock.Mock(side_effect=self.runner)
                def evaluator(payload):
                    with Store(self.home) as store:
                        store._connection.execute("UPDATE observation_jobs SET lease_expires_at='2000-01-01T00:00:00Z' WHERE status='running'")
                    return response(payload, grounded, overclaim)
                result = process_pending(self.project, self.home, runner=runner, jev_quality_evaluator=evaluator)
                self.assert_preserved(raw, result, "jev_quality_" + reason)
                self.assertEqual("lease_expired", result["jev_quality"]["blocked_by"])
                self.assertEqual(reason, result["jev_quality"]["decisions"][0]["route"])
                self.assertEqual(1, result["jev_quality"]["counts"]["requests"])
                next_poll = process_pending(self.project, self.home, runner=runner, jev_quality_evaluator=evaluator)
                self.assertEqual("idle", next_poll["status"])
                runner.assert_called_once()

    def test_successfully_refined_candidate_does_not_quarantine_natural_expiry(self):
        raw = self.remember()
        def evaluator(payload):
            if any("_field_" in key for key in payload["questions"]):
                with Store(self.home) as store:
                    store._connection.execute("UPDATE observation_jobs SET lease_expires_at='2000-01-01T00:00:00Z' WHERE status='running'")
                return response(payload)
            return response(payload, .5, .5)
        expired = process_pending(self.project, self.home, runner=self.runner, jev_quality_evaluator=evaluator)
        self.assertEqual("lease_expired", expired["code"], expired)
        decision = expired["jev_quality"]["decisions"][0]
        self.assertEqual("uncertain", decision["initial"]["route"])
        self.assertEqual("accept", decision["route"])
        self.assertEqual(2, expired["jev_quality"]["counts"]["requests"])
        with Store(self.home) as store:
            job = store.observation_job_status(self.project, expired["job_id"])
            self.assertEqual("running", job["status"])
            self.assertIsNone(store.get(self.project, [raw["id"]])[0]["superseded_by"])
        recovered = process_pending(self.project, self.home, runner=self.runner, jev_quality_evaluator=mock.Mock())
        self.assertEqual("processed", recovered["status"], recovered)
        self.assertEqual(2, recovered["jev_quality"]["counts"]["cache_hits"])
        self.assertEqual(0, recovered["jev_quality"]["counts"]["requests"])

    def test_expiry_during_failed_refinement_keeps_known_uncertainty_quarantined(self):
        raw = self.remember()
        runner = mock.Mock(side_effect=self.runner)
        def evaluator(payload):
            if any("_field_" in key for key in payload["questions"]):
                with Store(self.home) as store:
                    store._connection.execute("UPDATE observation_jobs SET lease_expires_at='2000-01-01T00:00:00Z' WHERE status='running'")
                raise RuntimeError("PRIVATE_REFINEMENT_TRANSPORT_FAILURE")
            return response(payload, .5, .5)
        result = process_pending(self.project, self.home, runner=runner, jev_quality_evaluator=evaluator)
        self.assert_preserved(raw, result, "jev_quality_uncertain")
        self.assertEqual(2, result["jev_quality"]["counts"]["requests"])
        self.assertEqual("partial", result["jev_quality"]["usage_status"])
        self.assertEqual(200, result["jev_quality"]["usage"]["input_tokens"])
        self.assertEqual("lease_expired", result["jev_quality"]["blocked_by"])
        next_poll = process_pending(self.project, self.home, runner=runner, jev_quality_evaluator=evaluator)
        self.assertEqual("idle", next_poll["status"])
        runner.assert_called_once()

    def test_expired_semantic_quarantine_requires_exact_owner_and_attempt(self):
        self.remember()
        with Store(self.home) as store:
            claimed = store.claim_observation_batch(self.project, PROCESSOR_ID, MODEL, REASONING_EFFORT)
            store._connection.execute("UPDATE observation_jobs SET lease_expires_at='2000-01-01T00:00:00Z' WHERE id=?", (claimed["job_id"],))
            for token, attempt in (("replacement-token", claimed["attempt_count"]),
                                   (claimed["lease_token"], claimed["attempt_count"] + 1)):
                with self.assertRaises(StoreError):
                    store.fail_observation_batch(self.project, claimed["job_id"], token,
                        code="invalid_response", reason_code="jev_quality_rejected",
                        expired_quality_quarantine_attempt=attempt)
            for code, reason in (("timeout", "jev_quality_rejected"), ("invalid_response", "jev_quality_unavailable")):
                with self.assertRaises(ValueError):
                    store.fail_observation_batch(self.project, claimed["job_id"], claimed["lease_token"],
                        code=code, reason_code=reason, expired_quality_quarantine_attempt=claimed["attempt_count"])
            row = store._connection.execute("SELECT status,lease_token,attempt_count FROM observation_jobs WHERE id=?", (claimed["job_id"],)).fetchone()
            self.assertEqual(("running", claimed["lease_token"], claimed["attempt_count"]), tuple(row))
            store.forget(self.project, [claimed["sources"][0]["id"]])
            with self.assertRaises(StoreError):
                store.fail_observation_batch(self.project, claimed["job_id"], claimed["lease_token"],
                    code="invalid_response", reason_code="jev_quality_rejected",
                    expired_quality_quarantine_attempt=claimed["attempt_count"])
            row = store._connection.execute("SELECT status,error_code FROM observation_jobs WHERE id=?", (claimed["job_id"],)).fetchone()
            self.assertEqual(("failed", "source_deleted"), tuple(row))

    def test_revocation_during_refinement_cache_lookup_prevents_second_dispatch(self):
        from codex_mem import jev_client
        raw = self.remember()
        original_cache_get = jev_client._cache_get
        reads = []
        def cache_get(*args, **kwargs):
            reads.append(True)
            result = original_cache_get(*args, **kwargs)
            if len(reads) == 2:
                with Store(self.home) as store:
                    store.forget(self.project, [raw["id"]])
            return result
        evaluator = mock.Mock(side_effect=lambda payload: response(payload, .5, .5))
        with mock.patch.object(jev_client, "_cache_get", side_effect=cache_get):
            result = process_pending(self.project, self.home, runner=self.runner, jev_quality_evaluator=evaluator)
        self.assertEqual("jev_quality_unavailable", result["reason_code"], result)
        self.assertEqual(2, len(reads))
        evaluator.assert_called_once()
        self.assertEqual(1, result["jev_quality"]["counts"]["requests"])
        self.assertEqual(200, result["jev_quality"]["usage"]["input_tokens"])
        self.assertEqual(0, result["jev_quality"]["counts"]["refined"])
        job, _ = self.stored(result)
        self.assertEqual([], job["output_ids"])

    def test_generator_applies_provenance_and_completion_scope_to_titles(self):
        self.remember("Use checkout_id for idempotency.", "hook:UserPromptSubmit")
        def runner(request):
            self.assertIn("Titles and subtitles must preserve the same attribution, uncertainty and planned-versus-completed scope",
                          request["prompt"])
            self.assertIn("A cautious body cannot repair a stronger title", request["prompt"])
            return self.runner(request)
        result = process_pending(self.project, self.home, runner=runner, jev_quality_evaluator=response)
        self.assertEqual("processed", result["status"], result)

    def test_superseded_history_is_allowed_when_retained(self):
        history = self.remember("Earlier evidence retained through its derived note.")
        earlier = process_pending(self.project, self.home, runner=self.runner, jev_quality_evaluator=response)
        self.assertEqual("processed", earlier["status"], earlier)
        self.remember("New evidence.")
        with Store(self.home) as store:
            claimed = store.claim_observation_batch(self.project, PROCESSOR_ID, MODEL, REASONING_EFFORT)
            raw_history = store.get(self.project, [history["id"]])[0]
            self.assertIsNotNone(raw_history["superseded_by"])
            evidence = {**claimed, "context": [raw_history]}
            self.assertTrue(_quality_sources_current(store, str(self.project.resolve()), claimed, evidence))


if __name__ == "__main__":
    unittest.main()
