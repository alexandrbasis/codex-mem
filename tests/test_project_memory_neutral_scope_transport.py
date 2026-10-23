"""Checked original scope survives neutral custody selection and grouping."""
from pathlib import Path
import tempfile
import unittest
from xml.etree import ElementTree as ET

from codex_mem.config import configure
from codex_mem.project_memory import ProjectMemory
from codex_mem.project_memory_retrieval import build_project_brief, render_project_brief
from codex_mem.store import Store


class NeutralOriginalScopeTransportTests(unittest.TestCase):
    def check_pair(self, scopes, completed):
        with tempfile.TemporaryDirectory(prefix='scope-') as temporary:
            project = Path(temporary).resolve() / 'p'
            project.mkdir()
            data = Path(temporary) / 'data'
            configure(data, capture_scope='all', project_memory_mode='pilot',
                      project_memory_projects=[str(project)])
            with Store(data) as store:
                memory = ProjectMemory(store)
                sources = []
                targets = []
                for index, (environment, version) in enumerate(scopes):
                    text = f'PUB-82 scope {index}'
                    source = store.remember(project, 'Public original', text,
                                            source='hook:UserPromptSubmit', session_id=str(index))
                    ref = {'source_id': source['id'], 'start': 0, 'end': len(text), 'quote': text}
                    result = memory.commit(memory.claim(project), {'episodes': [], 'claims': [
                        {'key': f'source-{index}', 'text': text, 'kind': 'requirement', 'workflow_state': 'open',
                         'scope': {'environment': environment, 'version': version},
                         'evidence': {'text': [ref], 'workflow_state': [ref]}}], 'relations': []})
                    self.assertEqual(result['status'], 'committed')
                    sources.append(source)
                    targets.append(result['object_ids'][0])
                if completed:
                    text = ' '.join(source['body'] + ' completed.' for source in sources)
                    closing = store.remember(project, 'Public closure observation', text,
                                              source='hook:PostToolUse', session_id='closure')
                    claims, relations = [], []
                    for index, (source, target, (environment, version)) in enumerate(zip(sources, targets, scopes)):
                        sentence = source['body'] + ' completed.'
                        start = text.index(sentence)
                        ref = {'source_id': closing['id'], 'start': start, 'end': start + len(sentence),
                               'quote': sentence}
                        claims.append({'key': f'closure-{index}', 'text': sentence, 'kind': 'result',
                                       'workflow_state': 'completed',
                                       'scope': {'environment': environment, 'version': version},
                                       'evidence': {'text': [ref], 'workflow_state': [ref]}})
                        relations.append({'from': f'closure-{index}', 'to': target, 'type': 'closes', 'evidence': [ref]})
                    result = memory.commit(memory.claim(project),
                                           {'episodes': [], 'claims': claims, 'relations': relations})
                    self.assertEqual(result['status'], 'committed')
                before = memory.snapshot(project)
                brief = build_project_brief(store, project,
                    query='Why was the telescope restriction requested?', budget=6000)
                self.assertEqual(brief['xml'], render_project_brief(brief, budget=6000))
                tree = ET.fromstring(brief['xml'])
                for source, (environment, version) in zip(sources, scopes):
                    wrappers = [item for item in tree.findall('item') if any(
                        ref.get('source_id') == source['id'] and ref.findtext('quote_excerpt') == source['body']
                        for ref in item.iter('source'))]
                    self.assertTrue(wrappers, brief['xml'])
                    for item in wrappers:
                        self.assertEqual(item.get('environment'), environment or None)
                        self.assertEqual(item.get('version'), version or None)
                        self.assertEqual(item.get('disposition'), 'completed' if completed else 'stored')
                        self.assertEqual(item.get('relationship_to_query'), 'unresolved')
                        self.assertEqual(item.get('is_requirement_claim'), 'False')
                        self.assertIsNone(item.get('workflow_state'))
                        self.assertEqual(len(list(item.iter('source'))), 1)
                self.assertEqual(memory.snapshot(project)['claims'], before['claims'])
                self.assertLessEqual(len(brief['xml']), 6000)

    def test_stored_and_completed_scopes_stay_distinct_in_both_orders(self):
        local, production = ('local', '1.2.0'), ('production', '2.0.0')
        for completed in (False, True):
            for scopes in ((local, production), (production, local)):
                with self.subTest(completed=completed, scopes=scopes):
                    self.check_pair(scopes, completed)

    def test_unknown_scope_stays_unknown_beside_declared_scope_in_both_orders(self):
        unknown, local = ('', ''), ('local', '1.2.0')
        for completed in (False, True):
            for scopes in ((unknown, local), (local, unknown)):
                with self.subTest(completed=completed, scopes=scopes):
                    self.check_pair(scopes, completed)
