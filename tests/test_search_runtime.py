"""Live plugin telemetry must stay distinct from historical memory claims."""
from pathlib import Path
import tempfile
import unittest
from codex_mem import __version__
from codex_mem.config import configure
from codex_mem.integration import search_memory
from codex_mem.store import Store


class SearchRuntimeTests(unittest.TestCase):
    def test_search_reports_current_plugin_and_disabled_optional_gates(self):
        with tempfile.TemporaryDirectory() as directory:
            configure(directory, semantic_enabled=False, processor_enabled=False,
                      service_enabled=False, jev_filter_enabled=False,
                      jev_quality_enabled=False, jev_retrieval_enabled=False)
            with Store(directory) as store:
                result = search_memory(store, str(Path(directory) / 'app'), 'unmatched',
                                       mode='lexical')
            self.assertEqual(__version__, result['runtime']['codex_mem_version'])
            self.assertEqual({False}, set(result['runtime']['jev_flags'].values()))
            self.assertEqual([], result['results'])
            self.assertNotIn('installed_app_version', result['runtime'])


if __name__ == '__main__':
    unittest.main()
