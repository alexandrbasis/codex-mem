"""Offline exact-source visibility of saved R16 views. Never reads answers/grades."""
from __future__ import annotations
import argparse
from collections import Counter
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
from pathlib import Path
import sys
from xml.etree import ElementTree as ET

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

def read(path):
    return json.loads(path.read_text())

def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()

def covered(catalog, source_id, field, start, end):
    cursor = start
    for lower, upper in sorted((span['start'], span['end']) for span in catalog
            if span['source_id'] == source_id and span['source_field'] == field):
        if lower > cursor:
            break
        cursor = max(cursor, upper)
    return cursor >= end

def exact_visibility(catalog, source_id, support, originals):
    """No paraphrase, cross-source substitute, or shortened required quote."""
    if source_id not in originals:
        return {'original_field_exists': False, 'anchor_exact': False,
                'strict_body_visible': False, 'exact_field_mirror_visible': False, 'mirrors': []}
    fields = originals[source_id]['fields']
    field, start, end, quote = support.get('source_field', 'body'), support['start'], support['end'], support['quote']
    exists = field in fields
    valid = bool(exists and type(start) is int and type(end) is int and
                 0 <= start < end <= len(fields[field]) and fields[field][start:end] == quote)
    body_exact = fields.get('body', '')[start:end] == quote
    strict = valid and body_exact and covered(catalog, source_id, 'body', start, end)
    mirrors = []
    if valid:
        for name, text in fields.items():
            offset = text.find(quote)
            while offset >= 0:
                if covered(catalog, source_id, name, offset, offset + len(quote)):
                    mirrors.append({'source_field': name, 'start': offset, 'end': offset + len(quote)})
                offset = text.find(quote, offset + 1)
    return {'original_field_exists': exists, 'anchor_exact': valid,
            'strict_body_visible': bool(strict), 'exact_field_mirror_visible': bool(valid and mirrors),
            'mirrors': mirrors}

def stored_episode_links(generation, source_id):
    """Only already-exported metadata, not new Store or database operations."""
    links = []
    for episode in generation.get('persisted_state', {}).get('episodes', []):
        record = episode.get('stored_record') or {}
        matching = []
        for field, refs in (record.get('evidence') or {}).items():
            matching.extend({'field': field, **ref} for ref in refs if ref.get('source_id') == source_id)
        if matching:
            links.append({'object_id': episode['id'], 'logical_episode_id': episode.get('logical_episode_id'),
                'episode_version': episode.get('episode_version'), 'project': record.get('project'),
                'evidence': matching})
    return links

def xml_metadata(memory, source_id=None):
    coverage, occurrences = [], []
    for block in __import__('re').findall(r'<project-memory\b.*?</project-memory>', memory, __import__('re').S):
        root = ET.fromstring(block)
        coverage.extend({'tag': node.tag, **node.attrib} for node in root.iter()
                        if node.tag in ('coverage', 'omitted', 'source-context-coverage'))
        if source_id:
            for item in root.findall('item'):
                for source in item.iter('source'):
                    if source.get('source_id') == source_id:
                        occurrences.append({'item': item.attrib, 'source': source.attrib,
                            'has_quote': source.find('quote_excerpt') is not None})
    return {'coverage_markers': coverage, 'source_occurrences': occurrences}

def self_test():
    support = {'start': 0, 'end': 12, 'quote': 'Exact output'}
    originals = {'one': {'fields': {'body': 'Exact output', 'tool_response.output': 'prefix Exact output suffix'}}}
    catalog = [{'source_id': 'one', 'source_field': 'tool_response.output', 'start': 7, 'end': 19}]
    assert exact_visibility(catalog, 'one', support, originals)['exact_field_mirror_visible']
    assert not exact_visibility(catalog, 'one', support, originals)['strict_body_visible']
    assert not exact_visibility([{**catalog[0], 'source_id': 'two'}], 'one', support, originals)['exact_field_mirror_visible']
    assert not exact_visibility([{**catalog[0], 'end': 18}], 'one', support, originals)['exact_field_mirror_visible']
    assert not exact_visibility(catalog, 'one', {**support, 'end': 11}, originals)['anchor_exact']
    assert not exact_visibility(catalog, 'missing', support, originals)['anchor_exact']
    assert covered([{'source_id': 'one', 'source_field': 'body', 'start': 0, 'end': 5},
                    {'source_id': 'one', 'source_field': 'body', 'start': 5, 'end': 12}], 'one', 'body', 0, 12)
    print('Seven generic exact-span/source-identity checks passed.')

def audit(args):
    spec = importlib.util.spec_from_file_location('visibility_helper', ROOT / 'scripts/project_memory_consolidation_experiment.py')
    helper = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(helper)
    freeze = read(args.run / 'freeze.json')
    before = helper.code_hash(ROOT)
    helper_hash = sha(Path(helper.__file__))
    if before != freeze['candidate_hashes'] or len(before) != 47 or helper_hash != freeze['helper_sha256']:
        raise ValueError('current candidate/helper differ from frozen bytes')
    fixture_path, gold_path = args.released / 'holdout-fixture.json', args.released / 'holdout-gold.json'
    fixture, gold = read(fixture_path), read(gold_path)['questions']
    if len(gold) != 96 or sum(len(case['required_facts']) for case in gold.values()) != 213:
        raise ValueError('immutable R16 question/fact denominator differs')
    counts = {arm: Counter() for arm in ('baseline', 'candidate')}
    questions, facts, input_hashes = [], [], {str(path): sha(path) for path in
        (fixture_path, gold_path, args.run / 'freeze.json', args.released / 'formal-release-receipt.json')}
    for history in fixture['histories']:
        for arm in ('baseline', 'candidate'):
            directory = args.run / arm / history['id']
            generation_path, context_path = directory / 'generation.json', directory / 'contexts.json'
            generation = read(generation_path) if generation_path.exists() else None
            saved = read(context_path) if context_path.exists() else []
            by_id = {item['id']: item for item in saved}
            if len(by_id) != len(saved):
                raise ValueError('duplicate saved context ID')
            if generation:
                input_hashes[str(generation_path)] = sha(generation_path)
                originals = helper.original_source_fields(history, generation['source_map'])
            else:
                originals = {}
            if context_path.exists():
                input_hashes[str(context_path)] = sha(context_path)
            event_scope = {event['id']: {'project_key': session.get('project_key', history['project_keys'][0]),
                'session_id': session['id'], 'event_type': event['type']}
                for session in history['sessions'] for event in session['events']}
            deleted = {event['observation_event_id'] for session in history['sessions']
                       for event in session['events'] if event['type'] == 'delete_observation'}
            for question in history['questions']:
                case = history['id'] + '/' + question['id']
                context = by_id.get(question['id'])
                if context is None:
                    counts[arm]['pending_contexts'] += 1
                    counts[arm]['pending_required_facts'] += len(gold[case]['required_facts'])
                    questions.append({'arm': arm, 'case': case, 'status': 'pending_saved_view'})
                    continue
                if context['question'] != question['question']:
                    raise ValueError('saved question differs from immutable fixture')
                if hashlib.sha256(context['memory'].encode()).hexdigest() != context['context_sha256']:
                    raise ValueError('saved XML SHA differs')
                if generation is None:
                    raise ValueError('view exists without capture mapping')
                catalog = helper.reader_citation_catalog([context], originals)
                counts[arm]['completed_contexts'] += 1
                keys = question.get('project_keys', [question['project_key']] if 'project_key' in question else history['project_keys'])
                question_record = {'arm': arm, 'case': case, 'status': 'saved_view_audited',
                    'context_sha256': context['context_sha256'], 'catalog_sha256': helper.digest(catalog),
                    'catalog': catalog, 'context_chars': len(context['memory']),
                    'metadata': xml_metadata(context['memory']), 'facts': []}
                for index, fact in enumerate(gold[case]['required_facts']):
                    supports = []
                    for support in fact['support']:
                        source_id = generation['source_map'].get(support['event_id'])
                        result = exact_visibility(catalog, source_id, support, originals)
                        supports.append({**support, 'source_id': source_id, **event_scope[support['event_id']],
                            'source_mapped_at_capture': source_id is not None,
                            'deleted_by_final_history': support['event_id'] in deleted,
                            'selected_project_scope': event_scope[support['event_id']]['project_key'] in keys,
                            **result, 'existing_episode_links': stored_episode_links(generation, source_id) if source_id else [],
                            'saved_source_metadata': xml_metadata(context['memory'], source_id)['source_occurrences'] if source_id else [],
                            'gap_stage': 'visible' if result['exact_field_mirror_visible'] else
                                'capture_map_or_original_field_gap' if not result['anchor_exact'] else
                                'saved_context_gap_selection_vs_render_unresolved'})
                    strict = all(item['strict_body_visible'] for item in supports)
                    mirror = all(item['exact_field_mirror_visible'] for item in supports)
                    row = {'arm': arm, 'case': case, 'fact_index': index, 'text': fact['text'],
                        'strict_body_visible': strict, 'exact_field_mirror_visible': mirror, 'support': supports}
                    facts.append(row)
                    question_record['facts'].append(row)
                    counts[arm].update(audited_facts=1, strict_body_visible=strict, exact_field_mirror_visible=mirror,
                                       exact_field_mirror_missing=not mirror)
                questions.append(question_record)
    pending = sum(count['pending_contexts'] for count in counts.values())
    if args.final and pending:
        raise ValueError('final report requires all96 primary saved views per arm; missing views remain pending')
    if helper.code_hash(ROOT) != before or sha(Path(helper.__file__)) != helper_hash:
        raise ValueError('frozen code changed during source-only audit')
    by_fact = {(row['arm'], row['case'], row['fact_index']): row for row in facts}
    paired, losses, gains = 0, [], []
    for row in facts:
        if row['arm'] != 'candidate':
            continue
        baseline = by_fact.get(('baseline', row['case'], row['fact_index']))
        if baseline is None:
            continue
        paired += 1
        if baseline['exact_field_mirror_visible'] and not row['exact_field_mirror_visible']:
            losses.append({'baseline': baseline, 'candidate': row})
        if row['exact_field_mirror_visible'] and not baseline['exact_field_mirror_visible']:
            gains.append({'baseline': baseline, 'candidate': row})
    result = {'status': 'final_saved_source_visibility_only' if args.final else 'partial_saved_source_visibility_only',
        'snapshot_utc': datetime.now(timezone.utc).isoformat(), 'question_denominator_per_arm': 96,
        'immutable_required_fact_denominator_per_arm': 213, 'counts': {arm: dict(count) for arm, count in counts.items()},
        'paired_audited_facts': paired, 'paired_exact_mirror_losses': losses, 'paired_exact_mirror_gains': gains,
        'questions': questions, 'facts': facts, 'models_called': 0, 'answers_or_grades_read': 0,
        'store_database_or_retrieval_operations': 0, 'product_hashes': before, 'helper_sha256': helper_hash,
        'input_hashes': input_hashes, 'script_sha256': sha(Path(__file__)),
        'limits': ['Source visibility is not independent answer quality, recall, or grade success.',
            'Missing saved views are pending and are not zero-as-quality.',
            'Full unchanged quote required; mirror uses another exact canonical field of the SAME mapped source only.',
            'Existing episode links are provenance metadata, not proof of task relevance, selection, current validity, or truth.',
            'Coverage/omission flags alone cannot prove which absent source lost the budget. No fresh retrieval or database inspection is performed.',
            'No original facts/anchors/denominators, answers or grades are altered; wording ambiguities remain outside this mechanical audit.']}
    if args.output.exists():
        raise ValueError('refuse to replace existing diagnosis')
    args.output.write_text(json.dumps(result, indent=2) + '\n')
    print(json.dumps({'status': result['status'], 'counts': result['counts'], 'paired_facts': paired,
        'paired_losses': len(losses), 'paired_gains': len(gains), 'output_sha256': sha(args.output)}))

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run', type=Path, default=Path('/private/tmp/codex-mem-quality-20261021-r16-experiment'))
    parser.add_argument('--released', type=Path, default=Path('/private/tmp/codex-mem-quality-20261021-r16-sealed'))
    parser.add_argument('--output', type=Path)
    parser.add_argument('--final', action='store_true')
    parser.add_argument('--self-test', action='store_true')
    args = parser.parse_args()
    if args.self_test:
        self_test()
    elif args.output:
        audit(args)
    else:
        parser.error('--output or --self-test required')
