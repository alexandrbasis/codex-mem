from __future__ import annotations

import errno
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from codex_mem import config


@unittest.skipIf(config.fcntl is None, "flock is unavailable on this platform")
class HookStateLockTests(unittest.TestCase):
    def test_held_lock_times_out_without_writing_and_recovers_after_release(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            lock_path = base / ".hook-state.lock"
            holder = subprocess.Popen(
                [
                    sys.executable,
                    "-u",
                    "-c",
                    (
                        "import fcntl, sys\n"
                        "with open(sys.argv[1], 'a+') as handle:\n"
                        "    fcntl.flock(handle.fileno(), fcntl.LOCK_EX)\n"
                        "    print('READY', flush=True)\n"
                        "    sys.stdin.readline()\n"
                    ),
                    str(lock_path),
                ],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            try:
                self.assertEqual("READY\n", holder.stdout.readline())
                lock = config._HookStateLock(base)
                opened = []
                original_open = Path.open

                def record_open(path: Path, *args: object, **kwargs: object):
                    handle = original_open(path, *args, **kwargs)
                    opened.append(handle)
                    return handle

                started = time.monotonic()
                with mock.patch.object(Path, "open", new=record_open):
                    with self.assertRaisesRegex(TimeoutError, "hook state lock"):
                        lock.__enter__()
                self.assertLess(time.monotonic() - started, 1.0)
                self.assertIsNone(lock.handle)
                self.assertEqual(1, len(opened))
                self.assertTrue(opened[0].closed)

                with self.assertRaises(TimeoutError):
                    config.mark_context_injected(
                        "session:a", source="context:one", data_dir=base
                    )
                self.assertFalse((base / config.HOOK_STATE_FILENAME).exists())
            finally:
                holder.stdin.write("\n")
                holder.stdin.flush()
                holder.communicate(timeout=5)

            self.assertEqual(0, holder.returncode)
            config.mark_context_injected("session:a", source="context:one", data_dir=base)
            self.assertTrue(
                config.context_was_injected(
                    "session:a", source="context:one", data_dir=base
                )
            )

    def test_unexpected_acquire_error_closes_descriptor_and_propagates(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            lock = config._HookStateLock(Path(temporary))
            opened = []
            original_open = Path.open

            def record_open(path: Path, *args: object, **kwargs: object):
                handle = original_open(path, *args, **kwargs)
                opened.append(handle)
                return handle

            with mock.patch.object(
                config.fcntl, "flock", side_effect=OSError(errno.EBADF, "bad descriptor")
            ), mock.patch.object(Path, "open", new=record_open):
                with self.assertRaises(OSError) as caught:
                    lock.__enter__()
            self.assertEqual(errno.EBADF, caught.exception.errno)
            self.assertIsNone(lock.handle)
            self.assertEqual(1, len(opened))
            self.assertTrue(opened[0].closed)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
