"""Compare exact visible source intervals and released required anchor spans only."""
import hashlib
import json
from pathlib import Path

out = Path(__file__).resolve().parent
root = out.parents[2]
before = json.loads((out / 'before-disclosed-replay.json').read_text())
after = json.loads((out / 'after-disclosed-replay.json').read_text())
assert before['original_pins'] == after['original_pins']
assert len(before['rows']) == len(after['rows']) == 96
assert all(row['saved_byteexact'] for row in before['rows'])
fixture_path = root / 'docs/evidence/project-memory-quality-2026-10-21-r20/holdout/fixture.json'
gold_path = root / 'docs/evidence/project-memory-quality-2026-10-21-r20/holdout/gold.json'
histories = {history['id']: history for history in json.loads(fixture_path.read_text())['histories']}
gold = json.loads(gold_path.read_text())['questions']
roles = {'user': 'user_intent', 'assistant': 'assistant_report', 'tool': 'tool_record'}


def covers(old, new):
    keys = ('source_id', 'source_role', 'source_field', 'read_owner')
    if not all(old.get(key) == new.get(key) for key in keys):
        return False
    if old.get('tool_use_id') and old.get('tool_use_id') != new.get('tool_use_id'):
        return False
    start = int(old['start']) - int(new['start'])
    return start >= 0 and new['quote'][start:start + len(old['quote'])] == old['quote']


def associated(old, new):
    if not covers(old, new) or old['field'] != new['field'] or old['item'] != new['item']:
        return False
    relevant_root = ('trust', 'freshness', 'worktree', 'capture_order_basis')
    return all(old['root_qualifiers'].get(key) == new['root_qualifiers'].get(key) for key in relevant_root)


rows, required, associations, metadata = [], [], [], []
for old, new in zip(before['rows'], after['rows']):
    assert (old['case'], old['id']) == (new['case'], new['id'])
    loss = [source for source in old['sources'] if not any(covers(source, item) for item in new['sources'])]
    gain = [source for source in new['sources'] if not any(covers(source, item) for item in old['sources'])]
    qualifier_loss = [association for association in old['associations'] if not any(
        associated(association, item) for item in new['associations'])]
    qualifier_gain = [association for association in new['associations'] if not any(
        associated(association, item) for item in old['associations'])]
    if old['context_sha256'] != new['context_sha256']:
        rows.append({'case': old['case'], 'id': old['id'], 'question': old['question'],
            'old_visible_source_intervals': len(old['sources']), 'new_visible_source_intervals': len(new['sources']),
            'lost_intervals': loss, 'gained_intervals': gain,
            'owner_budget': new['owner_budget'], 'after_owner_characters': new['owner_characters'],
            'after_context_characters': new['context_characters'],
            'before_counters': old['counters'], 'after_counters': new['counters']})
    if qualifier_loss or qualifier_gain:
        associations.append({'case': old['case'], 'id': old['id'],
            'removed_qualified_associations': qualifier_loss, 'added_qualified_associations': qualifier_gain})
    def metadata_key(item):
        return (item['read_owner'], item['item'].get('lane'), item['item'].get('id'))
    def metadata_values(item):
        return {key: value for key, value in item['item'].items()
                if key not in {'fields_omitted', 'field_associations_omitted'}}
    new_metadata = {metadata_key(item): item for item in new['record_metadata']
                    if item['item'].get('id') and item['item'].get('lane') in {'episode', 'claim', 'signal', 'source_evidence'}}
    changed_metadata = []
    for item in old['record_metadata']:
        if not item['item'].get('id') or item['item'].get('lane') not in {'episode', 'claim', 'signal', 'source_evidence'}:
            continue
        replacement = new_metadata.get(metadata_key(item))
        if replacement is None or metadata_values(item) != metadata_values(replacement):
            changed_metadata.append({'before': item, 'after': replacement})
    if changed_metadata:
        metadata.append({'case': old['case'], 'id': old['id'], 'changed_or_removed_record_metadata': changed_metadata})
    events = {event['id']: event for session in histories[old['case']]['sessions'] for event in session['events']}
    for fact_index, fact in enumerate(gold[old['case'] + '/' + old['id']]['required_facts']):
        for support_index, support in enumerate(fact['support']):
            source_id = old['source_map'][support['event_id']]
            anchor = {'source_id': source_id, 'source_role': roles[events[support['event_id']]['type']],
                'source_field': support['source_field'], 'read_owner': old['source_owners'].get(source_id),
                'start': support['start'], 'end': support['end'], 'quote': support['quote']}
            was = any(covers(anchor, source) for source in old['sources'])
            now = any(covers(anchor, source) for source in new['sources'])
            required.append({'case': old['case'], 'id': old['id'], 'fact_index': fact_index,
                'support_index': support_index, 'anchor': anchor, 'before_visible': was, 'after_visible': now})
sha = lambda path: hashlib.sha256(path.read_bytes()).hexdigest()
receipt = {'status': 'disclosed_source_transport_regression_only',
    'source_rows': rows, 'qualified_association_deltas': associations, 'required_anchor_checks': required,
    'record_metadata_deltas': metadata,
    'typed_association_losses': [dict(case=row['case'], id=row['id'], association=item)
        for row in associations for item in row['removed_qualified_associations'] if item['item'].get('lane') == 'claim'],
    'before_saved_byteexact': sum(row['saved_byteexact'] for row in before['rows']),
    'after_saved_byteexact': sum(row['saved_byteexact'] for row in after['rows']),
    'changed_contexts': len(rows),
    'gained_exact_intervals': sum(len(row['gained_intervals']) for row in rows),
    'lost_exact_intervals': sum(len(row['lost_intervals']) for row in rows),
    'required_anchor_count': len(required),
    'previously_visible_required_anchors': sum(row['before_visible'] for row in required),
    'required_anchor_losses': [row for row in required if row['before_visible'] and not row['after_visible']],
    'required_anchor_gains': [row for row in required if not row['before_visible'] and row['after_visible']],
    'removed_qualified_associations': sum(len(row['removed_qualified_associations']) for row in associations),
    'added_qualified_associations': sum(len(row['added_qualified_associations']) for row in associations),
    'maximum_context_characters': max(row['context_characters'] for row in after['rows']),
    'owner_bounds_pass': all(size <= row['owner_budget'] for row in after['rows'] for size in row['owner_characters']),
    'context_bounds_pass': all(row['context_characters'] <= 6000 for row in after['rows']),
    'original_pinned_files_unchanged': len(before['original_pins']),
    'frozen_helper_sha256': before['helper_sha256'], 'fixture_sha256': sha(fixture_path), 'gold_sha256': sha(gold_path),
    'before_replay_sha256': sha(out / 'before-disclosed-replay.json'),
    'after_replay_sha256': sha(out / 'after-disclosed-replay.json'),
    'reader_answers_or_grades_read_or_changed': False, 'semantic_regrade': False,
    'native_calls': 0, 'network_calls': 0, 'model_calls': 0,
    'limits': ['Every source interval and qualifier-association delta is mechanical. No transport difference establishes answer quality.',
        'A retained source interval does not prove that every original association remains. Local field/item qualifiers are compared separately.',
        'Required anchors use released R20 gold only. No fresh R21 sealed input is read.']}
(out / 'source-delta-receipt.json').write_text(json.dumps(receipt, indent=2) + '\n')
print(json.dumps({key: receipt[key] for key in ('before_saved_byteexact', 'changed_contexts',
    'gained_exact_intervals', 'lost_exact_intervals', 'required_anchor_count', 'previously_visible_required_anchors',
    'maximum_context_characters', 'removed_qualified_associations')}))
print('required anchor losses', len(receipt['required_anchor_losses']), 'gains', len(receipt['required_anchor_gains']))
for row in rows:
    if row['lost_intervals']:
        print('LOSS', row['case'], row['id'], len(row['lost_intervals']), row['question'])
