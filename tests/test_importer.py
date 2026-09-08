"""Focused, source-safe coverage for the optional Claude-Mem importer."""

from __future__ import annotations

import hashlib
from pathlib import Path
import shutil
import sqlite3
import tempfile
import unittest

from codex_mem.importer import ClaudeMemImportError, import_claude_mem
from codex_mem.privacy import redact_text
from codex_mem.store import Store


class ImporterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.legacy = root / "claude-mem.db"
        self.data_dir = root / "codex-home"
        self.project = root / "destination"
        self.project.mkdir()
        self._create_legacy_database()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _create_legacy_database(self) -> None:
        connection = sqlite3.connect(self.legacy)
        try:
            connection.executescript(
                """
                CREATE TABLE observations (
                    id INTEGER PRIMARY KEY,
                    memory_session_id TEXT NOT NULL,
                    project TEXT NOT NULL,
                    text TEXT,
                    type TEXT NOT NULL,
                    title TEXT,
                    subtitle TEXT,
                    facts TEXT,
                    narrative TEXT,
                    concepts TEXT,
                    files_read TEXT,
                    files_modified TEXT,
                    prompt_number INTEGER,
                    correlation_id TEXT,
                    created_at TEXT NOT NULL,
                    created_at_epoch INTEGER NOT NULL
                );
                CREATE TABLE session_summaries (
                    id INTEGER PRIMARY KEY,
                    memory_session_id TEXT NOT NULL,
                    project TEXT NOT NULL,
                    request TEXT,
                    investigated TEXT,
                    learned TEXT,
                    completed TEXT,
                    next_steps TEXT,
                    files_read TEXT,
                    files_edited TEXT,
                    notes TEXT,
                    prompt_number INTEGER,
                    created_at TEXT NOT NULL,
                    created_at_epoch INTEGER NOT NULL
                );
                """
            )
            connection.executemany(
                """
                INSERT INTO observations(
                    id, memory_session_id, project, text, type, title, subtitle,
                    facts, narrative, concepts, files_read, files_modified,
                    prompt_number, correlation_id, created_at, created_at_epoch
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        1,
                        "legacy-session-1",
                        "/work/project",
                        "Найдено решение: обновить индекс.",
                        "discovery",
                        "Обновление индекса",
                        "Проверено локально",
                        '["индекс"]',
                        "Команда проверила схему.",
                        '["sqlite"]',
                        '["db.sql"]',
                        '["db.sql"]',
                        2,
                        "corr-1",
                        "2026-09-08T00:00:00Z",
                        1,
                    ),
                    (
                        2,
                        "legacy-session-other",
                        "/work/project-other",
                        "Не импортируй эту строку.",
                        "change",
                        "Другой проект",
                        None,
                        None,
                        None,
                        None,
                        None,
                        None,
                        1,
                        None,
                        "2026-09-08T00:00:01Z",
                        2,
                    ),
                ],
            )
            connection.execute(
                """
                INSERT INTO session_summaries(
                    id, memory_session_id, project, request, investigated, learned,
                    completed, next_steps, files_read, files_edited, notes,
                    prompt_number, created_at, created_at_epoch
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    7,
                    "legacy-session-1",
                    "/work/project",
                    "Подвести итог миграции",
                    "Изучена старая SQLite схема",
                    "Нужно сохранить происхождение",
                    "Подготовлен план",
                    "Проверить импорт повторно",
                    '["schema.sql"]',
                    '["importer.py"]',
                    "Assistant output; verify it.",
                    2,
                    "2026-09-08T00:01:00Z",
                    3,
                ),
            )
            connection.commit()
        finally:
            connection.close()

    def _hash(self) -> str:
        return hashlib.sha256(self.legacy.read_bytes()).hexdigest()

    def test_dry_run_is_exact_and_does_not_write_destination_or_source(self) -> None:
        before_source = self._hash()
        with Store(self.data_dir) as store:
            before_destination = store.status()["entries"]
            result = import_claude_mem(
                store,
                database=self.legacy,
                project=str(self.project),
                legacy_project="/work/project",
            )
            self.assertTrue(result["dry_run"])
            self.assertEqual(2, result["scanned"])
            self.assertEqual(2, result["would_import"])
            self.assertEqual(0, result["imported"])
            self.assertEqual(before_destination, store.status()["entries"])
        self.assertEqual(before_source, self._hash())

    def test_apply_preserves_provenance_unicode_and_is_idempotent(self) -> None:
        before_source = self._hash()
        with Store(self.data_dir) as store:
            first = import_claude_mem(
                store,
                database=self.legacy,
                project=str(self.project),
                legacy_project="/work/project",
                dry_run=False,
            )
            self.assertEqual(2, first["imported"])
            self.assertEqual(0, first["already_imported"])
            self.assertEqual(2, store.status(project=str(self.project))["entries"])

            second = import_claude_mem(
                store,
                database=self.legacy,
                project=str(self.project),
                legacy_project="/work/project",
                dry_run=False,
            )
            self.assertEqual(0, second["imported"])
            self.assertEqual(2, second["already_imported"])
            self.assertEqual(2, store.status(project=str(self.project))["entries"])

            entries = store.timeline(str(self.project), limit=10)
            self.assertEqual(2, len(entries))
            ids = [entry["id"] for entry in entries]
            full = store.get(str(self.project), ids)
            self.assertEqual(2, len(full))
            bodies = "\n".join(entry["body"] for entry in full)
            self.assertIn("Legacy assistant-generated claims are unverified", bodies)
            self.assertIn("Найдено решение", bodies)
            self.assertTrue(all(entry["source"].startswith("claude-mem:") for entry in full))
            self.assertTrue(all("unverified" in entry["tags"] for entry in full))
        self.assertEqual(before_source, self._hash())

    def test_different_source_content_with_same_row_id_gets_a_distinct_key(self) -> None:
        second_legacy = self.legacy.with_name("claude-mem-copy.db")
        shutil.copy2(self.legacy, second_legacy)
        connection = sqlite3.connect(second_legacy)
        try:
            connection.execute(
                "UPDATE observations SET text = ?, title = ? WHERE id = 1",
                ("Изменено во второй копии базы.", "Другая версия"),
            )
            connection.commit()
        finally:
            connection.close()

        with Store(self.data_dir) as store:
            first = import_claude_mem(
                store,
                database=self.legacy,
                project=str(self.project),
                legacy_project="/work/project",
                dry_run=False,
            )
            second = import_claude_mem(
                store,
                database=second_legacy,
                project=str(self.project),
                legacy_project="/work/project",
                dry_run=False,
            )
            self.assertEqual(2, first["imported"])
            self.assertEqual(1, second["by_table"]["observations"]["imported"])
            self.assertEqual(1, second["by_table"]["session_summaries"]["already_imported"])
            self.assertEqual(3, store.status(project=str(self.project))["entries"])

    def test_unknown_supported_table_schema_fails_actionably(self) -> None:
        connection = sqlite3.connect(self.legacy)
        try:
            connection.execute("DROP TABLE session_summaries")
            connection.execute("DROP TABLE observations")
            connection.execute(
                "CREATE TABLE observations(id INTEGER PRIMARY KEY, project TEXT, tool_input_json TEXT)"
            )
            connection.commit()
        finally:
            connection.close()

        with Store(self.data_dir) as store:
            with self.assertRaisesRegex(ClaudeMemImportError, "unsupported observations schema"):
                import_claude_mem(
                    store,
                    database=self.legacy,
                    project=str(self.project),
                    legacy_project="/work/project",
                )

    def test_redacts_before_body_clipping_and_is_idempotent(self) -> None:
        redaction_database = self.legacy.with_name("redaction.db")
        connection = sqlite3.connect(redaction_database)
        try:
            connection.execute(
                "CREATE TABLE observations(id INTEGER PRIMARY KEY, project TEXT, text TEXT)"
            )
            connection.execute(
                "INSERT INTO observations(id, project, text) VALUES (?, ?, ?)",
                (
                    88,
                    "/work/redaction",
                    "A" * 75_000 + "\npassword=super-secret\n" + "B" * 25_000,
                ),
            )
            connection.commit()
        finally:
            connection.close()

        class CaptureStore:
            def __init__(self) -> None:
                self.calls: list[dict[str, object]] = []

            def remember(self, project: str, **kwargs: object) -> dict[str, object]:
                self.calls.append({"project": project, **kwargs})
                return {"deduplicated": False}

        destination = CaptureStore()
        result = import_claude_mem(
            destination,
            database=redaction_database,
            project=str(self.project),
            legacy_project="/work/redaction",
            dry_run=False,
        )
        self.assertEqual(1, result["imported"])
        body = str(destination.calls[0]["body"])
        self.assertNotIn("super-secret", body)
        self.assertEqual(body, redact_text(body))

    def test_empty_supported_content_is_a_gap_and_writes_nothing(self) -> None:
        connection = sqlite3.connect(self.legacy)
        try:
            connection.execute("DELETE FROM session_summaries")
            connection.execute("DELETE FROM observations")
            connection.execute(
                """
                INSERT INTO observations(
                    id, memory_session_id, project, text, type, title,
                    created_at, created_at_epoch
                ) VALUES (99, 'legacy-gap', '/work/project', NULL, 'change', NULL,
                          '2026-09-08T00:00:00Z', 99)
                """
            )
            connection.commit()
        finally:
            connection.close()

        source_before = self._hash()
        with Store(self.data_dir) as store:
            with self.assertRaisesRegex(
                ClaudeMemImportError, "row 99 has no usable compressed content"
            ):
                import_claude_mem(
                    store,
                    database=self.legacy,
                    project=str(self.project),
                    legacy_project="/work/project",
                    dry_run=False,
                )
            self.assertEqual(0, store.status()["entries"])
        self.assertEqual(source_before, self._hash())

    def test_limit_is_bounded_and_source_database_stays_unchanged(self) -> None:
        before_source = self._hash()
        with Store(self.data_dir) as store:
            result = import_claude_mem(
                store,
                database=self.legacy,
                project=str(self.project),
                legacy_project="/work/project",
                dry_run=False,
                limit=1,
            )
            self.assertEqual(1, result["scanned"])
            self.assertEqual(1, result["imported"])
            self.assertEqual(1, result["skipped"])
            self.assertTrue(result["warnings"])
        self.assertEqual(before_source, self._hash())


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
