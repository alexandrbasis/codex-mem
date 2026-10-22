from typing import Any
from pathlib import Path
import json
import hashlib

WITNESS_JUDGE_PROMPT_VERSION='answer-source-span-refs-v4'
READER_CITATION_CONTRACT='visible-span-refs-v1'
REF_READER_PROMPT_VERSION='role-attribution-visible-span-refs-v1'
ISOLATED_READER_PROMPT_VERSION='role-attribution-per-question-v1'
CRITICAL = ('false_merge', 'false_closure', 'project_mixing', 'deleted_resurrection')


def read(path: Path) -> Any:
    return json.loads(path.read_text())


def write(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + '.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str) + '\n')
    temporary.replace(path)


def digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def code_hash(root: Path) -> dict[str, str]:
    return {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted((root / 'codex_mem').glob('*.py'))}


def identifier(value: Any) -> str:
    if not isinstance(value, str) or not value or len(value) > 100 or any(c not in 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-' for c in value):
        raise ValueError('fixture identifiers require 1..100 ASCII letters, digits, underscores or hyphens')
    return value


def validate_tool_capture(event: dict) -> None:
    capture=event.get('tool_capture')
    if capture is None:
        return
    if event['type']!='tool' or not isinstance(capture,dict) or set(capture)!={'tool_name','tool_input','tool_response'}:
        raise ValueError('tool_capture requires a tool event and no scope overrides')
    if not isinstance(capture['tool_name'],str) or not 1<=len(capture['tool_name'])<=160:
        raise ValueError('tool_capture requires a bounded tool name')
    command=capture['tool_input']
    response=capture['tool_response']
    if not isinstance(command,dict) or not isinstance(command.get('command'),str) or not command['command']:
        raise ValueError('tool_capture requires an explicit inert command')
    if not isinstance(response,dict) or type(response.get('exit_code')) is not int or not isinstance(response.get('output'),str):
        raise ValueError('tool_capture requires an integer exit code and exact output')
    if len(json.dumps(capture,ensure_ascii=False).encode())>60000:
        raise ValueError('tool_capture exceeds the complete raw capture bound')


def fixture(path: Path) -> dict:
    value = read(path)
    if value.get('fictional') is not True:
        raise ValueError('experiment accepts only explicitly fictional fixtures')
    if value.get('schema_version') != 1 or value.get('split') not in ('dev', 'development', 'holdout'):
        raise ValueError('invalid fixture schema or split')
    histories = value.get('histories')
    if not isinstance(histories, list) or not 1 <= len(histories) <= 20:
        raise ValueError('fixture requires 1..20 histories')
    expected=16 if value['split']=='holdout' else 4
    if len(histories)!=expected:
        raise ValueError('fixture must contain exactly 16 holdout or four development histories')
    seen = set()
    for history in histories:
        key = identifier(history['id'])
        if key in seen:
            raise ValueError('duplicate history')
        seen.add(key)
        if not 1<=len(history['project_keys'])<=2 or len(set(history['project_keys']))!=len(history['project_keys']):
            raise ValueError('histories require one or two unique projects')
        for project in history['project_keys']:
            identifier(project)
        events = set()
        for session in history['sessions']:
            identifier(session['id'])
            if session.get('project_key', history['project_keys'][0]) not in history['project_keys']:
                raise ValueError('unknown session project')
            for event in session['events']:
                identifier(event['id'])
                if event['id'] in events or event['type'] not in ('user', 'assistant', 'tool', 'delete_observation'):
                    raise ValueError('duplicate event or unsupported event type')
                events.add(event['id'])
                if not isinstance(event.get('text', ''), str) or len(event.get('text', '')) > 60000:
                    raise ValueError('event text exceeds bound')
                validate_tool_capture(event)
            for checkpoint in session.get('checkpoints', []):
                if checkpoint['after_event_id'] not in events:
                    raise ValueError('checkpoint refers to unknown event')
        if len(history['questions']) != 6:
            raise ValueError('each history requires six questions')
        if len({q['id'] for q in history['questions']})!=6:
            raise ValueError('questions require six unique identifiers')
        for question in history['questions']:
            identifier(question['id'])
            if question.get('stage', 'final') != 'final':
                raise ValueError('only final questions are supported')
            if any(k not in history['project_keys'] for k in question.get('project_keys',[question['project_key']] if 'project_key' in question else history['project_keys'])):
                raise ValueError('unknown question project')
    return value
