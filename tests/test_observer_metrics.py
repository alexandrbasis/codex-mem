"""Actual app-server counter semantics and preservation across rejected output."""
from __future__ import annotations

import copy
import unittest
from unittest import mock

from codex_mem.processor import MODEL, NativeProcessorRunner, ProcessorFailure, _TurnMonitor


def usage_message(input_tokens=100, output_tokens=20, *, thread="thread-test", turn="turn-test"):
    counters = {
        "inputTokens": input_tokens, "cachedInputTokens": input_tokens // 2,
        "cacheWriteInputTokens": 0, "outputTokens": output_tokens,
        "reasoningOutputTokens": output_tokens // 2, "totalTokens": input_tokens + output_tokens,
    }
    return {"method": "thread/tokenUsage/updated", "params": {
        "threadId": thread, "turnId": turn,
        "tokenUsage": {"total": counters, "last": counters, "modelContextWindow": 245480},
    }}


def turn_message(method, *, status="completed"):
    return {"method": method, "params": {"threadId": "thread-test", "turn": {
        "id": "turn-test", "status": status, "items": [],
    }}}


class ObserverMonitorTests(unittest.TestCase):
    def monitor(self):
        monitor = _TurnMonitor("thread-test")
        monitor.set_turn("turn-test")
        return monitor

    def test_cumulative_snapshots_replace_instead_of_sum(self):
        monitor = self.monitor()
        monitor.observe(usage_message())
        monitor.observe(usage_message())
        monitor.observe(usage_message(200, 40))
        self.assertEqual("partial", monitor.usage_receipt()["status"])
        monitor.observe(turn_message("turn/completed"))
        result = monitor.usage_receipt()
        self.assertEqual("reported", result["status"])
        self.assertEqual(240, result["tokens"]["total_tokens"])
        self.assertEqual(3, result["updates"])

    def test_thread_and_turn_scope_including_early_notifications(self):
        monitor = _TurnMonitor("thread-test")
        monitor.observe(usage_message(thread="foreign-thread"))
        monitor.observe(usage_message(turn="foreign-turn"))
        monitor.observe(usage_message(200, 40))
        monitor.set_turn("turn-test")
        self.assertEqual(240, monitor.usage_receipt()["tokens"]["total_tokens"])
        self.assertEqual(1, monitor.usage_receipt()["updates"])

    def test_missing_usage_remains_unknown_after_completion(self):
        monitor = self.monitor()
        monitor.observe(turn_message("turn/completed"))
        self.assertEqual("unavailable", monitor.usage_receipt()["status"])
        self.assertIsNone(monitor.usage_receipt()["tokens"])

    def test_measured_zero_is_distinct_from_unknown(self):
        monitor = self.monitor()
        monitor.observe(usage_message(0, 0))
        monitor.observe(turn_message("turn/completed"))
        self.assertEqual("reported", monitor.usage_receipt()["status"])
        self.assertEqual(0, monitor.usage_receipt()["tokens"]["total_tokens"])

    def test_invalid_counters_do_not_reject_memory_or_become_zero(self):
        for key, value in (
            ("inputTokens", True), ("totalTokens", -1), ("totalTokens", 2**63),
            ("totalTokens", 999), ("reasoningOutputTokens", 21),
            ("cachedInputTokens", 101), ("cacheWriteInputTokens", 51),
            ("totalTokens", "private unexpected text"),
        ):
            with self.subTest(key=key, value=value):
                monitor = self.monitor()
                message = copy.deepcopy(usage_message())
                message["params"]["tokenUsage"]["total"][key] = value
                monitor.observe(message)
                monitor.observe(turn_message("turn/completed"))
                self.assertTrue(monitor.completed)
                self.assertEqual("invalid", monitor.usage_receipt()["status"])
                self.assertIsNone(monitor.usage_receipt()["tokens"])
                self.assertNotIn("private unexpected", str(monitor.usage_receipt()))

    def test_decreasing_total_cannot_replace_a_known_higher_count(self):
        monitor = self.monitor()
        monitor.observe(usage_message(200, 40))
        monitor.observe(usage_message())
        monitor.observe(usage_message(300, 60))
        self.assertEqual("invalid", monitor.usage_receipt()["status"])
        self.assertIsNone(monitor.usage_receipt()["tokens"])

    def test_failed_turn_retains_partial_observed_usage(self):
        monitor = self.monitor()
        monitor.observe(usage_message())
        with self.assertRaises(ProcessorFailure):
            monitor.observe(turn_message("turn/completed", status="failed"))
        self.assertEqual("partial", monitor.usage_receipt()["status"])
        self.assertEqual(120, monitor.usage_receipt()["tokens"]["total_tokens"])

    def test_returned_receipt_does_not_mutate_retained_snapshot(self):
        monitor = self.monitor()
        monitor.observe(usage_message())
        monitor.usage_receipt()["tokens"]["total_tokens"] = 999
        self.assertEqual(120, monitor.usage_receipt()["tokens"]["total_tokens"])


class RejectedNativeOutputTests(unittest.TestCase):
    def test_invalid_json_keeps_native_usage_and_specific_reason(self):
        class Client:
            def __init__(self, *args):
                self.events = iter([
                    usage_message(),
                    {"method": "item/completed", "params": {
                        "threadId": "thread-test", "turnId": "turn-test",
                        "item": {"id": "message-test", "type": "agentMessage", "phase": "final_answer", "text": "invalid json"},
                    }},
                    turn_message("turn/completed"),
                ])

            def request(self, method, params, *, notification_handler=None):
                if method == "model/list":
                    return {"data": [{"id": MODEL, "supportedReasoningEfforts": [{"reasoningEffort": "medium"}]}]}
                if method == "config/read":
                    return {"config": {}}
                if method == "thread/start":
                    return {"thread": {"id": "thread-test"}, "model": MODEL, "reasoningEffort": "medium", "modelProvider": "openai"}
                if method == "mcpServerStatus/list":
                    return {"data": []}
                if method == "turn/start":
                    notification_handler(turn_message("turn/started", status="inProgress"))
                    return {"turn": {"id": "turn-test"}}
                return {}

            def send(self, message):
                pass

            def next_notification(self, handler):
                handler(next(self.events))

            def interrupt(self, *args):
                pass

            def close(self):
                pass

        with mock.patch("codex_mem.processor._AppServer", Client), mock.patch("codex_mem.processor.shutil.which", return_value="codex"):
            with self.assertRaises(ProcessorFailure) as raised:
                NativeProcessorRunner().run({"prompt": "fictional fixture", "output_schema": {}})
        error = raised.exception
        self.assertEqual("invalid_response", error.code)
        self.assertEqual("invalid_json", error.reason_code)
        self.assertEqual("reported", error.metrics["usage"]["status"])
        self.assertEqual(120, error.metrics["usage"]["tokens"]["total_tokens"])
        self.assertNotIn("fictional", str(error.metrics))


if __name__ == "__main__":
    unittest.main()
