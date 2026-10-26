"""Deterministic external Pi RPC executable; never installed compatibility evidence."""
import json
import os
from pathlib import Path
import sys
import time


def support_row(request, name, evidence, verdict='supported'):
    """Synthetic binding policy for wiring tests only; explicit tests override it."""
    outcome = name in request['candidate_spans']
    reports = request['report_catalog']
    report = next((key for key in reversed(reports) if reports[key]['evidence_group']=='current'),
                  next(reversed(reports)) if reports else None)
    anchor = reports[report]['span_ids'][0] if report else None
    return dict(unit_id=name, coverage='complete', candidate_assertion=f'The complete synthetic {name} assertion in the input-owned unit.',
        evidence_assertion='The cited evidence supports this attributed assertion.',
        value_comparison='Same asserted value.', attribution_comparison='Same reporting role.',
        scope_comparison='Same scope and certainty.', verdict=verdict,
        evidence_span_ids=[anchor or evidence['spans'][0]['id']],
        candidate_span_ids=[request['candidate_spans'][name][0]['id']] if outcome else [],
        report_binding=dict(target='report_text' if outcome and report else 'other' if outcome else 'outside_outcome',
            report_span_ids=[anchor] if outcome and report else [],
            properties=['language','organization','content'] if outcome and report else []))


def outcome_output(request, relation='unused'):
    """Manually authored typed wiring replies, never converted native responses."""
    items = {item['id']: item for item in request['interpretation']['items']}
    rows = []
    for pair in request['relations']:
        item = items[pair['source_item_id']]
        available = item['availability'] in ('reported', 'observed_text', 'observed') and item['target'] != 'unresolved'
        app, scope, value = 'absent', 'not_compared', 'not_compared'
        if relation in ('conflict', 'incompatible', 'no_conflict', 'compatible') and available:
            app, scope, value = 'present', 'aligned', 'incompatible' if relation in ('conflict','incompatible') else 'compatible'
        elif relation in ('not_assessed', 'unresolved'):
            app, scope, value = 'unresolved', 'unresolved', 'unresolved'
        rows.append(dict(relation_id=pair['id'], applicability=app, scope_relation=scope, value=value,
            scope_comparison='Synthetic scoped comparison.', value_comparison='Synthetic value comparison.'))
    return dict(version=request['version'], rows=rows)


def support_output(payload, scenario, root):
    """Canned unit references exercise orchestration, never semantic assessment."""
    if payload['version'] in ('summary-source-v4', 'summary-outcome-v5'):
        stage = 'source' if payload['version'] == 'summary-source-v4' else 'outcome'
        counter = root.with_suffix('.' + stage + '-count')
        number = int(counter.read_text()) + 1 if counter.exists() else 1
        counter.write_text(str(number))
        mode = scenario.get(stage + '_mode', 'supported')
        if stage == 'source':
            output = dict(version=payload['version'], coverage='partial',
                          propositions=[dict(p, target=p.get('target','report_text')) for p in scenario.get('source_propositions', [dict(statement='Synthetic reported result.', subject='Current report', temporal_scope='Current Stop', modality='reported', source_id='report1')])],
                          text_observations=scenario.get('text_observations', {key:dict(status='unknown',
                              value=None, scope='Current report only', reason='Canned provider does not interpret text.')
                              for key in ('language', 'organization')}))
        else:
            verdicts = scenario.get('outcome_verdicts', ['unused'])
            verdict = verdicts[min(number - 1, len(verdicts) - 1)]
            output = outcome_output(payload, verdict)
        return {} if mode == 'malformed_review' else output, mode
    counter = root.with_suffix('.support-count')
    number = int(counter.read_text()) + 1 if counter.exists() else 1
    counter.write_text(str(number))
    mode = scenario.get('support_mode', 'supported')
    if scenario.get('support_modes'):
        mode = scenario['support_modes'][min(number - 1, len(scenario['support_modes']) - 1)]
    evidence = payload['evidence'][-1]
    verdict = mode if mode in ('unsupported', 'contradicted', 'insufficient_context') else 'supported'
    output = dict(version='summary-support-v6', units={field:[support_row(payload, field, evidence, verdict)] for field in payload['units']})
    if mode == 'malformed_review': output['units'] = {}
    return output, mode

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
support = False
mode = scenario.get('mode', 'valid')
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
        for notification in scenario.get('model_notifications', []):
            emit(notification)
            if notification.get('type') == 'thinking_level_changed':
                level = notification.get('level')
        data = scenario.get('model_response', model)
    elif kind == 'get_available_thinking_levels':
        if 'levels_notification' in scenario:
            emit(scenario['levels_notification'])
        data = {'levels': scenario.get('levels', ['off', 'low'])}
    elif kind == 'set_thinking_level':
        if 'thinking_notification' in scenario:
            emit(scenario['thinking_notification'])
        level = scenario.get('clamped', command['level'])
    elif kind == 'get_state':
        data = {'sessionId': 'fixture-session', 'model': model, 'thinkingLevel': level,
            'isStreaming': False, 'isCompacting': False, 'pendingMessageCount': 0,
            'messageCount': count * 2 + int(count > 0), 'autoCompactionEnabled': False}
        if count and mode == 'rerouted':
            data['model'] = dict(model, id='other')
    elif kind == 'prompt':
        count += 1
        if count == 1 and scenario.get('next_scenario'):
            root.with_suffix('.json').write_text(json.dumps(scenario['next_scenario']))
        mode = scenario.get('mode', 'valid')
        if scenario.get('modes'):
            mode = scenario['modes'][min(count - 1, len(scenario['modes']) - 1)]
        if count == 1:
            candidate_payload = command['message'].split('\n')[-1]
            schema_version = json.loads(command['message'].split('\n\n', 1)[0].removeprefix('Output schema: ')).get('properties', {}).get('version', {}).get('enum', [None])[0] if command['message'].startswith('Output schema: ') else None
            if schema_version == 'summary-source-v4' or candidate_payload.startswith(('{"version": "summary-support-v6"', '{"version": "summary-outcome-v5"')):
                support = True
                payload = {'version': schema_version} if schema_version == 'summary-source-v4' else json.loads(candidate_payload)
                review_output, mode = support_output(payload, scenario, root)
                supplied = {'events': [{'spans': [{'ref': 'unused', 'text': 'review'}]}]}
            elif scenario.get('observation'):
                import re
                sources = json.loads(re.search(r'<untrusted_observations>\n(.*?)\n</untrusted_observations>', command['message'], re.S).group(1))
                supplied = {'events': [{'spans': [{'ref': source['id'], 'text': source['body']} for source in sources]}]}
            else:
                supplied = json.loads(command['message'].split('\n')[-1])
        if scenario.get('schema_helper') and os.environ.get('CODEX_MEM_PI_SCHEMA_CONTRACT'):
            # Execute the real bundled hook with synthetic Pi context only. Node
            # has no provider client here, and this fixture never contacts a model.
            import subprocess
            contract = json.loads(Path(os.environ['CODEX_MEM_PI_SCHEMA_CONTRACT']).read_text())
            helper_mode = scenario['schema_helper']
            if helper_mode == 'tampered_schema':
                Path(contract['schema_path']).write_text('{}')
            script = """
import {readFileSync, writeFileSync} from 'node:fs';
const cfg=JSON.parse(readFileSync(process.env.CODEX_MEM_PI_SCHEMA_CONTRACT,'utf8'));
const helper=await import('data:text/javascript;base64,'+readFileSync(cfg.helper_path).toString('base64'));
let handler;
helper.default({on:(name,callback)=>{if(name!=='before_provider_request')throw Error('unexpected handler');handler=callback;}});
const payload={model:cfg.model,instructions:'Synthetic system',input:[{role:'user',content:'Synthetic evidence'}],text:{verbosity:'low'},reasoning:{effort:'low'},tools:[]};
try {const output=await handler({payload},{model:{id:cfg.model,provider:cfg.provider}});writeFileSync(process.argv[1],JSON.stringify(output));}
catch { /* Pi also catches extension hook errors; acceptance still needs proof. */ }
"""
            completed = subprocess.run(['node', '--input-type=module', '-e', script,
                str(root.with_suffix('.schema-payload'))], capture_output=True, text=True, timeout=3)
            if completed.returncode != 0:
                raise RuntimeError('synthetic helper runner failed: ' + completed.stderr)
            proof_path = Path(contract['proof_path'])
            if helper_mode in ('foreign_schema', 'changed_payload', 'duplicate', 'extra_key') and proof_path.exists():
                proof = json.loads(proof_path.read_text())
                if helper_mode == 'foreign_schema': proof['schema_sha256'] = '0' * 64
                if helper_mode == 'changed_payload': proof['restored_payload_sha256'] = '0' * 64
                if helper_mode == 'extra_key': proof['private_text'] = 'must not be retained'
                text = json.dumps(proof) + '\n'
                proof_path.write_text(text * (2 if helper_mode == 'duplicate' else 1))
            if helper_mode == 'tampered_helper':
                Path(contract['helper_path']).write_text('tampered after dispatch')
            root.with_suffix('.schema-settings').write_text((Path(os.environ['PI_CODING_AGENT_DIR']) / 'settings.json').read_text())
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
        if scenario.get('observation') and not support:
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
        output = review_output if support else scenario.get('output', output)
        if scenario.get('outputs') and not support:
            output = scenario['outputs'][min(count - 1, len(scenario['outputs']) - 1)]
        text = '{bad' if mode == 'invalid_json' else json.dumps(output)
        if mode == 'duplicate_units':
            text = '{"version":"summary-support-v6","units":{"title":[],"title":[]}}'
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
