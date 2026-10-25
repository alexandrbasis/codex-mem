"""Metadata-only Pi model selection uses the real runner and synthetic CLI."""
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

from codex_mem.execution import ExecutionProfile
from codex_mem.pi_runtime import PiProcessorRunner
from codex_mem.processor import ProcessorFailure


class PiModelSelectionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.pi = self.root / 'pi'
        self.pi.write_text('#!' + sys.executable + '\n' +
            (Path(__file__).parent / 'fixtures/execution_pi.py').read_text())
        self.pi.chmod(0o700)
        environment = mock.patch.dict(os.environ, PI_CODING_AGENT_DIR=str(self.root / 'empty-native'))
        environment.start()
        self.addCleanup(environment.stop)
        self.profile = ExecutionProfile('pi', 'selected-provider', 'selected-model', 'high', 240)

    def scenario(self, **changes):
        values = dict(models=[dict(provider=self.profile.provider, id=self.profile.model, reasoning=True)],
            levels=['low', 'high', 'max'],
            model_notifications=[dict(type='thinking_level_changed', level='high')])
        values.update(changes)
        self.pi.with_suffix('.json').write_text(json.dumps(values))

    def commands(self):
        return [json.loads(line) for line in self.pi.with_suffix('.trace').read_text().splitlines()]

    def check(self):
        return PiProcessorRunner(pi=str(self.pi)).check_profile(self.profile)

    def test_model_selection_notification_precedes_response_without_prompt(self):
        self.scenario()
        try:
            self.check()
        except ProcessorFailure as error:
            self.fail(dict(code=error.code, reason_code=error.reason_code))
        commands = self.commands()
        self.assertEqual(['get_available_models', 'set_model', 'get_available_thinking_levels',
                          'set_thinking_level', 'get_state'], [row['type'] for row in commands])
        self.assertEqual((self.profile.provider, self.profile.model),
            (commands[1]['provider'], commands[1]['modelId']))
        self.assertEqual('high', commands[3]['level'])
        launch = json.loads(self.pi.with_suffix('.launch').read_text().splitlines()[0])
        self.assertIn('--no-extensions', launch['args'])
        self.assertNotIn('--extension', launch['args'])
        self.assertFalse(Path(launch['cwd']).exists())

    def test_automatic_level_does_not_replace_explicit_requested_level(self):
        for requested in ('low', 'max'):
            with self.subTest(requested=requested):
                self.profile = ExecutionProfile('pi', 'selected-provider', 'selected-model', requested, 240)
                self.scenario()
                self.check()
                self.assertEqual(requested, [row['level'] for row in self.commands()
                                             if row['type'] == 'set_thinking_level'][-1])
                self.assertNotIn('prompt', [row['type'] for row in self.commands()])

    def test_unknown_extra_malformed_and_duplicate_switch_events_fail_closed(self):
        events = [dict(type='agent_start'), dict(type='thinking_level_changed', level='high', extra=True),
                  dict(type='thinking_level_changed'), dict(type='thinking_level_changed', level='ultra'),
                  dict(type='thinking_level_changed', level=['high'])]
        for notifications in [[event] for event in events] + [[dict(type='thinking_level_changed', level='high')] * 2]:
            with self.subTest(notifications=notifications):
                self.scenario(model_notifications=notifications)
                with self.assertRaises(ProcessorFailure) as failure:
                    self.check()
                self.assertEqual('pi_unexpected_event', failure.exception.reason_code)
                self.assertNotIn('prompt', [row['type'] for row in self.commands()])

    def test_switch_level_must_be_advertised_by_selected_model(self):
        self.profile = ExecutionProfile('pi', 'selected-provider', 'selected-model', 'low', 240)
        self.scenario(levels=['low', 'max'])
        with self.assertRaises(ProcessorFailure) as failure:
            self.check()
        self.assertEqual('pi_unexpected_event', failure.exception.reason_code)
        self.assertNotIn('set_thinking_level', [row['type'] for row in self.commands()])

    def test_notification_is_not_allowed_on_other_metadata_requests(self):
        self.scenario(levels_notification=dict(type='thinking_level_changed', level='high'))
        with self.assertRaises(ProcessorFailure) as failure:
            self.check()
        self.assertEqual('pi_unexpected_event', failure.exception.reason_code)

    def test_model_response_still_requires_exact_identity(self):
        for identity in (dict(provider='other', id='selected-model'),
                         dict(provider='selected-provider', id='other')):
            with self.subTest(identity=identity):
                self.scenario(model_response=identity)
                with self.assertRaises(ProcessorFailure) as failure:
                    self.check()
                self.assertEqual('pi_identity_mismatch', failure.exception.reason_code)

    def test_explicit_requested_level_and_final_state_remain_authoritative(self):
        for changes, reason in ((dict(levels=['low'], model_notifications=[]), 'unsupported_reasoning'),
                (dict(clamped='low'), 'pi_identity_mismatch'),
                (dict(thinking_notification=dict(type='thinking_level_changed', level='low')), 'pi_unexpected_event')):
            with self.subTest(changes=changes):
                self.scenario(**changes)
                with self.assertRaises(ProcessorFailure) as failure:
                    self.check()
                self.assertEqual(reason, failure.exception.reason_code)


if __name__ == '__main__':
    unittest.main()
