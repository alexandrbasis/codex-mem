"""Project brief selection and XML safety, using only temporary stores."""
import copy
from pathlib import Path
import tempfile
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import patch
from xml.etree import ElementTree

from codex_mem.config import configure
from codex_mem.project_memory_retrieval import build_project_brief, render_project_brief
from codex_mem.store import Store


class ProjectMemoryRetrievalTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.project = str(Path(self.temporary.name) / 'project')
        self.store = Store(Path(self.temporary.name) / 'data')
        self.addCleanup(self.store.close)
        configure(self.store.data_dir, project_memory_mode='pilot',
                  project_memory_projects=[self.project], capture_scope='all')
        self.snapshot = {'claims': [], 'episodes': [], 'provisional_signals': [], 'relations': [],
                         'coverage': {'processed_through_seq': 4, 'pending_events': 2}, 'pending': True}
        module = ModuleType('codex_mem.project_memory')
        module.ProjectMemory = lambda store: SimpleNamespace(snapshot=lambda project, **kw: copy.deepcopy(self.snapshot), get=lambda project, ids: [])
        self.module_patch = patch.dict('sys.modules', {'codex_mem.project_memory': module})
        self.module_patch.start()
        self.addCleanup(self.module_patch.stop)

    def claim(self, id, text, **kw):
        result = {'id': id, 'key': id, 'text': text, 'kind': 'decision',
                  'workflow_state': 'accepted', 'source_role': 'user_intent', 'session_id': 'old',
                  'evidence': {'text': [{'source_id': 'source-' + id, 'start': 0, 'end': len(text)}]}}
        result.update(kw)
        self.snapshot['claims'].append(result)
        return result

    def test_old_obligation_survives_new_completed_work_and_inference_is_marked(self):
        self.claim('deployment', 'Deployment remains unverified', kind='obligation', workflow_state='open')
        self.claim('new', 'Local test passed', kind='result', workflow_state='completed')
        self.claim('maybe', 'Proposed shared server', workflow_state='proposed', source_role='model_inference')
        brief = build_project_brief(self.store, self.project)
        self.assertEqual(['deployment', 'maybe', 'new'], [item['id'] for item in brief['claims']])
        self.assertTrue(brief['claims'][1]['inferred'])
        self.assertIn('proposed', brief['xml'])
        self.assertTrue(brief['pending'])
        self.assertIn('source-deployment', brief['xml'])

    def test_exact_scope_constraints_exclude_near_versions_identifiers_and_environments(self):
        self.claim('wanted', 'Release PRM-915 uses version 1.2.0', scope={'environment': 'production'})
        self.claim('wrong-version', 'Release PRM-915 uses version 1.2.01')
        self.claim('wrong-id', 'Release PRM-9150 uses version 1.2.0')
        self.claim('wrong-env', 'Release PRM-915 uses version 1.2.0', scope={'environment': 'local'})
        brief = build_project_brief(self.store, self.project, query='PRM-915 1.2.0', environment='production')
        self.assertEqual(['wanted'], [item['id'] for item in brief['claims']])

    def test_source_session_project_and_private_markers_fail_closed(self):
        self.claim('current', 'Current work', evidence={'text': [{'source_id': 's', 'session_id': 'current'}]})
        self.claim('foreign', 'Foreign work', project='/another-project')
        self.claim('private', '<private>hidden text</private>')
        self.claim('safe', 'Public task')
        brief = build_project_brief(self.store, self.project, exclude_session_id='current')
        self.assertEqual(['safe'], [item['id'] for item in brief['claims']])
        private = build_project_brief(self.store, self.project, query='<private>secret</private>')
        self.assertFalse(private['enabled'])
        self.assertEqual('', private['xml'])

    def test_only_new_user_signals_are_provisional(self):
        self.snapshot['provisional_signals'] = [
            {'id': 'processed', 'text': 'Correction', 'source_role': 'user_intent', 'seq': 4},
            {'id': 'fresh', 'source_id': 'raw-fresh', 'text': 'Do not deploy yet', 'source_role': 'user_intent', 'seq': 5},
            {'id': 'agent', 'text': 'Everything finished', 'source_role': 'assistant_claim', 'seq': 6}]
        brief = build_project_brief(self.store, self.project)
        self.assertEqual(['fresh'], [item['id'] for item in brief['signals']])
        self.assertEqual('preliminary', brief['signals'][0]['workflow_state'])
        self.assertIn('raw-fresh', brief['xml'])

    def test_conflicts_remain_explicit_without_replacing_claims(self):
        self.claim('a', 'Use local SQLite')
        self.claim('b', 'Use hosted PostgreSQL', workflow_state='proposed')
        self.snapshot['relations'] = [{'id': 'conflict', 'type': 'contradicts', 'from': 'a', 'to': 'b'}]
        brief = build_project_brief(self.store, self.project)
        self.assertEqual(2, len(brief['claims']))
        self.assertIn('contradicts', brief['xml'])

    def test_shadow_and_nonselected_projects_do_not_change_automatic_context(self):
        self.claim('a', 'Accepted decision')
        configure(self.store.data_dir, project_memory_mode='shadow')
        self.assertFalse(build_project_brief(self.store, self.project)['enabled'])
        self.assertTrue(build_project_brief(self.store, self.project, include_shadow=True)['enabled'])
        self.assertFalse(build_project_brief(self.store, self.project + '-other', include_shadow=True)['enabled'])
        configure(self.store.data_dir, capture_enabled=False)
        self.assertFalse(build_project_brief(self.store, self.project, include_shadow=True)['enabled'])

    def test_xml_is_valid_with_escaping_controls_and_exact_budgets(self):
        self.claim('a"<&', '<tag> & "quoted" \x01' * 200)
        brief = build_project_brief(self.store, self.project)
        for budget in (128, 200, 500, 1000, 6000, 60000):
            xml = render_project_brief(brief, budget)
            self.assertLessEqual(len(xml), budget)
            self.assertEqual('project-memory', ElementTree.fromstring(xml).tag)
        self.assertNotIn('<tag>', brief['xml'])
        with self.assertRaises(ValueError):
            render_project_brief(brief, 127)

    def test_query_relevance_limits_episodes_and_output_redacts_contacts(self):
        self.snapshot['episodes'] = [
            {'id': 'relevant', 'summary': 'Importer schedule remains open. Contact alex@example.com',
             'evidence': {'summary': [{'source_id': 'source-relevant'}]}},
            {'id': 'other', 'summary': 'Typography changed'}]
        brief = build_project_brief(self.store, self.project, query='importer schedule')
        self.assertEqual(['relevant'], [item['id'] for item in brief['episodes']])
        self.assertNotIn('alex@example.com', str(brief))
        self.assertIn('[CONTACT]', brief['xml'])

    def test_overflowed_query_constraints_preserve_baseline_and_fail_closed_in_pilot(self):
        query = 'How ' + ' '.join('topic' + str(index) for index in range(18)) + ' final_identifier'
        self.claim('a', 'A decision mentioning final_identifier')
        configure(self.store.data_dir, project_memory_mode='off')
        with patch('codex_mem.project_memory_retrieval.exact_constraints', side_effect=AssertionError('disabled route parsed query')):
            brief = build_project_brief(self.store, self.project, query=query)
        self.assertFalse(brief['enabled'])
        configure(self.store.data_dir, project_memory_mode='pilot')
        brief = build_project_brief(self.store, self.project, query=query)
        self.assertFalse(brief['enabled'])
        self.assertEqual([], brief['records'])
        self.assertEqual('unbounded_literal_constraints', brief['skip_reason'])

    def test_fresh_correction_and_scope_survive_long_claims_under_small_budget(self):
        for index in range(32):
            self.claim(str(index), 'Accepted architecture reason ' * 100,
                       scope={'environment': 'production', 'version': '1.2.0', 'worktree': self.project})
        self.snapshot['provisional_signals'] = [{'id': 'urgent', 'source_id': 'user-correction',
            'text': 'Correction: do not deploy to production', 'source_role': 'user_intent', 'seq': 5}]
        self.snapshot['episodes'] = [{'id': 'episode', 'summary': 'Deployment context'}]
        brief = build_project_brief(self.store, self.project, budget=3000)
        self.assertEqual('urgent', brief['records'][0]['id'])
        self.assertIn('do not deploy', brief['xml'])
        self.assertIn('environment="production"', brief['xml'])
        self.assertIn('version="1.2.0"', brief['xml'])
        self.assertIn('worktree=', brief['xml'])
        self.assertIn('episode', [row['id'] for row in brief['records'][:5]])

    def test_historical_queries_do_not_prepend_current_state(self):
        self.claim('current', 'Current storage uses PostgreSQL')
        brief = build_project_brief(self.store, self.project,
                                    query='What did we decide about storage originally?')
        self.assertFalse(brief['enabled'])
        self.assertEqual('historical_query', brief['skip_reason'])

    def test_exact_query_finds_claim_beyond_default_candidate_window(self):
        # Use validated ledger writes, not an expanded arbitrary snapshot cap.
        self.module_patch.stop()
        from codex_mem.project_memory import ProjectMemory
        memory = ProjectMemory(self.store)
        for batch in range(3):
            texts = ['Storage requirement ' + str(batch * 60 + index) for index in range(60)]
            if batch == 2:
                texts = ['PRM-915 production importer version 1.2.0']
            body = '\n'.join(texts)
            self.store.remember(self.project, 'Project requirements', body,
                                source='hook:UserPromptSubmit', session_id='old-' + str(batch))
            job = memory.claim(self.project)
            self.assertIsNotNone(job)
            source_id = job['sources'][0]['id']
            claims = []
            for index, text in enumerate(texts):
                start = body.index(text)
                claims.append({'key': 'b' + str(batch) + 'c' + str(index), 'text': text,
                    'kind': 'decision', 'workflow_state': 'accepted',
                    'scope': {'environment': 'production', 'version': '1.2.0'},
                    'evidence': {'text': [{'source_id': source_id, 'start': start, 'end': start + len(text), 'quote': text}]}})
            self.assertEqual('committed', memory.commit(job, {'claims': claims})['status'])
        brief = build_project_brief(self.store, self.project, query='PRM-915 1.2.0')
        self.assertEqual(1, len(brief['claims']))
        self.assertIn('PRM-915', brief['claims'][0]['preview'])
        self.assertIsInstance(brief['claims'][0]['source_capture_seq_max'], int)
        self.assertEqual('local_capture_sequence_not_event_time', brief['claims'][0]['capture_order_basis'])

    def test_downgraded_state_and_coverage_gaps_survive_full_and_tiny_xml_budgets(self):
        self.claim('proposed', 'Deployment was claimed complete', workflow_state='proposed',
                   requested_state='completed', source_role='model_inference', evidence_basis='model_inference')
        self.snapshot['coverage'].update(complete_transcript=False, observations_only=True,
            quarantined_events=2, coverage_gaps=True, pending_reconciliation=True,
            omitted_transitions_count=3, downgraded_states_count=1)
        brief = build_project_brief(self.store, self.project)
        self.assertEqual('completed', brief['records'][0]['requested_state'])
        self.assertIn('requested_state="completed"', brief['xml'])
        root = ElementTree.fromstring(brief['xml'])
        coverage = root.find('coverage')
        self.assertEqual('false', coverage.attrib['complete_transcript'])
        self.assertEqual('true', coverage.attrib['observations_only'])
        self.assertEqual('true', coverage.attrib['pending_reconciliation'])
        self.assertEqual('3', coverage.attrib['omitted_transitions_count'])
        self.assertEqual('1', coverage.attrib['downgraded_states_count'])
        self.assertEqual('2', coverage.attrib['quarantined_events'])
        tiny = render_project_brief(brief, budget=128)
        self.assertLessEqual(len(tiny), 128)
        tiny_coverage = ElementTree.fromstring(tiny).find('coverage')
        self.assertEqual('false', tiny_coverage.attrib['complete_transcript'])

    def test_natural_resume_framing_does_not_require_answer_facets_in_every_record(self):
        self.claim('importer', 'The importer regression passed locally; deployment is unverified.')
        self.claim('unrelated', 'The typography change passed locally and remains open.')
        self.snapshot['episodes'] = [{'id': 'importer-episode',
            'summary': 'Importer duplicate regression passed locally.',
            'open_work': 'Deployment remains open; redact contacts before diagnostic export.',
            'evidence': {'summary': [{'source_id': 'source-importer'}],
                         'open_work': [{'source_id': 'source-redaction'}]}}]
        brief = build_project_brief(self.store, self.project,
            query='Resume importer work. What passed and what remains open?')
        self.assertEqual(['importer'], [row['id'] for row in brief['claims']])
        self.assertEqual(['importer-episode'], [row['id'] for row in brief['episodes']])
        self.assertIn('redact contacts', brief['xml'])
        self.assertNotIn('typography', brief['xml'])
        exact = build_project_brief(self.store, self.project,
            query='Resume importer PRM-915 1.2.0 in production. What remains open?')
        self.assertEqual([], exact['records'])

    def test_russian_resume_framing_preserves_the_task_subject(self):
        self.claim('importer', 'Импортёр проверен локально. Развёртывание ещё не проверено.')
        self.claim('unrelated', 'Типографика проверена. Работа остаётся открытой.')
        brief = build_project_brief(self.store, self.project,
            query='Продолжим работу над импортёром. Что проверено и что осталось открытым?')
        self.assertEqual(['importer'], [row['id'] for row in brief['claims']])
        self.assertNotIn('Типографика', brief['xml'])

    def test_field_evidence_labels_and_exact_quotes_keep_open_work_source_distinct(self):
        self.snapshot['episodes'] = [{'id': 'mixed-topic',
            'summary': 'SQLite selected for local storage; contact redaction remains open.',
            'rationale': 'No separate server is required.',
            'open_work': 'Redact personal contacts before diagnostic export.',
            'evidence': {'rationale': [{'source_id': 'storage-source', 'start': 0, 'end': 31,
                                       'quote': 'No separate server is required.'}],
                         'open_work': [{'source_id': 'redaction-source', 'start': 0, 'end': 49,
                                        'quote': 'Redact personal contacts before diagnostic export.'}]}}]
        brief = build_project_brief(self.store, self.project,
            query='What requirement remains before diagnostic export?')
        root = ElementTree.fromstring(brief['xml'])
        sources = root.findall('./item/field/source')
        redaction = next(source for source in sources if source.attrib['source_id'] == 'redaction-source')
        self.assertEqual('open_work', redaction.attrib['field'])
        self.assertIn('Redact personal contacts', redaction.findtext('quote_excerpt'))
        storage = next(source for source in sources if source.attrib['source_id'] == 'storage-source')
        self.assertEqual('rationale', storage.attrib['field'])
        self.assertNotIn('Redact', storage.findtext('quote_excerpt'))

    def test_repeated_evidence_cannot_crowd_later_open_work_out_of_resume_xml(self):
        first = self.claim('accepted', 'The importer uses SQLite because no server is required.',
                           evidence={'text': [{'source_id': 'storage-' + str(index), 'start': 0, 'end': 200,
                                               'quote': 'A long exact storage source excerpt ' * 8} for index in range(8)]})
        self.snapshot['episodes'] = [
            {'id': 'a-local-pass', 'summary': 'The importer regression passed locally; deployment is unverified.',
             'evidence': {'summary': [{'source_id': 'local-result', 'start': 0, 'end': 200,
                                       'quote': 'The importer regression passed locally. ' * 8}]}},
            {'id': 'b-open-export', 'summary': 'Importer diagnostics have an independent open requirement.',
             'open_work': 'Redact personal contact details before diagnostic export.',
             'evidence': {'open_work': [{'source_id': 'redaction-source', 'start': 0, 'end': 55,
                                         'quote': 'Redact personal contact details before diagnostic export.'}]}}]
        brief = build_project_brief(self.store, self.project,
            query='Resume importer work. What passed and what remains open?', budget=3000)
        xml = brief['xml']
        self.assertLessEqual(len(xml), 3000)
        ElementTree.fromstring(xml)
        self.assertIn('passed locally', xml)
        self.assertIn('redaction-source', xml)
        self.assertIn('Redact personal contact', xml)
        self.assertIsNone(ElementTree.fromstring(xml).find('./item[@id="accepted"]'))

    def test_evidence_excerpt_truncation_does_not_claim_full_source_span_equality(self):
        from codex_mem.retrieval import preview_records
        quote = 'A source sentence with more than three hundred characters. ' * 8
        claim = self.claim('long-evidence', 'Keep the importer obligation open.',
            evidence={'text': [{'source_id': 'full-source', 'start': 10, 'end': 10 + len(quote), 'quote': quote}]})
        brief = build_project_brief(self.store, self.project)
        pointer = brief['records'][0]['evidence'][0]
        self.assertNotIn('quote', pointer)
        self.assertEqual(quote[:240], pointer['quote_excerpt'])
        self.assertTrue(pointer['quote_truncated'])
        self.assertEqual(10 + len(quote), pointer['end'])
        compact = preview_records(brief['records'])[0]['evidence'][0]
        self.assertTrue(compact['quote_truncated'])
        self.assertEqual(quote[:240], compact['quote_excerpt'])
        source = ElementTree.fromstring(brief['xml']).find('./item/field/source')
        self.assertEqual(quote[:160], source.findtext('quote_excerpt'))
        self.assertEqual('True', source.attrib['quote_truncated'])
        self.assertEqual(str(10 + len(quote)), source.attrib['end'])
        self.assertEqual(quote, claim['evidence']['text'][0]['quote'])
        tiny = render_project_brief(brief, 128)
        self.assertLessEqual(len(tiny), 128)
        self.assertEqual('project-memory', ElementTree.fromstring(tiny).tag)

    def test_xml_marks_its_extra_excerpt_truncation_when_structured_excerpt_is_complete(self):
        quote = 'Exact quote. ' * 15
        self.claim('xml-excerpt', 'Open work.', evidence={'text': [
            {'source_id': 'source', 'start': 0, 'end': len(quote), 'quote': quote}]})
        brief = build_project_brief(self.store, self.project)
        self.assertFalse(brief['records'][0]['evidence'][0]['quote_truncated'])
        source = ElementTree.fromstring(brief['xml']).find('./item/field/source')
        self.assertEqual('True', source.attrib['quote_truncated'])
        self.assertEqual(quote[:160], source.findtext('quote_excerpt'))

    def test_open_requirement_question_matches_inflected_topic_without_status_words(self):
        self.claim('redaction', 'Redact contacts before exporting diagnostic reports.',
                   kind='requirement', workflow_state='open')
        self.claim('unrelated', 'An independent requirement remains open for typography.')
        for query in ('What independent requirement remains before diagnostic export?',
                      'What requirements remain before diagnostics exports?'):
            brief = build_project_brief(self.store, self.project, query=query)
            self.assertEqual(['redaction'], [row['id'] for row in brief['claims']])
            self.assertIn('Redact contacts', brief['xml'])

    def test_fields_never_render_generated_outcome_with_only_unrelated_open_work_sources(self):
        self.snapshot['episodes'] = [{'id': 'mixed-environments',
            'outcome': 'Importer version 2.1 passed locally. No deployment was performed.',
            'open_work': 'Staging is 2.0 and production version is unchecked.',
            'summary': 'Importer passed locally; staging remains 2.0.',
            'source_capture_seq_min': 10, 'source_capture_seq_max': 12,
            'capture_order_basis': 'local_capture_sequence_not_event_time', 'revision': 3,
            'evidence': {'outcome': [{'source_id': 'local-source', 'start': 0, 'end': 70,
                                      'quote': 'Importer version 2.1 passed locally. No deployment was performed.'}],
                         'open_work': [{'source_id': 'staging-source', 'start': 0, 'end': 60,
                                       'quote': 'Staging is 2.0 and production version is unchecked.'}],
                         'summary': [{'source_id': 'local-source'}, {'source_id': 'staging-source'}]}}]
        brief = build_project_brief(self.store, self.project, query='Resume importer work.', budget=1500)
        root = ElementTree.fromstring(brief['xml'])
        for field in root.findall('./item/field'):
            ids = [source.attrib['source_id'] for source in field.findall('source')]
            if 'passed locally' in (field.findtext('derived_text') or ''):
                self.assertIn('local-source', ids)
            if field.attrib['name'] == 'outcome':
                self.assertNotIn('staging-source', ids)
        item = root.find('item')
        self.assertEqual('12', item.attrib['source_capture_seq_max'])
        self.assertEqual('local_capture_sequence_not_event_time', item.attrib['capture_order_basis'])
        self.assertEqual('3', item.attrib['revision'])
        self.assertNotIn('completed', brief['xml'])

    def test_requirement_topic_filter_precedes_candidate_limit_with_inflected_export(self):
        self.module_patch.stop()
        from codex_mem.project_memory import ProjectMemory
        memory = ProjectMemory(self.store)
        for batch in range(3):
            texts = ['Diagnostic typography obligation ' + str(batch * 60 + index) for index in range(60)]
            if batch == 2:
                texts = ['Redact contacts before exporting diagnostic reports.']
            body = '\n'.join(texts)
            self.store.remember(self.project, 'Requirements', body,
                source='hook:UserPromptSubmit', session_id='requirement-' + str(batch))
            job = memory.claim(self.project)
            source_id = job['sources'][0]['id']
            claims = []
            for index, text in enumerate(texts):
                start = body.index(text)
                claims.append({'key': 'r' + str(batch) + '_' + str(index), 'text': text,
                    'kind': 'requirement', 'workflow_state': 'open',
                    'evidence': {'text': [{'source_id': source_id, 'start': start,
                                          'end': start + len(text), 'quote': text}]}})
            self.assertEqual('committed', memory.commit(job, {'claims': claims})['status'])
        brief = build_project_brief(self.store, self.project,
            query='What independent requirement remains before diagnostic export?')
        self.assertEqual(1, len(brief['claims']))
        self.assertIn('Redact contacts', brief['xml'])

    def test_actual_startup_preserves_older_user_obligation_with_current_open_work_and_legacy_pass(self):
        self.claim('accepted-storage', 'The importer uses SQLite.',
                   scope={'environment': 'local installation'})
        self.claim('staging-fact', 'The importer staging version is 2.0.', kind='fact',
                   scope={'environment': 'staging', 'version': '2.0'})
        self.snapshot['episodes'] = [
            {'id': 'current-episode', 'summary': 'Importer checked locally.',
             'open_work': 'Production version is unchecked. No deployment was performed. Staging is 2.0.',
             'revision': 3, 'source_capture_seq_min': 13, 'source_capture_seq_max': 15,
             'capture_order_basis': 'local_capture_sequence_not_event_time',
             'evidence': {'open_work': [
                 {'source_id': 'staging-source', 'source_role': 'tool_record', 'start': 0, 'end': 32,
                  'quote': 'Production version is unchecked.'},
                 {'source_id': 'local-result', 'source_role': 'tool_record', 'start': 80, 'end': 109,
                  'quote': 'No deployment was performed.'}]}},
            {'id': 'old-user-obligation', 'summary': 'Importer diagnostic exports have an open requirement.',
             'open_work': 'Redact personal contact details before diagnostic export.',
             'revision': 1, 'source_capture_seq_min': 1, 'source_capture_seq_max': 5,
             'capture_order_basis': 'local_capture_sequence_not_event_time',
             'evidence': {'open_work': [{'source_id': 'redaction-source', 'source_role': 'user_intent',
                 'start': 0, 'end': 55, 'quote': 'Redact personal contact details before diagnostic export.'}]}},
            {'id': 'historical-investigation', 'summary': 'Importer investigation was incomplete.',
             'open_work': 'No verified fix yet.', 'revision': 2,
             'source_capture_seq_min': 7, 'source_capture_seq_max': 11,
             'capture_order_basis': 'local_capture_sequence_not_event_time',
             'evidence': {'open_work': [{'source_id': 'old-investigation', 'source_role': 'assistant_report',
                 'start': 0, 'end': 20, 'quote': 'No verified fix yet.'}]}}]
        self.module_patch.stop()
        self.store.remember(self.project, 'Importer regression passed locally',
            'Importer regression PASSED locally in version 2.1. Deployment remains open and has not been performed.',
            kind='session_summary')
        self.module_patch.start()
        xml = self.store.context(self.project,
            query='Resume importer work. What passed and what remains open?', budget=6000,
            allow_remote=False, include_health=False)
        self.assertLessEqual(len(xml), 6000)
        root = ElementTree.fromstring(xml)
        project_memory = root.find('project-memory')
        items = project_memory.findall('item')
        self.assertEqual('current-episode', items[0].attrib['id'])
        self.assertEqual('old-user-obligation', items[1].attrib['id'])
        self.assertIn('Redact personal contact', ''.join(project_memory.itertext()))
        self.assertIn('locally', ''.join(root.itertext()))
        no_deploy = [source for source in project_memory.findall('.//source')
                     if 'No deployment' in (source.findtext('quote_excerpt') or '')]
        self.assertTrue(no_deploy)
        self.assertTrue(all(source.attrib['source_id'] == 'local-result' for source in no_deploy))
        self.assertEqual('1', items[1].attrib['source_capture_seq_min'])

    def test_broad_resume_reserves_global_requirement_after_episode_head_advances(self):
        from codex_mem.retrieval import preview_records
        old = self.claim('old-redaction', 'Redact personal contacts before diagnostics export.',
                         kind='requirement', workflow_state='open')
        self.snapshot['claims'] = []
        self.snapshot['project_obligations'] = [old]
        self.snapshot['episodes'] = [
            {'id': 'head-v2', 'summary': 'Importer local regression passed.',
             'open_work': 'Deployment remains unverified.', 'logical_episode_id': 'importer-work',
             'episode_version': 2, 'parent_episode_id': 'head-v1', 'is_current_head': True,
             'continuity_basis': 'model_inference', 'revision': 2,
             'evidence': {'summary': [{'source_id': 'local-result'}],
                          'open_work': [{'source_id': 'deployment-source'}]}},
            {'id': 'head-v1', 'summary': 'Importer previous investigation.',
             'logical_episode_id': 'importer-work', 'episode_version': 1, 'is_current_head': False,
             'evidence': {'summary': [{'source_id': 'old-result'}]}}]
        brief = build_project_brief(self.store, self.project, query='Resume importer work.')
        self.assertEqual(['old-redaction'], [row['id'] for row in brief['claims']])
        self.assertEqual(['head-v2'], [row['id'] for row in brief['episodes']])
        self.assertEqual('old-redaction', brief['records'][0]['id'])
        self.assertIn('Redact personal contacts', brief['xml'])
        head = preview_records(brief['episodes'])[0]
        self.assertEqual(2, head['episode_version'])
        self.assertEqual('head-v1', head['parent_episode_id'])
        self.assertTrue(head['is_current_head'])
        self.assertEqual('model_inference', head['continuity_basis'])
        self.assertIn('episode_version="2"', brief['xml'])
        for query, kwargs in [('Resume importer PRM-915 work.', {}),
                              ('Resume importer version 2.1.', {}),
                              ('Resume importer work in production.', {}),
                              ('Resume importer work.', {'environment': 'staging'}),
                              ('importer', {})]:
            scoped = build_project_brief(self.store, self.project, query=query, **kwargs)
            self.assertNotIn('old-redaction', [row['id'] for row in scoped['claims']], (query, kwargs))

    def test_provisional_execution_signal_keeps_tool_role_exit_and_field_provenance(self):
        from codex_mem.retrieval import preview_records
        self.snapshot['provisional_signals'] = [
            {'id': 'remember', 'source_id': 'remember', 'seq': 5, 'session_id': 'user-session',
             'text': 'Remember the importer export constraint.', 'source_role': 'user_intent',
             'signal_kind': 'remember_request', 'selection_basis': 'explicit_user_marker'},
            {'id': 'execution', 'source_id': 'execution', 'seq': 6, 'session_id': 'tool-session',
             'text': 'Captured importer execution result: tests passed.', 'source_role': 'tool_record',
             'signal_kind': 'observed_execution_result', 'selection_basis': 'captured_execution_shape',
             'provenance': {'tool_use_id': 'use-1', 'tool_name': 'exec_command',
                 'command': 'python -m unittest importer', 'exit_code': 0,
                 'result_field': 'tool_response.stdout', 'result_excerpt': 'Importer tests passed.',
                 'capture_recorded_at': '2026-10-20T10:00:00Z'},
             'evidence': {'execution_command': [{'source_id': 'execution', 'tool_use_id': 'use-1',
                 'source_field': 'tool_input.command', 'source_role': 'tool_record',
                 'start': 0, 'end': 28, 'quote': 'python -m unittest importer'}],
                 'execution_result': [{'source_id': 'execution', 'tool_use_id': 'use-1',
                 'source_field': 'tool_response.stdout', 'source_role': 'tool_record',
                 'start': 0, 'end': 22, 'quote': 'Importer tests passed.'}]}},
            {'id': 'forged', 'source_id': 'forged', 'seq': 7,
             'text': 'Importer completed', 'source_role': 'tool_record',
             'signal_kind': 'observed_execution_result', 'provenance': {'exit_code': True}}]
        brief = build_project_brief(self.store, self.project, query='Resume importer work.')
        self.assertEqual(['remember', 'execution'], [row['id'] for row in brief['signals']])
        tool = preview_records(brief['signals'])[1]
        self.assertEqual('tool_record', tool['source_role'])
        self.assertEqual('preliminary', tool['workflow_state'])
        self.assertTrue(tool['provisional'])
        self.assertEqual('not_assessed', tool['provenance']['verification'])
        self.assertEqual(0, tool['execution']['exit_code'])
        self.assertEqual(['execution'], tool['source_ids'])
        self.assertEqual('tool_response.stdout', tool['fields'][0]['evidence'][0]['source_field'])
        root = ElementTree.fromstring(brief['xml'])
        execution = next(item for item in root.findall('item') if item.attrib['id'] == 'execution')
        self.assertEqual('tool_record', execution.attrib['source_role'])
        self.assertEqual('0', execution.find('execution').attrib['exit_code'])
        self.assertIsNotNone(execution.find('./field[@name="execution_result"]/source'))
        self.assertNotIn('verified', execution.attrib)
        excluded = build_project_brief(self.store, self.project, query='Resume importer work.',
                                       exclude_session_id='tool-session')
        self.assertNotIn('execution', [row['id'] for row in excluded['signals']])
        for budget in (128, 6000, 60000):
            xml = render_project_brief(brief, budget)
            self.assertLessEqual(len(xml), budget)
            ElementTree.fromstring(xml)

    def test_actual_store_context_keeps_global_requirement_latest_episode_head_and_fresh_roles(self):
        self.module_patch.stop()
        from codex_mem.project_memory import ProjectMemory
        from codex_mem.tool_io import normalize_capture
        memory = ProjectMemory(self.store)
        first_summary = 'PRM-915 importer investigation.'
        requirement = 'Redact contacts before exporting diagnostic reports.'
        body = first_summary + ' ' + requirement
        entry = self.store.remember(self.project, 'Initial task', body,
            source='hook:UserPromptSubmit', session_id='initial')
        job = memory.claim(self.project)
        def ref(source_id, body, text):
            start = body.index(text)
            return {'source_id': source_id, 'start': start, 'end': start + len(text), 'quote': text}
        receipt = memory.commit(job, {'episodes': [{'key': 'investigation', 'summary': first_summary,
            'evidence': {'summary': [ref(entry['id'], body, first_summary)]}}],
            'claims': [{'key': 'export-requirement', 'text': requirement, 'kind': 'requirement',
                'workflow_state': 'open', 'evidence': {'text': [ref(entry['id'], body, requirement)]}}]})
        self.assertEqual('committed', receipt['status'])
        parent = memory.snapshot(self.project)['episodes'][0]['id']
        requirement_id = memory.snapshot(self.project)['claims'][0]['id']
        continuation = 'Continue PRM-915 importer investigation.'
        latest = 'The importer checks passed locally; deployment remains unverified.'
        body = continuation + ' ' + latest
        entry = self.store.remember(self.project, 'Continuation', body,
            source='hook:UserPromptSubmit', session_id='continuation')
        job = memory.claim(self.project)
        receipt = memory.commit(job, {'episodes': [{'key': 'continuation', 'summary': latest,
            'continuation_of': parent, 'evidence': {
                'summary': [ref(entry['id'], body, latest)],
                'continuation_of': [ref(entry['id'], body, continuation)]}}]})
        self.assertEqual('committed', receipt['status'])
        heads = memory.snapshot(self.project)['episodes']
        self.assertEqual(1, len(heads))
        self.assertEqual(2, heads[0]['episode_version'])
        self.store.remember(self.project, 'Remember requirement', 'Remember the importer export constraint.',
            source='hook:UserPromptSubmit', session_id='fresh-user')
        capture = normalize_capture({'tool_name': 'functions.exec_command', 'session_id': 'fresh-tool',
            'tool_use_id': 'actual-execution', 'tool_input': {'cmd': 'python -m unittest importer'},
            'tool_response': {'exit_code': 0, 'output': 'Importer checks passed.'}}, project=self.project)
        self.store.remember(self.project, 'Execution', 'Captured tool output', session_id='fresh-tool',
            source='hook:PostToolUse:actual-execution', tool_capture=capture)
        brief = build_project_brief(self.store, self.project, query='Resume importer work.')
        self.assertEqual(['remember_request', 'observed_execution_result'],
                         [row['signal_kind'] for row in brief['signals']])
        self.assertIn(requirement, brief['xml'])
        self.assertIn('episode_version="2"', brief['xml'])
        xml = self.store.context(self.project, query='Resume importer work.', budget=6000,
                                 allow_remote=False, include_health=False)
        self.assertLessEqual(len(xml), 6000)
        root = ElementTree.fromstring(xml)
        self.assertIn(requirement, ''.join(root.itertext()))
        self.assertIn('episode_version="2"', xml)
        self.assertIn('signal_kind="remember_request"', xml)
        self.assertIn('signal_kind="observed_execution_result"', xml)
        self.assertNotIn('workflow_state="completed"', xml)
        exact = build_project_brief(self.store, self.project, query='Resume importer PRM-915 work.')
        self.assertNotIn(requirement_id, [row['id'] for row in exact['claims']])
        self.assertEqual('open', memory.get(self.project, [requirement_id])[0]['workflow_state'])

    def test_actual_startup_natural_task_question_preserves_versions_and_scope_separation(self):
        current = {'id': 'nex-current', 'summary': 'NEX-418 passed locally; staging and production differ.',
            'goal': 'Continue NEX-418 release work.', 'outcome': 'Local version 2.8.4 passed.',
            'open_work': 'Staging is 2.7.1; production is unchecked.', 'revision': 3,
            'source_capture_seq_min': 10, 'source_capture_seq_max': 13,
            'capture_order_basis': 'local_capture_sequence_not_event_time',
            'evidence': {'outcome': [
                {'source_id': 'local-pass', 'source_role': 'tool_record', 'start': 0, 'end': 42,
                 'quote': 'NEX-418 version 2.8.4 PASSED locally.'}],
                'open_work': [
                    {'source_id': 'staging', 'source_role': 'tool_record', 'start': 0, 'end': 42,
                     'quote': 'NEX-418 staging still reports version 2.7.1.'},
                    {'source_id': 'production', 'source_role': 'tool_record', 'start': 0, 'end': 42,
                     'quote': 'NEX-418 production version remains unchecked.'}]}}
        unrelated = {'id': 'other-task', 'summary': 'AUX-907 volunteer training passed locally in version 9.0.',
            'evidence': {'summary': [{'source_id': 'unrelated', 'quote': 'AUX-907 version 9.0 passed.'}]}}
        self.snapshot['episodes'] = [current, unrelated]
        self.claim('known-result', 'NEX-418 version 2.8.4 passed locally.', kind='result', workflow_state='completed')
        query = 'Summarize the local, staging, and production states for NEX-418. Keep volunteer-training separate and identify verification gaps.'
        xml = self.store.context(self.project, query=query, budget=6000, allow_remote=False, include_health=False)
        self.assertLessEqual(len(xml), 6000)
        self.assertIn('2.8.4', xml)
        self.assertIn('2.7.1', xml)
        self.assertIn('unchecked', xml)
        self.assertNotIn('9.0', xml)
        self.assertNotIn('AUX-907', xml)
        brief = build_project_brief(self.store, self.project,
            query='Resume NEX-418 at the release checkpoint. What succeeded and what current rule governs the next edit?')
        self.assertIn('known-result', [row['id'] for row in brief['claims']])
        self.assertNotIn('other-task', [row['id'] for row in brief['episodes']])

    def test_actual_startup_continuity_projects_session_labels_and_primary_source_statements(self):
        self.snapshot['episodes'] = []
        for index, session in enumerate(('studio-one', 'studio-two', 'studio-three'), 1):
            quote = ('Continue NEX-418 from studio-' + ('two' if index == 3 else 'one') + '.') if index > 1 else 'NEX-418 began in studio-one.'
            self.snapshot['episodes'].append({'id': 'episode-' + str(index),
                'summary': 'NEX-418 release work.', 'goal': 'Continue NEX-418 release work.',
                'session_id': session, 'revision': index, 'source_capture_seq_max': index,
                'evidence': {'continuation_of': [{'source_id': 'source-' + str(index),
                    'source_role': 'user_intent', 'start': 0, 'end': len(quote), 'quote': quote}],
                    'goal': [{'source_id': 'source-' + str(index), 'quote': quote}]}})
        query = 'Which sessions explicitly continue the same NEX-418 task? Explain the links using recorded user statements.'
        xml = self.store.context(self.project, query=query, budget=6000, allow_remote=False, include_health=False)
        self.assertLessEqual(len(xml), 6000)
        for name in ('studio-one', 'studio-two', 'studio-three'):
            self.assertIn(name, xml)
        self.assertIn('continuation_of', xml)
        self.assertIn('source_role="user_intent"', xml)
        self.assertIn('quote_excerpt', xml)

    def test_unanchored_rationale_keeps_reconsideration_source_before_unrelated_pending_rule(self):
        self.snapshot['episodes'] = [{'id': 'format-choice',
            'summary': 'The diagram generator uses an editable vector format.',
            'rationale': 'It stays sharp when resized. Reconsider if the hosting partner stops accepting vectors.',
            'open_work': 'The author attribution still needs review.',
            'evidence': {'rationale': [
                {'source_id': 'format-user', 'source_role': 'user_intent', 'start': 0, 'end': 32,
                 'quote': 'It stays sharp when resized.'},
                {'source_id': 'format-user', 'source_role': 'user_intent', 'start': 33, 'end': 115,
                 'quote': 'Reconsider if the hosting partner stops accepting vectors.'}],
                'open_work': [{'source_id': 'attribution-user', 'quote': 'Author attribution needs review.'}]}}]
        self.snapshot['provisional_signals'] = [{'id': 'pending-rule', 'source_id': 'pending-rule',
            'text': 'The diagram limit is now 23.', 'source_role': 'user_intent', 'signal_kind': 'correction', 'seq': 5}]
        xml = self.store.context(self.project,
            query='Why was the editable vector diagram format chosen, and when should that decision be reconsidered?',
            budget=6000, allow_remote=False, include_health=False)
        self.assertLessEqual(len(xml), 6000)
        self.assertIn('hosting partner stops accepting', xml)
        self.assertIn('sharp when resized', xml)
        root = ElementTree.fromstring(xml)
        first = root.find('./project-memory/item')
        self.assertEqual('format-choice', first.attrib['id'])

    def test_actual_cross_script_environment_fallback_keeps_recorded_english_versions(self):
        self.module_patch.stop()
        from codex_mem.project_memory import ProjectMemory
        memory = ProjectMemory(self.store)
        local = 'QNX-844 renderer 7.2.1 passed locally.'
        staging = 'QNX-844 staging renderer is 7.1.8.'
        production = 'QNX-844 production version has not been checked.'
        body = ' '.join((local, staging, production))
        entry = self.store.remember(self.project, 'Renderer checks', body,
            source='hook:UserPromptSubmit', session_id='english-records')
        job = memory.claim(self.project)
        def ref(text):
            start = body.index(text)
            return {'source_id': entry['id'], 'start': start, 'end': start + len(text), 'quote': text}
        memory.commit(job, {'episodes': [{'key': 'renderer', 'summary': body,
            'outcome': local, 'open_work': production,
            'evidence': {'summary': [ref(local), ref(staging), ref(production)],
                         'outcome': [ref(local)], 'open_work': [ref(production)]}}]})
        self.store.remember(self.project, 'Новое указание', 'Уточнение: локальная проверка не означает запуск в продакшене.',
            source='hook:UserPromptSubmit', session_id='russian-request')
        xml = self.store.context(self.project,
            query='Раздели состояние генератора по локальной среде, стейджингу и продакшену. Что проверено?',
            budget=6000, allow_remote=False, include_health=False)
        self.assertIn('7.2.1', xml)
        self.assertIn('7.1.8', xml)
        self.assertIn('has not been checked', xml)
        self.assertLessEqual(len(xml), 6000)
        excluded = build_project_brief(self.store, self.project,
            query='Раздели состояние генератора по локальной среде, стейджингу и продакшену. Что проверено?',
            exclude_session_id='english-records')
        self.assertNotIn('7.2.1', excluded['xml'])

    def test_continuity_adds_only_explicit_boundary_from_the_same_cited_user_event(self):
        lead = 'Resume the same NEX-418 task from studio-first.'
        boundary = ' Start a distinct task PRE-219; PRE-219 is not a continuation of NEX-418.'
        self.module_patch.stop()
        entry = self.store.remember(self.project, 'Continuation with boundary', lead + boundary,
            source='hook:UserPromptSubmit', session_id='studio-second')
        self.store.remember(self.project, 'Different event', 'NEVER_EXPAND_THIS_UNCITED_EVENT',
            source='hook:UserPromptSubmit', session_id='studio-second')
        self.module_patch.start()
        pointer = {'source_id': entry['id'], 'source_role': 'user_intent', 'start': 0,
                   'end': len(lead), 'quote': lead}
        self.snapshot['episodes'] = [{'id': 'continued-labels',
            'summary': 'NEX-418 label release work continued.', 'goal': 'Continue NEX-418 label release work.',
            'session_id': 'studio-second', 'revision': 2,
            'evidence': {'continuation_of': [pointer], 'goal': [pointer]}}]
        query = 'Which chats continue the same label task, and is the separate preview task part of it? Use user statements.'
        xml = self.store.context(self.project, query=query, budget=6000,
                                 allow_remote=False, include_health=False)
        self.assertIn('studio-first', xml)
        self.assertIn('studio-second', xml)
        self.assertIn('PRE-219 is not a continuation', xml)
        self.assertNotIn('NEVER_EXPAND_THIS_UNCITED_EVENT', xml)
        self.assertLessEqual(len(xml), 6000)
        sources = ElementTree.fromstring(xml).findall('.//field[@name="source_context"]/source')
        self.assertEqual(1, len(sources))
        self.assertEqual(entry['id'], sources[0].attrib['source_id'])
        self.assertEqual('same_source_adjacent_user_statement', sources[0].attrib['context_basis'])
        scoped = build_project_brief(self.store, self.project, query=query,
                                      exclude_session_id='studio-second')
        self.assertNotIn('PRE-219', scoped['xml'])
