"""Large tool results must be captured before the real hook worker deadline."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from codex_mem.config import configure
from codex_mem.hooks import MAX_STDIN_BYTES, _extract_tool_output, _handle_hook, _session_key
from codex_mem.private_gate import mark_private
from codex_mem.store import Store
from codex_mem.tool_io import MAX_TOOL_PAYLOAD_BYTES, _extract_text, get_tool_capture


LAUNCHER = Path(__file__).resolve().parents[1] / "scripts" / "codex-mem.py"


class LargeHookPayloadTests(unittest.TestCase):
    def test_large_results_are_captured_with_real_worker_deadline(self):
        cases = {
            "unique": [f"Verification row {index:05d}: passed" for index in range(20_000)],
            "repeated": ["x"] * 190_000,
        }
        for name, items in cases.items():
            with self.subTest(name=name), tempfile.TemporaryDirectory() as temp:
                data = Path(temp) / "memory"
                project = (Path(temp) / "project").resolve()
                project.mkdir()
                configure(data, capture_scope="all", service_enabled=False,
                          processor_enabled=False, semantic_enabled=False,
                          jev_retrieval_enabled=False)
                secret = "hidden-fixture-value"
                payload = dict(
                    hook_event_name="PostToolUse", cwd=str(project),
                    session_id="long-session-fixture", turn_id="turn-1",
                    tool_use_id=name, tool_name="functions.exec",
                    tool_input={"code": "verify_fixture()"},
                    tool_response={"output": [
                        "FIRST verification row", *items,
                        f"<private>{secret}</private>", "FINAL verification passed",
                    ]},
                )
                encoded = json.dumps(payload).encode()
                self.assertLess(len(encoded), MAX_STDIN_BYTES)
                result = subprocess.run(
                    [sys.executable, str(LAUNCHER), "hook"], input=encoded,
                    capture_output=True, timeout=6,
                    env=dict(os.environ, CODEX_MEM_HOME=str(data), CODEX_MEM_DISABLED="0"),
                )
                self.assertEqual(0, result.returncode, result.stderr.decode())
                self.assertEqual({"continue": True}, json.loads(result.stdout))
                self.assertEqual(b"", result.stderr)
                with Store(data) as store:
                    entries = store.timeline(project)
                    self.assertEqual(1, len(entries))
                    capture = get_tool_capture(store._connection, str(project), name,
                                               session_id="long-session-fixture")
                self.assertIsNotNone(capture)
                response = capture["tool_response"]
                self.assertLessEqual(len(response.encode()), MAX_TOOL_PAYLOAD_BYTES)
                self.assertIn("FIRST verification row", response)
                self.assertIn("FINAL verification passed", response)
                self.assertNotIn(secret, json.dumps(capture))
                self.assertNotIn(secret, json.dumps(entries))

    def test_output_extractors_preserve_order_and_nested_deduplication(self):
        value = {"output": ["second", "first", "second", {"text": "third"}, "first"]}
        self.assertEqual("second\nfirst\nthird", _extract_tool_output(value))
        self.assertEqual("second\nfirst\nthird", _extract_text(value))

    def test_privacy_enabled_during_store_open_discards_prepared_capture(self):
        with tempfile.TemporaryDirectory() as temp:
            data = Path(temp) / "memory"
            project = (Path(temp) / "project").resolve()
            configure(data, capture_scope="all", service_enabled=False)
            payload = dict(
                hook_event_name="PostToolUse", cwd=str(project),
                session_id="privacy-race", turn_id="turn-1",
                tool_use_id="private-result", tool_name="Bash",
                tool_input={"command": "python3 verify_fixture.py"},
                tool_response={"output": "Do not retain this private result"},
            )

            def open_store(*, data_dir):
                store = Store(data_dir)
                # Another prompt can set this while the tool hook waits for SQLite.
                mark_private(_session_key(payload, str(project)), data_dir)
                return store

            with patch("codex_mem.hooks.Store", side_effect=open_store) as opened:
                self.assertEqual({"continue": True},
                                 _handle_hook(payload, store=None, data_dir=data))
                opened.assert_called_once()
            with Store(data) as store:
                self.assertEqual([], store.timeline(project))
                self.assertEqual(0, store._connection.execute(
                    "SELECT COUNT(*) FROM tool_uses").fetchone()[0])


if __name__ == "__main__":
    unittest.main()
