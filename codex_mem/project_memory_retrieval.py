"""Read-only, bounded project briefs shared by startup and explicit resume.

Selection never decides whether work is complete. The ledger's explicit state
and evidence remain visible, including provisional signals and conflicts.
"""
from __future__ import annotations

from collections.abc import Mapping
import json
from pathlib import Path
from typing import Any
from xml.sax.saxutils import escape, quoteattr

from .jev_retrieval import exact_constraints
from .privacy import redact_search_value
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


def _evidence(item: Mapping[str, Any], limit: int = 8) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    def visit(value: Any, field: str = '') -> None:
        if isinstance(value, Mapping):
            if value.get('source_id'):
                pointer = {key: value[key] for key in ('source_id', 'start', 'end', 'source_role', 'tool_use_id', 'source_field') if key in value}
                if field:
                    pointer['field'] = field
                if isinstance(value.get('quote'), str):
                    pointer['quote_excerpt'] = value['quote'][:240]
                    pointer['quote_truncated'] = len(value['quote']) > 240
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
        'signal_kind', 'selection_basis', 'prior_context_status', 'retained_context_fields_omitted') if key in cleaned}
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
            record['fields'].append({'name': name, 'text': value[:1400],
                'evidence': _evidence({'evidence': {name: refs}}, limit=32 if name.startswith('prior_') else 8)})
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


def render_project_brief(brief: Mapping[str, Any], budget: int = 6000) -> str:
    """Return valid XML within a character budget, without slicing XML markup."""
    if isinstance(budget, bool) or not isinstance(budget, int) or budget < 128 or budget > 60000:
        raise ValueError('budget must be between 128 and 60000')
    opening = '<project-memory trust="untrusted" freshness="not_verified">'
    closing = '</project-memory>'
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
        'partially_processed_events', 'processed_through_seq', 'pending_events', 'historical_context_heads') if key in coverage})
    reconsideration = coverage.get('reconsideration') or {}
    for field in ('ambiguous_heads', 'omitted_sources', 'omitted_heads', 'identity_sources_omitted', 'omitted_ancestor_chains'):
        if reconsideration.get(field):
            values['reconsideration_' + field] = reconsideration[field]
    for key in ('complete_transcript', 'coverage_gaps', 'pending_reconciliation', 'pending',
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
    records = list(brief.get('records', []))
    for index, record in enumerate(records):
        attrs = ''.join(' ' + key + '=' + _attr(record[key]) for key in
                        ('id', 'lane', 'kind', 'project_memory_kind', 'workflow_state', 'requested_state', 'source_role', 'evidence_basis', 'provisional', 'inferred',
                         'revision', 'source_capture_seq_min', 'source_capture_seq_max', 'capture_order_basis',
                         'logical_episode_id', 'episode_version', 'parent_episode_id', 'is_current_head', 'continuity_basis',
                         'signal_kind', 'selection_basis', 'prior_context_status', 'retained_context_fields_omitted') if key in record and record[key] is not None)
        if brief.get('projection') == 'continuity' and record.get('session_id'):
            attrs += ' session_id=' + _attr(record['session_id'])
        if record.get('lane') == 'signal' and record.get('source_capture_seq_min') == record.get('source_capture_seq_max'):
            for key in ('source_capture_seq_min', 'source_capture_seq_max'):
                attrs = attrs.replace(' ' + key + '=' + _attr(record.get(key)), '')
            if record.get('source_capture_seq_max') is not None:
                attrs += ' source_capture_seq=' + _attr(record['source_capture_seq_max'])
        scope = record.get('scope') or {}
        attrs += ''.join(' ' + key + '=' + _attr(scope[key])
                         for key in ('environment', 'version', 'worktree') if scope.get(key))
        remaining = budget - sum(map(len, parts)) - len(closing)
        # Reserve room for multiple useful records rather than letting an
        # episode's repeated provenance consume the entire brief. Evidence is
        # still explicit, with omissions counted, and state/scope never sliced.
        item_budget = min(remaining, max((2200 if brief.get('anchored_natural') and brief.get('projection') != 'continuity' else 1400) if record.get('lane') == 'episode' else 1100 if record.get('execution') else 900, remaining // min(6, len(records) - index)))
        text = str(record.get('preview') or '')[:700]
        def source_xml(source: Mapping[str, Any]) -> str:
            source = dict(source)
            if isinstance(source.get('quote_excerpt'), str):
                source['quote_truncated'] = bool(source.get('quote_truncated') or len(source['quote_excerpt']) > 160)
                source['quote_excerpt'] = source['quote_excerpt'][:160]
            excerpt = source.pop('quote_excerpt', None)
            opening = '<source' + ''.join(' ' + key + '=' + _attr(value) for key, value in source.items())
            if excerpt is None:
                return opening + '/>'
            return opening + '><quote_excerpt>' + escape(_xml_text(excerpt)) + '</quote_excerpt></source>'
        content = ''
        if isinstance(record.get('execution'), Mapping):
            execution = record['execution']
            content = '<execution' + ''.join(' ' + key + '=' + _attr(
                str(value)[:128] if key == 'command' else value)
                for key, value in execution.items() if key != 'capture_recorded_at') + '/>'
        fields = record.get('fields') or [{'name': 'preview', 'text': text,
                                           'evidence': record.get('evidence', [])}]
        omitted_fields = 0
        for field in fields:
            refs = field.get('evidence') or []
            # Each prose field keeps ALL its source pointers. If they do not
            # fit together, omit that field instead of pairing its prose with
            # an unrelated subset of an episode's evidence.
            evidence = ''.join(source_xml(ref) for ref in refs)
            if field['name'] != 'preview' and not refs:
                omitted_fields += 1
                continue
            value = str(field.get('text') or '')[:700]
            def field_node(length: int) -> str:
                excerpt = value[:length] + ('…' if length < len(value) else '')
                prose = '<derived_text>' + escape(_xml_text(excerpt)) + '</derived_text>' if value else ''
                return '<field name=' + _attr(field['name']) + '>' + prose + evidence + '</field>'
            omission_reserve = ' fields_omitted="99"' if len(fields) > 1 else ''
            remaining_item = item_budget - len('<item' + attrs + omission_reserve + '>' + content + '</item>')
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
        if not content:
            continue
        if omitted_fields:
            attrs += ' fields_omitted=' + _attr(omitted_fields)
        item_xml = '<item' + attrs + '>' + content + '</item>'
        if len(item_xml) <= remaining:
            parts.append(item_xml)
    return ''.join(parts) + closing


def build_project_brief(store: Any, project: str | Path, query: str = '', budget: int = 6000,
                        exclude_session_id: str | None = None, *, include_shadow: bool = False,
                        mode: str | None = None, environment: str | None = None,
                        version: str | None = None) -> dict[str, Any]:
    """Select project ledger state locally; shadow reads require explicit opt-in.

    ``mode`` is a trusted configuration result, never evidence-supplied input.
    No evaluator or external service is required on this path.
    """
    from .project_memory import ProjectMemory
    from .store import project_key
    if not isinstance(query, str) or '\x00' in query or len(query) > 20000:
        raise ValueError('query must be bounded text')
    workspace = project_key(project)
    from .config import load_config, project_memory_enabled
    settings = load_config(store.data_dir)
    configured = project_memory_enabled(workspace, settings, pilot_only=not include_shadow)
    if mode is None:
        mode = settings.get('project_memory_mode', 'off')
    if mode == 'off':
        mode = 'disabled'
    if mode not in {'disabled', 'shadow', 'pilot'}:
        raise ValueError('invalid project memory mode')
    brief: dict[str, Any] = {'project': workspace, 'mode': mode, 'enabled': False,
        'claims': [], 'episodes': [], 'signals': [], 'records': [], 'coverage': {}, 'pending': False}
    # Validate the budget even when this read is disabled.
    render_project_brief(brief, budget)
    if not configured or mode == 'disabled' or (mode == 'shadow' and not include_shadow) or is_private_prompt(query):
        brief['xml'] = ''
        return brief
    # Optional recall must not change baseline behavior when disabled, nor
    # relax an over-cap exact query into a broad project-state selection.
    try:
        from .project_memory_query import plan_query
        query_plan = plan_query(query, workspace)
        constraints = query_plan.exact
    except ValueError:
        brief.update(xml='', skip_reason='unbounded_literal_constraints')
        return brief
    if query_plan.historical:
        brief.update(xml='', skip_reason='historical_query')
        return brief
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
        include_episode_history=query_plan.projection in {'continuity', 'history'},
        include_project_obligations=include_project_obligations) for candidate in query_plan.candidate_queries]
    fallback_used = False
    if query_plan.natural and not (query_plan.task_ids or query_plan.identifiers or query_plan.versions) and not any(
            snapshot.get('claims') or snapshot.get('episodes') for snapshot in snapshots):
        fallback_used = True
        snapshots.append(memory.snapshot(workspace, limit=_MAX_ITEMS, signal_limit=8,
            exclude_session_id=exclude_session_id, query='', environment=environment, version=version,
            include_project_obligations=include_project_obligations))
    snapshot = dict(snapshots[0])
    for lane in ('claims', 'episodes', 'provisional_signals', 'project_obligations', 'relations'):
        seen: set[str] = set()
        snapshot[lane] = [item for part in snapshots for item in part.get(lane, [])
                          if item.get('id') not in seen and not seen.add(item.get('id'))]
    def relevant(item: Mapping[str, Any]) -> bool:
        if not query_plan.matches(item):
            return False
        if query_plan.natural and (query_plan.task_ids or query_plan.identifiers or query_plan.versions):
            return True
        if not query_plan.natural:
            return bool(_query_score(item, query, workspace))
        plan = resume_topic_plan(query) or prompt_query_plan('How ' + query)
        if plan is None or not plan.get('terms'):
            return True
        soft = dict(plan, identifiers=[], versions=[], minimum=1,
            terms=[(term, term[:-1] if len(term) > 4 and term.isascii() and term.endswith('s') and
                    not term.endswith('ss') else stem) for term, stem in plan['terms']])
        score = bool(prompt_match_score(project_lexical_text(_text(item)), soft))
        if not score and fallback_used:
            import re
            # A local script mismatch cannot prove project evidence irrelevant.
            # Keep bounded project-state lanes and use lexical matches to rank.
            return bool(re.search(r'[а-яё]', query, re.I) and not re.search(r'[а-яё]', _text(item), re.I))
        return score
    brief['projection'] = query_plan.projection
    brief['anchored_natural'] = bool(query_plan.natural and (query_plan.task_ids or query_plan.identifiers or query_plan.versions))
    brief.update(enabled=True, coverage=snapshot.get('coverage') or {}, pending=bool(snapshot.get('pending')),
                 revision=snapshot.get('revision'), deletion_generation=snapshot.get('deletion_generation'))
    def eligible(item: Any) -> bool:
        return (_safe(item, workspace, exclude_session_id) and
                _scope_matches(item, environment, version, workspace))
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
                       (query_plan.projection in {'continuity', 'history'} or item.get('is_current_head', True) is not False) and relevant(item)),
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
                'outcome': 0, 'open_work': 1, 'prior_open_work': 2, 'summary': 3}
            for field in sorted(record['fields'], key=lambda field: priorities.get(field['name'], 3)):
                for pointer in field['evidence']:
                    identity = (pointer.get('source_id'), pointer.get('start'), pointer.get('end'))
                    if identity in seen_refs or not pointer.get('quote_excerpt'):
                        continue
                    if query_plan.projection == 'environments':
                        import re
                        from .project_memory_query import environment_names
                        observed = set(environment_names(pointer['quote_excerpt']))
                        if not observed or observed.intersection(query_plan.excluded_environments):
                            continue
                        if query_plan.environments and not observed.intersection(query_plan.environments):
                            continue
                    seen_refs.add(identity)
                    selected.append({'name': field['name'] + '_source_excerpt', 'text': '', 'evidence': [pointer]})
            # These are quoted evidence units, not regenerated state claims.
            # Each source remains physically attached to its own excerpt.
            record['fields'] = selected[:8] or record['fields']
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
    brief['records'] = records
    selected_ids = {item.get('id') for item in brief['records']}
    brief['relations'] = [dict(item) for item in snapshot.get('relations', []) if
                          item.get('from') in selected_ids and item.get('to') in selected_ids
                          and _safe(item, workspace, exclude_session_id)][:32]
    brief['xml'] = render_project_brief(brief, budget)
    return brief
