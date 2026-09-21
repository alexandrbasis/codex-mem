#!/usr/bin/env python3
"""Evaluate fixed synthetic labels with live Jev and inspect content-free usage.

Example:
  python3 scripts/jev_quality_eval.py --live --configured-key \
    --output /tmp/jev-quality.json

The live pass uses an isolated temporary SQLite cache and never runs the memory
generator. Optional production telemetry uses SQLite mode=ro, not Store.
"""
from __future__ import annotations

import argparse
from collections import Counter
from collections.abc import Callable, Mapping
from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import time
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from codex_mem import jev_cache, jev_filter
from codex_mem.config import load_config
from codex_mem.processor import ProcessorFailure, _filtered_lifecycle_claim
from codex_mem.pricing import price_event, snapshot_metadata
from codex_mem.store import Store, project_key

DEFAULT_FIXTURE = Path(__file__).resolve().parents[1] / "tests/fixtures/jev_quality_eval.json"
TOKEN_FIELDS = ("input_tokens", "cached_input_tokens", "cache_write_input_tokens",
                "output_tokens", "reasoning_output_tokens", "total_tokens")
JEV_PRICE = {"model": "jev-1.13.0", "input_usd_per_million": "0.042",
             "output_usd_per_million": "0", "reviewed_at": "2026-09-21",
             "source": "https://docs.typesafe.ai/models"}


def jev_cost(input_tokens: int, model: str) -> dict[str, Any]:
    subtotal = (str(Decimal(input_tokens) * Decimal(JEV_PRICE["input_usd_per_million"]) / 1_000_000)
                if model == JEV_PRICE["model"] else None)
    return {"observed_input_cost_usd": subtotal, "rate_card": JEV_PRICE,
            "basis": "public price applied to reported tokens; not an invoice"}


def duration_metrics(audits) -> dict[str, Any]:
    durations = [audit.get("duration_ms") for audit in audits]
    known = [value for value in durations if type(value) is int and value >= 0]
    return {"measured_duration_ms": sum(known), "duration_missing_attempts": len(durations) - len(known),
            "complete_duration_ms": sum(known) if len(known) == len(durations) else None}


def generator_metrics(rows) -> dict[str, Any]:
    """Price each attempt separately, preserving cached-input and unknown-tier semantics."""
    prices = []
    for row in rows:
        event = dict(row)
        if row["usage_status"] != "reported":
            event.update({key: None for key in TOKEN_FIELDS})
        prices.append(price_event(event)["api_equivalent_usd"])
    priceable = all(row["standard"] is not None for row in prices)
    standard = sum((Decimal(row["standard"]) for row in prices if row["standard"] is not None), Decimal(0))
    fast = sum((Decimal(row["fast"]) for row in prices if row["fast"] is not None), Decimal(0))
    return {
        "attempts": len(rows), "outcomes": dict(Counter(row["outcome"] for row in rows)),
        "models": dict(Counter((row["model"] or "unknown") for row in rows)),
        "usage_statuses": dict(Counter((row["usage_status"] or "unknown") for row in rows)),
        "tokens_reported": {key: sum((row[key] or 0) for row in rows if row["usage_status"] == "reported")
                            for key in TOKEN_FIELDS},
        "tokens_partial": {key: sum((row[key] or 0) for row in rows if row["usage_status"] == "partial")
                           for key in TOKEN_FIELDS},
        "measured_duration_ms": sum(row["duration_ms"] or 0 for row in rows),
        "duration_missing_attempts": sum(row["duration_ms"] is None for row in rows),
        "api_equivalent_usd": {
            "selected_total": None, "reason": "provider_service_tier_and_actual_billing_unavailable",
            "standard_scenario_subtotal": str(standard), "fast_scenario_subtotal": str(fast),
            "standard_scenario_total": str(standard) if priceable else None,
            "fast_scenario_total": str(fast) if priceable else None,
            "unpriced_attempts": sum(row["standard"] is None for row in prices),
            "rate_card": snapshot_metadata(),
            "precision": "attempt-total approximation; precise per-response context limits unavailable",
        },
    }


def _digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                    allow_nan=False).encode()).hexdigest()


def load_fixture(path: Path) -> dict[str, Any]:
    fixture = json.loads(path.read_text(encoding="utf-8"))
    cases, batches = fixture["cases"], fixture["batches"]
    if fixture.get("schema_version") != 1 or not cases or not batches:
        raise ValueError("invalid_fixture")
    ids = [row["id"] for row in cases]
    if len(ids) != len(set(ids)):
        raise ValueError("duplicate_fixture_id")
    for row in cases:
        if (row["expected"] not in {"retain", "discard"}
                or row["category"] not in jev_filter.CATEGORIES
                or not all(isinstance(row[key], str) and row[key]
                           for key in ("id", "source", "body", "reason"))):
            raise ValueError("invalid_fixture_case")
    for row in batches:
        if (type(row.get("summary_required")) is not bool
                or type(row.get("expected_generator_required")) is not bool
                or not row["sources"]
                or any(identifier not in ids for identifier in row["sources"] + row["context"])):
            raise ValueError("invalid_fixture_batch")
    return fixture


def _source(case: Mapping[str, Any], *, derived: bool = False) -> dict[str, Any]:
    # Labels and reasons are deliberately absent from the model input.
    source = {key: case[key] for key in ("id", "source", "body")}
    source["title"] = "Observation"
    if derived:
        source.update(source="processor:codex-observer-v1", kind="note")
    return source


def confusion(rows: list[dict[str, Any]]) -> dict[str, Any]:
    matrix = {"useful_retained": 0, "useful_discarded": 0,
              "routine_discarded": 0, "routine_retained": 0, "unresolved": 0}
    for row in rows:
        if row["actual"] not in {"retain", "discard"}:
            matrix["unresolved"] += 1
        elif row["expected"] == "retain":
            matrix["useful_retained" if row["actual"] == "retain" else "useful_discarded"] += 1
        else:
            matrix["routine_discarded" if row["actual"] == "discard" else "routine_retained"] += 1
    useful = matrix["useful_retained"] + matrix["useful_discarded"]
    routine = matrix["routine_discarded"] + matrix["routine_retained"]
    matrix["useful_recall"] = matrix["useful_retained"] / useful if useful else None
    matrix["routine_discard_rate"] = matrix["routine_discarded"] / routine if routine else None
    matrix["accuracy"] = ((matrix["useful_retained"] + matrix["routine_discarded"]) / len(rows)
                          if rows else None)
    return matrix


def evaluate_fixture(fixture: Mapping[str, Any], *, key_file: str = "", timeout: float = 60,
                     evaluator: Callable | None = None) -> dict[str, Any]:
    """Exercise the real gate/cache; an injected evaluator is test-only evidence."""
    started = time.monotonic()
    cases = {case["id"]: case for case in fixture["cases"]}
    report: dict[str, Any] = {
        "status": "running", "scope": "synthetic_regression_not_production_accuracy",
        "evidence_mode": "live_api" if evaluator is None else "test_double",
        "labeling": fixture["labeling"], "fixture_sha256": _digest(fixture),
        "model": jev_filter.MODEL, "policy_version": jev_filter.POLICY_VERSION,
        "questions_sha256": _digest(jev_filter.QUESTIONS),
        "case_count": len(cases), "passes": [], "batches": [],
        "generator": {"executed": False, "tokens": None, "latency_ms": None,
                      "counterfactual_saved_tokens": None, "counterfactual_saved_cost": None},
    }
    active_phase = "cold"
    with tempfile.TemporaryDirectory(prefix="codex-mem-jev-eval-") as temporary:
        project = Path(temporary) / "synthetic-project"
        project.mkdir()
        with Store(Path(temporary) / "cache") as store:
            def run(claim):
                begin = time.monotonic()
                filtered, audit = jev_filter.filter_claim(
                    claim, key_file=key_file, timeout=timeout, evaluator=evaluator,
                    cache_get=lambda payload: jev_cache.cache_get(store, project, payload),
                    cache_put=lambda payload, response: jev_cache.cache_put(store, project, payload, response))
                return filtered, audit, round((time.monotonic() - begin) * 1000)

            claim = {"sources": [_source(case) for case in cases.values()],
                     "context": [], "summary_required": False}
            try:
                for phase in ("cold", "warm"):
                    active_phase = phase
                    _, audit, elapsed = run(claim)
                    report["passes"].append({"phase": phase, "duration_ms": elapsed, "audit": audit})
                decisions = {row["source_id"]: row for row in report["passes"][0]["audit"]["decisions"]}
                report["results"] = [{
                    "case": case["id"], "expected": case["expected"],
                    "actual": decisions.get(case["id"], {}).get("route", "unresolved"),
                    "expected_category": case["category"],
                    "actual_categories": sorted({chunk["category"] for chunk in
                                                 decisions.get(case["id"], {}).get("chunks", [])}),
                } for case in cases.values()]
                report["confusion"] = confusion(report["results"])
                for batch in fixture["batches"]:
                    active_phase = "batch:" + batch["id"]
                    claimed = {"sources": [_source(cases[identifier]) for identifier in batch["sources"]],
                               "context": [_source(cases[identifier], derived=batch["summary_required"])
                                           for identifier in batch["context"]],
                               "summary_required": batch["summary_required"]}
                    filtered, audit, elapsed = run(claimed)
                    filtered = _filtered_lifecycle_claim(claimed, filtered, audit)
                    required = bool(filtered["sources"])
                    report["batches"].append({
                        "batch": batch["id"], "generator_required": required,
                        "expected_generator_required": batch["expected_generator_required"],
                        "matched": required == batch["expected_generator_required"],
                        "duration_ms": elapsed, "audit": audit})
                cold, warm = (row["audit"] for row in report["passes"])
                report["cache"] = {
                    "cold_requests": cold["counts"]["requests"],
                    "warm_requests": warm["counts"]["requests"],
                    "warm_hits": warm["counts"]["cache_hits"],
                    "warm_zero_tokens": warm["usage"] == {"input_tokens": 0, "output_tokens": 0},
                    "routes_unchanged": [(r["source_id"], r["route"]) for r in cold["decisions"]]
                    == [(r["source_id"], r["route"]) for r in warm["decisions"]],
                }
                report["batch_gate"] = {
                    "tested": len(report["batches"]),
                    "would_skip_generator": sum(not row["generator_required"] for row in report["batches"]),
                    "wrongful_skips": sum(not row["generator_required"] and row["expected_generator_required"]
                                          for row in report["batches"]),
                    "basis": "actual filter_claim and processor lifecycle gate; generator not executed",
                }
                report["status"] = "completed"
            except (jev_filter.JevFilterError, ProcessorFailure) as error:
                report.update(status="failed", failed_phase=active_phase,
                              code=error.code if isinstance(error, jev_filter.JevFilterError) else "processor_gate_failure")
                report["failed_audit"] = getattr(error, "audit", None)
    audits = [row["audit"] for row in report["passes"] + report["batches"]]
    if report.get("failed_audit"):
        audits.append(report["failed_audit"])
    report["jev_usage"] = {key: sum(audit.get("usage", {}).get(key, 0) for audit in audits)
                           for key in ("input_tokens", "output_tokens")}
    report["usage_completeness"] = "reported" if report["status"] == "completed" else "partial_or_unavailable"
    report["duration_ms"] = round((time.monotonic() - started) * 1000)
    report["cost_usd"] = None
    report["jev_cost"] = jev_cost(report["jev_usage"]["input_tokens"], jev_filter.MODEL)
    report["cost_status"] = "reported_Jev_portion_only_generator_not_executed"
    return report


def read_production_telemetry(database: Path, project: Path, since: str) -> dict[str, Any]:
    """Read existing receipts in one snapshot; never initialize production storage."""
    cutoff = datetime.fromisoformat(since.replace("Z", "+00:00"))
    if cutoff.tzinfo is None:
        raise ValueError("since_requires_timezone")
    cutoff = cutoff.astimezone(timezone.utc).isoformat()
    report: dict[str, Any] = {
        "scope": "production_metadata_only", "project": project_key(project), "since": cutoff,
        "read_at": datetime.now(timezone.utc).isoformat(),
        "event_quality": "unmeasured_no_record_bodies_or_independent_production_labels_read",
        "counterfactual_saved_tokens": None, "counterfactual_saved_cost_usd": None,
        "jev_duration_ms": None, "full_pipeline_duration_ms": None,
        "cost_usd": None, "cost_status": "public_price_scenarios_not_actual_billing",
    }
    uri = database.expanduser().resolve().as_uri() + "?mode=ro"
    with sqlite3.connect(uri, uri=True) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only=ON")
        connection.execute("BEGIN")
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "observation_jobs" not in tables:
            report.update(status="unavailable", reason="jobs_table_missing")
            return report
        filters = []
        if "jev_filter_attempts" in tables:
            filters = connection.execute(
                "SELECT f.job_id,f.attempt_count,f.audit_json,f.updated_at,j.status,j.disposition,"
                "j.attempt_count AS latest_attempt FROM jev_filter_attempts f JOIN observation_jobs j ON j.id=f.job_id "
                "WHERE j.project=? AND julianday(f.updated_at)>=julianday(?) ORDER BY f.updated_at",
                (report["project"], cutoff)).fetchall()
        usage = []
        if "observer_usage_attempts" in tables:
            usage = connection.execute(
                "SELECT u.*,j.model,j.reasoning_effort FROM observer_usage_attempts u "
                "JOIN observation_jobs j ON j.id=u.job_id "
                "WHERE j.project=? AND julianday(u.started_at)>=julianday(?) ORDER BY u.started_at",
                (report["project"], cutoff)).fetchall()
        records = []
        for row in filters:
            audit = json.loads(row["audit_json"])
            records.append((row, audit))
        report["jev"] = {
            "attempts": len(records), "unique_batches": len({row["job_id"] for row, _ in records}),
            "statuses": dict(Counter(audit.get("status", "unknown") for _, audit in records)),
            "models": dict(Counter(audit.get("model", "unknown") for _, audit in records)),
            "policy_versions": dict(Counter(audit.get("policy_version", "unknown") for _, audit in records)),
            "usage_statuses": dict(Counter(audit.get("usage_status", "unknown") for _, audit in records)),
            "counts": {key: sum(audit.get("counts", {}).get(key, 0) for _, audit in records)
                       for key in ("evaluated", "retained", "discarded", "chunks", "requests", "cache_hits")},
            "tokens_reported_or_partial": {key: sum(audit.get("usage", {}).get(key, 0) for _, audit in records)
                                           for key in ("input_tokens", "output_tokens")},
            "generator_started": sum(audit.get("generator_started") is True for _, audit in records),
            "confirmed_skipped_batches": sum(
                audit.get("status") == "success" and audit.get("generator_started") is False
                and row["status"] == "skipped" and row["attempt_count"] == row["latest_attempt"]
                for row, audit in records),
            "count_unit": "source or context evaluations across attempts, not distinct captured events",
            "counter_coverage": {key: sum(key in audit.get("counts", {}) for _, audit in records)
                                 for key in ("requests", "cache_hits")},
            **duration_metrics([audit for _, audit in records]),
        }
        report["generator"] = generator_metrics(usage)
        report["by_filter_policy"] = {}
        for policy in sorted({audit.get("policy_version", "unknown") for _, audit in records}):
            selected = [(row, audit) for row, audit in records if audit.get("policy_version", "unknown") == policy]
            keys = {(row["job_id"], row["attempt_count"]) for row, _ in selected}
            matched_usage = [row for row in usage if (row["job_id"], row["attempt_count"]) in keys]
            model_names = {audit.get("model", "unknown") for _, audit in selected}
            input_tokens = sum(audit.get("usage", {}).get("input_tokens", 0) for _, audit in selected)
            report["by_filter_policy"][policy] = {
                "attempts": len(selected), "generator": generator_metrics(matched_usage),
                "counts": {key: sum(audit.get("counts", {}).get(key, 0) for _, audit in selected)
                           for key in ("evaluated", "retained", "discarded", "chunks", "requests", "cache_hits")},
                "counter_coverage": {key: sum(key in audit.get("counts", {}) for _, audit in selected)
                                     for key in ("requests", "cache_hits")},
                "jev_input_tokens": input_tokens,
                "jev_cost": jev_cost(input_tokens, next(iter(model_names)) if len(model_names) == 1 else "mixed"),
                "jev_duration": duration_metrics([audit for _, audit in selected]),
                "generator_started": sum(audit.get("generator_started") is True for _, audit in selected),
                "confirmed_skipped_batches": sum(
                    audit.get("status") == "success" and audit.get("generator_started") is False
                    and row["status"] == "skipped" and row["attempt_count"] == row["latest_attempt"]
                    for row, audit in selected),
            }
        usage_keys = {(row["job_id"], row["attempt_count"]) for row in usage}
        report["generator_receipts_missing_after_start"] = sum(
            audit.get("generator_started") is True and (row["job_id"], row["attempt_count"]) not in usage_keys
            for row, audit in records)
        report["jev_duration_ms"] = report["jev"]["complete_duration_ms"]
        started_keys = {(row["job_id"], row["attempt_count"]) for row, audit in records
                        if audit.get("generator_started") is True}
        pipeline_generator = [row for row in usage if (row["job_id"], row["attempt_count"]) in started_keys]
        if (report["jev_duration_ms"] is not None
                and not report["generator_receipts_missing_after_start"]
                and all(row["duration_ms"] is not None for row in pipeline_generator)):
            report["full_pipeline_duration_ms"] = (report["jev_duration_ms"]
                                                    + sum(row["duration_ms"] for row in pipeline_generator))
        report["pipeline_duration_basis"] = "sum of recorded Jev gate and matching generator attempt durations; excludes queue wait"
        report["status"] = "completed"
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true", help="Spend Jev tokens on the synthetic fixture.")
    parser.add_argument("--fixture", type=Path, default=DEFAULT_FIXTURE)
    parser.add_argument("--key-file", default="")
    parser.add_argument("--configured-key", action="store_true", help="Read only the configured key path.")
    parser.add_argument("--timeout", type=float, default=60)
    parser.add_argument("--telemetry-db", type=Path)
    parser.add_argument("--project", type=Path)
    parser.add_argument("--since", help="Inclusive ISO timestamp with timezone for recorded telemetry.")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if not args.live and not args.telemetry_db:
        parser.error("choose --live or --telemetry-db; no network requests are made by default")
    if args.telemetry_db and (not args.project or not args.since):
        parser.error("--telemetry-db requires an explicit --project and --since")
    report: dict[str, Any] = {"schema_version": 1, "created_at": datetime.now(timezone.utc).isoformat()}
    if args.live:
        key_file = args.key_file or (load_config().get("jev_filter_key_file", "") if args.configured_key else "")
        report["synthetic"] = evaluate_fixture(load_fixture(args.fixture), key_file=key_file, timeout=args.timeout)
    if args.telemetry_db:
        report["production"] = read_production_telemetry(args.telemetry_db, args.project, args.since)
    if args.output:
        args.output.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    compact = {key: {field: value for field, value in section.items()
                     if field not in {"passes", "batches", "results", "failed_audit"}}
               if isinstance(section, dict) else section for key, section in report.items()}
    print(json.dumps(compact, indent=2, ensure_ascii=False))
    return 1 if any(isinstance(section, dict) and section.get("status") != "completed"
                    for section in report.values()) else 0


if __name__ == "__main__":
    raise SystemExit(main())
