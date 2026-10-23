"""Black-box coverage for the codex-mem MCP stdio transport."""

from __future__ import annotations

from contextlib import closing
import json
from pathlib import Path
import select
import sqlite3
import subprocess
import sys
import tempfile
import unittest

from codex_mem.mcp import MAX_LINE_BYTES, MemoryMCPServer
from codex_mem.store import MAX_OBSERVATION_CHARS, Store
from codex_mem.tool_io import normalize_capture


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
                "memory_get_tool_uses",
                "memory_get",
                "memory_timeline",
                "memory_remember",
                "memory_consolidate",
                "memory_forget",
                "memory_status",
            }.issubset(names)
        )
        search_schema = next(
            tool["inputSchema"] for tool in listing["result"]["tools"] if tool["name"] == "memory_search"
        )
        self.assertTrue({"kinds", "types", "concepts", "files", "intent"}.issubset(search_schema["properties"]))
        raw_tool = next(
            tool for tool in listing["result"]["tools"] if tool["name"] == "memory_get_tool_uses"
        )
        self.assertEqual(["project"], raw_tool["inputSchema"]["required"])
        self.assertEqual(256, raw_tool["inputSchema"]["properties"]["ids"]["items"]["maxLength"])
        self.assertNotIn("pattern", raw_tool["inputSchema"]["properties"]["ids"]["items"])
        self.assertIn("never executes", raw_tool["description"])

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

    def test_raw_structured_secrets_are_redacted_before_public_source_readback(self) -> None:
        self._initialize()
        payload = {"API_KEY": "synthetic-secret", "nested": [
            {"client_secret": {"credential": "nested-secret"}, "result": 7, "ok": True},
            {"refresh_token": ["array-secret"], "output": "public result"},
        ], "count": 2}
        with Store(self.data_dir) as store:
            capture = normalize_capture({
                "tool_name": "shell", "tool_use_id": "structured-credentials",
                "tool_input": payload, "tool_response": payload,
            }, project=str(self.project))
            store.remember(self.project, "Source", "Structured evidence.", tool_capture=capture)
        result = self._content(self._call(1, "memory_get_tool_uses", {
            "project": str(self.project), "ids": ["structured-credentials"],
        }))[0]
        for field in ("tool_input", "tool_response"):
            clean = json.loads(result[field])
            self.assertEqual("[REDACTED]", clean["API_KEY"])
            self.assertEqual("[REDACTED]", clean["nested"][0]["client_secret"])
            self.assertEqual("[REDACTED]", clean["nested"][1]["refresh_token"])
            self.assertEqual({"result": 7, "ok": True, "client_secret": "[REDACTED]"}, clean["nested"][0])
            self.assertEqual("public result", clean["nested"][1]["output"])
            self.assertEqual(2, clean["count"])
            self.assertNotIn("synthetic-secret", result[field])
            self.assertNotIn("nested-secret", result[field])
            self.assertNotIn("array-secret", result[field])
        # Simulate persisted captures produced before structured-field redaction.
        legacy = json.dumps(payload)
        database = self.data_dir / "memory.sqlite3"
        with closing(sqlite3.connect(database, isolation_level=None)) as connection:
            connection.execute("UPDATE tool_uses SET tool_input=?, input_excerpt=? WHERE tool_use_id=?",
                               (legacy, "synthetic-secret", "structured-credentials"))
        reread = self._content(self._call(2, "memory_get_tool_uses", {
            "project": str(self.project), "ids": ["structured-credentials"],
        }))[0]
        self.assertEqual(result["entry_id"], reread["entry_id"])
        self.assertEqual(result["content_hash"], reread["content_hash"])
        self.assertTrue(reread["input_metadata"]["read_redacted"])
        self.assertNotIn("synthetic-secret", json.dumps(reread))
        self.assertNotIn("nested-secret", json.dumps(reread))
        with closing(sqlite3.connect(database, isolation_level=None)) as connection:
            retained = connection.execute("SELECT tool_input FROM tool_uses WHERE tool_use_id=?",
                                          ("structured-credentials",)).fetchone()[0]
        self.assertEqual(legacy, retained)
        with closing(sqlite3.connect(database, isolation_level=None)) as connection:
            connection.execute("UPDATE tool_uses SET tool_input=?, input_excerpt=? WHERE tool_use_id=?",
                               ('{"API_KEY": "unfinished', "unsafe legacy excerpt", "structured-credentials"))
        omitted = self._content(self._call(3, "memory_get_tool_uses", {
            "project": str(self.project), "ids": ["structured-credentials"],
        }))[0]
        self.assertIsNone(omitted["tool_input"])
        self.assertIsNone(omitted["input_excerpt"])
        self.assertTrue(omitted["input_metadata"]["read_omitted"])
        self.assertNotIn("unsafe legacy excerpt", json.dumps(omitted))

    def test_legacy_truncated_raw_fragments_and_excerpts_are_explicitly_omitted(self) -> None:
        self._initialize()
        with Store(self.data_dir) as store:
            capture = normalize_capture({
                "tool_name": "shell", "tool_use_id": "legacy-truncated", "session_id": "privacy-fixture",
                "tool_input": {"text": "x" * 90_000},
                "tool_response": {"text": "x" * 90_000},
            }, project=str(self.project))
            entry = store.remember(self.project, "Legacy source", "Retained source.",
                                   source="hook:PostToolUse", session_id="privacy-fixture", tool_capture=capture)
        wrapper = json.loads(capture.tool_input)
        self.assertTrue(wrapper["__codex_mem_truncated__"])
        wrapper["head"] = '{"API_KEY":{"client_id":"public","value":"wrapped-secret"},"text":"' + wrapper["head"][100:]
        wrapper["tail"] = wrapper["tail"][:-100] + '","refresh_token":["tail-secret"]}'
        legacy = json.dumps(wrapper, separators=(",", ":"))
        self.assertLessEqual(len(legacy.encode()), 65_536)
        database = self.data_dir / "memory.sqlite3"
        with closing(sqlite3.connect(database, isolation_level=None)) as connection:
            connection.execute("UPDATE tool_uses SET tool_input=?, tool_response=?, input_excerpt=?, response_excerpt=? WHERE tool_use_id=?",
                               (legacy, legacy, "wrapped-secret", "tail-secret", "legacy-truncated"))
        result = self._content(self._call(1, "memory_get_tool_uses", {
            "project": str(self.project), "ids": ["legacy-truncated"],
        }))[0]
        self.assertEqual(entry["id"], result["entry_id"])
        self.assertEqual(capture.content_hash, result["content_hash"])
        for payload, excerpt, metadata in (("tool_input", "input_excerpt", "input_metadata"),
                                           ("tool_response", "response_excerpt", "response_metadata")):
            self.assertIsNone(result[payload])
            self.assertIsNone(result[excerpt])
            self.assertTrue(result[metadata]["read_omitted"])
            self.assertEqual("incomplete_json_fragments", result[metadata]["read_omitted_reason"])
        self.assertNotIn("wrapped-secret", json.dumps(result))
        self.assertNotIn("tail-secret", json.dumps(result))
        with closing(sqlite3.connect(database, isolation_level=None)) as connection:
            retained = connection.execute("SELECT tool_input, tool_response FROM tool_uses WHERE tool_use_id=?",
                                          ("legacy-truncated",)).fetchone()
        self.assertEqual((legacy, legacy), retained)
        # The observer receives omission metadata, not fragments as evidence.
        with Store(self.data_dir) as store:
            batch = store.claim_observation_batch(self.project, "privacy-check", "gpt-6-luna", "medium")
        self.assertEqual([entry["id"]], [source["id"] for source in batch["sources"]])
        tool_io = batch["sources"][0]["tool_io"]
        self.assertIsNone(tool_io["tool_input"])
        self.assertIsNone(tool_io["tool_response"])
        self.assertTrue(tool_io["input_metadata"]["read_omitted"])
        self.assertTrue(tool_io["response_metadata"]["read_omitted"])
        serialized = json.dumps(batch["sources"][0], ensure_ascii=False, separators=(",", ":"))
        self.assertLessEqual(len(serialized), MAX_OBSERVATION_CHARS)
        self.assertNotIn("wrapped-secret", serialized)
        self.assertNotIn("tail-secret", serialized)

    def test_resume_intent_is_exposed_ranked_and_validated(self) -> None:
        self._initialize()
        with Store(self.data_dir) as store:
            store.remember(self.project, "Roadmap roadmap theme", "Roadmap CSS theme.")
            summary = store.remember(self.project, "Handoff", "Roadmap deployment remains open.", kind="session_summary")
        response = self._call(1, "memory_search", {
            "project": str(self.project), "query": "roadmap", "intent": "resume", "limit": 1,
        })
        self.assertEqual([summary["id"]], [item["id"] for item in self._content(response)])
        self.assertEqual("resume", response["result"]["_meta"]["codexMemRetrieval"]["intent"])
        for request_id, intent in enumerate(("current", [], None), start=2):
            invalid = self._call(request_id, "memory_search", {
                "project": str(self.project), "query": "roadmap", "intent": intent,
            })
            self.assertTrue(invalid["result"]["isError"])

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

    def test_compact_search_anchor_timeline_and_full_details_round_trip(self) -> None:
        self._initialize()
        with Store(self.data_dir) as store:
            older = store.remember(self.project, "Before", "Earlier context.", session_id="review")
            anchor = store.remember(
                self.project, "Atomic rollback", "Rollback verified by a regression fixture.",
                session_id="review", observation={"type": "bugfix", "narrative": "Detailed evidence. " * 100},
            )
            newer = store.remember(self.project, "After", "Deployment remains unverified.", session_id="review")
        searched = self._content(self._call(1, "memory_search", {
            "project": str(self.project), "query": "rollback", "mode": "lexical",
        }))
        self.assertEqual([anchor["id"]], [record["id"] for record in searched])
        self.assertNotIn("narrative", searched[0]["observation"])
        self.assertNotIn("metadata", searched[0])
        timeline = self._content(self._call(2, "memory_timeline", {
            "project": str(self.project), "anchor_id": searched[0]["id"], "before": 1, "after": 1,
        }))
        self.assertEqual([older["id"], anchor["id"], newer["id"]], [record["id"] for record in timeline])
        self.assertEqual([False, True, False], [record["is_anchor"] for record in timeline])
        full_preview = self._content(self._call(3, "memory_search", {
            "project": str(self.project), "query": "rollback", "mode": "lexical", "detail": "full",
        }))
        fetched = self._content(self._call(4, "memory_get", {
            "project": str(self.project), "ids": [anchor["id"]],
        }))
        self.assertEqual(fetched[0]["observation"], full_preview[0]["observation"])
        for request_id, bad in enumerate((
            {"anchor_id": "../bad"}, {"anchor_id": anchor["id"], "before": True},
            {"anchor_id": anchor["id"], "before": 99, "after": 1}, {"before": 1},
            {"detail": []},
        ), start=5):
            response = self._call(request_id, "memory_timeline", {"project": str(self.project), **bad})
            self.assertTrue(response["result"]["isError"], bad)
        foreign = self._content(self._call(20, "memory_timeline", {
            "project": str(self.project / "other"), "anchor_id": anchor["id"],
        }))
        self.assertEqual([], foreign)

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


class MCPRawToolUseTests(unittest.TestCase):
    def test_raw_tool_read_passes_project_selectors_and_keeps_payload_opaque(self) -> None:
        class FakeStore:
            data_dir = "/tmp/codex-mem-test"

            def __init__(self) -> None:
                self.calls: list[tuple[str, object, object, int]] = []

            def get_tool_uses(
                self,
                project: str,
                *,
                ids: object,
                session_id: object,
                limit: int,
            ) -> list[dict[str, object]]:
                self.calls.append((project, ids, session_id, limit))
                return [
                    {
                        "project": project,
                        "tool_input": "echo never execute",
                        "tool_response": {"status": "ok"},
                    }
                ]

        fake = FakeStore()
        server = MemoryMCPServer(store=fake)  # type: ignore[arg-type]
        try:
            result = server._execute_tool(
                "memory_get_tool_uses",
                {
                    "project": "/tmp/project-a",
                    "ids": ["tool-use-1"],
                    "session_id": "session-a",
                    "limit": 3,
                },
            )
        finally:
            server.close()

        self.assertEqual(
            [("/tmp/project-a", ["tool-use-1"], "session-a", 3)],
            fake.calls,
        )
        self.assertEqual("echo never execute", result[0]["tool_input"])
        self.assertEqual({"status": "ok"}, result[0]["tool_response"])

    def test_native_punctuation_and_256_char_tool_use_id_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            project = root / "project"
            project.mkdir()
            tool_id = "native.call:" + ("x" * 244)
            self.assertEqual(256, len(tool_id))
            with Store(root / "memory-home") as store:
                capture = normalize_capture(
                    {
                        "tool_name": "shell",
                        "tool_use_id": tool_id,
                        "session_id": "session-a",
                        "tool_input": {"command": "echo safe"},
                        "tool_response": {"status": "ok"},
                    },
                    project=str(project),
                )
                self.assertIsNotNone(capture)
                assert capture is not None
                store.remember(
                    project,
                    "Captured tool evidence",
                    "The tool returned a bounded result.",
                    source="hook:PostToolUse",
                    tool_capture=capture,
                )
                server = MemoryMCPServer(store=store)
                result = server._execute_tool(
                    "memory_get_tool_uses",
                    {"project": str(project), "ids": [tool_id], "limit": 1},
                )
                self.assertEqual(tool_id, result[0]["tool_use_id"])

    def test_large_raw_page_keeps_records_and_marks_transport_field_truncation(self) -> None:
        class FakeStore:
            data_dir = "/tmp/codex-mem-test"

            def get_tool_uses(
                self,
                project: str,
                *,
                ids: object,
                session_id: object,
                limit: int,
            ) -> list[dict[str, object]]:
                return [
                    {
                        "tool_use_id": f"tool-{index}",
                        "project": project,
                        "tool_input": "x" * 70_000,
                        "tool_response": "y" * 70_000,
                        "input_metadata": {},
                        "response_metadata": {},
                    }
                    for index in range(5)
                ]

        server = MemoryMCPServer(store=FakeStore())  # type: ignore[arg-type]
        try:
            result = server._call_tool(
                {
                    "name": "memory_get_tool_uses",
                    "arguments": {"project": "/tmp/project-a", "limit": 5},
                }
            )
        finally:
            server.close()

        payload = json.loads(result["content"][0]["text"])
        self.assertTrue(payload["truncated"])
        self.assertEqual(5, payload["total"])
        self.assertEqual(5, payload["returned"])
        self.assertEqual(5, len(payload["tool_uses"]))
        self.assertEqual(5, len(payload["truncated_fields"]))
        self.assertTrue(payload["tool_uses"][0]["input_metadata"]["aggregate_truncated"])
        self.assertTrue(
            json.loads(payload["tool_uses"][0]["tool_input"])["__codex_mem_aggregate_truncated__"]
        )
        wire = json.dumps({"jsonrpc": "2.0", "id": 1, "result": result}, ensure_ascii=False).encode()
        self.assertLess(len(wire), MAX_LINE_BYTES)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
