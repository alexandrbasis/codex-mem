"""One same-thread correction, strict recheck, and cumulative usage accounting."""
from __future__ import annotations

import json
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest import mock

from codex_mem import processor
from codex_mem.config import configure
from codex_mem.store import Store
from tests.test_jev_quality import note, response
from tests.test_summary_support import supported_review


class FakeServer:
    instances = []
    include_second_usage = True
    fail_second = False
    unsafe_second = False

    def __init__(self, *args):
        self.turns = []
        self.review_turns = []
        self.interrupted = []
        self.closed = False
        type(self).instances.append(self)

    def send(self, value):
        pass

    def request(self, method, params, notification_handler=None):
        if method == "account/read":
            return {"account": {"type": "apiKey"}, "requiresOpenaiAuth": True}
        if method == "thread/start":
            return {"model": processor.MODEL, "reasoningEffort": processor.REASONING_EFFORT,
                    "modelProvider": "openai"}
        if method != "turn/start":
            return {}
        # Authored protocol responses exercise the real native adapter, not model quality.
        review = 'version' in params['outputSchema'].get('properties', {})
        if review:
            version = params['outputSchema']['properties']['version']['enum'][0]
            assert version == 'summary-support-v5', version
            self.review_turns.append(params)
        else:
            self.turns.append(params)
        number = len(self.review_turns) if review else len(self.turns)
        turn_id = f"turn-{number}"
        if not review and number == 2 and self.fail_second:
            raise processor.ProcessorFailure("runner_failure")
        notification_handler({"method": "turn/started", "params": {
            "threadId": "thread-test", "turn": {"id": turn_id}}})
        if number == 1 or self.include_second_usage:
            total = 100 if number == 1 else 160
            notification_handler({"method": "thread/tokenUsage/updated", "params": {
                "threadId": "thread-test", "turnId": turn_id, "tokenUsage": {"total": {
                    "inputTokens": total - 20, "outputTokens": 20, "totalTokens": total,
                    "cachedInputTokens": 0, "cacheWriteInputTokens": 0, "reasoningOutputTokens": 0}}}})
        output = {"notes": [note("s1", observation={"type": "discovery", "subtitle": "", "facts": [], "narrative": "", "concepts": [], "files_read": [], "files_modified": []}, body="Reported local result." if number == 1 else "The assistant reported a test pass.")],
                  "disposition": "processed", "session_summary": None}
        if review:
            payload = json.loads(params['input'][0]['text'].split('\n')[-1])
            text = json.dumps(supported_review(payload)['output'])
        else:
            text = json.dumps({'result': output})
        notification_handler({"method": "turn/completed", "params": {
            "threadId": "thread-test", "turn": {"id": turn_id, "status": "completed", "items": [{
                "type": "agentMessage", "id": f"message-{number}", "phase": "final_answer",
                "text": text}, *(
                    [{"type": "mcpToolCall"}] if not review and number == 2 and self.unsafe_second else [])]}}})
        return {"turn": {"id": turn_id}}

    def interrupt(self, thread, turn):
        self.interrupted.append((thread, turn))

    def close(self):
        self.closed = True


class QualityCorrectionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.home = Path(temporary.name) / "memory"
        self.project = Path(temporary.name) / "project"
        self.project.mkdir()
        configure(self.home, jev_quality_enabled=True)
        with Store(self.home) as store:
            self.raw = store.remember(self.project, "Raw", "The assistant reported a test pass.",
                                      source="hook:PostToolUse", session_id="quality-correction")
        FakeServer.instances = []
        FakeServer.include_second_usage = True
        FakeServer.fail_second = False
        FakeServer.unsafe_second = False
        stack = ExitStack()
        self.addCleanup(stack.close)
        for name, value in (
            ("_AppServer", FakeServer), ("_verify_model_available", lambda *_: None),
            ("_isolated_config_overrides", lambda _: {}),
            ("_verify_thread_start", lambda _, **kwargs: "thread-test"),
            ("_verify_empty_mcp_inventory", lambda *_: None),
        ):
            stack.enter_context(mock.patch.object(processor, name, value))
        stack.enter_context(mock.patch.object(processor.shutil, "which", return_value="/fake/codex"))

    def run_gate(self, final="accept"):
        calls = []
        def evaluator(payload):
            calls.append(payload)
            if final == "transport":
                raise RuntimeError("private transport detail")
            if final == "rejected":
                return response(payload, .01, .99)
            return response(payload, .5, .1) if len(calls) <= 2 or final == "uncertain" else response(payload)
        result = processor.process_pending(self.project, self.home, jev_quality_evaluator=evaluator)
        return result, calls, next(server for server in reversed(FakeServer.instances) if server.turns)

    def assert_usage(self, result, generation_total, generation_status, generation_updates, reviews):
        with Store(self.home) as store:
            usage = store.observation_attempts(self.project, result['job_id'])[0]
        ledger = json.loads(usage['execution_calls'])
        self.assertEqual({'generation', *(f'summary_support_{n}' for n in range(1, reviews + 1))}, set(ledger))
        generation = ledger['generation']['metrics']
        self.assertEqual(generation_total, generation['total_tokens'])
        self.assertEqual(generation_status, generation['usage_status'])
        self.assertEqual(generation_updates, generation['usage_updates'])
        for name, call in ledger.items():
            if name != 'generation':
                self.assertEqual(100, call['metrics']['total_tokens'])
                self.assertEqual('reported', call['metrics']['usage_status'])
                self.assertEqual(1, call['metrics']['usage_updates'])
        self.assertEqual(sum(call['metrics']['total_tokens'] for call in ledger.values()), usage['total_tokens'])
        self.assertEqual(generation_status, usage['usage_status'])
        self.assertEqual(generation_updates + reviews, usage['usage_updates'])
        self.assertEqual(reviews, sum(len(server.review_turns) for server in FakeServer.instances))
        self.assertTrue(all(server.closed for server in FakeServer.instances))
        return usage

    def test_uncertain_candidate_gets_one_same_thread_correction_and_strict_recheck(self):
        result, calls, server = self.run_gate()
        self.assertEqual("processed", result["status"], result)
        self.assertEqual(2, len(server.turns))
        self.assertEqual(["thread-test", "thread-test"], [turn["threadId"] for turn in server.turns])
        self.assertEqual(3, len(calls))  # aggregate + refinement, then corrected aggregate only
        self.assertEqual(3, result["jev_quality"]["counts"]["requests"])
        self.assertEqual(600, result["jev_quality"]["usage"]["input_tokens"])
        self.assertTrue(result["jev_quality"]["correction"]["attempted"])
        feedback = server.turns[1]["input"][0]["text"]
        self.assertIn('"field_path":"body"', feedback)
        self.assertNotIn(self.raw["body"], feedback)
        self.assertNotIn("Reported local result", feedback)
        usage = self.assert_usage(result, 160, 'reported', 2, reviews=2)
        self.assertEqual('turn-2', usage['worker_turn_id'])
        self.assertTrue(server.closed)

    def test_second_uncertain_candidate_is_quarantined_without_third_turn(self):
        result, calls, server = self.run_gate("uncertain")
        self.assertEqual("failed", result["status"])
        self.assertEqual("jev_quality_uncertain", result["reason_code"])
        self.assertEqual(2, len(server.turns))
        self.assertEqual(4, len(calls))
        self.assertEqual(4, result["jev_quality"]["counts"]["requests"])
        with Store(self.home) as store:
            self.assertIsNone(store.get(self.project, [self.raw["id"]])[0]["superseded_by"])
        usage = self.assert_usage(result, 160, 'reported', 2, reviews=2)
        self.assertEqual('turn-2', usage['worker_turn_id'])

    def test_rejected_or_transport_failure_never_starts_correction(self):
        for outcome in ("rejected", "transport"):
            with self.subTest(outcome=outcome):
                # A fresh scope avoids retrying the previous quarantined job.
                self.project = self.project / outcome
                self.project.mkdir()
                with Store(self.home) as store:
                    store.remember(self.project, "Raw", "Reported result", source="hook:PostToolUse")
                result, calls, server = self.run_gate(outcome)
                self.assertEqual("failed", result["status"])
                self.assertEqual(1, len(server.turns))
                self.assertEqual(1, len(calls))
                self.assertNotIn("private transport detail", json.dumps(result))

    def test_second_turn_missing_usage_is_partial_not_first_turn_reported_total(self):
        FakeServer.include_second_usage = False
        result, _, _ = self.run_gate()
        self.assertEqual("processed", result["status"])
        self.assert_usage(result, 100, 'partial', 1, reviews=2)

    def test_second_turn_failure_preserves_first_usage_without_replay(self):
        FakeServer.fail_second = True
        result, calls, server = self.run_gate()
        self.assertEqual("runner_failure", result["code"])
        self.assertEqual(2, len(server.turns))
        self.assertEqual(2, len(calls))
        self.assertTrue(result["jev_quality"]["correction"]["requested"])
        self.assert_usage(result, 100, 'partial', 1, reviews=1)

    def test_correction_turn_cannot_execute_tools(self):
        FakeServer.unsafe_second = True
        result, calls, server = self.run_gate()
        self.assertEqual("tool_called", result["code"])
        self.assertEqual(2, len(server.turns))
        self.assertEqual(2, len(calls))

    def test_first_accepted_candidate_never_gets_a_correction(self):
        result = processor.process_pending(self.project, self.home, jev_quality_evaluator=response)
        self.assertEqual("processed", result["status"])
        self.assertEqual(1, sum(len(server.turns) for server in FakeServer.instances))
        self.assert_usage(result, 100, 'reported', 1, reviews=1)
        self.assertEqual(1, result["jev_quality"]["counts"]["requests"])
        self.assertNotIn("correction", result["jev_quality"])

    def test_candidate_review_cannot_extend_native_deadline(self):
        clock = [0.0]
        def review(output, remaining):
            self.assertEqual(5, remaining)
            clock[0] = 6
            return "Correction requested"
        runner = processor.NativeProcessorRunner(timeout=5, candidate_review=review)
        with mock.patch.object(processor.time, "monotonic", side_effect=lambda: clock[0]):
            with self.assertRaises(processor.ProcessorFailure) as raised:
                runner({"prompt": "Original evidence", "output_schema": {"properties": {"result": {}}}})
        self.assertEqual("timeout", raised.exception.code)
        self.assertEqual(1, len(FakeServer.instances[-1].turns))


if __name__ == "__main__":
    unittest.main()
