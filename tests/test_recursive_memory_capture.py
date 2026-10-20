"""Known injected memory cannot become fresh hook or tool evidence."""
import json
import sqlite3
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from codex_mem.hooks import _remember_safely, _safe_prompt, _tool_output
from codex_mem.tool_io import normalize_capture, strip_injected_memory, insert_capture, get_tool_capture


class RecursiveMemoryCaptureTests(unittest.TestCase):
    def test_wrapper_variants_preserve_new_user_text(self):
        for name in ('project-memory', 'project_memory', 'codex-mem-context',
                     'codex_mem_context', 'codex_mem_untrusted_context',
                     'codex-mem-untrusted-context'):
            raw = '<' + name + ' trust="untrusted">Old fictional obligation.</' + name + '>'
            for quoted in (raw, '"' + raw + '"', raw.replace('<', '&lt;').replace('>', '&gt;'),
                           raw.replace('<', r'\u003c').replace('>', r'\u003e'),
                           raw.replace('<', r'\x3c').replace('>', r'\x3e'),
                           raw.replace('<', '&#60;').replace('>', '&#62;'),
                           raw.replace('</', r'<\/'),
                           json.dumps(raw)):
                with self.subTest(name=name, quoted=quoted):
                    value = 'New instruction before.\n' + quoted + '\nNew correction after.'
                    cleaned, count = strip_injected_memory(value)
                    self.assertEqual(1, count)
                    self.assertNotIn('Old fictional obligation', cleaned)
                    self.assertIn('New instruction before.', cleaned)
                    self.assertIn('New correction after.', cleaned)
                    self.assertNotIn('Old fictional obligation', _safe_prompt(value))

    def test_nested_and_unfinished_blocks_fail_closed(self):
        value = 'Before <codex_mem_untrusted_context><project-memory>Old text</project-memory></codex_mem_untrusted_context> After'
        cleaned, count = strip_injected_memory(value)
        self.assertEqual(2, count)
        self.assertEqual('Before \n After', cleaned)
        self.assertEqual(('Before \n', 1), strip_injected_memory('Before <project-memory>Old unfinished tail'))
        self.assertEqual(('Before \n After', 1), strip_injected_memory('Before <project-memory/> After'))

    def test_normal_tool_results_and_plain_mentions_are_unchanged(self):
        for value in ('Ran 12 tests. OK.', 'The project-memory feature uses SQLite.',
                      'A <project-memory-note> is an unrelated tag.',
                      'A <project-memory:note> is an unrelated tag.', 'prefix <example>data</example> suffix'):
            self.assertEqual((value, 0), strip_injected_memory(value))
            self.assertEqual(value, _tool_output({'output': value}, command='pytest -q'))

    def test_hook_output_and_final_capture_omit_memory_with_diagnostic(self):
        old = '<project-memory>Old obligation</project-memory>'
        with patch('codex_mem.hooks.trace_event') as trace:
            self.assertEqual('Current result\n\n\nNew error', _tool_output(
                {'output': 'Current result\n' + old + '\nNew error'}, command='cat report'))
            trace.assert_called_with('memory_context_omitted', wrapper_count=1)
        captured = []
        store = SimpleNamespace(remember=lambda project, **kwargs: captured.append(kwargs))
        _remember_safely(store, '/tmp/project', title='Report', body='Before '+old+' After',
            kind='session', payload={}, source='hook:Stop', tags=[], dedupe_prefix='stop')
        self.assertNotIn('Old obligation', captured[0]['body'])
        self.assertIn('Before', captured[0]['body'])
        self.assertIn('After', captured[0]['body'])

    def test_raw_nested_tool_capture_omits_wrappers_and_records_omission(self):
        payload = {'tool_name': 'functions.exec_command', 'session_id': 's', 'tool_use_id': 't',
            'tool_input': {'cmd': 'pytest -q'}, 'tool_response': {'output': [
                {'text': 'New result <project-memory>Old obligation</project-memory> Still new'},
                {'text': 'Normal output retained'}], 'exit_code': 0}}
        capture = normalize_capture(payload, project='/tmp/project')
        self.assertIsNotNone(capture)
        self.assertNotIn('Old obligation', capture.tool_response)
        self.assertIn('New result', capture.tool_response)
        self.assertIn('Still new', capture.tool_response)
        self.assertIn('Normal output retained', capture.tool_response)
        self.assertTrue(capture.response_metadata['memory_context_omitted'])
        self.assertFalse(capture.input_metadata['memory_context_omitted'])
        connection = sqlite3.connect(':memory:')
        connection.row_factory = sqlite3.Row
        try:
            insert_capture(connection, 'entry', '/tmp/project', capture)
            stored = get_tool_capture(connection, '/tmp/project', 't', session_id='s')
            self.assertTrue(stored['response_metadata']['memory_context_omitted'])
            self.assertNotIn('Old obligation', stored['tool_response'])
        finally:
            connection.close()


if __name__ == '__main__':
    unittest.main()
