"""A retained fragment stops only classification, never truncates the evidence."""
import threading
import unittest
from unittest import mock

from codex_mem import jev_filter as jf


def answer(keep):
    category = "problem" if keep else "routine"
    return {"model": jf.MODEL, "answers": {
        "useful": {"type": "noul", "noul": .9 if keep else .01},
        "category": {"type": "choice", "choice": category, "confidence": .99,
                     "probabilities": {name: int(name == category) for name in jf.CATEGORIES}},
    }, "usage": {"input_tokens": 100, "output_tokens": 2}}


def claim():
    return {"sources": [{"id": "s1", "title": "Evidence", "source": "hook:PostToolUse",
                         "body": "prefix " + "x" * 50_000 + " MIDDLE_FINDING " + "y" * 50_000,
                         "tool_io": {"tool_response": "FULL_TOOL_TAIL"}}],
            "context": [], "summary_required": False}


class JevShortCircuitTests(unittest.TestCase):
    def test_first_retained_fragment_preserves_whole_source_and_tool_io(self):
        original = claim()
        evaluator = mock.Mock(return_value=answer(True))
        result, audit = jf.filter_claim(original, evaluator=evaluator)
        candidates = len(list(jf._payloads(original["sources"][0], "sources", False)))
        self.assertGreater(candidates, 3)
        self.assertEqual(evaluator.call_count, 1)
        self.assertEqual(result["sources"], original["sources"])
        self.assertEqual(audit["decisions"][0]["route"], "retain")
        self.assertEqual(audit["counts"]["chunks"], 1)
        self.assertEqual(audit["counts"]["short_circuited_chunks"], candidates - 1)
        self.assertEqual(audit["counts"]["cache_hits"], 0)
        self.assertEqual(audit["usage"], {"input_tokens": 100, "output_tokens": 2})

    def test_routine_prefix_until_useful_middle_is_evaluated_then_full_tail_forwarded(self):
        original = claim()
        fragments = []
        def evaluate(payload):
            text = payload["state"]["source_fragment"]
            fragments.append(text)
            return answer("MIDDLE_FINDING" in text)
        result, audit = jf.filter_claim(original, evaluator=evaluate)
        self.assertGreater(len(fragments), 1)
        self.assertFalse("FULL_TOOL_TAIL" in "".join(fragments))
        self.assertIn("MIDDLE_FINDING", fragments[-1])
        self.assertEqual(result["sources"], original["sources"])
        self.assertGreater(audit["counts"]["short_circuited_chunks"], 0)

    def test_all_routine_requires_every_fragment_and_invalid_prefix_fails_closed(self):
        original = claim()
        evaluator = mock.Mock(return_value=answer(False))
        result, audit = jf.filter_claim(original, evaluator=evaluator)
        self.assertEqual(evaluator.call_count, len(list(jf._payloads(original["sources"][0], "sources", False))))
        self.assertEqual(result["sources"], [])
        self.assertEqual(audit["counts"].get("short_circuited_chunks", 0), 0)
        evaluator = mock.Mock(side_effect=[answer(False), {}])
        with self.assertRaises(jf.JevFilterError) as caught:
            jf.filter_claim(original, evaluator=evaluator)
        self.assertEqual(caught.exception.code, "jev_filter_invalid_response")
        self.assertEqual(caught.exception.audit["decisions"][0]["route"], "incomplete")
        self.assertEqual(caught.exception.audit["counts"].get("short_circuited_chunks", 0), 0)

    def test_cached_retained_prefix_skips_uncached_suffix_without_counting_cache_hits(self):
        original = claim()
        first = next(jf._payloads(original["sources"][0], "sources", False))
        evaluator = mock.Mock(side_effect=AssertionError("suffix should not be requested"))
        result, audit = jf.filter_claim(original, evaluator=evaluator,
            cache_get=lambda payload: answer(True) if payload == first else None)
        self.assertEqual(result["sources"], original["sources"])
        self.assertEqual(audit["counts"]["cache_hits"], 1)
        self.assertEqual(audit["counts"]["requests"], 0)
        self.assertEqual(audit["usage"], {"input_tokens": 0, "output_tokens": 0})
        self.assertGreater(audit["counts"]["short_circuited_chunks"], 0)

    def test_parallel_path_never_submits_suffix_after_first_retained_fragment(self):
        original = claim()
        original["sources"] = [dict(original["sources"][0], id=f"s{i}") for i in range(6)]
        calls = []
        caller = threading.get_ident()
        def post(payload, deadline, key_file):
            calls.append(payload["state"]["serialized_source_offset"])
            return answer(True)
        def put(payload, response):
            self.assertEqual(threading.get_ident(), caller)
        with mock.patch.object(jf, "_post", side_effect=post):
            result, audit = jf.filter_claim(original, cache_put=put)
        self.assertEqual(calls, [0] * 6)
        self.assertEqual(result["sources"], original["sources"])
        self.assertEqual(audit["counts"]["requests"], 6)

    def test_failure_in_another_source_keeps_failure_boundary(self):
        original = claim()
        original["sources"].append({"id": "s2", "title": "Other", "body": "bad"})
        evaluator = mock.Mock(side_effect=[answer(True), {}])
        with self.assertRaises(jf.JevFilterError) as caught:
            jf.filter_claim(original, evaluator=evaluator)
        self.assertEqual(caught.exception.code, "jev_filter_invalid_response")
        self.assertTrue(caught.exception.audit["incomplete"])


if __name__ == "__main__":
    unittest.main()
