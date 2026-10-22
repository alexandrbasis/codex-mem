"""Replay disclosed R19 read contexts on disposable SQLite backups only."""
import hashlib
import json
from pathlib import Path
import shutil
import socket
import sqlite3
import sys
import tempfile
from xml.etree import ElementTree

sys.dont_write_bytecode = True
runtime, output = map(Path, sys.argv[1:])
sys.path.insert(0, str(runtime))
from codex_mem.project_memory_retrieval import build_project_brief, render_project_brief
from codex_mem.store import Store


def no_network(*args, **kwargs):
    raise AssertionError('Network calls are forbidden in this replay')


socket.socket = no_network
run = Path('/private/tmp/codex-mem-quality-20261021-r19-experiment')
rows = []
original_pins = {}
for original in sorted((run / 'candidate').glob('R19H*')):
    paths = [original / name for name in ('contexts.json', 'generation.json',
                                          'data/config.json', 'data/memory.sqlite3')]
    original_pins.update({str(path): hashlib.sha256(path.read_bytes()).hexdigest() for path in paths})
    saved = json.loads((original / 'contexts.json').read_text())
    generation = json.loads((original / 'generation.json').read_text())
    with tempfile.TemporaryDirectory(prefix='r20-disclosed-query-replay-') as scratch:
        data = Path(scratch) / 'data'
        data.mkdir()
        shutil.copy2(original / 'data/config.json', data / 'config.json')
        source = sqlite3.connect((original / 'data/memory.sqlite3').as_uri() + '?mode=ro', uri=True)
        try:
            with sqlite3.connect(data / 'memory.sqlite3') as target:
                source.backup(target)
        finally:
            source.close()
        with Store(data) as store:
            for context in saved:
                scopes = context['selected_scope_context']['selected_project_scopes']
                owner_budget = max(1000, 6000 // len(scopes) - 100)
                parts, sources, sizes = [], [], []
                for project in scopes:
                    brief = build_project_brief(store, project, query=context['question'],
                        budget=owner_budget, include_shadow=True, selected_project_scopes=scopes)
                    xml = render_project_brief(brief, budget=owner_budget)
                    assert len(xml) <= owner_budget
                    sizes.append(len(xml))
                    key = next(key for key, value in generation['projects'].items() if value == project)
                    parts.append('PROJECT ' + key + '\n' + xml)
                    for item in ElementTree.fromstring(xml).iter('source'):
                        for quote in item.findall('quote_excerpt'):
                            text = ''.join(quote.itertext())
                            if text.strip():
                                sources.append({key: item.get(key) for key in
                                    ('source_id', 'source_field', 'source_role', 'start', 'end')} |
                                    {'quote': text})
                rebuilt = '\n\n'.join(parts)
                assert len(rebuilt) <= 6000
                rows.append({'case': original.name, 'id': context['id'],
                    'question': context['question'], 'saved_byteexact': rebuilt == context['memory'],
                    'context_sha256': hashlib.sha256(rebuilt.encode()).hexdigest(),
                    'owner_budget': owner_budget, 'context_characters': len(rebuilt),
                    'owner_characters': sizes, 'sources': sources})
for path, expected in original_pins.items():
    assert hashlib.sha256(Path(path).read_bytes()).hexdigest() == expected, path
output.write_text(json.dumps({'runtime': str(runtime), 'original_pins': original_pins,
    'original_writes': 0, 'network_calls': 0, 'model_calls': 0, 'native_calls': 0,
    'rows': rows}, indent=2) + '\n')
print(json.dumps({'views': len(rows), 'saved_byteexact': sum(row['saved_byteexact'] for row in rows),
                  'maximum_owner_characters': max(size for row in rows for size in row['owner_characters'])}))
