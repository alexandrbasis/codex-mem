from __future__ import annotations

import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

from codex_mem.hook_runner import _parse_response


ROOT = Path(__file__).resolve().parents[1]
LAUNCHER = ROOT / "scripts" / "codex-mem.py"


class HookRunnerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.fake_launcher = self.root / "fake_launcher.py"
        self.fake_launcher.write_text(
            """import os
import sys
import time
from pathlib import Path

if sys.argv[1:] == ['--hook-worker']:
    mode = os.environ['HOOK_RUNNER_CASE']
    if mode == 'read_stall':
        sys.stdin.read()
    elif mode == 'sleep':
        Path(os.environ['HOOK_RUNNER_WORKER_PID']).write_text(str(os.getpid()))
        time.sleep(1.5)
        Path(os.environ['HOOK_RUNNER_MARKER']).write_text('worker continued')
    elif mode == 'valid':
        sys.stderr.write('codex-mem hook: context unavailable\\nsecret prompt\\n')
        sys.stdout.write('{"continue": true, "hookSpecificOutput": {"additionalContext": "memory"}}\\n')
    elif mode == 'partial':
        sys.stdout.write('{"continue": true')
    elif mode == 'invalid':
        sys.stdout.write('{"continue": false}')
    elif mode == 'nonzero':
        sys.stdout.write('{"continue": true}')
        sys.exit(7)
    elif mode == 'oversize':
        sys.stdout.write('{"continue": true, "context": "' + 'x' * 65536 + '"}')
    elif mode == 'deadline':
        import json
        sys.stdout.write(json.dumps({'continue': True, 'remaining': float(os.environ['CODEX_MEM_HOOK_DEADLINE']) - time.monotonic()}))
    sys.exit(0)

from codex_mem.hook_runner import run_hook
sys.exit(run_hook(__file__, timeout=float(os.environ.get('HOOK_RUNNER_TIMEOUT', '0.7'))))
""",
            encoding="utf-8",
        )

    def _env(self, mode: str) -> dict[str, str]:
        env = os.environ.copy()
        env["PYTHONPATH"] = str(ROOT) + os.pathsep + env.get("PYTHONPATH", "")
        env["HOOK_RUNNER_CASE"] = mode
        env["HOOK_RUNNER_MARKER"] = str(self.root / "marker")
        env["HOOK_RUNNER_WORKER_PID"] = str(self.root / "worker.pid")
        return env

    def _run(self, mode: str) -> subprocess.CompletedProcess[bytes]:
        return subprocess.run(
            [sys.executable, str(self.fake_launcher)],
            input=b"",
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=self._env(mode),
            timeout=3,
        )

    def _one_response(self, result: subprocess.CompletedProcess[bytes]) -> dict[str, object]:
        self.assertEqual(0, result.returncode)
        self.assertEqual(1, len(result.stdout.splitlines()))
        return json.loads(result.stdout)

    def test_fast_valid_output_and_fixed_diagnostic(self) -> None:
        result = self._run("valid")
        response = self._one_response(result)
        self.assertEqual("memory", response["hookSpecificOutput"]["additionalContext"])
        self.assertIn(b"codex-mem hook: context unavailable", result.stderr)
        self.assertNotIn(b"secret prompt", result.stderr)

    def test_worker_gets_fresh_supervisor_deadline(self) -> None:
        env = self._env("deadline")
        env["CODEX_MEM_HOOK_DEADLINE"] = "0"
        result = subprocess.run(
            [sys.executable, str(self.fake_launcher)],
            input=b"",
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            timeout=3,
        )
        remaining = self._one_response(result)["remaining"]
        self.assertGreater(remaining, 0)
        self.assertLessEqual(remaining, 0.7)

    def test_invalid_partial_nonzero_and_oversize_are_fail_open(self) -> None:
        for mode in ("partial", "invalid", "nonzero", "oversize"):
            with self.subTest(mode=mode):
                response = self._one_response(self._run(mode))
                self.assertIs(response["continue"], True)
                self.assertIn("unconfirmed or skipped", response["systemMessage"])
                self.assertIn(
                    "exited with an error" if mode == "nonzero" else "invalid response",
                    response["systemMessage"],
                )
                self.assertNotIn("hookSpecificOutput", response)

    def test_parser_recursion_error_is_fail_open(self) -> None:
        with mock.patch("codex_mem.hook_runner.json.loads", side_effect=RecursionError):
            self.assertIsNone(_parse_response(b'{"continue": true}'))

    def test_open_stdin_cannot_hold_parent_past_deadline(self) -> None:
        started = time.monotonic()
        process = subprocess.Popen(
            [sys.executable, str(self.fake_launcher)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=self._env("read_stall"),
        )
        try:
            process.wait(timeout=2)
            stdout = process.stdout.read()
        finally:
            process.stdin.close()
            process.stdout.close()
            process.stderr.close()
            if process.poll() is None:
                process.kill()
                process.wait()
        self.assertLess(time.monotonic() - started, 1.5)
        self.assertEqual(0, process.returncode)
        self.assertIn("unconfirmed or skipped", json.loads(stdout)["systemMessage"])
        self.assertIn("exceeded its deadline", json.loads(stdout)["systemMessage"])

    def test_timed_out_worker_is_killed_and_reaped(self) -> None:
        started = time.monotonic()
        response = self._one_response(self._run("sleep"))
        self.assertLess(time.monotonic() - started, 1.5)
        self.assertIn("unconfirmed or skipped", response["systemMessage"])
        self.assertIn("exceeded its deadline", response["systemMessage"])
        time.sleep(1.0)
        self.assertFalse((self.root / "marker").exists())

    def test_sigterm_reaps_worker_without_later_side_effect(self) -> None:
        env = self._env("sleep")
        env["HOOK_RUNNER_TIMEOUT"] = "2.0"
        process = subprocess.Popen(
            [sys.executable, str(self.fake_launcher)],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
        )
        try:
            pid_file = self.root / "worker.pid"
            wait_until = time.monotonic() + 1.5
            while not pid_file.exists() and time.monotonic() < wait_until:
                time.sleep(0.01)
            self.assertTrue(pid_file.exists(), "worker did not start")
            worker_pid = int(pid_file.read_text())
            process.send_signal(signal.SIGTERM)
            stdout, _ = process.communicate(timeout=2)
            self.assertNotEqual(0, process.returncode)
            self.assertEqual(b"", stdout)
            with self.assertRaises(ProcessLookupError):
                os.kill(worker_pid, 0)
            time.sleep(1.5)
            self.assertFalse((self.root / "marker").exists())
        finally:
            if process.poll() is None:
                process.kill()
                process.communicate()

    def test_real_launcher_routes_hook_through_worker(self) -> None:
        result = subprocess.run(
            [sys.executable, str(LAUNCHER), "hook"],
            input=b'{"hook_event_name":"Unknown"}',
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=3,
        )
        response = self._one_response(result)
        self.assertIs(response["continue"], True)


if __name__ == "__main__":
    unittest.main()
