"""Deterministic external Pi RPC executable; never installed compatibility evidence."""
import json
import os
from pathlib import Path
import sys
import time

root = Path(__file__).resolve()
scenario = json.loads(root.with_suffix('.json').read_text())
if '--version' in sys.argv:
    if scenario.get('version_timeout'):
        time.sleep(30)
    print(scenario.get('version', '1.4.0'), flush=True)
    sys.exit(0)
if sys.argv[1:3] == ['auth', 'check']:
    status = scenario.get('auth_status', 'ready')
    print(json.dumps({'status': status, 'provider': sys.argv[sys.argv.index('--provider') + 1],
                      'reason': scenario.get('auth_reason')}), flush=True)
    sys.exit(0 if status == 'ready' else 1)
model = {'id': 'fixture', 'provider': 'fixture', 'reasoning': True}
level = 'low'
count = 0
with root.with_suffix('.launch').open('a') as trace:
    trace.write(json.dumps({'args': sys.argv[1:], 'cwd': os.getcwd(),
        'agent_dir': os.environ.get('PI_CODING_AGENT_DIR'), 'pid': os.getpid()}) + '\n')
def emit(value):
    print(json.dumps(value), flush=True)
for raw in sys.stdin:
    command = json.loads(raw)
    with root.with_suffix('.trace').open('a') as trace:
        trace.write(json.dumps(command) + '\n')
    kind = command['type']
    data = {}
    if kind == 'get_available_models':
        data = {'models': scenario.get('models', [model])}
    elif kind == 'set_model':
        model = dict(model, id=command['modelId'], provider=command['provider'])
        data = model
    elif kind == 'get_available_thinking_levels':
        data = {'levels': scenario.get('levels', ['off', 'low'])}
    elif kind == 'set_thinking_level':
        if 'thinking_notification' in scenario:
            emit(scenario['thinking_notification'])
        level = scenario.get('clamped', command['level'])
    elif kind == 'get_state':
        data = {'sessionId': 'fixture-session', 'model': model, 'thinkingLevel': level,
            'isStreaming': False, 'isCompacting': False, 'pendingMessageCount': 0,
            'messageCount': count * 2 + int(count > 0), 'autoCompactionEnabled': False}
        if count and scenario.get('mode') == 'rerouted':
            data['model'] = dict(model, id='other')
    elif kind == 'prompt':
        count += 1
        if count == 1 and scenario.get('next_scenario'):
            root.with_suffix('.json').write_text(json.dumps(scenario['next_scenario']))
        mode = scenario.get('mode', 'valid')
        if scenario.get('modes'):
            mode = scenario['modes'][min(count - 1, len(scenario['modes']) - 1)]
        if count == 1:
            if scenario.get('observation'):
                import re
                sources = json.loads(re.search(r'<untrusted_observations>\n(.*?)\n</untrusted_observations>', command['message'], re.S).group(1))
                supplied = {'events': [{'spans': [{'ref': source['id'], 'text': source['body']} for source in sources]}]}
            else:
                supplied = json.loads(command['message'].split('\n')[-1])
        emit({'id': command['id'], 'type': 'response', 'command': kind, 'success': True,
              'data': {'disposition': scenario.get('disposition', 'started')}})
        emit({'type': 'agent_start'})
        emit({'type': 'turn_start'})
        if count == 1:
            system = {'role': 'system', 'content': '', 'sections': {
                'preamble': 'Process only the supplied evidence. Return only the requested JSON.',
                'cwd': '<cwd>\n' + os.getcwd() + '\n</cwd>'}}
            emit({'type': 'message_start', 'message': system})
            emit({'type': 'message_end', 'message': system})
        emit({'type': 'message_start', 'message': {'role': 'assistant', **model, 'model': model['id']}})
        usage = (scenario['usages'][min(count - 1, len(scenario['usages']) - 1)] if scenario.get('usages') else scenario.get('usage', {'input': 10, 'output': 2, 'cacheRead': 0, 'cacheWrite': 0, 'totalTokens': 12}))
        for _ in range(2):
            emit({'type': 'message_update', 'usage': {} if scenario.get('omit_usage') or (count == 1 and scenario.get('first_usage_unknown')) or (count == 2 and scenario.get('last_usage_unknown')) else usage,
                  'assistantMessageEvent': {'type': 'text_delta', 'contentIndex': 0, 'delta': ''}})
        if scenario.get('config_change') and count == 1:
            change = scenario['config_change']
            config_path = Path(change['path'])
            config = json.loads(config_path.read_text())
            config.update(change['values'])
            config_path.write_text(json.dumps(config))
        if mode == 'timeout':
            time.sleep(30)
        if mode == 'disconnect':
            sys.exit(0)
        if mode == 'tool':
            emit({'type': 'tool_execution_start', 'toolName': 'bash'})
        if mode == 'malformed':
            print('not JSON', flush=True)
        if mode == 'stderr_overflow':
            print('x' * 9000000, file=sys.stderr, flush=True)
        if mode == 'overflow':
            print('x' * 9000000, flush=True)
        spans = [span for event in supplied['events'] for span in event['spans']]
        span = next((s for s in spans if 'Use local import.' in s['text']), spans[0])
        ref = span['ref']
        output = {'episodes': [{'summary': 'Use local import.', 'goal': '', 'rationale': '',
            'outcome': '', 'open_work': '', 'continuation_of': None, 'consolidation_of': [],
            'evidence': {'summary': [ref], 'goal': [], 'rationale': [], 'outcome': [],
                'open_work': [], 'continuation_of': [], 'consolidation_of': []}}], 'claims': []}
        if mode == 'unknown_ref':
            output['episodes'][0]['evidence']['summary'] = ['not-supplied']
        if mode == 'schema':
            output['extra'] = True
        if mode in ('pm_task', 'pm_consolidate'):
            refs = [span['ref'] for span in spans]
            heads = supplied['current_state']['episodes']
            merging = mode == 'pm_consolidate'
            output = {'episodes': [{'summary': ' '.join(span['text'] for span in spans),
                'goal': 'Task JOB-17', 'rationale': '', 'outcome': '', 'open_work': '',
                'continuation_of': heads[0]['id'] if merging else None,
                'consolidation_of': [head['id'] for head in heads[1:]] if merging else [],
                'evidence': {'summary': refs, 'goal': refs[:1], 'rationale': [], 'outcome': [],
                    'open_work': [], 'continuation_of': refs if merging else [],
                    'consolidation_of': refs if merging else []}}], 'claims': []}
        if scenario.get('observation'):
            note = {'title': 'Local import decision', 'body': 'The user requests local import.', 'tags': ['import'],
                'source_ids': [ref], 'observation': {'type': 'decision', 'subtitle': '',
                'narrative': 'The user requests local import.', 'facts': [], 'concepts': [], 'files_read': [], 'files_modified': []}}
            summary = None
            if any(s.get('evidence_role') == 'assistant_report' for s in sources):
                stop = next(s['id'] for s in sources if s.get('evidence_role') == 'assistant_report')
                summary = dict(title='Import session', request='Use local import.', investigated='',
                    learned='The user requests local import.' if count == 1 else 'The assistant reported that the user requests local import.', completed='', next_steps='', notes='', source_ids=[stop])
            output = {'result': {'notes': [note] if summary is None else [], 'disposition': 'processed', 'session_summary': summary}}
            if mode == 'unknown_ref':
                output['result']['notes'][0]['source_ids'] = ['unknown']
            if mode == 'schema':
                output['result']['extra'] = True
        if scenario.get('gate'):
            gate = Path(scenario['gate'])
            gate.with_suffix('.ready').touch()
            while not gate.exists():
                time.sleep(.01)
        output = scenario.get('output', output)
        text = '{bad' if mode == 'invalid_json' else json.dumps(output)
        message = {'role': 'assistant', 'provider': model['provider'], 'model': model['id'],
            'content': [{'type': 'text', 'text': text}], 'usage': {} if scenario.get('omit_usage') or (count == 1 and scenario.get('first_usage_unknown')) or (count == 2 and scenario.get('last_usage_unknown')) else usage,
            'stopReason': 'aborted' if mode == 'cancelled' else 'error' if mode == 'provider_error' else 'stop'}
        if scenario.get('generation_thinking_notification'):
            emit({'type': 'thinking_level_changed', 'level': level})
        emit({'type': 'message_end', 'message': message})
        emit({'type': 'turn_end', 'message': message, 'toolResults': []})
        emit({'type': 'agent_end', 'messages': [message]})
        if mode == 'no_settlement':
            time.sleep(30)
        emit({'type': 'agent_settled', 'aborted': mode == 'cancelled'})
        continue
    emit({'id': command['id'], 'type': 'response', 'command': kind, 'success': True, 'data': data})
