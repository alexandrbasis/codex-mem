"""Replay frozen R16 on private database copies, never production or run databases."""
from __future__ import annotations
import argparse
import hashlib
import importlib.util
import inspect
import json
from pathlib import Path
import shutil
import sqlite3
import sys
import tarfile
import tempfile
import threading

REPO = Path(__file__).resolve().parents[3]
RUN = Path('/private/tmp/codex-mem-quality-20261021-r16-experiment')
EVIDENCE = REPO / 'docs/evidence/project-memory-quality-2026-10-21-r16'
SEALED = Path('/private/tmp/codex-mem-quality-20261021-r16-sealed')

def read(path): return json.loads(path.read_text())
def sha(path): return hashlib.sha256(path.read_bytes()).hexdigest()
def mapped_sources(value, ids):
    text = json.dumps(value)
    return sorted(sid for sid in ids if sid in text)

def run(args):
    if args.output.exists(): raise ValueError('new output required')
    archive = EVIDENCE / 'frozen-product.tar.gz'
    if sha(archive) != '0d830735d72ceae3469206c7208baa54eb2b7f650fdcd7fe423072ad956fce40':
        raise ValueError('unexpected frozen archive')
    helper_path = EVIDENCE / 'frozen-helper.py'
    if sha(helper_path) != 'd935fe07687571bf42271e4a9606c9a98a75a162beb8a050f4b7fcc95960a388':
        raise ValueError('unexpected frozen helper')
    work = Path(tempfile.mkdtemp(prefix='codex-mem-r17-frozen-trace-', dir='/private/tmp'))
    runtime = work / 'runtime'; runtime.mkdir()
    with tarfile.open(archive) as tar:
        for member in tar.getmembers():
            target = (runtime / member.name).resolve()
            if not target.is_relative_to(runtime.resolve()) or not member.isfile():
                raise ValueError('archive must contain only safe regular source files')
        tar.extractall(runtime, filter='data')
    freeze = read(RUN / 'freeze.json'); frozen = freeze['candidate_hashes']
    if len(frozen) != 47 or any(sha(runtime / name) != digest for name, digest in frozen.items()):
        raise ValueError('frozen runtime differs from exact47 map')
    sys.path.insert(0, str(runtime))
    spec = importlib.util.spec_from_file_location('frozen_trace_helper', helper_path)
    helper = importlib.util.module_from_spec(spec); spec.loader.exec_module(helper)
    from codex_mem.store import Store
    import codex_mem.project_memory_retrieval as retrieval
    from codex_mem.project_memory import ProjectMemory
    if not Path(retrieval.__file__).resolve().is_relative_to(runtime): raise ValueError('runtime import contamination')
    diagnostic = read(EVIDENCE / 'source-visibility-final.json')
    targets = {}
    if args.all_absent:
        for fact in diagnostic['facts']:
            if fact['arm'] == 'candidate' and not fact['exact_field_mirror_visible']:
                for support in fact['support']:
                    if not support['exact_field_mirror_visible'] and not support['saved_source_metadata']:
                        targets.setdefault(fact['case'], []).append({'fact_index': fact['fact_index'], **support})
    else:
        for pair in diagnostic['paired_exact_mirror_losses']:
            fact = pair['candidate']
            targets.setdefault(fact['case'], []).extend({'fact_index': fact['fact_index'], **support} for support in fact['support'])
    fixture = read(SEALED / 'holdout-fixture.json'); histories = {h['id']: h for h in fixture['histories']}
    original_hashes, clones, cases = {}, {}, []
    originals = {}
    trace = {}; current = None
    original_snapshot = ProjectMemory.snapshot
    def snapshot(self, *pos, **kw):
        value = original_snapshot(self, *pos, **kw)
        trace[current]['snapshots'].append({'query': kw.get('query'), 'source_matches': mapped_sources(value, trace[current]['ids'])})
        return value
    ProjectMemory.snapshot = snapshot
    original_source = retrieval._source_evidence
    def source_evidence(*pos, **kw):
        bound = inspect.signature(original_source).bind(*pos, **kw); bound.apply_defaults()
        for callback in ('eligible', 'relevant'):
            old = bound.arguments[callback]
            def wrapped(item, callback=callback, old=old):
                result = old(item)
                matching = mapped_sources(item, trace[current]['ids'])
                if matching: trace[current]['source_callbacks'].append({'callback': callback, 'source_ids': matching, 'item_id': item.get('id'), 'result': bool(result)})
                return result
            bound.arguments[callback] = wrapped
        value = original_source(*bound.args, **bound.kwargs)
        trace[current]['source_evidence_calls'].append({'unassociated_context': bound.arguments.get('unassociated_context'),
            'returned_source_matches': mapped_sources(value[0], trace[current]['ids']), 'coverage': value[1]})
        return value
    retrieval._source_evidence = source_evidence
    original_execution = retrieval._execution_xml
    def execution(*pos, **kw):
        xml = original_execution(*pos, **kw)
        if mapped_sources(pos[0], trace[current]['ids']): trace[current]['atomic_size_calls'].append({'record_id': pos[0].get('id'), 'chars': len(xml), 'xml': xml})
        return xml
    retrieval._execution_xml = execution
    function_hashes = {name: hashlib.sha256(inspect.getsource(value).encode()).hexdigest() for name, value in {
        'ProjectMemory.snapshot': original_snapshot, '_source_evidence': original_source,
        '_execution_xml': original_execution, '_render_project_brief': retrieval._render_project_brief}.items()}
    for case, supports in targets.items():
        current = case; hid, qid = case.split('/'); directory = RUN / 'candidate' / hid
        if hid not in clones:
            db = directory / 'data/memory.sqlite3'; config = directory / 'data/config.json'
            original_hashes[str(db)] = sha(db); original_hashes[str(config)] = sha(config)
            clone = work / 'copies' / hid; shutil.copytree(directory / 'data', clone)
            clones[hid] = clone
        history = histories[hid]; generation = read(directory / 'generation.json')
        question = next(q for q in history['questions'] if q['id'] == qid)
        saved = next(c for c in read(directory / 'contexts.json') if c['id'] == qid)
        keys = question.get('project_keys', [question['project_key']] if 'project_key' in question else list(generation['projects']))
        scopes = [generation['projects'][key] for key in keys]
        trace[case] = {'ids': [s['source_id'] for s in supports], 'snapshots': [], 'source_callbacks': [], 'source_evidence_calls': [], 'atomic_size_calls': []}
        store = Store.__new__(Store); store.data_dir = clones[hid]; store.db_path = clones[hid] / 'memory.sqlite3'
        store._closed = False; store._lock = threading.RLock()
        store._conn = sqlite3.connect(f'file:{store.db_path}?mode=ro', uri=True); store._conn.row_factory = sqlite3.Row
        store._conn.execute('PRAGMA query_only=ON')
        parts, owners = [], []
        try:
            for key in keys:
                project = generation['projects'][key]; budget = max(1000, 6000 // len(keys) - 100)
                brief = retrieval.build_project_brief(store, project, query=question['question'], budget=budget,
                    include_shadow=True, selected_project_scopes=scopes)
                xml = retrieval.render_project_brief(brief, budget=budget)
                parts.append('PROJECT ' + key + '\n' + xml)
                owner = {'project_key': key, 'budget': budget, 'xml_chars': len(xml), 'free_chars': budget-len(xml),
                    'render_omissions': brief.get('render_omissions'), 'sources': []}
                for support in supports:
                    if support['project_key'] != key: continue
                    sid = support['source_id']; lanes = [lane for lane, value in brief.items() if lane != 'xml' and sid in json.dumps(value)]
                    records = [r for lane in ('records','unassociated_user_context','unassociated_role_context') for r in brief.get(lane,[]) if sid in json.dumps(r)]
                    owner['sources'].append({'support': support, 'pre_render_lanes': lanes, 'matching_records': records,
                        'stage': 'admitted_before_render_but_absent_delivered' if records else 'not_in_final_packing_candidates'})
                owners.append(owner)
        finally:
            store._conn.close(); store._closed = True
        replayed = '\n\n'.join(parts)
        if replayed != saved['memory']:
            args.output.write_text(json.dumps({'status':'replay_drift_stop_no_causal_claim','case':case,'saved_sha256':saved['context_sha256'],'replay_sha256':hashlib.sha256(replayed.encode()).hexdigest(),'workspace':str(work)},indent=2)+'\n')
            raise ValueError('saved context mismatch; causal claims stopped')
        originals[case] = helper.original_source_fields(history, generation['source_map'])
        catalog = helper.reader_citation_catalog([saved], originals[case])
        cases.append({'case':case,'replay_xml_exact':True,'saved_context_sha256':saved['context_sha256'],
            'catalog_sha256':helper.digest(catalog),'question':question['question'],'owners':owners,'trace':trace[case]})
    if any(sha(Path(path)) != digest for path,digest in original_hashes.items()): raise ValueError('original data changed')
    if any(sha(runtime/name) != digest for name,digest in frozen.items()) or sha(helper_path) != freeze['helper_sha256']:
        raise ValueError('frozen code changed')
    counts = {}
    for case in cases:
        for owner in case['owners']:
            for source in owner['sources']:
                counts[source['stage']] = counts.get(source['stage'],0)+1
    result={'status':'exact_frozen_private_copy_replay_trace','mode':'all43_absent'if args.all_absent else'all11_paired_losses',
        'workspace':str(work),'runtime':str(runtime),'frozen47':frozen,'archive_sha256':sha(archive),
        'helper_sha256':sha(helper_path),'original_function_source_sha256':function_hashes,'original_db_config_hashes_before_after_unchanged':original_hashes,
        'cases':cases,'counts':counts,'traced_anchors':sum(counts.values()),'untraced_entirely_absent_anchors':43-sum(counts.values()),
        'models':0,'answers_grades_read':0,'original_store_operations':0,'database_mode':'private copied SQLite URI mode=ro/query_only; Store constructor never called',
        'limits':['Snapshot source matches prove source-bearing candidate presence, not eligibility or task truth.',
            'Final packing candidate presence with exact missing delivered source proves a render delivery loss; prior absence still may be guard/window/relevance and requires callback evidence.',
            'Runtime instrumentation exists only in this isolated process memory; function source hashes and exact replay bind it to frozen code.']}
    args.output.write_text(json.dumps(result,indent=2)+'\n')
    print(json.dumps({'status':result['status'],'counts':counts,'traced':result['traced_anchors'],'untraced':result['untraced_entirely_absent_anchors'],'result_sha256':sha(args.output)}))

if __name__ == '__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--output',type=Path,required=True);parser.add_argument('--all-absent',action='store_true');run(parser.parse_args())
