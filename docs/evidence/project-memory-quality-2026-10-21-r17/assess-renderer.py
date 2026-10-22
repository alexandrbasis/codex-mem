"""Pure DTO renderer transport audit. No retrieval, Store, SQL, models or grading."""
from __future__ import annotations
import argparse
from collections import Counter
from copy import deepcopy
import hashlib
import importlib.util
import json
from pathlib import Path
import re
import sys
from xml.etree import ElementTree as ET

ROOT = Path(__file__).resolve().parents[3]
BASE = Path(__file__).resolve().parent
DTO_SHA = 'acb99781542aedac198f1d0ba1177f440b8e980e058d0ba21ace951f00b1f256'
HELPER_SHA = 'd935fe07687571bf42271e4a9606c9a98a75a162beb8a050f4b7fcc95960a388'

def read(path): return json.loads(path.read_text())
def sha(path): return hashlib.sha256(path.read_bytes()).hexdigest()
def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module

def source_identity(span):
    return tuple(span.get(key, 'body' if key == 'source_field' else None)
                 for key in ('source_id', 'source_field', 'source_role', 'tool_use_id'))

def merge(intervals):
    result = []
    for lower, upper in sorted(set(intervals)):
        if type(lower) is not int or type(upper) is not int or lower < 0 or upper <= lower:
            raise ValueError('invalid interval')
        if result and lower <= result[-1][1]:
            result[-1][1] = max(result[-1][1], upper)
        else:
            result.append([lower, upper])
    return result

def subtract(intervals, covering):
    """Literal same-field coverage only: return the exact uncovered intervals."""
    output = []
    cover = merge(covering)
    for lower, upper in merge(intervals):
        cursor = lower
        for start, end in cover:
            if end <= cursor: continue
            if start >= upper: break
            if start > cursor: output.append([cursor, min(start, upper)])
            cursor = max(cursor, end)
            if cursor >= upper: break
        if cursor < upper: output.append([cursor, upper])
    return output

def coverage_difference(first, second, originals):
    groups, other = {}, {}
    for catalog, target in ((first, groups), (second, other)):
        for span in catalog:
            target.setdefault(source_identity(span), []).append((span['start'], span['end']))
    output = []
    for identity, intervals in groups.items():
        for start, end in subtract(intervals, other.get(identity, [])):
            sid, field, role, tool_id = identity
            row = {'source_id': sid, 'source_field': field, 'source_role': role,
                   'start': start, 'end': end, 'quote': originals[sid]['fields'][field][start:end]}
            if tool_id is not None: row['tool_use_id'] = tool_id
            output.append(row)
    return output

def validate_catalog(catalog, originals):
    for span in catalog:
        sid, field, role, _ = source_identity(span)
        if sid not in originals or role != originals[sid]['source_role']:
            raise ValueError('catalog source/role differs from original')
        fields = originals[sid]['fields']
        if field not in fields or fields[field][span['start']:span['end']] != span['quote']:
            raise ValueError('catalog literal field/offset differs from original')
        if span['end'] - span['start'] != len(span['quote']):
            raise ValueError('catalog interval length differs')

def xml_metrics(memory):
    markers, roles = [], Counter()
    for block in re.findall(r'<project-memory\b.*?</project-memory>', memory, re.S):
        root = ET.fromstring(block)
        for item in root.iter():
            if item.tag in ('coverage', 'omitted', 'source-context-coverage'):
                markers.append({'tag': item.tag, **item.attrib})
            if item.tag == 'source' and item.find('quote_excerpt') is not None:
                roles[item.get('source_role')] += 1
    tags = re.findall(r'<[^>]*>', memory)
    return {'context_chars': len(memory), 'markup_chars': sum(map(len, tags)),
        'escaped_quote_text_chars': sum(len(text) for text in re.findall(r'<quote_excerpt>(.*?)</quote_excerpt>', memory, re.S)),
        'derived_text_chars': sum(len(text) for text in re.findall(r'<derived_text>(.*?)</derived_text>', memory, re.S)),
        'quoted_source_occurrences_by_role': dict(roles), 'omission_metadata': markers}

def expected_hashes(path):
    value = read(path)
    if all(isinstance(key, str) and key.startswith('codex_mem/') for key in value): return value
    for key in ('candidate_product_hashes', 'product_hashes', 'candidate_hashes', 'files'):
        if isinstance(value.get(key), dict): return value[key]
    raise ValueError('expected full package hash map required')

def assess(args):
    if args.output.exists(): raise ValueError('new output directory required')
    if sha(args.dtos) != DTO_SHA: raise ValueError('immutable DTO corpus differs')
    corpus = read(args.dtos)
    for path, digest in corpus['input_hashes'].items():
        if sha(Path(path)) != digest: raise ValueError('original input changed: ' + path)
    helper_path = ROOT / 'docs/evidence/project-memory-quality-2026-10-21-r16/frozen-helper.py'
    if sha(helper_path) != HELPER_SHA: raise ValueError('frozen helper differs')
    helper = load_module('assessment_frozen_helper', helper_path)
    current = helper.code_hash(ROOT)
    expected = expected_hashes(args.expected_source_hashes)
    if current != expected: raise ValueError('current complete package hashes differ from held source')
    sys.path.insert(0, str(ROOT))
    from codex_mem.project_memory_retrieval import render_project_brief
    fixture_path = Path('/private/tmp/codex-mem-quality-20261021-r16-sealed/holdout-fixture.json')
    fixture = read(fixture_path)
    histories = {history['id']: history for history in fixture['histories']}
    views, totals, fact_rows = [], Counter(), []
    gold = None
    if args.with_gold:
        gold = read(fixture_path.with_name('holdout-gold.json'))['questions']
        if len(gold) != 96 or sum(len(q['required_facts']) for q in gold.values()) != 213:
            raise ValueError('immutable gold denominator differs')
        exact = load_module('exact_visibility_observer', ROOT / 'docs/evidence/project-memory-quality-2026-10-21-r16/source-visibility-diagnosis.py')
    for case in corpus['cases']:
        hid, qid = case['case'].split('/')
        generation_path = Path('/private/tmp/codex-mem-quality-20261021-r16-experiment/candidate') / hid / 'generation.json'
        generation = read(generation_path)
        originals = helper.original_source_fields(histories[hid], generation['source_map'])
        saved_path = generation_path.with_name('contexts.json')
        old = next(context for context in read(saved_path) if context['id'] == qid)
        parts, owners = [], []
        for owner in case['owners']:
            brief = deepcopy(owner['pre_render_brief'])
            xml = render_project_brief(brief, budget=owner['budget'])
            if not isinstance(xml, str) or len(xml) > owner['budget']:
                raise ValueError('renderer exceeded exact owner budget')
            ET.fromstring(xml)
            parts.append('PROJECT ' + owner['project_key'] + '\n' + xml)
            owners.append({'project_key': owner['project_key'], 'budget': owner['budget'],
                'xml_chars': len(xml), 'free_chars': owner['budget'] - len(xml)})
        memory = '\n\n'.join(parts)
        if len(memory) > 6000: raise ValueError('assembled context exceeded6000')
        new = {**old, 'memory': memory, 'context_chars': len(memory),
               'context_sha256': hashlib.sha256(memory.encode()).hexdigest()}
        before, after = helper.reader_citation_catalog([old], originals), helper.reader_citation_catalog([new], originals)
        validate_catalog(before, originals); validate_catalog(after, originals)
        losses, gains = coverage_difference(before, after, originals), coverage_difference(after, before, originals)
        old_lost = [span for span in before if coverage_difference([span], after, originals)]
        totals.update(views=1, owner_parts=len(owners), old_source_spans=len(before), new_source_spans=len(after),
            old_source_spans_not_fully_covered=len(old_lost), lost_intervals=len(losses), gained_intervals=len(gains),
            lost_source_chars=sum(row['end']-row['start'] for row in losses),
            gained_source_chars=sum(row['end']-row['start'] for row in gains),
            old_context_chars=len(old['memory']), new_context_chars=len(memory))
        view = {'case': case['case'], 'owners': owners, 'old_context_sha256': old['context_sha256'],
            'new_context': new, 'old_catalog': before, 'new_catalog': after,
            'losses_same_field_role_tool_identity': losses, 'gains_same_field_role_tool_identity': gains,
            'old_spans_not_fully_covered': old_lost, 'old_metrics': xml_metrics(old['memory']), 'new_metrics': xml_metrics(memory)}
        views.append(view)
        if gold:
            for index, fact in enumerate(gold[case['case']]['required_facts']):
                old_support, new_support = [], []
                for support in fact['support']:
                    sid = generation['source_map'][support['event_id']]
                    old_support.append(exact.exact_visibility(before, sid, support, originals))
                    new_support.append(exact.exact_visibility(after, sid, support, originals))
                row = {'case': case['case'], 'fact_index': index, 'text': fact['text'], 'support': fact['support'],
                    'old_strict_body': all(s['strict_body_visible'] for s in old_support),
                    'new_strict_body': all(s['strict_body_visible'] for s in new_support),
                    'old_exact_field_mirror': all(s['exact_field_mirror_visible'] for s in old_support),
                    'new_exact_field_mirror': all(s['exact_field_mirror_visible'] for s in new_support)}
                fact_rows.append(row)
    if len(views) != 96: raise ValueError('all96 DTO views required')
    if helper.code_hash(ROOT) != current or sha(helper_path) != HELPER_SHA:
        raise ValueError('source changed during assessment; no result accepted')
    for path,digest in corpus['input_hashes'].items():
        if sha(Path(path)) != digest: raise ValueError('input changed during assessment')
    result = {'status': 'pure_renderer_transport_assessment_not_quality', 'totals': dict(totals),
        'product_hashes': current, 'expected_hash_receipt': str(args.expected_source_hashes),
        'expected_hash_receipt_sha256': sha(args.expected_source_hashes), 'dto_sha256': DTO_SHA,
        'helper_sha256': HELPER_SHA, 'script_sha256': sha(Path(__file__)),
        'models': 0, 'answers_or_grades_read': 0, 'store_sql_retrieval_operations': 0,
        'limits': ['Candidate acquisition is frozen in the DTOs; no query planning or retrieval ran.',
            'Old interval retention compares exact source ID, canonical field, role and tool identity; body/output substitution cannot mask a transport loss.',
            'Optional gold visibility separately permits exact same-source canonical-field mirrors; this is not semantic recall or answer quality.',
            'All negative versions and old views remain immutable; unfit sources may still lose budget and every loss is retained.']}
    if gold:
        result['gold_visibility'] = {'facts': len(fact_rows),
            'old_strict_body': sum(f['old_strict_body'] for f in fact_rows),
            'new_strict_body': sum(f['new_strict_body'] for f in fact_rows),
            'old_exact_field_mirror': sum(f['old_exact_field_mirror'] for f in fact_rows),
            'new_exact_field_mirror': sum(f['new_exact_field_mirror'] for f in fact_rows),
            'mirror_losses': [f for f in fact_rows if f['old_exact_field_mirror'] and not f['new_exact_field_mirror']],
            'mirror_gains': [f for f in fact_rows if not f['old_exact_field_mirror'] and f['new_exact_field_mirror']]}
    args.output.mkdir(parents=True)
    (args.output/'contexts-and-coverage.json').write_text(json.dumps(views,indent=2)+'\n')
    (args.output/'result.json').write_text(json.dumps(result,indent=2)+'\n')
    if gold: (args.output/'gold-source-visibility.json').write_text(json.dumps(fact_rows,indent=2)+'\n')
    print(json.dumps({'totals':result['totals'],'gold_visibility':{k:v for k,v in result.get('gold_visibility',{}).items() if k not in ('mirror_losses','mirror_gains')},'result_sha256':sha(args.output/'result.json')}))

if __name__ == '__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--dtos',type=Path,default=BASE/'frozen-all96-pre-render-dtos.json')
    parser.add_argument('--expected-source-hashes',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--with-gold',action='store_true')
    args = parser.parse_args()
    try:
        assess(args)
    except Exception as error:
        # Preserve a failed version without accepting partial coverage metrics.
        if not args.output.exists():
            args.output.mkdir(parents=True)
            (args.output/'validation-failure.json').write_text(json.dumps({
                'status':'assessment_invalid_no_coverage_conclusions',
                'error_type':type(error).__name__, 'error':str(error),
                'script_sha256':sha(Path(__file__)),
                'expected_hash_receipt_sha256':sha(args.expected_source_hashes) if args.expected_source_hashes.exists() else None,
                'models':0,'answers_grades_read':0,'store_sql_retrieval_operations':0},indent=2)+'\n')
        raise
