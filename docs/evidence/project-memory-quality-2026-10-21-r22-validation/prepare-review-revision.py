"""Rebuild the complete review patch after the isolated checkpoint repair."""
from pathlib import Path
import datetime
import hashlib
import json
import subprocess

ROOT = Path(__file__).resolve().parents[3]
OUT = ROOT / "docs/evidence/project-memory-quality-2026-10-21-r22"


def sha(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def paths(*args):
    return set(filter(None, subprocess.check_output(["git", *args], cwd=ROOT).decode().split("\0")))


def main():
    destination = OUT / "review-source-manifest.json"
    old = json.loads(destination.read_text())
    assert sha(destination) == "65da97be5c6aa8142365944535d2280aafdd961fd2aa813622ce345d8c69defb"
    changed_old = {name for name, pin in old["owned_file_sha256"].items() if sha(ROOT / name) != pin}
    expected = {
        "docs/evidence/project-memory-quality-2026-10-21-r22-audit/pipeline_contract.py",
        "docs/evidence/project-memory-quality-2026-10-21-r22-audit/test-pipeline-contract.py",
    }
    assert changed_old == expected, changed_old
    tracked = paths("diff", "--name-only", old["base_sha"], "-z")
    untracked = paths("ls-files", "--others", "--exclude-standard", "-z")
    excluded = set(old["self_and_reviewer_output_exclusions"])
    excluded.update(str((OUT / name).relative_to(ROOT)) for name in (
        "review-spec-source.v002.json", "review-spec-harness.v002.json",
        "review-standards-source.v002.json", "review-standards-harness.v002.json"))
    owned = sorted(name for name in tracked | untracked if name not in excluded
                   and name not in {"AGENTS.md", "docs/agents/installation-checks.json"}
                   and not name.startswith("docs/handoffs/"))
    assert set(old["owned_paths"]) <= set(owned)
    pins = {name: sha(ROOT / name) for name in owned}
    patch = Path("/private/tmp/codex-mem-quality-20261021-r22-complete-source.v003.patch")
    assert not patch.exists()
    replacements = {}
    for name in changed_old:
        result = subprocess.run(["git", "diff", "--no-index", "--binary", "--", "/dev/null", name],
                                cwd=ROOT, capture_output=True)
        assert result.returncode == 1
        header = f"diff --git a/{name} b/{name}\n".encode()
        assert result.stdout.startswith(header)
        replacements[header] = result.stdout
    replaced = set()
    with Path(old["complete_binary_patch"]).open("rb") as source, patch.open("xb") as target:
        skip = False
        for line in source:
            if line.startswith(b"diff --git "):
                skip = line in replacements
                if skip:
                    target.write(replacements[line])
                    replaced.add(line)
            if not skip:
                target.write(line)
        assert replaced == set(replacements)
        for name in sorted(set(owned) - set(old["owned_paths"])):
            assert name in untracked
            result = subprocess.run(["git", "diff", "--no-index", "--binary", "--", "/dev/null", name],
                                    cwd=ROOT, stdout=target)
            assert result.returncode in (0, 1), name
    prior = json.loads((ROOT / old["prior_review_manifest"]).read_text())
    unchanged = sorted(name for name in owned if prior["owned_file_sha256"].get(name) == pins[name])
    validation = OUT / "test-evidence.v002.json"
    result = {**old, "created_at_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
              "owned_paths": owned, "owned_file_sha256": pins,
              "owned_total_bytes": sum((ROOT / name).stat().st_size for name in owned),
              "tracked_owned_paths": sorted(tracked & set(owned)),
              "untracked_owned_paths": sorted(untracked & set(owned)),
              "self_and_reviewer_output_exclusions": sorted(excluded),
              "exact_unchanged_prior_paths": unchanged,
              "changed_or_new_since_prior": sorted(set(owned) - set(unchanged)),
              "complete_binary_patch": str(patch), "complete_binary_patch_sha256": sha(patch),
              "complete_binary_patch_bytes": patch.stat().st_size,
              "current_validation": str(validation.relative_to(ROOT)),
              "current_validation_sha256": sha(validation),
              "review_harness_files": str((OUT / "review-harness-files.v002.json").relative_to(ROOT)),
              "review_harness_files_sha256": sha(OUT / "review-harness-files.v002.json"),
              "supersedes_rejected_scope_sha256": "65da97be5c6aa8142365944535d2280aafdd961fd2aa813622ce345d8c69defb",
              "rejected_scope_preserved": "docs/evidence/project-memory-quality-2026-10-21-r22-validation/before-checkpoint-contract-fix/review-source-manifest.json",
              "revision_scope": "Only two existing auditor paths changed. All other existing owned pins remain exact. "
              "Product and helper remain the same bytes as the passing core checks. New preserved predecessors, "
              "six-counter controls, review findings and contents-free normalized metadata are included."}
    destination.write_text(json.dumps(result, indent=2) + "\n")
    assert all(sha(ROOT / name) == pin for name, pin in pins.items())
    print(json.dumps({"owned_files": len(owned), "unchanged_prior": len(unchanged),
                      "new_or_changed": len(result["changed_or_new_since_prior"]),
                      "manifest_sha256": sha(destination), "patch_sha256": sha(patch),
                      "patch_bytes": patch.stat().st_size}))


if __name__ == "__main__":
    main()
