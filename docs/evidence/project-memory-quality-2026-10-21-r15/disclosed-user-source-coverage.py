"""Ledger disclosed original-user visibility; no relevance or validity judgments."""
from __future__ import annotations
import argparse
from collections import Counter
import hashlib
import importlib.util
import json
from pathlib import Path
import sqlite3
import sys

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

def read(path):
    return json.loads(path.read_text())

def digest_file(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()

def module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    value = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(value)
    return value

def intervals(catalog, source_id, body):
    """Only canonical body spans of this exact source count for user coverage."""
    result = []
    for span in catalog:
        if span['source_id'] != source_id or span['source_field'] != 'body':
            continue
        start, end = span['start'], span['end']
        if span['source_role'] != 'user_intent' or body[start:end] != span['quote']:
            raise ValueError('user quotation differs from original field/role')
        result.append((start, end))
    merged = []
    for start, end in sorted(set(result)):
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    return merged

def main(args):
    helper = module('coverage_helper', ROOT / 'scripts/project_memory_consolidation_experiment.py')
    helper_hash = digest_file(Path(helper.__file__))
    fixture_path, gold_path = args.sealed / 'holdout-fixture.json', args.sealed / 'holdout-gold.json'
    fixture, gold = read(fixture_path), read(gold_path)['questions']
    contexts = {item['case']: item for item in read(args.contexts)}
    replay = read(args.contexts.parent / 'result.json')
    manifest_path = args.contexts.parent.parent / 'v6-source-receipt/product-manifest.json'
    if args.contexts.parent.name == 'disclosed-replay-v6' and args.contexts.parent.parent.name == 'project-memory-quality-2026-10-21-r14':
        if read(manifest_path)['files'] != replay['product_hashes']:
            raise ValueError('V6 replay differs from archived source receipt')
    if helper_hash != replay['helper_sha256'] or replay['render_seam'] != 'build_project_brief then render_project_brief, matching frozen helper':
        raise ValueError('helper or saved double-render seam changed')
    rows, checks, counts = [], [], Counter()
    selected_cases = {'hold_1002/q3', 'hold_1003/q4', 'hold_1006/q2', 'hold_1010/q6', 'hold_1016/q1', 'hold_1007/q3'}
    for history in fixture['histories']:
        directory = args.run / 'candidate' / history['id']
        generation = read(directory / 'generation.json')
        originals = helper.original_source_fields(history, generation['source_map'])
        delivered = {item['id']: item for item in read(directory / 'contexts.json')}
        deleted_events = {event['observation_event_id'] for session in history['sessions']
                          for event in session['events'] if event['type'] == 'delete_observation'}
        sources = []
        connection = sqlite3.connect(f'file:{directory / "data/memory.sqlite3"}?mode=ro', uri=True)
        connection.row_factory = sqlite3.Row
        connection.execute('PRAGMA query_only=ON')
        try:
            for session in history['sessions']:
                for event in session['events']:
                    if event['type'] != 'user':
                        continue
                    source_id = generation['source_map'][event['id']]
                    stored = connection.execute('SELECT e.id,e.project,e.session_id,e.body,e.superseded_by,v.seq FROM entries e LEFT JOIN pm_events v ON v.source_id=e.id AND v.project=e.project WHERE e.id=?', (source_id,)).fetchone()
                    body = originals[source_id]['fields']['body']
                    if stored and stored['body'] != body:
                        raise ValueError('stored original user body changed')
                    sources.append({'event_id': event['id'], 'source_id': source_id,
                        'source_field': 'body', 'source_role': 'user_intent', 'start': 0, 'end': len(body),
                        'body': body, 'project_key': session['project_key'],
                        'canonical_project': generation['projects'][session['project_key']],
                        'fixture_session_id': session['id'], 'stored_session_id': stored['session_id'] if stored else None,
                        'source_capture_seq': stored['seq'] if stored else None,
                        'source_present_in_store': bool(stored), 'deleted_by_history': event['id'] in deleted_events,
                        'superseded_by': stored['superseded_by'] if stored else None})
        finally:
            connection.close()
        for question in history['questions']:
            case = history['id'] + '/' + question['id']
            saved = contexts[case]
            original_catalog = helper.reader_citation_catalog([delivered[question['id']]], originals)
            receipt = read(directory / ('reader-0.q-' + question['id'] + '.json'))
            catalog = helper.reader_citation_catalog([saved['new_context']], originals)
            if (helper.digest(delivered[question['id']]) != receipt['context_sha256'] or
                    helper.digest(original_catalog) != receipt['catalog_sha256'] or
                    helper.digest(catalog) != saved['new_catalog_sha256'] or catalog != saved['new_catalog']):
                raise ValueError('own-question catalog/receipt binding differs')
            keys = question.get('project_keys', [question['project_key']] if 'project_key' in question else list(generation['projects']))
            for source in sources:
                scoped = source['project_key'] in keys
                eligible = scoped and source['source_present_in_store'] and not source['deleted_by_history'] and not source['superseded_by']
                visible = intervals(catalog, source['source_id'], source['body'])
                covered = sum(end - start for start, end in visible)
                status = 'complete' if visible == [[0, source['end']]] else 'partial' if visible else 'missing'
                gold_facts = [index for index, fact in enumerate(gold[case]['required_facts'])
                              if any(span['event_id'] == source['event_id'] for span in fact['support'])]
                row = {'case': case, 'question': question['question'], 'selected_project_keys': keys,
                    **source, 'selected_project_scope': scoped, 'capture_deletion_eligible': bool(eligible),
                    'visible_body_intervals': visible, 'visible_body_chars': covered, 'visibility': status,
                    'required_gold_fact_indices': gold_facts,
                    'coverage_kind': 'required_fact_source' if gold_facts else 'extra_original_observation',
                    'context_sha256': saved['new_context']['context_sha256'], 'catalog_sha256': saved['new_catalog_sha256']}
                rows.append(row)
                if eligible:
                    counts[status] += 1
                    counts['eligible_source_question_pairs'] += 1
                if case in selected_cases and scoped:
                    checks.append(row)
    if len(contexts) != 96 or len(gold) != 96:
        raise ValueError('disclosed question count changed')
    known = [row for row in rows if row['case'] == 'hold_1010/q6' and row['event_id'] in {'hold_1010_follow', 'hold_1010_tail'}]
    if {row['event_id']: row['end'] for row in known} != {'hold_1010_follow': 107, 'hold_1010_tail': 115}:
        raise ValueError('public transition source spans changed')
    result = {'status': 'disclosed_original_user_source_visibility_only', 'questions': 96,
        'positive_gold_facts_unchanged': sum(len(value['required_facts']) for value in gold.values()),
        'counts': dict(counts), 'source_question_pairs': rows, 'fixed_six_all_selected_project_user_spans': checks,
        'explicit_public_1010_transition_checks': known, 'models_called': 0, 'grades_changed': 0,
        'saved_replay_double_render_receipt_verified': True, 'own_question_catalog_receipts_verified': True,
        'hashes': {'fixture': digest_file(fixture_path), 'gold': digest_file(gold_path),
            'contexts': digest_file(args.contexts), 'replay_result': digest_file(args.contexts.parent / 'result.json'),
            'helper': helper_hash, 'script': digest_file(Path(__file__))},
        'product_hashes_for_saved_views': replay['product_hashes'],
        'limits': ['Visibility is not semantic relevance, task association, validity, or graded answer quality.',
            'Capture/deletion eligibility records selected project, source existence and supersession only; no requested session exclusion exists in these question calls.',
            'Cross-project user sources are enumerated but are not expected to be visible.',
            'Extra original observations are distinct from the unchanged required positive facts; missing irrelevant text is not called a failure.',
            'No current runtime replay is executed here; saved V6 double-render views and exact own-question catalogs are verified.']}
    if args.output.exists():
        raise ValueError('output already exists; use a new receipt')
    args.output.write_text(json.dumps(result, indent=2) + '\n')
    print(json.dumps({'counts': result['counts'], 'known_transition_checks': [(r['event_id'], r['visibility']) for r in known], 'output_sha256': digest_file(args.output)}))

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run', type=Path, default=Path('/private/tmp/codex-mem-quality-20261021-r13-experiment'))
    parser.add_argument('--sealed', type=Path, default=Path('/private/tmp/codex-mem-quality-20261021-r13-sealed'))
    parser.add_argument('--contexts', type=Path, default=ROOT / 'docs/evidence/project-memory-quality-2026-10-21-r14/disclosed-replay-v6/new-contexts.json')
    parser.add_argument('--output', type=Path, required=True)
    main(parser.parse_args())
