"""Standalone prospective fictional-trial author contract. No runtime calls.

Run: python3 public-fixture-contract.py YOUR_FIXTURE.json YOUR_GOLD.json

Fixture shape (schema_version remains 1):
  {"schema_version":1,"fictional":true,"split":"holdout","histories":[...]}
A holdout has exactly 16 histories, each with:
  id: ASCII identifier; project_keys: one or two identifiers;
  sessions: [{id, project_key, events:[...], checkpoints:[{after_event_id}]}];
  questions: six {id, question, stage:"final", project_key} objects.
The prompt string key is question, not text. Use project_keys instead of
project_key on a question that explicitly selects both projects. Event objects
use {id,type,text}; type is user, assistant, tool, or delete_observation.
A deletion event adds observation_event_id naming an earlier source in the
same project. Each holdout history has one deletion control (16 total).
Checkpoints name unique events in that same session, in event order. Final
settling includes events after the last checkpoint. No checkpoint questions
or source offsets in bytes are introduced; offsets count Python characters.

Tool events may add tool_capture:{tool_name,tool_input:{command},
 tool_response:{output,exit_code}}. An exec_command capture requires its full
output to equal event.text. This is an inert fictional record, not a command
that the validator executes. User/assistant text remains role-attributed.

Gold shape:
  {"schema_version":1,"fictional":true,"questions":{
    "HISTORY_ID/QUESTION_ID":{
      "category":"CATEGORY_STRING",
      "required_facts":[{"text":"FACT_TEXT","support":[
        {"event_id":"SURVIVING_EVENT_ID","start":0,"end":5,"quote":"EXACT"}]}],
      "forbidden_conclusions":["SEMANTIC PROHIBITION TEXT"],
      "forbidden_inferences":[{"error_type":"deleted_resurrection",
                                "text":"FORBIDDEN INFERENCE TEXT"}]
    }}}
Fill all 96 exact question keys. Positive support defaults to source_field
body; canonical string fields from tool captures are also accepted. Optional
reader_visible_equivalent:{source_field,start,end} must contain the exact
same quote in another field of the SAME source. Delete actions have no source
body to cite, and a deleted original cannot anchor a positive required fact.

forbidden_conclusions is semantic prose, not a typed gold:N witness reference.
forbidden_inferences creates typed gold:N references via error_type. The four
recognized types are false_merge, false_closure, project_mixing, and
 deleted_resurrection. Each holdout history needs at least one typed
 deleted_resurrection negative. This checks typed coverage, not whether its
meaningful scenario is well designed; independent semantic review remains.
No thresholds or critical-error rules are changed by this author contract.
"""

from pathlib import Path
import json
from typing import Any

def read(path: Path) -> Any:
    return json.loads(path.read_text())

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


def original_fields(event: dict) -> dict[str, str]:
    """Fields that a captured user, assistant or tool can supply as a source."""
    if event['type'] not in ('user', 'assistant', 'tool'):
        return {}
    fields = {'body': event.get('text', '')}
    capture = event.get('tool_capture')
    if event['type'] == 'tool' and capture:
        for container, names in (('tool_input', ('command', 'cmd')),
                                 ('tool_response', ('output', 'stdout', 'stderr'))):
            for name in names:
                value = capture.get(container, {}).get(name)
                if isinstance(value, str):
                    fields[container + '.' + name] = value
    return fields


def exact_span(fields: dict[str, str], support: dict, *, default_field: str = 'body',
               quote: str | None = None) -> str:
    field = support.get('source_field', default_field)
    if field not in fields:
        raise ValueError('positive support requires a captured canonical string field')
    start, end = support.get('start'), support.get('end')
    text = support.get('quote') if quote is None else quote
    if (type(start) is not int or type(end) is not int or
            not 0 <= start < end <= len(fields[field]) or
            not isinstance(text, str) or not text.strip() or fields[field][start:end] != text):
        raise ValueError('positive support requires exact field offsets and quote')
    return field


def validate_gold(value: dict, gold: dict) -> dict:
    """Validate source representability, not retrieval success or semantic truth.

    All supported questions are final-stage questions. Capture visits every
    event, applies deletion controls, then settles each project, including the
    tail after the last explicit checkpoint. An explicit checkpoint is not a
    requirement that every positive source must precede that checkpoint.
    """
    if gold.get('schema_version') != 1 or gold.get('fictional') is not True:
        raise ValueError('gold requires schema_version 1 and fictional true')
    expected = {history['id'] + '/' + question['id'] for history in value['histories']
                for question in history['questions']}
    if not isinstance(gold.get('questions'), dict) or set(gold['questions']) != expected:
        raise ValueError('gold must cover exactly the existing fixture questions')
    anchors, deletion_controls = [], 0
    roles = {'user': 'user_intent', 'assistant': 'assistant_report', 'tool': 'tool_record'}
    for history in value['histories']:
        history_deletions = 0
        typed_deletion_negative = False
        events, deleted, checkpoints = {}, set(), []
        sequence = 0
        for session in history['sessions']:
            project = session.get('project_key', history['project_keys'][0])
            local_positions = {event['id']: index for index, event in enumerate(session['events'])}
            checkpoint_positions = []
            for checkpoint in session.get('checkpoints', []):
                if checkpoint['after_event_id'] not in local_positions:
                    raise ValueError('author checkpoint must target its own session event')
                checkpoint_positions.append(local_positions[checkpoint['after_event_id']])
            if checkpoint_positions != sorted(set(checkpoint_positions)):
                raise ValueError('author checkpoints must follow unique session event order')
            for event in session['events']:
                sequence += 1
                capture = event.get('tool_capture')
                if (event['type'] == 'tool' and capture and
                        capture['tool_name'].split('.')[-1] == 'exec_command' and
                        capture['tool_response']['output'] != event.get('text', '')):
                    raise ValueError('exec_command output must equal event text')
                if event['type'] == 'delete_observation':
                    target = event.get('observation_event_id')
                    if target not in events or events[target]['event']['type'] not in roles:
                        raise ValueError('deletion must target an earlier captured source event')
                    if events[target]['project_key'] != project:
                        raise ValueError('deletion target must belong to the control project')
                    deleted.add(target)
                    deletion_controls += 1
                    history_deletions += 1
                events[event['id']] = {'event': event, 'project_key': project,
                    'session_id': session['id'], 'capture_sequence': sequence}
            for checkpoint in session.get('checkpoints', []):
                checkpoints.append({'project_key': project,
                    'after_event_id': checkpoint['after_event_id'],
                    'capture_sequence': events[checkpoint['after_event_id']]['capture_sequence']})
        if value['split'] == 'holdout' and history_deletions != 1:
            raise ValueError('trial holdout requires exactly sixteen deletion controls, one per history')
        for question in history['questions']:
            if not isinstance(question.get('question'), str) or not question['question'].strip():
                raise ValueError('question requires nonempty question text')
            case = history['id'] + '/' + question['id']
            selected = question.get('project_keys', [question['project_key']]
                if 'project_key' in question else history['project_keys'])
            entry = gold['questions'][case]
            if not isinstance(entry.get('category'), str) or not entry['category'].strip():
                raise ValueError('gold question requires a category string')
            inferences = entry.get('forbidden_inferences', [])
            if not isinstance(inferences, list) or any(not isinstance(item, dict) or
                    item.get('error_type') not in ('false_merge', 'false_closure', 'project_mixing', 'deleted_resurrection')
                    for item in inferences):
                raise ValueError('typed forbidden inferences require recognized error_type')
            typed_deletion_negative |= any(item['error_type'] == 'deleted_resurrection' for item in inferences)
            facts = entry.get('required_facts')
            if not isinstance(facts, list) or not facts:
                raise ValueError('every question requires positive facts')
            forbidden = gold['questions'][case].get('forbidden_conclusions', [])
            if not isinstance(forbidden, list) or any(not isinstance(text, str) for text in forbidden):
                raise ValueError('forbidden conclusions remain a separate string list')
            for index, fact in enumerate(facts):
                if not isinstance(fact.get('text'), str) or not fact['text'].strip():
                    raise ValueError('positive fact requires nonempty text')
                if not isinstance(fact.get('support'), list) or not fact['support']:
                    raise ValueError('positive fact requires at least one source anchor')
                for support in fact['support']:
                    event_id = support.get('event_id')
                    if event_id not in events:
                        raise ValueError('positive support refers to an unknown event')
                    record = events[event_id]
                    event = record['event']
                    if event['type'] not in roles:
                        raise ValueError('deletion controls are actions, not positive citation sources')
                    if event_id in deleted:
                        raise ValueError('positive support cannot require a deleted source')
                    if record['project_key'] not in selected:
                        raise ValueError('positive support must belong to a selected question project')
                    fields = original_fields(event)
                    field = exact_span(fields, support)
                    equivalent = support.get('reader_visible_equivalent')
                    if equivalent is not None:
                        # A mirror is another exact field of this SAME source.
                        if equivalent.get('event_id', event_id) != event_id:
                            raise ValueError('field mirror cannot select another source event')
                        exact_span(fields, equivalent, quote=support['quote'])
                    checkpoint = next((item for item in checkpoints
                        if item['project_key'] == record['project_key'] and
                        item['capture_sequence'] >= record['capture_sequence']), None)
                    anchors.append({'case': case, 'fact_index': index, 'event_id': event_id,
                        'source_role': roles[event['type']], 'source_field': field,
                        'start': support['start'], 'end': support['end'],
                        'project_key': record['project_key'], 'session_id': record['session_id'],
                        'question_visibility_stage': 'final',
                        'first_processing_opportunity': checkpoint or {'stage': 'final_settle'},
                        'survives_final_deletion': True})
        if value['split'] == 'holdout' and not typed_deletion_negative:
            raise ValueError('each trial history requires typed deleted_resurrection negative coverage')
    if value['split'] == 'holdout' and deletion_controls != 16:
        raise ValueError('trial holdout requires exactly sixteen deletion controls')
    return {'status': 'positive_source_anchors_representable',
        'histories': len(value['histories']), 'questions': len(expected),
        'positive_anchors': len(anchors), 'deletion_controls': deletion_controls,
        'anchors': anchors,
        'limits': ['Representability does not guarantee retrieval visibility, truth, or answer quality.',
            'A deletion action cannot be a positive cited fact. Deleted-source resurrection remains a forbidden conclusion or critical-error test.',
            'Surviving captured evidence may support a positive claim about a later state; this validator does not infer that state.',
            'Historical outcomes and prior fixture schema validity are unchanged.']}


def validate_files(fixture_path: Path, gold_path: Path) -> dict:
    return validate_gold(fixture(fixture_path), read(gold_path))


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser(description='Validate your fictional fixture and exact positive source anchors offline.')
    parser.add_argument('fixture_path', type=Path)
    parser.add_argument('gold_path', type=Path)
    args = parser.parse_args()
    print(json.dumps(validate_files(args.fixture_path, args.gold_path), indent=2))
