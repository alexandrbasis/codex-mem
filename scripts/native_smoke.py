#!/usr/bin/env python3
"""Verify installed Codex MCP discovery and one read-only call in a temporary store.

Uses the Codex 0.153.4 app-server protocol. It starts no model turn, changes no
hook trust or host configuration, and invokes only codex-mem's memory_status.
Other globally enabled MCP servers may start during native discovery.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import selectors
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from typing import Any


EXPECTED_TOOLS = {
    "memory_search", "memory_get", "memory_timeline", "memory_remember",
    "memory_consolidate", "memory_forget", "memory_status",
}
MAX_LINE = 16 * 1024 * 1024
MAX_OUTPUT = 64 * 1024 * 1024


class SmokeError(Exception):
    pass


def preflight(plugin_root: Path) -> str:
    """Fail before native startup if the inspected installation lacks isolation."""
    manifest = json.loads((plugin_root / ".codex-plugin/plugin.json").read_text())
    if manifest.get("name") != "codex-mem":
        raise SmokeError("--plugin-root is not a codex-mem installation")
    server = json.loads((plugin_root / ".mcp.json").read_text())["mcpServers"]["codex-mem"]
    if not {"CODEX_MEM_HOME", "CODEX_MEM_DISABLED"}.issubset(server.get("env_vars", [])):
        raise SmokeError("Installed MCP must forward CODEX_MEM_HOME and CODEX_MEM_DISABLED via env_vars")
    if {"CODEX_MEM_HOME", "CODEX_MEM_DISABLED"}.intersection(server.get("env", {})):
        raise SmokeError("Installed MCP overrides the temporary isolation environment")
    if (server.get("command") != "python3" or server.get("cwd") != "."
            or server.get("args") != ["scripts/codex-mem.py", "serve"]):
        raise SmokeError("Installed MCP launcher differs from the supported isolation contract")
    return str(manifest["version"])


class AppServer:
    """Bounded JSON-lines client; drain both pipes and terminate our process group."""

    def __init__(self, codex: str, cwd: Path, env: dict[str, str], timeout: float):
        self.deadline = time.monotonic() + timeout
        self.process = subprocess.Popen(
            [codex, "app-server"], cwd=cwd, env=env, stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True,
        )
        self.selector = selectors.DefaultSelector()
        for name in ("stdout", "stderr"):
            stream = getattr(self.process, name)
            os.set_blocking(stream.fileno(), False)
            self.selector.register(stream, selectors.EVENT_READ, name)
        self.pending = bytearray()
        self.output_bytes = 0
        self.stderr_bytes = 0
        self.stderr_tail = bytearray()
        self.next_id = 1

    def send(self, value: dict[str, Any]) -> None:
        try:
            self.process.stdin.write(json.dumps(value).encode() + b"\n")
            self.process.stdin.flush()
        except (BrokenPipeError, OSError) as exc:
            raise SmokeError("Codex app-server closed its input") from exc

    def message(self) -> dict[str, Any]:
        while True:
            remaining = self.deadline - time.monotonic()
            if remaining <= 0:
                raise SmokeError("Native smoke exceeded its total timeout")
            newline = self.pending.find(b"\n")
            if newline >= 0:
                if newline > MAX_LINE:
                    raise SmokeError("App-server JSON line exceeded buffer limit")
                raw = bytes(self.pending[:newline])
                del self.pending[:newline + 1]
                if not raw.strip():
                    continue
                try:
                    value = json.loads(raw)
                except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                    raise SmokeError("App-server returned invalid JSON") from exc
                if not isinstance(value, dict):
                    raise SmokeError("App-server returned a non-object message")
                return value
            if len(self.pending) > MAX_LINE:
                raise SmokeError("App-server JSON line exceeded buffer limit")
            if not self.selector.get_map():
                raise SmokeError(f"App-server exited before responding (exit {self.process.poll()})")
            for key, _ in self.selector.select(min(remaining, 1.0)):
                chunk = os.read(key.fd, 65536)
                if not chunk:
                    self.selector.unregister(key.fileobj)
                    continue
                self.output_bytes += len(chunk)
                if self.output_bytes > MAX_OUTPUT:
                    raise SmokeError("App-server output exceeded total buffer limit")
                if key.data == "stdout":
                    self.pending.extend(chunk)
                else:
                    self.stderr_bytes += len(chunk)
                    self.stderr_tail.extend(chunk)
                    del self.stderr_tail[:-8192]

    def request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        print(f"Native smoke: {method}", file=sys.stderr, flush=True)
        request_id = self.next_id
        self.next_id += 1
        self.send({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params})
        while True:
            message = self.message()
            if "method" in message:
                if "id" in message:
                    self.send({"jsonrpc": "2.0", "id": message["id"], "error": {
                        "code": -32601, "message": "Smoke client does not authorize server requests",
                    }})
                    raise SmokeError("App-server requested client action; smoke authorized none")
                continue
            if message.get("id") != request_id:
                raise SmokeError("App-server returned an unexpected response ID")
            if "error" in message:
                error = message["error"]
                raise SmokeError(f"{method} failed: {str(error.get('message', error))[:500]}")
            result = message.get("result")
            if not isinstance(result, dict):
                raise SmokeError(f"{method} returned a non-object result")
            return result

    def close(self) -> None:
        # Native MCP launchers create separate process groups. Capture only
        # descendants of this invocation before closing the app-server.
        groups: dict[int, set[int]] = {self.process.pid: {self.process.pid}}
        try:
            snapshot = subprocess.run(
                ["ps", "-axo", "pid=,ppid=,pgid="], capture_output=True,
                text=True, timeout=2, check=True,
            )
            rows = [tuple(map(int, line.split())) for line in snapshot.stdout.splitlines()]
            descendants = {self.process.pid}
            while True:
                found = {pid for pid, parent, _ in rows if parent in descendants} - descendants
                if not found:
                    break
                descendants.update(found)
            for pid, _, group in rows:
                if pid in descendants:
                    groups.setdefault(group, set()).add(pid)
        except (OSError, ValueError, subprocess.SubprocessError):
            pass

        def signal_owned_groups(sig: int) -> None:
            for group, members in groups.items():
                for pid in members:
                    try:
                        # PID reuse or a detached external service cannot make
                        # an unrelated session a cleanup target.
                        if os.getsid(pid) == self.process.pid and os.getpgid(pid) == group:
                            os.killpg(group, sig)
                            break
                    except ProcessLookupError:
                        continue

        try:
            self.process.stdin.close()
        except (BrokenPipeError, OSError):
            pass
        try:
            # EOF lets Codex drop MCP clients and clean up gracefully first.
            self.process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            pass
        finally:
            signal_owned_groups(signal.SIGTERM)
            try:
                self.process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                pass
            signal_owned_groups(signal.SIGKILL)
            self.process.wait(timeout=3)
            self.selector.close()
            for stream in (self.process.stdout, self.process.stderr):
                stream.close()


def status_payload(result: dict[str, Any]) -> dict[str, Any]:
    if result.get("isError"):
        raise SmokeError("Native memory_status returned a tool error")
    payload = result.get("structuredContent")
    if isinstance(payload, dict) and set(payload) == {"result"}:
        payload = payload["result"]
    if not isinstance(payload, dict):
        texts = [part.get("text") for part in result.get("content", [])
                 if isinstance(part, dict) and part.get("type") == "text"]
        if len(texts) != 1:
            raise SmokeError("memory_status did not return one structured status")
        payload = json.loads(texts[0])
    if not isinstance(payload, dict):
        raise SmokeError("memory_status returned a non-object status")
    return payload


def smoke(args: argparse.Namespace) -> dict[str, Any]:
    version = preflight(args.plugin_root.expanduser().resolve())
    codex = shutil.which(args.codex)
    if codex is None:
        raise SmokeError("Codex CLI was not found")
    with tempfile.TemporaryDirectory(prefix="codex-mem-native-smoke-") as temporary:
        root = Path(temporary).resolve()
        project, data_dir = root / "project", root / "memory"
        project.mkdir(mode=0o700)
        data_dir.mkdir(mode=0o700)
        env = dict(os.environ, CODEX_MEM_HOME=str(data_dir), CODEX_MEM_DISABLED="1")
        client = AppServer(codex, project, env, args.timeout)
        try:
            client.request("initialize", {
                "clientInfo": {"name": "codex-mem-native-smoke", "version": "1"},
                "capabilities": {"experimentalApi": True},
            })
            client.send({"jsonrpc": "2.0", "method": "initialized"})
            started = client.request("thread/start", {"cwd": str(project), "ephemeral": True})
            thread_id = started["thread"]["id"]
            servers: list[dict[str, Any]] = []
            cursor = None
            for _ in range(100):
                params = {"threadId": thread_id, "detail": "toolsAndAuthOnly"}
                if cursor is not None:
                    params["cursor"] = cursor
                listing = client.request("mcpServerStatus/list", params)
                servers.extend(listing["data"])
                cursor = listing.get("nextCursor")
                if cursor is None:
                    break
            else:
                raise SmokeError("MCP inventory pagination exceeded its limit")
            matches = [server for server in servers if server.get("pluginId") == args.plugin_id
                       and {tool.get("name") for tool in server.get("tools", {}).values()} == EXPECTED_TOOLS]
            if len(matches) != 1:
                raise SmokeError("Expected exactly one installed codex-mem MCP with all expected tools")
            server = matches[0]
            status = status_payload(client.request("mcpServer/tool/call", {
                "server": server["name"], "threadId": thread_id,
                "tool": "memory_status", "arguments": {"project": str(project)},
            }))
            if (Path(status.get("data_dir", "")).resolve() != data_dir
                    or Path(status.get("db_path", "")).resolve() != data_dir / "memory.sqlite3"):
                raise SmokeError("ISOLATION FAILURE: memory_status returned a non-temporary store")
            if status.get("entries") != 0 or status.get("project") != str(project):
                raise SmokeError("Temporary project status was not empty or correctly scoped")
            if not (data_dir / "memory.sqlite3").is_file():
                raise SmokeError("Temporary database was not created by the native MCP process")
            result = {"ready": True, "plugin_id": args.plugin_id, "version": version,
                      "server": server["name"], "tools": sorted(EXPECTED_TOOLS),
                      "live_call": "memory_status", "temporary_store_verified": True,
                      "entries": status["entries"], "model_turn_started": False}
        except SmokeError as exc:
            diagnostic = bytes(client.stderr_tail).decode("utf-8", errors="replace").strip()
            if diagnostic:
                raise SmokeError(f"{exc}\nApp-server stderr (last 8192 bytes):\n{diagnostic}") from exc
            raise
        finally:
            client.close()
    return {**result, "temporary_store_removed": not root.exists()}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plugin-root", required=True, type=Path,
                        help="Installed cache directory to verify before starting Codex")
    parser.add_argument("--plugin-id", default="codex-mem@personal")
    parser.add_argument("--codex", default="codex")
    parser.add_argument("--timeout", type=float, default=120,
                        help="Total native protocol timeout in seconds (default: 120)")
    args = parser.parse_args()
    if not 1 <= args.timeout <= 600:
        parser.error("--timeout must be between 1 and 600 seconds")
    if os.name != "posix":
        parser.error("Native smoke currently requires macOS or Linux")
    try:
        print(json.dumps(smoke(args), indent=2))
        return 0
    except (SmokeError, OSError, ValueError, KeyError, TypeError) as exc:
        print(json.dumps({"ready": False, "error": str(exc)}), file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("Native smoke interrupted; child processes cleaned up", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
