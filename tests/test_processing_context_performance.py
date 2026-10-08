"""Background references must not audit the whole project before each batch."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from codex_mem.freshness import _unknown_snapshot
from codex_mem.processor import MODEL, PROCESSOR_ID, REASONING_EFFORT
from codex_mem.store import Store


class ProcessingContextPerformanceTests(unittest.TestCase):
    def test_claim_reference_keeps_scoped_evidence_without_full_health_scan(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            project = root / 'project'
            project.mkdir()
            with Store(root / 'memory') as store:
                store.remember(project, 'Earlier decision', 'Use checkout_id for retries.',
                               kind='decision', session_id='earlier')
                store.remember(root / 'foreign', 'Foreign decision', 'FOREIGN_REFERENCE',
                               kind='decision', session_id='earlier')
                store.remember(project, 'Future same-session choice', 'FUTURE_REFERENCE',
                               kind='decision', session_id='current')
                source = store.remember(project, 'Test result', 'Retry regression passes.',
                                        source='hook:PostToolUse', session_id='current')
                with patch('codex_mem.freshness.freshness_snapshot',
                           return_value=_unknown_snapshot()) as full_health:
                    claim = store.claim_observation_batch(
                        project, PROCESSOR_ID, MODEL, REASONING_EFFORT)
                self.assertEqual(0, full_health.call_count,
                                 'Each claim must avoid an unbounded health audit')
                self.assertEqual([source['id']], [row['id'] for row in claim['sources']])
                reference = claim['project_context']
                self.assertIn('checkout_id', reference)
                self.assertNotIn('FOREIGN_REFERENCE', reference)
                self.assertNotIn('FUTURE_REFERENCE', reference)
                self.assertIn('freshness unknown', reference)
                self.assertIn('deferred', reference)
                self.assertLessEqual(len(reference), 3000)

    def test_explicit_context_still_performs_full_health_check(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            with Store(root / 'memory') as store:
                with patch('codex_mem.freshness.freshness_snapshot',
                           return_value=_unknown_snapshot()) as full_health:
                    store.context(root / 'project')
                full_health.assert_called_once_with(store, str(root / 'project'))


if __name__ == '__main__':
    unittest.main()
