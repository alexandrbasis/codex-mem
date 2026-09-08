"""Queue boundaries distinguish absent optional setup from broken artifacts."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from codex_mem.integration import index_project
from codex_mem.service import _index_pending


class IntegrationTests(unittest.TestCase):
    def test_absent_optional_model_does_not_block_observation_queue(self):
        with tempfile.TemporaryDirectory() as temporary:
            data = Path(temporary) / "memory"
            with patch("codex_mem.semantic.index_pending", return_value={
                "status": "unavailable", "code": "model_not_ready", "indexed": 0, "pending": 0,
            }):
                result = index_project(Path(temporary), data)
            self.assertEqual("unavailable", result["status"])
            self.assertFalse(_index_pending(result))

    def test_bad_model_hash_blocks_queue_until_explicit_repair(self):
        with tempfile.TemporaryDirectory() as temporary:
            data = Path(temporary) / "memory"
            with patch("codex_mem.semantic.index_pending", return_value={
                "status": "unavailable", "code": "model_hash_mismatch", "indexed": 0, "pending": 0,
            }):
                result = index_project(Path(temporary), data)
            self.assertEqual("failed", result["status"])
            from codex_mem.service import ServiceError
            with self.assertRaises(ServiceError):
                _index_pending(result)
