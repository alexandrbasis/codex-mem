"""Replay 96 disclosed R20 reads on fresh SQLite backups, without model calls."""
import hashlib
import importlib.util
import json
from pathlib import Path
import shutil
import socket
import sqlite3
import sys
import tempfile
from xml.etree import ElementTree as ET

sys.dont_write_bytecode = True
runtime, output = map(Path, sys.argv[1:])
runtime_hashes = {str(path.relative_to(runtime)): hashlib.sha256(path.read_bytes()).hexdigest()
                  for path in (runtime / 'codex_mem').glob('*.py')}
sys.path.insert(0, str(runtime))
from codex_mem.project_memory_retrieval import build_project_brief, render_project_brief
from codex_mem.store import Store

root = Path(__file__).resolve().parents[3]
helper_path = root / 'docs/evidence/project-memory-quality-2026-10-21-r20/frozen-helper.py'
assert hashlib.sha256(helper_path.read_bytes()).hexdigest() == 'ede2437d3a75b5c3ec8967409c44732ffa42b8873761cd3545590053b4c8d2b4'
spec = importlib.util.spec_from_file_location('r21_disclosed_frozen_helper', helper_path)
helper = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = helper
spec.loader.exec_module(helper)
helper.ROOT = runtime


def no_network(*args, **kwargs):
    raise AssertionError('Network calls are forbidden in this replay')


socket.socket = no_network
run = Path('/private/tmp/codex-mem-quality-20261021-r20-experiment')
fixture = json.loads((root / 'docs/evidence/project-memory-quality-2026-10-21-r20/holdout/fixture.json').read_text())
histories = {history['id']: history for history in fixture['histories']}
rows, pins = [], {}
for original in sorted((run / 'candidate').glob('h*')):
    paths = [original / name for name in ('contexts.json', 'generation.json', 'data/config.json', 'data/memory.sqlite3')]
    pins.update({str(path): hashlib.sha256(path.read_bytes()).hexdigest() for path in paths})
    saved = json.loads((original / 'contexts.json').read_text())
    generation = json.loads((original / 'generation.json').read_text())
    originals = helper.original_source_fields(histories[original.name], generation['source_map'])
    with tempfile.TemporaryDirectory(prefix='r21-disclosed-source-replay-') as scratch:
        data = Path(scratch) / 'data'
        data.mkdir()
        shutil.copy2(original / 'data/config.json', data / 'config.json')
        source = sqlite3.connect((original / 'data/memory.sqlite3').as_uri() + '?mode=ro', uri=True)
        try:
            owners = dict(source.execute('SELECT id, project FROM entries'))
            with sqlite3.connect(data / 'memory.sqlite3') as target:
                source.backup(target)
        finally:
            source.close()
        with Store(data) as store:
            for context in saved:
                scopes = context['selected_scope_context']['selected_project_scopes']
                budget = max(1000, 6000 // len(scopes) - 100)
                parts, sources, sizes, associations, counters, record_metadata = [], [], [], [], [], []
                for project in scopes:
                    brief = build_project_brief(store, project, query=context['question'],
                        budget=budget, include_shadow=True, selected_project_scopes=scopes)
                    xml = render_project_brief(brief, budget=budget)
                    assert len(xml) <= budget
                    sizes.append(len(xml))
                    key = next(key for key, value in generation['projects'].items() if value == project)
                    parts.append('PROJECT ' + key + '\n' + xml)
                    catalog = helper.reader_citation_catalog([{'memory': xml}], originals)
                    for item in catalog:
                        assert owners[item['source_id']] == project
                        sources.append({**item, 'read_owner': project, 'source_owner': owners[item['source_id']]})
                    tree = ET.fromstring(xml)
                    registry = {item.get('id'): item for item in tree.iter('source') if item.get('id')}
                    for item in tree.findall('item'):
                        record_metadata.append({'read_owner': project, 'item': dict(item.attrib),
                            'root_qualifiers': dict(tree.attrib)})
                        attrs = {key: value for key, value in item.attrib.items() if key not in {'id', 'fields_omitted'}}
                        if item.get('lane') in {'episode', 'claim', 'signal', 'source_evidence'}:
                            attrs['id'] = item.get('id')
                        for field in [item, *item.findall('field')]:
                            for pointer in list(field):
                                if pointer.tag not in {'source', 'quote_ref'}:
                                    continue
                                original_pointer = registry.get(pointer.get('ref')) if pointer.tag == 'quote_ref' else pointer
                                assert original_pointer is not None
                                text = original_pointer.findtext('quote_excerpt')
                                if text is None:
                                    continue
                                base = int(original_pointer.get('start'))
                                left = int(pointer.get('start', base))
                                right = int(pointer.get('end', original_pointer.get('end')))
                                text = text[left - base:right - base]
                                associations.append({'read_owner': project, 'item': attrs,
                                    'root_qualifiers': dict(tree.attrib),
                                    'field': dict(field.attrib) if field is not item else {},
                                    'source_id': original_pointer.get('source_id'),
                                    'source_role': original_pointer.get('source_role'),
                                    'source_field': original_pointer.get('source_field', 'body'),
                                    'start': left, 'end': right, 'quote': text})
                    counters.append({'read_owner': project, 'render_omissions': brief.get('render_omissions'),
                        'coverage': brief['coverage'], 'neutral': brief.get('unassociated_user_context_coverage'),
                        'builder_wire_equals_public': xml == brief['xml']})
                rebuilt = '\n\n'.join(parts)
                assert len(rebuilt) <= 6000
                rows.append({'case': original.name, 'id': context['id'], 'question': context['question'],
                    'saved_byteexact': rebuilt == context['memory'],
                    'context_sha256': hashlib.sha256(rebuilt.encode()).hexdigest(),
                    'context_characters': len(rebuilt), 'owner_characters': sizes, 'owner_budget': budget,
                    'sources': sources, 'associations': associations, 'counters': counters,
                    'record_metadata': record_metadata,
                    'source_map': generation['source_map'],
                    'source_owners': {source_id: owners.get(source_id) for source_id in generation['source_map'].values()}})
for path, expected in pins.items():
    assert hashlib.sha256(Path(path).read_bytes()).hexdigest() == expected, path
assert len(rows) == 96
assert runtime_hashes == {str(path.relative_to(runtime)): hashlib.sha256(path.read_bytes()).hexdigest()
                         for path in (runtime / 'codex_mem').glob('*.py')}
output.write_text(json.dumps({'runtime': str(runtime), 'original_pins': pins,
    'helper_sha256': hashlib.sha256(helper_path.read_bytes()).hexdigest(),
    'fixture_sha256': hashlib.sha256((root / 'docs/evidence/project-memory-quality-2026-10-21-r20/holdout/fixture.json').read_bytes()).hexdigest(),
    'runtime_hashes': runtime_hashes,
    'original_writes': 0, 'original_store_constructors': 0,
    'network_calls': 0, 'model_calls': 0, 'native_calls': 0, 'rows': rows}, indent=2) + '\n')
print(json.dumps({'views': len(rows), 'saved_byteexact': sum(row['saved_byteexact'] for row in rows),
                  'maximum_context_characters': max(row['context_characters'] for row in rows)}))
