#!/usr/bin/env python3
"""Offline accounting correctness and cached-query timing on 5,000 responses."""
from __future__ import annotations

import json
from pathlib import Path
import statistics
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from codex_mem import __version__
from codex_mem.usage_api import usage_report
from codex_mem.usage_store import UsageStore


def run():
    with tempfile.TemporaryDirectory(prefix="codex-mem-usage-acceptance-") as folder:
        base = Path(folder)
        data_dir = base / "data"
        project = base / "fictional-project"
        project.mkdir()
        session = {"thread_id": "root", "session_id": "root", "project": str(project)}
        events = [{"event_key": f"response-{i}", "response_id": f"response-{i}",
                   "thread_id": "root", "session_id": "root", "source_kind": "response",
                   "model": "gpt-6-astra", "model_source": "turn_context",
                   "requested_service_tier": "priority" if i % 2 else "default",
                   "requested_service_tier_source": "thread_settings_nested",
                   "recorded_at": "2026-09-11T22:00:00Z", "quality": "response_exact",
                   "input_tokens": 1000, "cached_input_tokens": 800, "cache_write_input_tokens": 0,
                   "output_tokens": 100, "reasoning_output_tokens": 40, "total_tokens": 1100}
                  for i in range(5000)]
        with UsageStore(data_dir) as store:
            for expected, offset in [(None, 1), (1, 2)]:
                assert store.commit_scan("fixture", expected, {"offset": offset, "parser_state": {}}, session, events)
            count = store.store._connection.execute("SELECT count(*) FROM usage_events").fetchone()[0]
        options = dict(from_date="2026-09-12", to_date="2026-09-13", timezone="Asia/Jerusalem",
                       project=str(project), group_by=("day", "model"), codex_home=base / "codex")
        elapsed = []
        for _ in range(3):
            start = time.perf_counter()
            result = usage_report(data_dir, **options)
            elapsed.append(time.perf_counter() - start)
        main = result["main"]
        from decimal import Decimal
        # Independent fixture arithmetic: 2,500 standard replies at $0.0078
        # and 2,500 fast replies at $0.0156; credits use their separate 2.5x.
        checks = {
            "replay_keeps_5000_responses": count == 5000,
            "exact_api_usd_58_50": Decimal(main["api_equivalent_usd"]["total"]) == Decimal("58.50"),
            "separate_credits_1706_25": Decimal(main["estimated_codex_credits"]["total"]) == Decimal("1706.25"),
            "reasoning_not_double_counted": main["total_tokens"] == 5_500_000,
            "cached_report_no_refresh": result["refresh"]["status"] == "not_requested",
            "median_under_one_second": statistics.median(elapsed) < 1,
        }
        return {"status": "passed" if all(checks.values()) else "failed", "version": __version__,
                "synthetic_only": True, "response_count": count, "checks": checks,
                "elapsed_seconds": [round(item, 6) for item in elapsed],
                "boundary": "Offline fixture, local machine timings; no model calls or real billing claim."}


if __name__ == "__main__":
    receipt = run()
    print(json.dumps(receipt, indent=2))
    raise SystemExit(0 if receipt["status"] == "passed" else 1)
