#!/usr/bin/env python3
"""Read model settings and completed tool outcomes for explicitly supplied test threads."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import tempfile

from native_smoke import AppServer


def report(ids: list[str]) -> dict:
    results = []
    with tempfile.TemporaryDirectory(prefix="codex-mem-thread-report-") as temporary:
        client = AppServer("codex", Path(temporary), dict(os.environ, CODEX_MEM_DISABLED="1"), 90)
        try:
            client.request("initialize", {"clientInfo": {"name": "codex-mem-thread-report", "version": "1"},
                                          "capabilities": {"experimentalApi": True}})
            client.send({"method": "initialized"})
            for thread_id in ids:
                result = client.request("thread/read", {"threadId": thread_id, "includeTurns": True})["thread"]
                turns = []
                for turn in result.get("turns", []):
                    calls = []
                    answers = []
                    for item in turn.get("items", []):
                        if item.get("type") == "mcpToolCall":
                            calls.append({key: item.get(key) for key in ("id", "server", "tool", "status", "arguments", "result", "error")})
                        elif item.get("type") == "agentMessage" and item.get("phase") == "final_answer":
                            answers.append(item.get("text"))
                    turns.append({"id": turn["id"], "status": turn.get("status"), "error": turn.get("error"),
                                  "mcp_calls": calls, "final_answers": answers})
                results.append({"id": thread_id, "model": result.get("model"),
                                "reasoning_effort": result.get("reasoningEffort"),
                                "cwd": result.get("cwd"), "turns": turns})
        finally:
            client.close()
    return {"evidence_scope": "Native persisted thread configuration and actual tool outcomes; configuration alone is not backend telemetry.",
            "threads": results}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("thread_ids", nargs="+")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    data = report(args.thread_ids)
    if args.output:
        args.output.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n")
    print(json.dumps({"threads": [{"id": t["id"], "model": t["model"],
                                   "reasoning_effort": t["reasoning_effort"], "turns": len(t["turns"])}
                                  for t in data["threads"]]}, indent=2))
