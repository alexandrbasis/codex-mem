#!/usr/bin/env python3
"""Bounded, opt-in live comparison of two quality policies on fixed synthetic cases.

No production database is opened. Both policies receive identical, unmodified
candidates and sources; labels never enter model state. The content-free report
pins both source files and the fixture. This is a small regression probe, not a
measurement of production quarantine recovery.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
from pathlib import Path
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from codex_mem import jev_client, jev_quality
from codex_mem.config import load_config

FIXTURE = Path(__file__).resolve().parents[1] / "tests/fixtures/jev_quality_v8_pilot.json"
MAX_REQUESTS = 32


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def compare(baseline, fixture, *, key_file="", evaluator=None):
    cases = fixture["cases"]
    if not 1 <= len(cases) <= 8 or any(case["expected"] not in {"accept", "reject"} for case in cases):
        raise ValueError("invalid_cases")
    calls = 0

    def dispatch(payload):
        nonlocal calls
        if calls >= MAX_REQUESTS:
            raise jev_client.JevError("jev_input_limit")
        calls += 1
        return (evaluator(payload) if evaluator else
                jev_client._post(payload, time.monotonic() + jev_quality.MAX_GATE_SECONDS, key_file))

    report = {"scope": fixture["scope"], "labeling": fixture["labeling"],
              "evidence_mode": "test_double" if evaluator else "live_api",
              "historical_candidate_payloads_available": False,
              "maximum_requests": MAX_REQUESTS, "passes": []}
    for name, module in (("before", baseline), ("after", jev_quality)):
        rows = []
        for case in cases:
            claim = {"sources": case["sources"], "context": [], "project_context": ""}
            try:
                audit = module.quality_gate([case["candidate"]], None, claim,
                                           project="/synthetic-quality-probe", evaluator=dispatch)
            except module.JevQualityError as error:
                audit = error.audit
            rows.append({"id": case["id"], "expected": case["expected"], "category": case["category"],
                         "actual": audit["route"], "audit": audit})
        report["passes"].append({"name": name, "model": module.MODEL,
            "policy_version": module.POLICY_VERSION, "module_sha256": digest(Path(module.__file__)),
            "cases": rows,
            "supported_accepted": sum(row["expected"] == "accept" and row["actual"] == "accept" for row in rows),
            "unsupported_accepted": sum(row["expected"] == "reject" and row["actual"] == "accept" for row in rows),
            "unresolved": sum(row["actual"] not in {"accept", "rejected"} for row in rows),
            "input_tokens": sum(row["audit"]["usage"]["input_tokens"] for row in rows),
            "usage_complete": all(row["audit"]["usage_status"] == "reported" for row in rows),
            "duration_ms": sum(row["audit"]["duration_ms"] for row in rows),
            "requests": sum(row["audit"]["counts"]["requests"] for row in rows)})
    report["dispatched_requests"] = calls
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--baseline-source", type=Path, required=True,
                        help="Explicit trusted baseline Python source copied before modification")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if not args.live:
        print(json.dumps({"status": "not_run", "reason": "requires_explicit_live_flag"}))
        return 1
    spec = importlib.util.spec_from_file_location("codex_mem.quality_comparison_baseline", args.baseline_source)
    baseline = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(baseline)
    fixture = json.loads(FIXTURE.read_text())
    key_file = load_config().get("jev_filter_key_file", "")
    report = compare(baseline, fixture, key_file=key_file)
    report["fixture_sha256"] = digest(FIXTURE)
    args.output.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps({"report": str(args.output), "dispatched_requests": report["dispatched_requests"],
                      "passes": [{key: value for key, value in phase.items() if key != "cases"}
                                 for phase in report["passes"]]}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
