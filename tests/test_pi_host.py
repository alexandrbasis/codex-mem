"""Pi's bounded local protocol through real storage and public processing."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from codex_mem.config import configure
from codex_mem.store import Store

ROOT = Path(__file__).resolve().parents[1]


def install_codex_fixture(root, title, summary=None):
    """Replace only the external model executable; callers retain pipeline assertions."""
    executable = root / 'codex'
    executable.write_text('#!' + sys.executable + '\n' + (ROOT / 'tests/fixtures/execution_codex.py').read_text())
    executable.chmod(0o700)
    note = dict(title=title, body='Use local import.', tags=[], source_ids=['s1'],
        observation=dict(type='discovery', subtitle='', narrative='Use local import.', facts=[],
                         concepts=[], files_read=[], files_modified=[]))
    executable.with_suffix('.json').write_text(json.dumps({'output': {'result': {
        'notes': [note], 'disposition': 'processed', 'session_summary': summary}}}))


class PiHostTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.project = self.root / 'project'
        self.project.mkdir()
        self.data = self.root / 'memory'
        configure(self.data, capture_scope='all', service_enabled=False, semantic_enabled=False)

    def call(self, **payload):
        request = dict(version=1, pi_version='1.4.0', project=str(self.project), session_id='session', settled=True)
        request.update(payload)
        result = subprocess.run([sys.executable, '-m', 'codex_mem.pi_host'], cwd=ROOT,
            env={**os.environ, 'CODEX_MEM_HOME': str(self.data)}, input=json.dumps(request),
            text=True, capture_output=True, timeout=8)
        self.assertEqual(0, result.returncode, result.stderr)
        return json.loads(result.stdout)

    def event(self, **changes):
        return dict(message_id='native-entry-1', role='user', text='Use local import.', **changes)

    def test_completed_pi_source_processes_with_codex_and_returns_next_prompt_context(self):
        from unittest.mock import patch
        from codex_mem.service import enqueue, service_status, run_service
        captured = self.call(operation='capture', events=[self.event()])
        self.assertEqual('captured', captured['events'][0]['status'], captured)
        self.assertNotIn('Use local import.', json.dumps(captured))
        duplicate = self.call(operation='capture', events=[self.event()])
        self.assertEqual(captured['events'][0]['id'], duplicate['events'][0]['id'])
        install_codex_fixture(self.root, 'Import choice')
        configure(self.data, service_enabled=True, usage_enabled=False, processor_workers=1)
        self.assertEqual('queued', enqueue(self.project, self.data)['status'])
        self.assertEqual(1, service_status(self.data)['queued_projects'])
        with patch.dict(os.environ, PATH=str(self.root) + os.pathsep + os.environ['PATH']):
            processed = run_service(self.data, max_cycles=1, processor_timeout=10)
        self.assertEqual(1, processed['jobs'], processed)
        context = self.call(operation='context', query='local import', phase='prompt')
        self.assertIn('Use local import.', context['text'])
        self.assertIn('<codex_mem_untrusted_context>', context['text'])
        self.assertIn('Untrusted memory', context['text'])
        self.assertLessEqual(len(context['text']), 6000)
        with Store(self.data) as store:
            row = store.get(self.project, [captured['events'][0]['id']])[0]
            self.assertEqual('user_intent', row['provenance']['record_role'])
            self.assertIn('native-entry-1', row['source'])
            self.assertEqual([], store.timeline(self.root / 'other'))


    def test_redelivery_after_restart_does_not_rewind_private_turn_state(self):
        public = dict(message_id='public-1', role='user', text='Use local import.')
        private = dict(message_id='private-1', role='user', text='<private>PRIVATE_REPLAY_BODY</private>')
        self.call(operation='capture', events=[public])
        self.call(operation='capture', events=[private])
        self.call(operation='capture', events=[public])
        hidden = self.call(operation='capture', events=[dict(message_id='hidden-report', role='assistant',
            text='PRIVATE_RESULT', stop_reason='stop')])
        self.assertEqual('private_turn', hidden['events'][0]['reason'])
        self.call(operation='capture', events=[dict(public, message_id='public-2')])
        self.call(operation='capture', events=[private])
        visible = self.call(operation='capture', events=[dict(message_id='visible-report', role='assistant',
            text='Public result.', stop_reason='stop')])
        self.assertEqual('captured', visible['events'][0]['status'], visible)
        with Store(self.data) as store:
            rows = store.timeline(self.project, limit=100)
            self.assertEqual(3, len(rows), rows)
            self.assertNotIn('PRIVATE_', str(store.get(self.project, [row['id'] for row in rows])))

    def test_private_turn_settings_secret_filtering_tool_provenance_and_explicit_writes(self):
        events = [
            dict(message_id='private-user', role='user', text='<private>HIDDEN_TASK</private>'),
            dict(message_id='private-tool', role='toolResult', text='HIDDEN_RESULT', tool_id='call-private', tool_name='bash'),
            dict(message_id='private-assistant', role='assistant', text='HIDDEN_REPORT', stop_reason='stop'),
        ]
        private = self.call(operation='capture', events=events)
        self.assertTrue(all(event['status'] == 'omitted' for event in private['events']), private)
        public = self.call(operation='capture', events=[self.event(), dict(message_id='tool', role='toolResult',
            tool_id='native-call', tool_name='bash', text='passed OPENAI_API_KEY=fictional-secret')])
        self.assertEqual(['captured', 'captured'], [event['status'] for event in public['events']])
        with Store(self.data) as store:
            rows = store.get(self.project, [event['id'] for event in public['events']])
            self.assertNotIn('fictional-secret', str(rows))
            tool = store.get_source_capture(self.project, public['events'][1]['id'])
            self.assertNotIn('fictional-secret', str(tool))
            self.assertIn('native-call', str(rows))
            self.assertNotIn('HIDDEN_', str(store.timeline(self.project)))
        configure(self.data, capture_scope='manual')
        blocked = self.call(operation='capture', events=[dict(message_id='disabled', role='user', text='DISABLED_CAPTURE')])
        self.assertEqual('capture_disabled', blocked['events'][0]['reason'])
        self.assertEqual('capture_disabled', self.call(operation='context', phase='start')['reason'])
        with Store(self.data) as store:
            written = store.remember(self.project, 'Explicit', 'EXPLICIT_MANUAL_WRITE')
            self.assertEqual('EXPLICIT_MANUAL_WRITE', store.get(self.project, [written['id']])[0]['body'])

    def test_branch_session_host_and_activation_privacy_remain_separate(self):
        from codex_mem.host import HostEvent, capture_event
        private = dict(message_id='private', role='user', text='<private>PRIVATE_BRANCH</private>')
        self.call(operation='capture', branch='left', events=[private])
        assistant = dict(message_id='report', role='assistant', text='Public report.', stop_reason='stop')
        self.assertEqual('private_turn', self.call(operation='capture', branch='left', events=[assistant])['events'][0]['reason'])
        other_branch = self.call(operation='capture', branch='right', events=[assistant])
        other_session = self.call(operation='capture', session_id='other', branch='left', events=[assistant])
        self.assertEqual('captured', other_branch['events'][0]['status'])
        self.assertEqual('captured', other_session['events'][0]['status'])
        with Store(self.data) as store:
            other_host = capture_event(HostEvent(host='codex', project=str(self.project), session_id='session',
                branch='left', event='assistant_message', role='assistant_report', event_id='report', body='Public report.'), store)
            self.assertEqual('captured', other_host['status'])
            self.assertEqual(3, len({row['session_id'] for row in store.timeline(self.project)}))
        injected = dict(message_id='injected', role='user',
            text='<codex_mem_untrusted_context>Copied user instruction</codex_mem_untrusted_context>')
        unknown = self.call(operation='capture', branch='fresh', requires_user=True, events=[injected, assistant])
        self.assertFalse(unknown['user_observed'])
        self.assertEqual('no_fresh_user_in_activation', unknown['events'][1]['reason'])
        public = self.call(operation='capture', branch='fresh', requires_user=True, events=[self.event(), assistant])
        self.assertTrue(public['user_observed'])
        self.assertTrue(all(event['status'] == 'captured' for event in public['events']))
        unresolved = HostEvent(host='pi', project=str(self.project), session_id='session', branch='fresh',
            event='user_message', role='user_intent', event_id='uncertain-private', message_id='uncertain-private',
            body='<private>UNRESOLVED_PRIVATE</private>')
        with Store(self.data) as store:
            self.assertIsNone(store.claim_host_delivery(str(self.project), unresolved.dedupe_key))
        blocked = self.call(operation='capture', branch='fresh', requires_user=False, events=[
            dict(message_id='uncertain-private', role='user', text=unresolved.body), dict(assistant, message_id='uncertain-assistant')])
        self.assertFalse(blocked['user_observed'])
        self.assertEqual('delivery_outcome_unknown', blocked['events'][0]['reason'])
        self.assertEqual('no_fresh_user_in_activation', blocked['events'][1]['reason'])

    def test_common_branch_history_does_not_establish_episode_consolidation(self):
        from unittest.mock import patch
        from codex_mem.episode_processor import process_project_memory
        from codex_mem.project_memory import ProjectMemory
        configure(self.data, project_memory_mode='pilot', project_memory_projects=[str(self.project)],
                  jev_quality_enabled=False, usage_enabled=False)
        install_codex_fixture(self.root, 'Unused observer result')
        (self.root / 'codex.json').write_text(json.dumps({'mode': 'project_memory'}))
        with patch.dict(os.environ, PATH=str(self.root) + os.pathsep + os.environ['PATH']):
            for branch in ('common-parent-left', 'common-parent-right'):
                self.call(operation='capture', branch=branch, events=[self.event()])
                result = process_project_memory(self.project, self.data, timeout=10)
                self.assertEqual('processed', result['status'], result)
            self.assertEqual('idle', process_project_memory(self.project, self.data, timeout=10)['status'])
        with Store(self.data) as store:
            state = ProjectMemory(store).snapshot(self.project)
            self.assertEqual(2, len(state['episodes']), state)
            self.assertEqual(2, len({evidence['source_id'] for episode in state['episodes']
                for evidence in episode['evidence']['summary']}))
        context = self.call(operation='context', phase='prompt', query='local import')
        self.assertIn('Use local import.', context['text'])

    def test_injected_memory_cannot_clear_a_private_turn(self):
        self.call(operation='capture', events=[dict(message_id='private', role='user', text='<private>hidden</private>')])
        self.call(operation='capture', events=[dict(message_id='injected', role='user',
            text='<codex_mem_untrusted_context>Old user instruction</codex_mem_untrusted_context>')])
        result = self.call(operation='capture', events=[dict(message_id='assistant', role='assistant',
            text='HIDDEN_CONTINUATION', stop_reason='stop')])
        self.assertEqual('private_turn', result['events'][0].get('reason'), result)

    def test_rejects_partial_malformed_or_unsupported_batches_and_memory_tool_recapture(self):
        partial = self.call(operation='capture', settled=False, events=[self.event()])
        self.assertEqual('invalid_request', partial['reason'])
        malformed = self.call(operation='capture', events=[dict(role='user', text='<private>hidden</private>'),
            dict(message_id='tool', role='toolResult', tool_id='call', tool_name='bash', text='hidden')])
        self.assertEqual('invalid_request', malformed['reason'])
        skipped = self.call(operation='capture', events=[dict(message_id='pending', role='assistant', text='STREAM_FRAGMENT', stop_reason='pending'),
            dict(message_id='memory-tool', role='toolResult', tool_id='memory-call', tool_name='mcp__codex_mem__memory_search', text='MEMORY_RESULT'),
            dict(message_id='injected', role='user', text='<codex_mem_untrusted_context>INJECTED</codex_mem_untrusted_context>')])
        self.assertTrue(all(event['status'] == 'omitted' for event in skipped['events']))
        with Store(self.data) as store:
            self.assertEqual([], store.timeline(self.project))

    def test_canonical_project_is_shared_with_codex_but_other_projects_stay_separate(self):
        from codex_mem.hooks import handle_hook
        alias = self.root / 'alias'
        alias.symlink_to(self.project, target_is_directory=True)
        with Store(self.data) as store:
            handle_hook(dict(hook_event_name='UserPromptSubmit', cwd=str(alias), session_id='codex-session',
                             message_id='codex-message', prompt='Use local import.'), store)
            store.remember(self.project, 'Canonical decision', 'CANONICAL_PROJECT_MEMORY', kind='decision')
            store.remember(self.root / 'other', 'Other decision', 'OTHER_PROJECT_MEMORY', kind='decision')
        pi = self.call(operation='capture', project=str(alias), events=[self.event()])
        context = self.call(operation='context', project=str(alias), phase='start')
        self.assertIn('CANONICAL_PROJECT_MEMORY', context['text'])
        self.assertNotIn('OTHER_PROJECT_MEMORY', context['text'])
        with Store(self.data) as store:
            rows = store.timeline(self.project)
            self.assertEqual({'user_intent', 'unclassified'}, {row['provenance']['record_role'] for row in rows})
            self.assertEqual(3, len(rows))
            self.assertEqual(str(self.project), store.get(alias, [pi['events'][0]['id']])[0]['project'])
        configure(self.data, capture_scope='selected', included_projects=[str(self.project)])
        omitted = self.call(operation='capture', project=str(self.root / 'other'), events=[self.event()])
        self.assertEqual('capture_disabled', omitted['events'][0]['reason'])
        configure(self.data, capture_tools=False)
        tool = self.call(operation='capture', events=[dict(message_id='off-tool', role='toolResult',
                        tool_id='off-call', tool_name='bash', text='DISABLED_TOOL')])
        self.assertEqual('tool_capture_disabled', tool['events'][0]['reason'])

    def test_tool_json_secrets_and_truncation_never_become_retained_excerpts(self):
        tool = dict(message_id='json-tool', role='toolResult', tool_id='json-call', tool_name='bash',
                    text=json.dumps({'ok': 'passed', 'credential': {'nested': ['SECRET_OBJECT_VALUE']}}))
        captured = self.call(operation='capture', events=[tool])
        with Store(self.data) as store:
            entry_id = captured['events'][0]['id']
            self.assertNotIn('SECRET_OBJECT_VALUE', str(store.get(self.project, [entry_id])))
            self.assertNotIn('SECRET_OBJECT_VALUE', str(store.get_source_capture(self.project, entry_id)))
        huge = self.call(operation='capture', events=[dict(tool, message_id='large', tool_id='large-call',
            text=json.dumps({'rows': ['OMITTED_FRAGMENT_' + str(index) for index in range(6000)]}))])
        with Store(self.data) as store:
            entry_id = huge['events'][0]['id']
            raw = store.get_source_capture(self.project, entry_id)
            self.assertIsNone(raw['tool_response'])
            self.assertIsNone(raw['response_excerpt'])
            self.assertEqual('incomplete_json_fragments', raw['response_metadata']['read_omitted_reason'])
            self.assertNotIn('OMITTED_FRAGMENT_', str(store.get(self.project, [entry_id])))

    def test_incoming_truncation_wrapper_never_enters_source_evidence(self):
        wrapper = {'__codex_mem_truncated__': True, 'original_bytes': 100000,
                   'head': '{"credential":{"value":"HEAD_SENTINEL', 'tail': 'TAIL_SENTINEL"}}'}
        result = self.call(operation='capture', events=[dict(message_id='wrapped', role='toolResult',
            tool_id='wrapped-call', tool_name='bash', text=json.dumps(wrapper))])
        with Store(self.data) as store:
            entry_id = result['events'][0]['id']
            record = store.get(self.project, [entry_id])[0]
            raw = store.get_source_capture(self.project, entry_id)
            self.assertNotIn('SENTINEL', record['body'])
            self.assertIn('incomplete_json_fragments', record['body'])
            self.assertIsNone(raw['tool_response'])
            self.assertIsNone(raw['response_excerpt'])
            self.assertEqual('incomplete_json_fragments', result['events'][0]['reason'])
            batch = store.claim_observation_batch(self.project, 'fixture-observer', 'gpt-6-luna', 'medium')
            self.assertNotIn('SENTINEL', str(batch['sources']))

    def test_context_limits_and_protocol_overflow_have_content_free_reasons(self):
        configure(self.data, context_chars=128)
        tiny = self.call(operation='context', phase='prompt', query='x' * 2000)
        self.assertEqual('budget_too_small', tiny['reason'])
        self.assertEqual('', tiny['text'])
        huge = self.call(operation='capture', events=[dict(message_id='huge', role='user', text='PRIVATE_SENTINEL' * 20000)])
        self.assertEqual('request_overflow', huge['reason'])
        self.assertNotIn('PRIVATE_SENTINEL', str(huge))
        with Store(self.data) as store:
            self.assertEqual([], store.timeline(self.project))


@unittest.skipUnless(os.environ.get('CODEX_MEM_PI'), 'set CODEX_MEM_PI for installed Pi compatibility')
class InstalledPiHostTests(unittest.TestCase):
    def setUp(self):
        import http.server
        import queue
        import threading
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.project = self.root / 'project'
        self.project.mkdir()
        self.data = self.root / 'memory'
        self.agent = self.root / 'agent'
        self.agent.mkdir()
        configure(self.data, capture_scope='all', service_enabled=False, semantic_enabled=False)
        self.requests = []
        self.capture_counts = []
        self.use_tool = False
        self.retry_failure = False
        owner = self

        class Provider(http.server.BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def do_POST(self):
                request = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                owner.requests.append(request)
                with Store(owner.data) as store:
                    owner.capture_counts.append(len([row for row in store.timeline(owner.project, limit=100)
                        if (row['source'] or '').startswith('host:pi:')]))
                if owner.retry_failure and len(owner.requests) == 1:
                    self.send_response(503)
                    self.send_header('Content-Type', 'application/json')
                    self.end_headers()
                    self.wfile.write(b'{"error":{"message":"temporarily unavailable"}}')
                    return
                self.send_response(200)
                self.send_header('Content-Type', 'text/event-stream')
                self.end_headers()
                changes = [({'role': 'assistant', 'content': 'Use local '}, None),
                           ({'content': 'import.'}, None), ({}, 'stop')]
                if owner.use_tool and not any(message['role'] == 'tool' for message in request['messages']):
                    changes = [({'role': 'assistant', 'tool_calls': [{'index': 0, 'id': 'native-tool-call',
                        'type': 'function', 'function': {'name': 'fixture_echo', 'arguments': '{}'}}]}, None), ({}, 'tool_calls')]
                for delta, finish in changes:
                    chunk = dict(id='fixture', object='chat.completion.chunk', model='fixture',
                                 choices=[dict(index=0, delta=delta, finish_reason=finish)])
                    self.wfile.write(('data: ' + json.dumps(chunk) + '\n\n').encode())
                    self.wfile.flush()
                self.wfile.write(b'data: [DONE]\n\n')

        self.http = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Provider)
        self.thread = threading.Thread(target=self.http.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.stop_http)
        (self.agent / 'models.json').write_text(json.dumps({'providers': {'fixture': {
            'baseUrl': f'http://127.0.0.1:{self.http.server_port}/v1', 'api': 'openai-completions',
            'apiKey': 'not-a-credential', 'models': [{'id': 'fixture', 'contextWindow': 128000, 'maxTokens': 1024}],
        }}}))
        (self.agent / 'settings.json').write_text(json.dumps({'compaction': {'enabled': False},
            'retry': {'enabled': False}, 'extensions': ['-builtin:codemode', '-builtin:tool_search']}))
        self.records = queue.Queue()
        self.process = None

    def stop_http(self):
        self.http.shutdown()
        self.http.server_close()
        self.thread.join(timeout=3)

    def start(self, *, python=None, extra_extension=None, session_file=None, package=None, execution_path=None, installed_project=False):
        import threading
        self.stderr = tempfile.TemporaryFile()
        self.addCleanup(self.stderr.close)
        session_args = ['--session', str(session_file)] if session_file else ['--no-session']
        extension_args = ['--extension', str(extra_extension)] if extra_extension else []
        memory_args = [] if installed_project else ['--no-mcp', '--extension', str(package or ROOT / 'pi/extension.ts')]
        self.process = subprocess.Popen([os.environ['CODEX_MEM_PI'], '--mode', 'rpc', *session_args,
            '--approve' if installed_project else '--no-approve', '--provider', 'fixture', '--model', 'fixture', '--thinking', 'off',
            '--no-builtin-tools', '--no-skills', '--no-prompt-templates', '--no-context-files',
            '--no-themes', *memory_args, *extension_args], cwd=self.project,
            env={'PATH': execution_path or os.environ['PATH'], 'HOME': str(self.root), 'PI_CODING_AGENT_DIR': str(self.agent),
                 'CODEX_MEM_HOME': str(self.data), 'CODEX_MEM_PYTHON': python or sys.executable},
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=self.stderr)
        self.addCleanup(self.stop)
        def read():
            for line in self.process.stdout:
                self.records.put(json.loads(line))
            self.records.put({'type': 'fixture_exit'})
        self.reader = threading.Thread(target=read, daemon=True)
        self.reader.start()

    def stop(self):
        if self.process is None:
            return
        self.process.stdin.close()
        try:
            self.process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait(timeout=5)
        self.reader.join(timeout=3)
        self.process.stdout.close()
        self.process = None

    def prompt(self, text):
        import time
        self.process.stdin.write((json.dumps(dict(type='prompt', id='prompt', message=text)) + '\n').encode())
        self.process.stdin.flush()
        events = []
        deadline = time.monotonic() + 25
        while True:
            event = self.records.get(timeout=max(.01, deadline - time.monotonic()))
            events.append(event)
            self.assertNotEqual('fixture_exit', event['type'], events)
            if event['type'] == 'agent_settled':
                return events

    def rpc(self, command, **values):
        import time
        command_id = 'control-' + command
        self.process.stdin.write((json.dumps(dict(type=command, id=command_id, **values)) + '\n').encode())
        self.process.stdin.flush()
        deadline = time.monotonic() + 25
        while True:
            event = self.records.get(timeout=max(.01, deadline - time.monotonic()))
            self.assertNotEqual('fixture_exit', event['type'], event)
            if event.get('id') == command_id and event['type'] == 'response':
                self.assertTrue(event.get('success'), event)
                return event.get('data', {})

    def native_session(self):
        path = self.root / 'lifecycle.jsonl'
        path.write_text(json.dumps(dict(type='session', version=3, id='lifecycle-session',
            timestamp='2026-01-01T00:00:00Z', cwd=str(self.project))) + '\n')
        return path

    def captured_sources(self):
        with Store(self.data) as store:
            rows = [row for row in store.timeline(self.project, limit=100) if row['source'].startswith('host:pi:')]
            return store.get(self.project, [row['id'] for row in rows]) if rows else []

    def process_sources(self):
        from unittest.mock import patch
        from codex_mem.service import enqueue, run_service
        install_codex_fixture(self.root, 'Lifecycle import choice', dict(title='Lifecycle import choice', request=None,
            investigated='', learned='Use local import.', completed='', next_steps='', notes='', source_ids=['s2']))
        configure(self.data, service_enabled=True, usage_enabled=False, processor_workers=1)
        self.assertEqual('queued', enqueue(self.project, self.data)['status'])
        with patch.dict(os.environ, PATH=str(self.root) + os.pathsep + os.environ['PATH']):
            result = run_service(self.data, max_cycles=1, processor_timeout=10)
        self.assertEqual(1, result['jobs'], result)
        configure(self.data, service_enabled=False)

    def test_installed_resume_and_tree_navigation_keep_branch_identity_and_context(self):
        import queue
        helper = self.root / 'branch.ts'
        helper.write_text("""export default function(pi) {
  pi.registerCommand('fixture-branch', {handler: async (_args, ctx) => {
    const target = ctx.sessionManager.getBranch().find(e => e.type === 'message' && e.message.role === 'assistant');
    await ctx.navigateTree(target.id, {summarize:false});
  }});
}""")
        path = self.native_session()
        self.start(session_file=path, extra_extension=helper)
        self.prompt('Use local import.')
        first = self.captured_sources()
        branch = json.loads(first[0]['source'].split(':', 3)[3])['branch']
        self.process_sources()
        self.stop()
        self.records = queue.Queue()
        self.start(session_file=path, extra_extension=helper)
        self.prompt('Use local import.')
        self.assertIn('Lifecycle import choice', json.dumps(self.requests[-1]))
        resumed = self.captured_sources()
        self.assertEqual(4, len(resumed))
        self.assertEqual({branch}, {json.loads(row['source'].split(':', 3)[3])['branch'] for row in resumed})
        self.rpc('prompt', message='/fixture-branch')
        self.prompt('Use local import.')
        self.assertIn('Lifecycle import choice', json.dumps(self.requests[-1]))
        branched = self.captured_sources()
        self.assertEqual(6, len(branched), branched)
        self.assertEqual(2, len({row['session_id'] for row in branched}))
        self.assertEqual(6, len({json.loads(row['source'].split(':', 3)[3])['message_id'] for row in branched}))

    def test_installed_fork_replacement_and_compaction_keep_old_sources_historical(self):
        path = self.native_session()
        settings_path = self.agent / 'settings.json'
        settings = json.loads(settings_path.read_text())
        settings['compaction']['keepRecentTokens'] = 1
        settings_path.write_text(json.dumps(settings))
        self.start(session_file=path)
        self.prompt('Use local import.')
        originals = {row['id'] for row in self.captured_sources()}
        self.process_sources()
        self.rpc('clone')
        fork = self.rpc('get_state')
        self.assertNotEqual('lifecycle-session', fork['sessionId'])
        self.prompt('Use local import.')
        self.assertIn('Lifecycle import choice', json.dumps(self.requests[-1]))
        self.assertEqual(4, len(self.captured_sources()))
        # Native compaction uses the same disposable provider. Its summary is
        # history, never a newly observed user or assistant source.
        self.rpc('compact')
        self.assertEqual(4, len(self.captured_sources()))
        self.prompt('Use local import.')
        self.assertIn('Lifecycle import choice', json.dumps(self.requests[-1]))
        self.assertEqual(6, len(self.captured_sources()))
        self.rpc('new_session', parentSession=str(path))
        self.prompt('Use local import.')
        self.assertIn('Lifecycle import choice', json.dumps(self.requests[-1]))
        self.assertEqual(8, len(self.captured_sources()))
        self.rpc('switch_session', sessionPath=str(path))
        self.prompt('Use local import.')
        self.assertIn('Lifecycle import choice', json.dumps(self.requests[-1]))
        rows = self.captured_sources()
        self.assertEqual(10, len(rows))
        self.assertTrue(originals <= {row['id'] for row in rows})
        self.assertEqual(3, len({row['session_id'] for row in rows}))
        self.assertTrue(all(row['provenance']['record_role'] in {'user_intent', 'assistant_report'} for row in rows))
        fork_entries = [json.loads(line) for line in Path(fork['sessionFile']).read_text().splitlines()]
        self.assertTrue(any(entry['type'] == 'compaction' for entry in fork_entries))
        copied_ids = {entry['id'] for entry in fork_entries if entry['type'] == 'message'}
        old_ids = {json.loads(row['source'].split(':', 3)[3])['message_id'] for row in rows if row['id'] in originals}
        self.assertTrue(old_ids <= copied_ids)
        for old_id in old_ids:
            self.assertEqual(1, sum(json.loads(row['source'].split(':', 3)[3])['message_id'] == old_id for row in rows))

    def test_installed_retry_waits_for_settlement_and_excludes_failed_response(self):
        settings_path = self.agent / 'settings.json'
        settings = json.loads(settings_path.read_text())
        settings['retry'] = {'enabled': True, 'maxRetries': 1, 'baseDelayMs': 1}
        settings_path.write_text(json.dumps(settings))
        self.retry_failure = True
        self.start(session_file=self.native_session())
        events = self.prompt('Use local import.')
        self.assertTrue(any(event['type'] == 'auto_retry_start' for event in events), events)
        self.assertTrue(all(count == 0 for count in self.capture_counts), self.capture_counts)
        rows = self.captured_sources()
        self.assertEqual(2, len(rows), rows)
        self.assertNotIn('temporarily unavailable', str(rows))
        self.process_sources()
        self.prompt('Use local import.')
        self.assertIn('Lifecycle import choice', json.dumps(self.requests[-1]))

    def test_installed_copied_private_history_needs_fresh_user_before_capture(self):
        helper = self.root / 'continue.ts'
        helper.write_text("""export default function(pi) {
  pi.registerCommand('fixture-continue', {handler: async () => {
    pi.sendMessage({customType:'fixture-continue',content:'Continue.',display:false}, {triggerTurn:true});
  }});
}""")
        self.start(session_file=self.native_session(), extra_extension=helper)
        self.prompt('<private>HIDDEN_BRANCH_HISTORY</private>')
        self.assertEqual([], self.captured_sources())
        self.rpc('clone')
        self.prompt('/fixture-continue')
        self.assertEqual([], self.captured_sources())
        self.prompt('Use local import.')
        self.assertEqual(2, len(self.captured_sources()))

    def test_installed_completed_native_events_keep_roles_ids_and_injection_out(self):
        with Store(self.data) as store:
            store.remember(self.project, 'Earlier decision', 'Use local import. PRIOR_PROJECT_MEMORY', kind='decision')
        session_file = self.root / 'session.jsonl'
        session_file.write_text('\n'.join(json.dumps(value) for value in [
            dict(type='session', version=3, id='native-session', timestamp='2026-01-01T00:00:00Z', cwd=str(self.project)),
            dict(type='message', id='existing1', parentId=None, timestamp='2026-01-01T00:00:00Z',
                 message=dict(role='user', content='Use local import.', timestamp=1767225600000)),
        ]) + '\n')
        self.start(session_file=session_file)
        self.prompt('Use local import.')
        # The only replacement is Codex's external executable. Pi, the Python
        # adapter, queue, Store, validation and next-request context are real.
        from unittest.mock import patch
        from codex_mem.service import enqueue, run_service
        install_codex_fixture(self.root, 'Verified import choice', dict(title='Import summary', request=None,
            investigated='', learned='Use local import.', completed='', next_steps='', notes='', source_ids=['s2']))
        configure(self.data, service_enabled=True, usage_enabled=False, processor_workers=1)
        self.assertEqual('queued', enqueue(self.project, self.data)['status'])
        with patch.dict(os.environ, PATH=str(self.root) + os.pathsep + os.environ['PATH']):
            result = run_service(self.data, max_cycles=1, processor_timeout=10)
        self.assertEqual(1, result['jobs'], result)
        configure(self.data, service_enabled=False)
        self.prompt('Use local import.')
        self.assertIn('Verified import choice', json.dumps(self.requests[-1]))
        with Store(self.data) as store:
            entries = [row for row in store.timeline(self.project, limit=100) if (row['source'] or '').startswith('host:pi:')]
            self.assertEqual(4, len(entries), entries)
            records = store.get(self.project, [row['id'] for row in entries])
            self.assertEqual({'user_intent', 'assistant_report'}, {row['provenance']['record_role'] for row in records})
            self.assertEqual(4, len({json.loads(row['source'].split(':', 3)[3])['message_id'] for row in records}))
            self.assertTrue(all('PRIOR_PROJECT_MEMORY' not in row['body'] for row in records))
            native = [json.loads(line) for line in session_file.read_text().splitlines()]
            native_ids = {entry['id'] for entry in native if entry['type'] == 'message' and entry['message']['role'] in {'user', 'assistant', 'toolResult'}} - {'existing1'}
            captured_ids = {json.loads(row['source'].split(':', 3)[3])['message_id'] for row in records}
            self.assertEqual(native_ids, captured_ids)
            self.assertNotIn('existing1', captured_ids)
        self.assertIn('PRIOR_PROJECT_MEMORY', json.dumps(self.requests[0]))
        self.assertIn('Untrusted memory', json.dumps(self.requests[0]))


    def test_installed_tool_and_continuation_wait_for_final_settlement(self):
        helper = self.root / 'continuation.ts'
        helper.write_text("""import { Type } from 'typebox';
export default function (pi) {
  pi.registerTool({ name: 'fixture_echo', label: 'Fixture', description: 'Disposable test tool',
    parameters: Type.Object({}), execute: async () => ({ content: [{type: 'text', text: 'TOOL_RESULT'}] }) });
  let continued = false;
  pi.on('agent_before_settle', async () => { if (!continued) { continued = true; return {continue: true, entries: [{type: 'custom_message', customType: 'fixture-continuation', content: 'Continue once.', display: false}]}; } });
}""")
        self.use_tool = True
        self.start(extra_extension=helper)
        events = self.prompt('Use local import.')
        self.assertGreaterEqual(sum(event['type'] == 'agent_end' for event in events), 2)
        self.assertTrue(all(count == 0 for count in self.capture_counts), self.capture_counts)
        with Store(self.data) as store:
            entries = store.timeline(self.project, limit=100)
            records = store.get(self.project, [row['id'] for row in entries])
            tools = [row for row in records if row['provenance']['record_role'] == 'tool_record']
            self.assertEqual(1, len(tools), records)
            self.assertIn('native-tool-call', tools[0]['source'])
            self.assertIn('TOOL_RESULT', str(store.get_source_capture(self.project, tools[0]['id'])))
            self.assertEqual(2, sum(row['provenance']['record_role'] == 'assistant_report' for row in records))

    def test_installed_service_failure_keeps_conversation_running(self):
        self.start(python=str(self.root / 'missing-python'))
        events = self.prompt('Use local import.')
        self.assertTrue(any(event['type'] == 'message_end' and event.get('message', {}).get('role') == 'assistant'
                            for event in events))
        self.assertIn('service_unavailable', json.dumps(events))
        with Store(self.data) as store:
            self.assertEqual([], store.timeline(self.project))

    def test_installed_private_turn_never_creates_sources(self):
        self.start()
        self.prompt('<private>HIDDEN_TASK</private>')
        with Store(self.data) as store:
            self.assertEqual([], store.timeline(self.project))
        self.prompt('Use local import.')
        with Store(self.data) as store:
            self.assertEqual(2, len(store.timeline(self.project)))


    def test_installed_empty_refresh_clears_only_automatic_memory_and_reports_budget(self):
        helper = self.root / 'unrelated.ts'
        helper.write_text("export default function(pi) { pi.on('session_start', () => { pi.sendMessage({customType:'other-context',content:'OTHER_CONTEXT',display:false}, {triggerTurn:false}); }); }")
        with Store(self.data) as store:
            store.remember(self.project, 'Earlier', 'Use local import. OLD_MEMORY_SENTINEL', kind='decision')
        self.start(extra_extension=helper)
        self.prompt('Use local import.')
        self.assertIn('OLD_MEMORY_SENTINEL', json.dumps(self.requests[-1]))
        configure(self.data, context_chars=128)
        events = self.prompt('Use local import.')
        supplied = json.dumps(self.requests[-1])
        self.assertNotIn('OLD_MEMORY_SENTINEL', supplied)
        self.assertNotIn('codex_mem_untrusted_context', supplied)
        self.assertIn('OTHER_CONTEXT', supplied)
        self.assertIn('budget_too_small', json.dumps(events))

    def test_installed_failed_refresh_clears_previous_memory(self):
        wrapper = self.root / 'switch-python'
        failure = self.root / 'fail-service'
        wrapper.write_text('#!' + sys.executable + '\n' + f"""import pathlib, subprocess, sys
if pathlib.Path({str(failure)!r}).exists():
    sys.exit(1)
sys.exit(subprocess.run([sys.executable, *sys.argv[1:]]).returncode)
""")
        wrapper.chmod(0o700)
        with Store(self.data) as store:
            store.remember(self.project, 'Earlier', 'Use local import. STALE_MEMORY_SENTINEL', kind='decision')
        self.start(python=str(wrapper))
        self.prompt('Use local import.')
        self.assertIn('STALE_MEMORY_SENTINEL', json.dumps(self.requests[-1]))
        failure.touch()
        events = self.prompt('Use local import.')
        self.assertNotIn('STALE_MEMORY_SENTINEL', json.dumps(self.requests[-1]))
        self.assertNotIn('codex_mem_untrusted_context', json.dumps(self.requests[-1]))
        self.assertIn('service_unavailable', json.dumps(events))

    def test_installed_utf8_split_across_real_service_output_chunks_preserves_text(self):
        wrapper = self.root / 'split-python'
        wrapper.write_text('#!' + sys.executable + '\n' + """import os, subprocess, sys, time
result = subprocess.run([sys.executable, *sys.argv[1:]], capture_output=True)
raw = result.stdout
index = raw.find('🧪'.encode())
if index >= 0:
    os.write(1, raw[:index + 1])
    time.sleep(.05)
    os.write(1, raw[index + 1:])
else:
    os.write(1, raw)
sys.exit(result.returncode)
""")
        wrapper.chmod(0o700)
        with Store(self.data) as store:
            store.remember(self.project, 'Unicode', 'Use local import. 🧪UNICODE_SENTINEL', kind='decision')
        self.start(python=str(wrapper))
        self.prompt('Use local import.')
        self.assertIn('🧪UNICODE_SENTINEL', json.dumps(self.requests[-1], ensure_ascii=False))
        self.assertNotIn('�', json.dumps(self.requests[-1], ensure_ascii=False))

    def test_installed_response_byte_overflow_is_fail_open(self):
        wrapper = self.root / 'overflow-python'
        wrapper.write_text('#!' + sys.executable + '\nimport os, time\nos.write(1, b" " * (64 * 1024 + 1))\ntime.sleep(10)\n')
        wrapper.chmod(0o700)
        self.start(python=str(wrapper))
        events = self.prompt('Use local import.')
        self.assertIn('response_overflow', json.dumps(events))
        self.assertTrue(any(event['type'] == 'message_end' and event.get('message', {}).get('role') == 'assistant' for event in events))
