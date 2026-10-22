"""Inventory owned working changes without reading the new sealed cases."""
from __future__ import annotations

import argparse
import datetime
import hashlib
import json
from pathlib import Path
import subprocess


REPO = Path(__file__).resolve().parents[3]
BASE = '2ca7445139805243c1d30a1ff7fd15be2b2dd85a'
PREFIX = 'docs/evidence/project-memory-quality-2026-10-21-r23/'
PRIOR = 'docs/evidence/project-memory-quality-2026-10-21-r22/'
EXCLUDED = {'AGENTS.md', 'docs/agents/installation-checks.json'}


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def read(name: str):
    return json.loads((REPO / name).read_text())


def git(*args: str) -> bytes:
    return subprocess.run(['git', *args], cwd=REPO, check=True,
                          stdout=subprocess.PIPE).stdout


def paths(raw: bytes) -> set[str]:
    return {part.decode() for part in raw.split(b'\0') if part}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--version', required=True)
    args = parser.parse_args()
    assert args.version.isalnum(), 'invalid version'
    destination = REPO / PREFIX
    manifest_path = destination / 'review-source-manifest.json'
    harness_path = destination / ('review-harness-files.' + args.version + '.json')
    delta_path = destination / ('review-source-delta.' + args.version + '.patch')
    assert not any(p.exists() for p in (manifest_path, harness_path, delta_path)), 'output exists'
    outputs = {str(p.relative_to(REPO)) for p in (manifest_path, harness_path, delta_path)}
    outputs.add(PREFIX + 'source-inventory.' + args.version + '.log')
    prior_path = PRIOR + 'review-source-manifest.json'
    addendum_path = PRIOR + 'source-readiness-addendum.json'
    prior = read(prior_path)
    addendum = read(addendum_path)
    held = dict(prior['owned_file_sha256'])
    for name, pin in addendum['new_file_sha256'].items():
        assert name not in held or held[name] == pin, name
        held[name] = pin
    drift = [name for name, pin in held.items() if sha(REPO / name) != pin]
    assert not drift, 'previous reviewed source changed: ' + repr(drift)
    tracked = paths(git('diff', '--name-only', '-z', BASE))
    untracked = paths(git('ls-files', '--others', '--exclude-standard', '-z'))
    owned = sorted(name for name in tracked | untracked
                   if name not in EXCLUDED | outputs and not name.startswith('docs/handoffs/')
                   and '__pycache__' not in Path(name).parts and (REPO / name).is_file())
    assert set(held) <= set(owned), 'previous owned file omitted'
    assert not any((REPO / name).is_symlink() for name in owned), 'owned symlink'
    pins = {name: sha(REPO / name) for name in owned}
    changed = sorted(name for name in owned if held.get(name) != pins[name])
    assert not (set(changed) & set(held)), 'held source mutation requires before bytes'
    with delta_path.open('xb') as stream:
        for name in changed:
            result = subprocess.run(['git', 'diff', '--no-index', '--binary', '--', '/dev/null', name],
                                    cwd=REPO, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            assert result.returncode in (0, 1), (name, result.stderr)
            stream.write(result.stdout)
    harness = {name: pins[name] for name in owned
               if name.startswith(PREFIX) or name.startswith('docs/evidence/project-memory-quality-2026-10-21-r23-audit/')
               or name.startswith('docs/evidence/project-memory-quality-2026-10-21-r23-preparation/')}
    for name in ('scripts/project_memory_consolidation_experiment.py',
                 'docs/evidence/project-memory-quality-2026-10-21-r22/frozen-helper.py'):
        harness[name] = sha(REPO / name)
    harness_path.write_text(json.dumps(harness, indent=2) + '\n')
    runtime = {str(p.relative_to(REPO)): sha(p) for p in sorted((REPO / 'codex_mem').glob('*.py'))}
    standards = ['AGENTS.md', 'CODING_STANDARDS.md', 'docs/agents/engineering-skills.md',
                 'docs/VERIFICATION.md', '.agents/skills/code-review/SKILL.md']
    result = {
        'mode': 'complete_owned_working_changes_with_verified_previous_reviews',
        'base_sha': BASE,
        'checkpoint_commit': git('rev-parse', 'HEAD').decode().strip(),
        'created_at_utc': datetime.datetime.now(datetime.timezone.utc).isoformat(),
        'requirements_source': 'docs/handoffs/project-memory-quality-2026-10-21.md',
        'requirements_sha256': sha(REPO / 'docs/handoffs/project-memory-quality-2026-10-21.md'),
        'preserved_user_exclusions': sorted(EXCLUDED), 'excluded_user_prefixes': ['docs/handoffs/'],
        'standards_sources': standards, 'standards_source_sha256': {n: sha(REPO / n) for n in standards},
        'owned_paths': owned, 'owned_file_sha256': pins,
        'owned_total_bytes': sum((REPO / n).stat().st_size for n in owned),
        'tracked_owned_paths': sorted(set(owned) & tracked),
        'untracked_owned_paths': sorted(set(owned) & untracked),
        'runtime_files': runtime,
        'helper_sha256': sha(REPO / 'scripts/project_memory_consolidation_experiment.py'),
        'prior_review_manifest': {'path': prior_path, 'sha256': sha(REPO / prior_path)},
        'prior_readiness_addendum': {'path': addendum_path, 'sha256': sha(REPO / addendum_path)},
        'previous_pins_reverified': len(held), 'previous_pin_drift': drift,
        'prior_review_policy': 'Held previous source and readiness bytes are unchanged. Prior source clearance remains scoped to its exact pins. Review the complete current inventory and new delta.',
        'exact_unchanged_prior_paths': sorted(held), 'changed_or_new_since_prior': changed,
        'complete_prior_binary_patch': {'path': prior['complete_binary_patch'], 'sha256': prior['complete_binary_patch_sha256']},
        'complete_prior_addendum_patch': {'path': addendum['complete_addendum_patch'], 'sha256': addendum['complete_addendum_patch_sha256']},
        'new_binary_delta': {'path': str(delta_path.relative_to(REPO)), 'sha256': sha(delta_path), 'bytes': delta_path.stat().st_size},
        'review_harness_files': str(harness_path.relative_to(REPO)), 'review_harness_files_sha256': sha(harness_path),
        'requirements_scope': 'R23 prospective harness paths, canonical R22 history, actual author metadata counts, six-counter auditor contract, exact authorization and unchanged product/runtime. New quality remains unmeasured.',
        'native_calls': 0, 'actual_protocol_generated': False, 'native_clock_started': False,
        'actual_freeze_exists': False, 'formal_release_exists': False, 'private_new_case_content_opened': False,
        'self_and_reviewer_output_exclusions': sorted(outputs),
        'later_output_policy': 'Subsequent review/readiness artifacts are outside this preexisting inventory. Later bindings must name them explicitly.',
        'limits': ['No new sealed case, gold, rationale or full semantic review was parsed.',
                   'Source readiness is separate from native public validation, fresh quality, final human review and commit.'],
    }
    for reference in (result['complete_prior_binary_patch'], result['complete_prior_addendum_patch']):
        assert sha(Path(reference['path'])) == reference['sha256'], reference
    for name, pin in pins.items():
        assert sha(REPO / name) == pin, 'source changed during inventory: ' + name
    manifest_path.write_text(json.dumps(result, indent=2) + '\n')
    print(json.dumps({'manifest_sha256': sha(manifest_path), 'harness_sha256': sha(harness_path),
                      'owned_files': len(owned), 'previous_pins_verified': len(held),
                      'new_files': len(changed), 'delta_bytes': delta_path.stat().st_size}))


if __name__ == '__main__':
    main()
