"""User-facing CLI smoke tests through the checked-in launcher."""

from __future__ import annotations

import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest

from codex_mem.store import Store
from codex_mem.tool_io import normalize_capture
from codex_mem.mcp import MAX_RAW_RESULT_BYTES


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

    def test_search_resume_intent_returns_ranked_results_and_metadata(self) -> None:
        with Store(self.data_dir) as store:
            store.remember(self.project, "Roadmap roadmap theme", "Roadmap CSS theme.")
            summary = store.remember(self.project, "Handoff", "Roadmap deployment remains open.", kind="session_summary")
        result = self._run(
            "search", "--data-dir", str(self.data_dir), "--project", str(self.project),
            "--query", "roadmap", "--intent", "resume", "--limit", "1",
        )
        self.assertEqual("resume", result["intent"])
        self.assertEqual([summary["id"]], [item["id"] for item in result["results"]])

    def test_recover_expired_rejects_unverified_job_without_creating_state(self) -> None:
        self._run("config", "--data-dir", str(self.data_dir),
                  "--capture-scope", "all")
        result = self._run(
            "service", "recover-expired", "--data-dir", str(self.data_dir),
            "--project", str(self.project), "--job-id", "a" * 32,
            expected=2,
        )
        self.assertEqual("blocked", result["status"])
        self.assertFalse((self.data_dir / "service-state.json").exists())

    def test_anchor_timeline_and_preview_detail(self) -> None:
        with Store(self.data_dir) as store:
            anchor = store.remember(self.project, "Rollback", "Fixture checked rollback.",
                                    observation={"type": "bugfix", "narrative": "Full fixture evidence."})
            store.remember(self.project, "Later", "Later event.")
        common = ("--data-dir", str(self.data_dir), "--project", str(self.project))
        compact = self._run("search", *common, "--query", "rollback")
        self.assertNotIn("metadata", compact[0])
        full = self._run("search", *common, "--query", "rollback", "--detail", "full")
        self.assertEqual("Full fixture evidence.", full[0]["observation"]["narrative"])
        window = self._run("timeline", *common, "--anchor-id", anchor["id"], "--before", "0", "--after", "1")
        self.assertEqual(2, len(window))
        self.assertTrue(window[0]["is_anchor"])
        self._run("timeline", *common, "--before", "0", expected=2)

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

    def test_usage_records_task_agents_and_models_without_replay_double_count(self) -> None:
        self._run("config", "--data-dir", str(self.data_dir), "--capture-scope", "all")
        home = Path(self.temporary.name) / "codex"
        sessions = home / "sessions"
        sessions.mkdir(parents=True)
        for thread, models in (("root", ["model-a", "model-b"]), ("child", ["model-c"])):
            records = [{"type": "session_meta", "payload": {
                "id": thread, "session_id": "root", "cwd": str(self.project),
                "parent_thread_id": "root" if thread == "child" else None,
                "model_provider": "openai"}}]
            for index, model in enumerate(models):
                records.extend([
                    {"type": "turn_context", "payload": {"turn_id": str(index), "model": model}},
                    {"type": "token_usage_record", "payload": {
                        "thread_id": thread, "session_id": "root", "turn_id": str(index),
                        "response_id": f"{thread}-{index}", "usage": {
                            "input_tokens": 100, "cached_input_tokens": 40, "cache_write_input_tokens": 0,
                            "output_tokens": 20, "reasoning_output_tokens": 5, "total_tokens": 120}}}])
            (sessions / f"{thread}.jsonl").write_text("".join(json.dumps(row) + "\n" for row in records))
        for _ in range(2):
            self._run("usage", "scan", "--data-dir", str(self.data_dir), "--codex-home", str(home))
        result = self._run("usage", "status", "--data-dir", str(self.data_dir), "--session-id", "root")
        rows = result["records"]
        self.assertEqual({row["model"] for row in rows}, {"model-a", "model-b", "model-c"})
        self.assertEqual(sum(row["total_tokens"] for row in rows), 360)
        self.assertEqual(sum(row["event_count"] for row in rows), 3)
        child = next(row for row in rows if row["thread_id"] == "child")
        self.assertEqual(child["parent_thread_id"], "root")

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

    def test_resume_pending_requires_a_verified_rejected_job(self) -> None:
        from codex_mem.config import configure
        configure(self.data_dir, capture_scope="selected", included_projects=[self.project])
        result = self._run(
            "service", "resume-pending", "--data-dir", str(self.data_dir),
            "--project", str(self.project), "--rejected-job-id", "a" * 32,
            expected=2,
        )
        self.assertEqual("blocked", result["status"])
        self.assertEqual("rejection_unavailable", result["code"])

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

    def test_structured_observation_filters_and_skip_tools_config(self) -> None:
        remembered = self._run(
            "remember",
            "--data-dir",
            str(self.data_dir),
            "--project",
            str(self.project),
            "--title",
            "Structured note",
            "--body",
            "The metadata carries the sqlite decision.",
            "--observation-type",
            "decision",
            "--concept",
            "sqlite",
            "--file-read",
            "codex_mem/store.py",
        )
        entry_id = remembered["id"]

        other_project = self.temporary.name + "/other-project"
        Path(other_project).mkdir()
        other = self._run(
            "remember",
            "--data-dir",
            str(self.data_dir),
            "--project",
            other_project,
            "--title",
            "Other project note",
            "--body",
            "The same sqlite decision exists in another project.",
            "--observation-type",
            "decision",
            "--concept",
            "sqlite",
            "--file-read",
            "codex_mem/store.py",
        )

        searched = self._run(
            "search",
            "--data-dir",
            str(self.data_dir),
            "--project",
            str(self.project),
            "--query",
            "sqlite",
            "--mode",
            "lexical",
            "--type",
            "decision",
            "--concept",
            "sqlite",
            "--file",
            "codex_mem/store.py",
        )
        self.assertEqual([entry_id], [item["id"] for item in searched["results"]])
        self.assertNotIn(other["id"], [item["id"] for item in searched["results"]])

        context = self._run(
            "context",
            "--data-dir",
            str(self.data_dir),
            "--project",
            str(self.project),
            "--type",
            "decision",
            "--concept",
            "sqlite",
            "--file",
            "codex_mem/store.py",
        )
        self.assertIn(entry_id, context["context"])

        configured = self._run(
            "config",
            "--data-dir",
            str(self.data_dir),
            "--set",
            'skip_tools=["shell","browser"]',
        )
        self.assertEqual(["shell", "browser"], configured["tool_skip_list"])

    def test_tool_uses_accepts_native_punctuation_and_256_char_id(self) -> None:
        tool_id = "native.call:" + ("x" * 244)
        self.assertEqual(256, len(tool_id))
        with Store(self.data_dir) as store:
            capture = normalize_capture(
                {
                    "tool_name": "shell",
                    "tool_use_id": tool_id,
                    "session_id": "session-a",
                    "tool_input": {"command": "echo safe"},
                    "tool_response": {"status": "ok"},
                },
                project=str(self.project),
            )
            self.assertIsNotNone(capture)
            assert capture is not None
            store.remember(
                self.project,
                "Captured tool evidence",
                "The tool returned a bounded result.",
                source="hook:PostToolUse",
                tool_capture=capture,
            )

        raw = self._run(
            "tool-uses",
            "--data-dir",
            str(self.data_dir),
            "--project",
            str(self.project),
            "--id",
            tool_id,
        )
        self.assertEqual(tool_id, raw[0]["tool_use_id"])

    def test_tool_uses_bounds_aggregate_with_explicit_field_markers(self) -> None:
        with Store(self.data_dir) as store:
            for index in range(5):
                capture = normalize_capture(
                    {
                        "tool_name": "shell",
                        "tool_use_id": f"large-{index}",
                        "session_id": "large-session",
                        "tool_input": {"payload": "x" * 70_000},
                        "tool_response": {"payload": "y" * 70_000},
                    },
                    project=str(self.project),
                )
                self.assertIsNotNone(capture)
                assert capture is not None
                store.remember(
                    self.project,
                    f"Large raw capture {index}",
                    "bounded raw evidence",
                    source="hook:PostToolUse",
                    tool_capture=capture,
                )

        raw = self._run(
            "tool-uses",
            "--data-dir",
            str(self.data_dir),
            "--project",
            str(self.project),
            "--session-id",
            "large-session",
            "--limit",
            "5",
        )
        self.assertTrue(raw["truncated"])
        self.assertEqual(5, raw["total"])
        self.assertEqual(5, raw["returned"])
        self.assertEqual(5, len(raw["tool_uses"]))
        self.assertTrue(raw["truncated_fields"])
        self.assertTrue(
            json.loads(raw["tool_uses"][0]["tool_input"])["__codex_mem_aggregate_truncated__"]
        )
        self.assertLessEqual(len(json.dumps(raw, ensure_ascii=False).encode()), MAX_RAW_RESULT_BYTES)

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
