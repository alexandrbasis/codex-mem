"""Public processor regressions for claim-owned native reconciliation v12."""
import json
from pathlib import Path
import tempfile
import unittest

from codex_mem import episode_processor as ep
from codex_mem.config import configure
from codex_mem.project_memory import ProjectMemory
from codex_mem.store import Store
from test_episode_processor import receipt


class NativeClaimRelationsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.project = Path(self.temp.name) / 'project'
        self.project.mkdir()
        self.data = Path(self.temp.name) / 'data'
        configure(self.data, capture_scope='all', project_memory_mode='shadow',
                  project_memory_projects=[str(self.project)], jev_quality_enabled=False)

    def remember(self, body, session):
        with Store(self.data) as store:
            return store.remember(self.project, 'Public task', body,
                                  source='hook:UserPromptSubmit', session_id=session)

    def state(self):
        with Store(self.data) as store:
            return ProjectMemory(store).snapshot(self.project)

    @staticmethod
    def claim(span, relations=()):
        return {'text': span['text'].strip(), 'kind': 'requirement', 'workflow_state': 'open',
                'scope': {'environment': '', 'version': ''}, 'rationale': '',
                'evidence': {'text': [span['ref']], 'workflow_state': [span['ref']], 'rationale': []},
                'outgoing_relations': list(relations)}

    def initial(self):
        self.remember('Keep task ALPHA manual.', 'one')
        def runner(request):
            payload = json.loads(request['prompt'].split('\n', 1)[1])
            return receipt({'episodes': [], 'claims': [self.claim(payload['events'][0]['spans'][0])]})
        result = ep.process_project_memory(self.project, self.data, runner=runner)
        self.assertEqual(result['status'], 'processed', result)
        return self.state()['claims'][0]['id']

    def test_schema_removes_dangling_index_by_claim_ownership(self):
        schema = ep.output_schema(existing_claim_ids=['old'], span_refs=['r1'])
        self.assertEqual(ep.OUTPUT_SCHEMA_VERSION, 'project-memory-output-v12')
        self.assertEqual(set(schema['properties']), {'episodes', 'claims'})
        claim = schema['properties']['claims']['items']
        self.assertIn('outgoing_relations', claim['required'])
        relation = claim['properties']['outgoing_relations']['items']
        self.assertEqual(set(relation['properties']), {'type', 'to_claim_id', 'evidence'})
        self.assertEqual(relation['properties']['to_claim_id']['enum'], ['old'])
        self.assertEqual(relation['properties']['evidence']['minItems'], 1)
        try:
            import jsonschema
        except ImportError:
            return  # Runtime public processor tests below do not need jsonschema.
        jsonschema.validate({'episodes': [], 'claims': []}, schema)
        # R11 failure shape, expressed without any private source content.
        old = {'episodes': [], 'claims': [], 'relations': [
            {'type': 'contradicts', 'from_claim_index': 0, 'to_claim_id': 'old', 'evidence': ['r1']}]}
        with self.assertRaises(jsonschema.ValidationError):
            jsonschema.validate(old, schema)

    def test_empty_and_new_claim_public_processing_preserve_canonical_store(self):
        target = self.initial()
        self.remember('Keep task ALPHA supervised.', 'two')
        def runner(request):
            payload = json.loads(request['prompt'].split('\n', 1)[1])
            span = payload['events'][0]['spans'][0]
            return receipt({'episodes': [], 'claims': [self.claim(span, [
                {'type': 'contradicts', 'to_claim_id': target, 'evidence': [span['ref']]}])]})
        result = ep.process_project_memory(self.project, self.data, runner=runner)
        self.assertEqual(result['status'], 'processed', result)
        state = self.state()
        self.assertEqual(len(state['claims']), 2)
        self.assertEqual(len(state['relations']), 1)
        self.assertEqual(state['relations'][0]['to'], target)
        self.assertNotEqual(state['relations'][0]['from'], target)
        self.assertNotIn('outgoing_relations', json.dumps(state))
        self.remember('No durable update.', 'three')
        result = ep.process_project_memory(self.project, self.data, runner=lambda request: receipt({'episodes': [], 'claims': []}))
        self.assertEqual(result['status'], 'processed', result)
        self.assertEqual(len(self.state()['claims']), 2)

    def test_multiple_owners_resolve_to_each_owning_claim_not_an_index(self):
        target = self.initial()
        self.remember('Keep task ALPHA supervised. Keep task BETA local.', 'two')
        def runner(request):
            payload = json.loads(request['prompt'].split('\n', 1)[1])
            spans = payload['events'][0]['spans']
            return receipt({'episodes': [], 'claims': [self.claim(span, [
                {'type': 'contradicts', 'to_claim_id': target, 'evidence': [span['ref']]}]) for span in spans]})
        result = ep.process_project_memory(self.project, self.data, runner=runner)
        self.assertEqual(result['status'], 'processed', result)
        state = self.state()
        expected = {c['id'] for c in state['claims'] if c['id'] != target}
        self.assertEqual({r['from'] for r in state['relations']}, expected)
        self.assertEqual({r['to'] for r in state['relations']}, {target})

    def test_invalid_target_evidence_mixed_or_legacy_shape_rejects_whole_batch(self):
        for invalid in ('target', 'evidence', 'empty_evidence', 'mixed', 'legacy',
                        'missing_list', 'authored_owner', 'scope', 'too_many'):
            with self.subTest(invalid=invalid):
                # A fresh Store per subcase avoids retrying quarantined inputs.
                self.data = Path(self.temp.name) / invalid
                configure(self.data, capture_scope='all', project_memory_mode='shadow',
                          project_memory_projects=[str(self.project)], jev_quality_enabled=False)
                target = self.initial()
                before = self.state()
                source = self.remember('Keep task ALPHA supervised.', 'two')
                def runner(request):
                    payload = json.loads(request['prompt'].split('\n', 1)[1])
                    span = payload['events'][0]['spans'][0]
                    relation = {'type': 'contradicts', 'to_claim_id': target, 'evidence': [span['ref']]}
                    if invalid == 'target': relation['to_claim_id'] = 'foreign-target'
                    if invalid == 'evidence': relation['evidence'] = ['unknown-ref']
                    if invalid == 'empty_evidence': relation['evidence'] = []
                    output = {'episodes': [], 'claims': [self.claim(span), self.claim(span, [relation])]}
                    if invalid == 'missing_list': output['claims'][1].pop('outgoing_relations')
                    if invalid == 'authored_owner': output['claims'][1]['key'] = 'claim_0'
                    if invalid == 'scope': output['claims'][1]['scope']['worktree'] = '/foreign'
                    if invalid == 'too_many':
                        output['claims'][0]['outgoing_relations'] = [dict(relation) for _ in range(8)]
                    if invalid == 'mixed': output['relations'] = []
                    if invalid == 'legacy':
                        output = {'episodes': [], 'claims': [], 'relations': [
                            {**relation, 'from_claim_index': 0}]}
                    return receipt(output)
                result = ep.process_project_memory(self.project, self.data, runner=runner)
                self.assertEqual((result['status'], result['code']), ('failed', 'invalid_response'))
                self.assertEqual(self.state()['claims'], before['claims'])
                self.assertEqual(self.state()['relations'], before['relations'])
                with Store(self.data) as store:
                    self.assertIsNotNone(store.get(self.project, source['id']))


if __name__ == '__main__':
    unittest.main()
