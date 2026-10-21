"""Replay disclosed development responses through baseline validators without writes/calls."""
from __future__ import annotations
import argparse
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
from types import SimpleNamespace


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run', type=Path, default=Path('/tmp/codex-mem-consolidation-retention-experiment'))
    parser.add_argument('--baseline', type=Path, default=Path('/tmp/codex-mem-retention-baseline'))
    args = parser.parse_args()
    sys.dont_write_bytecode = True
    sys.path.insert(0, str(args.baseline.resolve()))
    import jsonschema
    from codex_mem import episode_processor as processor
    from codex_mem.project_memory import ProjectMemory
    results = []
    # Explicit development-only allowlist. Never discover newer/holdout receipts.
    for index in range(1, 6):
        request_path = args.run / f'receipts/call-{index:04d}-request.json'
        response_path = args.run / f'receipts/call-{index:04d}-response.json'
        request = json.loads(request_path.read_text())
        response = json.loads(response_path.read_text())
        disclosed = json.loads(request['prompt'].split('\n', 1)[1])
        history = f'fiction-{index:02d}' if index < 5 else 'fiction-01'
        home = args.run if index < 5 else args.run / 'replicates/regeneration-1'
        connection = sqlite3.connect(f'file:{home}/baseline/{history}/data/memory.sqlite3?mode=ro', uri=True)
        try:
            jsonschema.validate(response['output'], request['output_schema'])
            snapshot = {
                'project': disclosed['project'], 'session_id': disclosed['session_id'],
                'sources': [dict(event, body=''.join(span['text'] for span in event['spans']))
                            for event in disclosed['events']],
                'state': disclosed['current_state'], 'job_id': 'offline-diagnosis', 'revision': 0,
            }
            output = processor._resolve_object_references(response['output'], snapshot)
            output = processor._resolve_evidence_refs(output, snapshot)
            output = processor._resolve_quotes(output, snapshot)
            output, _ = processor._omit_unsupported_optional_fields(output)
            processor._validate_output(output, snapshot)
            outcome = 'accepted'
            try:
                ProjectMemory(SimpleNamespace(_connection=connection))._validate(
                    snapshot, output, conservative_transitions=True)
            except ValueError as exc:
                if str(exc) != 'claim worktree must match project':
                    raise
                outcome = 'ValueError: claim worktree must match project'
            results.append({
                'attempt_alias': f'development-attempt-{index}', 'history_id': history,
                'replicate': 'primary' if index < 5 else 'regeneration-1',
                'request': {'path': str(request_path), 'sha256': hashlib.sha256(request_path.read_bytes()).hexdigest()},
                'response': {'path': str(response_path), 'sha256': hashlib.sha256(response_path.read_bytes()).hexdigest()},
                'requested_json_schema': 'pass', 'object_and_evidence_ref_resolution': 'pass',
                'processor_canonical_validation': 'pass', 'commit_validator_result': outcome,
                'all_claim_worktrees_equal_project_basename': all(
                    claim['scope']['worktree'] == Path(snapshot['project']).name for claim in output['claims']),
                'all_claim_worktrees_differ_from_canonical_project': all(
                    claim['scope']['worktree'] != snapshot['project'] for claim in output['claims']),
            })
        finally:
            connection.close()
    print(json.dumps({'attempts': results}, indent=2))


if __name__ == '__main__':
    main()
