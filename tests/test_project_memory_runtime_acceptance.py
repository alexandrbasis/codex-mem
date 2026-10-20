"""Runtime probe decodes the installed MCP server's structured result shape."""
import importlib.util
from pathlib import Path
import unittest

from codex_mem.mcp import _tool_result

spec = importlib.util.spec_from_file_location('project_memory_runtime_probe',
    Path(__file__).resolve().parents[1] / 'scripts/project_memory_runtime_acceptance.py')
probe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(probe)


class RuntimeProbeDecoderTests(unittest.TestCase):
    def test_memory_get_array_envelope_is_unwrapped(self):
        records = [{'id': 'synthetic-source', 'body': 'Synthetic fixture.'}]
        self.assertEqual(records, probe.content(_tool_result(records)))
        self.assertEqual([], probe.content(_tool_result([])))

    def test_project_state_object_is_preserved(self):
        brief = {'project': '/synthetic', 'records': [], 'enabled': False}
        self.assertEqual(brief, probe.content(_tool_result(brief)))

    def test_tool_error_cannot_be_misread_as_success(self):
        with self.assertRaises(probe.AcceptanceError):
            probe.content(_tool_result('Memory operation failed', is_error=True))
