"""Atomic execution transport at public renderer and unchanged citation seams."""
from copy import deepcopy
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
import unittest
from xml.etree import ElementTree as ET

from codex_mem.project_memory import ProjectMemory
from codex_mem.project_memory_retrieval import build_project_brief, render_project_brief
import test_project_memory_source_coverage_allocation as support


class CompactExecutionTransportTests(unittest.TestCase):
    setUp = support.SourceCoverageAllocationTests.setUp
    tool = support.SourceCoverageAllocationTests.tool

    def prepare(self):
        output = 'Instrument fixture: sample rows matched. Remote delivery was not attempted. Labels were not inspected.'
        source = self.tool(output, 71, code=1)
        memory = ProjectMemory(self.store)
        memory.commit(memory.claim(self.project), {'episodes': [], 'claims': [], 'relations': []})
        brief = build_project_brief(self.store, self.project,
            query='What did the instrument fixture check establish?', budget=6000)
        record = next(record for record in brief['source_evidence'] if record['id'] == source['id'])
        return source, {'records': [deepcopy(record)], 'coverage': {}}

    def assert_execution(self, xml, source):
        tree = ET.fromstring(xml)
        item = tree.find("item[execution]")
        self.assertIsNotNone(item, xml)
        execution = item.find('execution')
        self.assertIsNotNone(execution, xml)
        self.assertEqual(execution.get('command'), 'python check.py ORB-27 --run 71')
        self.assertEqual(execution.get('exit_code'), '1')
        self.assertEqual(execution.get('exit_field'), 'tool_response.exit_code')
        self.assertEqual(execution.get('tool_use_id'), 'capture-71')
        self.assertEqual(item.get('lane'), 'source_evidence')
        self.assertIn(item.get('source_role'), (None, 'tool_record'))
        pointers = item.findall('source')
        self.assertEqual({ref.get('source_field') for ref in pointers},
                         {'tool_input.cmd', 'tool_response.output'})
        by_field = {ref.get('source_field'): ref for ref in pointers}
        for field, text in [('tool_input.cmd', 'python check.py ORB-27 --run 71'),
                            ('tool_response.output', source['body'])]:
            ref = by_field[field]
            self.assertEqual(ref.get('source_id'), source['id'])
            self.assertEqual(ref.get('source_role'), 'tool_record')
            self.assertEqual((ref.get('start'), ref.get('end')), ('0', str(len(text))))
            self.assertEqual(ref.findtext('quote_excerpt'), text)
            self.assertEqual(ref.get('tool_use_id'), execution.get('tool_use_id'))
            self.assertIn(ref.get('quote_truncated'), (None, 'False'))
            if ref.get('quote_truncated') is None:
                self.assertEqual(item.get('complete_capture_fields'), 'true')
            else:
                self.assertEqual(ref.get('excerpt_status'), 'complete_capture_field')
        self.assertEqual(item.get('field_association'), 'original_execution_capture')
        self.assertEqual(item.findall('field'), [])
        return tree

    def test_complete_capture_fits_tight_budget_without_field_wrapper_tax(self):
        source, brief = self.prepare()
        xml = render_project_brief(brief, budget=1550)
        self.assert_execution(xml, source)
        self.assertLessEqual(len(xml), 1550)
        self.assertEqual(xml, render_project_brief(brief, budget=1550))

    def test_exact_boundary_retains_all_three_execution_components_or_omits_whole_group(self):
        source, brief = self.prepare()
        full = render_project_brief(brief, budget=6000)
        self.assert_execution(full, source)
        # Admission uses conservative primary prices. Discover the observable
        # threshold independently rather than equating it with compressed bytes.
        observations = [(budget, render_project_brief(brief, budget))
                        for budget in range(128, 1801)]
        threshold, complete = next((budget, xml) for budget, xml in observations
                                  if ET.fromstring(xml).find('.//execution') is not None)
        self.assert_execution(complete, source)
        self.assertLessEqual(len(complete), threshold)
        below = render_project_brief(brief, budget=threshold - 1)
        tree = ET.fromstring(below)
        self.assertIsNone(tree.find('item'), below)
        self.assertIsNone(tree.find('.//execution'), below)
        self.assertEqual(list(tree.iter('source')), [])
        self.assertEqual(tree.find('omitted').get('records'), '1')
        self.assertEqual(tree.find('omitted').get('fields'), '2')

    def test_two_project_views_keep_atomic_originals_and_owner_fences_at_2900_each(self):
        from codex_mem.tool_io import normalize_capture
        sources = []
        for index, project in enumerate((self.project, self.other)):
            command = f'python inspect.py --sample {index}'
            output = f'Independent sample {index}: rows matched. Remote delivery was not attempted.'
            capture = normalize_capture({'tool_name': 'functions.exec_command', 'session_id': f'tools-{index}',
                'tool_use_id': f'public-{index}', 'tool_input': {'cmd': command},
                'tool_response': {'exit_code': index, 'output': output}}, project=project)
            source = self.store.remember(project, 'Actual check', output,
                source=f'hook:PostToolUse:public-{index}', session_id=f'tools-{index}', tool_capture=capture)
            memory = ProjectMemory(self.store)
            memory.commit(memory.claim(project), {'episodes': [], 'claims': [], 'relations': []})
            sources.append((source, command, output, index))
        scopes = [str(self.project), str(self.other)]
        delivered = []
        for project, (source, command, output, code), foreign in zip(
                (self.project, self.other), sources, reversed(sources)):
            brief = build_project_brief(self.store, project,
                query='What did the actual sample check establish?', budget=2900,
                selected_project_scopes=scopes)
            xml = render_project_brief(brief, budget=2900)
            item = ET.fromstring(xml).find("item[execution]")
            self.assertIsNotNone(item, xml)
            self.assertEqual(item.find('execution').get('command'), command)
            self.assertEqual(item.find('execution').get('exit_code'), str(code))
            refs = {ref.get('source_field'): ref for ref in item.findall('source')}
            self.assertEqual(refs['tool_response.output'].findtext('quote_excerpt'), output)
            self.assertEqual(refs['tool_input.cmd'].findtext('quote_excerpt'), command)
            self.assertTrue(all(ref.get('source_id') == source['id'] for ref in refs.values()))
            self.assertEqual(item.get('lane'), 'source_evidence')
            self.assertEqual(item.get('field_association'), 'original_execution_capture')
            self.assertIn(item.get('source_role'), (None, 'tool_record'))
            self.assertEqual(item.find('execution').get('tool_use_id'), f'public-{code}')
            for field, value in [('tool_response.output', output), ('tool_input.cmd', command)]:
                self.assertEqual(refs[field].get('source_role'), 'tool_record')
                self.assertEqual(refs[field].get('tool_use_id'), f'public-{code}')
                self.assertEqual((refs[field].get('start'), refs[field].get('end')), ('0', str(len(value))))
                if refs[field].get('quote_truncated') is None:
                    self.assertEqual(item.get('complete_capture_fields'), 'true')
                else:
                    self.assertEqual(refs[field].get('quote_truncated'), 'False')
            self.assertNotIn(foreign[0]['id'], xml)
            self.assertEqual(brief['xml'], xml)
            self.assertLessEqual(len(xml), 2900)
            delivered.append(xml)
        self.assertLessEqual(sum(map(len, delivered)), 5800)

    def test_unchanged_catalog_resolves_same_originals_from_legacy_and_compact_transport(self):
        source, brief = self.prepare()
        root = Path(__file__).resolve().parents[1]
        spec = spec_from_file_location('compact_transport_helper', root / 'scripts/project_memory_consolidation_experiment.py')
        helper = module_from_spec(spec)
        spec.loader.exec_module(helper)
        command = 'python check.py ORB-27 --run 71'
        originals = {source['id']: {'source_role': 'tool_record', 'fields': {
            'body': source['body'], 'tool_response.output': source['body'], 'tool_input.cmd': command}}}
        legacy = ET.Element('project-memory')
        item = ET.SubElement(legacy, 'item', source_role='tool_record')
        ET.SubElement(item, 'execution', command=command, exit_code='1', exit_field='tool_response.exit_code')
        for field, text in [('tool_response.output', source['body']), ('tool_input.cmd', command)]:
            wrapper = ET.SubElement(item, 'field', basis='attributed_source_excerpts',
                                    field_association='original_execution_capture')
            ref = ET.SubElement(wrapper, 'source', source_id=source['id'], source_field=field,
                                source_role='tool_record', start='0', end=str(len(text)), tool_use_id='capture-71')
            ET.SubElement(ref, 'quote_excerpt').text = text
        old_context = {'id': 'q', 'memory': ET.tostring(legacy, encoding='unicode')}
        new_context = {'id': 'q', 'memory': render_project_brief(brief, budget=6000)}
        expected = helper.reader_citation_catalog([old_context], originals)
        actual = helper.reader_citation_catalog([new_context], originals)
        self.assertEqual(actual, expected)
        self.assertEqual(len(actual), 2)
        citations = [{'catalog_ref': entry['ref'], **{key: value for key, value in entry.items() if key != 'ref'}}
                     for entry in actual]
        self.assertTrue(helper.typed_citations_visible(citations, actual, originals))


if __name__ == '__main__':
    unittest.main()
