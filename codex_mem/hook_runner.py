"""Bound the POSIX native hook without reading its stdin in the parent.

This launcher targets the plugin's macOS/Linux hosts; selector pipe handling
has no Windows support guarantee.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import selectors
import signal
import subprocess
import sys
import threading
import time


HOOK_WORKER_TIMEOUT_SECONDS = 2.0
MAX_RESPONSE_BYTES = 64 * 1024
MAX_DIAGNOSTIC_BYTES = 4 * 1024
_FALLBACK_REASONS = {
    "timeout": "hook worker exceeded its deadline",
    "invalid-output": "hook worker returned an invalid response",
    "worker-failed": "hook worker exited with an error",
    "launch-failed": "hook worker could not start",
}
_SAFE_DIAGNOSTICS = frozenset(
    f"codex-mem hook: {reason}" for reason in (
        "configuration unavailable",
        "memory unavailable",
        "state unavailable",
        "privacy state unavailable",
        "context unavailable",
        "capture unavailable",
        "queue unavailable",
        "invalid arguments",
        "invalid input",
    )
)


def _safe_stderr(raw: bytes) -> None:
    """Forward only known, fixed hook messages, never payload-derived text."""
    for line in raw.decode("utf-8", errors="ignore").splitlines():
        if line in _SAFE_DIAGNOSTICS:
            print(line, file=sys.stderr)


def _parse_response(raw: bytes) -> dict[str, object] | None:
    if not raw or len(raw) > MAX_RESPONSE_BYTES:
        return None
    try:
        value = json.loads(
            raw.decode("utf-8"),
            parse_constant=lambda _: (_ for _ in ()).throw(ValueError("invalid JSON constant")),
        )
    except (UnicodeDecodeError, ValueError, RecursionError):
        return None
    if not isinstance(value, dict) or value.get("continue") is not True:
        return None
    return value


def _collect_output(child: subprocess.Popen[bytes], deadline: float) -> tuple[bytes, bytes]:
    stdout = bytearray()
    stderr = bytearray()
    with selectors.DefaultSelector() as selector:
        for stream in (child.stdout, child.stderr):
            assert stream is not None
            os.set_blocking(stream.fileno(), False)
            selector.register(stream, selectors.EVENT_READ)
        while selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError
            for key, _ in selector.select(remaining):
                chunk = os.read(key.fileobj.fileno(), 8192)
                if not chunk:
                    selector.unregister(key.fileobj)
                    continue
                if key.fileobj is child.stdout:
                    stdout.extend(chunk)
                    if len(stdout) > MAX_RESPONSE_BYTES:
                        raise ValueError("worker response too large")
                elif len(stderr) < MAX_DIAGNOSTIC_BYTES:
                    stderr.extend(chunk[: MAX_DIAGNOSTIC_BYTES - len(stderr)])
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError
        child.wait(timeout=remaining)
    return bytes(stdout), bytes(stderr)


def run_hook(launcher: str | Path, *, timeout: float = HOOK_WORKER_TIMEOUT_SECONDS) -> int:
    """Run one worker and fail open if it misses the deadline or response contract.

    The child inherits stdin. The parent never consumes or copies the hook
    payload, and no child stdout reaches the host before validation.
    """
    deadline = time.monotonic() + timeout
    response: dict[str, object] | None = None
    diagnostics = b""
    failure = "launch-failed"
    try:
        environment = os.environ.copy()
        environment["CODEX_MEM_HOOK_DEADLINE"] = repr(deadline)
        child = subprocess.Popen(
            [sys.executable, str(launcher), "--hook-worker"],
            stdin=None,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=environment,
        )
        previous_handlers: dict[signal.Signals, object] = {}
        if threading.current_thread() is threading.main_thread():
            def cancel(signum: int, _frame: object) -> None:
                # Kill only this hook worker. Detached service workers own their
                # own lifecycle and must not be signalled with its process group.
                if child.poll() is None:
                    child.kill()
                child.wait()
                raise SystemExit(128 + signum)

            for signum in (signal.SIGTERM, signal.SIGINT):
                previous_handlers[signum] = signal.getsignal(signum)
                signal.signal(signum, cancel)
        try:
            stdout, diagnostics = _collect_output(child, deadline)
            if child.returncode == 0:
                response = _parse_response(stdout)
                failure = "invalid-output"
            else:
                failure = "worker-failed"
        except (TimeoutError, subprocess.TimeoutExpired):
            failure = "timeout"
        except ValueError:
            failure = "invalid-output"
        finally:
            try:
                if child.poll() is None:
                    child.kill()
                child.wait()
                child.stdout.close()
                child.stderr.close()
            finally:
                for signum, handler in previous_handlers.items():
                    signal.signal(signum, handler)
    except OSError:
        pass

    if response is None:
        response = {
            "continue": True,
            "systemMessage": (
                f"codex-mem: {_FALLBACK_REASONS[failure]}; memory capture or context "
                "delivery for this hook invocation is unconfirmed or skipped."
            ),
        }
        print(f"codex-mem hook: worker {failure}", file=sys.stderr)
    else:
        _safe_stderr(diagnostics)
    print(json.dumps(response, ensure_ascii=False))
    return 0
