"""Deterministic external app-server for execution contract tests, never a model."""
import json
from pathlib import Path
import sys
import time

scenario = json.loads(Path(__file__).resolve().with_suffix('.json').read_text())
turn_number = 0


def support_output(payload, scenario, root):
    """Explicit canned reviewer decisions exercise orchestration, not semantics."""
    counter = root.with_suffix('.support-count')
    number = int(counter.read_text()) + 1 if counter.exists() else 1
    counter.write_text(str(number))
    mode = scenario.get('support_mode', 'supported')
    if scenario.get('support_modes'):
        mode = scenario['support_modes'][min(number - 1, len(scenario['support_modes']) - 1)]
    evidence = payload['evidence'][-1]
    verdict = mode if mode in ('unsupported', 'contradicted', 'insufficient_context') else 'supported'
    output = {'version': 'summary-support-v1', 'fields': [
        dict(field=field, verdict=verdict, reason='supported' if verdict == 'supported' else 'attribute_mismatch',
             references=[dict(source_id=evidence['id'], quote=evidence['body'])])
        for field in payload['fields']]}
    if mode == 'malformed_review':
        output['fields'] = []
    return output, mode

def send(value):
    print(json.dumps(value), flush=True)

for line in sys.stdin:
    message = json.loads(line)
    method, params = message.get('method'), message.get('params', {})
    with Path(__file__).resolve().with_suffix('.trace').open('a') as trace:
        trace.write(json.dumps(message) + '\n')
    if 'id' not in message:
        continue
    if method == scenario.get('missing_method'):
        send({'id': message['id'], 'error': {'code': -32601, 'message': 'PRIVATE_ERROR'}})
        continue
    result = {}
    if method == 'account/read':
        result = scenario.get('account', {'account': {'type': 'apiKey'}, 'requiresOpenaiAuth': True})
    elif method == 'model/list':
        result = {'data': scenario.get('models', [{'id': 'gpt-6-luna', 'model': 'gpt-6-luna', 'supportedReasoningEfforts': [{'reasoningEffort': 'medium'}]}])}
        if scenario.get('model_pages'):
            result = scenario['model_pages'].get(params.get('cursor', 'first'), {})
        if scenario.get('catalog_mode') == 'timeout':
            time.sleep(30)
        if scenario.get('catalog_mode') == 'disconnect':
            sys.exit(0)
    elif method == 'config/read':
        result = {'config': {}}
    elif method == 'thread/start':
        result = {'thread': {'id': 'fixture-worker'}, 'model': scenario.get('model', params['model']),
                  'reasoningEffort': params.get('config', {}).get('model_reasoning_effort', 'medium'),
                  'modelProvider': params.get('modelProvider', 'openai')}
    elif method == 'mcpServerStatus/list':
        result = {'data': []}
    elif method == 'turn/start':
        turn_number += 1
        if turn_number == 1 and scenario.get('config_change'):
            change = scenario['config_change']
            config_path = Path(change['path'])
            config = json.loads(config_path.read_text())
            config.update(change['values'])
            config_path.write_text(json.dumps(config))
        if turn_number == 1 and 'next_scenario' in scenario:
            Path(__file__).resolve().with_suffix('.json').write_text(json.dumps(scenario['next_scenario']))
        if scenario.get('gate'):
            gate = Path(scenario['gate'])
            gate.with_suffix('.ready').touch()
            deadline = time.monotonic() + 10
            while not gate.exists() and time.monotonic() < deadline:
                time.sleep(.01)

        turn_id = 'turn-' + str(turn_number)
        send({'id': message['id'], 'result': {'turn': {'id': turn_id}}})
        def event(name, **values):
            send({'method': name, 'params': dict(threadId='fixture-worker', turnId=turn_id, **values)})
        event('turn/started', turn={'id': turn_id})
        if not scenario.get('omit_usage'):
            event('thread/tokenUsage/updated', tokenUsage={'total': {'inputTokens': 10 * turn_number,
            'cachedInputTokens': 0, 'cacheWriteInputTokens': 0, 'outputTokens': 2 * turn_number,
            'reasoningOutputTokens': 0, 'totalTokens': 12 * turn_number}})
        support = 'version' in params['outputSchema'].get('properties', {})
        review_output = None
        mode = scenario.get('mode', 'valid')
        if support:
            payload = json.loads(params['input'][0]['text'].split('\n')[-1])
            review_output, mode = support_output(payload, scenario, Path(__file__).resolve())
        if mode == 'timeout':
            time.sleep(30)
        if mode == 'disconnect':
            sys.exit(0)
        if mode in ('cancelled', 'provider_error'):
            event('turn/completed', turn={'id': turn_id, 'status': 'interrupted' if mode == 'cancelled' else 'failed',
                                         'error': {'codexErrorInfo': scenario.get('error_info', 'unauthorized')}})
            continue
        if mode == 'tool':
            event('item/completed', item={'id': 'tool-1', 'type': 'commandExecution'})
        if mode == 'rerouted':
            event('model/rerouted')
        output = scenario.get('output', {'result': {'notes': [], 'disposition': 'skipped', 'session_summary': None}})
        if scenario.get('outputs'):
            output = scenario['outputs'][min(turn_number - 1, len(scenario['outputs']) - 1)]
        if mode == 'project_memory':
            if turn_number == 1:
                evidence = json.loads(params['input'][0]['text'].split('\n')[-1])
            ref = next(span['ref'] for event in evidence['events'] for span in event['spans']
                       if 'Use local import.' in span['text'])
            output = {'episodes': [{'summary': 'Use local import.', 'goal': '', 'rationale': '',
                'outcome': '', 'open_work': '', 'continuation_of': None, 'consolidation_of': [],
                'evidence': {'summary': [ref], 'goal': [], 'rationale': [], 'outcome': [],
                    'open_work': [], 'continuation_of': [], 'consolidation_of': []}}], 'claims': []}
        if mode == 'project_memory' and scenario.get('claim'):
            output['claims'] = [{'text': 'Use local import.', 'kind': 'requirement',
                'workflow_state': 'open', 'scope': {'environment': '', 'version': ''},
                'rationale': '', 'evidence': {'text': [ref], 'workflow_state': [ref], 'rationale': []},
                'outgoing_relations': []}]
        if mode in ('pm_task', 'pm_consolidate', 'pm_continue'):
            supplied = json.loads(params['input'][0]['text'].split('\n')[-1])
            spans = [span for event in supplied['events'] for span in event['spans']]
            refs = [span['ref'] for span in spans]
            heads = supplied['current_state']['episodes']
            merging = mode == 'pm_consolidate'
            output = {'episodes': [{'summary': ' '.join(span['text'] for span in spans),
                'goal': 'Task JOB-17', 'rationale': '', 'outcome': '', 'open_work': '',
                'continuation_of': heads[0]['id'] if merging or mode == 'pm_continue' else None,
                'consolidation_of': [head['id'] for head in heads[1:]] if merging else [],
                'evidence': {'summary': refs, 'goal': refs[:1], 'rationale': [], 'outcome': [],
                    'open_work': [], 'continuation_of': refs if merging or mode == 'pm_continue' else [],
                    'consolidation_of': refs if merging else []}}], 'claims': []}
        if support:
            output = review_output
        text = '{invalid'  if mode == 'invalid_json' else json.dumps(output)
        event('item/completed', item={'id': 'answer-' + str(turn_number), 'type': 'agentMessage', 'text': text, 'phase': 'final_answer'})
        event('turn/completed', turn={'id': turn_id, 'status': 'completed'})
        continue
    send({'id': message['id'], 'result': result})
