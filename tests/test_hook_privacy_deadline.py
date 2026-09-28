from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from codex_mem.config import configure
from codex_mem.hooks import _handle_hook, _private_tool_gate_active
from codex_mem.private_gate import private_active
from codex_mem.store import Store


class PrivacyDeadlineTests(unittest.TestCase):
    def test_privacy_marker_survives_failure_before_database_opens(self):
        with tempfile.TemporaryDirectory() as temp:
            data = Path(temp) / "memory"
            project = str((Path(temp) / "project").resolve())
            config = configure(data, capture_scope="all", service_enabled=False)
            payload = dict(hook_event_name="UserPromptSubmit", cwd=project,
                           session_id="private-session", turn_id="turn-1",
                           prompt="<private>Do not capture this request</private>")
            with patch("codex_mem.hooks.Store", side_effect=RuntimeError("unavailable")):
                self.assertEqual({"continue": True}, _handle_hook(payload, store=None, data_dir=data))
            self.assertTrue(_private_tool_gate_active(payload, project, config, data))
            self.assertFalse(_private_tool_gate_active(dict(payload, session_id="other"), project, config, data))
            # Clearing requires a subsequent successfully handled public prompt.
            response = _handle_hook(dict(payload, prompt="Public request", turn_id="turn-2"),
                                    store=None, data_dir=data)
            self.assertTrue(response["continue"])
            self.assertFalse(_private_tool_gate_active(payload, project, config, data))

    def test_private_state_read_error_denies_capture(self):
        with patch("pathlib.Path.lstat", side_effect=PermissionError("denied")):
            self.assertTrue(private_active("session", "/tmp/codex-mem-test-private-read"))

    def test_marker_directory_failure_uses_legacy_gate_before_database(self):
        with tempfile.TemporaryDirectory() as temp:
            data = Path(temp) / "memory"
            project = str((Path(temp) / "project").resolve())
            config = configure(data, capture_scope="all", service_enabled=False)
            payload = dict(hook_event_name="UserPromptSubmit", cwd=project, session_id="private",
                           turn_id="turn-1", prompt="<private>Private fixture</private>")
            with patch("codex_mem.private_gate.mark_private", side_effect=PermissionError("denied")), \
                 patch("codex_mem.hooks.Store", side_effect=RuntimeError("unavailable")):
                _handle_hook(payload, store=None, data_dir=data)
            self.assertTrue(_private_tool_gate_active(payload, project, config, data))

    def test_sqlite_stall_returns_before_host_deadline_and_preserves_privacy(self):
        with tempfile.TemporaryDirectory() as temp:
            data = Path(temp) / "memory"
            project = str((Path(temp) / "project").resolve())
            config = configure(data, capture_scope="all", service_enabled=False, processor_enabled=False)
            payload = dict(hook_event_name="UserPromptSubmit", cwd=project, session_id="sqlite-private",
                           turn_id="turn-1", prompt="<private>Private lock fixture</private>")
            launcher = Path(__file__).resolve().parents[1] / "scripts" / "codex-mem.py"
            with Store(data) as holder:
                holder._connection.execute("BEGIN IMMEDIATE")
                started = time.monotonic()
                try:
                    result = subprocess.run([sys.executable, str(launcher), "hook"],
                        input=json.dumps(payload), capture_output=True, text=True,
                        env=dict(os.environ, CODEX_MEM_HOME=str(data)), timeout=3)
                finally:
                    holder._connection.execute("ROLLBACK")
                self.assertLess(time.monotonic() - started, 2.85)
                self.assertEqual(0, result.returncode, result.stderr)
                response = json.loads(result.stdout)
                self.assertTrue(response["continue"])
                self.assertIn("unconfirmed", response["systemMessage"])
                self.assertTrue(_private_tool_gate_active(payload, project, config, data))
                tool = dict(payload, hook_event_name="PostToolUse", tool_name="Bash",
                            tool_use_id="private-tool", tool_input={"command": "echo fixture"},
                            tool_response={"output": "PRIVATE TOOL RESULT", "exit_code": 0})
                _handle_hook(tool, store=holder, data_dir=data)
                self.assertEqual([], holder.timeline(project))
            recovery = subprocess.run([sys.executable, str(launcher), "hook"],
                input=json.dumps(dict(payload, turn_id="turn-2", prompt="Public recovery fixture")),
                capture_output=True, text=True, env=dict(os.environ, CODEX_MEM_HOME=str(data)), timeout=3)
            self.assertEqual(0, recovery.returncode, recovery.stderr)
            self.assertNotIn("systemMessage", json.loads(recovery.stdout))
            self.assertFalse(_private_tool_gate_active(payload, project, config, data))

    @unittest.skipIf(os.name == "nt", "flock is POSIX-only")
    def test_private_prompt_remains_gated_while_shared_state_is_locked(self):
        import fcntl
        with tempfile.TemporaryDirectory() as temp:
            data=Path(temp)/"memory"; project=str((Path(temp)/"project").resolve())
            config=configure(data,capture_scope="all",service_enabled=False)
            payload=dict(hook_event_name="UserPromptSubmit",cwd=project,session_id="private",
                         turn_id="turn-1",prompt="<private>Private test request</private>")
            launcher=Path(__file__).resolve().parents[1]/"scripts"/"codex-mem.py"
            with (data/".hook-state.lock").open("a+") as lock:
                fcntl.flock(lock.fileno(),fcntl.LOCK_EX)
                result=subprocess.run([sys.executable,str(launcher),"hook"], input=json.dumps(payload),
                    capture_output=True,text=True,env=dict(os.environ,CODEX_MEM_HOME=str(data)),timeout=3)
                self.assertEqual(0,result.returncode,result.stderr)
                self.assertTrue(json.loads(result.stdout)["continue"])
                self.assertTrue(_private_tool_gate_active(payload,project,config,data))


if __name__ == "__main__":
    unittest.main()
