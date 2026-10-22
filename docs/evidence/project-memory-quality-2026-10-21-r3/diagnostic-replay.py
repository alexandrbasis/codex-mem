"""Replay disclosed source availability on temporary Store copies, without models.

Use --dataset RUN FIXTURE GOLD repeatedly and --output REPORT.json.
Every view uses the frozen experiment's exact 6,000-character partition.
"""
import argparse
import hashlib
import json
from pathlib import Path
import re
import shutil
import sqlite3
import sys
import tempfile
from xml.etree import ElementTree

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO))
from codex_mem.project_memory_retrieval import build_project_brief, render_project_brief
from codex_mem.store import Store


def load(path):
    return json.loads(path.read_text())


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def pointers(contexts):
    found = []
    executions = []
    for context in contexts:
        for xml in re.findall(r'<project-memory\b.*?</project-memory>', context['memory'], re.S):
            tree = ElementTree.fromstring(xml)
            for item in tree.findall('item'):
                if item.get('lane') == 'source_evidence' and item.find('execution') is not None:
                    executions.append({'query_view': context['id'], 'id': item.get('id'),
                        'execution': item.find('execution').attrib,
                        'source_fields': [source.get('source_field') for source in item.findall('./field/source')],
                        'fields_omitted': item.get('fields_omitted')})
                for source in item.findall('./field/source'):
                    quote = source.findtext('quote_excerpt')
                    if quote is not None:
                        found.append({'query_view': context['id'], 'lane': item.get('lane'),
                                      'item_id': item.get('id'), **source.attrib, 'quote': quote})
    return found, executions


def visible(pool, source_id, role, text):
    return [item for item in pool if item['source_id'] == source_id and
            item['source_role'] == role and text in item['quote']]


def replay(run, fixture_path, gold_path):
    fixture, gold = load(fixture_path), load(gold_path)
    results = []
    for history in fixture['histories']:
        hid = history['id']
        generation = load(run / 'candidate' / hid / 'generation.json')
        tracked = [run / arm / hid / name for arm in ['baseline', 'candidate'] for name in
                   ['data/memory.sqlite3', 'contexts.json', 'citation-catalog.json', 'reader-0.json', 'judge-0.json']]
        before = {str(path): sha(path) for path in tracked}
        old = {arm: load(run / arm / hid / 'citation-catalog.json')['entries'] for arm in ['baseline', 'candidate']}
        old_contexts = {arm: load(run / arm / hid / 'contexts.json') for arm in ['baseline', 'candidate']}
        with tempfile.TemporaryDirectory() as temporary:
            data = Path(temporary)
            original = run / 'candidate' / hid / 'data'
            shutil.copyfile(original / 'config.json', data / 'config.json')
            source = sqlite3.connect('file:' + str(original / 'memory.sqlite3') + '?mode=ro', uri=True)
            target = sqlite3.connect(data / 'memory.sqlite3')
            source.backup(target)
            source.close()
            target.close()
            contexts = []
            with Store(data) as store:
                for question in history['questions']:
                    keys = question['project_keys']
                    per_project = max(1000, 6000 // len(keys) - 100)
                    parts, omissions = [], []
                    for key in keys:
                        brief = build_project_brief(store, generation['projects'][key],
                            query=question['question'], budget=per_project, include_shadow=True)
                        xml = render_project_brief(brief, budget=per_project)
                        ElementTree.fromstring(xml)
                        parts.append('PROJECT ' + key + '\n' + xml)
                        omissions.append(brief.get('render_omissions'))
                    memory = '\n\n'.join(parts)
                    assert len(memory) <= 6000
                    contexts.append({'id': question['id'], 'question': question['question'], 'memory': memory,
                        'context_chars': len(memory), 'context_sha256': hashlib.sha256(memory.encode()).hexdigest(),
                        'render_omissions': omissions})
        new, executions = pointers(contexts)
        old_pointers = {arm: pointers(old_contexts[arm])[0] for arm in old_contexts}
        events = {event['id']: event for session in history['sessions'] for event in session['events']}
        availability = []
        for question in history['questions']:
            qkey = hid + '/' + question['id']
            for index, fact in enumerate(gold['questions'][qkey]['required_facts']):
                supports = []
                for support in fact['support']:
                    eid = support['event_id']
                    sid = generation['source_map'][eid]
                    role = {'user': 'user_intent', 'assistant': 'assistant_report', 'tool': 'tool_record'}[events[eid]['type']]
                    fragment = support['fragment']
                    hits = visible(new, sid, role, fragment)
                    supports.append({'event_id': eid, 'source_id': sid, 'source_role': role, 'fragment': fragment,
                        'baseline_visible': bool(visible(old['baseline'], sid, role, fragment)),
                        'previous_candidate_visible': bool(visible(old['candidate'], sid, role, fragment)),
                        'new_visible': bool(hits), 'new_own_query_visible': any(hit['query_view'] == question['id'] for hit in hits),
                        'previous_candidate_own_query_visible': any(hit['query_view'] == question['id'] for hit in visible(old_pointers['candidate'], sid, role, fragment)),
                        'new_pointers': hits})
                availability.append({'question': qkey, 'fact_index': index, 'fact': fact['text'], 'support': supports})
        losses = {arm: [entry for entry in old[arm] if not visible(new, entry['source_id'], entry['source_role'], entry['quote'].strip())]
                  for arm in old}
        role_losses = {}
        for arm in old:
            role_losses[arm] = {role: len([entry for entry in losses[arm] if entry['source_role'] == role])
                                for role in ['user_intent', 'tool_record', 'assistant_report']}
        after = {str(path): sha(path) for path in tracked}
        assert before == after
        q3_facts = [fact for fact in availability if fact['question'].endswith('/q3')]
        q3_roles = {}
        for role in ['tool_record', 'assistant_report']:
            supports = [support for fact in q3_facts for support in fact['support'] if support['source_role'] == role]
            q3_roles[role] = {'required_supports': len(supports),
                'candidate_before_union': sum(support['previous_candidate_visible'] for support in supports),
                'candidate_before_own_query': sum(support['previous_candidate_own_query_visible'] for support in supports),
                'after_union': sum(support['new_visible'] for support in supports),
                'after_own_query': sum(support['new_own_query_visible'] for support in supports)}
        q3_groups = []
        q3_context = next(context for context in contexts if context['id'] == 'q3')
        for xml in re.findall(r'<project-memory\b.*?</project-memory>', q3_context['memory'], re.S):
            tree = ElementTree.fromstring(xml)
            for item in tree.findall('item'):
                for field in item.findall('field'):
                    source_refs = [dict(source.attrib, quote=source.findtext('quote_excerpt')) for source in field.findall('source')]
                    if source_refs and any(source.get('source_role') in {'tool_record', 'assistant_report'} for source in source_refs):
                        q3_groups.append({'item_id': item.get('id'), 'lane': item.get('lane'), 'field_name': field.get('name'),
                            'association': field.get('field_association'), 'pointers': source_refs,
                            'validated_execution': item.find('execution').attrib if item.find('execution') is not None else None,
                            'all_displayed_quotes_complete': all(source.get('quote_truncated') != 'True' for source in source_refs)})
        results.append({'history_id': hid, 'own_q3_role_coverage': q3_roles, 'own_q3_source_groups': q3_groups, 'original_hashes_before': before, 'original_hashes_after': after,
            'originals_unchanged': True, 'new_contexts': contexts, 'new_visible_source_quotes': new,
            'new_execution_groups': executions, 'required_fact_source_availability': availability,
            'old_visible_required_support_lost': {arm: [{'question': item['question'], 'fact_index': item['fact_index'], **support}
                for item in availability for support in item['support'] if support[arm + '_visible' if arm == 'baseline' else 'previous_candidate_visible']
                and not support['new_visible']] for arm in old},
            'original_quote_spans_lost': losses, 'original_quote_span_losses_by_role': role_losses})
    facts = [fact for history in results for fact in history['required_fact_source_availability']]
    missing = [{'question': fact['question'], 'fact_index': fact['fact_index'], 'fact': fact['fact'],
                'support': fact['support']} for fact in facts if not any(support['new_visible'] for support in fact['support'])]
    return {'run': str(run), 'fixture': str(fixture_path), 'fixture_sha256': sha(fixture_path),
            'gold': str(gold_path), 'gold_sha256': sha(gold_path),
            'coverage_summary': {'required_facts': len(facts),
                'candidate_before_union': sum(any(support['previous_candidate_visible'] for support in fact['support']) for fact in facts),
                'after_union': sum(any(support['new_visible'] for support in fact['support']) for fact in facts),
                'candidate_before_own_query': sum(any(support['previous_candidate_own_query_visible'] for support in fact['support']) for fact in facts),
                'after_own_query': sum(any(support['new_own_query_visible'] for support in fact['support']) for fact in facts),
                'remaining_missing': missing}, 'histories': results}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--dataset', nargs=3, action='append', required=True, metavar=('RUN', 'FIXTURE', 'GOLD'))
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    datasets = [replay(*(Path(value).resolve() for value in dataset)) for dataset in args.dataset]
    report = {'schema_version': 1, 'model_calls': 0, 'grades_changed': False,
        'method': 'Exact frozen retrieve_question transport: per_project=max(1000,6000//len(keys)-100), include_shadow=True, build then render at per_project, PROJECT headers and double-newline joins; assert combined view<=6000; no slicing.',
        'script_sha256': sha(Path(__file__)), 'runtime_sha256': {str(path): sha(REPO / path) for path in
            map(Path, ['codex_mem/project_memory_retrieval.py', 'codex_mem/project_memory_query.py'])},
        'max_actual_view_chars': max(context['context_chars'] for dataset in datasets for history in dataset['histories'] for context in history['new_contexts']),
        'datasets': datasets, 'limits': ['Disclosed regression source availability only; no new reader/judge/model run, no rescore or new quality claim.']}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2, ensure_ascii=False) + '\n')
    print(json.dumps({'path': str(args.output), 'sha256': sha(args.output), 'max_chars': report['max_actual_view_chars'],
        'datasets': [{'histories': len(dataset['histories']), 'lost_required_support': {arm: sum(len(h['old_visible_required_support_lost'][arm]) for h in dataset['histories']) for arm in ['baseline', 'candidate']},
            'quote_losses_by_history': {h['history_id']: h['original_quote_span_losses_by_role'] for h in dataset['histories']}}
            for dataset in datasets]}))


if __name__ == '__main__':
    main()
