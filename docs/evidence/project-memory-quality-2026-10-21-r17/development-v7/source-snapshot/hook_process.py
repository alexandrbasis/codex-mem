"""A POSIX worker for the lean hook launcher, without another Python exec."""

from __future__ import annotations

import os
from pathlib import Path
import signal
import subprocess
import sys
import threading
import time
from typing import BinaryIO


class ForkedWorker:
    def __init__(self, pid: int, stdout: BinaryIO, stderr: BinaryIO) -> None:
        self.pid = pid
        self.stdout = stdout
        self.stderr = stderr
        self.returncode: int | None = None

    def poll(self) -> int | None:
        if self.returncode is None:
            try:
                pid, status = os.waitpid(self.pid, os.WNOHANG)
            except ChildProcessError:
                self.returncode = 255
            else:
                if pid:
                    self.returncode = os.waitstatus_to_exitcode(status)
        return self.returncode

    def wait(self, timeout: float) -> int:
        deadline = time.monotonic() + timeout
        while self.poll() is None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise subprocess.TimeoutExpired("hook worker", timeout)
            time.sleep(min(0.005, remaining))
        assert self.returncode is not None
        return self.returncode

    def kill(self) -> None:
        if self.poll() is None:
            try:
                os.kill(self.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass


def _can_fork() -> bool:
    # Fork only a fresh, single-threaded launcher with untouched standard I/O.
    # Library callers with threads or redirected Python streams use exec.
    if not hasattr(os, "fork") or threading.current_thread() is not threading.main_thread():
        return False
    if threading.active_count() != 1:
        return False
    try:
        return all(stream.fileno() == fd for stream, fd in
                   ((sys.stdin, 0), (sys.stdout, 1), (sys.stderr, 2)))
    except (AttributeError, OSError, ValueError):
        return False


def _child_main(launcher: str | Path, environment: dict[str, str]) -> None:
    status = 1
    try:
        # runpy reaches the existing --hook-worker entrypoint, which starts a
        # fresh trace and imports hook/storage code only inside this process.
        import runpy
        os.environ.update(environment)
        sys.argv = [str(launcher), "--hook-worker"]
        try:
            runpy.run_path(str(launcher), run_name="__main__")
            status = 0
        except SystemExit as exc:
            status = exc.code if isinstance(exc.code, int) else 0 if exc.code is None else 1
    except BaseException as exc:
        from .hook_diagnostics import begin_trace, finish_trace, get_trace, trace_error
        if get_trace() is None or get_trace().component != "worker":
            begin_trace("worker", run_id=environment.get("CODEX_MEM_HOOK_RUN_ID"))
        trace_error("worker_bootstrap_failed", exc)
        finish_trace("failed")
    finally:
        try:
            sys.stdout.flush()
            sys.stderr.flush()
        except BaseException:
            status = 1
        # Do not run inherited parent atexit handlers or flush its buffers twice.
        os._exit(status if 0 <= status <= 255 else 1)


def start_worker(launcher: str | Path, environment: dict[str, str], *,
                 allow_fork: bool = False) -> subprocess.Popen[bytes] | ForkedWorker:
    if not allow_fork or not _can_fork():
        return subprocess.Popen([sys.executable, str(launcher), "--hook-worker"],
                                stdin=None, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, env=environment)
    sys.stdout.flush()
    sys.stderr.flush()
    opened: list[int] = []
    worker: ForkedWorker | None = None
    stdout: BinaryIO | None = None
    stderr: BinaryIO | None = None
    pid: int | None = None
    handed_off = False
    try:
        stdout_read, stdout_write = os.pipe()
        opened.extend((stdout_read, stdout_write))
        stderr_read, stderr_write = os.pipe()
        opened.extend((stderr_read, stderr_write))
        # Allocate wrappers before creating a process. Resource errors must
        # not leave a worker reading stdin after its supervisor fails open.
        stdout = os.fdopen(stdout_read, "rb", buffering=0)
        opened.remove(stdout_read)
        stderr = os.fdopen(stderr_read, "rb", buffering=0)
        opened.remove(stderr_read)
        worker = ForkedWorker(0, stdout, stderr)
        pid = os.fork()
        if pid == 0:
            try:
                stdout.close()
                stderr.close()
                os.dup2(stdout_write, 1)
                os.dup2(stderr_write, 2)
                os.close(stdout_write)
                os.close(stderr_write)
                _child_main(launcher, environment)
            finally:
                os._exit(1)
        worker.pid = pid
        os.close(stdout_write)
        opened.remove(stdout_write)
        os.close(stderr_write)
        opened.remove(stderr_write)
        handed_off = True
        return worker
    finally:
        if not handed_off:
            if pid and worker is not None:
                worker.pid = pid
                worker.kill()
                try:
                    worker.wait(timeout=0.2)
                except subprocess.TimeoutExpired:
                    pass
            for stream in (stdout, stderr):
                if stream is not None:
                    stream.close()
        for fd in opened:
            os.close(fd)
