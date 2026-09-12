#!/usr/bin/env python3
"""Verify managed runtime refresh with disposable data and real idle workers."""
from __future__ import annotations

from contextlib import contextmanager
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from codex_mem.config import configure
from codex_mem.service import (
    SERVICE_STATE_FILENAME, SERVICE_STARTUP_ENV, ServiceAlreadyRunning,
    _PidLock, _new_record, _new_state, _write_state,
)
from codex_mem.store import Store

_SPEC = importlib.util.spec_from_file_location("acceptance_installer", ROOT / "scripts/install.py")
installer = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(installer)


@contextmanager
def isolated_environment(temporary: Path, data: Path):
    """Only this disposable acceptance process and its children see these paths."""
    changes = {
        "HOME": str(temporary / "home"),
        "CODEX_HOME": str(temporary / "codex-home"),
        "CODEX_MEM_HOME": str(data),
        "PATH": str(temporary / "empty-bin"),
        "PYTHONDONTWRITEBYTECODE": "1",
        "CODEX_MEM_DISABLED": None,
        "PYTHONPATH": None,
        SERVICE_STARTUP_ENV: None,
    }
    previous = {name: os.environ.get(name) for name in changes}
    try:
        for name, value in changes.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        yield
    finally:
        for name, value in previous.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


def wait_status(launcher: Path, data: Path, predicate, timeout: float = 10.0) -> dict:
    deadline = time.monotonic() + timeout
    last = {"status": "unavailable"}
    while time.monotonic() < deadline:
        last = installer._service_command(launcher, data, "status", timeout=2.0)
        if predicate(last):
            return last
        time.sleep(0.05)
    raise AssertionError(f"Disposable service status did not settle: {last.get('status')}")


def database_snapshot(data: Path) -> dict[str, str]:
    result = {}
    for database in sorted(data.glob("*.sqlite3")):
        with sqlite3.connect(database.as_uri() + "?mode=ro", uri=True) as connection:
            result[database.name] = "\n".join(connection.iterdump())
    return result


def run() -> dict:
    temporary = Path(tempfile.mkdtemp(prefix="codex-mem-installer-runtime-")).resolve()
    target = temporary / "managed"
    data = temporary / "memory"
    project = temporary / "project"
    launcher = target / "scripts/codex-mem.py"
    old_process = None
    cleanup_verified = False
    result = {"status": "failed", "fixture": "current code with a prior runtime version",
              "native_processing_enabled": False, "usage_collection_enabled": False,
              "semantic_indexing_enabled": False}
    for directory in (target, project, temporary / "home", temporary / "codex-home", temporary / "empty-bin"):
        directory.mkdir()
    try:
        for name in ("codex_mem", ".codex-plugin", "hooks"):
            shutil.copytree(ROOT / name, target / name,
                            ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        (target / "scripts").mkdir()
        shutil.copy2(ROOT / "scripts/codex-mem.py", launcher)
        shutil.copy2(ROOT / ".mcp.json", target / ".mcp.json")
        (target / installer.MARKER).write_text("disposable installer acceptance fixture\n")
        init_path = target / "codex_mem/__init__.py"
        current_init = init_path.read_text()
        manifest_path = target / ".codex-plugin/plugin.json"
        current_manifest = json.loads(manifest_path.read_text())
        current_version = current_manifest["version"]
        old_init, replacements = re.subn(r'^__version__\s*=\s*[^\n]+$',
                                         '__version__ = "0.0.0"', current_init, flags=re.MULTILINE)
        assert replacements == 1, "Cannot construct the prior-version fixture"
        init_path.write_text(old_init)
        manifest_path.write_text(json.dumps({**current_manifest, "version": "0.0.0"}))
        configure(data, capture_scope="selected", included_projects=[project], service_enabled=True,
                  processor_enabled=False, semantic_enabled=False, usage_enabled=False)
        with Store(data) as store:
            store.remember(str(project), "Disposable restart sentinel", "This record must survive a runtime refresh.")
        queue = _new_state()
        queue["projects"][str(project)] = {**_new_record(time.time() + 3600), "parked": "semantic_disabled"}
        _write_state(data, queue)
        with isolated_environment(temporary, data):
            try:
                with (temporary / "worker.stderr").open("w") as errors:
                    old_process = subprocess.Popen(
                        [sys.executable, str(launcher), "--data-dir", str(data), "service", "run"],
                        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=errors,
                    )
                before = wait_status(launcher, data, installer._verified_running)
                assert before["pid"] == old_process.pid and before["runtime_version"] == "0.0.0"
                database_before = database_snapshot(data)
                config_before = (data / "config.json").read_bytes()
                queue_before = json.loads((data / SERVICE_STATE_FILENAME).read_text())["projects"]

                # Installed files change while the prior worker retains its
                # imported version, reproducing the update/restart boundary.
                init_path.write_text(current_init)
                manifest_path.write_text(json.dumps(current_manifest))
                still_old = installer._service_command(launcher, data, "status")
                assert still_old["runtime_version"] == "0.0.0", "File replacement falsely changed runtime evidence"
                refresh = installer._refresh_runtime(target, current_version, data, before)
                assert refresh["status"] == "restarted", refresh
                after = wait_status(launcher, data, installer._verified_running)
                assert after["runtime_version"] == current_version
                assert after["owner_id"] != before["owner_id"] and after["pid"] != old_process.pid
                assert old_process.wait(timeout=5) == 0, "Prior disposable service did not exit cleanly"

                competing_lock = _PidLock(data, time.time())
                try:
                    competing_lock.acquire()
                except ServiceAlreadyRunning:
                    pass
                else:
                    competing_lock.release()
                    raise AssertionError("Restarted service did not retain its exclusive lock")
                repeated_start = installer._service_command(launcher, data, "start")
                assert repeated_start == {"status": "running", "pid": after["pid"]}
                assert installer._service_command(launcher, data, "status")["owner_id"] == after["owner_id"]
                assert database_snapshot(data) == database_before, "Durable database data changed during refresh"
                assert (data / "config.json").read_bytes() == config_before, "User configuration changed"
                assert json.loads((data / SERVICE_STATE_FILENAME).read_text())["projects"] == queue_before
                assert not list((temporary / "codex-home").iterdir()), "Acceptance touched its empty Codex history root"
                result.update(status="passed", previous_runtime_version=before["runtime_version"],
                              runtime_version=current_version, old_process_exited=True,
                              owner_changed=True, exclusive_lock_verified=True,
                              duplicate_start_prevented=True, databases_unchanged=True,
                              configuration_unchanged=True, queued_projects_unchanged=True)
            finally:
                # These paths were created by this run. Cleanup never signals a
                # PID and will retain the fixture if ownership is unverifiable.
                status = wait_status(launcher, data, lambda item:
                                     installer._verified_running(item) or installer._verified_stopped(item))
                if installer._verified_running(status):
                    stopped = installer._service_command(launcher, data, "stop", expected_owner=status["owner_id"])
                    assert stopped.get("status") == "stopping", "Disposable cooperative cleanup was not accepted"
                    wait_status(launcher, data, installer._verified_stopped, timeout=15)
                if old_process is not None:
                    old_process.wait(timeout=5)
                cleanup_verified = True
    except Exception as error:
        result.update(status="failed", error=str(error))
    finally:
        result["cleanup_verified"] = cleanup_verified
        if cleanup_verified or old_process is None:
            shutil.rmtree(temporary)
        else:
            result["retained_fixture"] = str(temporary)
    return result


if __name__ == "__main__":
    receipt = run()
    print(json.dumps(receipt, indent=2))
    raise SystemExit(0 if receipt["status"] == "passed" and receipt["cleanup_verified"] else 1)
