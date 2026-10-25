"""Public Pi runner schema controls use canned RPC, never a native model."""
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest import mock
from codex_mem.execution import ExecutionProfile, ExecutionRequest
from codex_mem.pi_runtime import PiProcessorRunner
from codex_mem.summary_support import prepare


class PiSchemaTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.pi = self.root / 'pi'
        self.pi.write_text('#!' + sys.executable + '\n' +
            (Path(__file__).parent / 'fixtures/execution_pi.py').read_text())
        self.pi.chmod(0o700)
        self.environment = mock.patch.dict(os.environ, PI_CODING_AGENT_DIR=str(self.root / 'native'))
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.scenario()

    def scenario(self, **values):
        self.pi.with_suffix('.json').write_text(json.dumps(dict(
            models=[dict(id='fixture', provider='openai-codex', reasoning=True)], **values)))

    def request(self, **changes):
        prompt, schema, _ = prepare(dict(title='Synthetic result', completed='One result.'),
            dict(sources=[dict(id='raw', body='One result.', source='hook:Stop')]))
        values = dict(prompt=prompt, output_schema=schema, job_type='summary_support',
            profile=ExecutionProfile('pi', 'openai-codex', 'fixture', 'low', 8),
            deadline=time.monotonic()+8, attempt_id='schema-test')
        values.update(changes)
        return ExecutionRequest(**values)

    def test_missing_schema_application_proof_cannot_accept_valid_json(self):
        result = PiProcessorRunner(pi=str(self.pi)).execute(self.request())
        self.assertIsNotNone(result.failure, result)
        self.assertEqual('pi_schema_proof_invalid', result.failure.reason_code)
        self.assertEqual(12, result.usage['tokens']['total_tokens'])

    def test_real_helper_applies_exact_schema_only_and_receipt_survives_sanitizing(self):
        from codex_mem.execution import pi_execution_receipt
        self.scenario(schema_helper='valid')
        request = self.request()
        result = PiProcessorRunner(pi=str(self.pi)).execute(request)
        self.assertIsNone(result.failure, result)
        payload = json.loads(self.pi.with_suffix('.schema-payload').read_text())
        self.assertEqual(dict(type='json_schema', name='codex_mem_review_v1', strict=True,
                              schema=request.output_schema), payload['text'].pop('format'))
        self.assertEqual(dict(model='fixture', instructions='Synthetic system',
            input=[dict(role='user', content='Synthetic evidence')], text=dict(verbosity='low'),
            reasoning=dict(effort='low'), tools=[]), payload)
        settings = json.loads(self.pi.with_suffix('.schema-settings').read_text())
        self.assertEqual(('sse', 'off', False, 0), (settings['transport'], settings['cacheWarming'],
            settings['retry']['enabled'], settings['retry']['provider']['maxRetries']))
        receipt = result.metrics['execution_receipt']
        self.assertEqual('bundled_schema_only', receipt['extensions_policy'])
        self.assertEqual('applied', receipt['structured_output']['status'])
        self.assertEqual(receipt, pi_execution_receipt(receipt))
        self.assertEqual(12, result.usage['tokens']['total_tokens'])
        launch = json.loads(self.pi.with_suffix('.launch').read_text().splitlines()[0])
        self.assertIn('--no-extensions', launch['args'])
        self.assertIn('--no-tools', launch['args'])
        self.assertIn('--extension', launch['args'])

    def test_foreign_tampered_duplicate_proofs_fail_without_losing_usage(self):
        for mode in ('foreign_schema', 'changed_payload', 'duplicate', 'extra_key', 'tampered_helper', 'tampered_schema'):
            with self.subTest(mode=mode):
                self.scenario(schema_helper=mode)
                result = PiProcessorRunner(pi=str(self.pi)).execute(self.request())
                self.assertEqual('pi_schema_proof_invalid', result.failure.reason_code, result)
                self.assertIsNone(result.output)
                self.assertEqual(12, result.usage['tokens']['total_tokens'])
                self.assertEqual('invalid', result.metrics['execution_receipt']['structured_output']['status'])

    def test_missing_or_tampered_bundled_helper_prevents_native_launch(self):
        from codex_mem import pi_schema
        for content in (None, b'foreign helper'):
            with self.subTest(content=content):
                path = self.root / 'foreign-helper.js'
                if content is not None: path.write_bytes(content)
                with mock.patch.object(pi_schema, 'HELPER_PATH', path):
                    result = PiProcessorRunner(pi=str(self.pi)).execute(self.request())
                self.assertEqual('pi_schema_helper_invalid', result.failure.reason_code, result)
                self.assertFalse(self.pi.with_suffix('.launch').exists())
                self.assertIsNone(result.usage['tokens'])

    def test_other_providers_keep_prompt_schema_behavior_and_historical_receipts_read(self):
        from codex_mem.execution import pi_execution_receipt
        self.pi.with_suffix('.json').write_text(json.dumps({}))
        result = PiProcessorRunner(pi=str(self.pi)).execute(self.request(
            profile=ExecutionProfile('pi', 'fixture', 'fixture', 'low', 8)))
        self.assertIsNone(result.failure, result)
        receipt = result.metrics['execution_receipt']
        self.assertEqual('cli_disabled', receipt['extensions_policy'])
        self.assertEqual('prompt_schema', receipt['structured_output']['mode'])
        old = dict(receipt); old.pop('structured_output')
        self.assertEqual(old, pi_execution_receipt(old))


    def test_exact_exposed_malformed_review_stays_rejected_without_repair(self):
        from codex_mem.pi_runtime import _json
        from codex_mem.processor import ProcessorFailure
        raw = (Path(__file__).parent / 'fixtures/known_pi_review_trailing_tag.txt').read_text()
        self.assertTrue(raw.endswith('</final>'))
        with self.assertRaises(ProcessorFailure) as error:
            _json(raw)
        self.assertEqual('invalid_json', error.exception.reason_code)
