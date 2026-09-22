#!/usr/bin/env python3
"""Compare full and short-circuit Jev evaluation using one explicit private sample.

The manifest binds project-local source IDs to blinded case IDs. Labels are a
separate list of {case, label} objects. Existing data is read with SQLite mode=ro;
only the disposable replay cache is writable. No network or model calls are made.
Reports contain metadata, never source bodies. Historical reconstruction is
accepted only when every full payload digest matches an existing cached response.
"""
from __future__ import annotations

import argparse
from collections import Counter
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from codex_mem import jev_cache, jev_filter
from codex_mem.store import Store, project_key
from codex_mem.tool_io import hydrate_source_tool_io


def _digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def read_sample(connection, project, manifest):
    """Use the source serializers without running Store initialization or schema code."""
    if project_key(manifest.get("scope", "")) != project:
        raise ValueError("manifest_project_mismatch")
    rows = manifest.get("sources")
    if not isinstance(rows, list) or not 1 <= len(rows) <= 100:
        raise ValueError("invalid_sample_size")
    if len({row["case"] for row in rows}) != len(rows) or len({row["source_id"] for row in rows}) != len(rows):
        raise ValueError("duplicate_sample_case")
    reader = object.__new__(Store)
    reader._conn = connection
    reader._closed = False
    sources, cases = [], {}
    for row in rows:
        case = row["case"]
        if not isinstance(case, str) or re.fullmatch(r"[A-Za-z0-9._-]{1,64}", case) is None:
            raise ValueError("invalid_case_id")
        stored = connection.execute("SELECT * FROM entries WHERE id=? AND project=?",
                                    (row["source_id"], project)).fetchone()
        if stored is None:
            raise ValueError("sample_source_unavailable")
        source = hydrate_source_tool_io(connection, reader._records_from_rows([stored])[0], project=project)
        expected = row.get("hydrated_source_sha256")
        if expected is not None and _digest(jev_filter._redact(source)) != expected:
            raise ValueError("sample_source_changed_since_labeling")
        sources.append(source)
        cases[source["id"]] = case
    return sources, cases


def compare_routes(sources, cases, responses, labels, summary_required=None):
    """Replay exact typed responses through the real filter and a temporary cache."""
    baseline, payloads = {}, {}
    summary_required = summary_required or {}
    for source in sources:
        parts = list(jev_filter._payloads(jev_filter._redact(source), "sources", summary_required.get(source["id"], False)))
        payloads[source["id"]] = parts
        decisions = [jev_filter._validate(responses[jev_cache.payload_key(part)])[0] for part in parts]
        # This is the unchanged v3 full-fragment OR policy, not a new threshold.
        discarded = all(d["useful_probability"] <= .2 and d["category"] == "routine" and d["confidence"] >= .8
                        for d in decisions)
        baseline[source["id"]] = "discard" if discarded else "retain"
    calls = []
    def evaluate(payload):
        key = jev_cache.payload_key(payload)
        calls.append(key)
        return responses[key]
    claims = [{"sources": [source for source in sources if summary_required.get(source["id"], False) == required],
               "context": [], "summary_required": required} for required in (False, True)]
    with tempfile.TemporaryDirectory(prefix="codex-mem-jev-replay-") as directory:
        project = Path(directory) / "project"
        with Store(Path(directory) / "cache") as store:
            kwargs = dict(evaluator=evaluate,
                          cache_get=lambda p: jev_cache.cache_get(store, project, p),
                          cache_put=lambda p, r: jev_cache.cache_put(store, project, p, r))
            cold = [jev_filter.filter_claim(claim, **kwargs) for claim in claims if claim["sources"]]
            warm = [jev_filter.filter_claim(claim, **kwargs)[1] for claim in claims if claim["sources"]]
    decisions = [row for _, audit in cold for row in audit["decisions"]]
    actual = {row["source_id"]: row["route"] for row in decisions}
    retained = {source["id"]: source for filtered, _ in cold for source in filtered["sources"]}
    quality = Counter()
    results = []
    for source in sources:
        identifier = source["id"]
        label = labels.get(cases[identifier], "unlabeled")
        if label not in {"useful", "routine", "uncertain", "ambiguous", "unlabeled"}:
            raise ValueError("invalid_sample_label")
        quality[label + "_" + actual[identifier]] += 1
        results.append({"case": cases[identifier], "label": label,
                        "baseline": baseline[identifier], "optimized": actual[identifier],
                        "baseline_fragments": len(payloads[identifier]),
                        "optimized_fragments": len(next(row["chunks"] for row in decisions
                                                        if row["source_id"] == identifier))})
    return {
        "source_count": len(sources), "quality_counts": dict(quality), "cases": results,
        "routes_unchanged": actual == baseline,
        "retained_sources_unchanged": all(retained[source["id"]] == jev_filter._redact(source) for source in sources
                                         if actual[source["id"]] == "retain"),
        "baseline_fragment_requests_without_cache": sum(len(parts) for parts in payloads.values()),
        "optimized_fragment_requests_without_cache": sum(audit["counts"]["requests"] for _, audit in cold),
        "short_circuited_fragments": sum(audit["counts"].get("short_circuited_chunks", 0) for _, audit in cold),
        "warm_requests": sum(audit["counts"]["requests"] for audit in warm),
        "warm_cache_hits": sum(audit["counts"]["cache_hits"] for audit in warm),
        "warm_zero_tokens": all(audit["usage"] == {"input_tokens": 0, "output_tokens": 0} for audit in warm),
        "generator_executed": False, "generator_calls_saved": None,
        "request_savings_basis": "cold exact-response replay; not measured production request savings",
        "quality_boundary": "one blinded reviewer and one selected workload; not general accuracy",
    }, calls


def run(database, project, manifest, labels, *, historical_exact=False):
    project = project_key(project)
    responses, missing, selected, summary_required, views, historical = {}, [], [], {}, Counter(), {}
    uri = database.expanduser().resolve().as_uri() + "?mode=ro"
    with closing(sqlite3.connect(uri, uri=True)) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only=ON")
        connection.execute("BEGIN")
        sources, cases = read_sample(connection, project, manifest)
        has_cache = connection.execute("SELECT 1 FROM sqlite_master WHERE name='jev_evaluation_cache'").fetchone()
        for source in sources:
            candidates = [("current", source)]
            if historical_exact:
                candidates.append(("before_supersession", dict(source, superseded_by=None,
                                   superseded_at=None, updated_at=source["created_at"])))
            match = None
            for view, candidate in candidates:
                for required in ((False, True) if historical_exact else (False,)):
                    found = {}
                    for payload in jev_filter._payloads(jev_filter._redact(candidate), "sources", required):
                        key = jev_cache.payload_key(payload)
                        row = connection.execute("SELECT response_json FROM jev_evaluation_cache WHERE project=? AND cache_key=?",
                                                 (project, key)).fetchone() if has_cache else None
                        try:
                            response = jev_cache._safe_response(json.loads(row[0])) if row is not None else None
                        except (ValueError, TypeError, jev_filter.JevFilterError):
                            response = None
                        if response is None:
                            break
                        found[key] = response
                    else:
                        match = (view, candidate, required, found)
                        break
                if match is not None:
                    break
            if match is None:
                missing.append(cases[source["id"]])
            else:
                view, candidate, required, found = match
                selected.append(candidate)
                summary_required[source["id"]] = required
                responses.update(found)
                views[view] += 1
        has_audits = connection.execute("SELECT 1 FROM sqlite_master WHERE name='jev_filter_attempts'").fetchone()
        for row in manifest["sources"]:
            if has_audits and row.get("job_id"):
                recorded = connection.execute("SELECT a.audit_json FROM jev_filter_attempts a JOIN observation_jobs j ON j.id=a.job_id "
                                              "WHERE a.job_id=? AND j.project=? ORDER BY a.attempt_count DESC LIMIT 1",
                                              (row["job_id"], project)).fetchone()
                if recorded is not None:
                    decision = next((d for d in json.loads(recorded[0])["decisions"]
                                     if d["location"] == "sources" and d["source_id"] == row["source_id"]), None)
                    if decision is not None:
                        historical[row["case"]] = decision["route"]
    report = {"scope": "one_project_private_source_replay", "project": project,
              "created_at": datetime.now(timezone.utc).isoformat(), "model": jev_filter.MODEL,
              "policy_version": jev_filter.POLICY_VERSION, "evaluation_strategy": jev_filter.EVALUATION_STRATEGY,
              "evidence_mode": "exact_cached_responses_no_network",
              "manifest_sha256": _digest(manifest), "labels_sha256": _digest(labels),
              "source_count": len(sources), "exact_cached_fragments": len(responses),
              "missing_source_cases": missing, "source_views": dict(views), "live_requests": 0,
              "live_usage": {"input_tokens": 0, "output_tokens": 0},
              "replay_scope": "full source payloads with exact cached digests, preserving matched summary_required; no historic context reconstruction"}
    report["historical_quality_counts"] = dict(Counter(labels.get(case, "unlabeled") + "_" + route
                                                       for case, route in historical.items()))
    report["historical_source_routes_count"] = len(historical)
    expected_historical = sum(bool(row.get("job_id")) for row in manifest["sources"])
    report["expected_historical_source_routes"] = expected_historical
    report["historical_provenance_status"] = ("not_requested" if not expected_historical else
                                               "complete" if len(historical) == expected_historical else "incomplete")
    if missing:
        return dict(report, status="incomplete", reason="missing_exact_cached_responses")
    result, _ = compare_routes(selected, cases, responses, labels, summary_required)
    report.update(result)
    mismatches = [row["case"] for row in report["cases"] if row["case"] in historical
                  and historical[row["case"]] != row["baseline"]]
    report["historical_route_mismatches"] = mismatches
    report["historical_routes_match_replay"] = (not mismatches if report["historical_provenance_status"] == "complete" else None)
    if not report["routes_unchanged"] or not report["retained_sources_unchanged"] or mismatches:
        report["status"] = "failed"
    elif report["historical_provenance_status"] == "incomplete":
        report["status"] = "incomplete"
    else:
        report["status"] = "completed"
    report["usage_completeness"] = "reported"
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--project", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--labels", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--historical-exact", action="store_true",
                        help="Try pre-supersession lifecycle fields only when the complete payload digest matches cache.")
    args = parser.parse_args()
    manifest = json.loads(args.manifest.read_text())
    rows = json.loads(args.labels.read_text())
    labels = {row["case"]: row["label"] for row in rows}
    if len(labels) != len(rows):
        parser.error("labels must contain unique case IDs")
    report = run(args.database, args.project, manifest, labels, historical_exact=args.historical_exact)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({key: value for key, value in report.items() if key != "cases"}, ensure_ascii=False, indent=2))
    return 0 if report["status"] == "completed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
