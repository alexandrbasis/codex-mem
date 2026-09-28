"""Typed retrieval plumbing; controlled judgments do not claim model accuracy."""
from pathlib import Path
import json
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from codex_mem.config import configure
from codex_mem.jev_client import MODEL, JevError
from codex_mem.jev_retrieval import MAX_CANDIDATES, rerank
from codex_mem.store import Store, StoreError


def response(payload, *, relevance=None, scores=None, current=False, confidence=1.0):
    """Build a strict official response including a self-contained Score legend."""
    answers = {}
    for key, question in payload["questions"].items():
        if key == "current_state":
            answers[key] = {"type": "noul", "noul": .98 if current else .02}
            continue
        index = int(key.rsplit("_", 1)[1])
        candidate = payload["state"]["candidates"][index]
        relevant = relevance(candidate) if callable(relevance) else (.98 if relevance is None else relevance)
        if key.startswith("relevant_"):
            answers[key] = {"type": "noul", "noul": relevant}
        else:
            score = scores(candidate) if callable(scores) else (3 if relevant >= .8 else 0)
            answers[key] = {"type": "score", "score": float(score), "confidence": confidence,
                "legend": {str(i): value for i, value in enumerate(question["criteria"])},
                "probabilities": {str(i): max(0., 1. - abs(i - score)) for i in range(len(question["criteria"]))}}
    return {"model": MODEL, "answers": answers, "usage": {"input_tokens": 50, "output_tokens": 20}}


class JevRetrievalTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.project = self.root / "project"
        self.store = Store(self.root / "data")
        self.addCleanup(self.store.close)
        self.settings = configure(self.store.data_dir, capture_scope="all", jev_retrieval_enabled=True)

    def note(self, title, body="A durable finding.", **kwargs):
        return self.store.remember(self.project, title, body, **kwargs)

    def rank(self, query, records=(), **kwargs):
        return rerank(self.store, self.project, query, records, settings=self.settings, **kwargs)

    def test_cross_language_candidates_are_recalled_with_typed_judgments(self):
        target = self.note("Payment idempotency", "We chose a payment idempotency key to prevent duplicate charges.")
        self.note("Panel color", "Use a blue heading.")
        for query in ("Как мы решили защищаться от повторного списания?",
                      "Напомни, почему выбрали ключ идемпотентности?"):
            with self.subTest(query=query):
                ranked, receipt = self.rank(query, evaluator=lambda payload: response(
                    payload, relevance=lambda item: .98 if "idempotency" in item["title"] else .01))
                self.assertEqual([target["id"]], [item["id"] for item in ranked])
                self.assertEqual(1, receipt["requests"])
                self.assertEqual(1, receipt["added_candidates"])
                self.assertTrue(receipt["audit_recorded"])

    def test_no_match_and_uncertain_expansion_do_not_invent_context(self):
        self.note("Payment idempotency")
        for suffix, relevance in (("unrelated", .02), ("ambiguous", .5), ("insufficient", .79)):
            with self.subTest(relevance=relevance):
                calls = []
                ranked, receipt = self.rank("A different question " + suffix,
                    evaluator=lambda payload: (calls.append(payload) or response(payload, relevance=relevance)))
                self.assertEqual(1, len(calls))
                self.assertEqual([], ranked)

    def test_failure_invalid_answers_and_uncertainty_preserve_baseline_exactly(self):
        first, second = self.note("Checkout first"), self.note("Checkout second")
        baseline = [dict(first, score=.4), dict(second, score=.1)]
        evaluators = [lambda payload: (_ for _ in ()).throw(JevError("jev_timeout")),
                      lambda payload: {"model": MODEL, "answers": {}},
                      lambda payload: response(payload, relevance=.5)]
        for index, evaluator in enumerate(evaluators):
            with self.subTest(index=index):
                ranked, receipt = self.rank("checkout " + "word " * index, baseline, evaluator=evaluator)
                self.assertEqual(baseline, ranked)
                self.assertTrue(receipt["status"].startswith("fallback_"))

    def test_low_score_confidence_does_not_veto_strong_independent_relevance(self):
        weak = self.note("Unrelated baseline", "Panel styling detail.")
        useful = self.note("Payment idempotency", "Retry keys prevent duplicate charges.")
        ranked, receipt = self.rank("Why did we select idempotency?", [weak], evaluator=lambda payload:
            response(payload, relevance=lambda item: .03 if "baseline" in item["title"] else .91,
                     scores=lambda item: .51 if "baseline" in item["title"] else 2.5, confidence=.45))
        self.assertEqual(useful["id"], ranked[0]["id"])
        self.assertEqual("ranked", receipt["status"])
        self.assertEqual(1, receipt["added_candidates"])

    def test_uncertain_baseline_retains_its_slot_while_strong_expansion_is_used(self):
        irrelevant = self.note("Unrelated baseline", "Panel styling.")
        uncertain = self.note("Uncertain baseline", "Payment retry clue without clear evidence.")
        useful = self.note("Payment idempotency", "Retry keys prevent duplicate charges.")
        def evaluator(payload):
            return response(payload, relevance=lambda item: .5 if "Uncertain" in item["title"] else
                .03 if "Unrelated" in item["title"] else .98)
        ranked, receipt = self.rank("Explain payment retries", [irrelevant, uncertain], evaluator=evaluator)
        self.assertEqual([useful["id"], uncertain["id"], irrelevant["id"]], [item["id"] for item in ranked])
        self.assertEqual(uncertain, ranked[1])
        self.assertEqual(1, receipt["uncertain_baseline_count"])

    def test_uncertain_current_intent_does_not_discard_relevant_judgments(self):
        useful = self.note("Checkout retry", "An independent payment issue remains open.")
        def evaluator(payload):
            result = response(payload)
            result["answers"]["current_state"] = {"type": "noul", "noul": .26}
            return result
        ranked, receipt = self.rank("Explain checkout retries", evaluator=evaluator)
        self.assertEqual([useful["id"]], [item["id"] for item in ranked])
        self.assertEqual("ranked", receipt["status"])
        self.assertFalse(receipt["current_state_ordering"])

    def test_warm_exact_input_uses_no_new_request_and_no_repeated_usage(self):
        self.note("Checkout retry")
        calls = []
        def evaluator(payload):
            calls.append(payload)
            return response(payload)
        cold, first = self.rank("checkout", evaluator=evaluator)
        warm, second = self.rank("checkout", evaluator=evaluator)
        self.assertEqual(cold, warm)
        self.assertEqual(1, len(calls))
        self.assertEqual(0, second["requests"])
        self.assertEqual(1, second["cache_hits"])
        self.assertEqual({"input_tokens": 0, "output_tokens": 0}, second["usage"])

    def test_empty_private_disabled_excluded_and_out_of_scope_do_no_work(self):
        note = self.note("Checkout")
        cases = [("", self.settings), ("Can you explain how this works?", self.settings),
            ("<private>checkout</private>", self.settings),
            ("checkout", dict(self.settings, jev_retrieval_enabled=False)),
            ("checkout", dict(self.settings, excluded_projects=[str(self.project)])),
            ("checkout", dict(self.settings, jev_retrieval_projects=[str(self.root / "other")]))]
        with patch.object(self.store, "retrieval_candidates", side_effect=AssertionError("no candidate read")):
            for query, settings in cases:
                with self.subTest(query=query, settings=settings):
                    ranked, receipt = rerank(self.store, self.project, query, [note], settings=settings,
                        evaluator=lambda _: self.fail("no request"))
                    self.assertEqual([note], ranked)
                    self.assertEqual(0, receipt["requests"])

    def test_scoped_candidates_cannot_send_raw_foreign_private_or_superseded_content(self):
        target = self.note("Checkout accepted", observation={"type": "bugfix", "concepts": ["payment"], "files_modified": ["checkout.py"]})
        hidden = [self.note("CURRENT_MARKER", session_id="current"),
                  self.note("RAW_MARKER", source="hook:Stop"),
                  self.note("PRIVATE_MARKER", "<private>PRIVATE_BODY</private>"),
                  self.note("SUPERSEDED_MARKER"),
                  self.store.remember(self.root / "foreign", "FOREIGN_MARKER", "Unrelated."),
                  self.note("WRONG_FILTER_MARKER", observation={"type": "discovery"})]
        self.note("Replacement", source_ids=[hidden[3]["id"]])
        payloads = []
        def evaluator(payload):
            payloads.append(payload)
            return response(payload)
        ranked, receipt = self.rank("checkout", candidates=[target, *hidden], exclude_session="current",
            types=["bugfix"], concepts=["payment"], files=["checkout.py"], evaluator=evaluator)
        self.assertEqual([target["id"]], [item["id"] for item in ranked])
        encoded = json.dumps(payloads)
        for record in hidden:
            self.assertNotIn(record["title"], encoded)

    def test_private_record_is_not_sent_even_without_metadata_filters(self):
        self.note("private title", "<private>SECRET_BODY</private>")
        ranked, receipt = self.rank("secret body", evaluator=lambda _: self.fail("private source submitted"))
        self.assertEqual([], ranked)
        self.assertEqual(0, receipt["requests"])

    def test_literal_identifier_version_issue_and_path_constraints_precede_request(self):
        good = self.note("memory_search 1.9.0 AIAN-700 checkout.py", "Exact source.")
        for title in ("memory_searcher 1.9.0 AIAN-700 checkout.py", "memory_search 1.8.0 AIAN-700 checkout.py",
                      "memory_search 1 9 0 AIAN-700 checkout.py", "memory_search 1.9.0 AIAN-701 checkout.py",
                      "memory_search 1.9.0 AIAN-700 checkout.pyc"):
            self.note(title)
        payloads = []
        ranked, _ = self.rank("How did memory_search 1.9.0 AIAN-700 checkout.py work?",
            evaluator=lambda payload: (payloads.append(payload) or response(payload)))
        self.assertEqual([good["id"]], [item["id"] for item in ranked])
        self.assertEqual([good["title"]], [item["title"] for item in payloads[0]["state"]["candidates"]])

    def test_absolute_path_and_complete_backticked_symbol_are_literal(self):
        good = self.note("/src/checkout.py __retry__", "The exact implementation.")
        self.note("/other/src/checkout.py __retry__", "Different absolute path.")
        self.note("/src/checkout.py retry", "Different code symbol.")
        ranked, _ = self.rank("What does `__retry__` do in /src/checkout.py?", evaluator=response)
        self.assertEqual([good["id"]], [item["id"] for item in ranked])

    def test_too_many_literal_constraints_do_not_relax_to_recent_candidates(self):
        note = self.note("Checkout", "Useful observation.")
        ranked, receipt = self.rank("How " + " ".join("symbol_" + str(i) for i in range(18)), [note],
                                    evaluator=lambda _: self.fail("constraints were dropped"))
        self.assertEqual([note], ranked)
        self.assertEqual("skipped", receipt["status"])

    def test_history_and_version_keep_important_baseline_order(self):
        old = self.note("Checkout 1.8.0 rationale", "We chose retry keys because retries caused double payments.")
        new = self.note("Checkout 1.8.0 retrospective", "A later account of the rollout.")
        for query in ("What was the checkout history?", "Checkout 1.8.0"):
            ranked, _ = self.rank(query, [old, new], evaluator=lambda payload: response(payload,
                scores=lambda candidate: 2 if "rationale" in candidate["title"] else 3))
            self.assertEqual([old["id"], new["id"]], [item["id"] for item in ranked])

    def test_payload_is_bounded_and_redacts_secrets_and_marks_clipped_excerpts(self):
        for index in range(30):
            self.note("Candidate " + str(index), "api_key=TOPSECRET " + "факт " * 1000,
                session_id=str(index), kind="session_summary" if index % 2 else "decision")
        payloads = []
        ranked, _ = self.rank("explain project " + "вопрос " * 50, evaluator=lambda payload:
            (payloads.append(payload) or response(payload)))
        self.assertLessEqual(len(payloads[0]["state"]["candidates"]), MAX_CANDIDATES)
        self.assertLessEqual(len(json.dumps(payloads[0], ensure_ascii=False).encode()), 24000)
        self.assertNotIn("TOPSECRET", json.dumps(payloads))
        self.assertTrue(all(item["excerpt_truncated"] for item in payloads[0]["state"]["candidates"]))

    def test_evaluator_runs_outside_store_lock(self):
        note = self.note("Checkout")
        def evaluator(payload):
            acquired = []
            def read():
                with self.store._lock:
                    acquired.append(True)
            worker = threading.Thread(target=read)
            worker.start()
            worker.join(.2)
            self.assertEqual([True], acquired)
            return response(payload)
        ranked, receipt = self.rank("checkout", [note], evaluator=evaluator)
        self.assertEqual("ranked", receipt["status"])

    def test_insufficient_shared_hook_budget_skips_candidate_reads_and_request(self):
        note = self.note("Checkout", "Checkout retry finding.")
        with patch.object(self.store, "retrieval_candidates", side_effect=AssertionError("late candidate read")):
            ranked, receipt = self.rank("checkout", [note], deadline_at=time.monotonic() + .1,
                evaluator=lambda _: self.fail("late request"))
        self.assertEqual([note], ranked)
        self.assertEqual("insufficient_hook_budget", receipt["skip_reason"])
        self.assertEqual(0, receipt["requests"])

    def test_current_state_uses_source_events_not_delayed_baseline_write_time(self):
        source_old = self.note("Earlier stop", "Previous work.", source="hook:Stop", session_id="old")
        source_new = self.note("Later stop", "New work.", source="hook:Stop", session_id="new")
        old = self.note("Old handoff", "Original release completed.", session_id="old",
                        kind="session_summary", source_ids=[source_old["id"]])
        new = self.note("New handoff", "Production verification remains.", session_id="new",
                        kind="session_summary", source_ids=[source_new["id"]])
        with self.store._lock:
            for item, instant in ((source_old, "2026-01-01T00:00:00Z"), (source_new, "2026-02-01T00:00:00Z"),
                                  (old, "2026-04-01T00:00:00Z"), (new, "2026-03-01T00:00:00Z")):
                self.store._connection.execute("UPDATE entries SET created_at=? WHERE id=?", (instant, item["id"]))
        baseline = self.store.get(self.project, [old["id"], new["id"]])
        ranked, receipt = self.rank("current project status", baseline, evaluator=response)
        self.assertEqual([new["id"], old["id"]], [item["id"] for item in ranked])
        self.assertEqual("source_event", ranked[0]["event_time_basis"])

    def test_current_state_compares_real_instants_across_timezone_offsets(self):
        older = self.note("Older local time", "Project handoff.", kind="session_summary", session_id="old")
        newer = self.note("Newer UTC time", "Project handoff.", kind="session_summary", session_id="new")
        with self.store._lock:
            self.store._connection.execute("UPDATE entries SET created_at=? WHERE id=?",
                ("2026-09-22T10:00:00+03:00", older["id"]))
            self.store._connection.execute("UPDATE entries SET created_at=? WHERE id=?",
                ("2026-09-22T08:00:00Z", newer["id"]))
        baseline = self.store.get(self.project, [older["id"], newer["id"]])
        ranked, _ = self.rank("current project status", baseline, evaluator=response)
        self.assertEqual([newer["id"], older["id"]], [item["id"] for item in ranked])

    def test_candidate_session_cap_uses_utc_microseconds_and_id_ties(self):
        for suffix, times in (("offset", ("2026-09-22T10:00:00+03:00", "2026-09-22T08:00:00Z")),
                              ("fraction", ("2026-09-22T07:00:00.000001Z", "2026-09-22T07:00:00.000002Z")),
                              ("tie", ("2026-09-22T10:00:00+03:00", "2026-09-22T07:00:00Z"))):
            with self.subTest(suffix=suffix):
                rows = sorted([self.note("Handoff " + suffix, "Current work.", kind="session_summary", session_id=suffix)
                               for _ in range(2)], key=lambda row: row["id"], reverse=True)
                with self.store._lock:
                    for row, timestamp in zip(rows, times):
                        self.store._connection.execute("UPDATE entries SET created_at=? WHERE id=?", (timestamp, row["id"]))
                expected = rows[0] if suffix == "tie" else rows[1]
                pool = [item for item in self.store.retrieval_candidates(self.project) if item["session_id"] == suffix]
                self.assertEqual([expected["id"]], [item["id"] for item in pool])

    def test_source_event_selection_normalizes_offset_before_choosing_latest_stop(self):
        older = self.note("Old stop", "Earlier work.", source="hook:Stop", session_id="same")
        newer = self.note("New stop", "Later work.", source="hook:Stop", session_id="same")
        note = self.note("Handoff", "Current project work.", kind="session_summary", session_id="same",
                         source_ids=[older["id"], newer["id"]])
        with self.store._lock:
            for row, timestamp in ((older, "2026-09-22T10:00:00+03:00"), (newer, "2026-09-22T08:00:00Z")):
                self.store._connection.execute("UPDATE entries SET created_at=? WHERE id=?", (timestamp, row["id"]))
        candidate = self.store.retrieval_candidates(self.project)[0]
        self.assertEqual(note["id"], candidate["id"])
        self.assertEqual(newer["id"], candidate["event_id"])
        self.assertEqual("2026-09-22T08:00:00Z", candidate["event_at"])

    def test_scoped_chronology_keeps_history_without_loading_peer_bodies(self):
        stop = self.note("Old stop", source="hook:Stop", session_id="same")
        tool = self.note("Later tool", source="hook:PostToolUse", session_id="same")
        foreign = self.note("Other session stop", source="hook:Stop", session_id="other")
        current_stop = self.note("Current stop", source="hook:Stop", session_id="same")
        old = self.note("Checkout 1.8.0", kind="session_summary", session_id="same",
            source_ids=[stop["id"], tool["id"], foreign["id"]])
        current = self.note("Checkout 1.9.0", kind="session_summary", session_id="same",
            source_ids=[current_stop["id"]])
        for row, at in ((stop, "2026-09-22T10:00:00+03:00"), (tool, "2026-09-22T12:00:00Z"),
                        (foreign, "2099-01-01T00:00:00Z"), (current_stop, "2026-09-22T08:00:00Z")):
            self.store._connection.execute("UPDATE entries SET created_at=? WHERE id=?", (at, row["id"]))
        expected = self.store.resume_metadata(self.project, [old["id"]])[old["id"]]
        hydrated = []
        hydrate = self.store._records_from_rows
        def record_rows(rows):
            hydrated.extend(row["id"] for row in rows)
            return hydrate(rows)
        with patch.object(self.store, "_records_from_rows", side_effect=record_rows), \
             patch.object(self.store, "resume_metadata", side_effect=AssertionError("broad peer body scan")):
            candidate = self.store.retrieval_candidates(self.project, query="Checkout 1.8.0", ids=[old["id"]])[0]
        self.assertEqual([old["id"]], hydrated)
        self.assertEqual(stop["id"], candidate["event_id"])
        self.assertEqual(current["id"], candidate["later_summary_id"])
        self.assertTrue(candidate["context_historical"])
        self.assertEqual(expected, {key: candidate[key] for key in expected})

    def test_large_session_dispatches_within_budget_and_hydrates_only_shortlists(self):
        # Match the live scale that exposed a derived x all-session-events join:
        # thousands of curated rows and raw events, each note linked to only two.
        # Bulk insert synthetic content; no production data is copied.
        project = str(self.project.resolve())
        rows, metadata, links = [], [], []
        body = "Payment retry keys prevent duplicate charges. " * 30
        for index in range(4300):
            entry_id = f"{index + 1:032x}"
            at = f"2026-09-22T07:00:00.{index:06d}Z"
            rows.append((entry_id, project, "Raw event", "Untrusted raw event.", "tool", "large-session",
                         "hook:Stop" if index % 2 else "hook:PostToolUse", "[]", at, at))
        for index in range(1760):
            entry_id = f"{10000 + index:032x}"
            at = f"2026-09-22T09:00:00.{index:06d}Z"
            summary = bool(index % 2)
            rows.append((entry_id, project, "Project handoff" if summary else "Payment finding", body,
                         "session_summary" if summary else "note", "large-session", "processor", "[]", at, at))
            metadata.append((entry_id, None if summary else '{"type":"discovery"}',
                             '{"next_steps":"Verify retries."}' if summary else None, at, at))
            links.extend((entry_id, f"{2 * index + offset:032x}") for offset in (1, 2))
        with self.store._connection:
            self.store._connection.executemany(
                "INSERT INTO entries (id,project,title,body,kind,session_id,source,tags_json,created_at,updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)", rows)
            self.store._connection.executemany(
                "INSERT INTO entry_metadata (entry_id,observation_json,session_summary_json,created_at,updated_at) "
                "VALUES (?,?,?,?,?)", metadata)
            self.store._connection.executemany("INSERT INTO entry_sources VALUES (?,?)", links)
        baseline = self.store.get(self.project, [f"{10000:032x}", f"{10002:032x}"])
        calls, hydrated = [], []
        hydrate = self.store._records_from_rows
        def record_rows(selected):
            hydrated.append(len(selected))
            return hydrate(selected)
        with patch.object(self.store, "_records_from_rows", side_effect=record_rows), \
             patch.object(self.store, "resume_metadata", side_effect=AssertionError("unbounded followup body scan")):
            ranked, receipt = self.rank("Как устроена защита от повторного списания?", baseline,
                evaluator=lambda payload: (calls.append(payload) or response(payload)))
        self.assertEqual("ranked", receipt["status"])
        self.assertEqual(1, len(calls))
        self.assertLess(receipt["duration_ms"], 1500)
        self.assertEqual([2, MAX_CANDIDATES], hydrated)
        self.assertLessEqual(len(calls[0]["state"]["candidates"]), MAX_CANDIDATES)
        self.assertIn(f"{11759:032x}", [record["id"] for record in ranked])
        self.assertTrue(all(not record["source"].startswith("hook:") for record in ranked))

    def test_expired_local_read_returns_exact_baseline_and_timeout_receipt(self):
        baseline = [self.note("Checkout")]
        def expired(*_args, **_kwargs):
            time.sleep(.015)
            raise StoreError("interrupted local read")
        with patch.object(self.store, "retrieval_candidates", side_effect=expired):
            ranked, receipt = self.rank("Checkout", baseline, timeout=.005,
                evaluator=lambda _: self.fail("request after local deadline"))
        self.assertEqual(baseline, ranked)
        self.assertEqual("fallback_timeout", receipt["status"])
        self.assertEqual(0, receipt["requests"])
