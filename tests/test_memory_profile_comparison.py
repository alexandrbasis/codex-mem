"""Comparison evidence at public processors with a real Store and external executable."""
import json
from pathlib import Path
import unittest
import test_execution_contract as fixture
from codex_mem.config import configure
from codex_mem.hooks import handle_hook
from codex_mem.processor import NativeProcessorRunner
from codex_mem.store import Store
from scripts.memory_profile_comparison import MAX_REVIEW_CALLS, Recorder, measure


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

    def test_current_summary_records_every_review_stage_without_reusing_one_call_path(self):
        from codex_mem.execution import ExecutionIdentity, ExecutionResult
        from tests.test_summary_support import supported_review
        data, project = self.root / 'memory', self.root / 'project'
        project.mkdir()
        configure(data, capture_scope='all', service_enabled=False, semantic_enabled=False,
            jev_filter_enabled=False, jev_quality_enabled=False)
        with Store(data) as store:
            store.remember(project, 'Report', 'The assistant reported a local test pass.',
                source='hook:Stop', session_id='quality')
        seen = []
        class ValidRunner:
            def execute(inner, request):
                version = request.context.get('version')
                seen.append(version or 'generation')
                if version:
                    output = supported_review(request.context)['output']
                else:
                    output = dict(notes=[], disposition='processed', session_summary=dict(
                        title='Local result', request='', investigated='', learned='',
                        completed='The assistant reported a local test pass.', next_steps='', notes='',
                        source_ids=[source['id'] for source in request.context['sources']]))
                profile = request.profile
                return ExecutionResult(output=output, identity=ExecutionIdentity(profile.runtime, profile.provider,
                    profile.model, profile.reasoning_effort, 'comparison-thread', 'turn-' + str(len(seen))))
        destination = self.root / 'evidence'
        report = measure(project, data, 'observation', destination, ValidRunner(), timeout=5)
        self.assertEqual('processed', report['receipt']['status'], report['receipt'])
        self.assertEqual(['generation', 'summary-support-v6', 'summary-source-v4', 'summary-outcome-v5'], seen)
        self.assertEqual(1, report['execution_calls'])
        self.assertEqual(3, report['review_execution_calls'])
        self.assertEqual(3, len(report['review_executions']))
        for number, version in enumerate(seen[1:], 1):
            folder = destination / 'summary-support' / ('call-' + str(number))
            self.assertEqual(version, json.loads((folder / 'request.json').read_text())['context']['version'])
            self.assertTrue((folder / 'execution.json').is_file())
            self.assertTrue((folder / 'timing.json').is_file())
        self.assertEqual(report, json.loads((destination / 'result.json').read_text()))

    def test_recorders_enforce_generation_and_review_bounds_before_execution(self):
        from dataclasses import dataclass
        from codex_mem.execution import ExecutionResult
        @dataclass
        class Request:
            attempt_id: str
            context: dict
        for bound in (1, MAX_REVIEW_CALLS):
            with self.subTest(bound=bound):
                calls = []
                class Runner:
                    def execute(inner, request):
                        calls.append(request.attempt_id)
                        return ExecutionResult(output={})
                destination = self.root / ('bounded-' + str(bound))
                recorder = Recorder(Runner(), destination, max_calls=bound)
                for number in range(1, bound + 1):
                    recorder.execute(Request(str(number), {'version': 'stage-' + str(number)}))
                with self.assertRaisesRegex(RuntimeError, 'bound exceeded'):
                    recorder.execute(Request('denied', {}))
                self.assertEqual([str(number) for number in range(1, bound + 1)], calls)
                self.assertEqual(bound, recorder.calls)
                self.assertEqual(bound, len(recorder.executions))
                self.assertEqual(bound, len(list(destination.rglob('request.json'))))
                self.assertEqual(bound, len(list(destination.rglob('execution.json'))))

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
