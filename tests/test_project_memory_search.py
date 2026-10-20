"""Pilot resume merges project ledger candidates without relaxing search gates."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from codex_mem.config import configure
from codex_mem.integration import search_memory
from codex_mem.store import Store


class ProjectMemorySearchTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.project = str(Path(self.temporary.name) / 'project')
        self.store = Store(Path(self.temporary.name) / 'data')
        self.addCleanup(self.store.close)
        configure(self.store.data_dir, semantic_enabled=False, capture_scope='all',
                  project_memory_mode='pilot', project_memory_projects=[self.project])
        self.note = self.store.remember(self.project, 'Importer schedule', 'Importer schedule uses cron.')
        self.record = {'id': 'pm_task', 'project': self.project, 'title': 'Importer deployment',
                       'preview': 'Importer deployment remains open', 'kind': 'project_memory_claim',
                       'workflow_state': 'open', 'source': 'project_memory', 'session_id': 'old',
                       'source_ids': [], 'provenance': {'verification': 'not_assessed'}}
        self.brief = {'enabled': True, 'mode': 'pilot', 'records': [self.record],
                      'coverage': {'processed_through_seq': 3}, 'pending': True, 'revision': 2}

    def test_resume_balances_project_ledger_with_existing_results_and_honors_limit(self):
        with patch('codex_mem.project_memory_retrieval.build_project_brief', return_value=self.brief) as brief:
            result = search_memory(self.store, self.project, 'importer', intent='resume', limit=2)
        brief.assert_called_once_with(self.store, self.project, 'importer')
        self.assertEqual(['pm_task', self.note['id']], [row['id'] for row in result['results']])
        self.assertTrue(result['project_memory']['pending'])
        self.assertEqual('not_verified', result['project_memory']['freshness'])
        self.assertEqual(1, result['project_memory']['included_count'])
        with patch('codex_mem.project_memory_retrieval.build_project_brief', return_value=self.brief):
            result = search_memory(self.store, self.project, 'importer', intent='resume', limit=1)
        self.assertEqual(1, len(result['results']))

    def test_lookup_never_requests_project_ledger_candidates(self):
        with patch('codex_mem.project_memory_retrieval.build_project_brief') as brief:
            result = search_memory(self.store, self.project, 'importer', intent='lookup')
        brief.assert_not_called()
        self.assertEqual(self.note['id'], result['results'][0]['id'])
        self.assertNotIn('project_memory', result)

    def test_every_explicit_metadata_filter_skips_ledger(self):
        filters = {'kinds': ['note'], 'types': ['decision'], 'files': ['src/importer.py'], 'concepts': ['cron']}
        for field, value in filters.items():
            with self.subTest(field=field), patch('codex_mem.project_memory_retrieval.build_project_brief') as brief:
                search_memory(self.store, self.project, 'importer', intent='resume', **{field: value})
                brief.assert_not_called()

    def test_established_validation_runs_before_project_ledger(self):
        for kwargs in ({'limit': 0}, {'intent': 'invalid'}, {'files': 15}):
            with self.subTest(kwargs=kwargs), patch('codex_mem.project_memory_retrieval.build_project_brief') as brief:
                options = {'intent': 'resume', **kwargs}
                with self.assertRaises(ValueError):
                    search_memory(self.store, self.project, 'importer', **options)
                brief.assert_not_called()

    def test_real_disabled_shadow_foreign_and_private_reads_add_no_ledger_metadata(self):
        for mode, project, query in (('off', self.project, 'importer'),
                                     ('shadow', self.project, 'importer'),
                                     ('pilot', self.project + '-other', 'importer'),
                                     ('pilot', self.project, '<private>importer</private>')):
            with self.subTest(mode=mode, project=project, query=query):
                configure(self.store.data_dir, project_memory_mode=mode)
                result = search_memory(self.store, project, query, intent='resume')
                self.assertNotIn('project_memory', result)

    def test_actual_project_signal_obeys_exact_identifiers_and_project_scope(self):
        # Captured user messages provide pending provisional state before any
        # model is available. The real ledger selector enforces exact literals.
        self.store.remember(self.project, 'Request', 'PRM-915 deploy version 1.2.0 later',
                            source='hook:UserPromptSubmit', session_id='user-session')
        result = search_memory(self.store, self.project, 'PRM-915 1.2.0', intent='resume')
        self.assertIn('project_memory', result)
        projected = [row for row in result['results'] if row.get('source') == 'project_memory']
        self.assertEqual(1, len(projected))
        self.assertTrue(projected[0]['provisional'])
        mismatch = search_memory(self.store, self.project, 'PRM-9150 1.2.0', intent='resume')
        self.assertFalse(any(row.get('source') == 'project_memory' for row in mismatch['results']))

    def test_historical_resume_retains_legacy_history_without_current_ledger_augmentation(self):
        with patch('codex_mem.project_memory_retrieval.build_project_brief') as brief:
            result = search_memory(self.store, self.project,
                                   'What did we decide about importer originally?', intent='resume')
        brief.assert_not_called()
        self.assertNotIn('project_memory', result)

    def test_mcp_default_compact_search_preserves_project_signal_status_scope_and_sources(self):
        from codex_mem.mcp import MemoryMCPServer
        self.record.update(scope={'environment': 'production', 'version': '1.2.0', 'worktree': self.project},
            workflow_state='proposed', requested_state='completed', source_role='user_intent', provisional=True,
            inferred=False, lane='signal', project_memory_kind='correction', revision=2,
            evidence=[{'source_id': 'exact-source', 'start': 0, 'end': 25}], source_ids=['exact-source'])
        server = MemoryMCPServer(store=self.store)
        with patch('codex_mem.project_memory_retrieval.build_project_brief', return_value=self.brief):
            records = server._execute_tool('memory_search', {'project': self.project, 'query': 'importer',
                                                           'intent': 'resume', 'limit': 2})
        first = records[0]
        self.assertEqual('compact', first['detail'])
        self.assertEqual('proposed', first['workflow_state'])
        self.assertEqual('completed', first['requested_state'])
        self.assertEqual('production', first['scope']['environment'])
        self.assertTrue(first['provisional'])
        self.assertEqual('user_intent', first['source_role'])
        self.assertEqual(['exact-source'], first['source_ids'])
        self.assertEqual('exact-source', first['evidence'][0]['source_id'])

    def test_default_compact_mcp_keeps_each_prose_field_with_its_own_sources(self):
        from codex_mem.mcp import MemoryMCPServer
        self.record['fields'] = [
            {'name': 'outcome', 'text': 'Importer passed locally.',
             'evidence': [{'source_id': 'local-source', 'field': 'outcome'}]},
            {'name': 'open_work', 'text': 'Production remains unchecked.',
             'evidence': [{'source_id': 'production-source', 'field': 'open_work'}]}]
        server = MemoryMCPServer(store=self.store)
        with patch('codex_mem.project_memory_retrieval.build_project_brief', return_value=self.brief):
            records = server._execute_tool('memory_search', {'project': self.project, 'query': 'importer',
                                                           'intent': 'resume', 'limit': 2})
        fields = {field['name']: field for field in records[0]['fields']}
        self.assertEqual('local-source', fields['outcome']['evidence'][0]['source_id'])
        self.assertEqual('production-source', fields['open_work']['evidence'][0]['source_id'])
