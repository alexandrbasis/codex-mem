"""Pin the complete owned working change set without staging any files."""
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import subprocess


ROOT = Path(__file__).resolve().parents[3]
OUT = ROOT / 'docs/evidence/project-memory-quality-2026-10-21-r20'
BASE = '2ca7445139805243c1d30a1ff7fd15be2b2dd85a'
PRIOR = ROOT / 'docs/evidence/project-memory-quality-2026-10-21-r19/review-source-manifest.json'


def sha(path):
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def read(path):
    return json.loads(path.read_text())


def write_new(path, value):
    with path.open('x') as stream:
        stream.write(json.dumps(value, indent=2) + '\n')


def git_paths(*arguments):
    return set(filter(None, subprocess.check_output(['git', *arguments], cwd=ROOT).decode().split('\0')))


def main():
    prior = read(PRIOR)
    release_path = OUT / 'public-harness-source-release.json'
    release = read(release_path)
    harness = {str((OUT / key).relative_to(ROOT)): value for key, value in release['files'].items()}
    harness.update(release['core_files'])
    for relative, pin in harness.items():
        assert sha(ROOT / relative) == pin, relative
    harness_path = OUT / 'review-harness-files.json'
    write_new(harness_path, harness)
    tracked = git_paths('diff', '--name-only', BASE, '-z')
    untracked = git_paths('ls-files', '--others', '--exclude-standard', '-z')
    foreign = {'AGENTS.md', 'docs/agents/installation-checks.json'}
    excluded = {str((OUT / name).relative_to(ROOT)) for name in (
        'central-public-readiness.json', 'review-source-manifest.json',
        'review-spec-harness.json', 'review-spec-source.json',
        'review-standards-harness.json', 'review-standards-source.json')}
    owned = sorted(path for path in tracked | untracked
                   if path not in foreign | excluded and not path.startswith('docs/handoffs/'))
    for relative in owned:
        assert (ROOT / relative).is_file() and not (ROOT / relative).is_symlink(), relative
    pins = {relative: sha(ROOT / relative) for relative in owned}
    unchanged = [path for path in owned if prior['owned_file_sha256'].get(path) == pins[path]]
    changed = [path for path in owned if path not in set(unchanged)]
    patch = Path('/private/tmp/codex-mem-quality-20261021-r20-complete-source.patch')
    with patch.open('xb') as stream:
        subprocess.run(['git', 'diff', '--binary', BASE, '--', *sorted(tracked & set(owned))],
                       cwd=ROOT, stdout=stream, check=True)
        for relative in sorted(untracked & set(owned)):
            result = subprocess.run(['git', 'diff', '--no-index', '--binary', '--', '/dev/null', relative],
                                    cwd=ROOT, stdout=stream)
            assert result.returncode in (0, 1), relative
    runtime = read(OUT / 'runtime-source-receipt.json')['runtime_files']
    common = ('mode', 'base_sha', 'requirements_source', 'requirements_sha256',
              'preserved_user_exclusions', 'excluded_user_prefixes', 'standards_sources', 'commands')
    manifest = {key: prior[key] for key in common}
    manifest.update(
        created_at_utc=datetime.now(timezone.utc).isoformat(),
        checkpoint_commit=subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT).decode().strip(),
        requirements_scope='R20 source readiness: descriptive compound admission, canonical cumulative '
        'accounting selection, unchanged model requests, and public harness. Fresh quality, final '
        'consolidated user Markdown review and local commit remain open.',
        owned_paths=owned, owned_file_sha256=pins,
        owned_total_bytes=sum((ROOT / path).stat().st_size for path in owned),
        tracked_owned_paths=sorted(tracked & set(owned)), untracked_owned_paths=sorted(untracked & set(owned)),
        self_and_reviewer_output_exclusions=sorted(excluded),
        standards_source_sha256={path: sha(ROOT / path) for path in prior['standards_sources']},
        prior_review_manifest=str(PRIOR.relative_to(ROOT)), prior_review_manifest_sha256=sha(PRIOR),
        prior_review_policy='Exact hash-unchanged files can carry prior independent review. Verify all '
        'pins and inspect new/changed source and evidence. Source clearance is not quality completion.',
        exact_unchanged_prior_paths=unchanged, changed_or_new_since_prior=changed, runtime_files=runtime,
        complete_binary_patch=str(patch), complete_binary_patch_sha256=sha(patch),
        complete_binary_patch_bytes=patch.stat().st_size,
        commit_list=subprocess.check_output(['git', 'log', BASE + '..HEAD', '--oneline'], cwd=ROOT).decode().splitlines(),
        r20_native_calls=0, actual_protocol_generated=False, native_clock_started=False,
        actual_freeze_exists=False, formal_release_exists=False, sealed_inputs_parent_opened=False,
        limits=['Source-only review. R19 complete FAIL remains unchanged; R20 quality is unmeasured.',
                'Private R20 fixture/gold/rationale/full findings remain parent-blind and outside this manifest.',
                'The known causal inference risk remains. Query repair is not a semantic support filter.',
                'Metadata attestations do not prove isolation or external truth.',
                'Foreign AGENTS/installation checks/handoffs remain outside commit ownership.'])
    for field, path in (
        ('current_validation', OUT / 'test-evidence-original.json'),
        ('held_runtime_receipt', OUT / 'runtime-source-receipt.json'),
        ('public_harness_source_release', release_path),
        ('harness_files_manifest', harness_path)):
        manifest[field] = str(path.relative_to(ROOT))
        manifest[field + '_sha256'] = sha(path)
    assert sha(ROOT / manifest['requirements_source']) == manifest['requirements_sha256']
    target = OUT / 'review-source-manifest.json'
    write_new(target, manifest)
    print(json.dumps({'manifest_sha256': sha(target), 'owned_files': len(owned),
                      'carry': len(unchanged), 'new_or_changed': len(changed),
                      'harness_files': len(harness), 'patch_bytes': patch.stat().st_size,
                      'complete_patch_sha256': manifest['complete_binary_patch_sha256']}))


if __name__ == '__main__':
    main()
