"""Installed Pi's public RPC -> native MCP -> real Store boundary.

Opt in with CODEX_MEM_PI=/absolute/path/to/pi. Only the external model endpoint
is replaced; Pi, MCP, the store, tool validation, and privacy remain real.
"""
from __future__ import annotations

from contextlib import closing
import http.server
import json
import os
from pathlib import Path
import queue
import subprocess
import sys
import tempfile
import threading
import time
import sqlite3
import unittest

from codex_mem.store import Store
from codex_mem.tool_io import normalize_capture

ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = ROOT / "docs" / "pi-mcp.example.json"
PI = os.environ.get("CODEX_MEM_PI")


class PiMCPConfigurationTests(unittest.TestCase):
    def test_default_connection_exposes_reads_and_requires_write_enablement(self):
        config = json.loads(TEMPLATE.read_text())
        self.assertEqual(["codex-mem"], list(config["mcpServers"]))
        server = config["mcpServers"]["codex-mem"]
        self.assertEqual("direct", server["exposure"])
        self.assertEqual({
            "memory_remember": "hidden", "memory_consolidate": "hidden",
            "memory_forget": "hidden", "memory_usage_refresh": "hidden",
        }, server["toolExposure"])
        self.assertEqual(["-m", "codex_mem.mcp"], server["args"])


@unittest.skipUnless(PI, "set CODEX_MEM_PI to test an installed Pi (no model credentials needed)")
class InstalledPiMCPTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.project = self.root / "project"
        self.project.mkdir()
        self.agent_dir = self.root / "pi-agent"
        self.agent_dir.mkdir()
        self.data_dir = self.root / "memory"
        self.requests = []
        self.http_errors = []
        owner = self

        class Provider(http.server.BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def do_POST(self):
                try:
                    request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                    owner.requests.append(request)
                    messages = request["messages"]
                    if messages[-1]["role"] == "user":
                        content = messages[-1]["content"]
                        if isinstance(content, list):
                            content = "".join(block.get("text", "") for block in content)
                        call = json.loads(content)
                        delta = {"role": "assistant", "tool_calls": [{
                            "index": 0, "id": "fixture_call", "type": "function",
                            "function": {"name": "mcp__codex_mem__" + call["name"],
                                         "arguments": json.dumps(call["arguments"])},
                        }]}
                        finish = "tool_calls"
                    else:
                        delta = {"role": "assistant", "content": "Fixture complete."}
                        finish = "stop"
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream")
                    self.end_headers()
                    for change, reason in ((delta, None), ({}, finish)):
                        chunk = {"id": "fixture", "object": "chat.completion.chunk", "model": "fixture",
                                 "choices": [{"index": 0, "delta": change, "finish_reason": reason}]}
                        self.wfile.write(("data: " + json.dumps(chunk) + "\n\n").encode())
                    self.wfile.write(b"data: [DONE]\n\n")
                    self.wfile.flush()
                except Exception as error:
                    owner.http_errors.append(type(error).__name__)
                    raise

        self.http = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Provider)
        self.thread = threading.Thread(target=self.http.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.stop_http)
        (self.agent_dir / "models.json").write_text(json.dumps({"providers": {"fixture": {
            "baseUrl": f"http://127.0.0.1:{self.http.server_port}/v1",
            "api": "openai-completions", "apiKey": "fixture-not-a-credential",
            "models": [{"id": "fixture", "contextWindow": 128000, "maxTokens": 1024}],
        }}}))
        (self.agent_dir / "settings.json").write_text(json.dumps({
            "compaction": {"enabled": False}, "retry": {"enabled": False},
            "extensions": ["-builtin:codemode", "-builtin:tool_search"],
        }))
        # Disable optional processing. No service install, wake, or model is needed.
        self.data_dir.mkdir()
        (self.data_dir / "config.json").write_text(json.dumps({
            "service_enabled": False, "processor_enabled": False,
            "semantic_enabled": False, "capture_scope": "manual",
        }))
        self.records = queue.Queue()
        self.process = None

    def stop_http(self):
        self.http.shutdown()
        self.http.server_close()
        self.thread.join(timeout=3)

    def start_pi(self, *, writes=False, broken=False):
        config = json.loads(TEMPLATE.read_text())
        server = config["mcpServers"]["codex-mem"]
        server.update(command=sys.executable, cwd=str(ROOT),
                      env={"CODEX_MEM_HOME": str(self.data_dir)})
        if writes:
            server["toolExposure"] = {name: "direct" for name in server["toolExposure"]}
        if broken:
            server["command"] = str(self.root / "missing-executable")
        (self.agent_dir / "mcp.json").write_text(json.dumps(config))
        environment = {"PATH": os.environ.get("PATH", ""), "HOME": str(self.root),
                       "PI_CODING_AGENT_DIR": str(self.agent_dir), "NO_COLOR": "1"}
        self.stderr = tempfile.TemporaryFile()
        self.addCleanup(self.stderr.close)
        self.process = subprocess.Popen([
            PI, "--mode", "rpc", "--no-session", "--no-approve", "--provider", "fixture",
            "--model", "fixture", "--thinking", "off", "--no-builtin-tools",
            "--no-skills", "--no-prompt-templates", "--no-context-files", "--no-themes",
        ], cwd=self.project, env=environment, stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=self.stderr)
        self.addCleanup(self.stop_pi)
        process = self.process

        def read_records():
            for raw in process.stdout:
                self.records.put(json.loads(raw))
            self.records.put({"type": "fixture_exit"})

        self.reader = threading.Thread(target=read_records, daemon=True)
        self.reader.start()

    def stop_pi(self):
        if self.process is None:
            return
        self.process.stdin.close()
        try:
            self.process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.process.terminate()
            self.process.wait(timeout=5)
        self.reader.join(timeout=3)
        self.process.stdout.close()
        self.process = None

    def call(self, name, **arguments):
        self.process.stdin.write((json.dumps({"id": "probe", "type": "prompt", "message": json.dumps({
            "name": name, "arguments": arguments,
        })}) + "\n").encode())
        self.process.stdin.flush()
        events = []
        deadline = time.monotonic() + 25
        while True:
            remaining = deadline - time.monotonic()
            self.assertGreater(remaining, 0, "Pi did not settle within 25 seconds")
            event = self.records.get(timeout=remaining)
            self.assertNotEqual("fixture_exit", event["type"], "Pi exited before completion")
            events.append(event)
            if event["type"] == "agent_settled":
                self.assertFalse(event["aborted"])
                break
        self.assertFalse(self.http_errors)
        completed = [event["message"] for event in events if event["type"] == "message_end"
                     and event["message"]["role"] == "assistant"]
        self.assertEqual("stop", completed[-1]["stopReason"])
        self.assertEqual("Fixture complete.", completed[-1]["content"][0]["text"])
        results = [event["message"] for event in events if event["type"] == "message_end"
                   and event["message"]["role"] == "toolResult"]
        self.assertEqual(1, len(results), events)
        return results[0]

    @staticmethod
    def payload(result):
        return json.loads(result["content"][0]["text"])

    def test_default_denies_mutation_even_when_model_attempts_it(self):
        self.start_pi()
        for name in ("memory_remember", "memory_consolidate", "memory_forget", "memory_usage_refresh"):
            denied = self.call(name, project=str(self.project), title="Forbidden", body="Must not persist")
            self.assertTrue(denied["isError"], name)
            self.assertIn("not found", denied["content"][0]["text"].lower())
        found = self.call("memory_search", project=str(self.project), query="Forbidden", mode="lexical")
        self.assertEqual([], self.payload(found))
        names = [tool["function"]["name"] for tool in self.requests[0]["tools"]]
        self.assertEqual(len(names), len(set(names)))
        self.assertNotIn("mcp__codex_mem__memory_remember", names)
        self.assertIn("mcp__codex_mem__memory_search", names)

    def test_enabled_writes_search_full_read_management_privacy_and_project_partition(self):
        with Store(self.data_dir) as store:
            capture = normalize_capture({
                "tool_name": "shell", "tool_use_id": "pi-tool:1", "session_id": "pi-fixture",
                "tool_input": {"command": "echo zircon"},
                "tool_response": {"output": "zircon", "API_KEY": "fixture-secret"},
            }, project=str(self.project))
            source = store.remember(self.project, "Tool source", "A tool returned reference evidence.",
                                    source="hook:PostToolUse", tool_capture=capture)
            wrapped_capture = normalize_capture({
                "tool_name": "shell", "tool_use_id": "pi-wrapped:1", "session_id": "pi-fixture",
                "tool_input": {"text": "x" * 90_000}, "tool_response": {"text": "x" * 90_000},
            }, project=str(self.project))
            wrapped_source = store.remember(self.project, "Wrapped source", "Retained truncated evidence.",
                                             source="hook:PostToolUse", tool_capture=wrapped_capture)
            database = store.db_path
        # Existing rows from an older release bypass the new capture sanitizer.
        legacy = json.dumps({"output": "zircon", "API_KEY": "legacy-fixture-secret",
                             "nested": [{"refresh_token": ["legacy-array-secret"], "count": 3}]})
        with closing(sqlite3.connect(database, isolation_level=None)) as connection:
            connection.execute("UPDATE tool_uses SET tool_response=?, response_excerpt=? WHERE tool_use_id=?",
                               (legacy, "legacy-fixture-secret", "pi-tool:1"))
        wrapper = json.loads(wrapped_capture.tool_input)
        wrapper["head"] = '{"API_KEY":{"client_id":"public","value":"wrapped-secret"},"text":"' + wrapper["head"][100:]
        wrapper["tail"] = wrapper["tail"][:-100] + '","refresh_token":["tail-secret"]}'
        wrapped_legacy = json.dumps(wrapper, separators=(",", ":"))
        with closing(sqlite3.connect(database, isolation_level=None)) as connection:
            connection.execute("UPDATE tool_uses SET tool_input=?, tool_response=?, input_excerpt=?, response_excerpt=? WHERE tool_use_id=?",
                               (wrapped_legacy, wrapped_legacy, "wrapped-secret", "tail-secret", "pi-wrapped:1"))
        self.start_pi(writes=True)
        wrapped_read = self.payload(self.call("memory_get_tool_uses", project=str(self.project), ids=["pi-wrapped:1"]))[0]
        self.assertEqual(wrapped_source["id"], wrapped_read["entry_id"])
        for payload, excerpt, metadata in (("tool_input", "input_excerpt", "input_metadata"),
                                           ("tool_response", "response_excerpt", "response_metadata")):
            self.assertIsNone(wrapped_read[payload])
            self.assertIsNone(wrapped_read[excerpt])
            self.assertEqual("incomplete_json_fragments", wrapped_read[metadata]["read_omitted_reason"])
        self.assertNotIn("wrapped-secret", json.dumps(wrapped_read))
        self.assertNotIn("tail-secret", json.dumps(wrapped_read))
        with closing(sqlite3.connect(database, isolation_level=None)) as connection:
            retained = connection.execute("SELECT tool_input FROM tool_uses WHERE tool_use_id=?", ("pi-wrapped:1",)).fetchone()[0]
        self.assertEqual(wrapped_legacy, retained)
        raw = self.payload(self.call("memory_get_tool_uses", project=str(self.project), ids=["pi-tool:1"]))
        self.assertEqual("pi-tool:1", raw[0]["tool_use_id"])
        self.assertIn("echo zircon", raw[0]["tool_input"])
        self.assertNotIn("fixture-secret", json.dumps(raw[0]))
        self.assertNotIn("legacy-array-secret", json.dumps(raw[0]))
        self.assertEqual(3, json.loads(raw[0]["tool_response"])["nested"][0]["count"])
        self.assertEqual(source["id"], raw[0]["entry_id"])
        self.assertTrue(raw[0]["response_metadata"]["read_redacted"])
        with closing(sqlite3.connect(database, isolation_level=None)) as connection:
            persisted = connection.execute("SELECT tool_response FROM tool_uses WHERE tool_use_id=?", ("pi-tool:1",)).fetchone()[0]
        self.assertEqual(legacy, persisted)
        self.assertEqual([], self.payload(self.call("memory_get_tool_uses", project=str(self.root / "other"), ids=["pi-tool:1"])))
        saved = self.payload(self.call("memory_remember", project=str(self.project), title="Zircon decision",
                                      body="Use zircon. <private>hidden fixture</private> API_KEY=fixture-secret",
                                      source="pi:explicit", session_id="pi-fixture"))
        entry_id = saved["id"]
        found = self.payload(self.call("memory_search", project=str(self.project), query="zircon", mode="lexical"))
        self.assertEqual([entry_id], [entry["id"] for entry in found])
        full = self.payload(self.call("memory_get", project=str(self.project), ids=[entry_id]))[0]
        self.assertEqual("pi:explicit", full["source"])
        self.assertNotIn("hidden fixture", full["body"])
        self.assertNotIn("fixture-secret", full["body"])
        self.assertIn("[REDACTED]", full["body"])
        other = self.payload(self.call("memory_get", project=str(self.root / "other"), ids=[entry_id]))
        self.assertEqual([], other)
        invalid = self.call("memory_remember", project="relative-project", title="Invalid", body="Must not persist")
        self.assertTrue(invalid["isError"])
        combined = self.payload(self.call("memory_consolidate", project=str(self.project), title="Zircon summary",
                                         body="Use zircon summary.", source_ids=[entry_id]))
        self.assertEqual([entry_id], combined["source_ids"])
        timeline = self.payload(self.call("memory_timeline", project=str(self.project)))
        self.assertIn(combined["id"], [entry["id"] for entry in timeline])
        status = self.payload(self.call("memory_status", project=str(self.project)))
        self.assertIsInstance(status, dict)
        deleted = self.payload(self.call("memory_forget", project=str(self.project), ids=[entry_id, combined["id"]]))
        self.assertEqual({entry_id, combined["id"]}, set(deleted["ids"]))
        self.assertEqual([], self.payload(self.call("memory_get", project=str(self.project), ids=[entry_id, combined["id"]])))
        # A real store failure remains content-free at the MCP boundary.
        with Store(self.data_dir) as store:
            database = store.db_path
        # Replacing the DB after connection would only test an old open inode.
        with closing(sqlite3.connect(database, isolation_level=None)) as connection:
            connection.execute("DROP TABLE entries")
        failed = self.call("memory_get", project=str(self.project), ids=[entry_id])
        self.assertTrue(failed["isError"])
        self.assertEqual("Memory operation failed", failed["content"][0]["text"])
        names = [tool["function"]["name"] for tool in self.requests[0]["tools"]]
        self.assertEqual(11, len(names))
        self.assertEqual(len(names), len(set(names)))

    def test_unavailable_mcp_is_a_safe_tool_failure_and_pi_continues(self):
        self.start_pi(broken=True)
        result = self.call("memory_search", project=str(self.project), query="zircon")
        self.assertTrue(result["isError"])
        self.assertNotIn("fixture-secret", json.dumps(result))
        self.assertEqual("tool", self.requests[-1]["messages"][-1]["role"])
