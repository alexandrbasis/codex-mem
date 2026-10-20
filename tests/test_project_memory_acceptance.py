"""Acceptance fixtures are not evidence of live model quality."""
from __future__ import annotations

import copy
from contextlib import closing
import importlib.util
import json
import sqlite3
from pathlib import Path
import unittest
import tempfile
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('project_memory_acceptance', ROOT / 'scripts/project_memory_acceptance.py')
acceptance = importlib.util.module_from_spec(spec)
spec.loader.exec_module(acceptance)


class ProjectMemoryAcceptanceTests(unittest.TestCase):
    def test_frozen_fingerprint_tracks_only_scoped_episode_heads_read_only(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            root = root.resolve()
            data = root / 'data'
            project, other = root / 'project', root / 'other'
            from codex_mem.project_memory import ProjectMemory
            with acceptance.Store(data) as store:
                ProjectMemory(store)
                with store._connection:
                    store._connection.executemany('INSERT INTO pm_episode_heads VALUES (?,?,?,?)',
                                                  [(str(project), 'root-z', 'head-z', 1),
                                                   (str(project), 'root-a', 'head-a', 1),
                                                   (str(other), 'root-other', 'head-other', 1)])
            initial = acceptance.frozen_state_fingerprint(data, project)
            self.assertEqual(2, initial['row_counts']['pm_episode_heads'])
            with closing(sqlite3.connect(data / 'memory.sqlite3')) as connection, connection:
                connection.execute('UPDATE pm_episode_heads SET head_id=? WHERE project=?',
                                   ('other-new-head', str(other)))
            self.assertEqual(initial, acceptance.frozen_state_fingerprint(data, project))
            with closing(sqlite3.connect(data / 'memory.sqlite3')) as connection, connection:
                connection.execute('UPDATE pm_episode_heads SET head_id=?,episode_version=2 '
                                   'WHERE project=? AND logical_episode_id=?', ('head-a-v2', str(project), 'root-a'))
            db_bytes = (data / 'memory.sqlite3').read_bytes()
            changed = acceptance.frozen_state_fingerprint(data, project)
            self.assertNotEqual(initial['sha256'], changed['sha256'])
            self.assertEqual(db_bytes, (data / 'memory.sqlite3').read_bytes())
            with closing(sqlite3.connect(data / 'memory.sqlite3')) as connection, connection:
                connection.execute('DROP TABLE pm_episode_heads')
            old_db_bytes = (data / 'memory.sqlite3').read_bytes()
            historical = acceptance.frozen_state_fingerprint(data, project)
            self.assertEqual(0, historical['row_counts']['pm_episode_heads'])
            self.assertEqual(old_db_bytes, (data / 'memory.sqlite3').read_bytes())
            with closing(sqlite3.connect(data / 'memory.sqlite3')) as connection, connection:
                self.assertIsNone(connection.execute("SELECT name FROM sqlite_master WHERE name='pm_episode_heads'").fetchone())

    def test_explicit_reader_modes_current_and_startup_share_budget(self):
        fixture = self.tiny_fixture()
        fixture['queries'].append({'id': 'second-query', 'query': 'What heading is requested?',
                                   'language': 'en', 'split': 'holdout'})
        result = acceptance.run_suite(fixture=fixture, reader_modes=['current', 'production_startup'])
        self.assertEqual(['current', 'production_startup'], result['reader_modes'])
        self.assertEqual(4, len(result['results']))
        self.assertTrue(result['startup_merge']['complete'])
        self.assertEqual({'context_chars': 2000, 'same_for_all_modes': True}, result['budgets'])
        self.assertTrue(all(len(case['context']) <= 2000 for case in result['results']))
        default = acceptance.run_suite(fixture=fixture)
        self.assertEqual(list(acceptance.MODES), default['reader_modes'])
        self.assertEqual(8, len(default['results']))
        self.assertIsNone(default['startup_merge'])

    def test_reader_modes_reject_unsupported_duplicate_empty_and_ambiguous_flags(self):
        for modes in ([], (), ['current', 'current'], ['invented'], 'current', [None], [['current']]):
            with self.subTest(modes=modes), self.assertRaisesRegex(ValueError, 'reader-modes'):
                acceptance.run_suite(fixture=self.tiny_fixture(), reader_modes=modes)
        for flags in ({'baseline_only': True}, {'startup_only': True}):
            with self.subTest(flags=flags), self.assertRaisesRegex(ValueError, 'cannot be combined'):
                acceptance.run_suite(fixture=self.tiny_fixture(), reader_modes=('current',), **flags)

    def test_code_fingerprint_discovers_all_modules_and_both_harnesses(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'codex_mem').mkdir()
            (root / 'scripts').mkdir()
            paths = ['codex_mem/integration.py', 'codex_mem/store.py',
                     'scripts/project_memory_acceptance.py',
                     'scripts/project_memory_late_arrival_acceptance.py']
            for path in paths:
                (root / path).write_text('# synthetic module\n')
            (root / 'codex_mem/ignored.txt').write_text('not Python')
            with mock.patch.object(acceptance, 'ROOT', root):
                before = acceptance.code_fingerprint()
                self.assertEqual(set(paths), set(before))
                # A newly introduced recall module is covered without editing a list.
                query = root / 'codex_mem/project_memory_query.py'
                query.write_text('# new query module\n')
                added = acceptance.code_fingerprint()
                self.assertEqual(set(paths) | {'codex_mem/project_memory_query.py'}, set(added))
                query.write_text('# revised query module\n')
                changed = acceptance.code_fingerprint()
            self.assertNotEqual(added['codex_mem/project_memory_query.py'],
                                changed['codex_mem/project_memory_query.py'])
            self.assertEqual(before, {key: changed[key] for key in before})

    @staticmethod
    def tiny_fixture():
        return {'schema_version': 1, 'fixture_id': 'tiny-loader-contract-v1',
                'default_cutoff': 2, 'background_cutoff': 1, 'budget_chars': 2000,
                'target_project': 'maple',
                'events': [{'id': 'tiny-1', 'sequence': 1, 'session_id': 'tiny-session',
                            'topic': 'format', 'role': 'user', 'text': 'Use plain text for the sample.',
                            'project': 'maple', 'stop': False},
                           {'id': 'tiny-2', 'sequence': 2, 'session_id': 'tiny-session',
                            'topic': 'format', 'role': 'user', 'text': 'Keep the heading short.',
                            'project': 'maple', 'stop': False}],
                'queries': [{'id': 'tiny-query', 'query': 'What sample format was requested?',
                             'language': 'en', 'split': 'holdout'}]}

    def test_external_fixture_cli_preserves_default_and_declared_split(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture_path = Path(directory) / 'tiny.json'
            output = Path(directory) / 'result.json'
            fixture = self.tiny_fixture()
            fixture_path.write_text(json.dumps(fixture))
            with mock.patch('sys.argv', ['acceptance', '--fixture', str(fixture_path),
                                         '--baseline-only', '--output', str(output)]):
                self.assertEqual(0, acceptance.main())
            report = json.loads(output.read_text())
            self.assertEqual('tiny-loader-contract-v1', report['fixture_id'])
            self.assertEqual('maple', report['target_project'])
            self.assertEqual(2, report['event_count'])
            self.assertEqual(['tiny-2'], report['pending_event_ids'])
            self.assertEqual('external_fixture', report['comparison_kind'])
            self.assertIn('not established', report['split_status'])
            self.assertEqual(str(fixture_path.resolve()), report['fixture_path'])
        self.assertEqual('project-memory-frozen-v1', acceptance.load_fixture()['fixture_id'])

    def test_external_fixture_reuse_rejects_changed_content(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            fixture = self.tiny_fixture()
            report = acceptance.run_suite(fixture=fixture, baseline_only=True, base_dir=base / 'first')
            prior = base / 'prior.json'
            prior.write_text(json.dumps(report, default=str))
            changed = copy.deepcopy(fixture)
            changed['events'][0]['text'] = 'Use a different sample format.'
            with self.assertRaisesRegex(ValueError, 'frozen fixture'):
                acceptance.run_suite(fixture=changed, baseline_only=True,
                                     base_dir=base / 'second', reuse_baseline_report=prior)

    def test_external_fixture_shape_and_cutoffs_are_validated(self):
        for mutate in (
            lambda f: f.update(events=[]),
            lambda f: f['events'][0].update(sequence=True),
            lambda f: f['events'][0].update(role='system'),
            lambda f: f['events'][0].update(text=None),
            lambda f: f['events'].append(copy.deepcopy(f['events'][0])),
            lambda f: f['queries'].append(copy.deepcopy(f['queries'][0])),
            lambda f: f['queries'][0].update(split='unspecified'),
            lambda f: f.update(default_cutoff=3),
            lambda f: f.update(background_cutoff=3),
            lambda f: f.update(budget_chars=60001),
            lambda f: f.update(target_project='absent'),
        ):
            fixture = self.tiny_fixture()
            mutate(fixture)
            with self.subTest(fixture=fixture), self.assertRaises(ValueError):
                acceptance.validate_fixture(fixture)
        with self.assertRaises(ValueError):
            acceptance.validate_fixture([])
        for options in ({'cutoff': -1}, {'cutoff': 3}, {'cutoff': True},
                        {'background_cutoff': 3}, {'background_cutoff': -1}, {'budget': 60001}):
            with self.subTest(options=options), self.assertRaises(ValueError):
                acceptance.run_suite(fixture=self.tiny_fixture(), **options)

    def test_frozen_histories_have_explicit_holdout_and_temporal_boundaries(self):
        fixture = acceptance.load_fixture()
        self.assertEqual(1, fixture['schema_version'])
        events = acceptance.eligible_events(fixture, 12)
        text = json.dumps(events, ensure_ascii=False)
        self.assertNotIn('FOREIGN_SENTINEL', text)
        self.assertNotIn('DELETE_SENTINEL', text)
        self.assertNotIn('production deployment verified', text)
        self.assertEqual(4, next(e for e in events if e['id'] == 'e12')['occurred_at'])
        self.assertGreater(len({e['session_id'] for e in events}), 2)
        self.assertEqual({'ru', 'en'}, {q['language'] for q in fixture['queries']})
        self.assertTrue(all(q['split'] in ('holdout', 'tuning') for q in fixture['queries']))
        self.assertIn('delete_while_worker_runs', fixture['coverage'])

    def test_v6_copy_generator_reconstructs_span_text_losslessly(self):
        parts = ['  Принято SQLite.\n', 'Preserve  two spaces; punctuation!\n', '\tTrailing whitespace.  ']
        source_id = 'a' * 32
        request = {'prompt': 'Untrusted synthetic evidence\n' + json.dumps({'events': [
            {'id': source_id, 'spans': [{'ref': f'E1S{i}', 'text': part} for i, part in enumerate(parts)]}
        ]}, ensure_ascii=False)}
        episode = acceptance.copy_generator(request)['output']['episodes'][0]
        body = ''.join(parts)
        self.assertEqual(body, episode['summary'])
        self.assertEqual([{'source_id': source_id, 'start': 0, 'end': len(body), 'quote': body}],
                         episode['evidence']['summary'])
        # The canonical injected seam remains compatible with archived body input.
        archived = {'prompt': 'Untrusted synthetic evidence\n' + json.dumps({'events': [
            {'id': source_id, 'body': body}
        ]}, ensure_ascii=False)}
        self.assertEqual(episode, acceptance.copy_generator(archived)['output']['episodes'][0])

    def test_reader_has_no_gold_labels_or_full_history(self):
        fixture = acceptance.load_fixture()
        query = fixture['queries'][0]
        request = acceptance.reader_request('bounded evidence only', query['query'])
        self.assertIn(query['query'], request['prompt'])
        self.assertNotIn('required', request['prompt'])
        self.assertNotIn('forbidden', request['prompt'])
        self.assertNotIn('holdout', request['prompt'])
        self.assertNotIn('Correction:', request['prompt'])

    def test_mode_projection_and_shared_budget_are_explicit(self):
        state = {'episodes': ['history'], 'claims': ['accepted'], 'relations': ['supports'],
                 'provisional_signals': ['new correction']}
        self.assertNotIn('accepted', acceptance.bounded_context('episodes', state, 6000))
        self.assertIn('accepted', acceptance.bounded_context('episodes_state', state, 6000))
        self.assertNotIn('new correction', acceptance.bounded_context('episodes_state', state, 6000))
        self.assertIn('new correction', acceptance.bounded_context('hybrid_signals', state, 6000))
        huge = copy.deepcopy(state)
        huge['episodes'] = ['x' * 20000]
        for mode in acceptance.MODES[1:]:
            context = acceptance.bounded_context(mode, huge, 6000)
            self.assertLessEqual(len(context), 6000)
            self.assertIn('truncated', context)

    def test_baseline_executes_real_temporary_storage_and_never_fakes_quality(self):
        with mock.patch('codex_mem.processor.NativeProcessorRunner', side_effect=AssertionError('No live models in deterministic tests')):
            result = acceptance.run_suite(baseline_only=True, query_ids=['rationale'])
        self.assertEqual('deterministic_transport_only', result['measurement'])
        self.assertIsNone(result['effectiveness_comparison'])
        self.assertIsNone(result['cost_usd'])
        self.assertTrue(any(j['status'] == 'processed' for j in result['baseline_jobs']))
        self.assertIsNone(result['results'][0]['answer'])
        self.assertTrue(all(v is None for v in result['results'][0]['quality'].values()))
        self.assertNotIn('FOREIGN_SENTINEL', result['results'][0]['context'])

    def test_delete_while_worker_runs_cannot_restore_source(self):
        from codex_mem.project_memory import ProjectMemory, capture_event
        from codex_mem.episode_processor import runner_request
        from codex_mem.store import Store
        fixture = acceptance.load_fixture()
        deleted = next(event for event in fixture['events'] if event['id'] == 'e13')
        foreign = next(event for event in fixture['events'] if event['id'] == 'foreign')
        with tempfile.TemporaryDirectory() as temporary, Store(Path(temporary) / 'data') as store:
            project = Path(temporary) / 'orchid'
            original = store.remember(project, 'diagnostic', deleted['text'],
                                      source='hook:UserPromptSubmit', session_id='s4')
            other = store.remember(Path(temporary) / 'other', 'foreign', foreign['text'],
                                   source='hook:UserPromptSubmit', session_id='foreign')
            with store._connection:
                capture_event(store._connection, original['id'])
                capture_event(store._connection, other['id'])
            memory = ProjectMemory(store)
            job = memory.claim(project)
            self.assertNotIn('FOREIGN_SENTINEL', json.dumps(job))
            output = acceptance.copy_generator(runner_request(job, 240))['output']
            store.forget(project, [original['id']])
            self.assertEqual('stale', memory.commit(job, output)['status'])
            snapshot = memory.snapshot(project)
            self.assertNotIn('DELETE_SENTINEL', json.dumps(snapshot))
            self.assertFalse(snapshot['episodes'])

    def test_retained_baseline_reuse_does_not_regenerate_it(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first = acceptance.run_suite(base_dir=root / 'first', query_ids=['rationale'])
            report = root / 'first.json'
            report.write_text(json.dumps(first))
            with mock.patch('codex_mem.processor.process_pending', side_effect=AssertionError('Do not regenerate baseline')):
                second = acceptance.run_suite(base_dir=root / 'second', query_ids=['rationale'],
                                              reuse_baseline_report=report)
            self.assertEqual(first['baseline_data'], second['baseline_data'])
            self.assertEqual(first['baseline_jobs'], second['baseline_jobs'])
            self.assertEqual(str(report), second['reused_baseline_report'])

    def test_project_resume_preserves_completed_sessions_and_readds_pending_tail(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first = acceptance.run_suite(base_dir=root / 'first', query_ids=['rationale'])
            report = root / 'first.json'
            report.write_text(json.dumps(first))
            with mock.patch('codex_mem.processor.process_pending', side_effect=AssertionError('Do not regenerate baseline')):
                second = acceptance.run_suite(base_dir=root / 'second', query_ids=['rationale'],
                                              reuse_baseline_report=report, reuse_project_report=report)
            self.assertEqual(['idle'], [job['status'] for job in second['project_jobs']])
            self.assertEqual({item['id'] for item in first['project_snapshot']['episodes']},
                             {item['id'] for item in second['project_snapshot']['episodes']})
            self.assertTrue(second['project_snapshot']['provisional_signals'])
            self.assertEqual(['e9', 'e10', 'e12'], second['pending_event_ids'])

    def test_reader_only_reuse_never_regenerates_or_reingests_frozen_state(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first = acceptance.run_suite(base_dir=root / 'first', query_ids=['rationale'])
            report = root / 'first.json'
            report.write_text(json.dumps(first))
            before = acceptance.frozen_state_fingerprint(root / 'first' / 'next', Path(first['project']))
            with mock.patch('codex_mem.processor.process_pending', side_effect=AssertionError('No legacy regeneration')), \
                 mock.patch('codex_mem.episode_processor.process_project_memory', side_effect=AssertionError('No PM regeneration')), \
                 mock.patch('codex_mem.processor.NativeProcessorRunner', side_effect=AssertionError('No native calls in preflight')):
                second = acceptance.run_suite(base_dir=root / 'second', query_ids=['rationale'],
                                              reader_only_report=report, production_startup=True)
            self.assertEqual([], second['project_jobs'])
            self.assertEqual(0, second['new_generator_invocations'])
            self.assertEqual('paired_retrieval_only_frozen_regression', second['comparison_kind'])
            self.assertEqual(first['versions'], second['generation_versions'])
            self.assertEqual(before, second['frozen_state_before'])
            self.assertEqual(before, second['frozen_state_after'])
            self.assertEqual(before, acceptance.frozen_state_fingerprint(root / 'first' / 'next', Path(first['project'])))
            self.assertEqual(3, second['frozen_state_after']['row_counts']['pm_usage_attempts'])
            self.assertEqual({item['id'] for item in first['project_snapshot']['provisional_signals']},
                             {item['id'] for item in second['project_snapshot']['provisional_signals']})
            self.assertTrue(second['startup_merge']['complete'])
            self.assertEqual(5, len(second['results']))
            self.assertTrue(all('context_sha256' in item for item in second['results']))
            self.assertTrue(all(item['answer'] is None for item in second['results']))

    def test_reader_only_cannot_mix_generation_reuse_flags(self):
        with self.assertRaisesRegex(ValueError, 'cannot be combined'):
            acceptance.run_suite(reader_only_report=Path('unused.json'), baseline_only=True)

    def test_startup_only_runs_five_mocked_fresh_readers_without_generators(self):
        from codex_mem.processor import MODEL, REASONING_EFFORT
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first = acceptance.run_suite(base_dir=root / 'first', production_startup=True)
            # Simulate native provenance only for the mocked live-routing contract.
            # These temporary canned answers never measure semantic effectiveness.
            first['generator'] = 'native'
            first['measurement'] = 'synthetic_native_contract_fixture_only'
            report = root / 'first.json'
            report.write_text(json.dumps(first))
            requests = []
            def fake_reader(request):
                requests.append(request)
                result = acceptance.receipt({'answer': 'Synthetic mocked reader output.',
                                             'source_ids': [], 'uncertainties': ['Not a quality measurement.']},
                                            MODEL, REASONING_EFFORT)
                result['evidence']['thread_start']['thread_id'] = f'mocked-reader-{len(requests)}'
                return result
            with mock.patch('codex_mem.processor.process_pending', side_effect=AssertionError('No baseline generator')), \
                 mock.patch('codex_mem.episode_processor.process_project_memory', side_effect=AssertionError('No PM generator')), \
                 mock.patch('codex_mem.processor.NativeProcessorRunner', return_value=fake_reader) as native_factory, \
                 mock.patch('builtins.print'):
                result = acceptance.run_suite(base_dir=root / 'second', reader_only_report=report,
                                              startup_only=True, live=True)
            self.assertEqual(5, native_factory.call_count)
            self.assertEqual(5, len(requests))
            self.assertEqual(5, len(result['results']))
            self.assertEqual({'production_startup'}, {item['mode'] for item in result['results']})
            self.assertEqual({q['id'] for q in acceptance.load_fixture()['queries']},
                             {item['query_id'] for item in result['results']})
            self.assertEqual([], result['project_jobs'])
            self.assertEqual(0, result['new_generator_invocations'])
            self.assertEqual(result['frozen_state_before'], result['frozen_state_after'])
            self.assertEqual(3, result['frozen_state_after']['row_counts']['pm_usage_attempts'])
            self.assertTrue(result['startup_merge']['complete'])
            self.assertTrue(all(item['fresh_reader'] for item in result['results']))
            self.assertTrue(all(value is None for item in result['results'] for value in item['quality'].values()))

    def test_startup_only_requires_reader_only_report(self):
        with self.assertRaisesRegex(ValueError, 'requires a reader-only report'):
            acceptance.run_suite(startup_only=True)

    def test_combined_startup_replays_legacy_lineage_in_safe_clone(self):
        result = acceptance.run_suite(production_startup=True, query_ids=['rationale'])
        self.assertTrue(result['startup_merge']['complete'], result['startup_merge'])
        self.assertTrue(result['startup_merge']['imported_legacy_ids'])
        self.assertGreaterEqual(len(result['startup_merge']['source_identity_mapping']), result['event_count'])
        startup = next(item for item in result['results'] if item['mode'] == 'production_startup')
        self.assertLessEqual(startup['context_chars'], 6000)
        self.assertNotIn('reader_skipped', startup)

    def test_four_modes_execute_injected_generator_without_network(self):
        with mock.patch('codex_mem.processor.NativeProcessorRunner', side_effect=AssertionError('No live models')):
            result = acceptance.run_suite(query_ids=['rationale'])
        self.assertEqual(set(acceptance.MODES), {r['mode'] for r in result['results']})
        self.assertEqual(['e9', 'e10', 'e12'], result['pending_event_ids'])
        self.assertTrue(result['project_snapshot']['provisional_signals'])
        self.assertTrue(result['versions']['code_sha256'])
        self.assertEqual('hook:UserPromptSubmit', acceptance.source_for_event({'role': 'user'}))
        self.assertEqual('hook:Stop', acceptance.source_for_event({'role': 'assistant'}))
        self.assertEqual('hook:PostToolUse', acceptance.source_for_event({'role': 'tool'}))
        self.assertTrue(any(job['status'] == 'processed' for job in result['project_jobs']), result['project_jobs'])
        self.assertTrue(all(item['context_chars'] <= 6000 for item in result['results']))
        for item in result['results']:
            self.assertIsNone(item['answer'])
            self.assertNotIn('FOREIGN_SENTINEL', item['context'])
            self.assertNotIn('DELETE_SENTINEL', item['context'])
            self.assertNotIn('production deployment verified', item['context'])


if __name__ == '__main__':
    unittest.main()
