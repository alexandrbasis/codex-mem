#!/usr/bin/env python3
"""Install or detach the Pi integration in one explicitly selected project."""
from __future__ import annotations

import argparse
import copy
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

from install import ROOT, FILES, is_distribution_path, write_json

SUPPORTED_PI = "1.4.0"
MARKER = ".codex-mem-pi-managed"


def safe_path(path: Path) -> None:
    for item in (path, *path.parents):
        if item.is_symlink():
            raise ValueError("Pi installation paths cannot contain symlinks")


def read_object(path: Path) -> dict:
    safe_path(path)
    if not path.exists():
        return {}
    if not path.is_file():
        raise ValueError("Pi configuration must be a regular file")
    if path.stat().st_size > 1024 * 1024:
        raise ValueError("Pi configuration exceeds 1 MiB")
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("Pi configuration must be a JSON object")
    return value


def save_configuration(updates: list[tuple[Path, dict]]) -> None:
    """Restore exact prior bytes if a configuration publication fails."""
    before = {path: path.read_bytes() if path.exists() else None for path, _ in updates}
    changed = []
    try:
        for path, value in updates:
            write_json(path, value)
            changed.append(path)
    except OSError:
        for path in reversed(changed):
            raw = before[path]
            if raw is None:
                path.unlink()
            else:
                fd, temporary = tempfile.mkstemp(prefix=".codex-mem-restore-", dir=path.parent)
                try:
                    with os.fdopen(fd, "wb") as stream:
                        stream.write(raw)
                        stream.flush()
                        os.fsync(stream.fileno())
                    os.replace(temporary, path)
                finally:
                    if os.path.exists(temporary):
                        os.unlink(temporary)
        raise


def integration(project: Path, action: str, data_dir: Path) -> dict:
    safe_path(project)
    if not project.is_dir():
        raise ValueError("Select an existing absolute project directory")
    directory = project / ".pi"
    target = directory / "codex-mem"
    settings_path = directory / "settings.json"
    mcp_path = directory / "mcp.json"
    receipt_path = directory / "codex-mem-install.json"
    for path in (directory, target, settings_path, mcp_path, receipt_path):
        safe_path(path)
    receipt = read_object(receipt_path)
    if action == "install":
        # The managed payload is replaceable. Memory must remain outside it,
        # including a previously selected path and aliases through symlinks.
        managed = target.resolve()
        for selected in (data_dir, receipt.get("data_dir")):
            if selected is None:
                continue
            if not isinstance(selected, (str, Path)) or not Path(selected).is_absolute():
                raise ValueError("Recorded memory directory must be absolute")
            if Path(selected).resolve().is_relative_to(managed):
                raise ValueError("Memory must stay outside the managed Pi package; relocate existing memory before updating")
    settings = read_object(settings_path)
    mcp = read_object(mcp_path)
    packages = settings.get("packages", [])
    servers = mcp.get("mcpServers", {})
    if not isinstance(packages, list) or not isinstance(servers, dict):
        raise ValueError("Invalid Pi packages or mcpServers configuration")
    owned = receipt.get("package") == str(target) and (target / MARKER).is_file()
    if target.exists() and not owned:
        raise ValueError("Refusing to replace an unmanaged Pi package")
    existing = servers.get("codex-mem")
    if existing is not None and (not owned or not isinstance(existing, dict)):
        raise ValueError("Existing codex-mem MCP entry is not managed here; preserve it and resolve the conflict")
    if existing is not None:
        expected = receipt.get("server", {})
        if any(existing.get(key) != expected.get(key) for key in ("command", "args", "env", "cwd")):
            raise ValueError("Managed MCP launch configuration changed; resolve it before installation or removal")
    def source(value):
        return value.get("source") if isinstance(value, dict) else value
    selected = [entry for entry in packages if source(entry) == str(target)]
    others = [entry for entry in packages if source(entry) != str(target)]
    if action in {"disable", "uninstall"}:
        if not owned:
            return {"status": "absent", "memory_preserved": True}
        settings["packages"] = others
        servers.pop("codex-mem", None)
        mcp["mcpServers"] = servers
        save_configuration([(settings_path, settings), (mcp_path, mcp)])
        # Retain the payload: another already-loaded Pi session can still use it.
        # No shared configuration, database or service lifecycle is changed.
        return {"status": "disabled", "memory_preserved": True, "package": str(target)}
    if receipt and (not isinstance(receipt.get("data_dir"), str)
                    or Path(receipt["data_dir"]).resolve() != data_dir.resolve()):
        raise ValueError("This project already uses another memory directory; reuse the recorded directory")
    directory.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".codex-mem-pi-", dir=directory) as temp:
        stage = Path(temp) / "package"
        stage.mkdir()
        for name in FILES:
            origin = ROOT / name
            safe_path(origin)
            if not origin.exists():
                raise ValueError("Release component missing: " + name)
            candidates = origin.rglob("*") if origin.is_dir() else [origin]
            for item in candidates:
                relative = item.relative_to(ROOT)
                if not is_distribution_path(relative):
                    continue
                if item.is_symlink():
                    raise ValueError("Release files cannot contain symlinks")
                if item.is_file():
                    destination = stage / relative
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(item, destination)
        (stage / MARKER).write_text("codex-mem Pi installation v1\n")
        write_json(stage / "pi/runtime.json", {"python": sys.executable, "data_dir": str(data_dir)})
        old = Path(temp) / "previous"
        server = copy.deepcopy(existing) if existing else json.loads((stage / "docs/pi-mcp.example.json").read_text())["mcpServers"]["codex-mem"]
        server.update(command=sys.executable, args=[str(target / "scripts/codex-mem.py"), "serve"],
                      cwd=str(target), env={"CODEX_MEM_HOME": str(data_dir)})
        settings["packages"] = others + (selected[:1] or [str(target)])
        servers["codex-mem"] = server
        mcp["mcpServers"] = servers
        if target.exists():
            target.rename(old)
        try:
            stage.rename(target)
            save_configuration([(settings_path, settings), (mcp_path, mcp),
                (receipt_path, {"package": str(target), "data_dir": str(data_dir), "server": server})])
        except OSError:
            if target.exists():
                shutil.rmtree(target)
            if old.exists():
                old.rename(target)
            raise
    return {"status": "installed", "package": str(target), "supported_pi": SUPPORTED_PI,
            "memory_preserved": True, "restart": "Reload Pi after reviewing project trust"}


def check_installation(project: Path, executable: str) -> dict:
    try:
        result = subprocess.run([executable, "--version"], capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        raise ValueError("Pi is unavailable; install @earendil-works/pi-coding-agent 1.4.0 separately") from None
    if result.returncode or result.stdout.strip() != SUPPORTED_PI:
        raise ValueError("Unsupported Pi version; Codex Mem requires Pi 1.4.0")
    target = project / ".pi/codex-mem"
    receipt = read_object(project / ".pi/codex-mem-install.json")
    if receipt.get("package") != str(target) or not (target / MARKER).is_file():
        raise ValueError("Pi integration is not installed in this project")
    runtime = read_object(target / "pi/runtime.json")
    mcp = read_object(project / ".pi/mcp.json")
    settings = read_object(project / ".pi/settings.json")
    servers = mcp.get("mcpServers")
    server = servers.get("codex-mem") if isinstance(servers, dict) else None
    packages = settings.get("packages")
    if (not isinstance(server, dict) or not isinstance(packages, list)
            or not any((item.get("source") if isinstance(item, dict) else item) == str(target) for item in packages)):
        raise ValueError("Pi integration is disabled; repeat installation to enable it")
    expected = receipt.get("server")
    python = runtime.get("python")
    memory = runtime.get("data_dir")
    launcher = target / "scripts/codex-mem.py"
    safe_path(launcher)
    if (not isinstance(expected, dict) or not isinstance(python, str) or not Path(python).is_absolute()
            or not isinstance(memory, str) or not Path(memory).is_absolute()
            or memory != receipt.get("data_dir") or server.get("command") != python
            or server.get("args") != [str(launcher), "serve"] or server.get("cwd") != str(target)
            or server.get("env") != {"CODEX_MEM_HOME": memory}
            or any(server.get(key) != expected.get(key) for key in ("command", "args", "env", "cwd"))):
        raise ValueError("Installed Python/MCP launch configuration changed; repeat installation to repair it")

    def inspect(command: str) -> dict:
        try:
            checked = subprocess.run([python, str(launcher), "--data-dir", memory, command],
                cwd=target, env={**os.environ, **server["env"]}, capture_output=True, text=True, timeout=10)
        except OSError:
            raise ValueError("Recorded Python executable is unavailable; repeat installation with Python 3.10 or later") from None
        if checked.returncode or len(checked.stdout) > 1024 * 1024:
            raise ValueError("Installed Python service check failed; repeat installation with Python 3.10 or later")
        value = json.loads(checked.stdout)
        if not isinstance(value, dict):
            raise ValueError("Installed Python service returned an invalid diagnostic")
        return value

    doctor = inspect("doctor")
    configuration = inspect("config")
    if doctor.get("ready") is not True:
        raise ValueError("Installed Python service is unavailable; run its doctor command")
    background = {}
    for processor, key in (("observation", "observation_profile"), ("project_memory", "project_memory_profile")):
        profile = configuration.get(key) or configuration.get("processing_profile") or {"runtime": "codex"}
        runtime_name = profile["runtime"]
        available = shutil.which(runtime_name) is not None
        background[processor] = {"runtime": runtime_name, "available": available,
            "code": "available" if available else runtime_name + "_runtime_unavailable"}
    return {"status": "compatible", "pi": SUPPORTED_PI, "python_ready": True, "background": background}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["install", "disable", "uninstall", "check"])
    parser.add_argument("--project", required=True, type=Path)
    parser.add_argument("--data-dir", type=Path, help="Shared memory directory; retain the saved selection on update")
    parser.add_argument("--pi", default="pi", help="Pi executable used only by check")
    args = parser.parse_args()
    try:
        if not args.project.is_absolute():
            raise ValueError("Project and memory directories must be absolute")
        if sys.version_info < (3, 10):
            raise ValueError("Python 3.10 or later is required")
        if args.action == "check":
            value = check_installation(args.project, args.pi)
        else:
            selected = args.data_dir
            if selected is None:
                receipt = read_object(args.project / ".pi/codex-mem-install.json")
                recorded = receipt.get("data_dir")
                if recorded is not None and not isinstance(recorded, str):
                    raise ValueError("Recorded memory directory must be absolute")
                selected = Path(recorded or os.environ.get("CODEX_MEM_HOME", "").strip()
                                or Path.home() / ".local/share/codex-mem")
            if not selected.is_absolute():
                raise ValueError("Project and memory directories must be absolute")
            value = integration(args.project, args.action, selected.resolve())
        print(json.dumps(value))
        return 0
    except (OSError, ValueError, shutil.Error, subprocess.SubprocessError):
        # Do not print native configuration or subprocess output: it can hold secrets.
        error = sys.exc_info()[1]
        detail = str(error) if isinstance(error, ValueError) and not isinstance(error, json.JSONDecodeError) else "Invalid or unavailable installation files"
        print(json.dumps({"status": "error", "detail": detail}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
