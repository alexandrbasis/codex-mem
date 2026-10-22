"""Generic toy anchors only. No runtime, helper, models or prior corpora."""
from copy import deepcopy
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

spec = importlib.util.spec_from_file_location('author_contract', Path(__file__).with_name('public-fixture-contract.py'))
contract = importlib.util.module_from_spec(spec)
spec.loader.exec_module(contract)


def toy():
    histories, questions = [], {}
    for number in range(16):
        hid = f'toy_{number}'
        events = [
            {'id': 'old', 'type': 'assistant', 'text': 'Old note.'},
            {'id': 'request', 'type': 'user', 'text': 'Keep the sample label.'},
            {'id': 'report', 'type': 'assistant', 'text': 'The sample is ready.'},
            {'id': 'check', 'type': 'tool', 'text': 'Sample check passed.', 'tool_capture': {
                'tool_name': 'exec_command', 'tool_input': {'command': 'inert-sample-check'},
                'tool_response': {'output': 'Sample check passed.', 'exit_code': 0}}},
            {'id': 'delete', 'type': 'delete_observation', 'text': 'Remove the old note.',
             'observation_event_id': 'old'},
            {'id': 'tail', 'type': 'user', 'text': 'Review is still pending.'},
        ]
        histories.append({'id': hid, 'project_keys': ['sample', 'other'],
            'sessions': [{'id': 'session', 'project_key': 'sample', 'events': events,
                          'checkpoints': [{'after_event_id': 'check'}]}],
            'questions': [{'id': f'q{i}', 'question': 'What does the sample record say?',
                           'project_key': 'sample', 'stage': 'final'} for i in range(6)]})
        for i, event in enumerate([events[1], events[2], events[3], events[5], events[1], events[2]]):
            questions[f'{hid}/q{i}'] = {'category': 'toy', 'required_facts': [{
                'text': 'A recorded statement.', 'support': [{'event_id': event['id'],
                    'start': 0, 'end': len(event['text']), 'quote': event['text']}]}],
                'forbidden_conclusions': ['The removed old note remains an active source.'],
                'forbidden_inferences': [{'error_type': 'deleted_resurrection',
                    'text': 'Treating the removed note as a current surviving source.'}]}
    return {'schema_version': 1, 'fictional': True, 'split': 'holdout', 'histories': histories}, {
        'schema_version': 1, 'fictional': True, 'questions': questions}


class AuthorContractTests(unittest.TestCase):
    def setUp(self):
        self.fixture, self.gold = toy()

    def validate(self):
        with tempfile.TemporaryDirectory() as directory:
            fp, gp = Path(directory) / 'fixture.json', Path(directory) / 'gold.json'
            fp.write_text(json.dumps(self.fixture))
            gp.write_text(json.dumps(self.gold))
            return contract.validate_files(fp, gp)

    def anchor(self):
        return self.gold['questions']['toy_0/q0']['required_facts'][0]['support'][0]

    def test_surviving_roles_sixteen_deletions_and_all_ninety_six_questions(self):
        result = self.validate()
        self.assertEqual((16, 96, 16), (result['histories'], result['questions'], result['deletion_controls']))
        self.assertEqual({'user_intent', 'assistant_report', 'tool_record'}, {a['source_role'] for a in result['anchors']})

    def test_deletion_control_is_not_a_positive_source(self):
        self.anchor().update(event_id='delete', start=0, end=20, quote='Remove the old note.')
        with self.assertRaisesRegex(ValueError, 'actions, not positive'):
            self.validate()

    def test_deleted_target_cannot_be_positive_but_negative_test_remains(self):
        self.anchor().update(event_id='old', start=0, end=9, quote='Old note.')
        with self.assertRaisesRegex(ValueError, 'deleted source'):
            self.validate()
        self.assertIn('removed old note', self.gold['questions']['toy_0/q0']['forbidden_conclusions'][0])

    def test_late_source_after_last_checkpoint_is_visible_at_final_settle(self):
        result = self.validate()
        anchor = next(a for a in result['anchors'] if a['case'] == 'toy_0/q3')
        self.assertEqual({'stage': 'final_settle'}, anchor['first_processing_opportunity'])
        self.assertEqual('final', anchor['question_visibility_stage'])

    def test_cross_project_positive_is_rejected_unless_question_selects_both(self):
        question = self.fixture['histories'][0]['questions'][0]
        question['project_key'] = 'other'
        with self.assertRaisesRegex(ValueError, 'selected question project'):
            self.validate()
        del question['project_key']
        question['project_keys'] = ['sample', 'other']
        self.validate()

    def test_bad_offsets_quotes_and_nonstring_field_are_rejected(self):
        original = deepcopy(self.anchor())
        for update in ({'start': True}, {'end': 999}, {'quote': 'Different text.'},
                       {'source_field': 'tool_response.exit_code'}):
            with self.subTest(update=update):
                self.anchor().clear()
                self.anchor().update(original, **update)
                with self.assertRaises(ValueError):
                    self.validate()

    def test_tool_canonical_string_field_and_same_source_mirror(self):
        support = self.gold['questions']['toy_0/q2']['required_facts'][0]['support'][0]
        support['reader_visible_equivalent'] = {'source_field': 'tool_response.output', 'start': 0, 'end': 20}
        self.validate()
        support['source_field'] = 'tool_response.output'
        self.validate()
        support['reader_visible_equivalent']['event_id'] = 'report'
        with self.assertRaisesRegex(ValueError, 'another source'):
            self.validate()

    def test_exact_fixture_cardinality_checkpoint_and_stage_parser_retained(self):
        self.fixture['histories'][0]['questions'][0]['stage'] = 'checkpoint'
        with self.assertRaisesRegex(ValueError, 'only final'):
            self.validate()
        self.fixture['histories'][0]['questions'][0]['stage'] = 'final'
        self.fixture['histories'][0]['sessions'][0]['checkpoints'][0]['after_event_id'] = 'unknown'
        with self.assertRaisesRegex(ValueError, 'checkpoint'):
            self.validate()

    def test_author_holdout_requires_exactly_sixteen_deletion_controls(self):
        self.fixture['histories'][0]['sessions'][0]['events'].pop(4)
        with self.assertRaisesRegex(ValueError, 'sixteen deletion'):
            self.validate()

    def test_recognized_execution_output_must_equal_event_text(self):
        self.fixture['histories'][0]['sessions'][0]['events'][3]['tool_capture']['tool_response']['output'] = 'Different output.'
        with self.assertRaisesRegex(ValueError, 'output must equal event text'):
            self.validate()

    def test_checkpoint_must_name_an_event_in_its_own_session(self):
        session = self.fixture['histories'][0]['sessions'][0]
        tail = session['events'].pop()
        self.fixture['histories'][0]['sessions'].append({'id': 'later', 'project_key': 'sample',
            'events': [tail], 'checkpoints': [{'after_event_id': 'check'}]})
        with self.assertRaisesRegex(ValueError, 'own session'):
            self.validate()

    def test_checkpoint_list_must_follow_session_event_order(self):
        self.fixture['histories'][0]['sessions'][0]['checkpoints'] = [
            {'after_event_id': 'check'}, {'after_event_id': 'request'}]
        with self.assertRaisesRegex(ValueError, 'event order'):
            self.validate()

    def test_deletion_negative_prose_does_not_replace_typed_coverage(self):
        for i in range(6):
            self.gold['questions'][f'toy_0/q{i}'].pop('forbidden_inferences')
        with self.assertRaisesRegex(ValueError, 'typed deleted_resurrection'):
            self.validate()

    def test_missing_unknown_or_outside_question_gold_is_rejected(self):
        self.anchor()['event_id'] = 'unknown'
        with self.assertRaisesRegex(ValueError, 'unknown event'):
            self.validate()
        self.gold['questions'].pop('toy_0/q0')
        with self.assertRaisesRegex(ValueError, 'exactly'):
            self.validate()


if __name__ == '__main__':
    unittest.main()
