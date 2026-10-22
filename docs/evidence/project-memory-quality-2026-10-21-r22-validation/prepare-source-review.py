"""Pin all owned working changes without staging or reading sealed cases."""
from pathlib import Path
import datetime
import hashlib
import json
import subprocess

ROOT = Path(__file__).resolve().parents[3]
OUT = ROOT / "docs/evidence/project-memory-quality-2026-10-21-r22"
PRIOR = ROOT / "docs/evidence/project-memory-quality-2026-10-21-r21/review-source-manifest.json"
BASE = "2ca7445139805243c1d30a1ff7fd15be2b2dd85a"


def sha(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def paths(*arguments):
    return set(filter(None, subprocess.check_output(
        ["git", *arguments], cwd=ROOT).decode().split("\0")))


def main():
    subprocess.run(["git", "rev-parse", "--verify", BASE], cwd=ROOT,
                   check=True, capture_output=True)
    prior = json.loads(PRIOR.read_text())
    tracked = paths("diff", "--name-only", BASE, "-z")
    untracked = paths("ls-files", "--others", "--exclude-standard", "-z")
    foreign = {"AGENTS.md", "docs/agents/installation-checks.json"}
    excluded = {str((OUT / name).relative_to(ROOT)) for name in (
        "review-source-manifest.json", "central-source-readiness.json",
        "review-spec-source.json", "review-spec-harness.json",
        "review-standards-source.json", "review-standards-harness.json")}
    owned = sorted(name for name in tracked | untracked
                   if name not in foreign | excluded and not name.startswith("docs/handoffs/"))
    for name in owned:
        assert (ROOT / name).is_file() and not (ROOT / name).is_symlink(), name
    pins = {name: sha(ROOT / name) for name in owned}
    unchanged = sorted(name for name in owned
                       if prior["owned_file_sha256"].get(name) == pins[name])
    changed = sorted(set(owned) - set(unchanged))
    patch = Path("/private/tmp/codex-mem-quality-20261021-r22-complete-source.patch")
    with patch.open("xb") as stream:
        subprocess.run(["git", "diff", "--binary", BASE, "--",
                        *sorted(tracked & set(owned))], cwd=ROOT, stdout=stream, check=True)
        for name in sorted(untracked & set(owned)):
            result = subprocess.run(["git", "diff", "--no-index", "--binary", "--",
                                     "/dev/null", name], cwd=ROOT, stdout=stream)
            assert result.returncode in (0, 1), name
    validation = OUT / "test-evidence.json"
    runtime = json.loads((OUT / "runtime-source-receipt.json").read_text())
    assert all(sha(ROOT / name) == pin for name, pin in runtime["runtime_files"].items())
    assert sha(ROOT / "scripts/project_memory_consolidation_experiment.py") == runtime["helper_sha256"]
    result = {key: prior[key] for key in (
        "mode", "base_sha", "requirements_source", "requirements_sha256",
        "preserved_user_exclusions", "excluded_user_prefixes", "standards_sources", "commands")}
    result.update(
        created_at_utc=datetime.datetime.now(datetime.timezone.utc).isoformat(),
        checkpoint_commit=subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT).decode().strip(),
        requirements_scope="R22 source readiness: supported continuation slots, bounded diagnostic checkpoints, "
        "explicit complete shared-pipeline receipts, unchanged reader requests, and strict prospective audit counts. "
        "Fresh quality, final consolidated human review and local commit remain open.",
        owned_paths=owned, owned_file_sha256=pins,
        owned_total_bytes=sum((ROOT / name).stat().st_size for name in owned),
        tracked_owned_paths=sorted(tracked & set(owned)),
        untracked_owned_paths=sorted(untracked & set(owned)),
        self_and_reviewer_output_exclusions=sorted(excluded),
        standards_source_sha256={name: sha(ROOT / name) for name in prior["standards_sources"]},
        prior_review_manifest=str(PRIOR.relative_to(ROOT)), prior_review_manifest_sha256=sha(PRIOR),
        prior_review_policy="Carry only exact hash-unchanged files from independent prior reviews. "
        "R21 source hold was explicitly released only after canonical incomplete export, actual audit and bundle verification.",
        exact_unchanged_prior_paths=unchanged, changed_or_new_since_prior=changed,
        runtime_files=runtime["runtime_files"], helper_sha256=runtime["helper_sha256"],
        complete_binary_patch=str(patch), complete_binary_patch_sha256=sha(patch),
        complete_binary_patch_bytes=patch.stat().st_size,
        commit_list=subprocess.check_output(["git", "log", BASE + "..HEAD", "--oneline"], cwd=ROOT).decode().splitlines(),
        current_validation=str(validation.relative_to(ROOT)), current_validation_sha256=sha(validation),
        native_calls=0, actual_protocol_generated=False, native_clock_started=False,
        actual_freeze_exists=False, formal_release_exists=False, private_new_case_content_opened=False,
        limits=["Complete owned working changes include untracked evidence and predecessor copies.",
                "R21 incomplete result and all historical grades, failures and costs remain unchanged.",
                "Private R22 fixture, gold, rationale and full semantic findings remain outside this manifest.",
                "Source clearance and offline tests do not establish new model answer quality.",
                "Foreign AGENTS, installation checks and handoffs remain outside commit ownership."])
    destination = OUT / "review-source-manifest.json"
    with destination.open("x") as stream:
        stream.write(json.dumps(result, indent=2) + "\n")
    print(json.dumps({"owned_files": len(owned), "exact_unchanged_prior": len(unchanged),
                      "new_or_changed": len(changed), "manifest_sha256": sha(destination),
                      "complete_patch_sha256": sha(patch), "patch_bytes": patch.stat().st_size}))


if __name__ == "__main__":
    main()
