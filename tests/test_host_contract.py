"""Shared host behavior through capture and a real temporary store."""
import tempfile
import unittest
from pathlib import Path

from codex_mem.config import configure
from codex_mem.host import HostEvent, capture_event
from codex_mem.store import Store


class HostContractTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.project = self.root / 'project'
        self.project.mkdir()
        self.data = self.root / 'memory'
        configure(self.data, capture_scope='all', service_enabled=False,
                  semantic_enabled=False)
        self.store = Store(self.data)
        self.addCleanup(self.store.close)

    def event(self, **changes):
        values = dict(host='pi', project=str(self.project), session_id='same-session',
                      event='user_message', role='user_intent', event_id='event-1',
                      message_id='message-1', body='Use local import.')
        values.update(changes)
        return HostEvent(**values)

    def test_redelivery_is_safe_but_equal_text_and_host_ids_are_distinct(self):
        first = capture_event(self.event(), self.store)
        duplicate = capture_event(self.event(), self.store)
        second = capture_event(self.event(event_id='event-2', message_id='message-2'), self.store)
        codex = capture_event(self.event(host='codex'), self.store)
        self.assertEqual(first['id'], duplicate['id'])
        self.assertEqual(3, len({first['id'], second['id'], codex['id']}))
        records = self.store.get(self.project, [first['id'], second['id'], codex['id']])
        self.assertEqual(3, len(records))
        self.assertEqual(2, len({row['session_id'] for row in records}))

    def test_private_turn_is_isolated_by_host_and_public_prompt_clears_it(self):
        private = capture_event(self.event(body='<private>hidden task</private>'), self.store)
        tool = self.event(event='tool_result', role='tool_record', event_id='tool-event',
                          tool_id='tool-1', body='hidden result')
        blocked = capture_event(tool, self.store)
        self.assertEqual('omitted', private['status'])
        self.assertEqual('omitted', blocked['status'])
        self.assertEqual('captured', capture_event(self.event(host='codex'), self.store)['status'])
        capture_event(self.event(event_id='public', body='Use public import.'), self.store)
        self.assertEqual('captured', capture_event(tool, self.store)['status'])
        self.assertEqual([], self.store.search(self.project, 'hidden task'))
        explicit = capture_event(self.event(private=True, event_id='explicit-private', body='Hidden without a marker.'), self.store)
        self.assertEqual('omitted', explicit['status'])
        self.assertNotIn('Hidden without a marker.', str(self.store.timeline(self.project)))

    def test_context_contract_applies_scope_budget_and_reports_omission(self):
        from codex_mem.host import ContextRequest, retrieve_context
        self.store.remember(self.project, 'Import decision', 'Use local import.', kind='decision')
        request = ContextRequest(host='pi', project=str(self.project), session_id='reader', budget=2000)
        result = retrieve_context(request, self.store)
        self.assertIn('Use local import.', result.text)
        self.assertTrue(result.untrusted)
        self.assertLessEqual(len(result.text), 2000)
        tiny = retrieve_context(ContextRequest(host='pi', project=str(self.project), budget=10), self.store)
        self.assertEqual('budget_too_small', tiny.reason)
        self.assertEqual('', tiny.text)
        configure(self.data, capture_scope='manual')
        blocked = capture_event(self.event(), self.store)
        self.assertEqual('capture_disabled', blocked['reason'])
        self.assertEqual('capture_disabled', retrieve_context(request, self.store).reason)
        # Manual policy still permits an explicit memory write.
        written = self.store.remember(self.project, 'Explicit note', 'Explicit manual memory.')
        self.assertEqual('Explicit manual memory.', self.store.get(self.project, [written['id']])[0]['body'])

    def test_codex_message_identity_does_not_collapse_equal_messages_in_one_turn(self):
        from codex_mem.hooks import handle_hook
        payload = dict(hook_event_name='UserPromptSubmit', cwd=str(self.project), session_id='session',
                       turn_id='turn', prompt='Use local import.')
        handle_hook(dict(payload, message_id='one'), self.store)
        handle_hook(dict(payload, message_id='two'), self.store)
        handle_hook(dict(payload, message_id='two'), self.store)
        self.assertEqual(2, len(self.store.timeline(self.project, session_id='session')))

    def test_branch_tool_evidence_keeps_ids_and_private_data_out_of_readback(self):
        from codex_mem.tool_io import normalize_capture
        tool = normalize_capture(dict(tool_name='Bash', session_id='same-session', tool_use_id='tool-1',
            tool_input={'command': 'pytest'}, tool_response={'output': 'passed OPENAI_API_KEY=fictional-secret'}),
            project=str(self.project))
        result = capture_event(self.event(event='tool_result', role='tool_record', branch='feature/a',
            event_id='tool-event', tool_id='tool-1', tool_capture=tool, body='Tests passed.'), self.store)
        entry = self.store.get(self.project, [result['id']])[0]
        self.assertIn('feature/a', entry['source'])
        self.assertIn('tool-event', entry['source'])
        self.assertNotIn('fictional-secret', str(entry))
        raw = self.store.get_source_capture(self.project, result['id'])
        self.assertNotIn('fictional-secret', str(raw))
        self.assertEqual(entry['session_id'], raw['session_id'])
        other = capture_event(self.event(event='tool_result', role='tool_record', branch='feature-b',
            event_id='tool-event', tool_id='tool-1', body='Tests passed.'), self.store)
        self.assertNotEqual(result['id'], other['id'])

    def test_legacy_codex_redelivery_after_reopen_keeps_the_existing_source(self):
        legacy = self.store.remember(self.project, 'Assistant final answer',
            '[Assistant final answer]\nUse local import.', kind='session', source='hook:Stop',
            session_id='legacy-session', turn_id='legacy-turn',
            dedupe_key='hook:final:cf17c610d860bf85af24d61f98ffb10c3d9fb0c324559c2b1daa615de7065616')
        self.store.close()
        self.store = Store(self.data)
        self.addCleanup(self.store.close)
        result = capture_event(self.event(host='codex', event='assistant_message', role='assistant_report',
            event_id=None, message_id=None, session_id='legacy-session', turn_id='legacy-turn',
            body='[Assistant final answer]\nUse local import.'), self.store)
        self.assertEqual(legacy['id'], result['id'])
        self.assertEqual(1, len(self.store.timeline(self.project)))
        claimed = self.store.claim_observation_batch(self.project, 'test-observer', 'gpt-6-luna', 'medium')
        self.assertEqual(legacy['id'], claimed['sources'][0]['id'])
        self.assertEqual('assistant_report', self.store.get(self.project, [legacy['id']])[0]['provenance']['record_role'])
        distinct = self.event(host='codex', event='assistant_message', role='assistant_report',
            event_id=None, message_id='new-message', session_id='legacy-session', turn_id='legacy-turn',
            body='[Assistant final answer]\nUse local import.')
        new = capture_event(distinct, self.store)
        self.assertNotEqual(legacy['id'], new['id'])
        self.assertEqual(new['id'], capture_event(distinct, self.store)['id'])
        foreign = capture_event(self.event(host='pi', event='assistant_message', role='assistant_report',
            event_id=None, message_id=None, session_id='legacy-session', turn_id='legacy-turn',
            body='[Assistant final answer]\nUse local import.'), self.store)
        self.assertNotEqual(legacy['id'], foreign['id'])

    def test_legacy_native_redelivery_strips_injected_memory_before_fingerprinting(self):
        from codex_mem.hooks import handle_hook
        # This retained key was produced by the pre-contract Codex hook.
        legacy = self.store.remember(self.project, 'Assistant final answer',
            '[Assistant final answer]\nBefore\n\n\nAfter', kind='session', source='hook:Stop',
            session_id='legacy-injected-session', turn_id='legacy-injected-turn',
            dedupe_key='hook:final:3d3451065e641174b3734f74e5a39903559608e71870b6fe5c3d3bf828ffe580')
        self.store.close()
        self.store = Store(self.data)
        self.addCleanup(self.store.close)
        payload = dict(hook_event_name='Stop', cwd=str(self.project),
            session_id='legacy-injected-session', turn_id='legacy-injected-turn',
            last_assistant_message='Before\n<codex_mem_untrusted_context>Old memory</codex_mem_untrusted_context>\nAfter')
        handle_hook(payload, self.store)
        handle_hook(payload, self.store)
        records = self.store.timeline(self.project)
        self.assertEqual([legacy['id']], [row['id'] for row in records])
        self.assertNotIn('Old memory', str(self.store.get(self.project, [legacy['id']])))

    def test_codex_branch_start_excludes_its_own_saved_context(self):
        from codex_mem.hooks import handle_hook
        captured = capture_event(self.event(host='codex', branch='feature/a'), self.store)
        session = self.store.get(self.project, [captured['id']])[0]['session_id']
        self.store.remember(self.project, 'Current branch', 'CURRENT_BRANCH_ONLY',
                            kind='decision', session_id=session)
        self.store.remember(self.project, 'Other session', 'PRIOR_PROJECT_DECISION', kind='decision')
        response = handle_hook(dict(hook_event_name='SessionStart', source='startup', cwd=str(self.project),
            session_id='same-session', branch='feature/a'), self.store)
        self.assertNotIn('CURRENT_BRANCH_ONLY', str(response))
        self.assertIn('PRIOR_PROJECT_DECISION', str(response))

    def test_injected_context_and_capture_overflow_have_explicit_receipts(self):
        injected = capture_event(self.event(body='<codex_mem_untrusted_context>old evidence</codex_mem_untrusted_context>'), self.store)
        self.assertEqual('empty_or_injected', injected['reason'])
        self.assertEqual([], self.store.timeline(self.project))
        overflow = capture_event(self.event(body='x' * 6100), self.store)
        self.assertEqual(100, overflow['omitted_chars'])
        self.assertEqual(6000, len(self.store.get(self.project, [overflow['id']])[0]['body']))
        from codex_mem.host import ContextRequest, retrieve_context
        from codex_mem.hooks import handle_hook
        self.store.close()
        self.assertEqual('context_unavailable', retrieve_context(ContextRequest(host='pi',
            project=str(self.project)), self.store).reason)
        response = handle_hook(dict(hook_event_name='SessionStart', source='startup',
            cwd=str(self.project), session_id='reader'), self.store)
        self.assertTrue(response['continue'])
        self.assertIn('unknown', response['hookSpecificOutput']['additionalContext'])

    def test_tool_calls_in_one_message_and_unidentified_messages_stay_distinct(self):
        first = capture_event(self.event(event='tool_result', role='tool_record', event_id=None,
            tool_id='first-tool'), self.store)
        second = capture_event(self.event(event='tool_result', role='tool_record', event_id=None,
            tool_id='second-tool'), self.store)
        self.assertNotEqual(first['id'], second['id'])
        anonymous = self.event(event_id=None, message_id=None, turn_id='one-turn')
        self.assertNotEqual(capture_event(anonymous, self.store)['id'],
                            capture_event(anonymous, self.store)['id'])


class HostExecutionTests(unittest.TestCase):
    from test_execution_contract import ExecutionContractTests as _Fixture
    scenario = _Fixture.scenario

    def setUp(self):
        self._Fixture.setUp(self)
        self.data = self.root / 'memory'
        self.project = self.root / 'project'
        self.project.mkdir()
        configure(self.data, capture_scope='all', service_enabled=False, semantic_enabled=False,
                  project_memory_mode='pilot', project_memory_projects=[str(self.project)])

    def test_shared_assistant_event_produces_summary_and_next_request_context(self):
        from codex_mem.processor import process_pending
        from codex_mem.hooks import handle_hook
        with Store(self.data) as store:
            captured = capture_event(HostEvent(host='pi', project=str(self.project), session_id='session',
                event='assistant_message', role='assistant_report', event_id='final-1',
                body='Use local import.'), store)
            self.assertEqual('captured', captured['status'])
        summary = dict(title='Import choice', request=None, investigated='', learned='Use local import.',
                       completed='', next_steps='', notes='', source_ids=['s1'])
        note = dict(title='Import choice', body='Use local import.', tags=[], source_ids=['s1'],
                    observation=dict(type='discovery', subtitle='', narrative='Use local import.', facts=[],
                                     concepts=[], files_read=[], files_modified=[]))
        self.scenario(output={'result': dict(notes=[note], disposition='processed', session_summary=summary)})
        result = process_pending(self.project, self.data, codex=str(self.executable), timeout=10)
        self.assertEqual('processed', result['status'], result)
        self.assertEqual(1, result['session_summary_count'])
        with Store(self.data) as store:
            response = handle_hook(dict(hook_event_name='SessionStart', source='startup',
                cwd=str(self.project), session_id='reader'), store)
            self.assertIn('Use local import.', str(response))
            self.assertIn('assistant_report', str(store.get(self.project, [captured['id']])) )
            self.assertEqual([], store.search(self.root / 'other', 'Import'))

    def test_shared_user_event_keeps_project_memory_role_in_next_host_context(self):
        from unittest import mock
        from codex_mem.episode_processor import process_project_memory
        from codex_mem.project_memory import ProjectMemory
        from codex_mem.host import ContextRequest, retrieve_context
        with Store(self.data) as store:
            capture_event(HostEvent(host='pi', project=str(self.project), session_id='session',
                event='user_message', role='user_intent', event_id='request-1',
                body='Use local import.'), store)
        self.scenario(mode='project_memory')
        with mock.patch('codex_mem.processor.shutil.which', return_value=str(self.executable)):
            result = process_project_memory(self.project, self.data, timeout=10)
        self.assertEqual('processed', result['status'], result)
        with Store(self.data) as store:
            state = ProjectMemory(store).snapshot(self.project)
            evidence = state['episodes'][0]['evidence']['summary'][0]
            self.assertEqual('user_intent', evidence['source_role'])
            self.assertTrue(evidence['source_id'])
            context = retrieve_context(ContextRequest(host='codex', project=str(self.project),
                session_id='reader'), store)
            self.assertIn('Use local import.', context.text)
            self.assertIn(evidence['source_id'], context.text)
            self.assertTrue(context.untrusted)
