from __future__ import annotations

import json
import io
import os
from pathlib import Path
import signal
import shlex
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

from codex_mem.config import configure
from codex_mem.hook_runner import _parse_response, run_hook
from codex_mem.store import Store


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
    from codex_mem.hook_diagnostics import begin_trace, finish_trace, trace_stage
    begin_trace('worker', run_id=os.environ.get('CODEX_MEM_HOOK_RUN_ID'))
    mode = os.environ['HOOK_RUNNER_CASE']
    if mode == 'read_stall':
        sys.stdin.read()
    elif mode == 'sleep':
        with trace_stage('fixture_sleep'):
            Path(os.environ['HOOK_RUNNER_WORKER_PID']).write_text(str(os.getpid()))
            time.sleep(3.0)
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
        finish_trace('failed')
        sys.exit(7)
    elif mode == 'oversize':
        sys.stdout.write('{"continue": true, "context": "' + 'x' * 65536 + '"}')
    elif mode == 'deadline':
        import json
        sys.stdout.write(json.dumps({'continue': True, 'remaining': float(os.environ['CODEX_MEM_HOOK_DEADLINE']) - time.monotonic()}))
    finish_trace()
    sys.exit(0)

from codex_mem.hook_runner import run_hook
if os.environ.get('HOOK_RUNNER_BLOCK_EXEC') == '1':
    import subprocess
    def unavailable_interpreter(*args, **kwargs):
        raise OSError(11, 'second interpreter startup unavailable')
    subprocess.Popen = unavailable_interpreter
if os.environ.get('HOOK_RUNNER_SLOW_REAP') == '1':
    import subprocess
    import codex_mem.hook_runner as runner
    original_start = runner.start_worker
    def slow_reaping_worker(*args, **kwargs):
        child = original_start(*args, **kwargs)
        original_wait = child.wait
        def delayed_wait(timeout=None):
            if timeout is None:
                time.sleep(3)
            elif timeout <= runner.WORKER_REAP_TIMEOUT_SECONDS:
                raise subprocess.TimeoutExpired('delayed exit notification', timeout)
            return original_wait(timeout=timeout)
        child.wait = delayed_wait
        return child
    runner.start_worker = slow_reaping_worker
sys.exit(run_hook(__file__, timeout=float(os.environ.get('HOOK_RUNNER_TIMEOUT', '0.7')), fork_worker=True))
""",
            encoding="utf-8",
        )

    def _env(self, mode: str) -> dict[str, str]:
        env = os.environ.copy()
        env["PYTHONPATH"] = str(ROOT) + os.pathsep + env.get("PYTHONPATH", "")
        env["HOOK_RUNNER_CASE"] = mode
        env["HOOK_RUNNER_MARKER"] = str(self.root / "marker")
        env["HOOK_RUNNER_WORKER_PID"] = str(self.root / "worker.pid")
        env["CODEX_MEM_HOME"] = str(self.root / "memory")
        env["CODEX_MEM_HOOK_LOG"] = "1"
        env["CODEX_MEM_DISABLED"] = "0"
        # Functional cases use the production budget. Tests of open stdin keep
        # a shorter explicit deadline, without relying on interpreter startup.
        env["HOOK_RUNNER_TIMEOUT"] = "2.0"
        return env

    def _log_rows(self) -> list[dict]:
        path = self.root / "memory" / "logs" / "hooks.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines()]

    def _run(self, mode: str) -> subprocess.CompletedProcess[bytes]:
        return subprocess.run(
            [sys.executable, str(self.fake_launcher)],
            input=b"",
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=self._env(mode),
            timeout=5,
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
        rows = self._log_rows()
        self.assertEqual(1, len({row["run_id"] for row in rows}))
        self.assertEqual({"supervisor", "worker"}, {row["component"] for row in rows})
        self.assertEqual("degraded", rows[-1]["status"])
        self.assertNotIn("secret prompt", json.dumps(rows))

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
        self.assertLessEqual(remaining, 2.0)

    def test_fresh_launcher_works_without_starting_another_interpreter(self) -> None:
        env = self._env("valid")
        env["HOOK_RUNNER_BLOCK_EXEC"] = "1"
        result = subprocess.run([sys.executable, str(self.fake_launcher)], input=b"",
                                capture_output=True, env=env, timeout=5)
        response = self._one_response(result)
        self.assertEqual("memory", response.get("hookSpecificOutput", {}).get("additionalContext"))
        rows = self._log_rows()
        self.assertTrue(any(row.get("code") == "fork" for row in rows))

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
                rows = self._log_rows()
                self.assertEqual("fallback", rows[-1]["status"])
                self.assertEqual("worker_failed" if mode == "nonzero" else "invalid_output",
                                 rows[-1]["code"])

    def test_parser_recursion_error_is_fail_open(self) -> None:
        with mock.patch("codex_mem.hook_runner.json.loads", side_effect=RecursionError):
            self.assertIsNone(_parse_response(b'{"continue": true}'))

    def test_open_stdin_cannot_hold_parent_past_deadline(self) -> None:
        started = time.monotonic()
        env = self._env("read_stall")
        env["HOOK_RUNNER_TIMEOUT"] = "0.7"
        process = subprocess.Popen(
            [sys.executable, str(self.fake_launcher)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
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
        self.assertLess(time.monotonic() - started, 3.5)
        self.assertEqual(0, process.returncode)
        self.assertIn("unconfirmed or skipped", json.loads(stdout)["systemMessage"])
        self.assertIn("exceeded its deadline", json.loads(stdout)["systemMessage"])

    def test_timed_out_worker_is_killed_and_reaped(self) -> None:
        started = time.monotonic()
        response = self._one_response(self._run("sleep"))
        self.assertLess(time.monotonic() - started, 3.5)
        self.assertIn("unconfirmed or skipped", response["systemMessage"])
        self.assertIn("exceeded its deadline", response["systemMessage"])
        rows = self._log_rows()
        self.assertTrue(any(row.get("code") == "worker_timeout" for row in rows))
        self.assertTrue(any(row["component"] == "worker" and row["stage"] == "fixture_sleep"
                            for row in rows))
        self.assertEqual("timeout", rows[-1]["code"])
        self.assertTrue(any(row["stage"] == "worker_reap" for row in rows))
        self.assertTrue(any(row["stage"] == "worker_stream_close" for row in rows))
        worker_pid = int((self.root / "worker.pid").read_text())
        with self.assertRaises(ProcessLookupError):
            os.kill(worker_pid, 0)
        self.assertFalse((self.root / "marker").exists())

    def test_delayed_exit_notification_cannot_hold_host_past_deadline(self) -> None:
        env = self._env("sleep")
        env["HOOK_RUNNER_SLOW_REAP"] = "1"
        started = time.monotonic()
        result = subprocess.run([sys.executable, str(self.fake_launcher)], input=b"",
                                capture_output=True, env=env, timeout=4)
        response = self._one_response(result)
        self.assertLess(time.monotonic() - started, 3.5)
        self.assertIn("exceeded its deadline", response["systemMessage"])
        self.assertTrue(any(row.get("code") == "worker_reap_timeout" for row in self._log_rows()))
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
            wait_until = time.monotonic() + 2.0
            while not pid_file.exists() and time.monotonic() < wait_until:
                time.sleep(0.01)
            self.assertTrue(pid_file.exists(), "worker did not start")
            worker_pid = int(pid_file.read_text())
            process.send_signal(signal.SIGTERM)
            stdout, _ = process.communicate(timeout=2)
            self.assertNotEqual(0, process.returncode)
            self.assertEqual(b"", stdout)
            rows = self._log_rows()
            self.assertEqual("cancelled", rows[-1]["status"])
            self.assertEqual(signal.SIGTERM, rows[-1]["signal"])
            with self.assertRaises(ProcessLookupError):
                os.kill(worker_pid, 0)
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
            timeout=5,
            env=self._env("valid"),
        )
        response = self._one_response(result)
        self.assertIs(response["continue"], True)
        rows = self._log_rows()
        self.assertTrue(any(row["stage"] == "worker_import" for row in rows))
        self.assertTrue(any(row.get("reason") == "invalid_event" for row in rows))

    def test_native_budget_includes_slow_first_interpreter_startup(self) -> None:
        manifest = json.loads((ROOT / "hooks/hooks.json").read_text())
        hook = next(hook for group in manifest["hooks"]["Stop"]
                    for hook in group["hooks"] if not hook.get("async"))
        startup = self.root / "cold-start"
        startup.mkdir()
        (startup / "sitecustomize.py").write_text("import time\ntime.sleep(5.3)\n")
        project = self.root / "project"
        project.mkdir()
        data = self.root / "memory"
        configure(data, capture_scope="all", service_enabled=False,
                  processor_enabled=False, semantic_enabled=False)
        env = self._env("valid")
        env["PYTHONPATH"] = str(startup) + os.pathsep + env["PYTHONPATH"]
        command = hook["command"].replace("${PLUGIN_ROOT}", str(ROOT))
        command = command.replace("python3 ", shlex.quote(sys.executable) + " ", 1)
        result = subprocess.run(["/bin/sh", "-c", command],
            input=json.dumps({"hook_event_name": "Stop", "cwd": str(project),
                              "last_assistant_message": "Fictional cold-start verification."}).encode(),
            capture_output=True, env=env, timeout=hook["timeout"])
        self.assertEqual({"continue": True}, self._one_response(result))
        with Store(data) as store:
            self.assertEqual(1, len(store.timeline(project)))
        rows = self._log_rows()
        self.assertEqual("ok", rows[-1]["status"])
        self.assertLess(rows[-1]["elapsed_ms"], 2000)

    def test_native_stop_keeps_launch_annotation_when_stdin_never_finishes(self) -> None:
        manifest = json.loads((ROOT / "hooks/hooks.json").read_text())
        hook = next(hook for group in manifest["hooks"]["Stop"]
                    for hook in group["hooks"] if not hook.get("async"))
        command = hook["command"].replace("${PLUGIN_ROOT}", str(ROOT))
        command = command.replace("python3 ", shlex.quote(sys.executable) + " ", 1)
        env = self._env("valid")
        env["CODEX_MEM_HOOK_EVENT"] = "Stop"
        process = subprocess.Popen(["/bin/sh", "-c", command], stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
        try:
            process.wait(timeout=hook["timeout"])
            response = json.loads(process.stdout.read())
            self.assertTrue(response["continue"])
            self.assertIn("exceeded its deadline", response["systemMessage"])
            rows = self._log_rows()
            self.assertEqual("Stop", rows[0]["declared_hook_event"])
            self.assertTrue(all(row.get("declared_hook_event") == "Stop" for row in rows))
            self.assertTrue(all("hook_event" not in row for row in rows))
            self.assertEqual("timeout", rows[-1]["code"])
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=3)
            for stream in (process.stdin, process.stdout, process.stderr):
                stream.close()

    def test_launch_error_is_logged_without_sensitive_exception_text(self) -> None:
        stdout, stderr = io.StringIO(), io.StringIO()
        with (mock.patch.dict(os.environ, self._env("valid")),
              mock.patch("codex_mem.hook_runner.subprocess.Popen",
                         side_effect=PermissionError(13, "PRIVATE_LAUNCH_ERROR")),
              mock.patch("sys.stdout", stdout), mock.patch("sys.stderr", stderr)):
            self.assertEqual(0, run_hook(self.fake_launcher))
        self.assertTrue(json.loads(stdout.getvalue())["continue"])
        rows = self._log_rows()
        self.assertEqual("launch_failed", rows[-1]["code"])
        error = next(row["error"] for row in rows if row["event"] == "error")
        self.assertEqual("EACCES", error["errno_name"])
        self.assertNotIn("PRIVATE_LAUNCH_ERROR", json.dumps(rows))

    def test_unavailable_log_does_not_change_worker_response(self) -> None:
        blocked = self.root / "not-directory"
        blocked.write_text("file")
        env = self._env("valid")
        env["CODEX_MEM_HOME"] = str(blocked)
        result = subprocess.run([sys.executable, str(self.fake_launcher)], input=b"",
                                capture_output=True, env=env, timeout=5)
        self.assertEqual("memory", self._one_response(result)["hookSpecificOutput"]["additionalContext"])


if __name__ == "__main__":
    unittest.main()
