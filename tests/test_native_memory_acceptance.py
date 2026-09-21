from __future__ import annotations

import importlib.util
from pathlib import Path
import sys
import unittest


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))
SPEC = importlib.util.spec_from_file_location("native_memory_acceptance", SCRIPTS / "native_memory_acceptance.py")
assert SPEC and SPEC.loader
probe = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(probe)


def answer(**updates):
    value = {"retry_limit": 11, "local_status": "completed", "duplicate_rejection": "verified",
             "production_status": "unverified", "local_work_remaining": False, "evidence": "SQLite UNIQUE regression passed"}
    value.update(updates)
    return value


def run(**updates):
    value = {"status": "passed", "answer": answer(), "prompt_sha256": "same", "model": "model",
             "reasoning_effort": "medium", "source_file_sha256": "same-source", "project": "/fixture",
             "shell_enabled": True, "main_duration_seconds": 1.0,
             "usage": {"status": "reported", "tokens": {"total_tokens": 100}}, "tool_items": []}
    value.update(updates)
    return value


class NativeMemoryAcceptanceTests(unittest.TestCase):
    def test_meaningful_stop_requires_note_summary_and_native_tool_provenance(self):
        snapshot = {"jobs": [{"status": "processed", "model": probe.MODEL, "reasoning_effort": probe.REASONING_EFFORT,
                             "worker_thread_id": "worker", "worker_turn_id": "turn", "output_ids": ["note", "summary"]}],
                    "tool_captures": [{"entry_id": "tool"}], "derived_output_cites_tool": True,
                    "derived_outputs": [{"kind": "note"}, {"kind": "session_summary"}]}
        self.assertTrue(all(probe.processing_checks(snapshot, "main", expected="processed", require_note=True).values()))
        snapshot["derived_outputs"] = [{"kind": "note"}]
        self.assertFalse(all(probe.processing_checks(snapshot, "main", expected="processed", require_note=True).values()))

    def test_routine_stop_skip_requires_no_outputs_and_native_worker(self):
        snapshot = {"jobs": [{"status": "skipped", "model": probe.MODEL, "reasoning_effort": probe.REASONING_EFFORT,
                             "worker_thread_id": "worker", "worker_turn_id": "turn", "output_ids": []}], "derived_outputs": []}
        self.assertTrue(all(probe.processing_checks(snapshot, "main", expected="skipped").values()))
        snapshot["jobs"][0]["output_ids"] = ["unexpected-note"]
        self.assertFalse(all(probe.processing_checks(snapshot, "main", expected="skipped").values()))

    def test_zero_turn_fixture_proves_before_and_after_without_answer_in_prompt(self):
        result = probe.dry_run()
        self.assertEqual("passed", result["status"])
        self.assertEqual(0, result["model_turns"])

    def test_wrong_completion_or_production_claim_cannot_pass(self):
        self.assertTrue(all(probe.answer_checks(answer()).values()))
        for update in ({"retry_limit": 4}, {"local_status": "unfinished"},
                       {"local_work_remaining": True}, {"production_status": "verified"}):
            self.assertFalse(all(probe.answer_checks(answer(**update)).values()))

    def test_empty_project_requires_unknown_instead_of_guessed_fact(self):
        unknown = {"retry_limit": None, "local_status": "unknown", "duplicate_rejection": "unknown",
                   "production_status": "unknown", "local_work_remaining": None, "evidence": "absent"}
        self.assertTrue(all(probe.answer_checks(unknown, absent=True).values()))
        self.assertFalse(all(probe.answer_checks(answer(), absent=True).values()))

    def test_pair_with_unequal_sources_or_answers_is_not_comparable(self):
        self.assertEqual("passed", probe.paired_comparison(run(), run())["status"])
        for changed in (run(source_file_sha256="different"), run(answer=answer(production_status="verified")),
                        run(shell_enabled=False), run(prompt_sha256="different")):
            result = probe.paired_comparison(run(), changed)
            self.assertEqual("incomparable", result["status"])
            self.assertIsNone(result["main_tokens_with_minus_without"])

    def test_missing_usage_never_becomes_zero(self):
        result = probe.paired_comparison(run(), run(usage={"status": "unavailable", "tokens": None}))
        self.assertEqual("passed", result["status"])
        self.assertIsNone(result["without_memory_main_tokens"])
        self.assertIsNone(result["main_tokens_with_minus_without"])

    def test_usage_total_updates_replace_previous_snapshot(self):
        events = probe.Events()
        events.set_thread("thread")
        events.set_turn("turn")
        for total in (100, 150):
            events.observe({"method": "thread/tokenUsage/updated", "params": {"threadId": "thread", "turnId": "turn",
                "tokenUsage": {"total": {"inputTokens": total - 10, "cachedInputTokens": 0, "cacheWriteInputTokens": 0,
                    "outputTokens": 10, "reasoningOutputTokens": 5, "totalTokens": total}}}})
        self.assertEqual(150, events.usage_receipt()["tokens"]["total_tokens"])
        self.assertEqual(2, events.usage_receipt()["updates"])


if __name__ == "__main__":
    unittest.main()
