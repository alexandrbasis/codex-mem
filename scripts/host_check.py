#!/usr/bin/env python3
"""Read installed Codex hook discovery without running or trusting any hook."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import select
import subprocess
import time


def check(cwd: str, codex: str = "codex") -> dict:
    process = subprocess.Popen([codex, "app-server", "--stdio"], stdin=subprocess.PIPE,
                               stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    buffer = b""

    def send(message):
        process.stdin.write((json.dumps(message) + "\n").encode())
        process.stdin.flush()

    def response(request_id):
        nonlocal buffer
        deadline = time.monotonic() + 45
        while time.monotonic() < deadline:
            while b"\n" in buffer:
                line, buffer = buffer.split(b"\n", 1)
                message = json.loads(line)
                if message.get("id") == request_id:
                    if "error" in message:
                        raise RuntimeError("Codex rejected the read-only host check")
                    return message["result"]
            if not select.select([process.stdout], [], [], min(1, max(0, deadline - time.monotonic())))[0]:
                continue
            chunk = os.read(process.stdout.fileno(), 65536)
            if not chunk:
                raise RuntimeError("Codex app-server closed before the host check completed")
            buffer += chunk
            if len(buffer) > 4 * 1024 * 1024:
                raise RuntimeError("Codex host response exceeded the check's bound")
        raise RuntimeError("Codex host check timed out")

    try:
        send({"id": 1, "method": "initialize", "params": {"clientInfo": {"name": "codex-mem-host-check", "version": "1.0.0"}, "capabilities": {"experimentalApi": True}}})
        response(1)
        send({"method": "initialized", "params": {}})
        send({"id": 2, "method": "hooks/list", "params": {"cwds": [str(Path(cwd).resolve())]}})
        result = response(2)
        found = []
        errors = []
        for group in result.get("data", []):
            for hook in group.get("hooks", []):
                if "codex-mem" in str(hook.get("pluginId", "")) or "/codex-mem/" in str(hook.get("sourcePath", "")):
                    found.append({key: hook.get(key) for key in ("eventName", "pluginId", "sourcePath", "key", "currentHash", "enabled", "trustStatus", "command", "timeoutSec")})
            errors.extend(e for e in group.get("errors", []) if "codex-mem" in json.dumps(e))
        errors.extend(e for e in result.get("errors", []) if "codex-mem" in json.dumps(e))
        return {"hook_count": len(found), "hooks": found, "errors": errors,
                "all_trusted": bool(found) and all(h.get("trustStatus") in ("trusted", "managed") for h in found),
                "note": "Read-only discovery. No hook was executed or trusted by this command."}
    finally:
        if process.stdin and not process.stdin.closed:
            process.stdin.close()
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            process.terminate()
            process.wait(timeout=3)
        process.stdout.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cwd", default=str(Path.cwd()))
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    result = check(args.cwd)
    output = json.dumps(result, indent=2) + "\n"
    if args.output:
        args.output.write_text(output)
    print(output, end="")
