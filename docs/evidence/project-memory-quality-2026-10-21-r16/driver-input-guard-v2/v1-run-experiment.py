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


def validate_author_inputs(fixture_path: Path, gold_path: Path, run: Path) -> dict:
    """Validate newly opened pinned inputs before any experiment phase starts.

    The caller first asks the frozen helper to validate its input pins. This
    prospective author contract checks source representability, not semantics
    or answer quality. A rejected packet is never repaired or dispatched.
    """
    contract_path = Path(__file__).with_name("public-fixture-contract.py")
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
    validate_author_inputs(args.fixture, args.gold, run)
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
