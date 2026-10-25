"""Prompt delivery and packet policy tests, not semantic model evaluations."""
import unittest

from codex_mem.execution import execute_codex
from codex_mem.processor import process_pending
from tests import test_packet_support as packet_fixtures
from tests import test_summary_support as fixtures

supported_review = fixtures.supported_review


class ObservationContractTests(unittest.TestCase):
    setUp = fixtures.SummarySupportTests.setUp
    stored = fixtures.SummarySupportTests.stored
    seed_tool = packet_fixtures.PacketSupportTests.seed_tool
    generate_note = packet_fixtures.PacketSupportTests.generate_note

    def test_generation_and_review_receive_the_same_category_rules(self):
        self.seed_tool()
        prompts = {}
        expected_rules = (
            "application classification inferred from the evidenced work or finding",
            "need not literally assign or name the category",
            "A finding or observed evidence limitation can be discovery without completion",
            "bugfix requires evidence of an actual correction",
            "decision requires an accepted choice with rationale",
            "feature, refactor and change require an observed change",
            "Generic topic and concept labels require topical fit",
            "completion, termination, failure, identity, quantity",
            "No material field is exempt from factual support",
        )
        class Reviewer:
            def execute(self, request):
                prompts['review'] = request.prompt
                return execute_codex(request, supported_review)
        def generate(request):
            prompts['generation'] = request['prompt']
            candidate = self.generate_note(request)
            note = candidate['output']['notes'][0]
            note['title'] = 'Operation checkpoint; final status unknown'
            note['observation']['subtitle'] = 'Final status unknown'
            return candidate
        result = process_pending(self.project, self.data, runner=generate, summary_reviewer=Reviewer())
        self.assertEqual('processed', result['status'], result)
        for prompt in prompts.values():
            for rule in expected_rules:
                self.assertIn(rule, prompt)
        from codex_mem.observation_contract import OBSERVATION_CLASSIFICATION_CONTRACT
        for prompt in prompts.values():
            self.assertEqual(1, prompt.count(OBSERVATION_CLASSIFICATION_CONTRACT))
        self.assertEqual(1, len(self.stored(result)[0]))

    def adverse_note(self, field, value, verdict):
        # Each input has its own queue. Canned verdicts exercise rejection policy only.
        from codex_mem.config import configure
        self.data = self.data.parent / (field.replace(':', '-') + '-' + str(value).replace('/', '-') + '-' + verdict)
        configure(self.data, capture_scope='all', service_enabled=False, semantic_enabled=False,
                  jev_filter_enabled=False, jev_quality_enabled=False)
        self.seed_tool()
        seen = []
        def generate(request):
            result = self.generate_note(request)
            note = result['output']['notes'][0]
            note['title'] = 'Operation checkpoint; final status unknown'
            note['observation']['subtitle'] = 'Final status unknown'
            if field in ('title', 'body', 'tags'):
                note[field] = value
            else:
                note['observation'][field] = value
            return result
        unit_id = 'note:0:' + (field if field in ('title', 'body', 'tags') else 'observation:' + field)
        def review(request):
            self.assertEqual(value, request['units'][unit_id])
            seen.append(request['version'])
            result = supported_review(request)
            row = next(row for row in result['output']['rows'] if row['unit_id'] == unit_id)
            row.update(candidate_assertion='The material field implies a final outcome or accepted change.',
                       evidence_assertion='The checkpoint establishes partial progress with final status unknown.',
                       value_comparison='The definitive outcome or accepted change is not established.',
                       attribution_comparison='The tool reports a bounded checkpoint.',
                       scope_comparison='The final result lies outside the captured checkpoint.', verdict=verdict)
            return result
        result = process_pending(self.project, self.data, runner=generate, summary_reviewer=review)
        expected = 'summary_support_uncertain' if verdict == 'insufficient_context' else 'summary_support_rejected'
        self.assertEqual(expected, result.get('reason_code'), result)
        self.assertEqual([], self.stored(result)[0])
        self.assertEqual(['summary-support-v5'], seen)
        self.assertEqual('uncertain' if verdict == 'insufficient_context' else 'rejected',
                         result['summary_support'][0]['status'])

    def test_adverse_status_in_every_material_note_field_still_blocks_storage(self):
        fields = dict(title='Operation completed', body='The process terminated.',
                      tags=['checkpoint', 'completed'], subtitle='Operation completed',
                      narrative='The process terminated.', facts=['The operation completed.'],
                      concepts=['process terminated'])
        for field, value in fields.items():
            for verdict in ('unsupported', 'contradicted', 'insufficient_context'):
                with self.subTest(field=field, verdict=verdict):
                    self.adverse_note(field, value, verdict)

    def test_category_classification_cannot_excuse_unobserved_corrections_choices_or_changes(self):
        for category in ('bugfix', 'decision', 'feature', 'refactor', 'change'):
            with self.subTest(category=category):
                self.adverse_note('type', category, 'unsupported')


class SummaryScopeContractTests(unittest.TestCase):
    setUp = fixtures.SummarySupportTests.setUp
    stored = fixtures.SummarySupportTests.stored

    def packet_review(self, kind):
        from codex_mem.config import configure
        from codex_mem.store import Store
        self.data = self.data.parent / kind
        configure(self.data, capture_scope='all', service_enabled=False, semantic_enabled=False,
                  jev_filter_enabled=False, jev_quality_enabled=False)
        body = 'The assistant reported a supported result; independent verification was not supplied.'
        with Store(self.data) as store:
            store.remember(self.project, 'Report', body,
                source='hook:PostToolUse' if kind == 'note-only' else 'hook:Stop', session_id='session')
        note = dict(title='Reported result', body=body, tags=['report'], source_ids=['s1'],
            observation=dict(type='discovery', subtitle='Reported result', narrative=body,
                facts=[body], concepts=['report'], files_read=[], files_modified=[]))
        summary = dict(title='Result report', request='', investigated='', learned='',
            completed=body, next_steps='', notes='', source_ids=['s1'])
        candidate = dict(disposition='processed', notes=[] if kind == 'summary-only' else [note],
                         session_summary=None if kind == 'note-only' else summary)
        seen = []
        class Reviewer:
            def execute(self, request):
                seen.append(request)
                return execute_codex(request, supported_review)
        result = process_pending(self.project, self.data, runner=lambda request: fixtures.receipt(candidate),
                                 summary_reviewer=Reviewer())
        self.assertEqual('processed', result['status'], result)
        support = next(request for request in seen if request.context['version'] == 'summary-support-v5')
        # Exact material values, complete source text, and strict schema remain at the public boundary.
        expected = {}
        if kind != 'note-only':
            expected.update(title='Result report', completed=body)
        if kind != 'summary-only':
            expected.update({'note:0:title':'Reported result', 'note:0:body':body,
                'note:0:tags':['report'], 'note:0:observation:type':'discovery',
                'note:0:observation:subtitle':'Reported result', 'note:0:observation:narrative':body,
                'note:0:observation:facts':[body], 'note:0:observation:concepts':['report']})
        self.assertEqual(expected, support.context['units'])
        self.assertEqual(body, support.context['evidence'][0]['body'])
        schema = support.output_schema
        self.assertFalse(schema['additionalProperties'])
        self.assertEqual(['version','rows'], schema['required'])
        self.assertEqual(len(expected), schema['properties']['rows']['minItems'])
        self.assertEqual(min(64, len(expected) * 8), schema['properties']['rows']['maxItems'])
        self.assertEqual(set(expected), set(schema['properties']['rows']['items']['properties']['unit_id']['enum']))
        return support

    def test_category_rules_are_scoped_to_packets_with_material_note_units(self):
        from codex_mem.observation_contract import OBSERVATION_CLASSIFICATION_CONTRACT
        for kind, count in [('summary-only',0), ('note-only',1), ('mixed',1)]:
            with self.subTest(kind=kind):
                request = self.packet_review(kind)
                self.assertEqual(count, request.prompt.count(OBSERVATION_CLASSIFICATION_CONTRACT))


    def test_general_support_criteria_reach_the_public_review_request(self):
        request = self.packet_review('summary-only')
        for rule in (
            'Judge the exact candidate assertion as written',
            'A neutral title can name the topic of a supplied request or report',
            'without asserting a completed or verified outcome',
            'Explicit completion, status, verification and report-text attributes in titles still need evidence',
            'An attributed report of X is supported when the actual source reports X',
            'with the same meaning, value, attribution, scope and certainty',
            'even if underlying X has no independent verification',
            'Do not add attribution or weaken certainty to rescue an unqualified factual claim',
            'Report language, quantity, organization and quoted content must match the actual supplied report text',
            'never user intent',
        ):
            self.assertIn(rule, request.prompt)
