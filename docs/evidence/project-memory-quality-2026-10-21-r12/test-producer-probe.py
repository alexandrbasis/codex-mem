"""Offline real-Store producer probe checks. No native or model dispatch."""
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest
from copy import deepcopy

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
SPEC = importlib.util.spec_from_file_location('public_producer_probe', Path(__file__).with_name('prepare-producer-probe.py'))
probe = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(probe)
from codex_mem import episode_processor as ep
from codex_mem.project_memory import ProjectMemory
from codex_mem.store import Store


class ProducerProbeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir='/private/tmp')
        self.addCleanup(self.temp.cleanup)
        self.plan = probe.prepare(Path(self.temp.name) / 'probe')

    def run_stub(self, mode='valid'):
        self.outputs, self.requests = [], []
        def runner(request):
            self.requests.append(request)
            supplied = json.loads(request['prompt'].split('\n', 1)[1])
            self.assertEqual(request['output_schema_version'], 'project-memory-output-v12')
            target = supplied['current_state']['claims'][0]['id']
            self.assertEqual(target, self.plan['seed_claim_id'])
            event = next(e for e in supplied['events'] if e['id'] == self.plan['current_source_id'])
            span = event['spans'][0]
            claim = {'text': span['text'], 'kind': 'requirement', 'workflow_state': 'open', 'rationale': '',
                     'scope': {'environment': '', 'version': ''},
                     'evidence': {'text': [span['ref']], 'workflow_state': [span['ref']], 'rationale': []},
                     'outgoing_relations': [{'type': 'replaces', 'to_claim_id': target, 'evidence': [span['ref']]}]}
            if mode == 'contradicts': claim['outgoing_relations'][0]['type'] = 'contradicts'
            if mode == 'empty_relation': claim['outgoing_relations'] = []
            if mode == 'bad_evidence': claim['outgoing_relations'][0]['evidence'] = ['forged-ref']
            claims = [claim]
            if mode == 'two_owners':
                second = deepcopy(claim)
                second['outgoing_relations'] = []
                claims.append(second)
            output = {'episodes': [], 'claims': claims}
            response = {'output': output, 'evidence': {'thread_start': {'thread_id': 'public-worker',
                'model': ep.MODEL, 'reasoning_effort': ep.REASONING_EFFORT, 'model_provider': 'openai'},
                'turn_started': {'thread_id': 'public-worker', 'turn_id': 'public-turn'},
                'turn_completed': True, 'no_tools': True, 'rerouted': False},
                'metrics': {'duration_ms': 1, 'usage': {'status': 'reported', 'source': 'injected_runner',
                    'tokens': {'input_tokens': 10, 'output_tokens': 10, 'total_tokens': 20}}}}
            self.outputs.append(response)
            return response
        result = ep.process_project_memory(self.plan['project'], self.plan['data'], runner=runner, timeout=180)
        self.assertEqual(len(self.requests), 1)
        return result, self.outputs[0]

    def test_prepare_is_fresh_seeded_and_later_event_is_pending(self):
        self.assertTrue(self.plan['seed_is_fixture_not_native_output'])
        self.assertEqual(self.plan['seed_commit']['processing']['model_calls'], 0)
        self.assertEqual(self.plan['seed_commit']['processing']['processor_id'], 'public-producer-fixture-seed')
        with Store(self.plan['data']) as store:
            memory = ProjectMemory(store)
            state = memory.snapshot(self.plan['project'])
            self.assertEqual([c['id'] for c in state['claims']], [self.plan['seed_claim_id']])
            self.assertEqual(state['relations'], [])
            pending = memory.claim(self.plan['project'])
            self.assertEqual(pending['session_id'], probe.CURRENT_SESSION)
            self.assertIn(self.plan['current_source_id'], [s['id'] for s in pending['sources']])
        with self.assertRaisesRegex(ValueError, 'fresh root'):
            probe.prepare(self.plan['root'])

    def test_valid_processed_native_owner_edge_passes_with_exact_scope_and_evidence(self):
        result, response = self.run_stub()
        checked = probe.verify(self.plan, result, response, native_request=self.requests[0])
        self.assertEqual(checked['status'], 'passed')
        self.assertFalse(checked['semantic_truth_established'])
        self.assertEqual(len(checked['native_new_claim_ids']), 1)
        self.assertEqual(len(checked['native_relation_ids']), 1)
        self.assertEqual(checked['seed_fixture_claim_id'], self.plan['seed_claim_id'])

    def test_actual_contradiction_enum_also_passes_without_invented_relation_types(self):
        result, response = self.run_stub('contradicts')
        checked = probe.verify(self.plan, result, response, native_request=self.requests[0])
        self.assertEqual(checked['status'], 'passed')

    def test_raw_evidence_cannot_borrow_a_committed_edge_or_forge_request(self):
        result, response = self.run_stub()
        wrong = deepcopy(response)
        wrong['output']['claims'][0]['outgoing_relations'][0]['evidence'] = ['forged-ref']
        with self.assertRaisesRegex(ValueError, 'evidence ref is not in the actual request'):
            probe.verify(self.plan, result, wrong, native_request=self.requests[0])
        payload = json.loads(self.requests[0]['prompt'].split('\n', 1)[1])
        payload['events'][0]['spans'][0]['text'] = 'Uncaptured content.'
        wrong_request = {**self.requests[0], 'prompt': 'Prefix\n' + json.dumps(payload)}
        with self.assertRaisesRegex(ValueError, 'differs from original captured source'):
            probe.verify(self.plan, result, response, native_request=wrong_request)
        with self.assertRaises(TypeError):
            probe.verify(self.plan, result, response)

    def test_empty_reconciliation_does_not_pass_even_when_product_commits(self):
        result, response = self.run_stub('empty_relation')
        self.assertEqual(result['status'], 'processed')
        with self.assertRaisesRegex(ValueError, 'requires an owner-derived'):
            probe.verify(self.plan, result, response, native_request=self.requests[0])

    def test_bad_evidence_rejects_atomically_and_failed_partial_state_refuses(self):
        result, response = self.run_stub('bad_evidence')
        self.assertEqual((result['status'], result['code']), ('failed', 'invalid_response'))
        with self.assertRaisesRegex(ValueError, 'did not process'):
            probe.verify(self.plan, result, response, native_request=self.requests[0])
        with Store(self.plan['data']) as store:
            state = ProjectMemory(store).snapshot(self.plan['project'])
            self.assertEqual([c['id'] for c in state['claims']], [self.plan['seed_claim_id']])
            self.assertEqual(state['relations'], [])

    def test_committed_state_with_failed_receipt_is_partial_not_a_pass(self):
        result, response = self.run_stub()
        with self.assertRaisesRegex(ValueError, 'partial canonical records'):
            probe.verify(self.plan, {**result, 'status': 'failed'}, response, native_request=self.requests[0])

    def test_mixed_legacy_and_wrong_native_owner_cannot_borrow_canonical_edge(self):
        result, response = self.run_stub('two_owners')
        self.assertEqual(probe.verify(self.plan, result, response, native_request=self.requests[0])['status'], 'passed')
        wrong = deepcopy(response)
        outgoing = wrong['output']['claims'][0]['outgoing_relations']
        wrong['output']['claims'][0]['outgoing_relations'] = []
        wrong['output']['claims'][1]['outgoing_relations'] = outgoing
        with self.assertRaisesRegex(ValueError, 'owner-derived canonical relation missing'):
            probe.verify(self.plan, result, wrong, native_request=self.requests[0])
        for output in ({'episodes': [], 'claims': [], 'relations': []},
                       {**response['output'], 'relations': [{'from_claim_index': 0}]}):
            with self.subTest(output=output), self.assertRaisesRegex(ValueError, 'native v12 layout'):
                probe.verify(self.plan, result, {**response, 'output': output}, native_request=self.requests[0])


if __name__ == '__main__':
    unittest.main()
