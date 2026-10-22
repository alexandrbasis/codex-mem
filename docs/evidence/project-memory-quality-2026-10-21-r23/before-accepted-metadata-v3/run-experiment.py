"""Run paired fictional readers from one shared prefix and one bounded ledger."""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import time


def phase_plan(histories: list[str], regeneration_ids: list[str]):
    """Yield the fixed execution order; eligibility is checked by the caller."""
    for history in histories:
        yield "generate", history, "baseline", None
        yield "clone", history, "candidate", None
        yield "consolidate", history, "candidate", None
    for phase in ("read", "judge"):
        for variant in ("baseline", "candidate"):
            for history in histories:
                yield phase, history, variant, None
    for history in histories:
        if history not in regeneration_ids:
            continue
        repeat = "regeneration-1"
        yield "generate", history, "baseline", repeat
        yield "clone", history, "candidate", repeat
        yield "consolidate", history, "candidate", repeat
        for phase in ("read", "judge"):
            for variant in ("baseline", "candidate"):
                yield phase, history, variant, repeat


def needs_model(phase: str) -> bool:
    # Candidate preparation is a state audit, not model consolidation.
    return phase in {"generate", "read", "judge"}


def phase_runtime(phase: str, variant: str | None, protocol: dict,
                  baseline: Path, repo: Path) -> Path:
    """Producer source does not change reader arms or shared storage identity."""
    source = protocol.get("shared_generation_runtime", "baseline")
    if source not in {"baseline", "candidate"} or (
            "shared_generation_runtime" in protocol and protocol.get("comparison_mode", "shared-prefix") != "shared-prefix"):
        raise ValueError("invalid_shared_generation_runtime")
    if source == "candidate":
        declared = protocol.get("baseline_runtime_root")
        if not isinstance(declared, str) or str(baseline.resolve()) != declared:
            raise ValueError("baseline_runtime_differs_from_protocol")
        if phase == "generate":
            return repo
    return baseline if variant == "baseline" else repo


def validate_independent_review(fixture_path: Path, gold_path: Path, run: Path, protocol: dict) -> dict | None:
    """Verify sealed review metadata, never execute or expose review findings."""
    expected = protocol.get("independent_review_contract")
    if expected is None:
        return None
    basename = "sealed-semantic-review-metadata.json"
    if (not isinstance(expected, dict) or type(expected.get("schema_version")) is not int
            or expected["schema_version"] != 1 or expected.get("metadata_receipt_basename") != basename):
        raise ValueError("invalid_independent_review_contract")
    pins = ("metadata_receipt_sha256", "metadata_schema_sha256", "fixture_sha256", "gold_sha256",
            "mechanical_contract_sha256", "semantic_guidance_sha256", "sealed_review_receipt_sha256")
    for key in pins:
        value = expected.get(key)
        if not isinstance(value, str) or len(value) != 64 or any(c not in "0123456789abcdef" for c in value):
            raise ValueError("invalid_independent_review_pin:" + key)
    here = Path(__file__).parent
    paths = {"metadata_receipt_sha256": fixture_path.with_name(basename),
             "metadata_schema_sha256": here / "sealed-semantic-review-metadata.schema.json",
             "fixture_sha256": fixture_path, "gold_sha256": gold_path,
             "mechanical_contract_sha256": here / "public-fixture-contract.py",
             "semantic_guidance_sha256": here / "prospective-gold-semantics-contract.json",
             "sealed_review_receipt_sha256": fixture_path.with_name("sealed-semantic-review.json")}
    hashes = {key: hashlib.sha256(path.read_bytes()).hexdigest() for key, path in paths.items()}
    if any(hashes[key] != expected[key] for key in pins):
        raise ValueError("independent_review_bytes_differ_from_protocol")
    if protocol.get("prospective_author_contract", {}).get("sha256") != hashes["mechanical_contract_sha256"]:
        raise ValueError("independent_review_mechanical_contract_pin_differs")
    raw = paths["metadata_receipt_sha256"].read_bytes()
    if len(raw) > 65536 or hashlib.sha256(raw).hexdigest() != expected["metadata_receipt_sha256"]:
        raise ValueError("independent_review_metadata_changed_or_oversized")
    metadata = json.loads(raw)
    bindings = ("fixture_sha256", "gold_sha256", "mechanical_contract_sha256",
                "semantic_guidance_sha256", "sealed_review_receipt_sha256")
    keys = {"schema_version", "status", "unresolved_findings", "independence_attestation", "coverage", *bindings}
    if not isinstance(metadata, dict) or set(metadata) != keys:
        raise ValueError("invalid_independent_review_metadata_shape")
    coverage = metadata["coverage"]
    coverage_keys = {"questions", "required_facts", "all_questions_reviewed",
                     "all_required_facts_reviewed", "negative_cases_reviewed"}
    if (type(metadata["schema_version"]) is not int or metadata["schema_version"] != 1
            or metadata["status"] != "accepted" or type(metadata["unresolved_findings"]) is not int
            or metadata["unresolved_findings"] != 0
            or metadata["independence_attestation"] != "independent_author_inputs_only_no_product_prior_inputs_answers_or_grades"
            or not isinstance(coverage, dict) or set(coverage) != coverage_keys
            or type(coverage["questions"]) is not int or coverage["questions"] != 96
            or type(coverage["required_facts"]) is not int or coverage["required_facts"] < 1
            or any(coverage[key] is not True for key in ("all_questions_reviewed", "all_required_facts_reviewed", "negative_cases_reviewed"))
            or any(metadata[key] != hashes[key] for key in bindings)):
        raise ValueError("independent_review_not_accepted_or_incomplete")
    # Count the same pinned bytes, without importing author code or reading
    # detailed review findings. Positive coverage attestations cannot replace
    # the actual question and required-fact denominators.
    input_bytes = {key: paths[key].read_bytes() for key in ("fixture_sha256", "gold_sha256")}
    if any(hashlib.sha256(raw).hexdigest() != hashes[key] for key, raw in input_bytes.items()):
        raise ValueError("independent_review_inputs_changed_before_count")
    fixture = json.loads(input_bytes["fixture_sha256"])
    gold = json.loads(input_bytes["gold_sha256"])
    try:
        actual_questions = sum(len(history["questions"]) for history in fixture["histories"])
        actual_facts = sum(len(question["required_facts"]) for question in gold["questions"].values())
    except (KeyError, TypeError, AttributeError):
        raise ValueError("invalid_independent_review_input_count_shape") from None
    if coverage["questions"] != actual_questions or coverage["required_facts"] != actual_facts:
        raise ValueError("independent_review_counts_differ_from_pinned_inputs")
    receipt = {"status": "independent_review_metadata_validated", **hashes,
               "questions_reviewed": coverage["questions"], "required_facts_reviewed": coverage["required_facts"]}
    receipt_path = run / "independent-review-validation.json"
    serialized = json.dumps(receipt, indent=2) + "\n"
    if receipt_path.exists():
        if receipt_path.read_text() != serialized:
            raise RuntimeError("independent_review_validation_receipt_differs")
    else:
        with receipt_path.open("x") as output:
            output.write(serialized)
    return receipt


def validate_author_inputs(fixture_path: Path, gold_path: Path, run: Path, *,
                           expected_contract_sha256: str) -> dict:
    """Validate newly opened pinned inputs before any experiment phase starts.

    The caller first asks the frozen helper to validate its input pins. This
    prospective author contract checks source representability, not semantics
    or answer quality. A rejected packet is never repaired or dispatched.
    """
    contract_path = Path(__file__).with_name("public-fixture-contract.py")
    observed_contract_sha256 = hashlib.sha256(contract_path.read_bytes()).hexdigest()
    if observed_contract_sha256 != expected_contract_sha256:
        raise RuntimeError("author_contract_differs_from_protocol")
    paths = {"fixture_sha256": fixture_path, "gold_sha256": gold_path,
             "contract_sha256": contract_path}
    hashes = {key: hashlib.sha256(path.read_bytes()).hexdigest() for key, path in paths.items()}
    spec = importlib.util.spec_from_file_location("prospective_author_contract", contract_path)
    contract = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(contract)
    validation = contract.validate_files(fixture_path, gold_path)
    if any(hashlib.sha256(path.read_bytes()).hexdigest() != hashes[key] for key, path in paths.items()):
        raise RuntimeError("author_input_changed_during_validation")
    receipt = {"status": "author_input_contract_validated", **hashes, "validation": validation}
    receipt_path = run / "author-input-validation.json"
    serialized = json.dumps(receipt, indent=2) + "\n"
    if receipt_path.exists():
        if receipt_path.read_text() != serialized:
            raise RuntimeError("author_input_validation_receipt_differs")
    else:
        with receipt_path.open("x") as output:
            output.write(serialized)
    return receipt


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", type=Path, required=True)
    parser.add_argument("--baseline", type=Path, required=True)
    parser.add_argument("--fixture", type=Path, required=True)
    parser.add_argument("--gold", type=Path, required=True)
    args = parser.parse_args()
    repo = Path(__file__).resolve().parents[3]
    script = repo / "scripts/project_memory_consolidation_experiment.py"
    protocol_path = Path(__file__).with_name("protocol.json")
    sys.path.insert(0, str(repo))
    spec = importlib.util.spec_from_file_location("experiment", script)
    experiment = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(experiment)
    protocol = experiment.read(protocol_path)
    if hashlib.sha256(Path(__file__).read_bytes()).hexdigest() != protocol["driver_sha256"]:
        raise RuntimeError("experiment_driver_changed_after_protocol_pin")
    if (protocol.get("comparison_mode") != "shared-prefix"
            or protocol.get("candidate_processing_mode") != "read_only_snapshot"):
        raise ValueError("driver_requires_read_only_shared_prefix")
    phase_runtime("read", "baseline", protocol, args.baseline, repo)
    run = args.run.resolve()
    if not str(run).startswith("/private/tmp/"):
        raise ValueError("Run directory must resolve under /private/tmp")
    if os.environ.get("CODEX_MEM_DISABLED") == "1":
        raise RuntimeError("unexpected_disabled_environment")
    # Frozen-input checks happen before this driver opens any holdout history.
    subprocess.run([
        sys.executable, str(script), "validate", "--run", str(run),
        "--protocol", str(protocol_path), "--fixture", str(args.fixture),
        "--gold", str(args.gold),
    ], cwd=repo, check=True)
    validate_independent_review(args.fixture, args.gold, run, protocol)
    validate_author_inputs(args.fixture, args.gold, run,
                           expected_contract_sha256=protocol["prospective_author_contract"]["sha256"])
    fixture = experiment.fixture(args.fixture)
    split = fixture["split"]
    journal = run / "baseline" / f"{split}-phase-execution.jsonl"
    if journal.exists():
        raise RuntimeError("split_already_started_no_automatic_retry")
    live_flag = {"generate", "consolidate", "read", "judge"}

    def execute(phase, history=None, variant=None, replicate=None):
        counters = experiment.usage(experiment.ledger_calls(run / "calls.jsonl"))
        if not counters["token_accounting_complete"]:
            raise RuntimeError("unknown_usage_stop")
        elapsed = time.time() - experiment.read(run / "run.json")["started_at"]
        limits = protocol["limits"]
        if needs_model(phase) and (
            counters["attempted_calls"] >= limits["max_calls"]
            or counters["tokens"]["total_tokens"] + limits["per_call_token_reservation"]
            > limits["max_observed_tokens"]
            or elapsed + limits["call_seconds"] >= limits["wall_seconds"]
        ):
            raise RuntimeError("budget_exhausted_stop")
        runtime = phase_runtime(phase, variant, protocol, args.baseline, repo)
        command = [
            sys.executable, str(script), phase, "--run", str(run),
            "--runtime-root", str(runtime), "--protocol", str(protocol_path),
            "--fixture", str(args.fixture),
        ]
        for flag, value in (("--history", history), ("--variant", variant),
                            ("--replicate", replicate)):
            if value:
                command += [flag, value]
        if phase in {"judge", "report"}:
            command += ["--gold", str(args.gold)]
        if phase in live_flag:
            command += ["--live"]
        identity = dict(phase=phase, history_id=history, variant=variant, replicate=replicate)
        experiment.append(journal, dict(identity, status="started", at=time.time(), command=command))
        result = subprocess.run(command, cwd=repo)
        experiment.append(journal, dict(identity, status="finished", at=time.time(),
                                        returncode=result.returncode))
        counters = experiment.usage(experiment.ledger_calls(run / "calls.jsonl"))
        if not counters["token_accounting_complete"]:
            raise RuntimeError("unknown_usage_stop")
        if result.returncode:
            raise RuntimeError("phase_failed_no_automatic_retry:" + phase)
        home = run / "replicates" / replicate if replicate else run
        if phase in {"generate", "clone", "consolidate"}:
            arm = "baseline" if phase == "generate" else "candidate"
            name = "consolidation.json" if phase == "consolidate" else "generation.json"
            metadata = experiment.read(home / arm / history / name)
            if metadata["status"] != "complete":
                if any(job.get("status") == "disabled" for job in metadata.get("jobs", [])):
                    raise RuntimeError("processing_disabled_stop")
                experiment.append(journal, dict(
                    identity, status="pipeline_failure_no_retry", at=time.time(),
                    jobs=[{key: job.get(key) for key in ("status", "code")}
                          for job in metadata.get("jobs", [])],
                ))
                return False
            if phase == "consolidate" and (
                metadata.get("candidate_processing_mode") != "read_only_snapshot"
                or metadata.get("accepted_merge_count") != 0
                or metadata.get("jobs")
            ):
                raise RuntimeError("candidate_preparation_was_not_read_only")
        print(json.dumps(dict(identity, status="phase_complete",
                              calls=counters["attempted_calls"],
                              tokens=counters["tokens"]["total_tokens"])), flush=True)
        return True

    try:
        histories = [history["id"] for history in fixture["histories"]]
        unavailable = set()
        for phase, history, variant, replicate in phase_plan(
            histories, protocol["regeneration_repeat_history_ids"]
        ):
            pair = history, replicate
            if pair in unavailable:
                experiment.append(journal, dict(
                    phase=phase, history_id=history, variant=variant, replicate=replicate,
                    status="skipped_invalid_shared_prefix", at=time.time(),
                ))
                continue
            if not execute(phase, history, variant, replicate):
                # A failed common prefix cannot produce a valid pair. The
                # report retains both missing arms in the planned denominator.
                unavailable.add(pair)
        execute("report")
    except Exception as exc:
        experiment.append(journal, dict(status="driver_stopped", at=time.time(),
                                        error_type=type(exc).__name__, error=str(exc),
                                        automatic_retry=False))
        try:
            execute("report")
        except Exception:
            pass
        raise
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
