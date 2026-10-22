#!/usr/bin/env python3
"""Offline disclosed-data context replay. No answer generation or grade repair.

The controller snapshots both runtimes and copies every original trial data
folder before Store opens it. Each runtime runs in a fresh subprocess. Worker
audit guards reject network, subprocess and SQLite access outside its scratch
copies. Gold is used only for literal source-span availability after rendering.
"""
from pathlib import Path
import argparse
import hashlib
import importlib.util
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[4]
BASE = Path(__file__).resolve().parent
OLD = ROOT / 'docs/evidence/project-memory-quality-2026-10-21-r18'
RUN = Path('/private/tmp/codex-mem-quality-20261021-r18-experiment')
HELPER_SHA = 'd935fe07687571bf42271e4a9606c9a98a75a162beb8a050f4b7fcc95960a388'
QUERY_SHA = '4d0bef0fe97774415cb856a8d2a20f4443e6648b9198100702b292d5f2100ada'
RECEIPT_SHA = 'ba9792fc33034f2209b27cc229e26fe74de9339810c6e474421ab6c78a479b5c'


def sha(data):
    return hashlib.sha256(data).hexdigest()


def read(path):
    return json.loads(path.read_bytes())


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        raise ValueError('Refusing to overwrite replay evidence: ' + str(path))
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n')


def file_hashes(directory):
    result = {}
    for path in sorted(directory.rglob('*')):
        if path.is_symlink():
            raise ValueError('Replay does not copy symlinks.')
        if path.is_file():
            result[str(path.relative_to(directory))] = sha(path.read_bytes())
    return result


def logical_hash(data):
    connection = sqlite3.connect('file:' + str(data / 'memory.sqlite3') + '?mode=ro', uri=True)
    try:
        connection.execute('PRAGMA query_only=ON')
        dump = list(connection.iterdump())
        return sha(json.dumps(dump, sort_keys=True, ensure_ascii=False).encode())
    finally:
        connection.close()


def full_anchor_available(catalog, source_id, source_field, start, end, quote):
    # Separate visible spans cannot be joined into a new canonical witness.
    return any(span['source_id'] == source_id and span['source_field'] == source_field
               and span['start'] <= start < end <= span['end']
               and span['quote'][start - span['start']:end - span['start']] == quote
               for span in catalog)


def offline_guard(scratch):
    scratch = scratch.resolve()
    def guard(event, args):
        if event in {'socket.connect', 'socket.getaddrinfo', 'subprocess.Popen', 'os.system', 'os.posix_spawn'}:
            raise RuntimeError('Offline replay blocks external execution/network: ' + event)
        if event == 'sqlite3.connect':
            target = str(args[0])
            if target == ':memory:':
                return
            if target.startswith('file:'):
                target = target[5:].split('?', 1)[0]
            if not Path(target).resolve().is_relative_to(scratch):
                raise RuntimeError('Offline replay blocks SQLite outside scratch.')
    sys.addaudithook(guard)


def worker(job_path):
    job = read(job_path)
    scratch = Path(job['scratch'])
    offline_guard(scratch)
    sys.dont_write_bytecode = True
    if any(name == 'codex_mem' or name.startswith('codex_mem.') for name in sys.modules):
        raise RuntimeError('Product modules were imported before runtime isolation.')
    runtime = Path(job['runtime']).resolve()
    sys.path.insert(0, str(runtime))
    from codex_mem.store import Store
    from codex_mem.project_memory_retrieval import build_project_brief, render_project_brief
    spec = importlib.util.spec_from_file_location('held_quote_validator', job['helper'])
    helper = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(helper)
    assert sha(Path(job['helper']).read_bytes()) == HELPER_SHA
    records, database_receipts = [], []
    gaps = []
    for history in job['fixture']['histories']:
        hid = history['id']
        meta = job['histories'][hid]
        data = Path(meta['scratch_data'])
        before = logical_hash(data)
        if before != meta['generation_state_sha256']:
            gaps.append({'history_id': hid, 'kind': 'scratch_initial_logical_state_mismatch'})
        config = read(data / 'config.json')
        if any(config.get(key) for key in ['jev_filter_enabled', 'jev_quality_enabled', 'jev_retrieval_enabled']):
            raise RuntimeError('Original trial configuration enables Jev; replay refuses it.')
        originals = helper.original_source_fields(history, meta['source_map'])
        event_projects = {}
        deleted_ids = set()
        for session in history['sessions']:
            project_key = session.get('project_key', history['project_keys'][0])
            for event in session['events']:
                source_id = meta['source_map'].get(event['id'])
                if source_id:
                    event_projects[source_id] = project_key
                if event['type'] == 'delete_observation':
                    deleted_ids.add(meta['source_map'].get(event['observation_event_id']))
        with Store(data) as store:
            # Both shipped Store constructors initialize paths/schema. Only
            # scratch copies are opened, then all retrieval SQL is read-only.
            store._connection.execute('PRAGMA query_only=ON')
            for question in history['questions']:
                keys = question.get('project_keys', [question['project_key']] if 'project_key' in question else list(meta['projects']))
                selected_scopes = [str(Path(meta['projects'][key]).resolve()) for key in keys]
                per_project = max(1000, job['budget'] // len(keys) - 100)
                parts, project_views = [], []
                for key in keys:
                    brief = build_project_brief(store, meta['projects'][key], query=question['question'], budget=per_project,
                                                include_shadow=True, selected_project_scopes=selected_scopes)
                    xml = render_project_brief(brief, budget=per_project)
                    parts.append('PROJECT ' + key + '\n' + xml)
                    project_catalog = []
                    validation_error = None
                    try:
                        project_catalog = helper.reader_citation_catalog([{'memory': xml}], originals)
                        invalid = [span['source_id'] for span in project_catalog
                                   if event_projects.get(span['source_id']) != key or span['source_id'] in deleted_ids]
                        if invalid:
                            gaps.append({'history_id': hid, 'question_id': question['id'], 'project_key': key,
                                         'kind': 'source_project_or_deleted_source_isolation', 'source_ids': sorted(set(invalid))})
                    except (ValueError, ET.ParseError) as error:
                        validation_error = type(error).__name__ + ': ' + str(error)
                        gaps.append({'history_id': hid, 'question_id': question['id'], 'project_key': key,
                                     'kind': 'ungrounded_rendered_quote', 'error': validation_error})
                    project_views.append({'project_key': key, 'budget': per_project, 'xml_chars': len(xml),
                                          'skip_reason': brief.get('skip_reason'), 'enabled': brief.get('enabled'),
                                          'quoted_spans': len(project_catalog), 'validation_error': validation_error})
                memory = '\n\n'.join(parts)
                context = {'id': question['id'], 'question': question['question'], 'memory': memory,
                           'context_chars': len(memory), 'context_sha256': sha(memory.encode())}
                if len(memory) > job['budget']:
                    gaps.append({'history_id': hid, 'question_id': question['id'], 'kind': 'combined_budget_exceeded'})
                catalog = []
                annotation_chars = None
                try:
                    catalog = helper.reader_citation_catalog([context], originals)
                    if catalog and not helper.typed_citations_visible(
                            [{'catalog_ref': entry['ref'], **{key: value for key, value in entry.items() if key != 'ref'}} for entry in catalog], catalog, originals):
                        raise ValueError('Canonical quote catalog cannot validate its own citation pointers.')
                    annotated = helper.annotated_reader_contexts([context], catalog, job['budget'])
                    annotation_chars = len(annotated[0]['memory'])
                except (ValueError, ET.ParseError) as error:
                    gaps.append({'history_id': hid, 'question_id': question['id'], 'kind': 'combined_quote_or_annotation_contract',
                                 'error': type(error).__name__ + ': ' + str(error)})
                anchor_checks = []
                gold_question = job['gold']['questions'][hid + '/' + question['id']]
                for fact_index, fact in enumerate(gold_question['required_facts']):
                    for support_index, support in enumerate(fact['support']):
                        source_id = meta['source_map'].get(support['event_id'])
                        field = support.get('source_field', 'body')
                        shown = full_anchor_available(catalog, source_id, field, support['start'], support['end'], support['quote'])
                        anchor_checks.append({'fact_index': fact_index, 'support_index': support_index,
                                              'event_id': support['event_id'], 'source_id': source_id, 'source_field': field,
                                              'start': support['start'], 'end': support['end'], 'quote': support['quote'],
                                              'fully_visible_in_one_grounded_span': shown})
                fact_visibility = []
                for fact_index, fact in enumerate(gold_question['required_facts']):
                    supports = [entry for entry in anchor_checks if entry['fact_index'] == fact_index]
                    fact_visibility.append({'fact_index': fact_index, 'all_support_anchors_visible': bool(supports) and all(
                        entry['fully_visible_in_one_grounded_span'] for entry in supports),
                        'any_support_anchor_visible': any(entry['fully_visible_in_one_grounded_span'] for entry in supports)})
                records.append({'history_id': hid, 'question_id': question['id'], 'selected_project_keys': keys,
                                'selected_project_scopes': selected_scopes, 'project_views': project_views,
                                'context': context, 'annotated_memory_chars': annotation_chars,
                                'grounded_quote_catalog': catalog, 'anchor_checks': anchor_checks,
                                'fact_source_availability': fact_visibility})
        after = logical_hash(data)
        database_receipts.append({'history_id': hid, 'before_logical_sha256': before, 'after_logical_sha256': after,
                                  'unchanged_by_retrieval': before == after, 'original_generation_state_sha256': meta['generation_state_sha256']})
        if before != after:
            gaps.append({'history_id': hid, 'kind': 'scratch_logical_state_changed'})
    imports = {name: str(Path(module.__file__).resolve()) for name, module in sys.modules.items()
               if (name == 'codex_mem' or name.startswith('codex_mem.')) and getattr(module, '__file__', None)}
    if not all(Path(path).is_relative_to(runtime) for path in imports.values()):
        gaps.append({'kind': 'runtime_import_isolation'})
    save(Path(job['output']), {'schema_version': 1, 'runtime_label': job['label'], 'runtime_hashes': job['runtime_hashes'],
                              'runtime_imports': imports, 'records': records, 'scratch_database_receipts': database_receipts,
                              'gaps': gaps, 'native_model_network_calls': 0,
                              'configuration_policy': 'Original copied configuration is unchanged. Jev must already be disabled. Network/process APIs and non-scratch SQLite opens are denied. Store constructors touch scratch only; subsequent retrieval SQL uses query_only.'})


def self_test():
    catalog = [{'source_id': 's1', 'source_field': 'body', 'source_role': 'user_intent', 'start': 0, 'end': 5, 'quote': 'Keep '},
               {'source_id': 's1', 'source_field': 'body', 'source_role': 'user_intent', 'start': 5, 'end': 10, 'quote': 'logs.'}]
    assert not full_anchor_available(catalog, 's1', 'body', 0, 10, 'Keep logs.')
    assert full_anchor_available(catalog, 's1', 'body', 5, 10, 'logs.')
    assert not full_anchor_available(catalog, 's1', 'tool_response.output', 5, 10, 'logs.')
    assert not full_anchor_available(catalog, 's2', 'body', 5, 10, 'logs.')
    assert not full_anchor_available(catalog, 's1', 'body', 5, 10, 'other')
    print('Five synthetic anchor-boundary checks passed. No trial data or runtime opened.')


def execute(args):
    if not args.execute_held_replay:
        raise ValueError('Final replay requires explicit --execute-held-replay after source hold.')
    receipt_path = ROOT / 'docs/evidence/project-memory-quality-2026-10-21-r19-query-development/scoped-preface-v2/final-query-receipt.json'
    assert sha(receipt_path.read_bytes()) == RECEIPT_SHA
    held = read(receipt_path)
    assert held['query_final_sha256'] == args.expected_query_sha == QUERY_SHA
    source_hashes = held['runtime_sha256']
    assert len(source_hashes) == 48
    for name, expected in source_hashes.items():
        assert sha((ROOT / name).read_bytes()) == expected
    helper_path = OLD / 'frozen-helper.py'
    assert sha(helper_path.read_bytes()) == HELPER_SHA
    archive_path = OLD / 'frozen-product.tar.gz'
    old_archive_hash = sha(archive_path.read_bytes())
    assert old_archive_hash == read(OLD / 'frozen-source-archive.json')['archive_sha256']
    fixture_path, gold_path = OLD / 'holdout/fixture.json', OLD / 'holdout/gold.json'
    fixture, gold = read(fixture_path), read(gold_path)
    protected = {str(path): sha(path.read_bytes()) for path in [archive_path, helper_path, fixture_path, gold_path, receipt_path, RUN / 'calls.jsonl', RUN / 'run.json']}
    scratch = Path(tempfile.mkdtemp(prefix='r19-query-offline-', dir='/private/tmp'))
    original_data_hashes = {}
    for history in fixture['histories']:
        directory = RUN / 'candidate' / history['id'] / 'data'
        original_data_hashes[history['id']] = file_hashes(directory)
    # Snapshot both runtime packages. Never execute product code from original source.
    old_runtime, new_runtime = scratch / 'old-runtime', scratch / 'new-runtime'
    old_hashes = {}
    with tarfile.open(archive_path) as handle:
        for item in handle.getmembers():
            path = Path(item.name)
            assert item.isfile() and len(path.parts) == 2 and path.parts[0] == 'codex_mem' and path.suffix == '.py'
            data = handle.extractfile(item).read()
            target = old_runtime / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
            old_hashes[str(path)] = sha(data)
    assert len(old_hashes) == 48
    for name in source_hashes:
        target = new_runtime / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes((ROOT / name).read_bytes())
    changed_runtime = [name for name in source_hashes if source_hashes[name] != old_hashes[name]]
    assert changed_runtime == ['codex_mem/project_memory_query.py']
    outputs = {}
    for label, runtime, hashes in [('old-r18-frozen', old_runtime, old_hashes), ('new-held-query', new_runtime, source_hashes)]:
        histories = {}
        for history in fixture['histories']:
            hid = history['id']
            original_dir = RUN / 'candidate' / hid
            meta = read(original_dir / 'generation.json')
            destination = scratch / label / hid / 'data'
            shutil.copytree(original_dir / 'data', destination)
            assert file_hashes(destination) == original_data_hashes[hid]
            histories[hid] = {'scratch_data': str(destination), 'projects': meta['projects'], 'source_map': meta['source_map'],
                              'generation_state_sha256': meta['state_sha256']}
        result = scratch / (label + '-result.json')
        job = {'label': label, 'runtime': str(runtime), 'runtime_hashes': hashes, 'scratch': str(scratch),
               'helper': str(helper_path), 'budget': 6000, 'fixture': fixture, 'gold': gold,
               'histories': histories, 'output': str(result)}
        job_path = scratch / (label + '-job.json')
        save(job_path, job)
        environment = dict(os.environ, PYTHONDONTWRITEBYTECODE='1')
        environment.pop('PYTHONPATH', None)
        completed = subprocess.run([sys.executable, str(Path(__file__).resolve()), '--worker-job', str(job_path)],
                                   cwd=scratch, env=environment, text=True, capture_output=True)
        if completed.returncode:
            save(BASE / (label + '-worker-failure.json'), {'returncode': completed.returncode, 'stdout': completed.stdout,
                                                         'stderr': completed.stderr, 'scratch': str(scratch)})
            raise RuntimeError('Offline worker failed; retained failure receipt. No automatic retry.')
        outputs[label] = read(result)
        save(BASE / (label + '-contexts.json'), outputs[label])
    by_arm = {label: {(row['history_id'], row['question_id']): row for row in output['records']}
              for label, output in outputs.items()}
    old, new = by_arm['old-r18-frozen'], by_arm['new-held-query']
    assert len(old) == len(new) == 96 and set(old) == set(new)
    comparisons, anchor_gains, anchor_losses, fact_gains, fact_losses = [], [], [], [], []
    recovered_empty = []
    for identity in old:
        before, after = old[identity], new[identity]
        before_anchors = {(entry['fact_index'], entry['support_index']): entry for entry in before['anchor_checks']}
        after_anchors = {(entry['fact_index'], entry['support_index']): entry for entry in after['anchor_checks']}
        assert set(before_anchors) == set(after_anchors)
        for key in before_anchors:
            bv, av = before_anchors[key]['fully_visible_in_one_grounded_span'], after_anchors[key]['fully_visible_in_one_grounded_span']
            if bv != av:
                entry = {'history_id': identity[0], 'question_id': identity[1], **after_anchors[key]}
                (anchor_gains if av else anchor_losses).append(entry)
        for bf, af in zip(before['fact_source_availability'], after['fact_source_availability']):
            if bf['all_support_anchors_visible'] != af['all_support_anchors_visible']:
                entry = {'history_id': identity[0], 'question_id': identity[1], 'fact_index': af['fact_index']}
                (fact_gains if af['all_support_anchors_visible'] else fact_losses).append(entry)
        if not before['grounded_quote_catalog'] and after['grounded_quote_catalog']:
            recovered_empty.append({'history_id': identity[0], 'question_id': identity[1], 'question': before['context']['question']})
        comparisons.append({'history_id': identity[0], 'question_id': identity[1], 'question': before['context']['question'],
                            'old_context_chars': before['context']['context_chars'], 'new_context_chars': after['context']['context_chars'],
                            'old_quoted_spans': len(before['grounded_quote_catalog']), 'new_quoted_spans': len(after['grounded_quote_catalog']),
                            'context_equal': before['context']['memory'] == after['context']['memory'],
                            'old_visible_anchor_count': sum(entry['fully_visible_in_one_grounded_span'] for entry in before_anchors.values()),
                            'new_visible_anchor_count': sum(entry['fully_visible_in_one_grounded_span'] for entry in after_anchors.values())})
    immutable = all(file_hashes(RUN / 'candidate' / hid / 'data') == expected for hid, expected in original_data_hashes.items())
    immutable = immutable and all(sha(Path(path).read_bytes()) == expected for path, expected in protected.items())
    current_sources_held = all(sha((ROOT / name).read_bytes()) == expected for name, expected in source_hashes.items())
    gaps = [entry for output in outputs.values() for entry in output['gaps']]
    if not immutable:
        gaps.append({'kind': 'original_data_or_evidence_changed'})
    if not current_sources_held:
        gaps.append({'kind': 'held_source_changed_during_replay'})
    counts = {}
    for label, output in outputs.items():
        counts[label] = {'questions': len(output['records']),
                         'visible_support_anchors': sum(entry['fully_visible_in_one_grounded_span'] for row in output['records'] for entry in row['anchor_checks']),
                         'all_support_anchors_visible_facts': sum(entry['all_support_anchors_visible'] for row in output['records'] for entry in row['fact_source_availability']),
                         'quoted_spans': sum(len(row['grounded_quote_catalog']) for row in output['records']),
                         'max_context_chars': max(row['context']['context_chars'] for row in output['records']),
                         'max_annotated_memory_chars': max((row['annotated_memory_chars'] or 0) for row in output['records']),
                         'no_visible_quotes_questions': sum(not row['grounded_quote_catalog'] for row in output['records'])}
    receipt = {'schema_version': 1, 'status': 'offline_disclosed_context_regression_complete' if not gaps else 'offline_replay_integrity_gaps',
               'scope': 'Same disclosed R18 candidate prefix data, queries, scopes and 6000-character budgets. Actual public build_project_brief/render_project_brief output and held d935 quote validator. No model readers or grades.',
               'scratch': str(scratch), 'old_runtime_archive_sha256': old_archive_hash, 'new_held_receipt_sha256': RECEIPT_SHA,
               'runtime_changes': changed_runtime, 'old_runtime_hashes': old_hashes, 'new_runtime_hashes': source_hashes,
               'held_quote_validator_sha256': HELPER_SHA, 'protected_evidence_sha256': protected,
               'original_data_file_sha256': original_data_hashes, 'original_data_and_evidence_unchanged': immutable,
               'current_runtime_sources_unchanged_during_replay': current_sources_held, 'counts': counts,
               'recovered_no_visible_quote_questions': recovered_empty, 'anchor_gains': anchor_gains, 'anchor_losses': anchor_losses,
               'all_anchor_fact_gains': fact_gains, 'all_anchor_fact_losses': fact_losses, 'comparisons': comparisons,
               'gaps': gaps, 'native_model_api_network_calls': 0, 'new_native_clock': False, 'old_inputs_grades_protocol_costs_changed': False,
               'limits': ['Disclosed regression only, not fresh quality evidence or grade repair.',
                          'Full anchor availability means one exact grounded displayed span covers its original quote and offsets. Separate spans are not joined.',
                          'All-support availability is mechanical source coverage, not a semantic supported-answer score.',
                          'Store has no read-only constructor in either held runtime. It opens scratch copies only; query_only then prevents retrieval writes.',
                          'No evidence is added to a model request. Gold is used only after rendered contexts for source-span accounting.',
                          'Network/process APIs and SQLite outside scratch are denied in workers. No user Store is read.']}
    save(BASE / 'replay-receipt.json', receipt)
    assert immutable and current_sources_held, 'Original/frozen data or source changed.'
    print(json.dumps({'status': receipt['status'], 'receipt_sha256': sha((BASE / 'replay-receipt.json').read_bytes()),
                      'counts': counts, 'anchor_gains': len(anchor_gains), 'anchor_losses': len(anchor_losses),
                      'recovered_empty_questions': len(recovered_empty), 'gaps': len(gaps)}, ensure_ascii=False))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--worker-job', type=Path)
    parser.add_argument('--self-test', action='store_true')
    parser.add_argument('--execute-held-replay', action='store_true')
    parser.add_argument('--expected-query-sha', default=QUERY_SHA)
    args = parser.parse_args()
    if args.worker_job:
        worker(args.worker_job)
    elif args.self_test:
        self_test()
    else:
        execute(args)


if __name__ == '__main__':
    main()
