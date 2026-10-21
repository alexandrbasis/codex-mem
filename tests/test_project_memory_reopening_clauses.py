"""Reopening authority through processing and reads from a real temporary Store."""
import json
from pathlib import Path
import tempfile
import unittest

from codex_mem.config import configure
from codex_mem.episode_processor import process_project_memory
from codex_mem.project_memory import ProjectMemory
from codex_mem.store import Store
from test_episode_processor import receipt


class ReopeningClauseTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.case = 0
        self.reset_case()

    def reset_case(self):
        self.case += 1
        self.project = Path(self.temp.name) / f'project-{self.case}'
        self.project.mkdir()
        self.data = Path(self.temp.name) / f'memory-{self.case}'
        configure(self.data, capture_scope='all', project_memory_mode='shadow',
                  project_memory_projects=[str(self.project)], jev_quality_enabled=False)

    def state(self):
        with Store(self.data) as store:
            return ProjectMemory(store).snapshot(self.project)

    def remember(self, body, session, source='hook:UserPromptSubmit'):
        with Store(self.data) as store:
            return store.remember(self.project, 'Task', body, source=source, session_id=session)

    def process(self, runner):
        return process_project_memory(self.project, self.data, runner=runner)

    def standalone(self, request):
        supplied = json.loads(request['prompt'].split('\n', 1)[1])
        spans = supplied['events'][0]['spans']
        refs = [span['ref'] for span in spans]
        item = {'summary': ' '.join(span['text'] for span in spans), 'goal': 'Task ORB-73',
                'rationale': '', 'outcome': '', 'open_work': '', 'continuation_of': None,
                'evidence': {'summary': refs, 'goal': refs[:1], 'rationale': [],
                             'outcome': [], 'open_work': [], 'continuation_of': []}}
        return receipt({'episodes': [item], 'claims': [], 'relations': []})

    def initial(self, request):
        response = self.standalone(request)
        spans = json.loads(request['prompt'].split('\n', 1)[1])['events'][0]['spans']
        response['output']['claims'] = [{'text': 'The audit export remains completed.', 'kind': 'task',
            'workflow_state': 'completed', 'rationale': '',
            'scope': {'environment': 'local', 'version': '', 'worktree': ''},
            'evidence': {'text': [spans[1]['ref']], 'workflow_state': [spans[1]['ref']], 'rationale': []}}]
        return response

    def consolidate(self, request):
        supplied = json.loads(request['prompt'].split('\n', 1)[1])
        self.assertEqual(supplied['job_kind'], 'reconsideration')
        heads = supplied['current_state']['episodes']
        refs = [span['ref'] for event in supplied['events'] for span in event['spans']]
        item = {'summary': 'Task ORB-73: reopen the renderer.', 'goal': 'Task ORB-73',
                'rationale': '', 'outcome': '', 'open_work': '',
                'continuation_of': heads[0]['id'], 'consolidation_of': [heads[1]['id']],
                'evidence': {'summary': refs, 'goal': refs[:1], 'rationale': [], 'outcome': [],
                             'open_work': [], 'continuation_of': refs, 'consolidation_of': refs}}
        return receipt({'episodes': [item], 'claims': [], 'relations': []})

    def split(self, sentence, source='hook:UserPromptSubmit'):
        self.remember('Task ORB-73: repair the renderer. The audit export remains completed.', 'one')
        self.assertEqual(self.process(self.initial)['status'], 'processed')
        claims = self.state()['claims']
        self.remember(sentence, 'two', source)
        self.assertEqual(self.process(self.standalone)['status'], 'processed')
        self.assertEqual(len(self.state()['episodes']), 2)
        return claims

    def assert_reconsidered(self, sentence):
        claims = self.split(sentence)
        result = self.process(self.consolidate)
        self.assertEqual(result['status'], 'processed', result)
        self.assertEqual(len(self.state()['episodes']), 1)
        self.assertEqual(self.state()['claims'], claims)
        self.assertEqual(self.process(lambda request: self.fail('replayed completed job'))['status'], 'idle')

    def continue_episode(self, sentence, source='hook:UserPromptSubmit', sliced_quote=None):
        self.remember('Task ORB-73: repair the renderer. The audit export remains completed.', 'one')
        self.assertEqual(self.process(self.initial)['status'], 'processed')
        before = self.state()
        self.remember(sentence, 'two', source)

        def continue_runner(request):
            supplied = json.loads(request['prompt'].split('\n', 1)[1])
            response = self.standalone(request)
            episode = response['output']['episodes'][0]
            episode['continuation_of'] = supplied['current_state']['episodes'][0]['id']
            ref = supplied['events'][0]['spans'][0]['ref']
            if sliced_quote:
                body = sentence
                start = body.index(sliced_quote)
                ref = {'source_id': supplied['events'][0]['id'], 'start': start,
                       'end': start + len(sliced_quote), 'quote': sliced_quote}
            episode['evidence']['continuation_of'] = [ref]
            return response

        result = self.process(continue_runner)
        self.assertEqual(result['status'], 'processed', result)
        self.assertEqual(self.state()['claims'], before['claims'])
        return before, self.state(), result

    def test_for_explanation_reopens_confirmed_task_after_store_restart(self):
        self.assert_reconsidered('Reopen ORB-73 for a showroom terminal trial.')

    def test_to_explanation_reopens_confirmed_task(self):
        self.assert_reconsidered('Reopen ORB-73 to fix the compact-price caption.')

    def test_because_explanation_reopens_confirmed_task(self):
        self.assert_reconsidered('Reopen ORB-73 because the currency glyph disappears.')

    def test_explanation_before_command_keeps_original_task_identity(self):
        self.assert_reconsidered('For a showroom terminal trial, reopen ORB-73.')

    def test_label_repair_is_a_purpose_rather_than_an_identity_reference(self):
        self.assert_reconsidered('Reopen ORB-73 to fix the compressed-price label.')

    def test_russian_explanation_reopens_confirmed_task(self):
        self.assert_reconsidered('Переоткрой задачу ORB-73 для проверки витринного терминала.')

    def test_russian_purpose_clause_reopens_confirmed_task(self):
        self.assert_reconsidered('Переоткрой ORB-73 чтобы исправить подпись цены.')

    def test_russian_reason_clause_reopens_confirmed_task(self):
        self.assert_reconsidered('Переоткрой ORB-73, потому что символ валюты исчезает.')

    def test_conditional_reopening_cannot_discover_a_task_through_generic_task_word(self):
        claims = self.split('Reopen task ORB-73 for a terminal trial only after approval.')
        result = self.process(self.consolidate)
        self.assertEqual(result['status'], 'idle', result)
        self.assertEqual(len(self.state()['episodes']), 2)
        self.assertEqual(self.state()['claims'], claims)

    def test_russian_explanation_before_command_keeps_task_identity(self):
        self.assert_reconsidered('Для проверки витринного терминала, повторно открой ORB-73.')

    def test_failed_check_in_reason_does_not_negate_the_reopening_command(self):
        self.assert_reconsidered('Reopen ORB-73 because the renderer check failed.')

    def test_quoted_reopening_cannot_disable_reference_check_for_another_sentence(self):
        sentence = ('CLI help: `Reopen ORB-73 for a terminal trial`. '
                    'The same ORB-73 task label identifies archived text.')
        _, after, _ = self.continue_episode(sentence, sliced_quote=sentence)
        self.assertEqual(len(after['episodes']), 2)

    def test_authorization_tail_cannot_qualify_an_explanatory_clause(self):
        _, after, _ = self.continue_episode(
            'Reopen ORB-73 for a terminal trial subject to authorization.')
        self.assertEqual(len(after['episodes']), 2)

    def test_permission_qualifiers_block_both_discovery_and_continuation(self):
        for sentence in (
            'Переоткрой задачу ORB-73 для исправления с разрешения руководителя.',
            'Для исправления с разрешения руководителя, переоткрой задачу ORB-73.',
            'Reopen task ORB-73 for the repair with permission from the supervisor.',
            'For the repair with permission from the supervisor, reopen task ORB-73.',
        ):
            with self.subTest(sentence=sentence):
                self.reset_case()
                _, after, _ = self.continue_episode(sentence)
                self.assertEqual(len(after['episodes']), 2)
                self.reset_case()
                self.split(sentence)
                result = self.process(lambda request: self.fail('conditional identity was discovered'))
                self.assertEqual(result['status'], 'idle', result)
                self.assertEqual(len(self.state()['episodes']), 2)

    def test_soft_line_wrap_cannot_hide_a_reopening_condition(self):
        quote = 'Reopen ORB-73 for a terminal trial'
        sentence = quote + '\nonly after approval.'
        _, after, _ = self.continue_episode(sentence, sliced_quote=quote)
        self.assertEqual(len(after['episodes']), 2)
        self.reset_case()
        self.split(sentence)
        result = self.process(lambda request: self.fail('wrapped condition was omitted'))
        self.assertEqual(result['status'], 'idle', result)

    def test_complete_reopening_sentence_keeps_separate_later_sentence_scope(self):
        self.assert_reconsidered(
            'Reopen ORB-73 for a terminal trial.\nDeployment still needs approval.')

    def test_permission_and_consent_variants_cannot_qualify_reopening(self):
        for sentence in (
            'Reopen ORB-73 for a terminal trial whenever permitted.',
            'Reopen ORB-73 for a terminal trial with the required permit.',
            'Reopen ORB-73 for a terminal trial when authorised.',
            'Reopen ORB-73 for a terminal trial with authorisation.',
            'Переоткрой ORB-73 для проверки с согласия руководителя.',
            'Для проверки с согласия руководителя, переоткрой ORB-73.',
        ):
            with self.subTest(sentence=sentence):
                self.reset_case()
                _, after, _ = self.continue_episode(sentence)
                self.assertEqual(len(after['episodes']), 2)
                self.reset_case()
                self.split(sentence)
                result = self.process(lambda request: self.fail('qualified identity was discovered'))
                self.assertEqual(result['status'], 'idle', result)

    def test_user_prompt_capture_header_preserves_reopening_and_its_qualifiers(self):
        quote = 'Reopen ORB-73 for a showroom terminal trial'
        sentence = '[User prompt]\n' + quote + '.'
        _, after, _ = self.continue_episode(sentence, sliced_quote=quote)
        self.assertEqual(len(after['episodes']), 1)
        self.reset_case()
        self.assert_reconsidered(sentence)
        self.reset_case()
        qualified = '[User prompt]\n' + quote + '\nonly after approval.'
        _, after, _ = self.continue_episode(qualified, sliced_quote=quote)
        self.assertEqual(len(after['episodes']), 2)

    def test_ordinary_continuation_accepts_the_same_bounded_clause_forms(self):
        for sentence in (
            'Reopen ORB-73.',
            'Reopen ORB-73 for a showroom terminal trial.',
            'Please reopen the same ticket ORB-73 to fix the compressed-price label.',
            'Reopen ORB-73 because the currency glyph disappears.',
            'For a showroom terminal trial, reopen ORB-73.',
            'To repair the renderer, please reopen ORB-73.',
            'Because the currency glyph disappears, reopen ORB-73.',
            'Переоткрой задачу ORB-73 для проверки витринного терминала.',
            'Переоткрой ORB-73, чтобы исправить подпись цены.',
            'Переоткрой ORB-73, потому что символ валюты исчезает.',
            'Для проверки витринного терминала, повторно открой ORB-73.',
        ):
            with self.subTest(sentence=sentence):
                self.reset_case()
                before, after, _ = self.continue_episode(sentence)
                self.assertEqual(len(after['episodes']), 1)
                self.assertEqual(after['episodes'][0]['parent_episode_id'], before['episodes'][0]['id'])
                self.assertEqual(after['episodes'][0]['episode_version'], 2)
                self.assertEqual(self.process(lambda request: self.fail('replayed continuation'))['status'], 'idle')

    def test_nonaffirmative_or_independent_commands_cannot_continue(self):
        rejected = (
            'Do not reopen ORB-73 for a terminal trial.',
            'Reopen ORB-73 for a terminal trial?',
            'Should we reopen ORB-73 to fix the caption?',
            'Reopen ORB-73 to fix the caption only after approval.',
            'Reopen ORB-73 for a terminal trial if approved.',
            'Reopen ORB-73 because the caption disappears unless approval arrives.',
            'Reopen ORB-73 to fix the caption pending approval.',
            'Reopen ORB-73 because the command is forbidden.',
            'Reopen ORB-73 for a separate task.',
            'A separate task: reopen ORB-73 for a terminal trial.',
            'Reopen NOVA-48 for a terminal trial.',
            'Reopen ORB-73 and NOVA-48 for a terminal trial.',
            'Reopen ORB-73 to fix the caption for NOVA-48.',
            '"Reopen ORB-73 for a terminal trial."',
            'CLI help: `Reopen ORB-73 for a terminal trial.`',
            '```\nReopen ORB-73 to fix the caption.\n```',
            'Reopen ORB-73 for a command example.',
            'Reopen ORB-73 to print an instruction.',
            'For a terminal trial after approval, reopen ORB-73.',
            'To fix the caption if approved, reopen ORB-73.',
            'Reopen ORB-73 for one two three four five six seven eight nine ten eleven twelve thirteen.',
            'Не переоткрой ORB-73 для проверки терминала.',
            'Переоткрой ORB-73 для проверки терминала?',
            'Переоткрой ORB-73, чтобы исправить подпись только после согласования.',
            'Переоткрой ORB-73, потому что подпись исчезает если разрешат.',
            'Для отдельной задачи, переоткрой ORB-73.',
            '«Переоткрой ORB-73 для проверки терминала.»',
        )
        for sentence in rejected:
            with self.subTest(sentence=sentence):
                self.reset_case()
                before, after, _ = self.continue_episode(sentence)
                self.assertEqual(len(after['episodes']), 2)
                self.assertTrue(all(item['parent_episode_id'] is None for item in after['episodes']))
                self.assertIn(before['episodes'][0]['id'], [item['id'] for item in after['episodes']])

    def test_assistant_and_tool_records_cannot_authorize_reopening(self):
        for source in ('hook:Stop', 'hook:PostToolUse'):
            with self.subTest(source=source):
                self.reset_case()
                _, after, _ = self.continue_episode('Reopen ORB-73 for the same task repair.', source)
                self.assertEqual(len(after['episodes']), 2)

    def test_sliced_evidence_cannot_hide_reopening_disqualifiers(self):
        quote = 'Reopen ORB-73 to fix the caption'
        for sentence in (
            'Do not ' + quote + '.',
            quote + ' only after approval.',
            quote + ' if approved.',
            'CLI help: `' + quote + '.`',
            '"' + quote + '."',
            '```\n' + quote + '.\n```',
        ):
            with self.subTest(sentence=sentence):
                self.reset_case()
                _, after, _ = self.continue_episode(sentence, sliced_quote=quote)
                self.assertEqual(len(after['episodes']), 2)
