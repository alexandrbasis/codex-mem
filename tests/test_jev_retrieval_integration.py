"""Context and manual search share bounded Jev relevance judgments."""
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch
import xml.etree.ElementTree as ET

from codex_mem import semantic
from codex_mem.config import configure
from codex_mem.hooks import handle_hook
from codex_mem.jev_retrieval import rerank
from codex_mem.store import Store
from tests.test_jev_retrieval import response


class RetrievalIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.project = self.root / "project"
        self.store = Store(self.root / "data")
        self.addCleanup(self.store.close)
        configure(self.store.data_dir, capture_scope="all", jev_retrieval_enabled=True,
                  semantic_enabled=False, processor_enabled=False, service_enabled=False)

    def note(self, title, body, **kwargs):
        return self.store.remember(self.project, title, body, **kwargs)

    def ids(self, query, **kwargs):
        markup = self.store.context(self.project, query=query, **kwargs)
        return [item.attrib["id"] for item in ET.fromstring(markup).findall("entry")]

    def test_context_cross_language_recall_preserves_wrapper_budget_and_provenance(self):
        target = self.note("Payment idempotency", "The payment idempotency key prevents duplicate charges.",
            observation={"type": "decision", "facts": ["A retry reuses the same payment key."]})
        self.note("Header color", "The panel heading is blue.")
        query = "Как мы решили защищаться от повторного списания?"
        configure(self.store.data_dir, jev_retrieval_enabled=False)
        self.assertEqual([], self.ids(query))
        configure(self.store.data_dir, jev_retrieval_enabled=True)
        with patch("codex_mem.semantic._backend", side_effect=AssertionError("no hook embedder")):
            markup = self.store.context(self.project, query=query, budget=2000,
                jev_evaluator=lambda payload: response(payload, relevance=lambda item:
                    .99 if "idempotency" in item["title"] else .01))
        root = ET.fromstring(markup)
        self.assertEqual("true", root.attrib["untrusted"])
        self.assertIsNotNone(root.find("freshness"))
        self.assertEqual([target["id"]], [item.attrib["id"] for item in root.findall("entry")])
        self.assertEqual("not_assessed", root.find("entry").attrib["verification"])
        self.assertLessEqual(len(markup), 2000)

    def test_manual_search_reranks_before_limit_and_returns_previews(self):
        self.note("Checkout", "Checkout checkout checkout panel styling.")
        target = self.note("Checkout idempotency decision", "Checkout uses an idempotency key to prevent duplicate payments.")
        receipt = semantic.search(self.store, self.project, "checkout", mode="lexical", limit=1,
            jev_evaluator=lambda payload: response(payload, scores=lambda item:
                3 if "idempotency" in item["title"] else 1))
        self.assertEqual([target["id"]], [item["id"] for item in receipt["results"]])
        self.assertIn("preview", receipt["results"][0])
        self.assertNotIn("body", receipt["results"][0])
        self.assertEqual("ranked", receipt["jev_retrieval"]["status"])

    def test_disabled_baseline_and_failed_overfetch_have_identical_records(self):
        for index in range(15):
            self.note("Checkout " + "details " * index, "Checkout observation.")
        configure(self.store.data_dir, jev_retrieval_enabled=False)
        baseline = semantic.search(self.store, self.project, "checkout", mode="lexical", limit=2)
        configure(self.store.data_dir, jev_retrieval_enabled=True)
        failed = semantic.search(self.store, self.project, "checkout", mode="lexical", limit=2,
            jev_evaluator=lambda _: (_ for _ in ()).throw(RuntimeError("PRIVATE_REMOTE_ERROR")))
        self.assertEqual(baseline["results"], failed["results"])
        self.assertNotIn("PRIVATE_REMOTE_ERROR", json.dumps(failed))

    def test_context_failure_preserves_identical_context(self):
        self.note("Checkout decision", "Checkout key reuse prevents double charging.")
        configure(self.store.data_dir, jev_retrieval_enabled=False)
        baseline = self.store.context(self.project, query="checkout")
        configure(self.store.data_dir, jev_retrieval_enabled=True)
        failed = self.store.context(self.project, query="checkout", jev_evaluator=lambda _: {})
        self.assertEqual(baseline, failed)

    def test_failed_hybrid_overfetch_preserves_the_original_rrf_window(self):
        records = [self.note("Checkout candidate " + str(index), "Checkout evidence.") for index in range(16)]
        lexical, dense = records[:8], [*records[8:12], records[1], *records[12:]]
        def lex(_store, _project, _query, limit, *_args):
            return lexical[:limit]
        def sem(_store, _project, _vector, limit, *_args):
            return dense[:limit]
        class Backend:
            ready = True
            unavailable_code = None
            def split(self, text, *, prefix=""):
                return [prefix + text]
            def embed(self, texts):
                return [[1] for _ in texts]
        with patch.object(semantic, "_lexical_results", side_effect=lex), \
             patch.object(semantic, "_semantic_results", side_effect=sem), \
             patch.object(semantic, "_normalized_query_vector", return_value=[1]):
            configure(self.store.data_dir, jev_retrieval_enabled=False)
            baseline = semantic.search(self.store, self.project, "checkout", mode="hybrid", limit=1, backend=Backend())
            configure(self.store.data_dir, jev_retrieval_enabled=True)
            failed = semantic.search(self.store, self.project, "checkout", mode="hybrid", limit=1,
                                     backend=Backend(), jev_evaluator=lambda _: {})
        # Preview adapters must supply previews, which the real Store already
        # does. Compare the selected source ID, independent of this test seam.
        self.assertEqual([item["id"] for item in baseline["results"]],
                         [item["id"] for item in failed["results"]])

    def test_current_state_paraphrase_orders_by_source_time_and_keeps_open_work(self):
        old = self.note("Original implementation", "Old work was reported completed.", session_id="older",
                        kind="session_summary", session_summary={"completed": "Old release shipped."})
        current = self.note("Latest handoff", "The implementation is merged; the production check is pending.",
                           session_id="new", kind="session_summary",
                           session_summary={"completed": "Implementation merged.", "next_steps": "Production check remains."})
        open_work = self.note("Independent retry failure", "Retry recovery still needs verification.",
                             observation={"type": "discovery"})
        with self.store._lock:
            for item, instant in ((old, "2025-01-01T00:00:00Z"), (current, "2026-09-01T00:00:00Z"),
                                  (open_work, "2026-09-02T00:00:00Z")):
                self.store._connection.execute("UPDATE entries SET created_at=? WHERE id=?", (instant, item["id"]))
        query = "Give me the latest state of the project and any unfinished items"
        evaluator = lambda payload: response(payload, current=True,
            scores=lambda item: 3 if "Original" in item["title"] else 2)
        ids = self.ids(query, jev_evaluator=evaluator)
        self.assertEqual(current["id"], ids[0])
        self.assertIn(open_work["id"], ids[:2])
        result = semantic.search(self.store, self.project, query, mode="lexical", intent="resume", limit=2,
                                 jev_evaluator=evaluator)
        self.assertEqual([current["id"], open_work["id"]], [item["id"] for item in result["results"]])

    def test_expansion_pool_is_balanced_and_collapses_repeated_session_summaries(self):
        for index in range(15):
            self.note("Handoff " + str(index), "Checkout details.", session_id="same",
                      kind="session_summary", session_summary={"next_steps": "Checkout validation."})
        for index in range(20):
            self.note("Observation " + str(index), "Checkout finding.", observation={"type": "discovery"})
        pool = self.store.retrieval_candidates(self.project)
        self.assertLessEqual(len(pool), 12)
        self.assertEqual(1, sum(item["kind"] == "session_summary" for item in pool))
        self.assertEqual("session_summary", pool[0]["kind"])
        self.assertNotEqual("session_summary", pool[1]["kind"])

    def test_context_expansion_preserves_all_caller_filters_and_excludes_current_session(self):
        accepted = self.note("Payment key", "A retry cannot duplicate a payment.", session_id="old",
                            observation={"type": "decision", "files_modified": ["checkout.py"], "concepts": ["payments"]})
        self.note("Private current", "Current session material.", session_id="now",
                  observation={"type": "decision", "files_modified": ["checkout.py"], "concepts": ["payments"]})
        self.note("Wrong type", "A finding.", observation={"type": "discovery"})
        payloads = []
        ids = self.ids("Почему нет повторных списаний?", exclude_session="now", types=["decision"],
            kinds=["note"], files=["checkout.py"], concepts=["payments"],
            jev_evaluator=lambda payload: (payloads.append(payload) or response(payload)))
        self.assertEqual([accepted["id"]], ids)
        self.assertEqual(1, len(payloads[0]["state"]["candidates"]))

    def test_empty_context_never_invokes_jev(self):
        self.note("Known note", "A useful fact.")
        self.store.context(self.project, jev_evaluator=lambda _: self.fail("empty context requested Jev"))

    def test_actual_hook_retains_full_prompt_privacy_before_query_clipping(self):
        target = self.note("Checkout payments", "Checkout payments use a retry key.")
        self.note("Unrelated item", "A different finding.")
        prompt = "Please review checkout payments. " + "ordinary context " * 75 + "<private>PRIVATE_MARKER</private>"
        self.assertGreater(len(prompt), 1000)
        for gate_enabled, padding in ((True, ""), (False, ""), (True, " extra public text " * 400)):
            with self.subTest(gate_enabled=gate_enabled, long_prompt=bool(padding)):
                configure(self.store.data_dir, private_prompt_gate=gate_enabled)
                payload = {"hook_event_name": "UserPromptSubmit", "cwd": str(self.project),
                           "session_id": "private-session-" + str(gate_enabled) + str(bool(padding)), "turn_id": "private-turn",
                           "prompt": padding + prompt}
                with patch("codex_mem.jev_client.evaluate", side_effect=AssertionError("private query sent")) as evaluate:
                    actual = handle_hook(payload, self.store)
                evaluate.assert_not_called()
                self.assertIn("hookSpecificOutput", actual)
                self.assertNotIn("PRIVATE_MARKER", json.dumps(actual))

    def test_actual_hook_respects_current_private_gate_without_changing_local_injection(self):
        self.note("Checkout", "Checkout uses a retry key.")
        payload = {"hook_event_name": "UserPromptSubmit", "cwd": str(self.project),
                   "session_id": "gated-session", "turn_id": "gated-turn", "prompt": "checkout"}
        with patch("codex_mem.hooks._private_tool_gate_active", return_value=True), \
             patch("codex_mem.jev_client.evaluate", side_effect=AssertionError("gated query sent")) as evaluate:
            actual = handle_hook(payload, self.store)
        evaluate.assert_not_called()
        self.assertIn("Checkout uses a retry key", json.dumps(actual))

    def test_late_response_preserves_original_records(self):
        note = self.note("Checkout", "Useful finding.")
        def delayed(payload):
            time.sleep(.03)
            return response(payload)
        ranked, receipt = rerank(self.store, self.project, "checkout", [note], timeout=.02, evaluator=delayed)
        self.assertEqual([note], ranked)
        self.assertEqual("fallback_error", receipt["status"])
        self.assertEqual("jev_timeout", receipt["error_code"])

    def test_actual_hook_spent_local_budget_skips_remote_and_returns_before_host_timeout(self):
        note = self.note("Checkout", "Checkout retry keys prevent duplicate payments.")
        original = self.store.resume_metadata
        def slow_local(*args, **kwargs):
            time.sleep(1.7)
            return original(*args, **kwargs)
        def slow_remote(**kwargs):
            time.sleep(1.4)
            raise RuntimeError("remote response should not be requested")
        payload = {"hook_event_name": "UserPromptSubmit", "cwd": str(self.project),
                   "session_id": "budget-session", "turn_id": "budget-turn", "prompt": "checkout"}
        started = time.monotonic()
        with patch.object(self.store, "resume_metadata", side_effect=slow_local), \
             patch("codex_mem.jev_client.evaluate", side_effect=slow_remote) as evaluate:
            actual = handle_hook(payload, self.store)
        elapsed = time.monotonic() - started
        evaluate.assert_not_called()
        self.assertLess(elapsed, 3.0)
        self.assertIn(note["id"], json.dumps(actual))
        self.assertIn("Checkout retry keys prevent duplicate payments", json.dumps(actual))

    def test_actual_hook_with_budget_still_uses_bounded_remote_ranking(self):
        self.note("Checkout", "Checkout retry keys prevent duplicate payments.")
        payload = {"hook_event_name": "UserPromptSubmit", "cwd": str(self.project),
                   "session_id": "available-session", "turn_id": "available-turn", "prompt": "checkout"}
        deadlines = []
        def evaluator(payload, deadline, key_file):
            deadlines.append(deadline - time.monotonic())
            return response(payload)
        with patch("codex_mem.jev_client._post", side_effect=evaluator):
            actual = handle_hook(payload, self.store)
        self.assertEqual(1, len(deadlines))
        self.assertGreater(deadlines[0], 0)
        self.assertLessEqual(deadlines[0], 1.5)
        self.assertIn("hookSpecificOutput", actual)
