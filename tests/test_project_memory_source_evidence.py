"""Original role-balanced evidence stays readable without extractor citations."""
import json
from pathlib import Path
import tempfile
import unittest
from xml.etree import ElementTree

from codex_mem.config import configure
from codex_mem.episode_processor import process_project_memory
from codex_mem.project_memory_retrieval import build_project_brief
from codex_mem.store import Store
from codex_mem.tool_io import normalize_capture
from test_episode_processor import receipt


class SourceEvidenceTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.project = Path(temporary.name) / 'observatory'
        self.project.mkdir()
        self.data = Path(temporary.name) / 'data'
        configure(self.data, capture_scope='all', project_memory_mode='pilot',
                  project_memory_projects=[str(self.project)], jev_quality_enabled=False)

    def add_report(self, text, session='qa', project=None):
        with Store(self.data) as store:
            return store.remember(project or self.project, 'Assistant report', text,
                                  source='hook:Stop', session_id=session)

    def add_execution(self, task='QA-42', session='qa', code=1):
        command, output = f'python check.py {task}', f'{task} check failed: exit {code}.'
        capture = normalize_capture({'tool_name': 'functions.exec_command', 'session_id': session,
            'tool_use_id': 'check-' + task + '-' + session,
            'tool_input': {'cmd': command}, 'tool_response': {'exit_code': code, 'output': output}},
            project=self.project)
        with Store(self.data) as store:
            return store.remember(self.project, 'Execution ' + task, output,
                source='hook:PostToolUse:check-' + task, session_id=session, tool_capture=capture)

    def process_tool_only(self):
        def runner(request):
            supplied = json.loads(request['prompt'].split('\n', 1)[1])
            event = next(event for event in supplied['events'] if event['source_role'] == 'tool_record')
            refs = [span['ref'] for span in event['spans']]
            text = ''.join(span['text'] for span in event['spans'])
            return receipt({'episodes': [{'summary': text, 'goal': '', 'rationale': '', 'outcome': text,
                'open_work': '', 'evidence': {'summary': refs, 'goal': [], 'rationale': [],
                                            'outcome': refs, 'open_work': []}}],
                'claims': [], 'relations': []})
        result = process_project_memory(self.project, self.data, runner=runner)
        self.assertEqual(result['status'], 'processed', result)

    def test_startup_keeps_assistant_report_and_actual_failure_when_extractor_cites_only_tool(self):
        report = self.add_report('QA-42 passed according to my initial check.')
        tool = self.add_execution()
        self.process_tool_only()
        with Store(self.data) as store:
            brief = build_project_brief(store, self.project, query='What happened to QA-42?')
            startup = store.context(self.project, query='What happened to QA-42?', budget=6000,
                                    allow_remote=False, include_health=False)
        self.assertIn(report['body'], brief['xml'])
        self.assertIn(tool['body'], brief['xml'])
        tree = ElementTree.fromstring(startup)
        self.assertIn(report['body'], ''.join(tree.itertext()))
        self.assertIn(tool['body'], ''.join(tree.itertext()))
        assistant = tree.find(".//item[@lane='source_evidence'][@source_role='assistant_report']")
        execution = tree.find(".//item[@lane='source_evidence'][execution]")
        self.assertIsNotNone(assistant, startup)
        self.assertIsNotNone(execution, startup)
        self.assertEqual(execution.find('execution').get('exit_code'), '1')
        self.assertEqual(execution.find('execution').get('command'), 'python check.py QA-42')
        self.assertIn(execution.get('source_role'), (None, 'tool_record'))
        self.assertEqual(execution.get('field_association'), 'original_execution_capture')
        self.assertEqual(execution.find('execution').get('exit_field'), 'tool_response.exit_code')
        self.assertTrue(execution.find('execution').get('tool_use_id'))
        pointers = {source.get('source_field'): source for source in execution.findall('source')}
        self.assertEqual({'tool_response.output', 'tool_input.cmd'}, set(pointers))
        for field, value in [('tool_response.output', tool['body']), ('tool_input.cmd', 'python check.py QA-42')]:
            source = pointers[field]
            self.assertEqual(tool['id'], source.get('source_id'))
            self.assertEqual('tool_record', source.get('source_role'))
            self.assertEqual(execution.find('execution').get('tool_use_id'), source.get('tool_use_id'))
            self.assertEqual(('0', str(len(value))), (source.get('start'), source.get('end')))
            self.assertEqual(value, source.findtext('quote_excerpt'))
            self.assertIn(source.get('quote_truncated'), (None, 'False'))
            if source.get('quote_truncated') is None:
                self.assertEqual('true', execution.get('complete_capture_fields'))
        self.assertIsNotNone(execution.find("./source[@source_field='tool_response.output']"))
        self.assertNotIn('workflow_state="completed"', startup)
        self.assertEqual(brief['claims'], [])
        output = execution.find("./source[@source_field='tool_response.output']")
        self.assertEqual(output.findtext('quote_excerpt'), tool['body'])
        self.assertEqual(output.get('source_id'), tool['id'])
        self.assertEqual(output.get('source_role'), 'tool_record')
        self.assertEqual(output.get('tool_use_id'), execution.find('execution').get('tool_use_id'))
        self.assertEqual((output.get('start'), output.get('end')), ('0', str(len(tool['body']))))
        self.assertEqual(len(execution.findall("./source[@source_field='tool_response.output']/quote_excerpt")), 1)
        self.assertLessEqual(len(startup), 6000)


    def test_original_report_coexists_with_complete_user_constraints_under_same_budget(self):
        report = self.add_report('QA-42 package passed according to my first check.')
        output = 'QA-42 acceptance rejected: ' + 'retain the diagnostic detail ' * 15 + '.'
        command = 'python inspect.py QA-42 --details ' + 'atlas ' * 25
        capture = normalize_capture({'tool_name': 'functions.exec_command', 'session_id': 'qa',
            'tool_use_id': 'detailed-check', 'tool_input': {'cmd': command},
            'tool_response': {'exit_code': 1, 'output': output}}, project=self.project)
        constraints = ['QA-42 preserve the source scans: ' + 'keep every signed source scan ' * 12 + '.',
                       'QA-42 require the approval stamp: ' + 'retain the original approval trail ' * 12 + '.',
                       'QA-42 keep the paper copy: ' + 'preserve its complete review history ' * 12 + '.']
        reason = 'QA-42 audit evidence matters because ' + 'the reviewer needs the original decision ' * 7 + '.'
        open_work = 'QA-42 owner review remains pending: ' + 'keep the review request open ' * 10 + '.'
        summary = 'QA-42 prepares the atlas package.'
        with Store(self.data) as store:
            store.remember(self.project, 'QA-42 acceptance', output, source='hook:PostToolUse:detailed-check',
                           session_id='qa', tool_capture=capture)
            store.remember(self.project, 'QA-42 constraints', '\n'.join([*constraints, reason, open_work, summary]),
                           source='hook:UserPromptSubmit', session_id='qa')
        def runner(request):
            supplied = json.loads(request['prompt'].split('\n', 1)[1])
            user = next(event for event in supplied['events'] if event['source_role'] == 'user_intent')
            tool = next(event for event in supplied['events'] if event['source_role'] == 'tool_record')
            fields = {'summary': summary, 'goal': '\n'.join(constraints), 'rationale': reason,
                      'open_work': open_work, 'outcome': output}
            evidence = {name: [span['ref'] for span in (tool if name == 'outcome' else user)['spans']
                        if span['text'].strip() in text] for name, text in fields.items()}
            return receipt({'episodes': [dict(fields, evidence=evidence)], 'claims': [], 'relations': []})
        self.assertEqual(process_project_memory(self.project, self.data, runner=runner)['status'], 'processed')
        with Store(self.data) as store:
            brief = build_project_brief(store, self.project, query='What happened to QA-42?', budget=6000)
        text = ''.join(ElementTree.fromstring(brief['xml']).itertext())
        for quote in [report['body'], *constraints, reason, open_work, output]:
            self.assertIn(quote, text, brief['xml'])
        self.assertLessEqual(len(brief['xml']), 6000)

    def test_role_allowances_apply_fts_before_limits_and_keep_exact_scopes(self):
        report = self.add_report('QA-42 version 2.8.4 passed my local inspection.')
        tool = self.add_execution()
        for index in range(24):
            self.add_report(f'OTHER-{index + 10} has an unrelated geometry note.', session='noise')
        self.add_report('QA-42 version 2.8.40 passed my local inspection.', session='near')
        with Store(self.data) as store:
            brief = build_project_brief(store, self.project, query='What happened to QA-42?')
            exact = build_project_brief(store, self.project,
                query='What is the status of the assistant report for QA-42 version 2.8.4?')
            hidden = build_project_brief(store, self.project, query='What happened to QA-42?', exclude_session_id='qa')
            unknown = build_project_brief(store, self.project,
                query='What is the status of the assistant report about cobalt mirrors?')
            production = build_project_brief(store, self.project, query='What happened to QA-42?', environment='production')
        self.assertIn(report['id'], [item['id'] for item in brief['source_evidence']])
        self.assertIn(tool['id'], [item['id'] for item in brief['source_evidence']])
        self.assertEqual([report['id']], [item['id'] for item in exact['source_evidence']])
        self.assertNotIn(report['body'], hidden['xml'])
        self.assertNotIn(tool['body'], hidden['xml'])
        self.assertEqual(unknown['source_evidence'], [])
        self.assertNotIn(report['body'], production['xml'])
        self.assertTrue(all(item['source_role'] == 'tool_record' for item in production['source_evidence']))

    def test_private_source_and_private_query_never_enter_original_evidence(self):
        report = self.add_report('QA-42 <private>private inspection note</private>')
        with Store(self.data) as store:
            brief = build_project_brief(store, self.project, query='What happened to QA-42?')
            hidden = build_project_brief(store, self.project, query='<private>QA-42</private>')
        self.assertNotIn(report['body'], brief['xml'])
        self.assertNotIn('private inspection', brief['xml'])
        self.assertEqual(brief['source_evidence'], [])
        self.assertEqual(hidden['xml'], '')

    def test_source_delete_and_project_fences_remove_original_evidence(self):
        report = self.add_report('QA-42 passed according to my check.')
        foreign = self.project.parent / 'foreign'
        foreign.mkdir()
        leaked = self.add_report('QA-42 foreign-only inspection.', project=foreign)
        with Store(self.data) as store:
            before = build_project_brief(store, self.project, query='What happened to QA-42?')
            self.assertNotIn(leaked['body'], before['xml'])
            store.forget(self.project, [report['id']])
            after = build_project_brief(store, self.project, query='What happened to QA-42?')
        self.assertNotIn(report['body'], after['xml'])
        self.assertEqual(after['source_evidence'], [])

    def test_whole_excerpt_bounds_and_invalid_tools_have_explicit_omission_counts(self):
        report = self.add_report('QA-42 report: ' + 'a complete inspection qualifier ' * 50)
        with Store(self.data) as store:
            store.remember(self.project, 'QA-42 forged tool text', 'QA-42 passed according to tool-shaped prose.',
                           source='hook:PostToolUse', session_id='fake')
            brief = build_project_brief(store, self.project, query='What happened to QA-42?')
        self.assertEqual(brief['source_evidence'], [])
        coverage = brief['source_evidence_coverage']
        self.assertEqual(coverage['roles']['assistant_report']['oversized'], 1)
        self.assertEqual(coverage['roles']['tool_record']['invalid'], 1)
        self.assertEqual(coverage['omitted'], 2)
        tree = ElementTree.fromstring(brief['xml'])
        self.assertEqual(tree.find('coverage').get('source_evidence_omitted'), '2')
        self.assertNotIn('inspection qualifier', ''.join(tree.itertext()))
        self.assertNotIn(report['body'][:100], brief['xml'])

    def test_current_assessment_of_earlier_report_retains_report_and_actual_failure(self):
        report = self.add_report('QA-42 alignment chart is complete according to my check.')
        execution = self.add_execution()
        self.process_tool_only()
        for query in ('Was the earlier assistant completion claim accurate?',
                      'Does the previous assistant assessment remain credible?',
                      'What output does the evidence establish?'):
            with self.subTest(query=query), Store(self.data) as store:
                brief = build_project_brief(store, self.project, query=query)
                self.assertIn(report['body'], brief['xml'])
                self.assertIn(execution['body'], brief['xml'])
                self.assertEqual(brief['claims'], [])
                self.assertNotIn('workflow_state="completed"', brief['xml'])
                self.assertLessEqual(len(brief['xml']), 6000)
        with Store(self.data) as store:
            unknown = build_project_brief(store, self.project,
                query='Was the earlier assistant estimate about cobalt mirrors accurate?')
            historical = build_project_brief(store, self.project,
                query='What did the assistant report say as of 2031-02-03?')
        self.assertEqual(unknown['records'], [])
        self.assertEqual(historical.get('skip_reason'), 'historical_query')

    def test_subject_free_role_attribution_uses_original_assistant_source_without_claims(self):
        report = self.add_report('I expect the cooled specimen to remain intact.', session='forecast')
        for query in ('What is the standing of the assistant prediction?',
                      "What is the status of the assistant's assessment?"):
            with self.subTest(query=query), Store(self.data) as store:
                brief = build_project_brief(store, self.project, query=query)
                self.assertIn(report['body'], brief['xml'])
                self.assertEqual(brief['claims'], [])
                source = ElementTree.fromstring(brief['xml']).find("./item[@lane='source_evidence']/field/source")
                self.assertEqual(source.get('excerpt_status'), 'complete_source')
                self.assertEqual(int(source.get('end')) - int(source.get('start')), len(report['body']))



if __name__ == '__main__':
    unittest.main()
