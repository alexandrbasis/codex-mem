"""Public configuration preserves old installations and exact profile choices."""
import io
import json
import os
from contextlib import redirect_stdout
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from codex_mem.config import configure, load_config
from codex_mem.cli import main


class ExecutionProfileTests(unittest.TestCase):
    def test_legacy_default_override_and_reset_through_configuration_cli(self):
        from codex_mem.execution import resolve_profile
        with tempfile.TemporaryDirectory() as directory:
            legacy = resolve_profile(load_config(directory), 'observation')
            self.assertEqual(('codex', 'openai', 'gpt-6-luna', 'medium', 240),
                             (legacy.runtime, legacy.provider, legacy.model, legacy.reasoning_effort, legacy.timeout))
            profile = dict(runtime='pi', provider='anthropic', model='claude-exact',
                           reasoning_effort='high', timeout=120)
            output = io.StringIO()
            with redirect_stdout(output):
                code = main(['--data-dir', directory, 'config', '--set',
                             'processing_profile=' + json.dumps(profile)])
            self.assertEqual(0, code, output.getvalue())
            settings = load_config(directory)
            self.assertEqual('pi', resolve_profile(settings, 'observation').runtime)
            self.assertEqual('pi', resolve_profile(settings, 'project_memory').runtime)
            configure(directory, observation_profile=legacy.to_dict())
            settings = load_config(directory)
            self.assertEqual('codex', resolve_profile(settings, 'observation').runtime)
            self.assertEqual('pi', resolve_profile(settings, 'project_memory').runtime)
            configure(directory, observation_profile=None, processing_profile=None)
            self.assertEqual(legacy, resolve_profile(load_config(directory), 'observation'))

    def test_invalid_profiles_cannot_overwrite_configuration_or_silently_fall_back(self):
        from codex_mem.execution import legacy_profile, resolve_profile
        with tempfile.TemporaryDirectory() as directory:
            valid = legacy_profile().to_dict()
            configure(directory, processing_profile=valid)
            path = Path(directory) / 'config.json'
            before = path.read_bytes()
            invalid = [dict(valid, runtime='other'), dict(valid, model='gpt-*'),
                       dict(valid, provider=' openai'), dict(valid, reasoning_effort='automatic'),
                       dict(valid, timeout=True), dict(valid, timeout=601),
                       dict(valid, api_key='fictional-secret'), {'runtime': 'codex'}]
            for value in invalid:
                with self.subTest(value=value):
                    with self.assertRaises(ValueError) as error:
                        configure(directory, processing_profile=value)
                    self.assertNotIn('fictional-secret', str(error.exception))
                    self.assertEqual(before, path.read_bytes())
            path.write_text(json.dumps({'processing_profile': invalid[0]}))
            settings = load_config(directory)
            self.assertFalse(settings.valid)
            with self.assertRaises(ValueError):
                resolve_profile(settings, 'observation')

    def test_cli_rejects_oversized_integer_timeouts_without_overwriting_configuration(self):
        from codex_mem.execution import PROFILE_KEYS, legacy_profile
        root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as directory:
            valid = legacy_profile().to_dict()
            configure(directory, processing_profile=valid)
            path = Path(directory) / 'config.json'
            before = path.read_bytes()
            for key in PROFILE_KEYS:
                for timeout in (10 ** 400, -(10 ** 400)):
                    with self.subTest(key=key, timeout=timeout):
                        profile = dict(valid, timeout=timeout)
                        result = subprocess.run(
                            [sys.executable, str(root / 'scripts' / 'codex-mem.py'),
                             '--data-dir', directory, 'config', '--set', key + '=' + json.dumps(profile)],
                            cwd=root, env=dict(os.environ, HOME=directory),
                            capture_output=True, text=True, timeout=10)
                        self.assertEqual(2, result.returncode, result.stderr)
                        self.assertEqual({'ok': False, 'error': {
                            'code': 'invalid_arguments', 'message': 'Invalid arguments'}}, json.loads(result.stdout))
                        self.assertEqual('', result.stderr)
                        self.assertEqual(before, path.read_bytes())

    def test_persisted_oversized_integer_timeouts_disable_invalid_configuration(self):
        from codex_mem.execution import PROFILE_KEYS, legacy_profile, resolve_profile
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'config.json'
            for key in PROFILE_KEYS:
                for timeout in (10 ** 400, -(10 ** 400)):
                    with self.subTest(key=key, timeout=timeout):
                        path.write_text(json.dumps({key: dict(legacy_profile().to_dict(), timeout=timeout)}))
                        before = path.read_bytes()
                        settings = load_config(directory)
                        self.assertFalse(settings.valid)
                        self.assertFalse(settings['processor_enabled'])
                        self.assertFalse(settings['capture_enabled'])
                        for processor in ('observation', 'project_memory'):
                            with self.assertRaises(ValueError):
                                resolve_profile(settings, processor)
                        self.assertEqual(before, path.read_bytes())

    def test_profile_resolution_copies_exact_identifiers_and_keeps_disabled_switches(self):
        from codex_mem.execution import resolve_profile
        with tempfile.TemporaryDirectory() as directory:
            profile = dict(runtime='pi', provider='custom-provider', model='exact/model:2026-10-23',
                           reasoning_effort=None, timeout=12.5)
            configure(directory, processor_enabled=False, processing_profile=profile,
                      project_memory_profile=dict(profile, model='other-exact-model'))
            settings = load_config(directory)
            resolved = resolve_profile(settings, 'observation')
            self.assertEqual(profile, resolved.to_dict())
            self.assertFalse(settings['processor_enabled'])
            self.assertEqual('other-exact-model', resolve_profile(settings, 'project_memory').model)
            settings['processing_profile']['model'] = 'changed-after-resolution'
            self.assertEqual('exact/model:2026-10-23', resolved.model)
