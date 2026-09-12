"""CLI and MCP expose the same read-only accounting result and explicit refresh."""
import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from codex_mem.cli import main
from codex_mem.mcp import MemoryMCPServer, TOOLS, bound_usage_report
from codex_mem.store import Store
from codex_mem.usage_store import UsageStore


class UsageInterfaceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.data_dir = Path(self.tmp.name) / "data"
        self.project = Path(self.tmp.name) / "project"
        self.project.mkdir()
        with UsageStore(self.data_dir) as store:
            store.commit_scan("fixture", None, {"offset": 1, "parser_state": {}},
                              {"thread_id": "root", "session_id": "root", "project": str(self.project)},
                              [{"event_key": "fixture-response", "thread_id": "root", "session_id": "root",
                                "response_id": "response", "source_kind": "response", "model": "gpt-6-astra",
                                "recorded_at": "2026-09-11T22:00:00Z", "input_tokens": 1000,
                                "cached_input_tokens": 800, "cache_write_input_tokens": 0,
                                "output_tokens": 100, "reasoning_output_tokens": 40, "total_tokens": 1100}])

    def test_cli_and_mcp_period_report_agree_without_scanning(self):
        from codex_mem.usage_api import usage_report
        args = {"from_date": "2026-09-12", "to_date": "2026-09-13", "timezone": "Asia/Jerusalem",
                "project": str(self.project), "group_by": ["day", "model"]}
        with patch("codex_mem.usage.UsageCollector", side_effect=AssertionError("cached report must not scan")):
            direct = usage_report(self.data_dir, **args)
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                code = main(["--data-dir", str(self.data_dir), "usage", "report", "--from", args["from_date"],
                             "--to", args["to_date"], "--timezone", args["timezone"], "--project", str(self.project),
                             "--group-by", "day,model"])
            self.assertEqual(0, code)
            cli = json.loads(output.getvalue())
            with Store(self.data_dir) as store, MemoryMCPServer(store=store) as server:
                mcp = server._execute_tool("memory_usage_report", args)
            for result in (cli, mcp):
                self.assertEqual(direct["period"], result["period"])
                self.assertEqual(direct["main"], result["main"])
                self.assertEqual("not_requested", result["refresh"]["status"])

    def test_report_and_refresh_have_distinct_effect_annotations(self):
        tools = {tool.name: tool for tool in TOOLS}
        self.assertTrue(tools["memory_usage_report"].annotations["readOnlyHint"])
        self.assertFalse(tools["memory_usage_refresh"].annotations["readOnlyHint"])
        self.assertNotIn("refresh", tools["memory_usage_report"].input_schema["properties"])

    def test_invalid_bounds_do_not_construct_collector(self):
        from codex_mem.usage_api import usage_refresh, usage_report
        with patch("codex_mem.usage.UsageCollector", side_effect=AssertionError("invalid refresh must not write")):
            for args in ({"max_files": True}, {"max_files": 129}, {"max_bytes": 0},
                         {"from_date": "2026-09-13", "to_date": "2026-09-12"}):
                with self.subTest(args=args), self.assertRaises(ValueError):
                    usage_refresh(self.data_dir, **args)
            with self.assertRaises(ValueError):
                usage_report(self.data_dir, refresh=True, group_by=("unsupported",))

    def test_mcp_invalid_grouping_returns_tool_error(self):
        with Store(self.data_dir) as store, MemoryMCPServer(store=store) as server:
            for group_by in (["day", "day"], ["unsupported"], "day", []):
                result = server._call_tool({"name": "memory_usage_report", "arguments": {"group_by": group_by}})
                self.assertTrue(result["isError"])

    def test_large_mcp_breakdown_preserves_totals_and_exposes_omissions(self):
        report = {"main": {"event_count": 10000}, "groups": {"main": {"agent": {
            "rows": [{"value": str(i), "label": "x" * 1000} for i in range(100)],
            "total_groups": 120, "omitted_groups": 20, "limit": 100}}}}
        result = bound_usage_report(report, maximum=10000)
        self.assertLessEqual(len(json.dumps(result).encode()), 10000)
        self.assertEqual(report["main"], result["main"])
        self.assertEqual(100, len(report["groups"]["main"]["agent"]["rows"]))
        group = result["groups"]["main"]["agent"]
        self.assertEqual(120, len(group["rows"]) + group["omitted_groups"])
        self.assertTrue(result["transport"]["truncated"])


if __name__ == "__main__":
    unittest.main()
