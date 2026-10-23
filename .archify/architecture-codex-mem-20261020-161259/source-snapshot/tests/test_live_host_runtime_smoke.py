"""The live smoke must not classify failed or fallback-only evidence as passed."""
import unittest


class LiveHostRuntimeSmokeTests(unittest.TestCase):
    def test_live_smoke_requires_processed_exact_identity_and_delivered_context(self):
        from scripts.live_host_runtime_smoke import passed
        profile = dict(runtime='pi', provider='fixture', model='fixture', reasoning_effort='low')
        report = dict(profile=profile, host='pi', runtime='pi', versions=dict(pi='1.4.0'), codex_executable_available=False,
            initial_entry_count=0, source_count=1, enqueue_status='queued', queue_file_created=True,
            generated_outputs_distinct_from_sources=True, generated_source_project_refs_valid=True,
            next_request_memory_contains_fact=True, generated_note_count=1, generated_note_delivered=True, jobs=[dict(status='processed')], attempts=[
                dict(outcome='processed', actual_runtime='pi', actual_provider='fixture',
                     actual_model='fixture', actual_reasoning_effort='low')])
        self.assertTrue(passed(report))
        for field, value in (('outcome', 'skipped'), ('actual_model', 'other'),
                             ('actual_runtime', None), ('actual_reasoning_effort', 'high')):
            with self.subTest(field=field):
                self.assertFalse(passed(dict(report, attempts=[dict(report['attempts'][0], **{field: value})])))
        self.assertFalse(passed(dict(report, next_request_memory_contains_fact=False)))
        self.assertFalse(passed(dict(report, jobs=[dict(status='skipped')])))

        self.assertFalse(passed(dict(report, generated_note_count=0)))
        self.assertFalse(passed(dict(report, generated_note_delivered=False)))
        self.assertFalse(passed(dict(report, generated_source_project_refs_valid=False)))
        self.assertFalse(passed(dict(report, generated_outputs_distinct_from_sources=False)))
        self.assertFalse(passed(dict(report, initial_entry_count=1)))
        self.assertFalse(passed(dict(report, codex_executable_available=True)))
        self.assertFalse(passed(dict(report, versions=dict(pi='1.3.0'))))

    def test_failures_keep_safe_cause_and_completed_progress(self):
        import json
        from pathlib import Path
        import subprocess
        import tempfile
        from unittest.mock import patch
        from scripts.live_host_runtime_smoke import pair

        secret = 'synthetic-secret-never-save'
        failures = (
            (subprocess.TimeoutExpired([secret], 17, output=secret, stderr=secret),
             dict(failure_code='process_timeout', timeout_seconds=17)),
            (PermissionError(13, secret, '/private/' + secret),
             dict(failure_code='filesystem_error', errno=13)),
            (json.JSONDecodeError(secret, secret, 3),
             dict(failure_code='invalid_json', line=1, column=4)),
            (subprocess.CalledProcessError(7, [secret], output=secret, stderr=secret),
             dict(failure_code='process_exit', returncode=7)),
        )
        with tempfile.TemporaryDirectory() as temporary:
            for failure, details in failures:
                with self.subTest(failure=type(failure).__name__), patch(
                        'scripts.live_host_runtime_smoke.run_service', side_effect=failure):
                    report = pair('pi', 'pi', timeout=60, native_pi=Path(temporary),
                                  native_codex=Path(temporary), executables={})
                    self.assertEqual(report['status'], 'unpassed')
                    self.assertEqual(report['failure_stage'], 'processing')
                    self.assertEqual(report['failure_type'], type(failure).__name__)
                    for key, value in details.items():
                        self.assertEqual(report[key], value)
                    self.assertEqual(report['source_count'], 1)
                    self.assertEqual(report['enqueue_status'], 'queued')
                    self.assertTrue(report['queue_file_created'])
                    self.assertNotIn('generated_note_count', report)
                    self.assertNotIn(secret, json.dumps(report))
                    self.assertNotIn('/private/', json.dumps(report))

    def test_main_saves_failed_pairs_and_continues_without_model(self):
        import json
        from pathlib import Path
        import subprocess
        import tempfile
        from unittest.mock import patch
        from scripts.live_host_runtime_smoke import main

        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / 'report.json'
            with patch('sys.argv', ['smoke', '--live', '--output', str(output)]), patch(
                    'scripts.live_host_runtime_smoke.shutil.which', return_value=None), patch(
                    'scripts.live_host_runtime_smoke.run_service',
                    side_effect=subprocess.TimeoutExpired(['synthetic-secret'], 17)) as service, patch.dict(
                    'os.environ', {'CODEX_HOME': temporary, 'PI_CODING_AGENT_DIR': temporary}):
                self.assertEqual(main(), 1)
            report = json.loads(output.read_text())
            self.assertEqual(service.call_count, 4)
            self.assertEqual(len(report['pairs']), 4)
            self.assertTrue(all(row['failure_stage'] == 'processing' for row in report['pairs']))
            self.assertTrue(all(row['failure_code'] == 'process_timeout' for row in report['pairs']))
            self.assertNotIn('synthetic-secret', output.read_text())
