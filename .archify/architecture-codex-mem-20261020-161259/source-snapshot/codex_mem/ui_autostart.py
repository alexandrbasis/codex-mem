"""An owned macOS LaunchAgent for the local dashboard."""
from __future__ import annotations

import os
from pathlib import Path
import plistlib
import re
import stat
import subprocess
import sys

from .config import data_dir_path
from .dashboard import DashboardServerError

LABEL = "com.codex-mem.dashboard"
MARKER = "CODEX_MEM_UI_MANAGED"
ERROR = "Cannot manage dashboard autostart. Check your macOS LaunchAgents permissions and launchctl session, then retry."


def _call(args, run):
    try:
        return run(["/bin/launchctl", *args], capture_output=True, text=True, timeout=5, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        raise DashboardServerError(ERROR) from exc


def _loaded(target, path, run):
    result = _call(["print", target], run)
    if result.returncode:
        if result.returncode in (3, 113) or "Could not find service" in result.stderr:
            return False
        raise DashboardServerError(ERROR)
    match = re.search(r"^\s*path = (.+)$", result.stdout, re.MULTILINE)
    if not match or match.group(1).strip() != str(path):
        raise DashboardServerError("Dashboard autostart label belongs to another job. Preserve that job and choose a different setup.")
    return True


def _existing(path):
    if path.is_symlink():
        raise DashboardServerError("Dashboard LaunchAgent is a symlink. Remove the link yourself before installing autostart.")
    if not path.exists():
        return None
    if not path.is_file():
        raise DashboardServerError("Dashboard LaunchAgent is not a regular plist file. Preserve it before installing autostart.")
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as handle:
            _guard_file(handle.fileno())
            value = plistlib.load(handle)
    except (OSError, ValueError, plistlib.InvalidFileException) as exc:
        raise DashboardServerError("Dashboard LaunchAgent is not a managed plist. Preserve it before installing autostart.") from exc
    environment = value.get("EnvironmentVariables") if isinstance(value, dict) else None
    if not isinstance(value, dict) or value.get("Label") != LABEL or not isinstance(environment, dict) or environment.get(MARKER) != "1":
        raise DashboardServerError("Dashboard LaunchAgent is unmanaged. Preserve it before installing autostart.")
    return value


def _context(home, platform):
    if platform != "darwin":
        raise DashboardServerError("Dashboard autostart requires macOS. Use codex-mem ui for a foreground dashboard.")
    path = Path(home) / "Library" / "LaunchAgents" / (LABEL + ".plist")
    return path, "gui/" + str(os.getuid()), "gui/" + str(os.getuid()) + "/" + LABEL


def _private_log(path):
    flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW | os.O_NONBLOCK
    descriptor = os.open(path, flags, 0o600)
    try:
        _guard_file(descriptor)
        os.fchmod(descriptor, 0o600)
    finally:
        os.close(descriptor)


def _guard_file(descriptor):
    info = os.fstat(descriptor)
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != os.getuid():
        raise DashboardServerError("Dashboard file must be a regular file owned by you with no hard links. Preserve it before installing autostart.")


def _write_plist(path, desired, old):
    flags = os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK
    try:
        fd = os.open(path, flags | os.O_CREAT | os.O_EXCL, 0o600)
        created = True
    except FileExistsError:
        fd = os.open(path, flags)
        created = False
    with os.fdopen(fd, "r+b") as handle:
        _guard_file(handle.fileno())
        if not created:
            try:
                current = plistlib.load(handle)
            except (ValueError, plistlib.InvalidFileException) as exc:
                raise DashboardServerError("Dashboard LaunchAgent changed during installation. Preserve it and retry.") from exc
            if old is None or current != old:
                raise DashboardServerError("Dashboard LaunchAgent changed during installation. Preserve it and retry.")
        os.fchmod(handle.fileno(), 0o600)
        handle.seek(0)
        handle.truncate()
        plistlib.dump(desired, handle)


def install_autostart(data_dir=None, port=8765, *, _home=None, _run=None, _platform=None):
    """Install or start our job; repeated identical installs leave it running."""
    run = _run or subprocess.run
    path, domain, target = _context(_home or Path.home(), _platform or sys.platform)
    if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
        raise DashboardServerError("Dashboard autostart port must be between 1 and 65535.")
    old = _existing(path)
    loaded = _loaded(target, path, run)
    if loaded and old is None:
        raise DashboardServerError("Dashboard autostart job has no managed plist. Preserve it before installing autostart.")
    base = data_dir_path(data_dir)
    stdout, stderr = base / "dashboard.stdout.log", base / "dashboard.stderr.log"
    for log in (stdout, stderr):
        if log.is_symlink() or (log.exists() and not log.is_file()):
            raise DashboardServerError("Dashboard log path is not a regular file. Preserve it before installing autostart.")
    launcher = Path(__file__).resolve().parents[1] / "scripts" / "codex-mem.py"
    desired = {
        "Label": LABEL,
        "ProgramArguments": [sys.executable, str(launcher), "--data-dir", str(base), "ui", "--port", str(port), "--persistent-token"],
        "EnvironmentVariables": {MARKER: "1"},
        "RunAtLoad": True, "KeepAlive": True, "ThrottleInterval": 10,
        # This process serves user-requested pages over HTTP, rather than XPC.
        # Background classification throttles reads enough to miss deadlines.
        "ProcessType": "Interactive", "StandardOutPath": str(stdout), "StandardErrorPath": str(stderr),
    }
    try:
        base.mkdir(parents=True, exist_ok=True, mode=0o700)
        for log in (stdout, stderr):
            _private_log(log)
        path.parent.mkdir(parents=True, exist_ok=True)
        if desired != old:
            if loaded:
                if _call(["bootout", target], run).returncode:
                    raise DashboardServerError(ERROR)
                loaded = False
            _write_plist(path, desired, old)
        if not loaded:
            if _call(["bootstrap", domain, str(path)], run).returncode:
                raise DashboardServerError(ERROR)
        if not _loaded(target, path, run):
            raise DashboardServerError(ERROR)
    except OSError as exc:
        raise DashboardServerError(ERROR) from exc
    return {"status": "installed", "path": str(path), "port": port}


def remove_autostart(data_dir=None, *, _home=None, _run=None, _platform=None):
    """Remove only our exact managed job, preserving logs and dashboard data."""
    run = _run or subprocess.run
    path, _, target = _context(_home or Path.home(), _platform or sys.platform)
    old = _existing(path)
    if old is None:
        return {"status": "absent", "path": str(path)}
    if _loaded(target, path, run):
        if _call(["bootout", target], run).returncode:
            raise DashboardServerError(ERROR)
    try:
        path.unlink()
    except OSError as exc:
        raise DashboardServerError(ERROR) from exc
    return {"status": "removed", "path": str(path)}
