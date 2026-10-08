"""Synthetic transport checks for the shared Jev HTTP concurrency bound."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
import json
from threading import Barrier, Event, Lock
import time
from types import SimpleNamespace
import unittest
from unittest import mock

from codex_mem import jev_filter, jev_quality, request_limits


def claim():
    return {"sources": [{"id": "s", "title": "Decision", "body": "Keep offline access.",
                         "source": "hook:UserPromptSubmit"}], "context": [], "summary_required": False}


def quality():
    return jev_quality.quality_gate(
        [{"title": "Decision", "body": "Keep offline access.", "source_ids": ["s"]}],
        None, claim(), project="/synthetic-concurrency", timeout=5,
    )


def answer(payload):
    if "useful" in payload["questions"]:
        answers = {"useful": {"type": "noul", "noul": .99}, "category": {
            "type": "choice", "choice": "decision", "confidence": .99,
            "probabilities": {key: float(key == "decision") for key in jev_filter.CATEGORIES}}}
    else:
        answers = {key: {"type": "noul", "noul": .99 if key.endswith("grounded") else .01}
                   for key in payload["questions"]}
    return {"model": jev_filter.MODEL, "answers": answers,
            "usage": {"input_tokens": 1, "output_tokens": 1}}


class RequestLimitTests(unittest.TestCase):
    def test_mixed_filter_and_quality_calls_share_four_slots_until_reads_finish(self):
        start = Barrier(9)
        four_open = Event()
        fifth_open = Event()
        release_reads = Event()
        lock = Lock()
        active = maximum = calls = 0

        class Response:
            status = 200
            fp = SimpleNamespace(raw=SimpleNamespace(_sock=SimpleNamespace(settimeout=lambda _: None)))

            def __init__(self, payload):
                self.payload = payload

            def __enter__(self):
                return self

            def __exit__(self, *args):
                nonlocal active
                with lock:
                    active -= 1

            def read1(self, size):
                if not release_reads.wait(5):
                    raise TimeoutError()
                return json.dumps(answer(self.payload)).encode()

            def isclosed(self):
                return True

        def open_request(request, timeout):
            nonlocal active, maximum, calls
            with lock:
                active += 1
                maximum = max(maximum, active)
                calls += 1
                if calls == 4:
                    four_open.set()
                if calls == 5:
                    fifth_open.set()
            return Response(json.loads(request.data))

        def run(index):
            start.wait(5)
            return (jev_filter.filter_claim(claim(), timeout=5) if index % 2 else quality())

        opener = mock.Mock()
        opener.open.side_effect = open_request
        with mock.patch.object(jev_filter, "_credentials", return_value="synthetic-key"), \
                mock.patch.object(jev_filter.urllib.request, "build_opener", return_value=opener), \
                ThreadPoolExecutor(max_workers=8) as pool:
            futures = [pool.submit(run, index) for index in range(8)]
            start.wait(5)
            try:
                self.assertTrue(four_open.wait(3))
                self.assertFalse(fifth_open.wait(.1), "permit released before reading finished")
            finally:
                release_reads.set()
            for future in futures:
                future.result(timeout=5)
        self.assertEqual(4, maximum)
        self.assertEqual(8, calls)
        self.assertEqual(0, active)

    def test_waiting_deadline_maps_to_existing_errors_without_dispatch(self):
        with ExitStack() as occupied:
            for _ in range(4):
                occupied.enter_context(request_limits.acquire_jev_slot(time.monotonic() + 1))
            with mock.patch.object(jev_filter, "_credentials", return_value="synthetic-key"), \
                    mock.patch.object(jev_filter.urllib.request, "build_opener") as build:
                with self.assertRaises(jev_filter.JevFilterError) as caught:
                    jev_filter._post({}, time.monotonic() + .02)
                self.assertEqual("jev_filter_timeout", caught.exception.code)
                with self.assertRaises(jev_quality.JevQualityError) as caught:
                    jev_quality.quality_gate(
                        [{"title": "Decision", "body": "Keep offline access.", "source_ids": ["s"]}],
                        None, claim(), project="/synthetic-concurrency", timeout=.02,
                    )
                self.assertEqual("jev_timeout", caught.exception.audit["evaluations"][0]["error_code"])
                build.assert_not_called()
        self.assert_all_slots_available()

    def assert_all_slots_available(self):
        with ExitStack() as slots:
            for _ in range(4):
                slots.enter_context(request_limits.acquire_jev_slot(time.monotonic() + .2))

    def test_transport_and_response_failures_release_slots_and_hide_error_bodies(self):
        for failure in (OSError("private-server-body"), TimeoutError("private-server-body"),
                        ValueError("private-server-body")):
            with self.subTest(failure=type(failure).__name__):
                opener = mock.Mock()
                opener.open.side_effect = failure
                with mock.patch.object(jev_filter, "_credentials", return_value="synthetic-key"), \
                        mock.patch.object(jev_filter.urllib.request, "build_opener", return_value=opener):
                    with self.assertRaises(jev_filter.JevFilterError) as caught:
                        jev_filter._post({}, time.monotonic() + 1)
                self.assertNotIn("private", str(caught.exception))
                self.assert_all_slots_available()
        for status, read_error in ((503, None), (200, OSError("private-body")),
                                   (200, TimeoutError("private-body"))):
            with self.subTest(status=status, read_error=read_error):
                response = mock.MagicMock()
                response.status = status
                response.__enter__.return_value = response
                response.read1.side_effect = read_error
                opener = mock.Mock()
                opener.open.return_value = response
                with mock.patch.object(jev_filter, "_credentials", return_value="synthetic-key"), \
                        mock.patch.object(jev_filter.urllib.request, "build_opener", return_value=opener):
                    with self.assertRaises(jev_filter.JevFilterError):
                        jev_filter._post({}, time.monotonic() + 1)
                response.__exit__.assert_called_once()
                self.assert_all_slots_available()

    def test_expired_deadline_and_exception_in_body_do_not_leak_permits(self):
        with self.assertRaisesRegex(request_limits.JevSlotTimeout, "^jev_slot_timeout$"):
            with request_limits.acquire_jev_slot(time.monotonic() - 1):
                self.fail("expired deadline entered the body")
        with self.assertRaises(ValueError):
            with request_limits.acquire_jev_slot(time.monotonic() + 1):
                raise ValueError()
        self.assert_all_slots_available()

    def test_permit_obtained_at_deadline_is_released_without_entering_body(self):
        semaphore = mock.Mock()
        semaphore.acquire.return_value = True
        with mock.patch.object(request_limits, "_jev_slots", semaphore), \
                mock.patch.object(request_limits.time, "monotonic", side_effect=[10, 11]):
            with self.assertRaises(request_limits.JevSlotTimeout):
                with request_limits.acquire_jev_slot(11):
                    self.fail("late permit entered the body")
        semaphore.acquire.assert_called_once_with(timeout=1)
        semaphore.release.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
