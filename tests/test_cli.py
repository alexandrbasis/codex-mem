"""User-facing CLI smoke tests through the checked-in launcher."""

from __future__ import annotations

import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
LAUNCHER = ROOT / "scripts" / "codex-mem.py"


class CLITests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        temporary_path = Path(self.temporary.name)
        self.data_dir = temporary_path / "memory-home"
        self.project = temporary_path / "project"
        self.project.mkdir()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _run(self, *arguments: str, expected: int = 0) -> object:
        result = subprocess.run(
            [sys.executable, str(LAUNCHER), *arguments],
            cwd=ROOT,
            text=True,
            capture_output=True,
            timeout=10,
        )
        self.assertEqual(expected, result.returncode, result.stderr)
        self.assertTrue(result.stdout, result.stderr)
        return json.loads(result.stdout)

    def test_launcher_data_commands_and_config(self) -> None:
        remembered = self._run(
            "remember",
            "--data-dir",
            str(self.data_dir),
            "--project",
            str(self.project),
            "--title",
            "CLI smoke note",
            "--body",
            "The launcher can write and read local memory.",
            "--tag",
            "smoke",
        )
        entry_id = remembered["id"]

        searched = self._run(
            "--data-dir",
            str(self.data_dir),
            "search",
            "--project",
            str(self.project),
            "--query",
            "launcher",
        )
        self.assertIn(entry_id, [item["id"] for item in searched])

        fetched = self._run(
            "--data-dir",
            str(self.data_dir),
            "get",
            "--project",
            str(self.project),
            "--id",
            entry_id,
        )
        self.assertEqual("The launcher can write and read local memory.", fetched[0]["body"])

        configured = self._run(
            "--data-dir",
            str(self.data_dir),
            "config",
            "--set",
            "capture_enabled=false",
            "--context-chars",
            "256",
        )
        self.assertFalse(configured["capture_enabled"])
        self.assertEqual(256, configured["context_chars"])

        configured = self._run("config", "--data-dir", str(self.data_dir), "--no-processor-enabled")
        self.assertFalse(configured["processor_enabled"])

        forgotten = self._run(
            "--data-dir",
            str(self.data_dir),
            "forget",
            "--project",
            str(self.project),
            entry_id,
        )
        self.assertIn(entry_id, forgotten["ids"])

    def test_cli_rejects_relative_projects_and_reports_unknown_hook_state(self) -> None:
        invalid = self._run(
            "--data-dir",
            str(self.data_dir),
            "search",
            "--project",
            "relative-project",
            "--query",
            "memory",
            expected=2,
        )
        self.assertEqual("invalid_arguments", invalid["error"]["code"])

        empty_status_project = self._run(
            "--data-dir",
            str(self.data_dir),
            "status",
            "--project",
            "",
            expected=2,
        )
        self.assertEqual("invalid_arguments", empty_status_project["error"]["code"])

        doctor = self._run("doctor")
        self.assertIn("runtime", doctor)
        self.assertIn("sqlite", doctor)
        self.assertEqual("UNKNOWN", doctor["hooks"]["status"])

    def test_processing_empty_project_does_not_start_a_job(self) -> None:
        result = self._run("process", "--data-dir", str(self.data_dir), "--project", str(self.project))
        self.assertEqual("idle", result["status"])
        status = self._run("status", "--data-dir", str(self.data_dir), "--project", str(self.project))
        self.assertEqual(0, status["observation_jobs"]["jobs"])

    def test_disabled_semantic_search_has_explicit_fallback_and_error(self) -> None:
        configured = self._run("config", "--data-dir", str(self.data_dir),
                               "--no-semantic-enabled", "--no-service-enabled")
        self.assertFalse(configured["semantic_enabled"])
        self.assertFalse(configured["service_enabled"])
        result = self._run("search", "--data-dir", str(self.data_dir),
                           "--project", str(self.project), "--query", "memory", "--mode", "auto")
        self.assertEqual("lexical", result["used_mode"])
        self.assertEqual("semantic_disabled", result["fallback_reason"])
        self.assertEqual([], result["results"])
        error = self._run("search", "--data-dir", str(self.data_dir),
                          "--project", str(self.project), "--query", "memory", "--mode", "semantic",
                          expected=2)
        self.assertEqual("semantic_unavailable", error["error"]["code"])

    def test_import_preview_does_not_initialize_destination_store(self) -> None:
        legacy = Path(self.temporary.name) / "legacy.sqlite"
        connection = sqlite3.connect(legacy)
        try:
            connection.execute(
                "CREATE TABLE observations (id INTEGER PRIMARY KEY, project TEXT NOT NULL, text TEXT)"
            )
            connection.execute(
                "INSERT INTO observations(id, project, text) VALUES (1, ?, ?)",
                ("legacy-project", "Synthetic legacy note."),
            )
            connection.commit()
        finally:
            connection.close()

        preview = self._run(
            "--data-dir",
            str(self.data_dir),
            "import-claude",
            "--database",
            str(legacy),
            "--project",
            str(self.project),
            "--legacy-project",
            "legacy-project",
        )
        self.assertTrue(preview["dry_run"])
        self.assertEqual(1, preview["would_import"])
        self.assertFalse(self.data_dir.exists())


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
