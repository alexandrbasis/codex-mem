"""Shared host events, provenance and bounded context operations.

Adapters own their wire format. Legacy hook sources remain valid without replay.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
import hashlib
import json
from typing import Any, Mapping

_EVENTS = {
    'user_message': ('user_intent', 'UserPromptSubmit'),
    'assistant_message': ('assistant_report', 'Stop'),
    'tool_result': ('tool_record', 'PostToolUse'),
    'compaction': ('lifecycle_marker', 'PreCompact'),
}
_LEGACY_EVENTS = {legacy: event for event, (_, legacy) in _EVENTS.items()}


def source_event(source: object) -> str | None:
    value = str(source or '')
    pieces = value.split(':', 3)
    if len(pieces) >= 2 and pieces[0] == 'hook':
        return _LEGACY_EVENTS.get(pieces[1])
    if len(pieces) >= 3 and pieces[0] == 'host' and pieces[1] in {'codex', 'pi'}:
        return pieces[2] if pieces[2] in _EVENTS else None
    return None


def source_role(source: object) -> str:
    event = source_event(source)
    if event:
        return _EVENTS[event][0]
    value = str(source or '')
    if value.startswith('processor:'):
        return 'derived_note'
    if value.startswith('import:'):
        return 'imported_memory'
    return 'unclassified'


def is_capture_source(source: object) -> bool:
    # Unknown legacy hook sources stay raw and cannot enter curated retrieval.
    return str(source or '').startswith('hook:') or source_event(source) is not None


def validate_identifier(value: str | None) -> None:
    if value is not None and (not isinstance(value, str) or not value or len(value) > 256
                              or any(ord(c) < 32 for c in value) or '|' in value):
        raise ValueError('invalid host identifier')


def session_namespace(host: str, session_id: str | None, branch: str | None = None) -> str | None:
    if host not in {'codex', 'pi'}:
        raise ValueError('unsupported interactive host')
    validate_identifier(session_id)
    validate_identifier(branch)
    if host == 'codex' and not branch:
        # Existing Codex sessions are the reserved legacy namespace.
        return session_id
    identity = json.dumps([session_id, branch], ensure_ascii=False, separators=(',', ':'))
    # The separator is outside the native Codex identifier alphabet.
    return host + '|' + hashlib.sha256(identity.encode()).hexdigest()


@dataclass(frozen=True)
class HostEvent:
    host: str
    project: str
    session_id: str | None
    event: str
    role: str
    body: str
    branch: str | None = None
    event_id: str | None = None
    message_id: str | None = None
    tool_id: str | None = None
    turn_id: str | None = None
    title: str = ''
    tool_capture: Mapping[str, Any] | None = None
    injected: bool = False
    private: bool = False
    tags: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        from .store import project_key
        if self.host not in {'codex', 'pi'} or self.event not in _EVENTS:
            raise ValueError('unsupported host event')
        if self.role != _EVENTS[self.event][0]:
            raise ValueError('event role does not match its source')
        object.__setattr__(self, 'project', project_key(self.project))
        for name in ('session_id', 'branch', 'event_id', 'message_id', 'tool_id', 'turn_id'):
            value = getattr(self, name)
            validate_identifier(value)
        if not isinstance(self.body, str) or not isinstance(self.title, str):
            raise ValueError('host event text must be a string')

    @property
    def source(self) -> str:
        if self.host == 'codex' and not (self.branch or self.event_id or self.message_id):
            legacy = 'hook:' + _EVENTS[self.event][1]
            return legacy + ':' + self.tool_id if self.tool_id else legacy
        metadata = {name: getattr(self, name) for name in
                    ('session_id', 'branch', 'event_id', 'message_id', 'tool_id', 'turn_id')
                    if getattr(self, name) is not None}
        return 'host:' + self.host + ':' + self.event + ':' + json.dumps(
            metadata, ensure_ascii=False, separators=(',', ':'))

    @property
    def stored_session(self) -> str | None:
        return session_namespace(self.host, self.session_id, self.branch)

    @property
    def dedupe_key(self) -> str | None:
        fields = ('event_id', 'tool_id', 'message_id') if self.event == 'tool_result' else ('event_id', 'message_id')
        if self.host == 'codex':
            fields += ('turn_id',)
        identity = next(((name, getattr(self, name)) for name in fields if getattr(self, name)), None)
        if identity is None:
            return None
        parts = [self.host, self.session_id, self.branch, self.event, self.turn_id, identity]
        return 'host:' + hashlib.sha256(json.dumps(parts, ensure_ascii=False).encode()).hexdigest()

    @property
    def legacy_dedupe_alias(self) -> str | None:
        if (self.host != 'codex' or self.branch or self.event_id or self.message_id
                or not (self.turn_id or self.tool_id)):
            return None
        prefix = {'user_message': 'prompt', 'assistant_message': 'final',
                  'tool_result': 'tool', 'compaction': 'precompact'}[self.event]
        from .tool_io import strip_injected_memory
        # Match the pre-upgrade fingerprint before redaction and truncation.
        legacy_body, _ = strip_injected_memory(self.body)
        fingerprint = '|'.join([prefix, self.session_id or '', self.turn_id or '',
                                self.tool_id or '', legacy_body])
        return f'hook:{prefix}:' + hashlib.sha256(fingerprint.encode()).hexdigest()


def capture_event(event: HostEvent, store: Any) -> dict[str, Any]:
    """Capture completed evidence, or return a content-free omission receipt."""
    from .config import (automatic_capture_enabled, hooks_disabled, load_config,
                         private_prompt_gate_enabled, private_prompt_gate_active,
                         mark_private_prompt_gate, clear_private_prompt_gate)
    from .privacy import redact_text
    from .tool_io import is_private_prompt, strip_injected_memory
    try:
        config = load_config(store.data_dir)
        if hooks_disabled() or not config.get('capture_enabled') or not automatic_capture_enabled(event.project, config):
            return {'status': 'omitted', 'reason': 'capture_disabled'}
        if event.event == 'tool_result' and not config.get('capture_tools'):
            return {'status': 'omitted', 'reason': 'tool_capture_disabled'}
        key = session_state_key(event.project, event.stored_session)
        private = event.private or (event.event == 'user_message' and is_private_prompt(event.body))
        if private_prompt_gate_enabled(config):
            from .private_gate import mark_private, clear_private, private_active
            if event.event == 'user_message':
                if private:
                    mark_private(key, store.data_dir)
                    mark_private_prompt_gate(key, turn_id=event.turn_id, data_dir=store.data_dir)
                elif not event.injected:
                    clear_private_prompt_gate(key, data_dir=store.data_dir)
                    clear_private(key, store.data_dir)
            elif (private_active(key, store.data_dir) or private_prompt_gate_active(
                    key, turn_id=event.turn_id, data_dir=store.data_dir)):
                return {'status': 'omitted', 'reason': 'private_turn'}
        if event.injected or event.private or private and redact_text(event.body) != event.body:
            return {'status': 'omitted', 'reason': 'private_or_injected'}
        body, removed = strip_injected_memory(event.body)
        body = redact_text(body).strip()
        if not body:
            return {'status': 'omitted', 'reason': 'empty_or_injected'}
        tool_capture = event.tool_capture
        if tool_capture is not None:
            from .tool_io import ToolCapture
            provenance = dict(project=event.project, session_id=event.stored_session or 'anonymous',
                              turn_id=event.turn_id, tool_use_id=event.tool_id)
            tool_capture = (replace(tool_capture, **provenance) if isinstance(tool_capture, ToolCapture)
                            else dict(tool_capture, **provenance))
        result = store.remember(event.project, title=redact_text(event.title or event.event)[:300],
                                body=body[:6000], kind='tool' if event.event == 'tool_result' else 'session',
                                session_id=event.stored_session, turn_id=event.turn_id,
                                source=event.source, dedupe_key=event.dedupe_key, tags=event.tags,
                                dedupe_alias=event.legacy_dedupe_alias,
                                tool_capture=tool_capture)
        return dict(result if isinstance(result, Mapping) else {}, status='captured', omitted_chars=max(0, len(body) - 6000),
                    injected_sections_removed=removed)
    except Exception as exc:
        from .hook_diagnostics import trace_error
        trace_error('capture_remember_failed', exc)
        return {'status': 'unavailable', 'reason': 'capture_unavailable'}


def register_source_functions(connection: Any) -> None:
    """Use the same classification for SQL selection and Python validation."""
    connection.create_function('codex_mem_source_role', 1, source_role, deterministic=True)
    connection.create_function('codex_mem_is_capture', 1, is_capture_source, deterministic=True)


def session_state_key(project: str, session_id: str | None) -> str:
    project_digest = hashlib.sha256(project.encode('utf-8')).hexdigest()[:16]
    return (f'project:{project_digest}:session:{session_id}' if session_id
            else f'project:{project_digest}:anonymous')


@dataclass(frozen=True)
class ContextRequest:
    host: str
    project: str
    session_id: str | None = None
    branch: str | None = None
    query: str = ''
    budget: int = 6000
    exclude_current_session: bool = True
    allow_remote: bool = True
    remote_deadline: float | None = None

    def __post_init__(self) -> None:
        from .store import project_key
        object.__setattr__(self, 'project', project_key(self.project))
        session_namespace(self.host, self.session_id, self.branch)
        if isinstance(self.budget, bool) or not isinstance(self.budget, int) or self.budget < 0:
            raise ValueError('context budget must be nonnegative')


@dataclass(frozen=True)
class ContextResult:
    text: str = ''
    reason: str | None = None
    omitted_chars: int = 0
    untrusted: bool = True


def retrieve_context(request: ContextRequest, store: Any) -> ContextResult:
    """Return bounded untrusted data; host adapters own response framing."""
    from .config import automatic_capture_enabled, hooks_disabled, load_config, MAX_CONTEXT_CHARS
    from .query import normalize_retrieval_query
    from .tool_io import is_private_prompt, strip_injected_memory
    from .privacy import redact_text
    try:
        config = load_config(store.data_dir)
        if hooks_disabled() or not automatic_capture_enabled(request.project, config):
            return ContextResult(reason='capture_disabled')
        budget = min(request.budget, int(config.get('max_context_chars', MAX_CONTEXT_CHARS)), MAX_CONTEXT_CHARS)
        if budget < 128:
            return ContextResult(reason='budget_too_small')
        query = normalize_retrieval_query(redact_text(strip_injected_memory(request.query)[0]))
        if request.query.strip() and not query.strip():
            return ContextResult(reason='routing_only_query')
        options: dict[str, Any] = {'hook_mode': True}
        if config.get('jev_retrieval_enabled'):
            from .config import private_prompt_gate_active, private_prompt_gate_enabled
            from .private_gate import private_active
            key = session_state_key(request.project, session_namespace(
                request.host, request.session_id, request.branch))
            private_turn = private_prompt_gate_enabled(config) and (
                private_active(key, store.data_dir) or private_prompt_gate_active(key, data_dir=store.data_dir))
            if not request.allow_remote or is_private_prompt(request.query) or private_turn:
                options['allow_remote'] = False
            if request.remote_deadline is not None:
                options['remote_deadline'] = request.remote_deadline
        context = store.context(request.project, query=query[:1000], budget=budget,
            exclude_session=session_namespace(request.host, request.session_id, request.branch)
                if request.exclude_current_session else None, **options)
        if not isinstance(context, str):
            return ContextResult(reason='invalid_context')
        omitted = max(0, len(context) - budget)
        query_omitted = max(0, len(query) - 1000)
        reason = 'budget_exhausted' if omitted else 'query_truncated' if query_omitted else None
        return ContextResult(context[:budget], reason, omitted + query_omitted)
    except Exception as exc:
        from .hook_diagnostics import trace_error
        trace_error('context_read_failed', exc)
        return ContextResult(reason='context_unavailable')
