"""Actual app-server counter semantics and preservation across rejected output."""
from __future__ import annotations

import copy
import unittest
from unittest import mock

from codex_mem.processor import MODEL, NativeProcessorRunner, ProcessorFailure, _TurnMonitor, _UsageCheckpointer


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
        snapshots = []
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
                NativeProcessorRunner(usage_checkpoint=lambda value: snapshots.append(copy.deepcopy(value))).run(
                    {"prompt": "fictional fixture", "output_schema": {}})
        error = raised.exception
        self.assertEqual("invalid_response", error.code)
        self.assertEqual("invalid_json", error.reason_code)
        self.assertEqual("reported", error.metrics["usage"]["status"])
        self.assertEqual(120, error.metrics["usage"]["tokens"]["total_tokens"])
        self.assertNotIn("fictional", str(error.metrics))
        self.assertEqual("thread-test", snapshots[0]["worker_thread_id"])
        self.assertIsNone(snapshots[0]["worker_turn_id"])
        self.assertTrue(any(item["metrics"]["usage"]["status"] == "partial" for item in snapshots))
        self.assertEqual("reported", snapshots[-1]["metrics"]["usage"]["status"])
        self.assertEqual(120, snapshots[-1]["metrics"]["usage"]["tokens"]["total_tokens"])
        self.assertNotIn("fictional", str(snapshots))
        self.assertNotIn("invalid json", str(snapshots))


class ObserverCheckpointTests(unittest.TestCase):
    def snapshot(self, input_tokens=100, output_tokens=20, *, completed=False):
        monitor = _TurnMonitor("thread-test")
        monitor.set_turn("turn-test")
        monitor.observe(usage_message(input_tokens, output_tokens))
        if completed:
            monitor.observe(turn_message("turn/completed"))
        return {"worker_thread_id": "thread-test", "worker_turn_id": "turn-test",
                "metrics": {"duration_ms": 10, "usage": monitor.usage_receipt()}}

    def test_first_snapshot_throttled_updates_and_forced_flush(self):
        saved = []
        checkpointer = _UsageCheckpointer(lambda value: saved.append(copy.deepcopy(value)))
        now = [0.0]
        with mock.patch("codex_mem.processor.time.monotonic", side_effect=lambda: now[0]):
            first = self.snapshot()
            checkpointer.save(first)
            for index in range(100):
                duplicate = copy.deepcopy(first)
                duplicate["metrics"]["usage"]["updates"] = index + 2
                checkpointer.save(duplicate)
            now[0] = 0.2
            checkpointer.save(self.snapshot(200, 40))
            self.assertEqual(1, len(saved))
            now[0] = 1.1
            checkpointer.save(self.snapshot(200, 40))
            now[0] = 1.2
            checkpointer.save(self.snapshot(300, 60))
            self.assertEqual(2, len(saved))
            checkpointer.save(self.snapshot(300, 60), force=True)
            self.assertEqual([120, 240, 360],
                             [item["metrics"]["usage"]["tokens"]["total_tokens"] for item in saved])
            checkpointer.save(self.snapshot(300, 60, completed=True))
            self.assertEqual("reported", saved[-1]["metrics"]["usage"]["status"])
            self.assertEqual(4, len(saved))

    def test_storage_failure_retries_without_rejecting_native_usage(self):
        callback = mock.Mock(side_effect=[OSError("private storage exception"), None])
        checkpointer = _UsageCheckpointer(callback)
        with mock.patch("codex_mem.processor.time.monotonic", return_value=0.0):
            checkpointer.save(self.snapshot())
            checkpointer.save(self.snapshot())
            self.assertEqual(1, callback.call_count)
            checkpointer.save(self.snapshot(), force=True)
        self.assertEqual(2, callback.call_count)

    def test_timeout_and_interruption_flush_latest_throttled_snapshot(self):
        for failure in (ProcessorFailure("timeout"), KeyboardInterrupt()):
            with self.subTest(failure=type(failure).__name__):
                saved = []
                client = mock.Mock()
                responses = {
                    "initialize": {}, "config/read": {"config": {}},
                    "model/list": {"data": [{"id": MODEL, "supportedReasoningEfforts": [{"reasoningEffort": "medium"}]}]},
                    "thread/start": {"thread": {"id": "thread-test"}, "model": MODEL,
                                     "reasoningEffort": "medium", "modelProvider": "openai"},
                    "mcpServerStatus/list": {"data": []},
                    "turn/start": {"turn": {"id": "turn-test"}},
                }
                client.request.side_effect = lambda method, *args, **kwargs: responses[method]
                events = iter([usage_message(), usage_message(200, 40), failure])

                def notify(handler):
                    event = next(events)
                    if isinstance(event, BaseException):
                        raise event
                    handler(event)

                client.next_notification.side_effect = notify
                with mock.patch("codex_mem.processor._AppServer", return_value=client), \
                        mock.patch("codex_mem.processor.shutil.which", return_value="codex"), \
                        mock.patch("codex_mem.processor.time.monotonic", return_value=0.0):
                    with self.assertRaises(type(failure)):
                        NativeProcessorRunner(usage_checkpoint=lambda value: saved.append(copy.deepcopy(value))).run(
                            {"prompt": "private fixture", "output_schema": {}})
                self.assertEqual("partial", saved[-1]["metrics"]["usage"]["status"])
                self.assertEqual(240, saved[-1]["metrics"]["usage"]["tokens"]["total_tokens"])
                self.assertEqual(1, client.close.call_count)
                self.assertNotIn("private fixture", str(saved))


if __name__ == "__main__":
    unittest.main()
