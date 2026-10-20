"""Offline contract tests; mocked readers/generators never establish quality."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('late_acceptance', ROOT / 'scripts/project_memory_late_arrival_acceptance.py')
late = importlib.util.module_from_spec(spec)
spec.loader.exec_module(late)


class LateArrivalAcceptanceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.base = Path(self.temporary.name)
        self.addCleanup(self.temporary.cleanup)
        self.fixture = {'schema_version': 1, 'fixture_id': 'tiny-late-transport-v1',
                        'default_cutoff': 2, 'background_cutoff': 1, 'budget_chars': 2000,
                        'events': [{'id': 't1', 'sequence': 1, 'session_id': 'first', 'topic': 'sample',
                                    'role': 'user', 'project': 'maple', 'text': 'TASK-321: Implement sample output.', 'occurred_at': 1},
                                   {'id': 't2', 'sequence': 2, 'session_id': 'second', 'topic': 'sample',
                                    'role': 'user', 'project': 'maple', 'text': 'Continue TASK-321: review sample output.', 'occurred_at': 0}],
                        'queries': [{'id': 'q', 'query': 'What sample output is requested?', 'language': 'en', 'split': 'holdout'}]}
        self.fixture_path = self.base / 'fixture.json'
        self.fixture_path.write_text(json.dumps(self.fixture))
        self.parent = late.acceptance.run_suite(fixture=self.fixture, base_dir=self.base / 'parent')
        self.report_path = self.base / 'parent.json'
        self.report_path.write_text(json.dumps(self.parent, default=str))

    def run_helper(self, **options):
        return late.run(report_path=self.report_path, fixture_path=self.fixture_path,
                        base_dir=self.base / 'clone', **options)

    def test_default_is_inspection_only_and_preserves_source_ids(self):
        with mock.patch('codex_mem.processor.NativeProcessorRunner', side_effect=AssertionError('no models')):
            result = self.run_helper()
        self.assertEqual('late_arrival_inspection_only', result['measurement'])
        self.assertEqual(result['before'], result['after'])
        self.assertEqual([], result['native_generations'])
        self.assertEqual([], result['new_attempts'])
        self.assertTrue(result['observations']['original_state_unchanged'])
        pending = next(source for source in result['source_annotations'] if source['fixture_event_id'] == 't2')
        self.assertEqual(0, pending['fixture_occurrence_annotation'])
        self.assertEqual(2, pending['fixture_arrival_sequence'])
        self.assertTrue(pending['was_pending_before'])
        self.assertIsNone(result['semantic_revision_correctness'])

    def test_mocked_live_reports_actual_parent_version_and_ledger_without_quality_verdict(self):
        before = late.state(self.base / 'parent' / 'next', self.parent['project'])
        parent_id = before['episodes'][0]['id']
        def fake_native(request):
            response = late.acceptance.copy_generator(request)
            episode = response['output']['episodes'][0]
            episode['continuation_of'] = parent_id
            episode['evidence']['continuation_of'] = episode['evidence']['summary']
            return response
        with mock.patch('codex_mem.processor.NativeProcessorRunner') as factory:
            factory.return_value.side_effect = fake_native
            result = self.run_helper(live=True, max_jobs=1)
        self.assertEqual(1, factory.call_count)
        self.assertEqual(1, len(result['new_attempts']))
        self.assertEqual(1, len(result['jobs_processed_this_run']))
        self.assertEqual(1, result['current_run_observed_usage']['model_calls'])
        link = result['observations']['new_episodes_with_prior_parent'][0]
        self.assertEqual(parent_id, link['logical_episode_id'])
        self.assertEqual(parent_id, link['parent_episode_id'])
        self.assertEqual(2, link['episode_version'])
        self.assertTrue(result['observations']['revision_links_observed'])
        self.assertTrue(result['observations']['original_state_unchanged'])
        self.assertEqual(0, sum(not event['done'] for event in result['after']['events']))
        self.assertIsNone(result['semantic_revision_correctness'])
        self.assertEqual(before, late.state(self.base / 'parent' / 'next', self.parent['project']))

    def test_processed_standalone_is_not_reported_as_revision(self):
        with mock.patch('codex_mem.processor.NativeProcessorRunner') as factory:
            factory.return_value.side_effect = late.acceptance.copy_generator
            result = self.run_helper(live=True)
        self.assertEqual(1, len(result['observations']['new_episode_ids']))
        self.assertFalse(result['observations']['revision_links_observed'])
        self.assertEqual([], result['observations']['new_episodes_with_prior_parent'])
        self.assertIsNone(result['semantic_revision_correctness'])

    def test_failed_native_is_retained_without_retry(self):
        with mock.patch('codex_mem.processor.NativeProcessorRunner') as factory:
            factory.return_value.side_effect = ValueError('synthetic failure')
            result = self.run_helper(live=True, max_jobs=3)
        self.assertEqual(1, factory.call_count)
        self.assertEqual('failed', result['jobs_processed_this_run'][0]['status'])
        self.assertEqual('ValueError', result['native_generations'][0]['error']['type'])
        self.assertEqual(1, len(result['new_attempts']))
        self.assertFalse(result['observations']['revision_links_observed'])

    def test_empty_tail_needs_no_native_and_bad_inputs_are_rejected(self):
        self.fixture['background_cutoff'] = 2
        self.fixture_path.write_text(json.dumps(self.fixture))
        parent = late.acceptance.run_suite(fixture=self.fixture, base_dir=self.base / 'settled')
        self.report_path.write_text(json.dumps(parent, default=str))
        with mock.patch('codex_mem.processor.NativeProcessorRunner', side_effect=AssertionError('no pending')):
            result = self.run_helper(live=True)
        self.assertEqual([], result['jobs_processed_this_run'])
        self.assertEqual([], result['new_attempts'])
        for count in (0, 4, True):
            with self.assertRaises(ValueError):
                self.run_helper(max_jobs=count)
        self.fixture['events'][0]['text'] += ' Changed.'
        self.fixture_path.write_text(json.dumps(self.fixture))
        with self.assertRaisesRegex(ValueError, 'exact fixture'):
            self.run_helper()


if __name__ == '__main__':
    unittest.main()
