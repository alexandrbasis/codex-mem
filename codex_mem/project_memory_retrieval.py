"""Read-only, bounded project briefs shared by startup and explicit resume.

Selection never decides whether work is complete. The ledger's explicit state
and evidence remain visible, including provisional signals and conflicts.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
import json
from pathlib import Path
from typing import Any
from xml.sax.saxutils import escape, quoteattr

from .jev_retrieval import exact_constraints
from .privacy import redact_search_value
from .project_memory import _EXECUTION_TOOLS, _execution_result_signal, _fts_query
from .project_memory_source_transport import OriginalSourcePlan, source_qualifiers, qualifier_key, compact_selected_xml
from .retrieval import current_state_scope, historical_query, prompt_match_score, prompt_query_plan, resume_topic_plan, project_lexical_text
from .tool_io import is_private_prompt

_MAX_ITEMS = 100
_CLOSED = frozenset({'closed', 'completed', 'resolved', 'replaced', 'superseded', 'retracted', 'deleted'})
_OBLIGATIONS = frozenset({'obligation', 'requirement', 'task', 'open_question', 'question', 'constraint'})


def _text(item: Mapping[str, Any]) -> str:
    return '\n'.join((key + ': ' + str(item.get(key))) for key in (
        'text', 'summary', 'goal', 'rationale', 'outcome', 'open_work', 'prior_goal', 'prior_rationale', 'prior_outcome', 'prior_open_work', 'key', 'scope') if item.get(key))


def _scope_matches(item: Mapping[str, Any], environment: str | None, version: str | None,
                   project: str) -> bool:
    scope = item.get('scope') or {}
    if not isinstance(scope, Mapping):
        return False
    worktree = scope.get('worktree')
    if worktree and str(Path(str(worktree)).expanduser().resolve()) != project:
        return False
    for key, value in (('environment', environment), ('version', version)):
        # A scoped claim cannot silently become an assertion about a different
        # explicit environment/version. Unscoped claims remain project-wide.
        if value and scope.get(key):
            actual, requested = str(scope[key]).casefold(), value.casefold()
            if key == 'environment':
                aliases = {'prod': 'production', 'dev': 'development'}
                actual, requested = aliases.get(actual, actual), aliases.get(requested, requested)
            if key == 'version':
                actual, requested = actual.lstrip('v'), requested.lstrip('v')
            if actual != requested:
                return False
    return True


def _environment_evidence_matches(item: Mapping[str, Any], requested: Sequence[str],
                                  excluded: Sequence[str]) -> bool:
    """Filter declared scopes and execution reports, never classify user prose.

    A user constraint can name another environment without belonging to it.
    Unscoped user material remains unscoped; retaining its quotation does not
    establish an execution result in the requested environment.
    """
    from .project_memory_query import environment_names
    scope = item.get('scope') or {}
    declared = str(scope.get('environment') or '') if isinstance(scope, Mapping) else ''
    if declared:
        names = set(environment_names(declared)) or {declared.casefold()}
        if names.intersection(excluded) or requested and not names.intersection(requested):
            return False
    role = item.get('source_role')
    if not role:
        capture = str(item.get('source') or '')
        if capture == 'hook:PostToolUse' or capture.startswith('hook:PostToolUse:'):
            role = 'tool_record'
        elif capture == 'hook:Stop' or capture.startswith('hook:Stop:'):
            role = 'assistant_report'
    if role not in {'tool_record', 'assistant_report'}:
        if role == 'model_inference' and item.get('object_type') == 'claim':
            # A paraphrase cannot conceal the execution scope of its own
            # attributed evidence. User-only inferences remain unscoped.
            return all(_environment_evidence_matches(
                {'source_role': ref.get('source_role'), 'text': ref.get('quote') or ''},
                requested, excluded)
                for refs in (item.get('evidence') or {}).values() for ref in refs)
        return True
    # Declared metadata must not make a contradictory execution quotation
    # appear to mention the requested environment itself.
    text = _text(dict(item, scope={})) + '\n' + str(item.get('body') or '')
    observed = set(environment_names(text))
    if observed.intersection(excluded):
        return False
    return not (requested and observed and not observed.intersection(requested))


def _custody_scope_bindings(store: Any, project: str, sources: Mapping[str, Mapping[str, Any]],
                            exclude_session_id: str | None) -> dict[str, tuple[str, ...] | None]:
    """Read existing whole-source scope metadata, without classifying custody.

    A partial clause or paraphrase cannot assign its environment to the rest
    of a user source. Conflicting exact declarations leave custody unscoped.
    An over-cap lookup is an omission, never proof that no other scope exists.
    """
    from .project_memory_query import environment_names
    bindings: dict[str, tuple[str, ...] | None] = {}
    for source_id, source in sources.items():
        if not _safe(source, project, exclude_session_id):
            continue
        body = source.get('body')
        if not isinstance(body, str):
            continue
        with store._lock:
            rows = store._read(lambda: store._connection.execute(
                "SELECT o.data_json FROM pm_objects o JOIN pm_evidence p ON p.object_id=o.id "
                "WHERE o.project=? AND o.object_type='claim' AND p.source_id=? "
                "AND json_extract(o.data_json,'$.text')=? "
                "AND COALESCE(json_extract(o.data_json,'$.scope.worktree'),'') IN ('',?) "
                "AND (? IS NULL OR (COALESCE(o.session_id,'')!=? AND NOT EXISTS "
                "(SELECT 1 FROM pm_evidence hidden JOIN entries e ON e.id=hidden.source_id "
                "WHERE hidden.object_id=o.id AND e.session_id=?))) "
                "AND COALESCE(json_extract(o.data_json,'$.scope.environment'),'')!='' "
                "AND json_extract(o.data_json,'$.workflow_state') IN ('open','blocked','accepted','completed') "
                "AND (json_extract(o.data_json,'$.workflow_state')!='completed' "
                "OR json_extract(o.data_json,'$.kind') IN ('fact','result')) "
                "AND NOT EXISTS (SELECT 1 FROM pm_relations r WHERE r.project=o.project "
                "AND r.to_id=o.id AND (r.type='replaces' OR (r.type='closes' "
                "AND json_extract(o.data_json,'$.kind') NOT IN ('fact','result'))) "
                "AND (? IS NULL OR NOT EXISTS "
                "(SELECT 1 FROM pm_relation_evidence re JOIN entries e ON e.id=re.source_id "
                "WHERE re.relation_id=r.id AND e.session_id=?))) "
                "ORDER BY o.id LIMIT 65", (project, source_id, body, project,
                                          exclude_session_id, exclude_session_id, exclude_session_id,
                                          exclude_session_id, exclude_session_id)).fetchall())
        if len(rows) > 64:
            bindings[source_id] = None
            continue
        environments = set()
        for row in rows:
            claim = json.loads(row[0])
            if not (_safe(claim, project, exclude_session_id) and
                    _scope_matches(claim, None, None, project)):
                continue
            refs = [ref for values in (claim.get('evidence') or {}).values() for ref in values]
            if (claim.get('source_role') != 'user_intent' or not refs or
                    any(key.startswith('prior_') for key in claim) or
                    any(ref.get('source_id') != source_id or ref.get('source_role') != 'user_intent'
                        or ref.get('source_field', 'body') != 'body' for ref in refs)):
                continue
            if not any(ref.get('source_id') == source_id and ref.get('source_role') == 'user_intent'
                       and ref.get('start') == 0 and ref.get('end') == len(body) and ref.get('quote') == body
                       for ref in (claim.get('evidence') or {}).get('text', [])):
                continue
            declared = str(claim['scope']['environment'])
            names = environment_names(declared)
            environments.add(names[0] if len(names) == 1 else declared.casefold())
        bindings[source_id] = tuple(sorted(environments))
    return bindings


def _safe(item: Any, project: str, exclude_session_id: str | None) -> bool:
    if not isinstance(item, Mapping):
        return False
    if item.get('project') and str(item['project']) != project:
        return False
    if exclude_session_id:
        def has_session(value: Any) -> bool:
            if isinstance(value, Mapping):
                return (value.get('session_id') == exclude_session_id or
                        exclude_session_id in (value.get('source_session_ids') or []) or
                        any(has_session(nested) for nested in value.values()))
            if isinstance(value, list):
                return any(has_session(nested) for nested in value)
            return False
        if has_session(item):
            return False
    if item.get('private') or item.get('deleted') or item.get('redacted'):
        return False
    # Examine original evidence before presentation redaction hides a marker.
    return not is_private_prompt(json.dumps(dict(item), ensure_ascii=False, default=str))


def _query_score(item: Mapping[str, Any], query: str, project: str) -> int:
    if not query.strip() or current_state_scope(query, project) is not None:
        return 1
    text = _text(item)
    constraints = exact_constraints(query, project)
    if not prompt_match_score(text, constraints) and (constraints['identifiers'] or constraints['versions']):
        return 0
    plan = resume_topic_plan(query) or prompt_query_plan('How ' + query)
    if plan is not None:
        plan = dict(plan, terms=[(term, term[:-1] if len(term) > 4 and term.isascii() and
                                term.endswith('s') and not term.endswith('ss') else stem)
                               for term, stem in plan['terms']])
    return prompt_match_score(project_lexical_text(text), plan) if plan else 0


def _signal_allowed(item: Mapping[str, Any]) -> bool:
    if item.get('source_role') == 'user_intent':
        return True
    if item.get('source_role') != 'tool_record' or item.get('signal_kind') != 'observed_execution_result':
        return False
    provenance = item.get('provenance') or {}
    if not isinstance(provenance, Mapping) or type(provenance.get('exit_code')) is not int:
        return False
    if not all(isinstance(provenance.get(key), str) and provenance[key]
               for key in ('tool_use_id', 'tool_name', 'command', 'result_field')):
        return False
    refs = (item.get('evidence') or {}).get('execution_result') or []
    return any(isinstance(ref, Mapping) and ref.get('source_role') == 'tool_record'
               and ref.get('source_id') == item.get('source_id') and
               ref.get('source_field') in {'tool_response.output', 'tool_response.stdout', 'tool_response.stderr'}
               for ref in refs)


def _continuity_context(store: Any, project: str, episodes: list[dict[str, Any]],
                        exclude_session_id: str | None) -> dict[str, list[dict[str, Any]]]:
    """Read adjacent explicit task-boundary statements from an already cited user event.

    This never searches a session or adds another event by proximity. The
    returned quote remains attributed to that exact source, not a task link.
    """
    import re
    from .privacy import redact_search_text
    refs_by_id: dict[str, list[tuple[str, Mapping[str, Any]]]] = {}
    for episode in episodes:
        evidence = episode.get('evidence') or {}
        for field in ('continuation_of', 'goal'):
            for pointer in evidence.get(field, []):
                if pointer.get('source_role') == 'user_intent' and pointer.get('source_id'):
                    refs_by_id.setdefault(pointer['source_id'], []).append((episode['id'], pointer))
    if not refs_by_id:
        return {}
    try:
        sources = store.get(project, list(refs_by_id)[:8])
    except ValueError:
        return {}
    result: dict[str, list[dict[str, Any]]] = {}
    relation = re.compile(r'\b(?:distinct|separate|different|independent)\s+task\b|\bnot\s+a\s+continuation\b|отдельн[а-яё]*\s+задач|другая\s+задача|не\s+продолж', re.I)
    for source in sources:
        if not _safe(source, project, exclude_session_id) or not str(source.get('source') or '').startswith('hook:UserPromptSubmit'):
            continue
        body = source.get('body')
        if not isinstance(body, str):
            continue
        for episode_id, pointer in refs_by_id.get(source['id'], []):
            end = pointer.get('end')
            if type(end) is not int:
                continue
            # At most two neighboring explicit boundary statements and 500
            # source characters, from the same persisted user message only.
            window = body[max(0, end):max(0, end) + 500]
            matches = list(re.finditer(r'[^.!?]+(?:[.!?]|$)', window))
            for match in [match for match in matches if relation.search(match.group())][:2]:
                start = max(0, end) + match.start()
                stop = max(0, end) + match.end()
                quote = body[start:stop]
                pointer = {'source_id': source['id'], 'start': start,
                    'end': stop, 'quote_excerpt': redact_search_text(quote[:240]),
                    'quote_truncated': len(quote) > 240, 'source_role': 'user_intent',
                    'field': 'source_context', 'context_basis': 'same_source_adjacent_user_statement'}
                existing = result.setdefault(episode_id, [])
                if not any(all(value.get(key) == pointer[key] for key in ('source_id', 'start', 'end')) for value in existing):
                    existing.append(pointer)
    return result


def _xml_text(value: Any) -> str:
    # XML 1.0 disallows control characters even when escaped.
    return ''.join(c for c in str(value) if c in '\t\n\r' or
                   0x20 <= ord(c) <= 0xD7FF or 0xE000 <= ord(c) <= 0xFFFD or
                   0x10000 <= ord(c) <= 0x10FFFF)


def _attr(value: Any) -> str:
    return quoteattr(_xml_text(value))


def _evidence(item: Mapping[str, Any], limit: int = 8, *, quote_limit: int = 240) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    def visit(value: Any, field: str = '') -> None:
        if isinstance(value, Mapping):
            if value.get('source_id'):
                pointer = {key: value[key] for key in ('source_id', 'start', 'end', 'source_role', 'tool_use_id', 'source_field') if key in value}
                if field:
                    pointer['field'] = field
                if isinstance(value.get('quote'), str):
                    pointer['quote_excerpt'] = value['quote'][:quote_limit]
                    pointer['quote_truncated'] = len(value['quote']) > quote_limit
                if pointer not in found:
                    found.append(pointer)
            else:
                for key, nested in value.items():
                    visit(nested, str(key))
        elif isinstance(value, list):
            for nested in value:
                visit(nested, field)
    visit(item.get('evidence'))
    if item.get('source_id'):
        found.append({'source_id': item['source_id']})
    priority = {'text': 0, 'open_work': 0, 'outcome': 1, 'rationale': 2, 'goal': 3, 'summary': 4, 'workflow_state': 5}
    found.sort(key=lambda pointer: priority.get(pointer.get('field'), 6))
    unique: list[dict[str, Any]] = []
    for pointer in found:
        existing = next((value for value in unique if all(value.get(key) == pointer.get(key)
                        for key in ('source_id', 'start', 'end', 'source_field', 'tool_use_id'))), None)
        if existing is None:
            unique.append(pointer)
        elif pointer.get('field') and pointer['field'] not in existing.get('field', '').split(','):
            existing['field'] = ','.join(filter(None, (existing.get('field'), pointer['field'])))
    return unique[:limit]


def _record(item: Mapping[str, Any], lane: str) -> dict[str, Any]:
    cleaned = redact_search_value(dict(item))
    assert isinstance(cleaned, dict)
    # Preserve provenance and status but bound every narrative field. Sources
    # use bounded excerpts; full sources are opened separately.
    record = {key: cleaned[key] for key in ('id', 'key', 'kind', 'workflow_state', 'scope',
        'source_role', 'evidence_basis', 'requested_state', 'revision', 'session_id', 'seq', 'provisional',
        'source_capture_seq_min', 'source_capture_seq_max', 'capture_order_basis',
        'logical_episode_id', 'episode_version', 'parent_episode_id', 'is_current_head', 'continuity_basis',
        'signal_kind', 'selection_basis', 'relationship_to_query', 'prior_context_status', 'retained_context_fields_omitted',
        'disposition', 'is_requirement_claim', 'meaning', 'uncertainty') if key in cleaned}
    record.update(project=cleaned.get('project'), source='project_memory',
                  created_at=cleaned.get('created_at'), updated_at=cleaned.get('updated_at'), tags=[],
                  source_ids=list(dict.fromkeys(source['source_id'] for source in _evidence(cleaned))),
                  provenance={'verification': 'not_assessed', 'record_role': 'project_memory_' + lane},
                  lane=lane, title=str(cleaned.get('key') or lane)[:160],
                  preview=_text(cleaned)[:1400], evidence=_evidence(cleaned))
    record['fields'] = []
    for name in ('text', 'open_work', 'outcome', 'summary', 'goal', 'rationale', 'prior_open_work', 'prior_rationale', 'prior_goal', 'prior_outcome'):
        value = cleaned.get(name)
        if isinstance(value, str) and value:
            refs = (cleaned.get('evidence') or {}).get(name, [])
            if lane == 'signal' and cleaned.get('source_role') == 'user_intent' and cleaned.get('source_id'):
                refs = [{'source_id': cleaned['source_id'], 'source_role': 'user_intent'}]
            provenance = (cleaned.get('field_provenance') or {}).get(name, {})
            grounded = provenance.get('basis') == 'attributed_source_excerpts'
            record['fields'].append({'name': name, 'text': value if grounded else value[:1400],
                **({key: provenance[key] for key in ('basis', 'field_association', 'partial_context')
                    if key in provenance} if grounded else {}),
                'evidence': _evidence({'evidence': {name: refs}},
                    limit=32 if grounded or name.startswith('prior_') else 8,
                    quote_limit=6000 if grounded else 240)})
    if cleaned.get('field_provenance') and lane == 'episode':
        record['preview'] = 'Attributed source excerpts; field association remains inferred.'
    if lane == 'signal' and cleaned.get('signal_kind') == 'observed_execution_result':
        provenance = cleaned.get('provenance') or {}
        execution = {key: (value[:512] if isinstance(value, str) else value)
            for key, value in provenance.items() if key in {'tool_use_id', 'tool_name', 'command',
                'exit_code', 'result_field', 'capture_recorded_at'}
            and (isinstance(value, str) or type(value) is int)}
        record['execution'] = execution
        record['provenance']['execution'] = execution
        record['fields'] = []
        for name, text in (('execution_result', provenance.get('result_excerpt')),
                           ('execution_command', provenance.get('command'))):
            refs = (cleaned.get('evidence') or {}).get(name, [])
            if isinstance(text, str) and refs:
                record['fields'].append({'name': name, 'text': text[:700],
                    'evidence': _evidence({'evidence': {name: refs}})})
    if lane == 'signal':
        record['provisional'] = True
        record['workflow_state'] = 'preliminary'
        if type(record.get('seq')) is int:
            record.update(source_capture_seq_min=record['seq'], source_capture_seq_max=record['seq'],
                          capture_order_basis='local_capture_sequence_not_event_time')
    if item.get('source_role') == 'model_inference' or lane == 'episode':
        record['inferred'] = True
        record.setdefault('source_role', 'model_inference')
    if isinstance(record.get('scope'), Mapping):
        record['scope'] = {key: str(value)[:512] for key, value in record['scope'].items()
                           if key in {'environment', 'version', 'worktree'}}
    record['kind'] = 'project_memory_' + lane
    record['project_memory_kind'] = cleaned.get('kind')
    return record


def _source_evidence(store: Any, project: str, query_plan: Any, exclude_session_id: str | None,
                     eligible: Any, relevant: Any, environment: str | None, version: str | None, *,
                     unassociated_context: bool = False) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Read bounded original roles independently of extraction, without new state."""
    limit, candidate_limit, excerpt_limit = 2, 16, 1200
    def source_expression(candidate: str) -> str:
        expression = _fts_query(candidate, project, topic_query=False)
        from .project_memory_query import requests_source_evidence
        if not (requests_source_evidence(query_plan.content_query) or
                'execution_assessment' in query_plan.answer_facets):
            return expression
        # Ordinary source words get the same bounded prefix window as topic
        # reads. Literal identifiers and versions retain their exact spelling.
        anchors = exact_constraints(candidate, project)
        literal = {str(value).casefold() for value in [*anchors['identifiers'], *anchors['versions']]}
        import re
        def ordinary(match: Any) -> str:
            word = match.group(1)
            if word.casefold() in literal or not re.fullmatch(r'[a-z]{4,}', word):
                return match.group()
            stem = word[:-1] if len(word) > 4 and word.endswith('s') and not word.endswith(('ss', 'us', 'is')) else word
            variants = project_lexical_text(stem).split()
            return '(' + ' OR '.join('"' + value + '"*' for value in dict.fromkeys(variants)) + ')'
        return re.sub(r'"([^"\n]+)"', ordinary, expression)
    expressions = [] if unassociated_context else [source_expression(candidate) for candidate in query_plan.candidate_queries]
    match = ''
    parameters: list[Any] = [project, exclude_session_id, exclude_session_id]
    if any(expressions):
        match = ' AND (' + ' OR '.join(
            'e.rowid IN (SELECT rowid FROM entries_fts WHERE entries_fts MATCH ?)' for expression in expressions if expression) + ')'
        parameters.extend(expression for expression in expressions if expression)
    elif not unassociated_context and not query_plan.generic_project_state and any(query_plan.candidate_queries):
        return [], {'returned': 0, 'omitted': 0, 'roles': {}}
    roles = {'assistant_report': 'hook:Stop', 'tool_record': 'hook:PostToolUse'}
    selected, coverage = {}, {}
    def scoped(value: Mapping[str, Any]) -> bool:
        from .project_memory_query import environment_names
        text = str(value.get('text') or value.get('body') or '')
        requested = environment_names(environment) if environment else ()
        if not _environment_evidence_matches(value, requested, query_plan.excluded_environments):
            return False
        if version:
            try:
                declared = exact_constraints(text, project)['versions']
            except ValueError:
                return False
            if declared and not prompt_match_score(text, {'identifiers': [], 'versions': [version], 'terms': [], 'minimum': 0}):
                return False
        return query_plan.matches(value) and (relevant(value) or unassociated_context)
    def read():
        connection = store._connection
        for role, source in roles.items():
            predicate = (" FROM entries e JOIN pm_events v ON v.source_id=e.id AND v.project=e.project "
                "WHERE e.project=? AND (? IS NULL OR COALESCE(e.session_id,'')!=?) "
                "AND e.superseded_by IS NULL" + match + " AND (e.source=? OR e.source LIKE ?)")
            args = (*parameters, source, source + ':%')
            total = connection.execute('SELECT COUNT(*)' + predicate, args).fetchone()[0]
            order = 'ASC' if query_plan.source_order == 'earliest' else 'DESC'
            columns = ('e.id,e.project,e.title,e.kind,e.session_id,e.source,e.tags_json,e.created_at,'
                'e.superseded_by,length(e.body) source_chars,'
                'CASE WHEN length(e.body)<=1200 THEN e.body ELSE NULL END body,v.seq'
                if unassociated_context else 'e.*,v.seq')
            rows = connection.execute('SELECT ' + columns + predicate + ' ORDER BY v.seq ' + order + ',e.id LIMIT ?',
                                      (*args, candidate_limit)).fetchall()
            records, oversized, invalid = [], 0, 0
            for row in rows:
                original = dict(row)
                if unassociated_context and original['body'] is None:
                    oversized += 1
                    continue
                if not eligible(original):
                    invalid += 1
                    continue
                if role == 'assistant_report':
                    body = original['body']
                    if not body or len(body) > excerpt_limit:
                        oversized += 1
                        continue
                    value = dict(original, text=body, source_role=role, source_id=original['id'])
                    if not scoped(value):
                        invalid += 1
                        continue
                    record = _record(value, 'source_evidence')
                    record['fields'] = [{'name': 'assistant_report', 'text': '',
                        'basis': 'attributed_source_excerpts', 'field_association': 'original_source',
                        'evidence': [{'source_id': original['id'], 'start': 0, 'end': len(body),
                            'source_role': role, 'quote_excerpt': redact_search_value(body), 'quote_truncated': False,
                            'excerpt_status': 'complete_source', 'source_chars': len(body)}]}]
                else:
                    signal = _execution_result_signal(connection, project, row)
                    if signal is None or not scoped(signal):
                        invalid += 1
                        continue
                    if signal['truncated']:
                        oversized += 1
                        continue
                    value = signal
                    record = _record(signal, 'signal')
                    record.update(lane='source_evidence', kind='project_memory_source_evidence')
                    record['provenance']['record_role'] = 'project_memory_source_evidence'
                    record['execution'].update({key: signal['provenance'][key] for key in ('command_field', 'exit_field')})
                    record.pop('workflow_state', None)
                    record.pop('provisional', None)
                    for field in record['fields']:
                        field.update(text='', basis='attributed_source_excerpts', field_association='original_execution_capture')
                        refs = signal['evidence'][field['name']]
                        field['evidence'] = _evidence({'evidence': {field['name']: refs}}, quote_limit=excerpt_limit)
                        for ref in field['evidence']:
                            ref.update(excerpt_status='complete_capture_field', source_chars=ref['end'] - ref['start'])
                if unassociated_context:
                    record.update(selection_basis='unassociated_source_context', relationship_to_query='unresolved',
                                  is_requirement_claim=False, meaning='original_source_without_task_association')
                record.update(project=project, source_capture_seq_min=row['seq'], source_capture_seq_max=row['seq'],
                              capture_order_basis='local_capture_sequence_not_event_time')
                if len(records) < limit:
                    records.append(record)
            selected[role] = records
            coverage[role] = {'matched': total, 'candidates': len(rows), 'returned': len(records),
                              'omitted': total - len(records), 'oversized': oversized, 'invalid': invalid}
    with store._lock:
        store._read(read)
    records = [selected[role][index] for index in range(limit) for role in roles if index < len(selected[role])]
    return records, {'returned': len(records), 'omitted': sum(value['omitted'] for value in coverage.values()),
                     'roles': coverage, 'candidate_limit_per_role': candidate_limit, 'returned_limit_per_role': limit,
                     'excerpt_char_limit': excerpt_limit, 'verification': 'not_assessed',
                     'source_order': query_plan.source_order or 'latest',
                     'order_basis': 'eligible_local_capture_sequence_not_event_time'}


def _source_xml(source: Mapping[str, Any], *, complete: bool = False) -> str:
    source = dict(source)
    source.setdefault('source_field', 'body')
    if (complete and source.get('source_role') != 'tool_record' and
            source.get('quote_truncated') is False and isinstance(source.get('quote_excerpt'), str) and
            type(source.get('start')) is int and type(source.get('end')) is int and
            source['end'] - source['start'] == len(source['quote_excerpt'])):
        source.pop('quote_truncated')
    if not complete and isinstance(source.get('quote_excerpt'), str):
        source['quote_truncated'] = bool(source.get('quote_truncated') or len(source['quote_excerpt']) > 160)
        source['quote_excerpt'] = source['quote_excerpt'][:160]
    excerpt = source.pop('quote_excerpt', None)
    opening = '<source' + ''.join(' ' + key + '=' + _attr(value) for key, value in source.items())
    if excerpt is None:
        return opening + '/>'
    return opening + '><quote_excerpt>' + escape(_xml_text(excerpt)) + '</quote_excerpt></source>'


def _item_attrs(record: Mapping[str, Any], projection: str | None) -> str:
    attrs = ''.join(' ' + key + '=' + _attr(record[key]) for key in
                    ('id', 'lane', 'kind', 'project_memory_kind', 'workflow_state', 'requested_state', 'source_role', 'evidence_basis', 'provisional', 'inferred',
                     'revision', 'source_capture_seq_min', 'source_capture_seq_max', 'capture_order_basis',
                     'logical_episode_id', 'episode_version', 'parent_episode_id', 'is_current_head', 'continuity_basis',
                     'context_episode_version', 'context_is_current_head',
                     'signal_kind', 'selection_basis', 'relationship_to_query', 'prior_context_status', 'retained_context_fields_omitted',
                     'disposition', 'is_requirement_claim', 'meaning', 'uncertainty', 'session_applicability',
                     'source_bundle', 'source_count') if key in record and record[key] is not None)
    if record.get('source_bundle') or record.get('kind') == 'project_memory_' + str(record.get('lane')):
        attrs = attrs.replace(' kind=' + _attr(record.get('kind')), '')
    if projection == 'continuity' and record.get('session_id'):
        attrs += ' session_id=' + _attr(record['session_id'])
    if record.get('lane') in {'signal', 'claim', 'source_evidence'} and record.get('source_capture_seq_min') == record.get('source_capture_seq_max'):
        for key in ('source_capture_seq_min', 'source_capture_seq_max'):
            attrs = attrs.replace(' ' + key + '=' + _attr(record.get(key)), '')
        if record.get('source_capture_seq_max') is not None:
            attrs += ' source_capture_seq=' + _attr(record['source_capture_seq_max'])
    scope = record.get('scope') or {}
    attrs += ''.join(' ' + key + '=' + _attr(scope[key])
                     for key in ('environment', 'version', 'worktree') if scope.get(key))
    return attrs


def _reserved_record_size(record: Mapping[str, Any], projection: str | None,
                          seen: set[tuple[Any, ...]], maximum: int) -> int:
    """Reserve complete useful groups; oversized groups cannot hide small reasons."""
    if record.get('lane') == 'source_evidence' and record.get('execution'):
        node = _execution_xml(record, projection)
        if not node or len(node) > maximum:
            return 0
        for field in record['fields']:
            seen.update((ref.get('source_id'), ref.get('start'), ref.get('end'), ref.get('source_role'))
                        for ref in field['evidence'])
        return len(node)
    if record.get('source_overlap_with_material'):
        return 0
    groups = []
    for field in record.get('fields', []):
        name = field['name'].removesuffix('_source_excerpt')
        if name not in {'text', 'open_work', 'prior_open_work', 'rationale', 'prior_rationale', 'source_custody', 'assistant_report'}:
            continue
        refs = field.get('evidence') or []
        identities = {(ref.get('source_id'), ref.get('start'), ref.get('end'), ref.get('source_role')) for ref in refs}
        if not identities or identities.issubset(seen):
            continue
        if field.get('basis') != 'attributed_source_excerpts':
            size = 900
        else:
            metadata = ''.join(' ' + key + '=' + _attr(field[key]) for key in
                               ('basis', 'field_association', 'partial_context') if key in field)
            size = len('<field name=' + _attr(field['name']) + metadata + '>' +
                       ''.join(_source_xml(ref, complete=True) for ref in refs) + '</field>')
        groups.append((size, identities))
    overhead = len('<item' + _item_attrs(record, projection) + ' fields_omitted="99"></item>')
    if overhead + sum(size for size, _ in groups) > maximum:
        groups = [min(groups, key=lambda group: group[0])] if groups else []
    total = overhead + sum(size for size, _ in groups)
    if not groups or total > maximum:
        return 0
    for _, identities in groups:
        seen.update(identities)
    return total


def _interval_covered(pointer: Mapping[str, Any], intervals: list[tuple[Any, ...]]) -> bool:
    start, end = pointer.get('start'), pointer.get('end')
    if type(start) is not int or type(end) is not int:
        return False
    key = (pointer.get('source_id'), pointer.get('source_field') or 'body', pointer.get('source_role'))
    cursor = start
    for left, right in sorted((left, right) for identity, left, right in intervals if identity == key):
        if left > cursor:
            break
        cursor = max(cursor, right)
    return cursor >= end


def _compact_custody_size(record: Mapping[str, Any], projection: str | None) -> int:
    """Reserve a bounded whole-source fallback, never a shortened quotation."""
    if record.get('lane') != 'intent_custody':
        return 0
    if record.get('source_overlap_with_material') and not record.get('source_fallback_for_reason'):
        return 0
    fields = record.get('fields') or []
    if len(fields) != 1 or len(fields[0].get('evidence', [])) != 1:
        return 0
    field, pointer = fields[0], fields[0]['evidence'][0]
    quote = pointer.get('quote_excerpt')
    if not isinstance(quote, str) or len(quote) > 600 or pointer.get('quote_truncated'):
        return 0
    metadata = ''.join(' ' + key + '=' + _attr(field[key]) for key in
                       ('basis', 'field_association', 'partial_context') if key in field)
    return len('<item' + _item_attrs(record, projection) + '><field name=' + _attr(field['name']) + metadata +
               '>' + _source_xml(pointer, complete=True) + '</field></item>')


def _execution_xml(record: Mapping[str, Any], projection: str | None) -> str:
    """Serialize one complete original execution for both fit and emission.

    Original capture pointers are children of the item. A common association
    qualifier replaces identical per-field wrappers. Exact native pairs carry
    command and output field identity on their own citable pointers. Their
    duplicate field labels and lengths need no second encoding. Canonical
    command and output quotations always remain in the same atomic group.
    """
    if record.get('lane') != 'source_evidence' or not record.get('execution'):
        return ''
    fields = record.get('fields') or []
    if not fields or any(not field.get('evidence') or any(ref.get('quote_truncated')
            for ref in field['evidence']) for field in fields):
        return ''
    compact = all(field.get('basis') == 'attributed_source_excerpts' and
                  field.get('field_association') == 'original_execution_capture' for field in fields)
    original = record['execution']
    pointers = [ref for field in fields for ref in field['evidence']]
    native_pair = (compact and len(pointers) == 2 and
        str(original.get('tool_name', '')).lower() in _EXECUTION_TOOLS and
        original.get('command_field') in {'tool_input.cmd', 'tool_input.command'} and
        original.get('result_field') in {'tool_response.output', 'tool_response.stdout', 'tool_response.stderr'} and
        original.get('exit_field') in {'tool_response.exit_code', 'tool_response.exitCode',
                                      'tool_response.returncode', 'tool_response.return_code'} and
        type(original.get('exit_code')) is int and bool(original.get('tool_use_id')) and bool(record.get('id')) and
        {ref.get('source_field') for ref in pointers} == {original['command_field'], original['result_field']} and
        all(ref.get('source_id') == record.get('id') and ref.get('source_role') == 'tool_record' and
            ref.get('tool_use_id') == original['tool_use_id'] and type(ref.get('start')) is int and ref['start'] == 0 and
            type(ref.get('end')) is int and isinstance(ref.get('quote_excerpt'), str) and
            ref['end'] == len(ref['quote_excerpt']) and not ref.get('quote_truncated') and
            ref.get('excerpt_status') == 'complete_capture_field' for ref in pointers) and
        next(ref['quote_excerpt'] for ref in pointers if ref['source_field'] == original['command_field']) == original.get('command'))
    attrs = _item_attrs(record, projection)
    if compact:
        attrs += ' field_association="original_execution_capture"'
    execution = '<execution' + ''.join(' ' + key + '=' + _attr(value)
        for key, value in original.items() if key != 'capture_recorded_at' and
        not (native_pair and key in {'command_field', 'result_field'})) + '/>'
    content = ''
    for field in fields:
        refs = []
        for ref in field['evidence']:
            displayed = dict(ref)
            if native_pair:
                displayed.pop('field', None)
                if type(displayed.get('source_chars')) is int and displayed['source_chars'] == displayed['end']:
                    displayed.pop('source_chars')
            refs.append(displayed)
        sources = ''.join(_source_xml(ref, complete=True) for ref in refs)
        if compact:
            content += sources
        else:
            metadata = ''.join(' ' + key + '=' + _attr(field[key]) for key in
                               ('basis', 'field_association', 'partial_context') if key in field)
            content += '<field name=' + _attr(field['name']) + metadata + '>' + sources + '</field>'
    return '<item' + attrs + '>' + execution + content + '</item>'


def _whole_execution_size(record: Mapping[str, Any], projection: str | None) -> int:
    """The exact complete-node size, with no separately maintained estimate."""
    return len(_execution_xml(record, projection))


def _bundle_field_xml(field: Mapping[str, Any]) -> str:
    return '<field>' + ''.join(
        (_source_reference_xml(pointer['_reference'], pointer) if '_reference' in pointer else
         _source_xml({key: value for key, value in pointer.items() if key != 'field'}, complete=True))
        for pointer in field['evidence']) + '</field>'


def _source_reference_xml(match: tuple[str, bool, Mapping[str, Any]], pointer: Mapping[str, Any], *,
                          association_field: str | None = None) -> str:
    """Share proven quote identity, retaining every other association qualifier."""
    resolved = match[2]
    identity = {'source_id', 'source_role', 'source_field', 'tool_use_id',
                'start', 'end', 'quote_excerpt', 'id', 'ref', '_reference'}
    source_facts = {'quote_truncated', 'excerpt_status', 'source_chars', 'source_capture_seq'}
    qualifiers = {}
    for key, value in pointer.items():
        if key in identity:
            continue
        if key == 'field' and association_field and value == association_field.removesuffix('_source_excerpt'):
            continue
        if (key in source_facts and key in resolved and
                type(value) is type(resolved[key]) and value == resolved[key]):
            continue
        qualifiers[key] = value
    return '<quote_ref ref=' + _attr(match[0]) + (
        '' if match[1] else ' start=' + _attr(pointer['start']) + ' end=' + _attr(pointer['end'])) + ''.join(
        ' ' + key + '=' + _attr(value) for key, value in qualifiers.items()) + '/>'


def _role_original_xml(record: Mapping[str, Any], projection: str | None) -> str:
    """The complete role-original string used for both pricing and emission."""
    if record.get('execution'):
        return _execution_xml(record, projection)
    if record.get('lane') != 'source_evidence' or record.get('source_role') != 'assistant_report':
        return ''
    content = ''
    for field in record.get('fields', []):
        refs = field.get('evidence') or []
        if not refs or any(ref.get('quote_truncated') for ref in refs):
            return ''
        metadata = ''.join(' ' + key + '=' + _attr(field[key]) for key in
                           ('basis', 'field_association', 'partial_context') if key in field)
        content += '<field name=' + _attr(field['name']) + metadata + '>' + ''.join(
            _source_xml(ref, complete=True) for ref in refs) + '</field>'
    return '<item' + _item_attrs(record, projection) + '>' + content + '</item>' if content else ''


def _quoted_claim_association_xml(record: Mapping[str, Any], plan: OriginalSourcePlan,
                                  projection: str | None, identifiers: set[str]) -> str:
    """Price a complete typed association whose exact originals already fit.

    This preserves recorded state and scope. It does not infer currentness or
    replace independent prose with quotations that merely resemble it.
    """
    if record.get('lane') != 'claim' or 'workflow_state' not in record:
        return ''
    fields = []
    for field in record.get('fields', []):
        refs = field.get('evidence') or []
        value = str(field.get('text') or '')
        if (not refs or field.get('basis') != 'attributed_source_excerpts' and
                value and not _source_value_repeated(value, refs)):
            return ''
        resolved = [plan.find(ref, source_qualifiers(record), same_context=False) for ref in refs]
        if not all(match and match[2].get('id') in identifiers for match in resolved):
            return ''
        metadata = ''.join(' ' + key + '=' + _attr(field[key]) for key in
                           ('basis', 'field_association', 'partial_context') if key in field)
        fields.append('<field name=' + _attr(field['name']) + metadata + '>' + ''.join(
            _source_reference_xml(match, ref, association_field=field['name'])
            for ref, match in zip(refs, resolved)) + '</field>')
    return '<item' + _item_attrs(record, projection) + '>' + ''.join(fields) + '</item>' if fields else ''


def _role_quote_ids(record: Mapping[str, Any], plan: OriginalSourcePlan) -> dict[str, Any]:
    index = len(plan.entries)
    fields = []
    for field in record.get('fields', []):
        refs = []
        for ref in field.get('evidence', []):
            index += 1
            refs.append({**ref, 'id': 's' + str(index)})
        fields.append({**field, 'evidence': refs})
    return {**record, 'fields': fields}


def _compact_coverage_xml(node: str) -> str:
    """Drop optional zero counts and a processing watermark, never risk flags."""
    import re
    required = {'complete_transcript', 'observations_only', 'pending', 'coverage_gaps',
                'pending_reconciliation'}
    attributes = re.findall(r' ([a-z_]+)="([^"]*)"', node)
    return '<coverage' + ''.join(' ' + key + '=' + _attr(value) for key, value in attributes
        if key in required or key != 'processed_through_seq' and value != '0') + '/>'


def _omissions_xml(omissions: Mapping[str, int]) -> str:
    if not any(omissions.values()):
        return ''
    return '<omitted records=' + _attr(omissions['records']) + ' fields=' + _attr(omissions['fields']) + ''.join(
        ' ' + key + '=' + _attr(omissions[key]) for key in
        ('user_sources', 'unassociated_user_sources', 'original_context_sources') if omissions.get(key)) + '/>'


def _select_bundle_fields(record: Mapping[str, Any], maximum: int,
                          projection: str | None) -> tuple[list[dict[str, Any]], int]:
    """Choose bounded complete originals, newest first; present capture order."""
    fields = list(record.get('fields') or [])
    selected = []
    attrs = _item_attrs(record, projection)
    for field in sorted(fields, key=lambda field: -int(field['evidence'][0].get('source_capture_seq') or 0)):
        pointer = field['evidence'][0]
        quote = pointer.get('quote_excerpt')
        if (len(selected) >= 8 or not isinstance(quote, str) or len(quote) > 600 or
                pointer.get('quote_truncated')):
            continue
        trial = selected + [field]
        omitted = len(fields) - len(trial)
        marker = ' fields_omitted=' + _attr(omitted) if omitted else ''
        group = {'evidence': [pointer for item in trial for pointer in item['evidence']]}
        size = len('<item' + attrs + marker + '>' + _bundle_field_xml(group) + '</item>')
        if size <= maximum:
            selected = trial
    selected.sort(key=lambda field: int(field['evidence'][0].get('source_capture_seq') or 0))
    return selected, len(fields) - len(selected)


def _complete_source_groups(records: list[Mapping[str, Any]], *, user_only: bool = False) -> list[dict[str, Any]]:
    """Coalesce only consistent, complete intervals of an already selected source.

    These are source excerpts, not verified field associations or executions.
    Scope and uncertainty stay with each record; distinct source IDs never join.
    """
    groups: dict[tuple[Any, ...], dict[str, Any]] = {}
    custody_ids = {ref.get('source_id') for record in records if record.get('lane') == 'intent_custody'
                   for field in record.get('fields', []) for ref in field.get('evidence', [])}
    native_ids = {record.get('id') for record in records if record.get('execution')}
    historical_ids = {ref.get('source_id') for record in records for field in record.get('fields', [])
                      if field.get('name', '').startswith('prior_') for ref in field.get('evidence', [])}
    for record in records:
        if not user_only and record.get('lane') != 'episode':
            continue
        qualifiers = source_qualifiers(record)
        custody = user_only and record.get('lane') == 'intent_custody'
        if custody:
            qualifiers.update({key: record[key] for key in ('source_role', 'disposition',
                'is_requirement_claim', 'meaning', 'uncertainty') if key in record})
        if user_only and record.get('lane') == 'episode' and record.get('is_current_head') is True:
            qualifiers.update({('context_' + key): record[key] for key in
                ('episode_version', 'is_current_head') if key in record})
        for field in record.get('fields', []):
            for ref in field.get('evidence', []):
                # A prior association limits inferred episode transport, not
                # the independently qualified, already admitted custody body.
                if user_only and (ref.get('source_role') != 'user_intent' or
                                  not custody and ref.get('source_id') in historical_ids):
                    continue
                if not user_only and ref.get('source_id') in custody_ids | native_ids:
                    continue
                quote, start, end = ref.get('quote_excerpt'), ref.get('start'), ref.get('end')
                if (not ref.get('source_id') or not isinstance(quote, str) or not quote or
                        ref.get('quote_truncated') or type(start) is not int or type(end) is not int or
                        not 0 <= start < end or end - start != len(quote) or len(quote) > (1200 if user_only else 600)):
                    continue
                key = (ref['source_id'], ref.get('source_field') or 'body', ref.get('source_role'),
                       qualifier_key(qualifiers), custody)
                group = groups.setdefault(key, {'qualifiers': qualifiers, 'refs': [], 'custody': custody,
                    'fallback_only': bool(record.get('source_fallback_for_reason'))})
                group['refs'].append(dict(ref))
    result = []
    for group in groups.values():
        merged: list[dict[str, Any]] = []
        for ref in sorted(group['refs'], key=lambda ref: (ref['start'], ref['end'])):
            ref.pop('field', None)
            if merged and ref['start'] <= merged[-1]['end']:
                prior = merged[-1]
                overlap = min(prior['end'], ref['end']) - ref['start']
                offset = ref['start'] - prior['start']
                if prior['quote_excerpt'][offset:offset + overlap] != ref['quote_excerpt'][:overlap]:
                    # Conflicting text cannot establish an exact assembly.
                    merged = []
                    break
                suffix = ref['quote_excerpt'][overlap:]
                if len(prior['quote_excerpt']) + len(suffix) <= (1200 if user_only else 600):
                    prior.update(end=max(prior['end'], ref['end']),
                                 quote_excerpt=prior['quote_excerpt'] + suffix)
                    continue
            merged.append(ref)
        if merged and (user_only or len({(ref['start'], ref['end']) for ref in group['refs']}) > 1):
            result.append({**group, 'refs': merged})
    return result


def _source_value_repeated(value: str, refs: list[Mapping[str, Any]]) -> bool:
    """Only exact literal reconstruction can replace duplicate derived prose."""
    if not value or not refs or not refs[0].get('source_id'):
        return False
    identity = {(ref.get('source_id'), ref.get('source_field') or 'body', ref.get('source_role')) for ref in refs}
    if len(identity) != 1 or any(ref.get('quote_truncated') or not isinstance(ref.get('quote_excerpt'), str)
            or type(ref.get('start')) is not int or type(ref.get('end')) is not int
            or not 0 <= ref['start'] < ref['end']
            or ref['end'] - ref['start'] != len(ref['quote_excerpt']) for ref in refs):
        return False
    ordered = refs
    if any(left['end'] != right['start'] for left, right in zip(ordered, ordered[1:])):
        return False
    return value == ''.join(ref['quote_excerpt'] for ref in ordered)


def _plan_original_source_transport(brief: Mapping[str, Any], records: list[Mapping[str, Any]],
                                    budget: int, available: int,
                                    plan: OriginalSourcePlan, *, complementary: bool = False) -> tuple[list[str], list[dict[str, Any]], int, int]:
    """Allocate complete selected originals before bounded neutral supplements.

    This plan owns quote transport only. Each association keeps its own scope,
    role and uncertainty; an omitted duplicate association stays for the normal
    record renderer, which reports any association it cannot retain.
    """
    user_groups = _complete_source_groups(records, user_only=True) if not complementary and (
        brief.get('user_source_coverage_requested') or brief.get('neutral_original_context')) else []
    capture_order = dict(brief.get('user_source_capture_order') or {})
    # Per-source capture metadata wins over an episode's multi-source range.
    # A range maximum cannot date each of that episode's older source pointers.
    for record in records:
        for field in record.get('fields', []):
            for ref in field.get('evidence', []):
                seq = ref.get('source_capture_seq')
                if ref.get('source_role') == 'user_intent' and type(seq) is int:
                    capture_order[ref.get('source_id')] = seq
    for record in records:
        seq = record.get('source_capture_seq_max')
        if type(seq) is not int or record.get('source_capture_seq_min') != seq:
            continue
        for field in record.get('fields', []):
            for ref in field.get('evidence', []):
                if ref.get('source_role') == 'user_intent':
                    capture_order.setdefault(ref.get('source_id'), seq)
    user_groups.sort(key=lambda group: (-max((capture_order.get(ref['source_id'], 0) for ref in group['refs']), default=0),
                                       not (group.get('custody') and not group.get('fallback_only')), -sum(len(ref['quote_excerpt']) for ref in group['refs'])))
    source_containers: dict[str, dict[str, Any]] = {}
    grouped_intervals = []
    other_groups = _complete_source_groups(records) if complementary else []
    neutral = sorted([] if complementary else brief.get('unassociated_user_context') or [],
                     key=lambda record: int(record.get('source_capture_seq_max') or 0))
    neutral = ([neutral[-1], neutral[0]] + neutral[1:-1]) if len(neutral) > 1 else neutral
    neutral_groups = []
    for record in neutral:
        refs = [ref for field in record.get('fields', []) for ref in field.get('evidence', [])]
        if len(refs) != 1 or refs[0].get('quote_truncated') or len(refs[0].get('quote_excerpt') or '') > 600:
            continue
        metadata = source_qualifiers(record)
        neutral_groups.append({'qualifiers': metadata, 'refs': refs, 'neutral': True})
    # Selected material retains its existing capture/reason order. Optional
    # unassociated originals use only the room left by those admitted sources.
    # Neither chronology nor lexical association asserts source applicability.
    pending_groups = user_groups + neutral_groups + other_groups
    neutral_selected, neutral_spent = 0, 0
    neutral_limit = min(1800, budget // 3)
    for group in pending_groups:
        if not group.get('neutral') and all(plan.find(ref, group['qualifiers'], same_context=False) for ref in group['refs']):
            continue
        is_neutral = group.get('neutral', False)
        key = json.dumps((is_neutral, group.get('custody', False), group['qualifiers']), sort_keys=True)
        container = source_containers.setdefault(key, {
            'id': ('unassociated-user-sources-' if is_neutral else 'selected-source-') + str(plan.container_count + len(source_containers) + 1),
            'qualifiers': group['qualifiers'], 'refs': [], 'neutral': is_neutral})
        trial = [*container['refs'], *group['refs']]
        if group.get('custody'):
            trial.sort(key=lambda ref: int(ref.get('source_capture_seq') or 0))
        new_refs = []
        for ref in group['refs']:
            match = plan.find(ref, group['qualifiers'], same_context=False)
            new_refs.append({**ref, '_reference': match} if match else
                {**ref, 'source_field': ref.get('source_field') or 'body',
                 'id': 's' + str(len(plan.entries) + len([p for p in new_refs if '_reference' not in p]) + 1)})
        display_refs = container.get('display_refs', []) + new_refs
        metadata = {'lane': 'intent_custody' if is_neutral or group.get('custody') else 'source_excerpts',
                    **group['qualifiers']}
        if not (is_neutral or group.get('custody')) or any(container['id'] in (relation.get('from'), relation.get('to'))
               for relation in brief.get('relations', [])):
            metadata['id'] = container['id']
        if is_neutral or group.get('custody'):
            metadata.update(source_bundle=True)
            if not brief.get('_shared_capture_order_basis'):
                metadata['capture_order_basis'] = 'local_capture_sequence_not_event_time'
        if group.get('custody'):
            display_refs.sort(key=lambda ref: int(ref.get('source_capture_seq') or 0))
        attrs = _item_attrs(metadata, brief.get('projection'))
        node = ('<item' + attrs + '>' + _bundle_field_xml({'evidence': display_refs}) + '</item>' if is_neutral or group.get('custody') else
                '<item' + attrs + '><field basis="attributed_source_excerpts" field_association="not_assessed">' +
                ''.join(_source_xml(ref, complete=True) for ref in display_refs) + '</field></item>')
        cost = len(node) - len(container.get('xml', ''))
        marker = '<source-context-coverage selected_source_count=' + _attr(neutral_selected + int(is_neutral)) + ' omitted=' + _attr(len(neutral) - neutral_selected - int(is_neutral)) + '/>'
        if cost + (len(marker) if neutral else 0) > available or (
                is_neutral and neutral_spent + cost + len(marker) > neutral_limit):
            continue
        available -= cost
        if is_neutral:
            neutral_spent += cost
            neutral_selected += 1
        container.update(refs=trial, display_refs=display_refs, xml=node)
        for ref in new_refs:
            if '_reference' not in ref:
                plan.add(ref, group['qualifiers'])
        grouped_intervals.extend(((ref.get('source_id'), ref.get('source_field') or 'body', ref.get('source_role')),
                                  ref['start'], ref['end']) for ref in group['refs'])
    nodes = [container['xml'] for container in source_containers.values() if container.get('xml')]
    plan.container_count += len(source_containers)
    pointers = [ref for container in source_containers.values() if container.get('xml') for ref in container['refs']]
    if neutral:
        neutral_omitted = len(neutral) - neutral_selected
        marker = '<source-context-coverage selected_source_count=' + _attr(neutral_selected) + ' omitted=' + _attr(neutral_omitted) + '/>'
        if len(marker) <= available and neutral_spent + len(marker) <= neutral_limit:
            nodes.append(marker)
    return nodes, pointers, neutral_selected, len(neutral) - neutral_selected


def _render_project_brief(brief: Mapping[str, Any], budget: int = 6000, *,
                          _omission_reserve: int = 0) -> tuple[str, dict[str, int]]:
    """Return valid XML within a character budget, without slicing XML markup."""
    if isinstance(budget, bool) or not isinstance(budget, int) or budget < 128 or budget > 60000:
        raise ValueError('budget must be between 128 and 60000')
    original_brief = brief
    records = list(brief.get('records', []))
    material = lambda record: any(field.get('text') or field.get('evidence') for field in record.get('fields', []))
    records.sort(key=lambda record: not material(record))
    shared_worktree = brief.get('project')
    shared_worktree = shared_worktree if isinstance(shared_worktree, str) and any(
        (record.get('scope') or {}).get('worktree') == shared_worktree for record in records) else None
    capture_basis = 'local_capture_sequence_not_event_time'
    shared_capture_order = bool(shared_worktree) or any(record.get('capture_order_basis') == capture_basis
        for record in [*records, *(brief.get('unassociated_user_context') or []),
                       *(brief.get('unassociated_role_context') or [])])
    closing = '</project-memory>'
    promoted_opening = '<project-memory trust="untrusted" freshness="not_verified"' + (
        ' worktree=' + _attr(shared_worktree) if shared_worktree else '') + (
        ' capture_order_basis=' + _attr(capture_basis) if shared_capture_order else '') + '>'
    if len(promoted_opening + closing) > budget:
        shared_worktree = None
        shared_capture_order = False
        promoted_opening = '<project-memory trust="untrusted" freshness="not_verified">'
    if shared_worktree or shared_capture_order:
        records = [{**record, 'scope': {key: value for key, value in record['scope'].items() if key != 'worktree'}}
                   if shared_worktree and (record.get('scope') or {}).get('worktree') == shared_worktree else record for record in records]
        records = [{**record, 'capture_order_basis': None} if
                   shared_capture_order and record.get('capture_order_basis') == capture_basis else record for record in records]
        brief = dict(brief)
        brief['_shared_capture_order_basis'] = shared_capture_order
        for key in ('unassociated_user_context', 'unassociated_role_context'):
            brief[key] = [{**record, 'scope': {name: value for name, value in
                          (record.get('scope') or {}).items() if name != 'worktree'}}
                         if shared_worktree and (record.get('scope') or {}).get('worktree') == shared_worktree else record
                         for record in brief.get(key, [])]
            brief[key] = [{**record, 'capture_order_basis': None} if shared_capture_order and
                          record.get('capture_order_basis') == capture_basis else record for record in brief[key]]
    opening = promoted_opening
    parts = [opening]
    coverage = brief.get('coverage') or {}
    # Coverage qualifiers precede a watermark: processed observations never
    # imply a complete transcript or successful state reconciliation. Under a
    # tiny budget keep the strongest warning and omit lower-priority metrics.
    status = '<coverage'
    values = {'complete_transcript': coverage.get('complete_transcript', False),
              'observations_only': coverage.get('observations_only', True),
              'pending': bool(brief.get('pending'))}
    values.update({key: coverage[key] for key in (
        'coverage_gaps', 'pending_reconciliation', 'quarantined_events',
        'omitted_transitions_count', 'downgraded_states_count',
        'partially_processed_events', 'processed_through_seq', 'pending_events', 'historical_context_heads',
        'unresolved_user_sources', 'user_sources_omitted', 'source_evidence_omitted',
        'user_source_order_omitted') if key in coverage})
    reconsideration = coverage.get('reconsideration') or {}
    for field in ('ambiguous_heads', 'omitted_sources', 'omitted_heads', 'identity_sources_omitted', 'omitted_ancestor_chains'):
        if reconsideration.get(field):
            values['reconsideration_' + field] = reconsideration[field]
    for key in ('complete_transcript', 'coverage_gaps', 'unresolved_user_sources', 'user_sources_omitted', 'source_evidence_omitted',
                'user_source_order_omitted',
                'pending_reconciliation', 'pending',
                'observations_only', 'quarantined_events', 'omitted_transitions_count',
                'downgraded_states_count', 'partially_processed_events',
                'processed_through_seq', 'pending_events', 'historical_context_heads',
                'reconsideration_ambiguous_heads', 'reconsideration_omitted_sources',
                'reconsideration_omitted_heads', 'reconsideration_identity_sources_omitted',
                'reconsideration_omitted_ancestor_chains'):
        if key not in values:
            continue
        value = values[key]
        serialized = str(value).lower() if isinstance(value, bool) else str(value)[:64]
        attr = ' ' + key + '=' + _attr(serialized)
        if len(opening + status + attr + '/>' + closing) <= budget:
            status += attr
    if len(opening + status + '/>' + closing) <= budget:
        parts.append(status + '/>')
    for relation in brief.get('relations', []):
        node = '<relation' + ''.join(' ' + key + '=' + _attr(relation[key]) for key in ('id', 'type', 'from', 'to') if key in relation) + '/>'
        if sum(map(len, parts)) + len(node) + len(closing) <= budget:
            parts.append(node)
    reserve_position = len(parts)
    if _omission_reserve:
        parts.append(' ' * _omission_reserve)
    omissions = {'records': 0, 'fields': 0, 'user_sources': 0}
    complete_refs: set[tuple[Any, ...]] = set()
    complete_quotes: set[tuple[Any, ...]] = set()
    emitted_intervals: list[tuple[Any, ...]] = []
    # One source plan pays for already selected user originals before neutral
    # supplements and repeated association wrappers. Every association retains
    # its own qualifiers; capture order only orders display candidates.
    remaining = budget - sum(map(len, parts)) - len(closing)
    compact_coverage = _compact_coverage_xml(parts[1]) if len(parts) > 1 and parts[1].startswith('<coverage') else ''
    coverage_savings = len(parts[1]) - len(compact_coverage) if compact_coverage else 0
    original_plan = OriginalSourcePlan(remaining + coverage_savings)
    expanded_evidence = bool(brief.get('source_evidence_requested'))
    role_context = list(brief.get('unassociated_role_context') or [])
    original_roles = [record for record in [*records, *role_context]
                      if _role_original_xml(record, brief.get('projection'))]
    if brief.get('source_order') in {'earliest', 'latest'}:
        def capture_position(record: Mapping[str, Any]) -> tuple[bool, int]:
            seq = record.get('source_capture_seq_max')
            known = type(seq) is int
            return not known, (seq if brief['source_order'] == 'earliest' else -seq) if known else 0
        original_roles.sort(key=capture_position)
    protection_pool = [record for record in original_roles if not expanded_evidence or record in records]
    protected_roles: set[str] = set()
    execution_allowance = min(1800, budget // 3)
    role_attempts: set[tuple[Any, ...]] = set()
    delivered_role_ids: set[tuple[Any, ...]] = set()

    def retain_originals(pointers: list[Mapping[str, Any]]) -> None:
        for ref in pointers:
            complete_refs.add(tuple(ref.get(key) for key in
                ('source_id', 'start', 'end', 'source_field', 'tool_use_id', 'source_role')))
            complete_quotes.add(tuple(ref.get(key) for key in
                ('source_id', 'start', 'end', 'source_field', 'tool_use_id', 'source_role', 'quote_excerpt')))
            emitted_intervals.append(((ref.get('source_id'), ref.get('source_field') or 'body',
                                       ref.get('source_role')), ref['start'], ref['end']))

    def offer_role(record: Mapping[str, Any]) -> bool:
        displayed = _role_quote_ids(record, original_plan)
        node = _role_original_xml(displayed, brief.get('projection'))
        if not original_plan.offer(node):
            return False
        pointers = [ref for field in displayed.get('fields', []) for ref in field.get('evidence', [])]
        for ref in pointers:
            original_plan.add(ref, source_qualifiers(record))
        retain_originals(pointers)
        delivered_role_ids.add((record.get('lane'), record.get('id')))
        return True

    # The existing first-role allowances protect proof without pre-reserving
    # every additional capture ahead of already admitted user originals.
    for record in protection_pool:
        displayed = _role_quote_ids(record, original_plan)
        node = _role_original_xml(displayed, brief.get('projection'))
        role = 'execution' if record.get('execution') else 'report'
        if not node or role in protected_roles:
            continue
        if role == 'report' and len(node) > min(900, budget // 6):
            continue
        if not expanded_evidence and role == 'execution' and len(node) > execution_allowance:
            continue
        if offer_role(record):
            protected_roles.add(role)
            role_attempts.add((record.get('lane'), record.get('id')))
            if not expanded_evidence and role == 'execution':
                execution_allowance -= len(node)

    source_nodes, source_pointers, neutral_selected, neutral_omitted = _plan_original_source_transport(
        brief, records, budget, original_plan.remaining, original_plan)
    for node in source_nodes:
        if not original_plan.offer(node):
            raise AssertionError('original source pricing differs from emission')
    retain_originals(source_pointers)
    if brief.get('unassociated_user_context'):
        omissions['unassociated_user_sources'] = neutral_omitted

    # Additional canonical captures use the same exact-node offer after users.
    # Requested source order stays the order of the already admitted records.
    for record in original_roles:
        identity = (record.get('lane'), record.get('id'))
        if identity in role_attempts:
            continue
        role_attempts.add(identity)
        if not offer_role(record) and record in records:
            omissions['records'] += 1
            omissions['fields'] += len(record.get('fields') or [])
    if role_context:
        omissions['original_context_sources'] = sum((record.get('lane'), record.get('id')) not in
            delivered_role_ids for record in role_context)

    # Body-field excerpts remain distinct citations. They get a later turn
    # than complete canonical captures, without aliasing an output field.
    source_nodes, source_pointers, _, _ = _plan_original_source_transport(
        brief, records, budget, original_plan.remaining, original_plan, complementary=True)
    for node in source_nodes:
        if not original_plan.offer(node):
            raise AssertionError('complementary source pricing differs from emission')
    retain_originals(source_pointers)
    if sum(map(len, original_plan.nodes)) > remaining and compact_coverage:
        parts[1] = compact_coverage
    parts.extend(original_plan.nodes)
    # These compact originals protect bounded source recall when competing
    # candidate material does not actually fit. The rest of the budget remains
    # available to independent claims and complete material groups.
    fallback_reservations = {}
    fallback_allowance = min(2000, max(0, budget - sum(map(len, parts)) - len(closing)) // 3)
    for index, record in enumerate(records):
        size = _compact_custody_size(record, brief.get('projection'))
        if size and size <= fallback_allowance:
            fallback_reservations[index] = size
            fallback_allowance -= size
    heads = [(index, record) for index, record in enumerate(records)
             if record.get('lane') == 'episode' and record.get('is_current_head') is True]
    latest_head = max(heads, key=lambda pair: (int(pair[1].get('source_capture_seq_max') or 0),
                      int(pair[1].get('episode_version') or 0)), default=None)
    head_reservations = {}
    # Extracted observed output is still independent evidence when a captured
    # tool has no supported native execution shape. Reserve its complete role
    # group as well; a neutral user-source bundle must not erase observations.
    tool_allowance = min(1800, budget // 3)
    observed_fields = {}
    native_sources = {record.get('id') for record in records
                      if record.get('lane') == 'source_evidence' and record.get('execution')}
    for index, record in sorted(heads, key=lambda pair: -int(pair[1].get('source_capture_seq_max') or 0)):
        fields = [field for field in record.get('fields', []) if field.get('evidence') and
                  all(ref.get('source_role') == 'tool_record' for ref in field['evidence']) and
                  not all(ref.get('source_id') in native_sources for ref in field['evidence']) and
                  not all(_interval_covered(ref, emitted_intervals) or tuple(ref.get(key) for key in
                    ('source_id', 'start', 'end', 'source_field', 'tool_use_id', 'source_role', 'quote_excerpt')) in complete_quotes
                    for ref in field['evidence'])]
        if not fields:
            continue
        field = min(fields, key=lambda field: sum(len(_source_xml(ref, complete=True)) for ref in field['evidence']))
        metadata = ''.join(' ' + key + '=' + _attr(field[key]) for key in
            ('basis', 'field_association', 'partial_context') if key in field)
        size = len('<item' + _item_attrs(record, brief.get('projection')) + ' fields_omitted="99"><field name=' +
                   _attr(field['name']) + metadata + '>' + ''.join(_source_xml(ref, complete=True)
                   for ref in field['evidence']) + '</field></item>')
        if size <= tool_allowance:
            head_reservations[index] = max(head_reservations.get(index, 0), size)
            observed_fields[index] = field
            tool_allowance -= size
    rendered_record_ids = {record_id for _, record_id in delivered_role_ids}
    for index, record in enumerate(records):
        if (record.get('lane'), record.get('id')) in role_attempts:
            continue
        if record.get('lane') == 'intent_custody' and all(
                original_plan.find(pointer, source_qualifiers(record))
                for field in record.get('fields', []) for pointer in field.get('evidence', [])):
            continue
        attrs = _item_attrs(record, brief.get('projection'))
        remaining = budget - sum(map(len, parts)) - len(closing)
        # Reserve room for multiple useful records rather than letting an
        # episode's repeated provenance consume the entire brief. Evidence is
        # still explicit, with omissions counted, and state/scope never sliced.
        item_budget = min(remaining, max((2200 if brief.get('anchored_natural') and brief.get('projection') != 'continuity' else 1400) if record.get('lane') == 'episode' else 1100 if record.get('execution') else 900, remaining // min(6, len(records) - index)))
        # Complete attributed fields compete against the global budget. A
        # per-item share can reject a useful whole group while lower-priority
        # records consume the space that group needed.
        if any(field.get('basis') == 'attributed_source_excerpts'
               for field in record.get('fields', [])):
            # Reserve complete later obligations/reasons before admitting an
            # early record's secondary outcome. An impossible reservation must
            # not prevent a fitting primary group from using the whole budget.
            seen: set[tuple[Any, ...]] = set()
            reservations = [_reserved_record_size(later, brief.get('projection'), seen, remaining // 2)
                            for later in records[index + 1:]]
            reserved = sum(reservations)
            item_budget = max(item_budget, remaining - min(reserved, remaining // 2))
        compact_reserve = sum(size for later, size in fallback_reservations.items() if later > index and
            not all(_interval_covered(pointer, emitted_intervals) for field in records[later].get('fields', [])
                    for pointer in field.get('evidence', [])))
        execution_reserve = 0
        head_reserve = sum(size for later, size in head_reservations.items() if later > index)
        item_budget = min(item_budget, max(0, remaining - compact_reserve - execution_reserve - head_reserve))
        if index in head_reservations:
            item_budget = max(item_budget, min(head_reservations[index],
                max(0, remaining - compact_reserve - execution_reserve - head_reserve)))
        if index in observed_fields:
            chosen_field = observed_fields[index]
            record = {**record, 'fields': [chosen_field] + [field for field in record['fields'] if field is not chosen_field]}
        bundle_omissions = 0
        bundle_selected_sources = 0
        if record.get('source_bundle'):
            # Common neutral qualifiers are paid once. Each original remains a
            # whole source group, selected using the actual render budget.
            maximum = min(3200, max(0, remaining - execution_reserve - head_reserve))
            uncovered = [field for field in record['fields'] if not all(
                _interval_covered(pointer, emitted_intervals) for pointer in field['evidence'])]
            chosen, bundle_omissions = _select_bundle_fields({**record, 'fields': uncovered}, maximum, brief.get('projection'))
            omissions['user_sources'] += bundle_omissions
            if not chosen:
                omissions['records'] += 1
                omissions['fields'] += bundle_omissions
                continue
            bundle_selected_sources = len(chosen)
            record = {**record, 'fields': [{'name': 'source_custody', 'text': '',
                'basis': 'attributed_source_excerpts',
                'evidence': [pointer for field in chosen for pointer in field['evidence']]}]}
            item_budget = maximum
        # Original validated executions retain command and full output together.
        # Their allowance cannot consume later custody or execution reserves.
        atomic_execution = record.get('lane') == 'source_evidence' and bool(record.get('execution'))
        if atomic_execution:
            node = _execution_xml(record, brief.get('projection'))
            if not node or len(node) > max(0, remaining - compact_reserve - execution_reserve - head_reserve):
                omissions['records'] += 1
                omissions['fields'] += len(record.get('fields') or [])
                continue
            parts.append(node)
            rendered_record_ids.add(record.get('id'))
            for field in record['fields']:
                for ref in field['evidence']:
                    complete_refs.add(tuple(ref.get(key) for key in ('source_id', 'start', 'end', 'source_field', 'tool_use_id', 'source_role')))
                    complete_quotes.add(tuple(ref.get(key) for key in ('source_id', 'start', 'end', 'source_field', 'tool_use_id', 'source_role', 'quote_excerpt')))
                    emitted_intervals.append(((ref.get('source_id'), ref.get('source_field') or 'body', ref.get('source_role')), ref['start'], ref['end']))
            continue
        text = str(record.get('preview') or '')[:700]
        content = ''
        if isinstance(record.get('execution'), Mapping):
            execution = record['execution']
            content = '<execution' + ''.join(' ' + key + '=' + _attr(
                str(value)[:128] if key == 'command' and record.get('lane') != 'source_evidence' else value)
                for key, value in execution.items() if key != 'capture_recorded_at') + '/>'
        fields = record.get('fields') or [{'name': 'preview', 'text': text,
                                           'evidence': record.get('evidence', [])}]
        omitted_fields = bundle_omissions
        grounded_omissions = 0
        quoted_groups = 0
        emitted_refs = set()
        pending_refs, pending_quotes, pending_intervals = set(), set(), []
        def retain_complete_pointers(refs: list[Mapping[str, Any]], *, grounded: bool) -> None:
            for ref in refs:
                quote, left, right = ref.get('quote_excerpt'), ref.get('start'), ref.get('end')
                if (not isinstance(quote, str) or ref.get('quote_truncated') or
                        not grounded and len(quote) > 160 or type(left) is not int or
                        type(right) is not int or right - left != len(quote)):
                    continue
                pending_refs.add(tuple(ref.get(key) for key in
                    ('source_id', 'start', 'end', 'source_field', 'tool_use_id', 'source_role')))
                pending_quotes.add(tuple(ref.get(key) for key in
                    ('source_id', 'start', 'end', 'source_field', 'tool_use_id', 'source_role', 'quote_excerpt')))
                pending_intervals.append(((ref.get('source_id'), ref.get('source_field') or 'body', ref.get('source_role')), left, right))
        for field in fields:
            refs = field.get('evidence') or []
            grounded = field.get('basis') == 'attributed_source_excerpts'
            resolved = [original_plan.find(ref, source_qualifiers(record), same_context=False) for ref in refs]
            if grounded and refs and all(resolved):
                metadata = ''.join(' ' + key + '=' + _attr(field[key]) for key in
                                   ('basis', 'field_association', 'partial_context') if key in field)
                references = ''.join(_source_reference_xml(match, ref, association_field=field['name'])
                    for ref, match in zip(refs, resolved))
                node = '<field name=' + _attr(field['name']) + metadata + '>' + references + '</field>'
                reserve = ' fields_omitted="99"' if len(fields) > 1 else ''
                if len('<item' + attrs + reserve + '>' + content + node + '</item>') <= item_budget:
                    content += node
                else:
                    omitted_fields += 1
                    grounded_omissions += 1
                continue
            identities = {(ref.get('source_id'), ref.get('start'), ref.get('end'),
                           ref.get('source_field'), ref.get('tool_use_id'), ref.get('source_role'))
                          for ref in refs}
            quote_keys = {tuple(ref.get(key) for key in
                ('source_id', 'start', 'end', 'source_field', 'tool_use_id', 'source_role', 'quote_excerpt')) for ref in refs}
            if (grounded and not record.get('source_bundle') and refs and
                    all(_interval_covered(ref, pending_intervals) for ref in refs)):
                quoted_groups += 1
                omitted_fields += 1
                continue
            if (grounded and record.get('lane') == 'episode' and refs and
                    all(ref.get('source_role') == 'user_intent' and _interval_covered(ref, emitted_intervals) for ref in refs)):
                quoted_groups += 1
                omitted_fields += 1
                continue
            if (grounded and record.get('lane') != 'source_evidence' and refs and
                    all(ref.get('source_role') == 'tool_record' for ref in refs) and
                    quote_keys.issubset(complete_quotes)):
                quoted_groups += 1
                omitted_fields += 1
                continue
            if identities and (grounded and identities.issubset(emitted_refs) or
                               record.get('lane') == 'source_evidence' and identities.issubset(complete_refs)):
                continue
            # Each prose field keeps ALL its source pointers. If they do not
            # fit together, omit that field instead of pairing its prose with
            # an unrelated subset of an episode's evidence.
            displayed_refs = []
            for ref in refs:
                displayed = dict(ref)
                quote_key = tuple(ref.get(key) for key in ('source_id', 'start', 'end', 'source_field', 'tool_use_id', 'source_role', 'quote_excerpt'))
                if record.get('lane') == 'source_evidence' and quote_key in complete_quotes:
                    displayed.pop('quote_excerpt', None)
                    displayed['quote_display'] = 'quoted_above'
                elif record.get('lane') == 'claim' and not grounded and ref.get('source_role') == 'user_intent' and _interval_covered(ref, emitted_intervals):
                    displayed.pop('quote_excerpt', None)
                    displayed['quote_display'] = 'quoted_in_context'
                displayed_refs.append(displayed)
            repeated_value = _source_value_repeated(str(field.get('text') or ''), refs)
            evidence = ''.join(_source_reference_xml(match, original, association_field=field['name']) if match else
                _source_xml(displayed, complete=grounded or repeated_value)
                for original, displayed in zip(refs, displayed_refs)
                for match in [original_plan.find(original, source_qualifiers(record), same_context=False)])
            if field['name'] != 'preview' and not refs:
                omitted_fields += 1
                continue
            value = str(field.get('text') or '')
            if not grounded:
                value = value[:700]
            def field_node(length: int) -> str:
                if record.get('source_bundle'):
                    return _bundle_field_xml(field)
                excerpt = value[:length] + ('…' if length < len(value) else '')
                prose = '<derived_text>' + escape(_xml_text(excerpt)) + '</derived_text>' if value and not grounded else ''
                if repeated_value:
                    # The complete displayed source already carries this exact
                    # literal value. Field, role and qualifiers remain explicit.
                    prose = ''
                metadata = ''.join(' ' + key + '=' + _attr(field[key]) for key in
                                   ('basis', 'field_association', 'partial_context') if key in field)
                if record.get('lane') == 'source_evidence' and record.get('source_role') == 'assistant_report':
                    metadata = ''
                return '<field name=' + _attr(field['name']) + metadata + '>' + prose + evidence + '</field>'
            omission_reserve = (' fields_omitted=' + _attr(bundle_omissions) if bundle_omissions else '') if record.get('source_bundle') else (
                ' fields_omitted="99"' if len(fields) > 1 else '')
            remaining_item = item_budget - len('<item' + attrs + omission_reserve + '>' + content + '</item>')
            if grounded:
                whole = field_node(len(value))
                if len(whole) <= remaining_item:
                    content += whole
                    emitted_refs.update(identities)
                    retain_complete_pointers(refs, grounded=True)
                else:
                    omitted_fields += 1
                    grounded_omissions += 1
                continue
            if len(field_node(min(80, len(value)))) > remaining_item:
                omitted_fields += 1
                continue
            low, high = 0, len(value)
            while low < high:
                middle = (low + high + 1) // 2
                if len(field_node(middle)) <= remaining_item:
                    low = middle
                else:
                    high = middle - 1
            content += field_node(low)
            retain_complete_pointers(refs, grounded=False)
        omissions['fields'] += omitted_fields
        if quoted_groups and record.get('lane') == 'episode':
            # Original quotes remain available above, but this inferred field
            # association was compressed rather than represented by its refs.
            attrs += ' field_associations_omitted=' + _attr(quoted_groups)
        if not content and not grounded_omissions and not quoted_groups:
            omissions['records'] += 1
            continue
        if omitted_fields:
            attrs += ' fields_omitted=' + _attr(omitted_fields)
        item_xml = '<item' + attrs + '>' + content + '</item>'
        if len(item_xml) <= min(remaining, item_budget):
            parts.append(item_xml)
            rendered_record_ids.add(record.get('id'))
            complete_refs.update(pending_refs)
            complete_quotes.update(pending_quotes)
            emitted_intervals.extend(pending_intervals)
            if not content:
                omissions['records'] += 1
        else:
            omissions['records'] += 1
            omissions['fields'] += bundle_selected_sources if record.get('source_bundle') else len(fields) - omitted_fields
            if record.get('source_bundle'):
                omissions['user_sources'] += bundle_selected_sources
    omission_marker = _omissions_xml(omissions)
    if latest_head is not None and latest_head[1]['id'] not in rendered_record_ids:
        head = latest_head[1]
        metadata = {key: head[key] for key in ('id', 'episode_version', 'is_current_head',
                    'continuity_basis', 'uncertainty', 'prior_context_status', 'retained_context_fields_omitted') if key in head}
        compressed = sum(bool(field.get('evidence')) and all(_interval_covered(ref, emitted_intervals)
            for ref in field['evidence']) for field in head.get('fields', []))
        if compressed:
            metadata['field_associations_omitted'] = compressed
        marker = '<item lane="episode"' + ''.join(' ' + key + '=' + _attr(value)
                    for key, value in metadata.items()) + ' fields_omitted=' + _attr(len(head.get('fields', []))) + '/>'
        marker_cost = len(marker) + (0 if _omission_reserve else len(omission_marker))
        if sum(map(len, parts)) + marker_cost + len(closing) > budget and compact_coverage:
            parts[1] = compact_coverage
        if sum(map(len, parts)) + marker_cost + len(closing) <= budget:
            parts.append(marker)
    # Supplemental source custody is not task association. Spend only capacity
    # left after every material candidate and head qualifier had its turn.
    if _omission_reserve:
        parts.pop(reserve_position)
    if omission_marker:
        marker = omission_marker
        # Counters use spare space or replace lower-priority coverage metrics.
        # Never remove a whole fitting quotation merely to fit its marker.
        if len(''.join(parts) + marker + closing) > budget and compact_coverage:
            parts[1] = compact_coverage
        if len(''.join(parts) + marker + closing) <= budget:
            parts.append(marker)
        elif not _omission_reserve and budget >= 256:
            # A second pure packing pass reserves the actual maximum marker
            # spelling. No reads, candidate changes, or source slicing occur.
            total_fields = sum(max(1, len(record.get('fields') or [])) for record in records)
            reserve = len(_omissions_xml({'records': 3 * len(records), 'fields': 3 * total_fields,
                'user_sources': total_fields if any(record.get('source_bundle') for record in records) else 0,
                'unassociated_user_sources': len(brief.get('unassociated_user_context') or []),
                'original_context_sources': len(role_context)}))
            return _render_project_brief(original_brief, budget, _omission_reserve=reserve)
    return ''.join(parts) + closing, omissions


def render_project_brief(brief: Mapping[str, Any], budget: int = 6000) -> str:
    """Return valid bounded XML; whole attributed quotation groups stay intact."""
    return _render_final_project_brief(brief, budget)[0]


def _render_final_project_brief(brief: Mapping[str, Any], budget: int) -> tuple[str, dict[str, int]]:
    canonical, omissions = _render_project_brief(brief, budget)
    return _compact_and_refill_sources(brief, canonical, budget, omissions)


def _compact_and_refill_sources(brief: Mapping[str, Any], canonical: str, budget: int,
                               omissions: dict[str, int] | None = None) -> tuple[str, dict[str, int]]:
    """Preserve the primary selection, then spend proven wire savings once.

    quote_refs="provenance_only" means that association qualifiers stay local.
    A quoted observation never inherits another container's truth, scope,
    history, custody, or relationship. New source quotations do not activate
    omitted canonical associations, so their omission counters remain intact.
    """
    from xml.etree import ElementTree as ET
    import re

    omissions = dict(omissions or {})
    declaration = ' quote_refs="provenance_only"'
    incoming = {value for relation in brief.get('relations', [])
                for value in (relation.get('from'), relation.get('to')) if value}
    # A generated display label must not shadow any admitted canonical identity.
    for record in [*(brief.get('records') or []), *(brief.get('unassociated_user_context') or []),
                   *(brief.get('unassociated_role_context') or [])]:
        if record.get('id'):
            incoming.add(record['id'])
        incoming.update(ref['source_id'] for field in record.get('fields', [])
                        for ref in field.get('evidence', []) if ref.get('source_id'))
    wire = compact_selected_xml(canonical, incoming, native_tools=_EXECUTION_TOOLS)
    has_references = ET.fromstring(wire).find('.//quote_ref') is not None
    if has_references and len(wire) + len(declaration) > budget:
        # The mandatory wire format has a real price. Retry the unchanged
        # primary policy once with that price reserved. Refill never evicts.
        canonical, omissions = _render_project_brief(brief, budget - len(declaration))
        wire = compact_selected_xml(canonical, incoming, native_tools=_EXECUTION_TOOLS)
        has_references = ET.fromstring(wire).find('.//quote_ref') is not None
    if has_references:
        wire = wire.replace('<project-memory', '<project-memory' + declaration, 1)
    # Existing slack alone does not trigger a different primary allocation.
    if len(wire) >= len(canonical):
        return wire, omissions
    tree = ET.fromstring(wire)
    registry = OriginalSourcePlan(0)
    used = {source.get('id') for source in tree.iter('source') if source.get('id')}
    for source in tree.iter('source'):
        quote = source.findtext('quote_excerpt')
        if quote is None:
            continue
        pointer = dict(source.attrib)
        pointer.update(start=int(pointer['start']), end=int(pointer['end']), quote_excerpt=quote,
                       quote_truncated=pointer.get('quote_truncated') not in {None, 'False'})
        identifier = source.get('id')
        if not identifier:
            identifier = 'unlabelled-' + str(len(registry.entries))
            while identifier in used:
                identifier += '-'
        registry.add(pointer, {}, identifier=identifier)
    next_id = 1
    for group in _complete_source_groups(list(brief.get('records') or [])):
        refs = group['refs']
        if all(registry.find(ref, {}, same_context=False) is not None for ref in refs):
            continue
        qualifiers = dict(group['qualifiers'])
        scope = qualifiers.get('scope') or {}
        if tree.get('worktree') and scope.get('worktree') == tree.get('worktree'):
            qualifiers['scope'] = {key: value for key, value in scope.items() if key != 'worktree'}
        displayed = []
        for ref in refs:
            while 's' + str(next_id) in used:
                next_id += 1
            identifier = 's' + str(next_id)
            used.add(identifier)
            next_id += 1
            displayed.append({**ref, 'id': identifier})
        node = '<item' + _item_attrs({'lane': 'source_excerpts', **qualifiers}, brief.get('projection')) + (
            '><field basis="attributed_source_excerpts" field_association="not_assessed">') + ''.join(
            _source_xml(ref, complete=True) for ref in displayed) + '</field></item>'
        if len(wire) + len(node) > budget:
            continue
        wire = wire.removesuffix('</project-memory>') + node + '</project-memory>'
        for ref in displayed:
            registry.add(ref, qualifiers, identifier=ref['id'])
    # Refill complete typed associations only from displayed exact originals.
    # A provenance reference keeps this association's own workflow and scope.
    # The primary source selection never changes or pays for an inferred link.
    represented = {item.get('id') for item in tree.findall('item') if item.get('id')}
    for record in brief.get('records') or []:
        if not record.get('id') or record['id'] in represented:
            continue
        displayed = dict(record)
        scope = dict(record.get('scope') or {})
        if tree.get('worktree') and scope.get('worktree') == tree.get('worktree'):
            displayed['scope'] = {key: value for key, value in scope.items() if key != 'worktree'}
        if tree.get('capture_order_basis') == record.get('capture_order_basis'):
            displayed['capture_order_basis'] = None
        node = _quoted_claim_association_xml(displayed, registry, brief.get('projection'), used)
        if not node:
            continue
        revised = dict(omissions, records=max(0, omissions.get('records', 0) - 1),
                       fields=max(0, omissions.get('fields', 0) - len(record.get('fields') or [])))
        marker = _omissions_xml(revised)
        trial = re.sub(r'<omitted\b[^>]*/>', lambda _: marker, wire)
        trial = trial.removesuffix('</project-memory>') + node + '</project-memory>'
        if ' quote_refs="provenance_only"' not in trial.split('>', 1)[0]:
            trial = trial.replace('<project-memory', '<project-memory' + declaration, 1)
        if len(trial) > budget:
            continue
        wire, omissions = trial, revised
        represented.add(record['id'])
    assert len(wire) <= budget
    return wire, omissions


def build_project_brief(store: Any, project: str | Path, query: str = '', budget: int = 6000,
                        exclude_session_id: str | None = None, *, include_shadow: bool = False,
                        mode: str | None = None, environment: str | None = None,
                        version: str | None = None,
                        selected_project_scopes: Sequence[str | Path] | None = None) -> dict[str, Any]:
    """Select project ledger state locally; shadow reads require explicit opt-in.

    ``mode`` is a trusted configuration result, never evidence-supplied input.
    Selected project scopes only identify names in the question. They do not
    expand this read beyond ``project`` or bypass project configuration.
    No evaluator or external service is required on this path.
    """
    from .project_memory import ProjectMemory
    from .store import project_key
    if not isinstance(query, str) or '\x00' in query or len(query) > 20000:
        raise ValueError('query must be bounded text')
    workspace = project_key(project)
    from .config import load_config, project_memory_enabled
    settings = load_config(store.data_dir)
    from .project_memory_query import validated_project_scopes
    selected_scopes = validated_project_scopes(workspace, selected_project_scopes, settings)
    configured = project_memory_enabled(workspace, settings, pilot_only=not include_shadow)
    if mode is None:
        mode = settings.get('project_memory_mode', 'off')
    if mode == 'off':
        mode = 'disabled'
    if mode not in {'disabled', 'shadow', 'pilot'}:
        raise ValueError('invalid project memory mode')
    brief: dict[str, Any] = {'project': workspace, 'mode': mode, 'enabled': False,
        'claims': [], 'episodes': [], 'signals': [], 'intent_custody': [], 'records': [], 'coverage': {}, 'pending': False}
    # Validate the budget even when this read is disabled.
    render_project_brief(brief, budget)
    if not configured or mode == 'disabled' or (mode == 'shadow' and not include_shadow) or is_private_prompt(query):
        brief['xml'] = ''
        return brief
    # Optional recall must not change baseline behavior when disabled, nor
    # relax an over-cap exact query into a broad project-state selection.
    try:
        from .project_memory_query import plan_query
        query_plan = plan_query(query, workspace, selected_project_scopes=selected_scopes)
        constraints = query_plan.exact
    except ValueError:
        brief.update(xml='', skip_reason='unbounded_literal_constraints')
        return brief
    if query_plan.historical:
        brief.update(xml='', skip_reason='historical_query')
        return brief
    query = query_plan.content_query
    if environment is None and len(query_plan.environments) == 1:
        environment = query_plan.environments[0]
    if version is None and len(query_plan.versions) == 1:
        version = query_plan.versions[0]
    memory = ProjectMemory(store)
    topical = resume_topic_plan(query)
    continuation = query_plan.continuation
    include_project_obligations = bool(continuation and not query_plan.task_ids and
        not query_plan.identifiers and not query_plan.versions and environment is None and version is None)
    snapshots = [memory.snapshot(workspace, limit=_MAX_ITEMS, signal_limit=8,
        exclude_session_id=exclude_session_id, query=candidate,
        environment=environment, version=version if len(query_plan.versions) <= 1 else None,
        topic_query=query_plan.topic_query,
        include_episode_history=query_plan.assesses_current_evidence or query_plan.source_order is not None or query_plan.projection in {'continuity', 'history'},
        include_project_obligations=include_project_obligations) for candidate in query_plan.candidate_queries]
    fallback_used = False
    if query_plan.natural and not (query_plan.task_ids or query_plan.identifiers or query_plan.versions) and not any(
            snapshot.get('claims') or snapshot.get('episodes') for snapshot in snapshots):
        fallback_used = True
        snapshots.append(memory.snapshot(workspace, limit=_MAX_ITEMS, signal_limit=8,
            exclude_session_id=exclude_session_id, query='', environment=environment, version=version,
            include_episode_history=query_plan.assesses_current_evidence or query_plan.source_order is not None or query_plan.projection in {'continuity', 'history'},
            include_project_obligations=include_project_obligations))
    snapshot = dict(snapshots[0])
    for lane in ('claims', 'episodes', 'provisional_signals', 'project_obligations', 'relations', 'intent_custody'):
        seen: set[str] = set()
        snapshot[lane] = [item for part in snapshots for item in part.get(lane, [])
                          if item.get('id') not in seen and not seen.add(item.get('id'))]
    def relevant(item: Mapping[str, Any]) -> bool:
        if not query_plan.matches(item):
            return False
        if query_plan.generic_project_state or query_plan.answer_subject == '':
            return True
        if query_plan.natural and query_plan.answer_subject is None and query_plan.requirement_subject is None and (
                query_plan.task_ids or query_plan.identifiers or query_plan.versions):
            return True
        if not query_plan.natural:
            return bool(_query_score(item, query, workspace))
        relevance_query = (query_plan.answer_subject if query_plan.answer_subject is not None else
                           query_plan.requirement_subject if query_plan.requirement_subject is not None else
                           ' '.join(query_plan.candidate_queries) if query_plan.assesses_current_evidence else query)
        plan = resume_topic_plan(relevance_query) or prompt_query_plan('How ' + relevance_query)
        if plan is None or not plan.get('terms'):
            return True
        def soft_term(term: str, stem: str) -> tuple[str, str]:
            # One lexical requirement stays one scored term; variants cannot
            # increase the minimum or weaken separately checked hard anchors.
            variants = project_lexical_text(term).split()
            return term, variants[1] if len(variants) > 1 else stem
        soft = dict(plan, identifiers=[], versions=[],
            minimum=len(plan['terms']) if query_plan.requirement_subject is not None or query_plan.complete_subject else 1,
            terms=[soft_term(term, stem) for term, stem in plan['terms']])
        score = bool(prompt_match_score(project_lexical_text(_text(item)), soft))
        if not score and fallback_used:
            import re
            # A local script mismatch cannot prove project evidence irrelevant.
            # Keep bounded project-state lanes and use lexical matches to rank.
            return bool(re.search(r'[а-яё]', query, re.I) and not re.search(r'[а-яё]', _text(item), re.I))
        return score
    from .project_memory_query import requests_source_evidence
    brief['source_evidence_requested'] = requests_source_evidence(query) or query_plan.assesses_current_evidence
    brief['projection'] = query_plan.projection
    brief['user_source_coverage_requested'] = bool(query_plan.natural and not query_plan.continuation and query_plan.source_order is None
        and not query_plan.assesses_current_evidence and 'execution_assessment' not in query_plan.answer_facets
        and (not query_plan.generic_project_state or query_plan.answer_subject is not None
             or bool(query_plan.answer_facets)))
    brief['source_order'] = query_plan.source_order
    brief['anchored_natural'] = bool(query_plan.natural and (query_plan.task_ids or query_plan.identifiers or query_plan.versions))
    brief.update(enabled=True, coverage=snapshot.get('coverage') or {}, pending=bool(snapshot.get('pending')),
                 revision=snapshot.get('revision'), deletion_generation=snapshot.get('deletion_generation'))
    def eligible(item: Any) -> bool:
        if not (_safe(item, workspace, exclude_session_id) and
                _scope_matches(item, environment, version, workspace)):
            return False
        if query_plan.natural and (environment or query_plan.excluded_environments):
            from .project_memory_query import environment_names
            requested = environment_names(environment) if environment else ()
            if not _environment_evidence_matches(item, requested, query_plan.excluded_environments):
                return False
        return True
    claims = [item for item in snapshot.get('claims', []) if eligible(item) and
              (item.get('workflow_state') not in _CLOSED or
               item.get('workflow_state') == 'completed' and item.get('kind') in {'fact', 'result'}) and relevant(item)]
    reserved_ids: set[str] = set()
    reserved_order: dict[str, int] = {}
    if include_project_obligations:
        for item in snapshot.get('project_obligations', []):
            if (eligible(item) and item.get('kind') in {'requirement', 'constraint'} and
                    item.get('workflow_state') not in _CLOSED):
                reserved_order.setdefault(item['id'], len(reserved_order))
                reserved_ids.add(item['id'])
                if all(claim['id'] != item['id'] for claim in claims):
                    claims.append(item)
    # Old unresolved obligations outrank newer incidental material. Recency is
    # only a stable tie-breaker; it never changes workflow state.
    def priority(item: Mapping[str, Any]) -> tuple[Any, ...]:
        obligation = item.get('kind') in _OBLIGATIONS and item.get('workflow_state') in {'open', 'blocked', 'unresolved', 'accepted', 'active'}
        accepted = item.get('workflow_state') in {'accepted', 'active'}
        return (-1 if item.get('id') in reserved_ids else 0 if obligation else 1 if accepted else 2,
                reserved_order.get(item.get('id'), -_query_score(item, query, workspace)),
                0 if obligation or accepted else -int(item.get('source_capture_seq_max') or 0),
                0 if obligation or accepted else -int(item.get('revision') or 0), str(item.get('id', '')))
    claims.sort(key=priority)
    episodes = sorted((item for item in snapshot.get('episodes', []) if eligible(item) and
                       (query_plan.assesses_current_evidence or query_plan.projection in {'continuity', 'history'} or
                        item.get('is_current_head', True) is not False) and relevant(item)),
                      key=lambda item: (-_query_score(item, query, workspace),
                                        -int(item.get('source_capture_seq_max') or 0),
                                        -int(item.get('revision') or 0), str(item.get('id', ''))))[:8]
    watermark = brief['coverage'].get('processed_through_seq', 0) or 0
    signals = [item for item in snapshot.get('provisional_signals', []) if eligible(item) and
               _signal_allowed(item) and item.get('seq', 0) > watermark and
               relevant(item)]
    brief['claims'] = [_record(item, 'claim') for item in claims[:32]]
    brief['episodes'] = [_record(item, 'episode') for item in episodes]
    if query_plan.natural and query_plan.projection != 'continuity':
        for record in brief['episodes']:
            selected = []
            seen_refs = set()
            priorities = {'rationale': 0, 'prior_rationale': 1, 'goal': 2} if query_plan.projection == 'rationale' else {
                'open_work': 0, 'prior_open_work': 1, 'rationale': 2, 'prior_rationale': 2, 'goal': 2, 'prior_goal': 2, 'outcome': 3, 'summary': 4}
            if query_plan.projection == 'environments':
                # Prefer the complete cross-environment field before narrower
                # outcome/obligation groups repeat its sources and use its room.
                priorities = {'summary': 0, 'outcome': 1, 'open_work': 2, 'prior_open_work': 3}
            for field in sorted(record['fields'], key=lambda field: priorities.get(field['name'], 3)):
                selected_pointers = []
                if query_plan.projection == 'environments' and any(
                        not _environment_evidence_matches(
                            {'source_role': pointer.get('source_role'), 'text': pointer.get('quote_excerpt') or ''},
                            query_plan.environments, query_plan.excluded_environments)
                        for pointer in field['evidence']):
                    # Grounded fields are complete quotation groups. Reject an
                    # incompatible execution group rather than resurface it via
                    # another pointer or silently cut its qualifications.
                    continue
                for pointer in field['evidence']:
                    identity = (pointer.get('source_id'), pointer.get('start'), pointer.get('end'))
                    if (field.get('basis') != 'attributed_source_excerpts' and identity in seen_refs
                            or not pointer.get('quote_excerpt')):
                        continue
                    seen_refs.add(identity)
                    selected_pointers.append(pointer)
                    if field.get('basis') != 'attributed_source_excerpts':
                        selected.append({'name': field['name'] + '_source_excerpt', 'text': '', 'evidence': [pointer]})
                if selected_pointers and field.get('basis') == 'attributed_source_excerpts':
                    # A grounded field is one bounded evidence group. Projection
                    # may select it, but cannot discard its qualifiers or refs.
                    selected.append({**field, 'name': field['name'] + '_source_excerpt', 'text': ''})
            # These are quoted evidence units, not regenerated state claims.
            # Each source remains physically attached to its own excerpt.
            record['fields'] = selected[:8] if query_plan.projection == 'environments' else selected[:8] or record['fields']
        brief['episodes'] = [record for record in brief['episodes'] if record['fields']]
    if query_plan.projection == 'continuity':
        by_id = {item['id']: item for item in episodes}
        context_by_episode = _continuity_context(store, workspace, episodes, exclude_session_id)
        for record in brief['episodes']:
            original = by_id[record['id']]
            refs = (original.get('evidence') or {}).get('continuation_of', [])
            if refs:
                record['fields'].insert(0, {'name': 'continuation_of',
                    'text': '',
                    'evidence': _evidence({'evidence': {'continuation_of': refs}})})
            context = context_by_episode.get(record['id'], [])
            if context:
                record['fields'].append({'name': 'source_context', 'text': '', 'evidence': context})
            record['fields'].sort(key=lambda field: {'continuation_of': 0, 'source_context': 1, 'goal': 2}.get(field['name'], 3))
    brief['signals'] = [_record(item, 'signal') for item in signals[:8]]
    for record in brief['claims'] + brief['signals'] + brief['episodes']:
        record['project'] = workspace
    obligations = [record for record in brief['claims']
                   if record.get('project_memory_kind') in _OBLIGATIONS and
                   record.get('workflow_state') in {'open', 'blocked', 'unresolved', 'accepted', 'active'}]
    obligation_ids = {record['id'] for record in obligations}
    other_claims = [record for record in brief['claims'] if record['id'] not in obligation_ids]
    # Fresh corrections must survive both startup character budgets and the
    # explicit search's result limit. Then retain obligations and alternate
    # other durable state with episode context instead of exhausting one lane.
    records = brief['signals'] + obligations
    for index in range(max(len(other_claims), len(brief['episodes']))):
        for lane in (other_claims, brief['episodes']):
            if index < len(lane):
                records.append(lane[index])
    if topical is not None:
        import re
        if continuation and query_plan.projection != 'continuity':
            open_episodes = [record for record in brief['episodes'] if any(
                field['name'] in {'open_work', 'open_work_source_excerpt'} and field.get('evidence') for field in record.get('fields', []))]
            latest = max((int(record.get('source_capture_seq_max') or record.get('revision') or 0)
                          for record in open_episodes), default=0)
            def open_priority(record: Mapping[str, Any]) -> tuple[int, int]:
                captured = int(record.get('source_capture_seq_max') or record.get('revision') or 0)
                direct_user_work = any(pointer.get('source_role') == 'user_intent'
                    for field in record.get('fields', []) if field['name'] in {'open_work', 'open_work_source_excerpt'}
                    for pointer in field.get('evidence', []))
                return (0 if captured == latest else 1 if direct_user_work else 2, -captured)
            open_episodes.sort(key=open_priority)
            promoted = {record['id'] for record in open_episodes}
            # Fresh unfinished work and durable user instructions must fit in
            # startup's smaller slice before incidental facts or preferences.
            # Captured historical reports keep their order labels; no state is
            # closed or reinterpreted here.
            reserved = [record for record in brief['claims'] if record['id'] in reserved_ids]
            records = brief['signals'] + reserved + open_episodes + [record for record in records
                       if record['id'] not in promoted and record['id'] not in reserved_ids
                       and record.get('lane') != 'signal']
    if query_plan.natural:
        important = [record for record in brief['signals'] if record.get('signal_kind') != 'user_message' or record.get('selection_basis') == 'explicit_user_marker']
        ordinary = [record for record in brief['signals'] if record not in important]
        ep_ids = {record['id'] for record in brief['episodes']}
        sig_ids = {record['id'] for record in brief['signals']}
        ordered_episodes = [record for record in records if record['id'] in ep_ids]
        other = [record for record in records if record['id'] not in ep_ids | sig_ids]
        if query_plan.projection in {'continuity', 'history', 'environments', 'rationale'}:
            records = brief['episodes'] + other + important + ordinary
        else:
            reserved = [record for record in other if record['id'] in reserved_ids]
            records = important + reserved + ordered_episodes + [record for record in other if record['id'] not in reserved_ids] + ordinary
    if query_plan.projection == 'history' and brief['episodes']:
        ordered = sorted(brief['episodes'], key=lambda item: (
            int(item.get('source_capture_seq_max') or 0), int(item.get('revision') or 0)))
        boundary = [ordered[0]] + ([ordered[-1]] if ordered[-1]['id'] != ordered[0]['id'] else [])
        boundary_ids = {item['id'] for item in boundary}
        records = boundary + [item for item in records if item['id'] not in boundary_ids]
    custody = []
    unassociated = []
    supplement_allowed = bool(query_plan.natural and not query_plan.continuation and query_plan.source_order is None)
    brief['neutral_original_context'] = supplement_allowed
    material_user_sources = {pointer.get('source_id') for record in records for field in record.get('fields', [])
                             for pointer in field.get('evidence', []) if pointer.get('source_role') == 'user_intent'}
    unresolved = [item for item in snapshot.get('intent_custody', [])
                  if item.get('disposition') == 'unresolved' and eligible(item)]
    supplement_sources = {}
    if supplement_allowed:
        from .project_memory_custody import snapshot as custody_snapshot
        with store._lock:
            recent, recent_coverage = store._read(lambda: custody_snapshot(
                store._connection, workspace, 16, exclude_session_id, recent_context=True))
        brief['unassociated_user_candidate_coverage'] = recent_coverage
        seen = {item['id'] for item in unresolved}
        for item in recent:
            if item['id'] not in seen and eligible(item):
                source = item.pop('source_record')
                if eligible(source):
                    unresolved.append(item)
                    supplement_sources[item['source_id']] = source
    if unresolved:
        source_ids = list(dict.fromkeys(item['source_id'] for item in unresolved
                                       if item['source_id'] not in supplement_sources))
        sources = {item['id']: item for item in store.get(workspace, source_ids)} if source_ids else {}
        sources.update(supplement_sources)
        source_scope_bindings = _custody_scope_bindings(store, workspace, sources, exclude_session_id)
        conflicts = {source_id for source_id, binding in source_scope_bindings.items()
                     if binding is not None and len(binding) > 1}
        if conflicts:
            brief['coverage'] = dict(brief['coverage'], custody_scope_conflicts=len(conflicts))
        capped = sum(binding is None for binding in source_scope_bindings.values())
        if capped:
            brief['coverage'] = dict(brief['coverage'], custody_scope_binding_lookups_omitted=capped)
        for item in unresolved:
            source = sources.get(item['source_id'])
            if not source or not _safe(source, workspace, exclude_session_id):
                continue
            body, start, end = source.get('body'), item.get('start'), item.get('end')
            if not isinstance(body, str) or type(start) is not int or type(end) is not int or not 0 <= start < end <= len(body):
                continue
            quote = body[start:end]
            scoped_original = dict(item, text=quote, source_role='user_intent')
            if source.get('scope'):
                scoped_original['scope'] = source['scope']
            declared_scopes = source_scope_bindings.get(item['source_id'], ())
            if declared_scopes is None:
                continue
            if len(declared_scopes) > 1 and (environment or query_plan.excluded_environments):
                continue
            environment_guard = scoped_original
            if len(declared_scopes) == 1:
                environment_guard = dict(scoped_original, scope={'environment': declared_scopes[0]})
            from .project_memory_query import environment_names
            requested_environments = environment_names(environment) if environment else ()
            if not _environment_evidence_matches(
                    environment_guard, requested_environments, query_plan.excluded_environments):
                continue
            if version and exact_constraints(quote, workspace)['versions'] and not prompt_match_score(
                    quote, {'identifiers': [], 'versions': [version], 'terms': [], 'minimum': 0}):
                continue
            if not query_plan.matches(scoped_original):
                continue
            associated = item.get('disposition') == 'unresolved' and (relevant(scoped_original) or item['source_id'] in material_user_sources)
            if not associated and (not supplement_allowed or start != 0 or end != len(body) or len(body) > 600):
                continue
            original = dict(scoped_original, evidence={'text': [{'source_id': item['source_id'],
                'start': start, 'end': end, 'source_role': 'user_intent', 'quote': quote}]})
            record = _record(original, 'intent_custody')
            if item['source_id'] in conflicts:
                record['uncertainty'] = 'conflicting_declared_environment_scopes'
            if type(item.get('seq')) is int:
                record.update(source_capture_seq_min=item['seq'], source_capture_seq_max=item['seq'],
                              capture_order_basis='local_capture_sequence_not_event_time')
            record['fields'] = [{'name': 'source_custody', 'text': '', 'basis': 'attributed_source_excerpts',
                'field_association': 'verbatim_user_source',
                'evidence': [{'source_id': item['source_id'], 'start': start, 'end': end,
                    'source_role': 'user_intent', 'field': 'source_custody',
                    'source_capture_seq': item.get('seq'),
                    'quote_excerpt': redact_search_value(quote), 'quote_truncated': False,
                    **({'excerpt_status': 'complete_consumed_interval', 'source_chars': len(body)}
                       if start != 0 or end != len(body) else {})}]}]
            if associated:
                custody.append(record)
            else:
                record.update(selection_basis='unassociated_user_context', relationship_to_query='unresolved')
                if item.get('disposition') != 'unresolved':
                    record.update(meaning='original_user_observation', uncertainty='current_applicability_unresolved')
                unassociated.append(record)
    brief['unassociated_user_context'] = unassociated
    brief['intent_custody'] = custody
    custody_coverage = snapshot.get('intent_custody_coverage') or {}
    brief['intent_custody_coverage'] = custody_coverage
    if custody_coverage:
        brief['coverage'] = dict(brief['coverage'])
        brief['coverage']['unresolved_user_sources'] = custody_coverage.get('unresolved', 0)
        brief['coverage']['user_sources_omitted'] = custody_coverage.get('omitted', 0)
        if custody_coverage.get('unresolved') or custody_coverage.get('omitted'):
            brief['coverage'].update(coverage_gaps=True, pending_reconciliation=True)
    # Every eligible source remains a render candidate. Only displayed complete
    # intervals can establish that its full-source fallback would be redundant.
    unrepresented, deferred = [], []
    for record in custody:
        source_id = record['fields'][0]['evidence'][0]['source_id']
        overlaps = any(ref.get('source_id') == source_id for other in records
                       for field in other.get('fields', []) for ref in field.get('evidence', []))
        if overlaps:
            record['source_overlap_with_material'] = True
            record['source_fallback_for_reason'] = any(ref.get('source_id') == source_id
                for other in records for field in other.get('fields', [])
                if field['name'].removesuffix('_source_excerpt') in {'rationale', 'prior_rationale'}
                for ref in field.get('evidence', []))
            deferred.append(record)
        else:
            unrepresented.append(record)
    # Neutral unresolved sources follow independent open claims and precede
    # incidental outcomes. No read infers that these quotes are active work.
    open_ids = {record['id'] for record in obligations}
    priority_records = [record for record in records if record['id'] in open_ids]
    records = priority_records + unrepresented + [record for record in records if record['id'] not in open_ids] + deferred
    sources, source_coverage = _source_evidence(store, workspace, query_plan, exclude_session_id, eligible, relevant, environment, version)
    context_sources = []
    context_allowed = supplement_allowed
    if query_plan.natural and context_allowed and query_plan.source_order is None:
        context_sources, context_coverage = _source_evidence(store, workspace, query_plan,
            exclude_session_id, eligible, relevant, environment, version, unassociated_context=True)
        selected_sources = {record['id'] for record in sources}
        context_sources = [record for record in context_sources if record['id'] not in selected_sources]
        brief['original_source_context_coverage'] = dict(context_coverage, additional_candidates=len(context_sources))
    brief['unassociated_role_context'] = context_sources
    brief['source_evidence'] = sources
    brief['source_evidence_coverage'] = source_coverage
    brief['coverage'] = dict(brief['coverage'], source_evidence_omitted=source_coverage['omitted'])
    priority_records = [record for record in records if record['id'] in open_ids]
    def source_identity(ref: Mapping[str, Any]) -> tuple[Any, ...]:
        return tuple(ref.get(key) for key in ('source_id', 'start', 'end', 'source_field',
                                             'tool_use_id', 'source_role', 'quote_excerpt'))
    material_refs = {source_identity(ref) for record in records for field in record.get('fields', [])
                     for ref in field.get('evidence', []) if not ref.get('quote_truncated')}
    independent, repeated = [], []
    material_source_ids = {ref[0] for ref in material_refs}
    for record in sources:
        refs = [ref for field in record['fields'] for ref in field['evidence']]
        already_cited_tool = record.get('source_role') == 'tool_record' and record['id'] in material_source_ids
        duplicate = refs and all(source_identity(ref) in material_refs for ref in refs)
        (repeated if already_cited_tool or duplicate else independent).append(record)
    # Compact original executions keep role/provenance visible even when their
    # extracted outcome is merely a candidate that may lose the render budget.
    # Bound promotion across all tools so large captures cannot displace whole
    # independent constraints, open work or compact original user reasons.
    compact_tools = []
    allowance = min(1800, budget // 3)
    repeated.sort(key=lambda record: -int(record.get('source_capture_seq_max') or 0))
    for record in repeated:
        size = _whole_execution_size(record, query_plan.projection)
        if size and size <= allowance:
            record['compact_execution'] = True
            compact_tools.append(record)
            allowance -= size
    compact_ids = {record['id'] for record in compact_tools}
    records = priority_records + independent + compact_tools + [record for record in records if record['id'] not in open_ids] + [
        record for record in repeated if record['id'] not in compact_ids]
    if len(custody) >= 2:
        # Bundle only already eligible neutral sources. This does not derive
        # instructions, expand query scope or extend source retention.
        metadata_keys = ('lane', 'kind', 'source_role', 'disposition',
                         'is_requirement_claim', 'meaning', 'uncertainty')
        groups = {}
        for original in custody:
            metadata = tuple((key, original[key]) for key in metadata_keys if key in original)
            groups.setdefault(metadata, []).append(original)
        bundles = []
        for index, (metadata, group) in enumerate(groups.items()):
            # Record-level uncertainty belongs to every field in that record.
            # Different source states therefore need different containers.
            bundle = dict(metadata)
            bundle.update(id='original-user-sources' + (f'-{index + 1}' if len(groups) > 1 else ''),
                source_bundle=True, source_count=len(group),
                capture_order_basis='local_capture_sequence_not_event_time',
                fields=[field for original in group for field in original['fields']])
            bundles.append(bundle)
        tools = [record for record in records if record.get('lane') == 'source_evidence' and record.get('execution')]
        fresh = [record for record in records if record.get('lane') == 'signal']
        reports = [record for record in records if record.get('lane') == 'source_evidence' and record.get('source_role') == 'assistant_report']
        records = priority_records + fresh + tools + reports + bundles + [record for record in records
            if record not in priority_records and record not in fresh and record not in tools and
            record not in reports and record.get('lane') != 'intent_custody']
    if query_plan.source_order is not None:
        ordered_tools = [record for record in sources if record.get('execution')]
        ordered_ids = {record['id'] for record in ordered_tools}
        records = ordered_tools + [record for record in records if record.get('id') not in ordered_ids]
    if brief['user_source_coverage_requested']:
        source_ids = list(dict.fromkeys(ref['source_id'] for record in sorted(records,
            key=lambda record: -int(record.get('source_capture_seq_max') or 0))
            for field in record.get('fields', []) for ref in field.get('evidence', [])
            if ref.get('source_role') == 'user_intent' and ref.get('source_id')))
        selected_ids = source_ids[:64]
        brief['coverage'] = dict(brief['coverage'], user_source_order_omitted=max(0, len(source_ids) - len(selected_ids)))
        if selected_ids:
            with store._lock:
                rows = store._read(lambda: store._connection.execute(
                    'SELECT source_id,seq FROM pm_events WHERE project=? AND source_id IN (' +
                    ','.join('?' for _ in selected_ids) + ')', (workspace, *selected_ids)).fetchall())
            brief['user_source_capture_order'] = dict(rows)
    brief['records'] = records
    selected_ids = {item.get('id') for item in brief['records']}
    brief['relations'] = [dict(item) for item in snapshot.get('relations', []) if
                          item.get('from') in selected_ids and item.get('to') in selected_ids
                          and _safe(item, workspace, exclude_session_id)][:32]
    brief['xml'], brief['render_omissions'] = _render_final_project_brief(brief, budget)
    count = len(unassociated)
    omitted = brief['render_omissions'].get('unassociated_user_sources', 0)
    brief['unassociated_user_context_coverage'] = {'eligible': count, 'selected_source_count': count - omitted,
        'omitted': omitted, 'returned_source_limit': count, 'render_allowance': min(1800, budget // 3),
        'relationship_to_query': 'unresolved', 'verification': 'not_assessed'}
    return brief
