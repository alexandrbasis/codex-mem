from __future__ import annotations

import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from codex_mem.tool_io import (
    MAX_TOOL_EXCERPT_CHARS,
    MAX_TOOL_PAYLOAD_BYTES,
    get_tool_capture,
    hydrate_source_tool_io,
    insert_capture,
    install_schema,
    list_tool_captures,
    normalize_capture,
    replay_plan,
)


class ToolIoTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temporary.name) / "captures.sqlite3"
        self.project = str(Path(self.temporary.name) / "project")
        self.connection = sqlite3.connect(self.db_path)
        install_schema(self.connection)
        self.connection.commit()

    def tearDown(self) -> None:
        self.connection.close()
        self.temporary.cleanup()

    def capture(self, *, tool_use_id: str = "tool-1", **overrides: object):
        payload: dict[str, object] = {
            "tool_name": "Bash",
            "session_id": "session-1",
            "turn_id": "turn-1",
            "tool_use_id": tool_use_id,
            "cwd": str(Path(self.temporary.name) / "tool-workdir"),
            "tool_input": {"command": "pytest -q", "text": "useful full input"},
            "tool_response": {"exit_code": 0, "output": "useful full response"},
        }
        payload.update(overrides)
        result = normalize_capture(payload, project=self.project)
        self.assertIsNotNone(result)
        assert result is not None
        return result

    def test_stable_json_redaction_shape_and_bounded_excerpts(self) -> None:
        first = self.capture(
            tool_input={
                "z": "last",
                "command": "cat /tmp/.env",
                "a": "<private>nested <private>secret</private> value</private>",
            },
            tool_response={"output": "<private>unfinished secret"},
        )
        second = self.capture(
            tool_input={
                "a": "<private>nested <private>secret</private> value</private>",
                "command": "cat /tmp/.env",
                "z": "last",
            },
            tool_response={"output": "<private>unfinished secret"},
        )

        self.assertEqual(first.tool_input, second.tool_input)
        self.assertEqual(first.tool_response, second.tool_response)
        self.assertIn('"a":', first.tool_input or "")
        self.assertLessEqual(len((first.tool_input or "").encode("utf-8")), MAX_TOOL_PAYLOAD_BYTES)
        self.assertLessEqual(len(first.input_excerpt or ""), MAX_TOOL_EXCERPT_CHARS)
        self.assertLessEqual(len(first.response_excerpt or ""), MAX_TOOL_EXCERPT_CHARS)
        self.assertNotIn("/tmp/.env", first.tool_input or "")
        self.assertNotIn("nested", first.tool_input or "")
        self.assertNotIn("secret", first.tool_response or "")
        self.assertTrue(first.input_metadata["redacted"])
        self.assertTrue(first.response_metadata["redacted"])
        sensitive_cwd = self.capture(cwd="/tmp/.env")
        self.assertEqual("[REDACTED]", sensitive_cwd.cwd)

    def test_large_payload_is_valid_json_with_explicit_truncation_and_tail(self) -> None:
        value = "prefix-" + ("x" * 90_000) + "-final-error"
        capture = self.capture(tool_input={"text": value})

        assert capture.tool_input is not None
        decoded = json.loads(capture.tool_input)
        self.assertTrue(decoded["__codex_mem_truncated__"])
        self.assertIn("final-error", decoded["tail"])
        self.assertLessEqual(len(capture.tool_input.encode("utf-8")), MAX_TOOL_PAYLOAD_BYTES)
        self.assertTrue(capture.input_metadata["truncated"])
        self.assertGreater(capture.input_metadata["original_bytes"], 90_000)
        # Truncation alone is not reported as a privacy redaction.
        self.assertFalse(capture.input_metadata["redacted"])

    def test_binary_and_media_payloads_are_excluded(self) -> None:
        capture = self.capture(
            tool_input=b"opaque bytes",
            tool_response={"type": "image", "mimeType": "image/png", "data": "pixels"},
        )

        self.assertIsNone(capture.tool_input)
        self.assertTrue(capture.input_metadata["excluded"])
        self.assertIsNone(capture.tool_response)
        self.assertTrue(capture.response_metadata["excluded"])

    def test_sensitive_paths_adjacent_to_shell_operators_are_redacted(self) -> None:
        command = "/tmp/.env&&echo /tmp/.env;echo /tmp/.env|cat"
        capture = self.capture(tool_input=command)

        self.assertIsNotNone(capture.tool_input)
        assert capture.tool_input is not None
        self.assertNotIn("/tmp/.env", capture.tool_input)
        self.assertEqual(3, capture.tool_input.count("[REDACTED]"))
        self.assertIn("&&echo", capture.tool_input)
        self.assertIn("|cat", capture.tool_input)

    def test_top_level_and_nested_media_text_is_excluded(self) -> None:
        bare_base64 = "ABCD" * 64
        data_uri = "data:image/png;base64," + bare_base64

        top_level = self.capture(tool_input=data_uri, tool_response=bare_base64)
        self.assertIsNone(top_level.tool_input)
        self.assertTrue(top_level.input_metadata["excluded"])
        self.assertIsNone(top_level.tool_response)
        self.assertTrue(top_level.response_metadata["excluded"])

        nested = self.capture(
            tool_use_id="nested-media",
            tool_input={
                "parts": [data_uri, bare_base64, "keep this text"],
                "object": {"payload": data_uri},
            },
        )
        self.assertIsNotNone(nested.tool_input)
        assert nested.tool_input is not None
        self.assertNotIn("data:image/png", nested.tool_input)
        self.assertNotIn(bare_base64, nested.tool_input)
        self.assertIn("keep this text", nested.tool_input)

    def test_skip_filters_exclude_memory_tools_and_session_memory_recursion(self) -> None:
        self.assertIsNone(
            normalize_capture(
                {
                    "tool_name": "NoisyTool",
                    "session_id": "s",
                    "tool_use_id": "skip-1",
                    "tool_input": {"value": "x"},
                    "tool_response": {"output": "y"},
                },
                project=self.project,
                config={"tool_skip_list": ["noisytool"]},
            )
        )
        self.assertIsNone(
            normalize_capture(
                {
                    "tool_name": "mcp__codex__memory_search",
                    "session_id": "s",
                    "tool_use_id": "memory-1",
                    "tool_input": {"query": "x"},
                    "tool_response": {"output": "y"},
                },
                project=self.project,
            )
        )
        self.assertIsNone(
            normalize_capture(
                {
                    "tool_name": "Bash",
                    "session_id": "s",
                    "tool_use_id": "recursive-1",
                    "tool_input": {"nested": {"sessionMemory": "session-memory/abc"}},
                    "tool_response": {"output": "y"},
                },
                project=self.project,
            )
        )

    def test_restart_dedupe_readback_hydration_and_inert_replay(self) -> None:
        capture = self.capture(tool_use_id="durable-1")
        row_id = insert_capture(self.connection, "entry-1", self.project, capture)
        self.connection.commit()
        self.assertIsNotNone(row_id)

        # The same identity is idempotent and an altered later payload cannot
        # mutate evidence already visible to a processor.
        replacement = self.capture(
            tool_use_id="durable-1", tool_response={"output": "mutated"}
        )
        self.assertEqual(row_id, insert_capture(self.connection, "entry-2", self.project, replacement))
        self.connection.commit()
        self.assertEqual(1, self.connection.execute("SELECT COUNT(*) FROM tool_uses").fetchone()[0])

        self.connection.close()
        reopened = sqlite3.connect(self.db_path)
        try:
            restored = get_tool_capture(reopened, self.project, "durable-1", session_id="session-1")
            self.assertIsNotNone(restored)
            assert restored is not None
            self.assertEqual("entry-1", restored["entry_id"])
            self.assertEqual(str(Path(self.temporary.name) / "tool-workdir"), restored["cwd"])
            self.assertIn("useful full response", restored["tool_response"])
            self.assertEqual(1, len(list_tool_captures(reopened, self.project, session_id="session-1")))

            source = hydrate_source_tool_io(
                reopened,
                {"id": "entry-1", "body": "short reader-facing excerpt"},
                project=self.project,
            )
            self.assertIn("tool_io", source)
            self.assertIn("useful full input", source["tool_io"]["tool_input"])
            self.assertEqual(restored["cwd"], source["tool_io"]["cwd"])
            self.assertEqual("short reader-facing excerpt", source["body"])

            plan = replay_plan(reopened, self.project, "durable-1", session_id="session-1")
            self.assertEqual(1, len(plan))
            self.assertFalse(plan[0]["execute"])
            self.assertEqual("replay", plan[0]["operation"])
            self.assertIn("pytest -q", plan[0]["tool_input"])
        finally:
            reopened.close()

    def test_store_boundary_redacts_plain_mapping_before_storage(self) -> None:
        row_id = insert_capture(
            self.connection,
            "entry-plain",
            self.project,
            {
                "tool_name": "Bash",
                "session_id": "session-plain",
                "tool_use_id": "plain-1",
                "tool_input": {"command": "cat /tmp/.env", "token": "ghp_" + "A" * 30},
                "tool_response": {"output": "ok"},
            },
        )
        self.connection.commit()
        self.assertIsNotNone(row_id)
        row = get_tool_capture(self.connection, self.project, "plain-1")
        self.assertIsNotNone(row)
        assert row is not None
        self.assertNotIn("/tmp/.env", row["tool_input"])
        self.assertNotIn("ghp_", row["tool_input"])
        self.assertLessEqual(len(row["tool_input"].encode("utf-8")), MAX_TOOL_PAYLOAD_BYTES)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
