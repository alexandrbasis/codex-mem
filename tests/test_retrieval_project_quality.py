"""Answer-bearing retrieval regressions using fictional project evidence."""
from pathlib import Path
import tempfile
import unittest

from codex_mem import semantic
from codex_mem.config import configure
from codex_mem.store import Store
from tests.test_resume_current_state import Backend, CandidateStore, event, summary


class ProjectQualityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.project = str(Path(self.temp.name) / "project")

    def search(self, rows, query, limit=2):
        store = CandidateStore(rows, {r['id']: event('2026-10-10T10:00:00Z') for r in rows})
        return semantic.search(store, self.project, query, mode='auto', intent='resume',
                               limit=limit, backend=Backend())['results']

    def test_named_integration_abstains_on_generic_vector_neighbors(self):
        rows = [summary('neighbor', title='Integration release', body='Deployment checks remain open.')]
        self.assertEqual([], self.search(rows, 'Что сейчас с интеграцией Mercury и что осталось проверить?'))

    def test_rationale_finding_survives_repeated_topic_only_neighbors(self):
        rows = [summary('handoff', title='Orion disabled', body='Orion is disabled. Capture backlog remains.')]
        rows += [{'id': str(i), 'kind': 'note', 'title': 'Orion documentation',
                  'body': 'Orion examples reviewed.', 'observation': {'type': 'discovery'}} for i in range(5)]
        rows += [{'id': 'reason', 'kind': 'note', 'title': 'Orion disablement reason',
                  'body': 'Orion was disabled because latency exceeded the budget.',
                  'observation': {'type': 'decision'}}]
        result = self.search(rows, 'Почему отключили Orion и какие проблемы остались?')
        self.assertIn('reason', [r['id'] for r in result])

    def test_notification_evidence_precedes_roster_administration(self):
        rows = [summary('roster', title='Nebula roster administration',
                        body='Nebula team roster updated. Notification settings reviewed.'),
                summary('delivery', 'delivery-session', title='Nebula notifications deployed',
                        body='Nebula notifications changed. Delivery tests passed after deployment.')]
        result = self.search(rows, 'Что изменили в уведомлениях Nebula и что проверили после выкладки?', limit=1)
        self.assertEqual(['delivery'], [r['id'] for r in result])

    def test_version_question_recovers_test_and_installation_receipts(self):
        data = Path(self.temp.name) / 'data'
        configure(data, semantic_enabled=False, processor_enabled=False, service_enabled=False)
        with Store(data) as store:
            store.remember(self.project, 'Release 4.2.1 archive', 'Archive packaged.', kind='session_summary')
            receipt = store.remember(self.project, 'Release 4.2.1 verification',
                'Browser tests passed. Installation in the working Tracker remains unverified.',
                kind='session_summary', session_id='validation')
            result = semantic.search(store, self.project,
                'Что проверили в версии 4.2.1 и установили ли приложение в рабочий Tracker?',
                mode='auto', intent='resume', limit=1, backend=Backend())
            self.assertEqual([receipt['id']], [r['id'] for r in result['results']])

    def test_unresolved_conditions_precede_generic_task_details(self):
        rows = [summary('handoff', title='ACME-754 feed importer', body='ACME-754 importer task created.')]
        rows += [{'id': str(i), 'kind': 'note', 'title': 'ACME-754 feed fields',
                  'body': 'ACME-754 maps feed title and image.', 'observation': {'type': 'discovery'}} for i in range(5)]
        rows += [{'id': 'conditions', 'kind': 'note', 'title': 'ACME-754 open operational conditions',
                  'body': 'ACME-754 schedule, geography and credentials remain unconfirmed.',
                  'observation': {'type': 'discovery'}}]
        result = self.search(rows, 'Какие условия импорта ACME-754 ещё не согласованы?')
        self.assertIn('conditions', [r['id'] for r in result])

    def test_named_project_version_recall_keeps_issue_and_path_constraints(self):
        from codex_mem.retrieval import resume_subject_plan
        project = '/fictional/apps/atlas-tracker'
        plan = resume_subject_plan('Что проверили в версии 4.2.1 в Tracker?', project)
        self.assertEqual(['4.2.1'], plan['versions'])
        self.assertEqual([], plan['names'])
        plan = resume_subject_plan('Что проверили в версии 4.2.1 в Tracker для ACME-754 и src/api.py?', project)
        self.assertEqual(['acme-754', 'src/api.py'], plan['identifiers'])

    def test_history_retains_original_relevance_and_does_not_abstain(self):
        rows = [summary('old', title='Historical generic integration'),
                summary('new', 'other', title='Mercury tests passed')]
        result = self.search(rows, 'Покажи историю интеграции Mercury', limit=1)
        self.assertEqual(['old'], [row['id'] for row in result])

    def test_named_topic_in_source_ids_is_not_concrete_evidence(self):
        rows = [summary('neighbor', title='Integration report', source_ids=['Mercury'],
                        body='Only generic release details were captured.')]
        self.assertEqual([], self.search(rows, 'What remains unverified in Mercury?'))

    def test_platform_install_heading_accepts_explicit_project_receipt(self):
        from codex_mem.retrieval import state_scope_matches
        record = {'id': 'install', 'kind': 'note', 'title': 'macOS installation verified',
                  'body': 'Atlas version 4.2.1 is installed in the project. Runtime was verified.'}
        self.assertTrue(state_scope_matches(record, 'installation', '/fictional/apps/atlas'))
        result = semantic.search(CandidateStore([record], {'install': event('2026-10-10T10:00:00Z')}),
            '/fictional/apps/atlas', 'What version is installed in this project?',
            mode='auto', intent='resume', limit=1, backend=Backend())
        self.assertEqual(['install'], [row['id'] for row in result['results']])

    def test_dependency_receipt_cannot_borrow_project_mentions_or_source_ids(self):
        from codex_mem.retrieval import state_scope_matches
        for body in ('Atlas project uses Ruff. Ruff version 0.14.0 is installed.',
                     'Installed Ruff dependency for the Atlas project.',
                     'The source ID is Atlas. Installed Ruff version 0.14.0.'):
            record = {'id': 'dependency', 'title': 'Ruff installation verified', 'body': body}
            self.assertFalse(state_scope_matches(record, 'installation', '/fictional/apps/atlas'))

    def test_structured_receipt_can_supply_explicit_owning_project(self):
        from codex_mem.retrieval import state_scope_matches
        record = {'title': 'macOS installation verified',
                  'observation': {'facts': ['Atlas version 4.2.1 is installed. Runtime was verified.']}}
        self.assertTrue(state_scope_matches(record, 'version', '/fictional/apps/atlas'))

    def test_platform_receipt_survives_crowded_store_installation_window(self):
        data = Path(self.temp.name) / 'data'
        configure(data, semantic_enabled=False, processor_enabled=False, service_enabled=False)
        project = '/fictional/apps/atlas'
        with Store(data) as store:
            receipt = store.remember(project, 'macOS installation verified',
                'Atlas version 4.2.1 is installed in the project. Runtime was verified.',
                observation={'type': 'discovery'})
            store._connection.execute('UPDATE entries SET created_at=? WHERE id=?',
                                      ('2026-10-01T10:00:00Z', receipt['id']))
            for index in range(20):
                store.remember(project, f'Installed Ruff version 0.14.{index} dependency',
                    'Installed Ruff dependency for the Atlas project.', observation={'type': 'discovery'})
            for query in ('What version is installed in this project?', 'What is the current project version?'):
                with self.subTest(query=query):
                    pool = store.retrieval_candidates(project, query=query)
                    self.assertEqual([receipt['id']], [row['id'] for row in pool])
                    result = semantic.search(store, project, query, mode='auto', intent='resume',
                                             limit=1, backend=Backend())
                    self.assertEqual([receipt['id']], [row['id'] for row in result['results']])
