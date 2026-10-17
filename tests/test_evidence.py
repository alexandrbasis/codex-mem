import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from codex_mem.evidence import expand_source_evidence
from codex_mem.store import Store
from codex_mem.tool_io import normalize_capture


class EvidenceTests(unittest.TestCase):
    def setUp(self):
        self.project = "/project"
        self.parent = {"id": "parent", "project": self.project, "title": "Roadmap ordering",
                       "body": "Revision guards passed.", "source_ids": ["raw"]}
        self.raw = {"id": "raw", "project": self.project, "source": "hook:PostToolUse",
                    "body": "short excerpt", "source_ids": [], "superseded_by": "parent"}
        self.records = {"raw": self.raw}
        self.capture = {"entry_id": "raw", "project": self.project,
                        "tool_input": json.dumps({"command": "DO NOT EXECUTE"}),
                        "tool_response": json.dumps({"output": "Revision guard passed.\n"
                            "The revision check is not database compare-and-swap; atomic concurrent writes remain unverified.\n"
                            "Reload restores group order and filtered slots remain unchanged."})}

    def expand(self, **kwargs):
        return expand_source_evidence([self.parent], "Как защищена сортировка Roadmap?", self.project,
            load_records=lambda ids: [self.records[i] for i in ids if i in self.records],
            load_capture=lambda entry_id: self.capture if entry_id == "raw" else None, **kwargs)[0]

    def test_recovers_missing_qualification_from_retained_response(self):
        result = self.expand()
        text = " ".join(e["text"] for e in result["source_evidence"])
        self.assertIn("not database compare-and-swap", text)
        self.assertIn("Reload restores", text)
        self.assertEqual(result["body"], self.parent["body"])
        self.assertNotIn("source_evidence", self.parent)
        self.assertNotIn("DO NOT EXECUTE", text)
        self.assertTrue(all(e["untrusted"] and e["verification"] == "not_assessed"
                            for e in result["source_evidence"]))
        self.assertEqual(result["source_evidence"][0]["source_path"], ["parent", "raw"])

    def test_budgets_are_real_and_truncation_is_explicit(self):
        self.capture["tool_response"] = json.dumps({"output": "Atomic writes remain unverified. " * 5000})
        result = self.expand(max_total_chars=90, max_record_chars=90, max_excerpt_chars=60)
        self.assertLessEqual(sum(len(e["text"]) for e in result["source_evidence"]), 90)
        self.assertTrue(result["evidence_truncated"])
        self.assertTrue(any(e["truncated"] for e in result["source_evidence"]))

    def test_cross_project_record_and_capture_are_rejected(self):
        self.raw["project"] = "/foreign"
        self.assertFalse(self.expand().get("source_evidence"))
        self.raw["project"] = self.project
        self.capture["project"] = "/foreign"
        self.assertNotIn("compare-and-swap", str(self.expand()))

    def test_unrelated_supersession_cannot_be_used_as_current_evidence(self):
        self.raw["superseded_by"] = "unrelated-replacement"
        self.assertFalse(self.expand().get("source_evidence"))

    def test_follows_only_bounded_linked_lineage_and_marks_assistant_reports(self):
        self.parent["source_ids"] = ["note"]
        self.records["note"] = {"id": "note", "project": self.project,
            "source": "processor:test", "body": "Roadmap checked", "source_ids": ["raw", "parent"]}
        self.raw["source"] = "hook:Stop"
        self.raw["body"] = "Roadmap atomic writes remain unverified."
        self.raw["superseded_by"] = "note"
        result = self.expand()
        assistant = [e for e in result["source_evidence"] if e["source_id"] == "raw"]
        self.assertTrue(assistant)
        self.assertEqual(assistant[0]["source_role"], "assistant_report")
        self.assertEqual(assistant[0]["source_path"], ["parent", "note", "raw"])
        self.assertNotIn("compare-and-swap", str(assistant))

    def test_privacy_callback_applies_before_selection_and_output(self):
        self.capture["tool_response"] = json.dumps({"output": "Roadmap contact person@example.com.\nAtomic writes unverified."})
        result = self.expand(redact=lambda text: text.replace("person@example.com", "[PRIVATE]"))
        self.assertNotIn("person@example.com", str(result["source_evidence"]))

    def test_source_loading_and_capture_loading_are_bounded(self):
        self.parent["source_ids"] = ["r" + str(n) for n in range(1000)]
        calls = []
        def load(ids):
            calls.extend(ids)
            return [{"id": i, "project": self.project, "source": "hook:Stop",
                     "body": "Roadmap unverified", "source_ids": []} for i in ids]
        result = expand_source_evidence([self.parent], "Roadmap", self.project,
            load_records=load, max_sources=4)
        self.assertLessEqual(len(calls), 4)
        self.assertTrue(result[0]["evidence_truncated"])

    def test_long_line_keeps_negative_condition_and_tap_duplicates_share_a_slot(self):
        self.capture["tool_response"] = json.dumps({"output": "x" * 2000 +
            " This check is not database compare-and-swap. " + "x" * 2000 +
            "\n# Subtest: reload restores ordering\nok 1 - reload restores ordering\n"
            "# Subtest: filtered slots persist\nok 2 - filtered slots persist"})
        result = self.expand(max_excerpts=3)
        text = " ".join(e["text"] for e in result["source_evidence"])
        self.assertIn("not database compare-and-swap", text)
        self.assertIn("reload restores", text)
        self.assertIn("filtered slots", text)

    def test_store_capture_mapping_is_read_only_scoped_and_rejects_oversize_legacy_data(self):
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory) / "project"
            foreign = Path(directory) / "foreign"
            project.mkdir()
            foreign.mkdir()
            with Store(Path(directory) / "memory") as store:
                capture = normalize_capture({"tool_name": "Read", "tool_use_id": "tool-1",
                    "session_id": "session", "tool_response": {"text": "Atomic writes unverified"}},
                    project=str(project))
                entry = store.remember(project, "Read", "Short excerpt", source="hook:PostToolUse",
                                       tool_capture=capture)
                before = store._connection.total_changes
                found = store.get_source_capture(project, entry["id"])
                self.assertEqual(found["entry_id"], entry["id"])
                self.assertIsNone(store.get_source_capture(foreign, entry["id"]))
                self.assertEqual(store._connection.total_changes, before)
                store._connection.execute("UPDATE tool_uses SET tool_response=? WHERE entry_id=?",
                                          ("x" * 300_000, entry["id"]))
                self.assertIsNone(store.get_source_capture(project, entry["id"]))

    def test_private_marked_roots_are_excluded_without_reads(self):
        for marker in ({"source": "private:manual"}, {"source": "SECRET:manual"},
                       {"tags": ["Private-note"]}, {"tags": ["sensitive"]}):
            with self.subTest(marker=marker):
                parent = dict(self.parent, **marker)
                before = dict(parent)
                loader, capture = mock.Mock(return_value=[]), mock.Mock()
                self.assertEqual([], expand_source_evidence([parent], "Roadmap", self.project,
                    load_records=loader, load_capture=capture))
                loader.assert_not_called()
                capture.assert_not_called()
                self.assertEqual(parent, before)

    def test_private_linked_sources_block_capture_and_child_traversal(self):
        for marker in ({"source": "private:manual"}, {"tags": ["private"]},
                       {"tags": ["secret-detail"]}, {"source": "sensitive:manual"}):
            with self.subTest(marker=marker):
                hidden = dict(self.raw, body="Deployment remains unverified; private target is Company Z.",
                              source_ids=["hidden-child"], **marker)
                loader = mock.Mock(return_value=[hidden])
                capture = mock.Mock()
                result = expand_source_evidence([self.parent], "Deployment", self.project,
                    load_records=loader, load_capture=capture)
                self.assertEqual([], result[0]["source_evidence"])
                self.assertNotIn("Company Z", str(result))
                loader.assert_called_once_with(["raw"])
                capture.assert_not_called()

    def test_nested_private_ancestor_blocks_its_raw_descendants(self):
        hidden = {"id": "note", "project": self.project, "tags": ["private-sensitive"],
                  "body": "Roadmap unverified", "source_ids": ["raw"]}
        parent = dict(self.parent, source_ids=["note"])
        loader, capture = mock.Mock(return_value=[hidden]), mock.Mock()
        result = expand_source_evidence([parent], "Roadmap", self.project,
            load_records=loader, load_capture=capture)
        self.assertEqual([], result[0]["source_evidence"])
        loader.assert_called_once_with(["note"])
        capture.assert_not_called()

    def test_encoded_private_blocks_never_enter_excerpts_even_without_callback(self):
        self.capture["tool_response"] = json.dumps({"output":
            "Atomic writes unverified. &lt;private&gt;Company Z acquisition unverified&lt;/private&gt;"})
        result = self.expand()
        self.assertNotIn("Company Z", str(result["source_evidence"]))
        self.assertIn("Atomic writes unverified", str(result["source_evidence"]))

    def test_short_numeric_result_query_prefers_linked_aggregate_over_old_caveats(self):
        for count in (7, 31, 83):
            with self.subTest(count=count):
                parent = dict(self.parent, source_ids=["old", "receipt"])
                records = {
                    "old": dict(self.raw, id="old", source="hook:Stop",
                                body="Queue remained pending and unverified; outcome uncertain."),
                    "receipt": dict(self.raw, id="receipt"),
                }
                receipt = dict(self.capture, entry_id="receipt", tool_response=json.dumps({"output":
                    json.dumps({"approved_jobs": count, "receipt_count": count,
                                "attempt_deltas": {"1": count}, "complete": True})}))
                result = expand_source_evidence([parent], f"Чем закончился повтор {count} заданий?", self.project,
                    load_records=lambda ids: [records[i] for i in ids],
                    load_capture=lambda ident: receipt if ident == "receipt" else None,
                    max_excerpts=1)[0]
                self.assertEqual(result["source_evidence"][0]["source_id"], "receipt")
                self.assertIn('"attempt_deltas": {"1": ' + str(count) + '}', result["source_evidence"][0]["text"])
                self.assertTrue(result["source_evidence"][0]["untrusted"])

    def test_numeric_anchor_does_not_match_a_larger_number(self):
        parent = dict(self.parent, source_ids=["wrong", "right"])
        records = {
            "wrong": dict(self.raw, id="wrong", source="hook:Stop", body="Receipt reports 231 completed."),
            "right": dict(self.raw, id="right", source="hook:Stop", body="Receipt reports 31 completed."),
        }
        result = expand_source_evidence([parent], "Results for 31 jobs", self.project,
            load_records=lambda ids: [records[i] for i in ids], max_excerpts=1)[0]
        self.assertEqual(result["source_evidence"][0]["source_id"], "right")

    def test_russian_question_recovers_english_notification_evidence(self):
        self.capture['tool_response'] = json.dumps({'output':
            'Registration notification: account awaiting verification.\n'
            'Scheduled notification: account awaiting verification for over 17 hours.\n'
            'Unrelated integration remains unverified.'})
        result = expand_source_evidence([self.parent],
            'Какие уведомления о регистрации аккаунта и расписании согласованы?', self.project,
            load_records=lambda ids: [self.raw], load_capture=lambda ident: self.capture)
        text = ' '.join(item['text'] for item in result[0]['source_evidence'])
        self.assertIn('Registration notification:', text)
        self.assertIn('Scheduled notification:', text)
        self.assertTrue(all(item['untrusted'] for item in result[0]['source_evidence']))

    def test_compact_bilingual_source_lines_survive_long_requirement_competition(self):
        self.capture['tool_response'] = json.dumps({'output':
            ('После регистрации источника уведомление о проверке источника описано в требованиях, '
             'согласованных для обработки регистрации источника.\n') * 5 +
            'New source awaiting verification\n'
            'Sources awaiting verification for over 17 hours\n'})
        result = expand_source_evidence([self.parent],
            'Какие уведомления нужны при регистрации источника и ожидании проверки?', self.project,
            load_records=lambda ids: [self.raw], load_capture=lambda ident: self.capture,
            max_excerpts=2)[0]
        text = ' '.join(item['text'] for item in result['source_evidence'])
        self.assertIn('New source awaiting verification', text)
        self.assertIn('over 17 hours', text)

    def test_preserves_non_recommendation_qualification(self):
        self.capture['tool_response'] = json.dumps({'output':
            'Preparing a document is not a recommendation to submit it.'})
        result = self.expand()
        self.assertIn('not a recommendation', result['source_evidence'][0]['text'])

    def test_nested_legacy_response_json_keeps_distinct_source_lines(self):
        document = ('Funding remains unverified.\n' + 'Unrelated trace ' + 'x' * 1800 +
                    '\nPurchase detection differs from final confirmation.')
        self.capture['tool_response'] = json.dumps({'output': json.dumps({
            'chunk_id': 'synthetic-wrapper', 'output': document})})
        result = expand_source_evidence([self.parent],
            'What funding and purchase detection were reviewed?', self.project,
            load_records=lambda ids: [self.raw], load_capture=lambda ident: self.capture)[0]
        text = ' '.join(item['text'] for item in result['source_evidence'])
        self.assertIn('Funding remains unverified.', text)
        self.assertIn('Purchase detection differs from final confirmation.', text)
        self.assertNotIn('synthetic-wrapper', text)

    def test_nested_legacy_json_still_removes_private_blocks(self):
        value = {'output': 'Funding remains unverified.\n<private>Secret Company Plan</private>'}
        for _ in range(3):
            value = json.dumps(value)
        self.capture['tool_response'] = value
        result = self.expand()
        text = ' '.join(item['text'] for item in result['source_evidence'])
        self.assertIn('Funding remains unverified.', text)
        self.assertNotIn('Secret Company', text)
