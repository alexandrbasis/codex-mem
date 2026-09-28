#!/usr/bin/env python3
"""Evaluate source support and bounded retrieval on fixed synthetic fixtures.

Live evaluation is opt-in:
  python3 scripts/jev_integration_eval.py --live --configured-key --output /tmp/jev-integration.json

Every run uses a temporary SQLite Store. No production corpus is opened, no
memory generator runs, and no generator savings are estimated. Without --live,
only the fixture manifest is produced and acceptance remains unresolved.
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
import sys
import tempfile
import time
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from codex_mem import jev_client, jev_quality, jev_retrieval
from codex_mem.config import load_config
from codex_mem.store import Store

DEFAULT_FIXTURE = ROOT / "tests/fixtures/jev_integration_eval.json"
RATE_CARD = {"model": "jev-1.13.0", "input_usd_per_million": "0.042",
             "reviewed_at": "2026-09-22", "source": "https://docs.typesafe.ai/models"}
ACCEPTANCE_POLICY = {"quality_useful_recall_min": 1.0, "critical_unsupported_acceptances_max": 0,
                     "retrieval_recall_regressions_max": 0, "retrieval_forbidden_results_max": 0,
                     "require_retrieval_improvement": True, "require_warm_cache": True}


def digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                    allow_nan=False, separators=(",", ":")).encode()).hexdigest()


def load_fixture(path: Path) -> dict[str, Any]:
    fixture = json.loads(path.read_text(encoding="utf-8"))
    if fixture.get("schema_version") != 1 or not fixture.get("labeling"):
        raise ValueError("invalid_fixture")
    if fixture.get("acceptance_policy") != ACCEPTANCE_POLICY:
        raise ValueError("unsupported_acceptance_policy")
    for group in ("quality_cases", "retrieval_cases"):
        cases = fixture.get(group)
        if not isinstance(cases, list) or not cases:
            raise ValueError("missing_fixture_cases")
        ids = [case["id"] for case in cases]
        if len(ids) != len(set(ids)):
            raise ValueError("duplicate_fixture_case")
    for case in fixture["quality_cases"]:
        if (case.get("expected") not in {"accept", "reject"}
                or type(case.get("critical_unsupported")) is not bool
                or case.get("candidate_kind") not in {"note", "session_summary"}
                or not isinstance(case.get("project_context", ""), str)
                or not case.get("reason") or not case.get("sources")):
            raise ValueError("invalid_quality_case")
        source_ids = {source["id"] for source in case["sources"]}
        if (len(source_ids) != len(case["sources"])
                or not case["candidate"].get("source_ids")
                or not set(case["candidate"]["source_ids"]) <= source_ids
                or case["critical_unsupported"] != (case["expected"] == "reject")):
            raise ValueError("invalid_quality_evidence")
    for case in fixture["retrieval_cases"]:
        ids = [candidate["id"] for candidate in case["candidates"]]
        if (not ids or len(ids) != len(set(ids)) or not case.get("query")
                or case.get("route") not in {"context", "search"}
                or type(case.get("limit")) is not int or not 1 <= case["limit"] <= 12
                or not case.get("reason")):
            raise ValueError("invalid_retrieval_case")
        for field in ("baseline_ids", "relevant_ids", "forbidden_ids"):
            if not isinstance(case.get(field), list) or not set(case[field]) <= set(ids):
                raise ValueError("invalid_retrieval_labels")
        if set(case["relevant_ids"]) & set(case["forbidden_ids"]):
            raise ValueError("contradictory_retrieval_labels")
    return fixture


def manifest(fixture: Mapping[str, Any]) -> dict[str, Any]:
    policies = {}
    for name, module in (("quality", jev_quality), ("retrieval", jev_retrieval)):
        policies[name] = {"version": module.POLICY_VERSION,
                          "module_sha256": hashlib.sha256(Path(module.__file__).read_bytes()).hexdigest()}
    return {"schema_version": 1, "created_at": datetime.now(timezone.utc).isoformat(),
            "status": "not_run", "scope": "synthetic_regression_not_production_accuracy",
            "labeling": fixture["labeling"], "fixture_sha256": digest(fixture),
            "acceptance_policy": {**fixture["acceptance_policy"],
                "require_live_api_evidence": True, "require_resolved_judgments": True,
                "require_lower_warm_gate_wall_time": True, "require_lower_warm_reported_input_tokens": True},
            "requested_model": jev_client.MODEL, "model_sha256": digest(jev_client.MODEL),
            "policies": policies, "client_sha256": hashlib.sha256(Path(jev_client.__file__).read_bytes()).hexdigest(),
            "case_counts": {"quality": len(fixture["quality_cases"]), "retrieval": len(fixture["retrieval_cases"])},
            "baseline_scope": {"quality": "Ungated persistence of these same generated candidates.",
                               "retrieval": "Authored bounded shortlists; not a measured production search baseline."},
            "generator": {"executed": False, "tokens": None, "latency_ms": None,
                          "saved_tokens": None, "saved_cost_usd": None},
            "acceptance": {"passed": False, "fixture_checks_passed": False,
                           "blocking_reasons": ["live_evaluation_not_run"]}}


def quality_inputs(case: Mapping[str, Any]) -> tuple[list, dict | None, dict]:
    # Labels, category and rationale never enter candidate/evidence state.
    candidate = dict(case["candidate"])
    summary = candidate if case["candidate_kind"] == "session_summary" else None
    claimed = {key: case.get(key, [] if key != "project_context" else "")
               for key in ("sources", "context", "project_context")}
    claimed["summary_required"] = summary is not None
    return [] if summary is not None else [candidate], summary, claimed


def _client_audits(audit: Mapping[str, Any]) -> list[dict[str, Any]]:
    # A quality aggregate already contains all request receipts. Count the
    # individual receipts once, never both the aggregate and its children.
    if isinstance(audit.get("evaluations"), list):
        return list(audit["evaluations"])
    return [dict(audit)] if (isinstance(audit.get("counts"), Mapping)
                            and isinstance(audit.get("model"), str)) else []


def _integer(value: Any) -> bool:
    return type(value) is int and value >= 0


def metrics(rows: list[dict[str, Any]]) -> dict[str, Any]:
    audits = [audit for row in rows for audit in _client_audits(row.get("audit", {}))]
    requests = [audit.get("counts", {}).get("requests") for audit in audits]
    hits = [audit.get("counts", {}).get("cache_hits") for audit in audits]
    known_noops = sum(row.get("decision_source") == "local_protected_noop" for row in rows)
    all_usage = (bool(rows) and all(_client_audits(row.get("audit", {}))
                 or row.get("decision_source") == "local_protected_noop" for row in rows)
                 and all(audit.get("usage_status") == "reported" for audit in audits))
    result: dict[str, Any] = {
        "gate_calls": len(rows), "evaluation_receipts": len(audits),
        "gate_calls_without_evaluation_receipt": sum(not _client_audits(row.get("audit", {})) for row in rows),
        "verified_local_noop_calls": known_noops,
        "requests": sum(value for value in requests if _integer(value)),
        "cache_hits": sum(value for value in hits if _integer(value)),
        "counter_coverage_complete": len(requests) == len(audits) and all(_integer(v) for v in requests + hits),
        "usage_statuses": dict(Counter(audit.get("usage_status", "unavailable") for audit in audits)),
        "measured_gate_wall_ms": round(sum(row.get("wall_ms", 0) for row in rows), 3),
    }
    for field in ("input_tokens", "output_tokens"):
        values = [audit.get("usage", {}).get(field) for audit in audits]
        known = sum(value for value in values if _integer(value))
        result[field + "_reported_or_partial"] = known
        result[field] = known if all_usage and all(_integer(value) for value in values) else None
    result["usage_complete"] = all_usage and result["input_tokens"] is not None and result["output_tokens"] is not None
    durations = [audit.get("duration_ms") for audit in audits]
    result["evaluation_duration_ms"] = (round(sum(durations), 3) if durations and
        all(type(value) in {int, float} and value >= 0 for value in durations) else None)
    models = {audit.get("model") for audit in audits}
    known_cost = (str(Decimal(result["input_tokens_reported_or_partial"]) *
                     Decimal(RATE_CARD["input_usd_per_million"]) / 1_000_000)
                  if models == {RATE_CARD["model"]} else "0" if known_noops == len(rows) and rows else None)
    result["input_cost_usd"] = {"reported_token_subtotal": known_cost,
                               "complete_total": known_cost if result["usage_complete"] else None,
                               "rate_card": RATE_CARD,
                               "basis": "Public input price applied only to API-reported tokens; not an invoice or generator savings."}
    return result


def quality_scores(rows: list[dict[str, Any]]) -> dict[str, Any]:
    useful = [row for row in rows if row["expected"] == "accept"]
    return {"cases": len(rows), "correct": sum(row["actual"] == row["expected"] for row in rows),
            "critical_unsupported_accepted": sum(row["critical_unsupported"] and row["actual"] == "accept" for row in rows),
            "useful_accepted": sum(row["actual"] == "accept" for row in useful),
            "useful_rejected": sum(row["actual"] == "reject" for row in useful),
            "unresolved": sum(row["actual"] not in {"accept", "reject"} for row in rows),
            "useful_recall": sum(row["actual"] == "accept" for row in useful) / len(useful) if useful else None,
            "accuracy": sum(row["actual"] == row["expected"] for row in rows) / len(rows) if rows else None,
            "ungated_critical_unsupported_accepted": sum(row["critical_unsupported"] for row in rows)}


def retrieval_score(case: Mapping[str, Any], selected: list[str]) -> dict[str, Any]:
    relevant = set(case["relevant_ids"])
    baseline = case["baseline_ids"][:case["limit"]]
    baseline_hits, selected_hits = len(set(baseline) & relevant), len(set(selected) & relevant)
    return {"case": case["id"], "baseline_ids": baseline, "selected_ids": selected,
            "relevant_ids": case["relevant_ids"], "baseline_hits": baseline_hits, "selected_hits": selected_hits,
            "baseline_recall": baseline_hits / len(relevant) if relevant else None,
            "recall": selected_hits / len(relevant) if relevant else None,
            "baseline_precision": baseline_hits / len(baseline) if baseline else None,
            "precision": selected_hits / len(selected) if selected else None,
            "recall_regressed": selected_hits < baseline_hits,
            "recall_improved": selected_hits > baseline_hits,
            "forbidden_results": sorted(set(selected) & set(case["forbidden_ids"])),
            "out_of_fixture_results": sorted(set(selected) - {row["id"] for row in case["candidates"]}),
            "no_match_correct": not selected if not relevant else None}


def retrieval_resolution(case: Mapping[str, Any], score: Mapping[str, Any], audit: Mapping[str, Any]) -> tuple[str, str]:
    if audit.get("status") == "ranked":
        return "resolved", "jev"
    if (audit.get("status") == "skipped" and audit.get("skip_reason") == "protected_no_additions"
            and audit.get("requests") == 0 and audit.get("evaluated_candidates") == 0
            and score["selected_ids"] == score["baseline_ids"] and score["recall"] == 1
            and not score["forbidden_results"] and not score["out_of_fixture_results"]):
        return "resolved", "local_protected_noop"
    return "unresolved", "unresolved"


def _seed_retrieval(store: Store, project: Path, case: Mapping[str, Any]) -> tuple[dict, dict]:
    by_fixture, by_record = {}, {}
    for candidate in case["candidates"]:
        record = store.remember(project, candidate["title"], candidate["body"], kind=candidate["kind"],
                                session_id=candidate["id"], source="manual")
        with store._lock:
            store._write(lambda: store._connection.execute(
                "UPDATE entries SET created_at=?,updated_at=? WHERE id=?",
                (candidate["event_at"], candidate["event_at"], record["id"])))
        by_fixture[candidate["id"]] = store.get(project, [record["id"]])[0]
        by_record[record["id"]] = candidate["id"]
    return by_fixture, by_record


def acceptance(report: Mapping[str, Any]) -> dict[str, Any]:
    phases = report.get("passes", [])
    reasons = []
    if len(phases) != 2 or report.get("status") != "completed":
        reasons.append("execution_incomplete")
    for phase in phases:
        quality, retrieval = phase["quality_scores"], phase["retrieval"]
        if quality["critical_unsupported_accepted"]:
            reasons.append(phase["phase"] + ":critical_unsupported_claim_accepted")
        if quality["useful_recall"] != 1:
            reasons.append(phase["phase"] + ":useful_quality_recall_regressed")
        if quality["unresolved"] or any(row["resolution"] != "resolved" for row in retrieval):
            reasons.append(phase["phase"] + ":unresolved_judgment")
        if any(row["recall_regressed"] for row in retrieval):
            reasons.append(phase["phase"] + ":retrieval_recall_regressed")
        if any(row["forbidden_results"] or row["out_of_fixture_results"]
               or row["no_match_correct"] is False for row in retrieval):
            reasons.append(phase["phase"] + ":irrelevant_or_forbidden_result")
        if not any(row["recall_improved"] for row in retrieval):
            reasons.append(phase["phase"] + ":retrieval_improvement_unproven")
    cache = report.get("cache_comparison", {})
    if not cache.get("decisions_unchanged"):
        reasons.append("cache_changed_decisions")
    if not cache.get("warm_zero_requests_and_tokens") or not cache.get("warm_has_hits"):
        reasons.append("warm_cache_not_verified")
    if not cache.get("measured_gate_wall_time_reduced"):
        reasons.append("warm_latency_improvement_unproven")
    if not cache.get("reported_input_cost_reduced"):
        reasons.append("warm_input_cost_improvement_unproven")
    fixture_passed = not reasons
    if report.get("evidence_mode") != "live_api":
        reasons.append("test_double_is_not_live_quality_evidence")
    return {"passed": not reasons, "fixture_checks_passed": fixture_passed,
            "blocking_reasons": reasons,
            "scope": "Only this fixed synthetic fixture and warm exact-input repeats; production and generator savings remain unmeasured."}


def evaluate_fixture(fixture: Mapping[str, Any], *, key_file: str = "", timeout: float = 20,
                     evaluator: Callable | None = None) -> dict[str, Any]:
    started = time.perf_counter()
    report = manifest(fixture)
    report.update(status="running", evidence_mode="live_api" if evaluator is None else "test_double", passes=[])
    settings = {"jev_retrieval_enabled": True, "jev_retrieval_projects": [], "excluded_projects": [],
                "jev_filter_key_file": key_file}
    with tempfile.TemporaryDirectory(prefix="codex-mem-jev-integration-") as temporary:
        root = Path(temporary)
        with Store(root / "data") as store:
            retrieval_data = {case["id"]: _seed_retrieval(store, root / case["id"], case)
                              for case in fixture["retrieval_cases"]}
            for phase in ("cold", "warm"):
                quality_rows, retrieval_rows = [], []
                for case in fixture["quality_cases"]:
                    begin = time.perf_counter()
                    actual, error_code = "unresolved", None
                    try:
                        audit = jev_quality.quality_gate(*quality_inputs(case), project=str(root / "quality"),
                            store=store, timeout=timeout, key_file=key_file, evaluator=evaluator)
                        actual = "accept" if audit.get("route") == "accept" else "unresolved"
                    except jev_quality.JevQualityError as error:
                        audit, error_code = error.audit, error.code
                        actual = "reject" if error.code == "jev_quality_rejected" else "unresolved"
                    quality_rows.append({"case": case["id"], "expected": case["expected"],
                        "critical_unsupported": case["critical_unsupported"], "actual": actual,
                        "error_code": error_code, "audit": audit,
                        "wall_ms": round((time.perf_counter() - begin) * 1000, 3)})
                for case in fixture["retrieval_cases"]:
                    records, by_record = retrieval_data[case["id"]]
                    begin = time.perf_counter()
                    ranked, audit = jev_retrieval.rerank(store, root / case["id"], case["query"],
                        [records[identifier] for identifier in case["baseline_ids"]], candidates=list(records.values()),
                        settings=settings, evaluator=evaluator, timeout=timeout, route=case["route"])
                    selected = [by_record.get(record["id"], "unexpected_record") for record in ranked[:case["limit"]]]
                    score = retrieval_score(case, selected)
                    resolution, decision_source = retrieval_resolution(case, score, audit)
                    score.update(audit=audit, resolution=resolution, decision_source=decision_source,
                                 wall_ms=round((time.perf_counter() - begin) * 1000, 3))
                    retrieval_rows.append(score)
                report["passes"].append({"phase": phase, "quality": quality_rows,
                    "quality_scores": quality_scores(quality_rows), "retrieval": retrieval_rows,
                    "metrics": metrics(quality_rows + retrieval_rows)})
    cold, warm = report["passes"]
    c, w = cold["metrics"], warm["metrics"]
    report["cache_comparison"] = {
        "cold_requests": c["requests"], "warm_requests": w["requests"], "warm_cache_hits": w["cache_hits"],
        "warm_zero_requests_and_tokens": w["requests"] == 0 and w["input_tokens"] == 0 and w["output_tokens"] == 0,
        "warm_has_hits": w["cache_hits"] > 0,
        "decisions_unchanged": ([row["actual"] for row in cold["quality"]] == [row["actual"] for row in warm["quality"]]
            and [row["selected_ids"] for row in cold["retrieval"]] == [row["selected_ids"] for row in warm["retrieval"]]),
        "measured_gate_wall_time_reduced": w["measured_gate_wall_ms"] < c["measured_gate_wall_ms"],
        "reported_input_cost_reduced": (c["input_tokens"] is not None and w["input_tokens"] is not None
                                         and w["input_tokens"] < c["input_tokens"]),
        "latency_scope": "Sum of measured gate calls on this fixture; excludes synthetic setup and any generator.",
    }
    rows = [row for phase in report["passes"] for section in ("quality", "retrieval") for row in phase[section]]
    report.update(status="completed", cumulative_metrics=metrics(rows),
                  total_wall_ms=round((time.perf_counter() - started) * 1000, 3))
    report["acceptance"] = acceptance(report)
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--configured-key", action="store_true", help="Opt in to reading the configured key path.")
    parser.add_argument("--key-file", default="")
    parser.add_argument("--fixture", type=Path, default=DEFAULT_FIXTURE)
    parser.add_argument("--timeout", type=float, default=20)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    if args.configured_key and not args.live:
        parser.error("--configured-key requires --live")
    fixture = load_fixture(args.fixture)
    if args.live:
        key_file = args.key_file or (load_config().get("jev_filter_key_file", "") if args.configured_key else "")
        report = evaluate_fixture(fixture, key_file=key_file, timeout=args.timeout)
    else:
        report = manifest(fixture)
    if args.output:
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    compact = {key: value for key, value in report.items() if key != "passes"}
    if "passes" in report:
        compact["passes"] = [{key: value for key, value in phase.items() if key not in {"quality", "retrieval"}}
                             for phase in report["passes"]]
    print(json.dumps(compact, ensure_ascii=False, indent=2, allow_nan=False))
    return 0 if report["status"] == "completed" and report["acceptance"]["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
