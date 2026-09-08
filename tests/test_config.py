from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from codex_mem.config import (
    automatic_capture_enabled,
    configure,
    context_was_injected,
    data_dir_path,
    is_excluded_project,
    is_included_project,
    load_config,
    mark_context_injected,
)


class ConfigTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.data_dir = Path(self.temporary.name) / "memory-home"

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_missing_config_is_private_by_default(self) -> None:
        config = load_config(self.data_dir)

        self.assertTrue(config.valid)
        self.assertTrue(config["capture_enabled"])
        self.assertTrue(config["processor_enabled"])
        self.assertEqual("selected", config["capture_scope"])
        self.assertEqual([], config["included_projects"])
        self.assertFalse(automatic_capture_enabled(self.data_dir / "project", config))

    def test_loaded_defaults_do_not_share_project_lists(self) -> None:
        first = load_config(self.data_dir)
        first["included_projects"].append(str(self.data_dir / "project"))

        second = load_config(self.data_dir)
        self.assertEqual([], second["included_projects"])

    def test_whitespace_home_environment_uses_store_default_location(self) -> None:
        with mock.patch.dict(os.environ, {"CODEX_MEM_HOME": "   "}, clear=False):
            location = data_dir_path()

        self.assertEqual(
            (Path.home() / ".local" / "share" / "codex-mem").resolve(), location
        )

    def test_configure_persists_selected_project_scope(self) -> None:
        project = self.data_dir / "project"
        child = project / "worktree"
        config = configure(
            self.data_dir,
            capture_scope="selected",
            included_projects=[project],
            excluded_projects=[project / "private"],
            context_chars=1200,
            capture_tools=False,
            processor_enabled=False,
        )

        reloaded = load_config(self.data_dir)
        self.assertTrue(config.valid)
        self.assertEqual(reloaded, config)
        self.assertEqual(1200, reloaded["context_chars"])
        self.assertFalse(reloaded["processor_enabled"])
        self.assertTrue(is_included_project(child, reloaded))
        self.assertTrue(is_excluded_project(project / "private" / "nested", reloaded))
        self.assertTrue(automatic_capture_enabled(child, reloaded))
        self.assertFalse(automatic_capture_enabled(project / "private", reloaded))

    def test_all_and_manual_scope_have_clear_authority(self) -> None:
        project = self.data_dir / "project"
        private = project / "private"
        all_config = configure(
            self.data_dir,
            capture_scope="all",
            excluded_projects=[private],
        )

        self.assertTrue(automatic_capture_enabled(project, all_config))
        self.assertFalse(automatic_capture_enabled(private / "nested", all_config))

        manual = configure(self.data_dir, capture_scope="manual")
        self.assertFalse(automatic_capture_enabled(project, manual))

    def test_configure_rejects_invalid_values(self) -> None:
        invalid_updates = (
            {"capture_enabled": "yes"},
            {"processor_enabled": "yes"},
            {"capture_scope": "everywhere"},
            {"context_chars": 6001},
            {"excluded_projects": "not-a-list"},
            {"included_projects": [""]},
        )
        for updates in invalid_updates:
            with self.subTest(updates=updates):
                with self.assertRaises(ValueError):
                    configure(self.data_dir, **updates)

    def test_existing_invalid_config_fails_closed(self) -> None:
        self.data_dir.mkdir()
        (self.data_dir / "config.json").write_text(
            json.dumps(
                {
                    "capture_enabled": False,
                    "capture_scope": "selected",
                    "included_projects": "this-would-lose-scope",
                }
            ),
            encoding="utf-8",
        )

        config = load_config(self.data_dir)
        self.assertFalse(config.valid)
        self.assertFalse(config["capture_enabled"])
        self.assertFalse(automatic_capture_enabled(self.data_dir / "project", config))

    def test_unreadable_json_fails_closed(self) -> None:
        self.data_dir.mkdir()
        (self.data_dir / "config.json").write_text("{", encoding="utf-8")

        config = load_config(self.data_dir)
        self.assertFalse(config.valid)
        self.assertFalse(config["capture_enabled"])

    def test_context_delivery_state_keeps_multiple_sessions(self) -> None:
        mark_context_injected("session:a", source="context:one", data_dir=self.data_dir)
        mark_context_injected("session:b", source="context:two", data_dir=self.data_dir)

        self.assertTrue(
            context_was_injected("session:a", source="context:one", data_dir=self.data_dir)
        )
        self.assertTrue(
            context_was_injected("session:b", source="context:two", data_dir=self.data_dir)
        )
        self.assertFalse(
            context_was_injected("session:a", source="context:two", data_dir=self.data_dir)
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
