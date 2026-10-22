"""Measure exact support visibility on disclosed R12 Stores without model calls."""
from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import importlib.util
import json
from pathlib import Path
import sqlite3
import sys
import threading

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))


def read(path):
    return json.loads(path.read_text())


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def covered(catalog, source_id, start, end, field='body'):
    intervals = sorted((span['start'], span['end']) for span in catalog
                       if span['source_id'] == source_id and span['source_field'] == field)
    cursor = start
    for lower, upper in intervals:
        if lower > cursor:
            break
        cursor = max(cursor, upper)
    return cursor >= end


def readonly_store(data):
    """Use the frozen database through a query-only connection; never migrate it."""
    from codex_mem.store import Store
    store = Store.__new__(Store)
    store.data_dir = data
    store.db_path = data / 'memory.sqlite3'
    store._closed = False
    store._lock = threading.RLock()
    store._conn = sqlite3.connect(f'file:{store.db_path}?mode=ro', uri=True)
    store._conn.row_factory = sqlite3.Row
    store._conn.execute('PRAGMA query_only=ON')
    return store


def replay(run, output):
    from codex_mem.project_memory_retrieval import build_project_brief
    spec = importlib.util.spec_from_file_location('replay_helper',
        ROOT / 'scripts/project_memory_consolidation_experiment.py')
    helper = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(helper)
    evidence = ROOT / 'docs/evidence/project-memory-quality-2026-10-21-r12/holdout'
    fixture, gold = read(evidence / 'fixture.json'), read(evidence / 'gold.json')['questions']
    protocol = read(evidence / 'protocol.json')
    original_hashes = helper.code_hash(ROOT)
    helper_hash = digest(Path(helper.__file__))
    if helper_hash != read(run / 'freeze.json')['helper_sha256']:
        raise ValueError('replay requires the unchanged frozen experiment helper')
    output.mkdir(parents=True, exist_ok=False)
    counts, gains, losses, incidental, sizes, contexts = Counter(), [], [], [], [], []
    for history in fixture['histories']:
        directory = run / 'candidate' / history['id']
        generation = read(directory / 'generation.json')
        old_contexts = {item['id']: item for item in read(directory / 'contexts.json')}
        originals = helper.original_source_fields(history, generation['source_map'])
        store = readonly_store(directory / 'data')
        try:
            for question in history['questions']:
                keys = question.get('project_keys', [question['project_key']]
                    if 'project_key' in question else list(generation['projects']))
                scopes = [generation['projects'][key] for key in keys]
                old = old_contexts[question['id']]
                parts = []
                for key in keys:
                    brief = build_project_brief(store, generation['projects'][key],
                        query=question['question'], budget=max(1000, protocol['context_chars'] // len(keys) - 100),
                        include_shadow=True, selected_project_scopes=scopes)
                    parts.append('PROJECT ' + key + '\n' + brief['xml'])
                memory = '\n\n'.join(parts)
                if len(memory) > protocol['context_chars']:
                    raise ValueError('replay exceeded the original per-question budget')
                new = {**old, 'memory': memory, 'context_chars': len(memory),
                    'context_sha256': hashlib.sha256(memory.encode()).hexdigest()}
                before = helper.reader_citation_catalog([old], originals)
                after = helper.reader_citation_catalog([new], originals)
                case = history['id'] + '/' + question['id']
                contexts.append({'case': case, 'old_context_sha256': old['context_sha256'],
                    'new_context': new, 'old_catalog_sha256': helper.digest(before),
                    'new_catalog_sha256': helper.digest(after)})
                sizes.append({'case': case, 'old_chars': len(old['memory']), 'new_chars': len(memory)})
                for span in before:
                    if not covered(after, span['source_id'], span['start'], span['end'], span['source_field']):
                        incidental.append({'case': case, **{key: span[key] for key in
                            ('source_id', 'source_field', 'start', 'end')}})
                for index, fact in enumerate(gold[case]['required_facts']):
                    def visible(catalog):
                        return all(covered(catalog, generation['source_map'][span['event_id']],
                                           span['start'], span['end']) for span in fact['support'])
                    old_full, new_full = visible(before), visible(after)
                    counts.update(facts=1, old_full=old_full, new_full=new_full)
                    if new_full and not old_full:
                        gains.append({'case': case, 'fact_index': index})
                    if old_full and not new_full:
                        losses.append({'case': case, 'fact_index': index})
        finally:
            store._conn.close()
            store._closed = True
    if helper.code_hash(ROOT) != original_hashes or digest(Path(helper.__file__)) != helper_hash:
        raise ValueError('source changed during the disclosed replay')
    result = {'status': 'disclosed_exact_support_visibility_only', 'counts': dict(counts),
        'gains': gains, 'losses': losses, 'incidental_previous_span_losses': incidental,
        'sizes': sizes, 'old_total_context_chars': sum(item['old_chars'] for item in sizes),
        'new_total_context_chars': sum(item['new_chars'] for item in sizes),
        'product_hashes': original_hashes, 'helper_sha256': helper_hash,
        'fixture_sha256': digest(evidence / 'fixture.json'), 'gold_sha256': digest(evidence / 'gold.json'),
        'models': 0, 'grades_changed': 0,
        'limits': ['Known disclosed cases, not an independent quality result.',
                   'Measures complete gold-declared body intervals, not semantic entailment or answer quality.',
                   'Incidental source spans may be displaced; list them separately.',
                   'Private original Stores are required; all database access is query-only.']}
    helper.write(output / 'new-contexts.json', contexts)
    helper.write(output / 'result.json', result)
    print(json.dumps({key: value for key, value in result.items()
                      if key not in ('sizes', 'product_hashes', 'incidental_previous_span_losses')}))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run', type=Path, default=Path('/private/tmp/codex-mem-quality-20261021-r12-experiment'))
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    replay(args.run.resolve(), args.output.resolve())
