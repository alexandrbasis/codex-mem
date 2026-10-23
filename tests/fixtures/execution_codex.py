"""Deterministic external app-server for execution contract tests, never a model."""
import json
from pathlib import Path
import sys
import time

scenario = json.loads(Path(__file__).with_suffix('.json').read_text())
turn_number = 0

def send(value):
    print(json.dumps(value), flush=True)

for line in sys.stdin:
    message = json.loads(line)
    method, params = message.get('method'), message.get('params', {})
    if 'id' not in message:
        continue
    result = {}
    if method == 'model/list':
        result = {'data': [{'id': 'gpt-6-luna', 'supportedReasoningEfforts': [{'reasoningEffort': 'medium'}]}]}
    elif method == 'config/read':
        result = {'config': {}}
    elif method == 'thread/start':
        result = {'thread': {'id': 'fixture-worker'}, 'model': scenario.get('model', params['model']),
                  'reasoningEffort': 'medium', 'modelProvider': 'openai'}
    elif method == 'mcpServerStatus/list':
        result = {'data': []}
    elif method == 'turn/start':
        turn_number += 1
        turn_id = 'turn-' + str(turn_number)
        send({'id': message['id'], 'result': {'turn': {'id': turn_id}}})
        def event(name, **values):
            send({'method': name, 'params': dict(threadId='fixture-worker', turnId=turn_id, **values)})
        event('turn/started', turn={'id': turn_id})
        event('thread/tokenUsage/updated', tokenUsage={'total': {'inputTokens': 10 * turn_number,
            'cachedInputTokens': 0, 'cacheWriteInputTokens': 0, 'outputTokens': 2 * turn_number,
            'reasoningOutputTokens': 0, 'totalTokens': 12 * turn_number}})
        mode = scenario.get('mode', 'valid')
        if mode == 'timeout':
            time.sleep(30)
        if mode == 'disconnect':
            sys.exit(0)
        if mode in ('cancelled', 'provider_error'):
            event('turn/completed', turn={'id': turn_id, 'status': 'interrupted' if mode == 'cancelled' else 'failed',
                                         'error': {'codexErrorInfo': 'unauthorized'}})
            continue
        if mode == 'tool':
            event('item/completed', item={'id': 'tool-1', 'type': 'commandExecution'})
        if mode == 'rerouted':
            event('model/rerouted')
        output = scenario.get('output', {'result': {'notes': [], 'disposition': 'skipped', 'session_summary': None}})
        if mode == 'project_memory':
            evidence = json.loads(params['input'][0]['text'].split('\n')[-1])
            ref = next(span['ref'] for event in evidence['events'] for span in event['spans']
                       if 'Use local import.' in span['text'])
            output = {'episodes': [{'summary': 'Use local import.', 'goal': '', 'rationale': '',
                'outcome': '', 'open_work': '', 'continuation_of': None, 'consolidation_of': [],
                'evidence': {'summary': [ref], 'goal': [], 'rationale': [], 'outcome': [],
                    'open_work': [], 'continuation_of': [], 'consolidation_of': []}}], 'claims': []}
        text = '{invalid' if mode == 'invalid_json' else json.dumps(output)
        event('item/completed', item={'id': 'answer-' + str(turn_number), 'type': 'agentMessage', 'text': text, 'phase': 'final_answer'})
        event('turn/completed', turn={'id': turn_id, 'status': 'completed'})
        continue
    send({'id': message['id'], 'result': result})
