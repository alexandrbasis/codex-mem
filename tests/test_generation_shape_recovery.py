"""Invented inventory replies exercise real adapter loops with in-memory transports.

All meanings and usage samples are authored. No process, provider or model runs.
"""
import contextlib
import hashlib
import copy
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import time
import unittest
from unittest import mock

from codex_mem import processor
from codex_mem.config import configure
from codex_mem.execution import ExecutionIdentity, ExecutionResult
from codex_mem.store import Store
from tests.test_summary_support import supported_review, support_rows


class InventoryTransport:
    def __init__(self, candidates):
        self.raw = [output if isinstance(output, str) else
                    json.dumps({'result': output}, ensure_ascii=False, indent=2) for output in candidates]
        self.delivered_raw = []
        self.sent = []
        self.events = []
        self.closed = False
        self.interruptions = []

    def send(self, value):
        pass

    def request(self, method, params=None, **kwargs):
        if method == 'initialize': return {}
        if method == 'account/read': return {'account': {'type': 'apiKey'}, 'requiresOpenaiAuth': True}
        if method == 'model/list':
            return {'data': [dict(id=processor.MODEL, model=processor.MODEL,
                supportedReasoningEfforts=[dict(reasoningEffort=processor.REASONING_EFFORT)])]}
        if method == 'config/read': return {'config': {}}
        if method == 'thread/start':
            return dict(thread={'id': 'inventory-generation'}, model=processor.MODEL,
                        reasoningEffort=processor.REASONING_EFFORT, modelProvider='openai')
        if method == 'mcpServerStatus/list': return {'data': []}
        if method != 'turn/start': raise AssertionError(method)
        self.sent.append(copy.deepcopy(params))
        number = len(self.sent)
        if number > len(self.raw): raise AssertionError('Unexpected extra generation')
        turn = f'inventory-turn-{number}'
        def event(method, **values):
            return dict(method=method, params=dict(threadId='inventory-generation', turnId=turn, **values))
        self.events = [event('turn/started', turn={'id': turn}),
            event('thread/tokenUsage/updated', tokenUsage={'total': dict(inputTokens=10*number,
                cachedInputTokens=0, cacheWriteInputTokens=0, outputTokens=2*number,
                reasoningOutputTokens=0, totalTokens=12*number)}),
            event('item/completed', item=dict(id=f'answer-{number}', type='agentMessage', phase='final_answer', text=self.raw[number-1])),
            event('turn/completed', turn=dict(id=turn, status='completed'))]
        return {'turn': {'id': turn}}

    def next_notification(self, handler):
        event = self.events.pop(0)
        if event['method']=='item/completed':
            self.delivered_raw.append(event['params']['item']['text'])
        handler(event)

    def interrupt(self, thread, turn):
        self.interruptions.append((thread, turn))

    def close(self):
        self.closed = True


class PiInventoryTransport(InventoryTransport):
    def __init__(self, candidates, profile, deadline, run):
        super().__init__(candidates)
        self.profile, self.deadline, self.run = profile, deadline, run

    def state(self, initial=False):
        return dict(model=dict(id=self.profile.model, provider=self.profile.provider),
            thinkingLevel=self.profile.reasoning_effort, sessionId='inventory-generation',
            isStreaming=False, isCompacting=False, pendingMessageCount=0, autoCompactionEnabled=False,
            messageCount=0 if initial else len(self.sent)*2+1, sessionFile=None)

    def request(self, method, observer=None, **kwargs):
        if method == 'get_state': return self.state()
        if method != 'prompt': raise AssertionError(method)
        from codex_mem.pi_runtime import SYSTEM_PROMPT
        self.sent.append(copy.deepcopy(kwargs))
        number = len(self.sent)
        if number > len(self.raw): raise AssertionError('Unexpected extra Pi generation')
        self.events = [dict(type='agent_start')]
        if number == 1:
            self.events.append(dict(type='message_end', message=dict(role='system', content='',
                sections=dict(preamble=SYSTEM_PROMPT, cwd=f'<cwd>\n{self.run.cwd}\n</cwd>'))))
        self.events.extend([dict(type='message_end', message=dict(role='assistant', provider=self.profile.provider,
            model=self.profile.model, content=[dict(type='text', text=self.raw[number-1])], stopReason='stop',
            usage=dict(input=10, output=2, cacheRead=0, cacheWrite=0, reasoning=0, totalTokens=12))),
            dict(type='agent_end'), dict(type='agent_settled', aborted=False)])
        return dict(disposition='started')

    def message(self):
        event = self.events.pop(0)
        if event['type']=='message_end' and event['message']['role']=='assistant':
            self.delivered_raw.append(event['message']['content'][0]['text'])
        return event

    def remaining(self):
        from codex_mem import pi_runtime
        left = self.deadline-pi_runtime.time.monotonic()
        if left <= 0: raise processor.ProcessorFailure('timeout')
        return left


class GenerationShapeRecoveryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.project, self.data = self.root/'library', self.root/'memory'
        self.project.mkdir()
        configure(self.data, capture_scope='all', service_enabled=False, semantic_enabled=False,
                  jev_filter_enabled=False, jev_quality_enabled=False)
        self.body = 'The librarian recorded the donated atlas in the library inventory.'
        with Store(self.data) as store:
            self.raw = store.remember(self.project, 'Library inventory report', self.body,
                                      source='hook:Stop', session_id='library-session')

    def candidate(self, missing=False):
        summary = dict(title='Library inventory report', request=None, investigated='', learned='',
            completed=self.body, next_steps='', notes='', source_ids=['s1'])
        note = dict(title='Donated atlas inventory', body=self.body, tags=['inventory'], source_ids=['s1'],
            observation=dict(type='discovery', subtitle='', narrative=self.body,
                             facts=[], concepts=[], files_read=[], files_modified=[]))
        return dict(notes=[note], disposition='processed', session_summary=None if missing else summary)

    def run_candidates(self, candidates, *, verdict='supported', runtime='codex', expire_on_diagnostic=False):
        transport = InventoryTransport(candidates)
        reviews = []
        support_number = 0
        if runtime == 'pi':
            configure(self.data, processing_profile=dict(runtime='pi', provider='fixture', model='inventory-fixture',
                                                       reasoning_effort='low', timeout=240))
        clock = [time.monotonic()]
        controlled_time = SimpleNamespace(monotonic=lambda:clock[0], time=time.time)
        class Reviewer:
            def execute(inner, request):
                reviews.append(request)
                output = supported_review(request.context)['output']
                nonlocal support_number
                if request.context['version'] == 'summary-support-v6':
                    chosen = verdict[support_number] if isinstance(verdict, list) else verdict
                    support_number += 1
                    if chosen != 'supported':
                        next(row for row in support_rows(output) if row['unit_id']=='completed')['verdict'] = chosen
                profile = request.profile
                return ExecutionResult(output=output, identity=ExecutionIdentity(profile.runtime, profile.provider,
                    profile.model, profile.reasoning_effort, f'inventory-review-{len(reviews)}', 'review-turn'),
                    metrics=dict(duration_ms=1, usage=dict(status='reported', source='app_server_thread_total', updates=1,
                        tokens=dict(input_tokens=16, output_tokens=4, total_tokens=20, cached_input_tokens=0,
                                    cache_write_input_tokens=0, reasoning_output_tokens=0))))
        @contextlib.contextmanager
        def prepared(pi, profile, deadline, run, **kwargs):
            nonlocal transport
            transport = PiInventoryTransport(candidates, profile, deadline, run)
            run.version_verified = run.launched = True
            run.cwd = '/in-memory/library'
            run.state(transport.state(initial=True), initial=True)
            try:
                yield transport
            finally:
                transport.close()
        from codex_mem.observer_usage_store import record_execution_call
        checkpoints = []
        def record(*args, **kwargs):
            value = record_execution_call(*args, **kwargs)
            metrics = kwargs.get('metrics') or {}
            tokens = metrics.get('usage', {}).get('tokens')
            if args[4] == 'generation' and tokens:
                checkpoints.append(tokens['total_tokens'])
            if expire_on_diagnostic and kwargs.get('review_diagnostic'):
                clock[0] += 241.0
            return value
        with contextlib.ExitStack() as stack:
            stack.enter_context(mock.patch('codex_mem.processor.shutil.which', return_value='/in-memory/codex'))
            stack.enter_context(mock.patch('codex_mem.processor._AppServer', return_value=transport))
            stack.enter_context(mock.patch('codex_mem.pi_runtime._prepared', prepared))
            stack.enter_context(mock.patch('codex_mem.observer_usage_store.record_execution_call', record))
            if expire_on_diagnostic:
                stack.enter_context(mock.patch('codex_mem.processor.time', controlled_time))
                stack.enter_context(mock.patch('codex_mem.pi_runtime.time', controlled_time))
            result = processor.process_pending(self.project, self.data, summary_reviewer=Reviewer())
        with Store(self.data) as store:
            job = store.observation_job_status(self.project, result['job_id'])
            outputs = store.get(self.project, job['output_ids']) if job['output_ids'] else []
            attempts = store.observation_attempts(self.project, result['job_id'])
            raw = store.get(self.project, [self.raw['id']])[0]
        transport.checkpoints = checkpoints
        return result, transport, reviews, outputs, attempts, raw

    def test_missing_summary_uses_existing_correction_then_full_review_and_atomic_commit(self):
        missing, complete = self.candidate(True), self.candidate()
        result, transport, reviews, outputs, attempts, raw = self.run_candidates([missing, complete])
        self.assertEqual('processed', result['status'], result)
        self.assertEqual(2, len(transport.sent))
        self.assertEqual(['summary-support-v6', 'summary-source-v4', 'summary-outcome-v5'],
                         [request.context['version'] for request in reviews])
        self.assertEqual(2, len(outputs))
        self.assertEqual(self.body, next(row for row in outputs if row['kind']=='note')['body'])
        self.assertIsNotNone(raw['superseded_by'])
        calls = json.loads(attempts[0]['execution_calls'])
        self.assertEqual({'generation', 'summary_support_1', 'summary_source_1', 'summary_outcome_1'}, set(calls))
        self.assertEqual(24, calls['generation']['metrics']['total_tokens'])
        self.assertEqual(84, attempts[0]['total_tokens'])
        self.assertEqual('inventory-generation', calls['generation']['execution_id'])
        self.assertEqual('missing_required_summary', result['generation_correction']['initial_reason_code'])
        diagnostic = calls['generation']['review_diagnostic']
        self.assertEqual(result['generation_correction']['candidate_fingerprint'], diagnostic['candidate_fingerprint'])
        self.assertEqual('invalid', diagnostic['status'])
        self.assertIn('missing_required_summary', transport.sent[1]['input'][0]['text'])
        self.assertEqual(transport.raw[0], json.dumps({'result':missing}, ensure_ascii=False, indent=2))
        self.assertEqual(complete, json.loads(transport.raw[1])['result'])
        self.assertTrue(transport.closed)

    def assert_no_partial_write(self, outputs, raw):
        self.assertEqual([], outputs)
        self.assertEqual(self.body, raw['body'])
        self.assertIsNone(raw['superseded_by'])

    def test_repeated_missing_summary_exhausts_same_allowance_without_review_or_writes(self):
        result, transport, reviews, outputs, attempts, raw = self.run_candidates([self.candidate(True)]*2)
        self.assertEqual('missing_required_summary', result.get('reason_code'), result)
        self.assertEqual(2, len(transport.sent))
        self.assertEqual([], reviews)
        self.assert_no_partial_write(outputs, raw)
        calls = json.loads(attempts[0]['execution_calls'])
        self.assertEqual({'generation'}, set(calls))
        self.assertEqual(24, calls['generation']['metrics']['total_tokens'])
        self.assertEqual('invalid', calls['generation']['review_diagnostic']['status'])
        self.assertEqual('missing_required_summary', result['generation_correction']['initial_reason_code'])
        self.assertTrue(transport.closed)

    def test_structural_correction_cannot_request_third_generation_after_semantic_rejection(self):
        result, transport, reviews, outputs, attempts, raw = self.run_candidates(
            [self.candidate(True), self.candidate()], verdict='unsupported')
        self.assertEqual('summary_support_rejected', result.get('reason_code'), result)
        self.assertEqual(2, len(transport.sent))
        self.assertEqual(['summary-support-v6'], [request.context['version'] for request in reviews])
        self.assert_no_partial_write(outputs, raw)
        calls = json.loads(attempts[0]['execution_calls'])
        self.assertEqual({'generation', 'summary_support_1'}, set(calls))
        self.assertEqual(44, attempts[0]['total_tokens'])
        self.assertEqual('missing_required_summary', result['generation_correction']['initial_reason_code'])

    def test_initial_valid_candidate_keeps_single_generation_and_normal_reviews(self):
        result, transport, reviews, outputs, attempts, raw = self.run_candidates([self.candidate()])
        self.assertEqual('processed', result['status'], result)
        self.assertEqual(1, len(transport.sent))
        self.assertEqual(3, len(reviews))
        self.assertEqual(72, attempts[0]['total_tokens'])
        self.assertNotIn('generation_correction', result)
        self.assertNotIn('review_diagnostic', json.loads(attempts[0]['execution_calls'])['generation'])

    def test_unknown_source_handle_and_other_invalid_shapes_still_fail_before_correction(self):
        cases = [dict(self.candidate(), disposition='skipped'), self.candidate(True)]
        cases[1]['notes'][0]['source_ids'] = ['unknown-handle']
        for value, expected in zip(cases, ['skipped_with_content', 'unknown_source_handle']):
            with self.subTest(reason=expected):
                # A separate disposable project retains the independent first-failure path.
                self.setUp()
                result, transport, reviews, outputs, attempts, raw = self.run_candidates([value])
                self.assertEqual(expected, result.get('reason_code'), result)
                self.assertEqual(1, len(transport.sent))
                self.assertEqual([], reviews)
                self.assertNotIn('generation_correction', result)
                self.assert_no_partial_write(outputs, raw)

    def test_pi_adapter_uses_same_two_turn_allowance_and_keeps_independent_review_receipts(self):
        result, transport, reviews, outputs, attempts, raw = self.run_candidates(
            [self.candidate(True), self.candidate()], runtime='pi')
        self.assertEqual('processed', result['status'], result)
        self.assertEqual(2, len(transport.sent))
        self.assertIn('missing_required_summary', transport.sent[1]['message'])
        self.assertEqual(3, len(reviews))
        self.assertTrue(all(request.profile.runtime=='pi' and request.profile.model=='inventory-fixture'
                            and request.profile.timeout==240 for request in reviews))
        self.assertEqual(1, len({request.deadline for request in reviews}))
        calls = json.loads(attempts[0]['execution_calls'])
        self.assertEqual(24, calls['generation']['metrics']['total_tokens'])
        self.assertEqual(84, attempts[0]['total_tokens'])
        self.assertEqual('invalid', calls['generation']['review_diagnostic']['status'])
        self.assertEqual('structure', calls['generation']['review_diagnostic']['invalid_subtype'])
        self.assertEqual(2, json.loads(attempts[0]['execution_receipt'])['generation'])
        self.assertEqual(2, len(outputs))
        self.assertTrue(transport.closed)

    def test_pi_second_candidate_semantic_rejection_cannot_trigger_a_third_generation(self):
        result, transport, reviews, outputs, attempts, raw = self.run_candidates(
            [self.candidate(True), self.candidate()], runtime='pi', verdict='unsupported')
        self.assertEqual('summary_support_rejected', result.get('reason_code'), result)
        self.assertEqual(2, len(transport.sent))
        self.assertEqual(1, len(reviews))
        self.assertEqual(44, attempts[0]['total_tokens'])
        self.assert_no_partial_write(outputs, raw)

    def test_shared_deadline_expiry_after_structural_feedback_keeps_measured_usage_without_second_dispatch(self):
        for runtime in ('codex', 'pi'):
            with self.subTest(runtime=runtime):
                self.setUp()
                result, transport, reviews, outputs, attempts, raw = self.run_candidates(
                    [self.candidate(True), self.candidate()], runtime=runtime, expire_on_diagnostic=True)
                self.assertEqual('timeout', result.get('code'), result)
                self.assertEqual(1, len(transport.sent))
                self.assertEqual([], reviews)
                self.assert_no_partial_write(outputs, raw)
                self.assertEqual(12, attempts[0]['total_tokens'])
                calls = json.loads(attempts[0]['execution_calls'])
                self.assertEqual({'generation'}, set(calls))
                self.assertEqual('invalid', calls['generation']['review_diagnostic']['status'])
                self.assertEqual('missing_required_summary', result['generation_correction']['initial_reason_code'])

    def test_existing_semantic_correction_still_uses_one_allowance_and_all_review_layers(self):
        complete, corrected = self.candidate(), self.candidate()
        corrected['session_summary']['completed'] = 'The report says the donated atlas was recorded in the inventory.'
        result, transport, reviews, outputs, attempts, raw = self.run_candidates(
            [complete, corrected], verdict=['unsupported', 'supported'])
        self.assertEqual('processed', result['status'], result)
        self.assertEqual(2, len(transport.sent))
        self.assertEqual(['summary-support-v6', 'summary-support-v6', 'summary-source-v4', 'summary-outcome-v5'],
                         [request.context['version'] for request in reviews])
        self.assertEqual(104, attempts[0]['total_tokens'])
        self.assertNotIn('generation_correction', result)
        self.assertEqual(2, len(outputs))

    def test_exact_native_raw_trace_and_safe_durable_readback_preserve_first_problem(self):
        for runtime in ('codex', 'pi'):
            with self.subTest(runtime=runtime):
                self.setUp()
                missing, complete = self.candidate(True), self.candidate()
                result, transport, reviews, outputs, attempts, raw = self.run_candidates(
                    [missing, complete], runtime=runtime)
                self.assertEqual('processed', result['status'], result)
                self.assertEqual(transport.raw, transport.delivered_raw)
                self.assertEqual([missing, complete], [json.loads(text)['result'] for text in transport.delivered_raw])
                self.assertIn(12, transport.checkpoints)
                self.assertIn(24, transport.checkpoints)
                calls = json.loads(attempts[0]['execution_calls'])
                detail = calls['generation']['review_diagnostic']
                self.assertEqual({'version','candidate_fingerprint','evidence_fingerprint','status','invalid_subtype'}, set(detail))
                self.assertEqual(('invalid','structure'), (detail['status'], detail['invalid_subtype']))
                self.assertEqual('missing_required_summary', result['generation_correction']['initial_reason_code'])
                self.assertNotIn('initial_reason_code', detail)
                canonical = copy.deepcopy(missing)
                canonical['notes'][0]['source_ids'] = [self.raw['id']]
                parsed_digest = hashlib.sha256(json.dumps(canonical, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
                self.assertEqual(parsed_digest, detail['candidate_fingerprint'])
                self.assertNotEqual(hashlib.sha256(transport.raw[0].encode()).hexdigest(), parsed_digest)
                self.assertNotIn(self.body, json.dumps(calls))
                self.assertEqual(84, attempts[0]['total_tokens'])
                print('PUBLIC_SYNTHETIC_TRACE '+json.dumps(dict(runtime=runtime,
                    exact_delivered_raw=transport.delivered_raw, generation_checkpoints=transport.checkpoints,
                    returned_generation_correction=result['generation_correction'], durable_calls=calls,
                    total_tokens=attempts[0]['total_tokens']), sort_keys=True))

    def test_malformed_json_still_fails_without_structural_feedback(self):
        raw = '{"result": '
        result, transport, reviews, outputs, attempts, source = self.run_candidates([raw])
        self.assertEqual('invalid_json', result.get('reason_code'), result)
        self.assertEqual([raw], transport.delivered_raw)
        self.assertEqual(1, len(transport.sent))
        self.assertEqual([], reviews)
        self.assertNotIn('generation_correction', result)
        self.assert_no_partial_write(outputs, source)
