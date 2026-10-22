#!/usr/bin/env python3
"""Prepare/verify one public producer probe; this module never dispatches a model.

After selecting the pinned runtime on sys.path, the parent calls:
    plan = prepare(fresh_temporary_path)
    result = episode_processor.process_project_memory(
        plan['project'], plan['data'], runner=parent_capture, timeout=180)
    receipt = verify(plan, result, exact_response_retained_by_parent_capture,
        native_request=exact_request_retained_by_parent_capture)
Call the processor once only. Parent Capture owns raw receipts, budgets and clocks.
"""
import hashlib
import json
from pathlib import Path

PRIOR_TEXT = 'Task ORBIT report format: XML.'
CURRENT_TEXT = 'Replace task ORBIT report format: XML with JSON.'
SEED_SESSION = 'public-producer-seed'
CURRENT_SESSION = 'public-producer-current'


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def identity(claim):
    """Stable seeded identity, excluding projected transition display fields."""
    fields = ('id', 'key', 'object_type', 'text', 'kind', 'scope', 'project', 'session_id',
              'revision', 'created_at', 'evidence', 'base_workflow_state')
    return {key: claim.get(key) for key in fields}


def prepare(root):
    """Create a fresh fictional Store, seed canonical prior, queue later user event.

    The seed is a trusted fixture commit, never a native output or grade. The
    pending later event is left unclaimed for the parent's one processor call.
    Imports are delayed so the caller can first select its pinned runtime.
    """
    from codex_mem.config import configure
    from codex_mem.project_memory import ProjectMemory
    from codex_mem.store import Store
    root = Path(root).absolute()
    if root.exists() or root.is_symlink():
        raise ValueError('producer probe requires a fresh root; never resume or overwrite')
    root = root.resolve()
    if not str(root).startswith(('/private/tmp/', '/tmp/')):
        raise ValueError('producer probe requires an isolated temporary root')
    root.mkdir(parents=True, exist_ok=False)
    project, data = root / 'project', root / 'data'
    project.mkdir()
    configure(data, capture_scope='all', project_memory_mode='shadow',
              project_memory_projects=[str(project)], jev_quality_enabled=False)
    with Store(data) as store:
        prior = store.remember(project, 'Public seeded requirement', PRIOR_TEXT,
                               source='hook:UserPromptSubmit', session_id=SEED_SESSION)
        memory = ProjectMemory(store)
        snapshot = memory.claim(project)
        span = {'source_id': prior['id'], 'start': 0, 'end': len(PRIOR_TEXT), 'quote': PRIOR_TEXT}
        seed = {'key': 'seeded_prior', 'text': PRIOR_TEXT, 'kind': 'requirement',
                'workflow_state': 'open', 'scope': {'environment': '', 'version': '', 'worktree': str(project)},
                'rationale': '', 'evidence': {'text': [span], 'workflow_state': [span], 'rationale': []}}
        committed = memory.commit(snapshot, {'episodes': [], 'claims': [seed], 'relations': []},
                                  receipt={'processor_id': 'public-producer-fixture-seed',
                                           'output_schema_version': 'canonical-fixture-v1', 'model_calls': 0})
        if committed['status'] != 'committed' or len(committed['object_ids']) != 1:
            raise ValueError('fixture seed was not committed')
        seeded = memory.get(project, committed['object_ids'])[0]
        current = store.remember(project, 'Public replacement instruction', CURRENT_TEXT,
                                 source='hook:UserPromptSubmit', session_id=CURRENT_SESSION)
        baseline = memory.snapshot(project)
    plan = {'version': 'public-producer-probe-v1', 'fictional': True, 'root': str(root),
            'project': str(project), 'data': str(data), 'seed_claim_id': seeded['id'],
            'seed_claim_identity': identity(seeded), 'seed_commit': committed,
            'seed_is_fixture_not_native_output': True, 'sources': {
                prior['id']: {'body': PRIOR_TEXT, 'session_id': SEED_SESSION},
                current['id']: {'body': CURRENT_TEXT, 'session_id': CURRENT_SESSION}},
            'current_source_id': current['id'], 'baseline_claim_ids': [c['id'] for c in baseline['claims']],
            'baseline_episode_ids': [e['id'] for e in baseline['episodes']],
            'baseline_relation_ids': [r['id'] for r in baseline['relations']],
            'processor_call': {'function': 'codex_mem.episode_processor.process_project_memory',
                'project': str(project), 'data_dir': str(data), 'timeout': 180,
                'runner': 'parent-owned Capture', 'max_invocations': 1},
            'gate': {'allowed_relation_types': ['replaces', 'contradicts'],
                     'required_native_schema_version': 'project-memory-output-v12'},
            'limits': 'Structural/provenance regression only; semantic truth is not established.'}
    plan['manifest_sha256'] = digest(plan)
    (root / 'preparation.json').write_text(json.dumps(plan, indent=2) + '\n')
    return plan


def verify(plan, processor_receipt, native_response, *, native_request):
    """Require a new canonical edge from its actual native owning claim.

    Refuses failed/partial processing, empty reconciliation, mixed or legacy
    layouts and preexisting/unrelated edges. This does not adjudicate semantics.
    """
    from codex_mem.project_memory import ProjectMemory
    from codex_mem.store import Store
    if json.loads((Path(plan['root']) / 'preparation.json').read_text()) != plan:
        raise ValueError('producer probe preparation changed')
    unsigned = {key: value for key, value in plan.items() if key != 'manifest_sha256'}
    if digest(unsigned) != plan['manifest_sha256']:
        raise ValueError('producer probe preparation digest changed')
    with Store(plan['data']) as store:
        memory = ProjectMemory(store)
        state = memory.snapshot(plan['project'])
        seeded = memory.get(plan['project'], [plan['seed_claim_id']])
        if len(seeded) != 1 or identity(seeded[0]) != plan['seed_claim_identity']:
            raise ValueError('seeded canonical identity changed')
        for source_id, expected in plan['sources'].items():
            source = store.get(plan['project'], source_id)
            if (len(source) != 1 or source[0]['body'] != expected['body']
                    or source[0]['session_id'] != expected['session_id']
                    or source[0]['source'] != 'hook:UserPromptSubmit'):
                raise ValueError('captured source provenance changed')
    new_claims = [c for c in state['claims'] if c['id'] not in plan['baseline_claim_ids']]
    new_episodes = [e for e in state['episodes'] if e['id'] not in plan['baseline_episode_ids']]
    new_relations = [r for r in state['relations'] if r['id'] not in plan['baseline_relation_ids']]
    if processor_receipt.get('status') != 'processed':
        if new_claims or new_episodes or new_relations:
            raise ValueError('failed processing left partial canonical records')
        raise ValueError('producer did not process the probe')
    if (processor_receipt.get('output_schema_version') != plan['gate']['required_native_schema_version']
            or processor_receipt.get('commit_status') != 'committed'):
        raise ValueError('producer version or commit receipt invalid')
    if (native_request.get('output_schema_version') != plan['gate']['required_native_schema_version']
            or not isinstance(native_request.get('prompt'), str)):
        raise ValueError('exact native v12 request required')
    payload = json.loads(native_request['prompt'].split('\n', 1)[1])
    if payload.get('project') != plan['project'] or payload.get('session_id') != CURRENT_SESSION:
        raise ValueError('native request scope differs from prepared probe')
    refs = {}
    for event in payload['events']:
        source = plan['sources'].get(event['id'])
        offset = event['body_offset']
        if not source or event['source_role'] != 'user_intent':
            raise ValueError('request source provenance differs from prepared probe')
        for span in event['spans']:
            end = offset + len(span['text'])
            if span['ref'] in refs or source['body'][offset:end] != span['text']:
                raise ValueError('request span differs from original captured source')
            refs[span['ref']] = {'source_id': event['id'], 'start': offset, 'end': end,
                                 'quote': span['text'], 'source_role': event['source_role']}
            offset = end
    output = native_response.get('output', {})
    if (not isinstance(output, dict) or set(output) != {'episodes', 'claims'}
            or not isinstance(output['claims'], list) or not isinstance(output['episodes'], list)):
        raise ValueError('native v12 layout required; no mixed or indexed relations')
    if not output['claims'] or len(new_claims) != len(output['claims']):
        raise ValueError('native claims do not match committed new claims')
    owners = {}
    for index, raw in enumerate(output['claims']):
        if (not isinstance(raw, dict) or 'key' in raw or 'from_claim_index' in raw
                or not isinstance(raw.get('outgoing_relations'), list)):
            raise ValueError('native claim ownership invalid')
        matches = [c for c in new_claims if c['key'] == f'claim_{index}' and c['text'] == raw.get('text')
                   and c['session_id'] == CURRENT_SESSION and c['scope'] == plan['seed_claim_identity']['scope']]
        if len(matches) != 1:
            raise ValueError('native owning claim does not match canonical claim')
        owners[index] = matches[0]
    accepted = []
    for index, raw in enumerate(output['claims']):
        owner = owners[index]
        for relation in raw['outgoing_relations']:
            if (not isinstance(relation, dict) or set(relation) != {'type', 'to_claim_id', 'evidence'}
                    or relation['type'] not in plan['gate']['allowed_relation_types']
                    or relation['to_claim_id'] != plan['seed_claim_id']):
                raise ValueError('native outgoing relation does not target the seeded prior')
            matches = [r for r in new_relations if r['from'] == owner['id']
                       and r['to'] == plan['seed_claim_id'] and r['type'] == relation['type']]
            if len(matches) != 1:
                raise ValueError('owner-derived canonical relation missing')
            edge = matches[0]
            if (not isinstance(relation['evidence'], list) or not relation['evidence']
                    or any(not isinstance(ref, str) or ref not in refs for ref in relation['evidence'])):
                raise ValueError('native outgoing evidence ref is not in the actual request')
            expected_spans = [refs[ref] for ref in relation['evidence']]
            actual_spans = [{key: span.get(key) for key in expected_spans[0]} for span in edge['evidence']]
            if expected_spans != actual_spans:
                raise ValueError('raw native evidence differs from canonical relation evidence')
            for span in edge['evidence']:
                source = plan['sources'].get(span['source_id'])
                if (not source or span['source_id'] != plan['current_source_id']
                        or span.get('source_role') != 'user_intent'
                        or not 0 <= span['start'] < span['end'] <= len(source['body'])
                        or source['body'][span['start']:span['end']] != span['quote']):
                    raise ValueError('canonical relation evidence is not exact current user source')
            if not edge['evidence']:
                raise ValueError('canonical relation evidence missing')
            accepted.append(edge['id'])
    if not accepted or set(accepted) != {r['id'] for r in new_relations}:
        raise ValueError('probe requires an owner-derived replacement or contradiction edge')
    return {'status': 'passed', 'probe_version': plan['version'], 'manifest_sha256': plan['manifest_sha256'],
            'seed_fixture_claim_id': plan['seed_claim_id'], 'native_new_claim_ids': [c['id'] for c in new_claims],
            'native_relation_ids': accepted, 'seed_is_fixture_not_native_output': True,
            'semantic_truth_established': False, 'model_calls_by_verifier': 0}
