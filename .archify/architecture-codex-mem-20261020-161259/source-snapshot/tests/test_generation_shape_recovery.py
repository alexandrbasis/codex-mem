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
    def __init__(self, candidates, unknown_usage=()):
        self.unknown_usage = unknown_usage
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
        if number in self.unknown_usage:
            self.events = [event for event in self.events if event['method'] != 'thread/tokenUsage/updated']
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
    def __init__(self, candidates, profile, deadline, run, unknown_usage=()):
        super().__init__(candidates, unknown_usage)
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
        if number in self.unknown_usage:
            next(event['message'] for event in self.events if event.get('message', {}).get('role') == 'assistant').pop('usage')
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

    def run_candidates(self, candidates, *, verdict='supported', runtime='codex', expire_on_diagnostic=False,
                       on_generation_failure=None, unknown_usage=(), reject_unit='completed', quality_evaluator=None):
        transport = InventoryTransport(candidates, unknown_usage)
        reviews = []
        support_number = 0
        if runtime == 'pi':
            configure(self.data, processing_profile=dict(runtime='pi', provider='fixture', model='inventory-fixture',
                                                       reasoning_effort='low', timeout=240))
        if quality_evaluator is not None:
            configure(self.data, jev_quality_enabled=True)
        clock = [time.monotonic()]
        controlled_time = SimpleNamespace(monotonic=lambda:clock[0], time=time.time)
        class Reviewer:
            def execute(inner, request):
                reviews.append(request)
                output = supported_review(request.context)['output']
                nonlocal support_number
                if request.context['version'] == 'summary-support-v7':
                    chosen = verdict[support_number] if isinstance(verdict, list) else verdict
                    support_number += 1
                    if chosen != 'supported':
                        next(row for row in support_rows(output) if row['unit_id']==reject_unit)['verdict'] = chosen
                profile = request.profile
                return ExecutionResult(output=output, identity=ExecutionIdentity(profile.runtime, profile.provider,
                    profile.model, profile.reasoning_effort, f'inventory-review-{len(reviews)}', 'review-turn'),
                    metrics=dict(duration_ms=1, usage=dict(status='reported', source='app_server_thread_total', updates=1,
                        tokens=dict(input_tokens=16, output_tokens=4, total_tokens=20, cached_input_tokens=0,
                                    cache_write_input_tokens=0, reasoning_output_tokens=0))))
        @contextlib.contextmanager
        def prepared(pi, profile, deadline, run, **kwargs):
            nonlocal transport
            transport = PiInventoryTransport(candidates, profile, deadline, run, unknown_usage)
            run.version_verified = run.launched = True
            run.cwd = '/in-memory/library'
            run.state(transport.state(initial=True), initial=True)
            try:
                yield transport
            finally:
                transport.close()
        from codex_mem.observer_usage_store import record_execution_call
        checkpoints = []
        failures_seen = []
        def record(*args, **kwargs):
            value = record_execution_call(*args, **kwargs)
            metrics = kwargs.get('metrics') or {}
            tokens = metrics.get('usage', {}).get('tokens')
            if args[4] == 'generation' and tokens:
                checkpoints.append(tokens['total_tokens'])
            failure = next((row for row in metrics.get('generations', []) if row.get('reason_code') == 'invalid_json'), None)
            if failure and not failures_seen:
                failures_seen.append(copy.deepcopy(failure))
                if on_generation_failure:
                    on_generation_failure()
            if expire_on_diagnostic and (kwargs.get('review_diagnostic') or failure):
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
            result = processor.process_pending(self.project, self.data, summary_reviewer=Reviewer(),
                                               jev_quality_evaluator=quality_evaluator)
        with Store(self.data) as store:
            job = store.observation_job_status(self.project, result['job_id'])
            outputs = store.get(self.project, job['output_ids']) if job['output_ids'] else []
            attempts = store.observation_attempts(self.project, result['job_id'])
            raw_rows = store.get(self.project, [self.raw['id']])
            raw = raw_rows[0] if raw_rows else None
        transport.failures_seen = failures_seen
        transport.checkpoints = checkpoints
        return result, transport, reviews, outputs, attempts, raw

    def test_missing_summary_uses_existing_correction_then_full_review_and_atomic_commit(self):
        missing, complete = self.candidate(True), self.candidate()
        result, transport, reviews, outputs, attempts, raw = self.run_candidates([missing, complete])
        self.assertEqual('processed', result['status'], result)
        self.assertEqual(2, len(transport.sent))
        self.assertEqual(['summary-support-v7', 'summary-source-v4', 'summary-outcome-v6'],
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

    def test_empty_summary_can_use_only_the_existing_correction_in_both_runtimes(self):
        for runtime in ('codex', 'pi'):
            for replacement in ('supported', 'empty', 'unsupported'):
                with self.subTest(runtime=runtime, replacement=replacement):
                    self.setUp()
                    empty = self.candidate()
                    for field in ('investigated', 'learned', 'completed', 'next_steps', 'notes'):
                        empty['session_summary'][field] = ''
                    fixed = empty if replacement == 'empty' else self.candidate()
                    result, transport, reviews, outputs, attempts, raw = self.run_candidates(
                        [empty, fixed], runtime=runtime,
                        verdict='unsupported' if replacement == 'unsupported' else 'supported')
                    self.assertEqual(2, len(transport.sent))
                    self.assertEqual('empty_session_summary',
                                     result['generation_correction']['initial_reason_code'])
                    self.assertEqual(24, json.loads(attempts[0]['execution_calls'])['generation']['metrics']['total_tokens'])
                    if replacement == 'supported':
                        self.assertEqual('processed', result['status'], result)
                        self.assertEqual(2, len(outputs))
                        self.assertEqual(self.body, next(row for row in outputs if row['kind'] == 'note')['body'])
                        self.assertEqual(3, len(reviews))
                    else:
                        self.assert_no_partial_write(outputs, raw)
                        self.assertEqual('empty_session_summary' if replacement == 'empty'
                                         else 'summary_support_rejected', result.get('reason_code'), result)
                        self.assertEqual(0 if replacement == 'empty' else 1, len(reviews))

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
        self.assertEqual(['summary-support-v7'], [request.context['version'] for request in reviews])
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
        self.assertEqual(['summary-support-v7', 'summary-support-v7', 'summary-source-v4', 'summary-outcome-v6'],
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

    def test_malformed_json_remains_rejected_after_one_full_replacement(self):
        raw = '{"result": '
        result, transport, reviews, outputs, attempts, source = self.run_candidates([raw, raw])
        self.assertEqual('invalid_json', result.get('reason_code'), result)
        self.assertEqual([raw, raw], transport.delivered_raw)
        self.assertEqual(2, len(transport.sent))
        self.assertEqual([], reviews)
        self.assertEqual('invalid_json', result['generation_correction']['initial_reason_code'])
        self.assert_no_partial_write(outputs, source)


class GenerationJsonRecoveryTests(unittest.TestCase):
    """Public payload facts test control flow, not model judgment quality."""
    run_candidates = GenerationShapeRecoveryTests.run_candidates
    assert_no_partial_write = GenerationShapeRecoveryTests.assert_no_partial_write

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.project, self.data = self.root/'payload', self.root/'memory'
        self.project.mkdir()
        configure(self.data, capture_scope='all', service_enabled=False, semantic_enabled=False,
                  jev_filter_enabled=False, jev_quality_enabled=False)
        self.intent = 'Set the maximum payload to 4096 bytes, run validation checks and create a local bundle.'
        self.body = 'The payload limit is 4096 bytes, six validation checks passed, and the local bundle was created.'
        with Store(self.data) as store:
            store.remember(self.project, 'Payload request', self.intent,
                           source='hook:UserPromptSubmit', session_id='payload-session')
            store.remember(self.project, 'Payload operation',
                'Command: python tools/set_payload_limit_and_bundle.py\nApplied MAX_PAYLOAD_BYTES = 4096. '
                'Validation checks: 6 passed, 0 failed. Created dist/payload-demo.tar.gz locally. Exit code: 0.',
                source='hook:PostToolUse', session_id='payload-session')
            self.raw = store.remember(self.project, 'Payload report', self.body,
                                      source='hook:Stop', session_id='payload-session')

    def candidate(self, missing=False):
        summary = dict(title='Payload configuration', request=self.intent, investigated='', learned='',
            completed=self.body, next_steps='', notes='', source_ids=['s1','s2','s3'])
        note = dict(title='Payload configuration', body=self.body, tags=['payload'], source_ids=['s2','s3'],
            observation=dict(type='change', subtitle='4096-byte limit applied; six validation checks passed',
                narrative=self.body, facts=['A local bundle was created.'], concepts=['payload'],
                files_read=[], files_modified=[]))
        return dict(notes=[note], disposition='processed', session_summary=None if missing else summary)

    def malformed(self):
        # Independently remove the envelope delimiter; do not repair it in production.
        return json.dumps({'result': self.candidate()}, ensure_ascii=False)[:-1]

    def test_malformed_first_gets_one_full_replacement_with_original_failure_and_usage(self):
        for runtime in ('codex', 'pi'):
            with self.subTest(runtime=runtime):
                self.setUp()
                malformed, replacement = self.malformed(), self.candidate()
                result, transport, reviews, outputs, attempts, raw = self.run_candidates(
                    [malformed, replacement], runtime=runtime)
                self.assertEqual('processed', result['status'], result)
                self.assertEqual(2, len(transport.sent))
                self.assertEqual(malformed, transport.delivered_raw[0])
                self.assertEqual(replacement, json.loads(transport.delivered_raw[1])['result'])
                self.assertEqual(['summary-support-v7','summary-source-v4','summary-outcome-v6'],
                                 [request.context['version'] for request in reviews])
                note = next(row for row in outputs if row['kind']=='note')
                self.assertEqual(replacement['notes'][0]['observation'], note['observation'])
                self.assertEqual(self.body, note['body'])
                stored_summary = next(row for row in outputs if row['kind']=='session_summary')['session_summary']
                expected_summary = dict(title='Payload configuration', request=self.intent,
                    investigated=None, learned=None, completed=self.body, next_steps=None, notes=None)
                self.assertEqual(expected_summary, {field: stored_summary[field] for field in expected_summary})
                if runtime=='codex':
                    for field in ('model','effort','outputSchema'):
                        self.assertEqual(transport.sent[0][field], transport.sent[1][field])
                self.assertEqual(1, len({request.deadline for request in reviews}))
                self.assertIsNotNone(raw['superseded_by'])
                calls = json.loads(attempts[0]['execution_calls'])
                generation = calls['generation']
                self.assertEqual(24, generation['metrics']['total_tokens'])
                self.assertEqual(84, attempts[0]['total_tokens'])
                details = generation['generations']
                self.assertEqual([1,2], [row['number'] for row in details])
                self.assertEqual(['failed','success'], [row['outcome'] for row in details])
                self.assertEqual('invalid_json', details[0]['reason_code'])
                self.assertEqual('invalid_response', details[0]['error_code'])
                self.assertEqual('inventory-generation', details[0]['execution_id'])
                self.assertEqual('generation-1' if runtime=='pi' else 'inventory-turn-1', details[0]['turn_id'])
                self.assertEqual([12,12], [row['metrics']['total_tokens'] for row in details])
                self.assertEqual('invalid_json', result['generation_correction']['initial_reason_code'])
                self.assertNotIn(self.body, json.dumps(calls))
                self.assertTrue(transport.closed)

    def test_repeated_malformed_json_is_terminal_after_two_dispatches(self):
        for runtime in ('codex', 'pi'):
            with self.subTest(runtime=runtime):
                self.setUp()
                result, transport, reviews, outputs, attempts, raw = self.run_candidates(
                    [self.malformed()]*2, runtime=runtime)
                self.assertEqual('invalid_json', result.get('reason_code'), result)
                self.assertEqual(2, len(transport.sent))
                self.assertEqual([], reviews)
                self.assert_no_partial_write(outputs, raw)
                generation = json.loads(attempts[0]['execution_calls'])['generation']
                self.assertEqual(['invalid_json']*2, [row['reason_code'] for row in generation['generations']])
                self.assertEqual([12,12], [row['metrics']['total_tokens'] for row in generation['generations']])
                self.assertEqual(24, attempts[0]['total_tokens'])

    def test_replacement_cannot_get_another_shape_or_material_support_correction(self):
        for runtime in ('codex', 'pi'):
            for rejected in ('missing_summary', 'subtitle'):
                with self.subTest(runtime=runtime, rejected=rejected):
                    self.setUp()
                    replacement = self.candidate(missing=rejected=='missing_summary')
                    if rejected == 'subtitle':
                        replacement['notes'][0]['observation']['subtitle'] = '4096-byte limit verified by six validation checks'
                    # Authored support verdict checks routing only, never semantic quality.
                    result, transport, reviews, outputs, attempts, raw = self.run_candidates(
                        [self.malformed(), replacement], runtime=runtime,
                        verdict='unsupported' if rejected=='subtitle' else 'supported',
                        reject_unit='note:0:observation:subtitle')
                    self.assertEqual('missing_required_summary' if rejected=='missing_summary' else 'summary_support_rejected',
                                     result.get('reason_code'), result)
                    self.assertEqual(2, len(transport.sent))
                    self.assertEqual(0 if rejected=='missing_summary' else 1, len(reviews))
                    self.assert_no_partial_write(outputs, raw)
                    self.assertEqual('invalid_json', result['generation_correction']['initial_reason_code'])
                    self.assertEqual(24 if rejected=='missing_summary' else 44, attempts[0]['total_tokens'])

    def test_failure_checkpoint_precedes_custody_and_deadline_recheck(self):
        for runtime in ('codex', 'pi'):
            for changed in ('deadline', 'edited', 'deleted'):
                with self.subTest(runtime=runtime, changed=changed):
                    self.setUp()
                    def change_source():
                        with Store(self.data) as store:
                            if changed == 'deleted':
                                store.forget(self.project, [self.raw['id']])
                            elif changed == 'edited':
                                store._connection.execute('UPDATE entries SET body=? WHERE id=?',
                                                          ('Changed public source', self.raw['id']))
                                store._connection.commit()
                    result, transport, reviews, outputs, attempts, raw = self.run_candidates(
                        [self.malformed(), self.candidate()], runtime=runtime,
                        expire_on_diagnostic=changed=='deadline', on_generation_failure=change_source)
                    self.assertEqual(1, len(transport.sent), result)
                    self.assertEqual(1, len(transport.failures_seen))
                    self.assertEqual([], reviews)
                    self.assertEqual([], outputs)
                    self.assertEqual('timeout' if changed=='deadline' else 'invalid_response', result.get('code'), result)
                    if changed in ('edited', 'deleted'):
                        self.assertEqual('summary_support_stale', result.get('reason_code'))
                    self.assertEqual(12, attempts[0]['total_tokens'])
                    details = json.loads(attempts[0]['execution_calls'])['generation']['generations']
                    self.assertEqual(1, len(details))
                    self.assertEqual('invalid_json', details[0]['reason_code'])

    def test_per_generation_usage_keeps_unknown_boundaries_without_double_counting(self):
        for runtime in ('codex', 'pi'):
            for unknown in ((1,), (2,), (1,2)):
                with self.subTest(runtime=runtime, unknown=unknown):
                    self.setUp()
                    result, transport, reviews, outputs, attempts, raw = self.run_candidates(
                        [self.malformed(), self.candidate()], runtime=runtime, unknown_usage=unknown)
                    self.assertEqual('processed', result['status'], result)
                    generation = json.loads(attempts[0]['execution_calls'])['generation']
                    expected_details = ([None, None] if runtime=='codex' and 1 in unknown else
                                        [None if n in unknown else 12 for n in (1,2)])
                    self.assertEqual(expected_details, [row['metrics']['total_tokens'] for row in generation['generations']])
                    expected_total = (None if len(unknown)==2 else 24 if runtime=='codex' and unknown==(1,) else 12)
                    self.assertEqual(expected_total, generation['metrics']['total_tokens'])
                    self.assertEqual(None if expected_total is None else expected_total+60, attempts[0]['total_tokens'])
                    for row, total in zip(generation['generations'], expected_details):
                        if total is None:
                            self.assertEqual('unavailable', row['metrics']['usage_status'])

    def test_quality_failure_after_json_replacement_cannot_buy_another_generation(self):
        from tests.test_jev_quality import response
        for runtime in ('codex', 'pi'):
            for uncertain in (False, True):
                with self.subTest(runtime=runtime, uncertain=uncertain):
                    self.setUp()
                    def evaluate(payload):
                        return response(payload, .5 if uncertain else .01, .5 if uncertain else .99)
                    result, transport, reviews, outputs, attempts, raw = self.run_candidates(
                        [self.malformed(), self.candidate()], runtime=runtime, quality_evaluator=evaluate)
                    self.assertEqual('jev_quality_uncertain' if uncertain else 'jev_quality_rejected',
                                     result.get('reason_code'), result)
                    self.assertEqual(2, len(transport.sent))
                    self.assert_no_partial_write(outputs, raw)
                    self.assertEqual('invalid_json', result['generation_correction']['initial_reason_code'])

    def test_valid_shape_correction_then_malformed_cannot_reset_the_allowance(self):
        for runtime in ('codex', 'pi'):
            with self.subTest(runtime=runtime):
                self.setUp()
                result, transport, reviews, outputs, attempts, raw = self.run_candidates(
                    [self.candidate(True), self.malformed()], runtime=runtime)
                self.assertEqual('invalid_json', result.get('reason_code'), result)
                self.assertEqual(2, len(transport.sent))
                self.assertEqual('missing_required_summary', result['generation_correction']['initial_reason_code'])
                self.assert_no_partial_write(outputs, raw)
                self.assertEqual(24, attempts[0]['total_tokens'])
