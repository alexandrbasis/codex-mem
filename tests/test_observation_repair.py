"""Guarded processed-output maintenance; mocked reviews prove wiring only."""
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from codex_mem.config import configure
from codex_mem.execution import legacy_profile
from codex_mem.observation_repair import inspect_processed_repair, repair_processed_outputs
from codex_mem.processor import PROCESSOR_ID, MODEL, REASONING_EFFORT
from codex_mem.store import Store, StoreError, ObservationEvidenceChanged
from tests.test_summary_support import supported_review, support_rows


class ObservationRepairTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.project = Path(temporary.name) / "project"
        self.project.mkdir()
        self.data = Path(temporary.name) / "data"
        configure(self.data, capture_scope="all", service_enabled=False, semantic_enabled=False,
                  jev_filter_enabled=False, jev_quality_enabled=False)
        with Store(self.data) as store:
            self.tool = store.remember(self.project, "Partner message", "Afterward 149 per month. No fees.",
                source="hook:PostToolUse", session_id="original-session")
            self.stop = store.remember(self.project, "Report", "I reported the offer and stopped checks.",
                source="hook:Stop", session_id="original-session")
            first = store.claim_observation_batch(self.project, PROCESSOR_ID, MODEL, REASONING_EFFORT,
                                                 execution_profile=legacy_profile().to_dict())
            store.fail_observation_batch(self.project, first["job_id"], first["lease_token"],
                                         code="invalid_response")
            claimed = store.claim_observation_batch(self.project, PROCESSOR_ID, MODEL, REASONING_EFFORT,
                retry_failed=True, execution_profile=legacy_profile().to_dict())
            self.job_id = claimed["job_id"]
            self.summary = dict(title="Partner offer", request="", investigated="", learned="",
                completed="The assistant reported the offer.", next_steps="", notes="",
                source_ids=[self.stop["id"]])
            result = store.finish_observation_batch(self.project, self.job_id, claimed["lease_token"],
                notes=[dict(title="Partner message", body="The message says 129 for 18 months, then 149.",
                            tags=[], source_ids=[self.tool["id"]])], session_summary=self.summary)
            self.output_ids = result["output_ids"]
            self.note_id = self.output_ids[0]
            self.old_outputs = store.get(self.project, self.output_ids)
            self.original_job = store.observation_job_status(self.project, self.job_id)
            self.original_attempts = store.observation_attempts(self.project, self.job_id)
            self.raw = store.get(self.project, [self.tool["id"], self.stop["id"]])
        self.guard = self.inspect([self.note_id])
        self.replacements = {self.note_id: dict(title="Partner message",
            body="The message says afterward 149 per month and no fees.", tags=[],
            source_ids=[self.tool["id"]])}

    def inspect(self, ids):
        return inspect_processed_repair(self.project, self.data, job_id=self.job_id,
                                       expected_attempt_count=2, output_ids=ids)

    def repair(self, **options):
        return repair_processed_outputs(self.project, self.data, guard=self.guard,
            replacements=self.replacements, summary_reviewer=supported_review, **options)

    def entries(self):
        with Store(self.data) as store:
            return store.timeline(self.project, limit=100)

    def test_wrong_guards_and_unrelated_outputs_do_not_dispatch_or_mutate(self):
        baseline = self.entries()
        with self.assertRaises(ObservationEvidenceChanged):
            inspect_processed_repair(self.project, self.data, job_id=self.job_id,
                                     expected_attempt_count=1, output_ids=[self.note_id])
        with self.assertRaises(ObservationEvidenceChanged):
            self.inspect([self.tool["id"]])
        self.guard["output_fingerprints"][self.note_id] = "0" * 64
        reviewer = mock.Mock(side_effect=AssertionError("must not dispatch"))
        result = repair_processed_outputs(self.project, self.data, guard=self.guard,
            replacements=self.replacements, summary_reviewer=reviewer)
        self.assertEqual("repair_evidence_changed", result["code"])
        reviewer.assert_not_called()
        self.assertEqual(baseline, self.entries())

    def test_rejected_or_malformed_review_writes_nothing(self):
        baseline = self.entries()
        def reject(payload):
            response = supported_review(payload)
            if payload["version"].startswith("summary-support"):
                support_rows(response["output"])[0]["verdict"] = "unsupported"
            return response
        for reviewer, reason in ((reject, "summary_support_rejected"),
                                  (lambda request: dict(output={}, evidence={}), "invalid_runner_evidence")):
            result = repair_processed_outputs(self.project, self.data, guard=self.guard,
                replacements=self.replacements, summary_reviewer=reviewer)
            self.assertEqual("failed", result["status"], result)
            self.assertEqual(reason, result["reason_code"], result)
            self.assertEqual(baseline, self.entries())
        with Store(self.data) as store:
            self.assertEqual(self.original_job, store.observation_job_status(self.project, self.job_id))
            self.assertEqual(self.original_attempts, store.observation_attempts(self.project, self.job_id))

    def test_success_retains_raw_job_receipts_history_and_only_supersedes_selected(self):
        with Store(self.data) as store:
            newer = store.remember(self.project, "Later summary", "An unrelated later summary.",
                                   kind="session_summary", source=f"processor:{PROCESSOR_ID}")
        requests = []
        def review(payload):
            requests.append(copy.deepcopy(payload))
            return supported_review(payload)
        result = repair_processed_outputs(self.project, self.data, guard=self.guard,
            replacements=self.replacements, summary_reviewer=review)
        self.assertEqual("repaired", result["status"], result)
        self.assertEqual({self.note_id}, set(result["successors"]))
        versions = {request["version"] for request in requests}
        self.assertTrue(any(version.startswith("summary-support") for version in versions))
        self.assertIn("summary-source-v4", versions)
        self.assertTrue(any(version.startswith("summary-outcome") for version in versions))
        with Store(self.data) as store:
            old = store.get(self.project, [self.note_id])[0]
            successor = store.get(self.project, [result["successors"][self.note_id]])[0]
            self.assertEqual(successor["id"], old["superseded_by"])
            self.assertEqual(self.old_outputs[0]["body"], old["body"])
            self.assertEqual(self.replacements[self.note_id]["body"], successor["body"])
            self.assertEqual({self.note_id, self.tool["id"]}, set(successor["source_ids"]))
            self.assertEqual(self.raw, store.get(self.project, [self.tool["id"], self.stop["id"]]))
            self.assertEqual(self.original_job, store.observation_job_status(self.project, self.job_id))
            self.assertEqual(self.original_attempts, store.observation_attempts(self.project, self.job_id))
            self.assertIsNone(store.get(self.project, [self.output_ids[1]])[0]["superseded_by"])
            self.assertEqual({key: value for key, value in newer.items() if key != "deduplicated"},
                             store.get(self.project, [newer["id"]])[0])
        for request in requests:
            for evidence in request.get("evidence", []):
                self.assertNotIn(evidence["retained_id"], self.output_ids)
        repeated = repair_processed_outputs(self.project, self.data, guard=self.guard,
            replacements=self.replacements, summary_reviewer=mock.Mock(side_effect=AssertionError("replay")))
        self.assertTrue(repeated["deduplicated"])
        self.assertEqual(result["successors"], repeated["successors"])

    def test_stale_evidence_during_review_rejects_commit(self):
        def review(payload):
            response = supported_review(payload)
            with Store(self.data) as store:
                store.remember(self.project, "Intervening consolidation", "A concurrent operation.",
                               source_ids=[self.tool["id"]])
            return response
        result = repair_processed_outputs(self.project, self.data, guard=self.guard,
            replacements=self.replacements, summary_reviewer=review)
        self.assertEqual("repair_evidence_changed", result["code"], result)
        with Store(self.data) as store:
            self.assertIsNone(store.get(self.project, [self.note_id])[0]["superseded_by"])
            self.assertEqual(self.original_job, store.observation_job_status(self.project, self.job_id))

    def test_configuration_change_rejects_without_review(self):
        configure(self.data, observation_profile=dict(runtime="codex", provider="openai",
                  model=MODEL, reasoning_effort="high", timeout=240))
        result = self.repair()
        self.assertEqual("repair_settings_changed", result["code"])

    def test_summary_replacement_keeps_typed_metadata_and_original_provenance(self):
        summary_id = self.output_ids[1]
        guard = self.inspect([summary_id])
        replacement = dict(self.summary, completed="The assistant reported stopping checks.")
        result = repair_processed_outputs(self.project, self.data, guard=guard,
            replacements={summary_id: replacement}, summary_reviewer=supported_review)
        self.assertEqual("repaired", result["status"], result)
        with Store(self.data) as store:
            output = store.get(self.project, [result["successors"][summary_id]])[0]
            self.assertEqual({key: (None if value == "" else value) for key, value in replacement.items()},
                             output["session_summary"])
            self.assertEqual({summary_id, self.stop["id"]}, set(output["source_ids"]))

    def test_invalid_configuration_never_dispatches_or_mutates(self):
        baseline = self.entries()
        (self.data / "config.json").write_text("{invalid")
        with self.assertRaises(ValueError):
            self.inspect([self.note_id])
        reviewer = mock.Mock(side_effect=AssertionError("configuration must block dispatch"))
        result = repair_processed_outputs(self.project, self.data, guard=self.guard,
            replacements=self.replacements, summary_reviewer=reviewer)
        self.assertEqual("configuration_unavailable", result["code"])
        reviewer.assert_not_called()
        self.assertEqual(baseline, self.entries())

    def test_configured_filter_and_quality_gates_are_retained_and_rejection_writes_nothing(self):
        from tests.test_jev_filter import answer
        from tests.test_jev_quality import response
        configure(self.data, jev_filter_enabled=True, jev_quality_enabled=True)
        self.guard = self.inspect([self.note_id])
        baseline = self.entries()
        filter_calls, quality_calls = [], []
        def filter_evaluate(payload):
            filter_calls.append(payload)
            return answer()
        def quality_evaluate(payload):
            quality_calls.append(payload)
            return response(payload, grounded=.01, overclaim=.99)
        result = self.repair(jev_evaluator=filter_evaluate, jev_quality_evaluator=quality_evaluate)
        self.assertEqual("jev_quality_rejected", result["reason_code"], result)
        self.assertTrue(filter_calls)
        self.assertTrue(quality_calls)
        self.assertEqual(baseline, self.entries())

    def test_canonical_proposal_is_reviewed_and_committed_without_raw_pm_requeue(self):
        configure(self.data, project_memory_mode="shadow", project_memory_projects=[str(self.project)])
        self.guard = self.inspect([self.note_id])
        self.replacements[self.note_id]["title"] = "  Corrected Partner message  "
        seen = []
        def review(payload):
            if payload["version"].startswith("summary-support"):
                seen.append(payload["units"]["note:0:title"])
            return supported_review(payload)
        with Store(self.data) as store:
            before = list(store._connection.execute("SELECT * FROM pm_events"))
        result = repair_processed_outputs(self.project, self.data, guard=self.guard,
            replacements=self.replacements, summary_reviewer=review)
        self.assertEqual("repaired", result["status"], result)
        new_id = result["successors"][self.note_id]
        with Store(self.data) as store:
            new = store.get(self.project, [new_id])[0]
            self.assertEqual([new["title"]], seen)
            self.assertEqual("Corrected Partner message", new["title"])
            self.assertEqual(before, list(store._connection.execute("SELECT * FROM pm_events")))
            self.assertIsNotNone(store._connection.execute(
                "SELECT entry_id FROM embedding_documents WHERE entry_id=?", (new_id,)).fetchone())
            self.assertIsNotNone(store._connection.execute(
                "SELECT rowid FROM entries_fts WHERE entries_fts MATCH 'Corrected' "
                "AND rowid=(SELECT rowid FROM entries WHERE id=?)", (new_id,)).fetchone())

    def test_other_project_cannot_use_an_exact_guard(self):
        other = self.project.parent / "other"
        other.mkdir()
        baseline = self.entries()
        reviewer = mock.Mock(side_effect=AssertionError("project mismatch must block dispatch"))
        result = repair_processed_outputs(other, self.data, guard=self.guard,
            replacements=self.replacements, summary_reviewer=reviewer)
        self.assertEqual("repair_evidence_changed", result["code"])
        reviewer.assert_not_called()
        self.assertEqual(baseline, self.entries())

    def test_original_runtime_provider_profile_cannot_be_replaced_by_current_configuration(self):
        configure(self.data, observation_profile=dict(runtime="pi", provider="other-provider",
                  model=MODEL, reasoning_effort=REASONING_EFFORT, timeout=240))
        self.guard = self.inspect([self.note_id])
        reviewer = mock.Mock(side_effect=AssertionError("original profile mismatch"))
        result = repair_processed_outputs(self.project, self.data, guard=self.guard,
            replacements=self.replacements, summary_reviewer=reviewer)
        self.assertEqual("repair_profile_changed", result["code"])
        reviewer.assert_not_called()

    def test_final_transaction_rechecks_output_guard_after_successful_reviews(self):
        commit = Store.commit_observation_repair
        def race(store, *args, **kwargs):
            with Store(self.data) as concurrent:
                concurrent.remember(self.project, "Concurrent correction", "Another correction.",
                                    source_ids=[self.note_id])
            return commit(store, *args, **kwargs)
        with mock.patch.object(Store, "commit_observation_repair", race):
            result = self.repair()
        self.assertEqual("repair_evidence_changed", result["code"], result)
        with Store(self.data) as store:
            records = store.get(self.project, [record["id"] for record in store.timeline(self.project)])
            self.assertFalse(any(record["body"] == self.replacements[self.note_id]["body"] for record in records))
            self.assertIsNone(store.observation_repair_receipt(self.project, "0" * 64))

    def test_storage_failure_rolls_back_partial_successor_and_supersession(self):
        baseline = self.entries()
        with mock.patch.object(Store, "_upsert_metadata", side_effect=StoreError("Synthetic storage failure")):
            result = self.repair()
        self.assertEqual("storage_failure", result["code"], result)
        self.assertEqual(baseline, self.entries())
        with Store(self.data) as store:
            self.assertEqual(self.original_job, store.observation_job_status(self.project, self.job_id))
            self.assertIsNone(store.get(self.project, [self.note_id])[0]["superseded_by"])

    def test_settings_change_after_precommit_guard_is_rechecked_inside_transaction(self):
        baseline = self.entries()
        original_write = Store._write
        entered = []

        def boundary_write(store, operation):
            if operation.__name__ != "commit":
                return original_write(store, operation)

            def changed_inside_transaction():
                self.assertTrue(store._connection.in_transaction)
                entered.append(True)
                configure(self.data, processor_enabled=False)
                return operation()

            return original_write(store, changed_inside_transaction)

        with mock.patch.object(Store, "_write", boundary_write):
            result = self.repair()
        self.assertEqual([True], entered)
        self.assertEqual("repair_evidence_changed", result["code"], result)
        self.assertEqual(baseline, self.entries())
        with Store(self.data) as store:
            self.assertEqual(self.original_job, store.observation_job_status(self.project, self.job_id))
            self.assertIsNone(store.get(self.project, [self.note_id])[0]["superseded_by"])
            self.assertIsNone(store.observation_repair_receipt(self.project, "0" * 64))

    def test_credentials_are_redacted_before_every_semantic_dispatch_and_commit_matches_review(self):
        from tests.test_jev_filter import answer
        from tests.test_jev_quality import response
        configure(self.data, jev_filter_enabled=True, jev_quality_enabled=True)
        self.guard = self.inspect([self.note_id])
        secrets = [f"synthetic-secret-{index}" for index in range(4)]
        self.replacements[self.note_id].update(
            title=f"  Message password={secrets[0]}  ",
            body=f"Reported password={secrets[1]}", tags=[f"password={secrets[2]}"],
            observation=dict(type="discovery", subtitle="", narrative="",
                facts=[f"password={secrets[3]}"], concepts=[], files_read=[], files_modified=[]))
        seen_units = []

        def safe(payload):
            serialized = json.dumps(payload, ensure_ascii=False)
            for secret in secrets:
                self.assertNotIn(secret, serialized)

        def review(payload):
            safe(payload)
            if payload["version"].startswith("summary-support"):
                seen_units.append(copy.deepcopy(payload["units"]))
            return supported_review(payload)

        def filter_evaluate(payload):
            safe(payload)
            return answer()

        def quality_evaluate(payload):
            safe(payload)
            return response(payload)

        result = repair_processed_outputs(self.project, self.data, guard=self.guard,
            replacements=self.replacements, summary_reviewer=review,
            jev_evaluator=filter_evaluate, jev_quality_evaluator=quality_evaluate)
        self.assertEqual("repaired", result["status"], result)
        safe(result)
        with Store(self.data) as store:
            output = store.get(self.project, [result["successors"][self.note_id]])[0]
        safe(output)
        self.assertEqual(output["title"], seen_units[0]["note:0:title"])
        self.assertEqual(output["body"], seen_units[0]["note:0:body"])
        self.assertEqual(output["tags"], seen_units[0]["note:0:tags"])
        self.assertEqual(output["observation"]["facts"], seen_units[0]["note:0:observation:facts"])

    def test_unchanged_sibling_note_with_normalized_empty_metadata_remains_valid(self):
        metadata = dict(type="discovery", subtitle="", narrative="", facts=[], concepts=[],
                        files_read=[], files_modified=[])
        with Store(self.data) as store:
            store.remember(self.project, "Ignored duplicate title", "Ignored duplicate body",
                           dedupe_key=f"observation:{self.job_id}:0", observation=metadata)
            sibling = store.get(self.project, [self.note_id])[0]
        self.assertIsNone(sibling["observation"]["subtitle"])
        self.assertIsNone(sibling["observation"]["narrative"])
        summary_id = self.output_ids[1]
        guard = self.inspect([summary_id])
        replacement = dict(self.summary, completed="The assistant reported stopping checks.")
        result = repair_processed_outputs(self.project, self.data, guard=guard,
            replacements={summary_id: replacement}, summary_reviewer=supported_review)
        self.assertEqual("repaired", result["status"], result)
        with Store(self.data) as store:
            self.assertEqual(sibling, store.get(self.project, [self.note_id])[0])


if __name__ == "__main__":
    unittest.main()
