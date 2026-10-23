"""Comparison evidence at public processors with a real Store and external executable."""
import json
from pathlib import Path
import unittest
import test_execution_contract as fixture
from codex_mem.config import configure
from codex_mem.hooks import handle_hook
from codex_mem.processor import NativeProcessorRunner
from codex_mem.store import Store
from scripts.memory_profile_comparison import measure


class MemoryProfileComparisonTests(unittest.TestCase):
    setUp = fixture.ExecutionContractTests.setUp
    scenario = fixture.ExecutionContractTests.scenario
    # Reuse setup only; inherited contract cases remain owned by their original suite.
    def test_failed_output_preserves_attempt_and_sources_without_generated_records(self):
        data, project = self.root / 'memory', self.root / 'project'
        project.mkdir()
        configure(data, capture_scope='all', service_enabled=False, semantic_enabled=False)
        with Store(data) as store:
            handle_hook(dict(hook_event_name='UserPromptSubmit', cwd=str(project),
                             session_id='quality', prompt='Use local import.'), store)
        self.scenario(mode='invalid_json')
        report = measure(project, data, 'observation', self.root / 'evidence',
                         NativeProcessorRunner(codex=str(self.executable)), timeout=5)
        self.assertEqual('failed', report['receipt']['status'])
        self.assertEqual([], report['generated'])
        self.assertEqual(1, len(report['sources']))
        self.assertEqual(12, report['attempts'][0]['total_tokens'])
        self.assertEqual('invalid_output', report['execution']['failure']['reason'])
        self.assertTrue((self.root / 'evidence/request.json').is_file())
        self.assertEqual(report, json.loads((self.root / 'evidence/result.json').read_text()))

    def test_project_memory_keeps_original_packet_separate_from_committed_fields(self):
        data, project = self.root / 'memory', self.root / 'project'
        project.mkdir()
        configure(data, capture_scope='all', service_enabled=False, semantic_enabled=False,
                  project_memory_mode='pilot', project_memory_projects=[str(project)])
        with Store(data) as store:
            handle_hook(dict(hook_event_name='UserPromptSubmit', cwd=str(project),
                             session_id='quality', prompt='Use local import.'), store)
        self.scenario(mode='project_memory', claim=True)
        report = measure(project, data, 'project_memory', self.root / 'evidence',
                         NativeProcessorRunner(codex=str(self.executable)), timeout=5)
        self.assertEqual('processed', report['receipt']['status'], report['receipt'])
        self.assertEqual('Use local import.', report['execution']['output']['claims'][0]['text'])
        self.assertEqual('Use local import.', report['generated']['claims'][0]['text'])
        self.assertEqual(1, report['execution_calls'])
        self.assertEqual(12, report['attempts'][0]['total_tokens'])
        self.assertEqual(report['execution'], json.loads((self.root / 'evidence/execution.json').read_text()))
