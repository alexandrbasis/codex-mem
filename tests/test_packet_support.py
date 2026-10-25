"""Public packet grounding controls. Injected verdicts test wiring, not semantics."""
import copy
import unittest
from codex_mem.processor import process_pending
from codex_mem.store import Store
from tests import test_summary_support as fixtures
receipt = fixtures.receipt
supported_review = fixtures.supported_review


class PacketSupportTests(unittest.TestCase):
    setUp = fixtures.SummarySupportTests.setUp
    stored = fixtures.SummarySupportTests.stored

    def seed_tool(self):
        with Store(self.data) as store:
            self.raw = store.remember(self.project, 'Tool checkpoint',
                'The operation reported 6 of 10 items processed. The stream disconnected; final status is unknown.',
                source='hook:PostToolUse', session_id='session')

    def generate_note(self, request):
        return receipt(dict(disposition='processed', session_summary=None, notes=[dict(
            title='Operation terminated', body='Six items were reported processed; final status is unknown.',
            tags=['checkpoint'], source_ids=[request['sources'][0]['id']], observation=dict(
                type='discovery', subtitle='Operation stopped', narrative='The captured stream disconnected.',
                facts=['Six of ten items were processed.'], concepts=['progress'],
                files_read=[], files_modified=[]))]))

    def test_note_only_false_title_cannot_bypass_mandatory_review(self):
        self.seed_tool()
        def review(request):
            result = supported_review(request)
            for row in result['output']['rows']:
                if row['unit_id'] == 'note:0:title': row['verdict'] = 'unsupported'
            return result
        result = process_pending(self.project, self.data, runner=self.generate_note, summary_reviewer=review)
        self.assertEqual('summary_support_rejected', result.get('reason_code'), result)
        self.assertEqual([], self.stored(result)[0])

    def test_supported_tool_only_note_remains_useful_and_arrays_are_exact(self):
        self.seed_tool()
        observed = []
        def generate(request):
            result = self.generate_note(request)
            result['output']['notes'][0].update(title='Operation checkpoint')
            result['output']['notes'][0]['observation']['subtitle'] = 'Final status unknown'
            return result
        def review(request):
            observed.append(request)
            self.assertEqual(['Six of ten items were processed.'], request['units']['note:0:observation:facts'])
            self.assertEqual(['checkpoint'], request['units']['note:0:tags'])
            return supported_review(request)
        result = process_pending(self.project, self.data, runner=generate, summary_reviewer=review)
        self.assertEqual('processed', result['status'], result)
        self.assertEqual('Operation checkpoint', self.stored(result)[0][0]['title'])
        self.assertEqual(1, len(observed))
        self.assertEqual({'note:0:title','note:0:body','note:0:tags','note:0:observation:type',
            'note:0:observation:subtitle','note:0:observation:narrative','note:0:observation:facts',
            'note:0:observation:concepts'}, set(observed[0]['units']))

    def test_note_only_evidence_change_at_commit_is_atomically_fenced(self):
        from unittest import mock
        self.seed_tool()
        original = Store.finish_observation_batch
        def finish(store, *args, **kwargs):
            # Mutate after Python prechecks but before the storage transaction.
            store._connection.execute('UPDATE entries SET body=? WHERE id=?',
                ('Changed after review', self.raw['id']))
            store._connection.commit()
            return original(store, *args, **kwargs)
        with mock.patch.object(Store, 'finish_observation_batch', finish):
            result = process_pending(self.project, self.data, runner=self.generate_note,
                summary_reviewer=supported_review)
        self.assertEqual('summary_support_stale', result.get('reason_code'), result)
        self.assertEqual([], self.stored(result)[0])

    def test_supported_summary_does_not_cover_unsupported_note_subtitle(self):
        self.seed_tool()
        with Store(self.data) as store:
            self.raw = store.remember(self.project, 'Report', 'Six items were processed; final status unknown.',
                source='hook:Stop', session_id='session')
        def generate(request):
            result = fixtures.SummarySupportTests.generate(self, request)
            result['output']['notes'] = self.generate_note(request)['output']['notes']
            return result
        def review(request):
            result = supported_review(request)
            for row in result['output']['rows']:
                if row['unit_id'] == 'note:0:observation:subtitle': row['verdict'] = 'unsupported'
            return result
        result = process_pending(self.project, self.data, runner=generate, summary_reviewer=review)
        self.assertEqual('summary_support_rejected', result.get('reason_code'), result)
        self.assertEqual([], self.stored(result)[0])


    def test_reordering_or_editing_another_note_cannot_reset_unchanged_rejection(self):
        from unittest import mock
        from codex_mem.processor import NativeProcessorRunner
        self.seed_tool()
        calls = []
        def review(request):
            calls.append(request['version'])
            result = supported_review(request)
            if len(calls) == 1:
                next(r for r in result['output']['rows'] if r['unit_id'] == 'note:0:title')['verdict'] = 'unsupported'
            return result
        def generate(instance, request):
            candidate = self.generate_note(request)['output']
            bad = candidate['notes'][0]
            other = dict(copy.deepcopy(bad), title='Reported checkpoint')
            candidate['notes'].append(other)
            self.assertIsInstance(instance.candidate_review(candidate, 240), str)
            other['body'] = 'Changed unrelated note while keeping the rejected record.'
            candidate['notes'] = [other, bad]
            instance.candidate_review(candidate, 240)
            return receipt(candidate)
        with mock.patch.object(NativeProcessorRunner, 'check_profile'), mock.patch.object(NativeProcessorRunner, 'run', generate):
            result = process_pending(self.project, self.data, summary_reviewer=review)
        self.assertEqual('summary_support_rejected', result.get('reason_code'), result)
        self.assertEqual(['summary-support-v5'], calls)
        self.assertEqual([], self.stored(result)[0])

    def test_changed_rejected_note_can_use_the_single_correction(self):
        from unittest import mock
        from codex_mem.processor import NativeProcessorRunner
        self.seed_tool()
        calls = []
        def review(request):
            calls.append(request['version'])
            result = supported_review(request)
            if len(calls) == 1:
                next(r for r in result['output']['rows'] if r['unit_id'] == 'note:0:title')['verdict'] = 'unsupported'
            return result
        def generate(instance, request):
            candidate = self.generate_note(request)['output']
            self.assertIsInstance(instance.candidate_review(candidate, 240), str)
            candidate['notes'][0]['title'] = 'Reported checkpoint with unknown final status'
            candidate['notes'][0]['observation']['subtitle'] = 'Final status unknown'
            self.assertIsNone(instance.candidate_review(candidate, 240))
            return receipt(candidate)
        with mock.patch.object(NativeProcessorRunner, 'check_profile'), mock.patch.object(NativeProcessorRunner, 'run', generate):
            result = process_pending(self.project, self.data, summary_reviewer=review)
        self.assertEqual('processed', result['status'], result)
        self.assertEqual(['summary-support-v5'] * 2, calls)
        self.assertEqual('Reported checkpoint with unknown final status', self.stored(result)[0][0]['title'])


class SourceInputBoundaryTests(unittest.TestCase):
    def test_source_interpretation_prompt_contains_only_body_as_evidence(self):
        from codex_mem.summary_outcome import prepare_source
        body = 'A report with a list.\n- One observed fact.'
        prompt, _, payload = prepare_source(dict(sources=[dict(id='PRIVATE_ID', source='hook:Stop',
            body=body, session_id='PRIVATE_SESSION', turn_id='PRIVATE_TURN', created_at='PRIVATE_TIME')]))
        self.assertIn(body, prompt)
        for value in ('PRIVATE_ID', 'PRIVATE_SESSION', 'PRIVATE_TURN', 'PRIVATE_TIME', 'current_index', 'provenance'):
            self.assertNotIn(value, prompt)
        self.assertEqual('PRIVATE_ID', payload['report']['provenance']['id'])

    def test_support_rejects_same_text_span_owned_by_another_field(self):
        from codex_mem.summary_support import prepare, validate, SupportError
        summary = dict(title='Same text', completed='Same text')
        _, _, payload = prepare(summary, dict(sources=[dict(id='raw',source='hook:Stop',body='Same text')]))
        output = supported_review(payload)['output']
        next(row for row in output['rows'] if row['unit_id']=='title')['candidate_span_ids'] = ['completed:1']
        with self.assertRaises(SupportError) as caught: validate(output,payload)
        self.assertEqual('unknown_span',caught.exception.subtype)


class PacketContractLimitsTests(unittest.TestCase):
    def test_all_47_material_fields_fit_but_65_comparisons_fail_closed(self):
        from codex_mem.summary_support import prepare, validate, FIELDS, SupportError
        summary = {field: 'One supported assertion.' for field in FIELDS}
        note = dict(title='Topic', body='One result.', tags=['result'], observation=dict(type='discovery',
            subtitle='Result', narrative='One result.', facts=['One result.'], concepts=['result'],
            files_read=['read.txt'], files_modified=['modified.txt']))
        claim = dict(sources=[dict(id='raw', body='One result.', source='hook:PostToolUse')])
        _, schema, payload = prepare(summary, claim, [copy.deepcopy(note) for _ in range(4)])
        self.assertEqual(47, len(payload['units']))
        self.assertEqual(64, schema['properties']['rows']['maxItems'])
        output = supported_review(payload)['output']
        output['rows'].extend(copy.deepcopy(output['rows'][i]) for i in range(17))
        self.assertEqual((), validate(output, payload).issues)
        output['rows'].append(copy.deepcopy(output['rows'][20]))
        with self.assertRaises(SupportError): validate(output, payload).issues
        output = supported_review(payload)['output']
        output['rows'][-1]['unit_id'] = output['rows'][0]['unit_id']
        with self.assertRaises(SupportError): validate(output, payload).issues
        for version in ('summary-support-v1', 'summary-support-v2', 'summary-support-v3', 'summary-source-v1', 'summary-source-v2', 'summary-outcome-v1', 'summary-outcome-v2'):
            from codex_mem.summary_support import diagnostic, safe_diagnostic
            self.assertEqual(version, safe_diagnostic(diagnostic(summary, claim, version=version))['version'])
