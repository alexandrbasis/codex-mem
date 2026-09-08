"""Black-box coverage for the codex-mem MCP stdio transport."""

from __future__ import annotations

import json
from pathlib import Path
import select
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]


class MCPSubprocessTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        temporary_path = Path(self.temporary.name)
        self.data_dir = temporary_path / "memory-home"
        self.project = temporary_path / "project"
        self.project.mkdir()
        self.process = subprocess.Popen(
            [sys.executable, "-m", "codex_mem.mcp", "--data-dir", str(self.data_dir)],
            cwd=ROOT,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

    def tearDown(self) -> None:
        if self.process.stdin is not None:
            self.process.stdin.close()
        try:
            self.process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            self.process.terminate()
            self.process.wait(timeout=3)
        if self.process.stdout is not None:
            self.process.stdout.close()
        if self.process.stderr is not None:
            self.process.stderr.close()
        self.temporary.cleanup()

    def _send(self, value: dict[str, object]) -> None:
        assert self.process.stdin is not None
        self.process.stdin.write(json.dumps(value).encode("utf-8") + b"\n")
        self.process.stdin.flush()

    def _response(self) -> dict[str, object]:
        assert self.process.stdout is not None
        readable, _, _ = select.select([self.process.stdout], [], [], 3)
        self.assertTrue(readable, "MCP server did not produce a response within three seconds")
        raw = self.process.stdout.readline()
        self.assertTrue(raw)
        return json.loads(raw.decode("utf-8"))

    def _request(self, value: dict[str, object]) -> dict[str, object]:
        self._send(value)
        return self._response()

    def _initialize(self, *, ready: bool = True) -> None:
        response = self._request(
            {
                "jsonrpc": "2.0",
                "id": 0,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-11-25",
                    "capabilities": {},
                    "clientInfo": {"name": "unittest", "version": "1"},
                },
            }
        )
        self.assertEqual(0, response["id"])
        result = response["result"]
        self.assertEqual("2025-11-25", result["protocolVersion"])
        self.assertIn("untrusted", result["instructions"])
        if ready:
            self._send({"jsonrpc": "2.0", "method": "notifications/initialized"})

    @staticmethod
    def _content(response: dict[str, object]) -> object:
        result = response["result"]
        if "structuredContent" in result:
            structured = result["structuredContent"]
            if isinstance(structured, dict) and set(structured) == {"result"}:
                return structured["result"]
            return structured
        return json.loads(result["content"][0]["text"])

    def _call(self, request_id: int, name: str, arguments: dict[str, object]) -> dict[str, object]:
        return self._request(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "method": "tools/call",
                "params": {"name": name, "arguments": arguments},
            }
        )

    def test_handshake_and_memory_round_trip(self) -> None:
        self._initialize()
        listing = self._request({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        names = {tool["name"] for tool in listing["result"]["tools"]}
        self.assertTrue(
            {
                "memory_search",
                "memory_get",
                "memory_timeline",
                "memory_remember",
                "memory_consolidate",
                "memory_forget",
                "memory_status",
            }.issubset(names)
        )

        remembered = self._call(
            2,
            "memory_remember",
            {
                "project": str(self.project),
                "title": "MCP smoke note",
                "body": "This record proves the local stdio round trip.",
                "kind": "note",
                "tags": ["smoke"],
            },
        )
        entry = self._content(remembered)
        entry_id = entry["id"]

        searched = self._call(
            3,
            "memory_search",
            {"project": str(self.project), "query": "stdio", "limit": 10},
        )
        previews = self._content(searched)
        self.assertIn(entry_id, [item["id"] for item in previews])

        fetched = self._call(4, "memory_get", {"project": str(self.project), "ids": [entry_id]})
        records = self._content(fetched)
        self.assertEqual("This record proves the local stdio round trip.", records[0]["body"])

        forgotten = self._call(5, "memory_forget", {"project": str(self.project), "ids": [entry_id]})
        self.assertIn(entry_id, self._content(forgotten)["ids"])

    def test_payload_cap_bad_arguments_and_notifications(self) -> None:
        assert self.process.stdin is not None
        self.process.stdin.write(b"x" * (1024 * 1024 + 1) + b"\n")
        self.process.stdin.flush()
        oversized = self._response()
        self.assertEqual(-32700, oversized["error"]["code"])
        self.assertEqual("Parse error", oversized["error"]["message"])

        self._initialize()
        bad_arguments = self._call(
            1,
            "memory_search",
            {"project": str(self.project), "query": "", "unknown": True},
        )
        self.assertTrue(bad_arguments["result"]["isError"])

        # A notification receives no wire response. The next response must be
        # the request with ID 0, which also catches falsey-ID handling bugs.
        self._send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        pong = self._request({"jsonrpc": "2.0", "id": 0, "method": "ping"})
        self.assertEqual(0, pong["id"])
        self.assertEqual({}, pong["result"])

    def test_lifecycle_and_protocol_envelope_validation(self) -> None:
        incomplete = self._request(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {"protocolVersion": "2025-11-25"},
            }
        )
        self.assertEqual(-32602, incomplete["error"]["code"])

        self._initialize(ready=False)
        premature = self._request({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        self.assertEqual(-32002, premature["error"]["code"])
        self._send({"jsonrpc": "2.0", "method": "notifications/initialized"})

        malformed_envelope = self._request(
            {
                "jsonrpc": "2.0",
                "id": 0,
                "method": "tools/call",
                "params": {"name": "memory_search", "arguments": []},
            }
        )
        self.assertEqual(0, malformed_envelope["id"])
        self.assertEqual(-32602, malformed_envelope["error"]["code"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
