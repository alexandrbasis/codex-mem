from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

from codex_mem import hook_process


class HookProcessTests(unittest.TestCase):
    def test_pipe_wrapper_failure_does_not_launch_or_leave_open_descriptors(self) -> None:
        descriptors: list[int] = []
        original_pipe, original_fdopen = os.pipe, os.fdopen
        wrappers = []

        def pipe():
            pair = original_pipe()
            descriptors.extend(pair)
            return pair

        def fdopen(*args, **kwargs):
            if wrappers:
                raise OSError(24, "fixture descriptor limit")
            wrapper = original_fdopen(*args, **kwargs)
            wrappers.append(wrapper)
            return wrapper

        with (mock.patch.object(hook_process, "_can_fork", return_value=True),
              mock.patch.object(hook_process.os, "pipe", side_effect=pipe),
              mock.patch.object(hook_process.os, "fdopen", side_effect=fdopen),
              mock.patch.object(hook_process.os, "fork", create=True) as fork):
            with self.assertRaises(OSError):
                hook_process.start_worker("unused-launcher", dict(os.environ), allow_fork=True)
        fork.assert_not_called()
        self.assertTrue(wrappers[0].closed)
        for fd in descriptors:
            with self.assertRaises(OSError):
                os.fstat(fd)

    def test_failed_fork_closes_all_pipe_descriptors(self) -> None:
        descriptors: list[int] = []
        original_pipe = os.pipe

        def pipe():
            pair = original_pipe()
            descriptors.extend(pair)
            return pair

        with (mock.patch.object(hook_process, "_can_fork", return_value=True),
              mock.patch.object(hook_process.os, "pipe", side_effect=pipe),
              mock.patch.object(hook_process.os, "fork", side_effect=OSError(11, "fixture fork limit"), create=True)):
            with self.assertRaises(OSError):
                hook_process.start_worker("unused-launcher", dict(os.environ), allow_fork=True)
        for fd in descriptors:
            with self.assertRaises(OSError):
                os.fstat(fd)

    def test_threaded_library_caller_uses_exec_and_keeps_response(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            launcher = Path(temporary) / "worker.py"
            launcher.write_text("import sys\nassert sys.argv[1:] == ['--hook-worker']\nprint('{\"continue\": true}')\n")
            with ThreadPoolExecutor(max_workers=1) as pool:
                child = pool.submit(hook_process.start_worker, launcher, dict(os.environ), allow_fork=True).result()
            self.assertIsInstance(child, subprocess.Popen)
            stdout, stderr = child.communicate(timeout=5)
            self.assertEqual(0, child.returncode)
            self.assertEqual(b'{"continue": true}\n', stdout)
            self.assertEqual(b"", stderr)


if __name__ == "__main__":
    unittest.main()
