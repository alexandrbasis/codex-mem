"""Local read planning: separate exact subjects from answer instructions.

This planner does not judge evidence or infer task ownership. Literal lookups
keep the existing exact constraints; natural questions use bounded subject
windows and rank answer facets after those windows are scoped.
"""
from __future__ import annotations

from dataclasses import dataclass
import re
from pathlib import Path
from typing import Any, Literal, Mapping, Sequence

from .jev_retrieval import exact_constraints
from .retrieval import historical_query, prompt_match_score, prompt_query_plan, resume_topic_plan

_TASK = re.compile(r'(?<![\w-])[A-Za-z][A-Za-z0-9]{1,15}-[0-9]{1,10}(?![\w-])')
_NATURAL = re.compile(r'\?|^(?:resume|continue|summari[sz]e|compare|explain|show|which|what|how|why|when|where|tell|list|describe|какой|какие|что|как|почему|продолж|сравни|покажи|расскажи)', re.I)
_COMPARE = re.compile(r'\b(?:compar(?:e[ds]?|ing|isons?)|versus|vs|changed|evolved|difference|then)\b|сравн|изменил|отлич', re.I)
_CONTINUE = re.compile(r'\b(?:resume|continue|continuing|continuation)\b|pick\s+up|продолж[а-яё]*', re.I)
_HISTORY = re.compile(r'\b(?:originally|as[- ]of|historical|history)\b|первоначаль|истори|по состоянию на', re.I)
_ENVS = re.compile(r'(?<![\w.-])(?:production|prod|staging|development|dev|local(?:ly)?|локальн[а-яё]*|стейджинг[а-яё]*|продакш(?:е|ё)?н[а-яё]*)(?![\w-]|\.[\w])', re.I)

# Full grammatical frames distinguish an answer facet from a topical subject.
# Any residual subject words keep the normal lexical candidate window.
_OUTCOME_FACET = r'(?:delivery|deliveries|results?|outcomes?|work|progress|status|completion|implementation)'
_OUTCOME_STATE = r'(?:supported|confirmed|observed|established|verified|completed?|delivered|finished|accepted)'
_GENERIC_OUTCOME = re.compile(
    rf'(?:what|which)\s+(?:'
    rf'(?:{_OUTCOME_FACET})\s+(?:(?:is|are|was|were|has been|have been)\s+)?{_OUTCOME_STATE}'
    rf'|(?:has|have)\s+(?:actually\s+)?been\s+{_OUTCOME_STATE}'
    r'|did\s+(?:we|you)\s+(?:deliver|complete|finish|establish)'
    r'|have\s+(?:we|you)\s+(?:delivered|completed|finished|established))'
    rf'|what\s+is\s+(?:the\s+)?(?:current\s+)?(?:{_OUTCOME_FACET})(?:\s+status)?', re.I)
_RECENT_REQUEST = r'(?:the\s+)?(?:last|latest|most recent)\s+(?:request|task|instruction)'
_EARLIER_WORK = r'(?:the\s+)?(?:prior|previous|earlier|existing)\s+(?:work|tasks?|requests?|instructions?)'
_GENERIC_REQUEST_RELATION = re.compile(
    rf'how\s+(?:(?:does|did)\s+{_RECENT_REQUEST}\s+(?:relate|connect|compare)'
    rf'|(?:is|was)\s+{_RECENT_REQUEST}\s+(?:related|connected|linked))\s+(?:to|with)\s+{_EARLIER_WORK}'
    rf'|is\s+{_RECENT_REQUEST}\s+(?:a\s+)?(?:continuation|extension)\s+of\s+{_EARLIER_WORK}'
    r'(?:\s+or\s+(?:a\s+)?separate\s+task)?', re.I)


_GENERIC_ROLE_ATTRIBUTION = re.compile(
    r'what\s+(?:is|was)\s+(?:the\s+)?(?:standing|status|support|evidence)\s+(?:of|for)\s+'
    r'(?:the\s+)?assistant(?:[’\']s)?\s+(?:report|prediction|estimate|claim|assessment)', re.I)


_GENERIC_OBJECT = rf'(?:(?:completed|accepted|observed|supported)\s+)?(?:{_OUTCOME_FACET}|outputs?|evidence)'
_EVIDENCE_AGENT = r'(?:the\s+)?(?:(?:current|available|recorded|captured)\s+)?(?:evidence|records?|reports?|sources?|tools?)'
_EVIDENCE_VERB = r'(?:establish|support|show|confirm|demonstrate|prove)'
_GENERIC_EVIDENCE = re.compile(
    rf'(?:what|which)\s+{_GENERIC_OBJECT}\s+(?:does|did|can|could)\s+{_EVIDENCE_AGENT}\s+{_EVIDENCE_VERB}'
    rf'|what\s+(?:does|did|can|could)\s+{_EVIDENCE_AGENT}\s+{_EVIDENCE_VERB}'
    rf'|(?:what|which)\s+{_GENERIC_OBJECT}\s+(?:can|may|should)\s+be\s+(?:retained|supported|established|confirmed)'
    rf'|(?:what|which)\s+{_GENERIC_OBJECT}\s+(?:can|should)\s+(?:we|you)\s+retain'
    rf'|(?:what|which)\s+(?:evidence|records?|sources?)\s+(?:establishes?|supports?|confirms?|shows?)\s+(?:the\s+)?{_GENERIC_OBJECT}'
    rf'|what\s+(?:does|did|can)\s+{_EVIDENCE_AGENT}\s+{_EVIDENCE_VERB}\s+(?:about|regarding)\s+(?:the\s+)?{_GENERIC_OBJECT}', re.I)
_ASSISTANT_ASSERTION = (r'(?:the\s+)?(?:(?:earlier|prior|previous|original)\s+)?assistant(?:[’\']s)?\s+'
                        r'(?:(?:completion|passing)\s+)?(?:claim|report|statement|prediction|estimate|closure|assessment)')
_EXPLICIT_AS_OF = re.compile(r'\bas[- ]of\b|\bat the time\b|\b(?:originally|historical|history)\b|по состоянию на|истори', re.I)


def validated_project_scopes(project: str | Path, selected_project_scopes: Sequence[str | Path] | None,
                             settings: Mapping[str, Any]) -> tuple[str, ...]:
    """Validate caller-selected scope metadata without discovering other projects."""
    from .config import project_memory_enabled
    from .store import project_key
    owner = project_key(project)
    if selected_project_scopes is None:
        return (owner,)
    if (isinstance(selected_project_scopes, (str, bytes)) or
            not isinstance(selected_project_scopes, Sequence) or len(selected_project_scopes) > 16):
        raise ValueError('selected_project_scopes must contain at most 16 absolute project paths')
    paths = [owner]
    for value in selected_project_scopes:
        if not isinstance(value, (str, Path)) or not Path(value).expanduser().is_absolute():
            raise ValueError('selected project scopes must be absolute paths')
        scope = project_key(value)
        if not project_memory_enabled(scope, settings):
            raise ValueError('selected project scope is not enabled')
        paths.append(scope)
    return tuple(dict.fromkeys(paths))


def scope_content_query(query: str, project: str | Path,
                        selected_project_scopes: Sequence[str | Path] | None = None) -> tuple[str, tuple[str, ...]]:
    """Separate explicit natural scope selectors from literal content anchors.

    Only exact canonical basenames in scope positions qualify. Quoting or an
    ambiguous basename keeps a token literal. This never changes a read owner.
    """
    if not _NATURAL.search(query.strip()):
        return query, ()
    if selected_project_scopes is not None and (
            isinstance(selected_project_scopes, (str, bytes)) or
            not isinstance(selected_project_scopes, Sequence) or len(selected_project_scopes) > 16):
        raise ValueError('selected_project_scopes must contain at most 16 project paths')
    scopes = {str(Path(value).expanduser().resolve()) for value in
              (project, *(selected_project_scopes or ()))}
    aliases: dict[str, set[str]] = {}
    for scope in scopes:
        aliases.setdefault(Path(scope).name.casefold(), set()).add(scope)
    quoted = [match.span() for match in re.finditer(r'`[^`\n]*`|"[^"\n]*"|\'[^\'\n]*\'', query)]
    changes, used, candidates = [], [], []
    for alias, owners in aliases.items():
        if (not alias or len(owners) != 1 or _ENVS.fullmatch(alias) or _TASK.fullmatch(alias)
                or re.fullmatch(r'v?\d+(?:\.\d+)+', alias, re.I)):
            continue
        for match in re.finditer(r'(?<![\w./:-])' + re.escape(alias) + r'(?![\w./:-])', query, re.I):
            candidates.append((match.start(), match.end(), alias))
    for left, right, alias in sorted(candidates):
        if any(start <= left < end for start, end in quoted):
            continue
        prefix = query[:left]
        position = re.search(r'\b(?P<prep>for|in|from|to|on|about|regarding|within|of|project|projects|compare|versus|vs|between|with|and)\s+$', prefix, re.I)
        if not position:
            continue
        # Keep the comparison operator, but remove a redundant scope clause.
        prep = position.group('prep').casefold()
        if prep in {'with', 'and'} and not (_COMPARE.search(query) and changes):
            continue
        start = left if prep in {'compare', 'versus', 'vs', 'between'} else position.start()
        changes.append((start, right))
        used.append(alias)
    for start, end in sorted(changes, reverse=True):
        query = query[:start] + ' ' + query[end:]
    return ' '.join(query.split()), tuple(dict.fromkeys(used))


def _requirement_state_subject(query: str) -> str | None:
    """Recognize answer fields only when every clause has no remaining subject."""
    question = re.sub(r'^given\s+(?:the\s+)?(?:check|evidence|record|validation)\s+and\s+(?:my|the user[’\']s)\s+(?:last|latest)\s+message,\s*', '', _question_body(query), flags=re.I)
    question = re.sub(r'\s+and\s+why$', '', question, flags=re.I)
    question = re.sub(r'\b(?:in|for)\s+(?:this|the current)\s+project\b', '', question, flags=re.I)
    question = re.sub(r'\s+after\s+(?:the\s+)?(?:continuation|(?:last|latest)\s+(?:request|instruction))$', '', question, flags=re.I)
    clauses = [part.strip() for part in re.split(r'[,;?]|\band\b', question, flags=re.I) if part.strip()]
    noun = r'(?:requirements?|constraints?|obligations?|tasks?|steps?|outputs?|rules?|permissions?|work|deliverables?|additions?|features?|requests?)'
    state = r'(?:cancell?ed|removed|dropped|deferred|replaced|retained|active|binding|open|pending|remaining|unfinished|authorized|requested|required|ready|known|validated)'
    patterns = (
        rf'(?:which|what)\s+(?:requested\s+)?(?:(?P<subject>.+?)\s+)?{noun}\s+'
        r'(?:did\s+(?:I|we|the user)\s+(?:cancel|withdraw|drop|remove)'
        r'|(?:was|were|has been|have been)\s+(?:cancell?ed|withdrawn|dropped|removed)'
        r'|(?:is|are)\s+no\s+longer\s+(?:requested|required))',
        r'what\s+(?:should|must)\s+be\s+(?:omitted|removed|dropped)\s+from\s+(?:the\s+)?(?:continuing|remaining)\s+work',
        rf'(?:which|what)\s+(?:unfinished|remaining|pending)\s+{noun}\s+remains?',
        rf'what\s+(?:is|are)\s+(?:still\s+)?{state}(?:\s+now)?',
        rf'what\s+remains?\s+(?:still\s+)?{state}',
        r'what\s+does\s+(?:the\s+)?(?P<subject>.+?)\s+(?:continuation|request|instruction)\s+require',
        rf'(?:which|what)\s+(?:requested\s+)?(?:(?P<subject>.+?)\s+)?{noun}\s+(?:(?:is|are|was|were|has been|have been)\s+)?(?:still\s+)?{state}(?:\s+(?:for|about|regarding)\s+(?P<topic>.+))?',
        rf'(?:which|what)\s+(?:requested\s+)?(?:(?P<subject>.+?)\s+)?{noun}\s+(?:still\s+)?(?:remains?|survives?|applies?)(?:\s+(?:active|binding|open|requested|required))?(?:\s+(?:for|about|regarding)\s+(?P<topic>.+))?',
        r'(?:what|which)\s+(?P<subject>.+?)\s+(?:still\s+)?remains?\s+(?:requested|required)(?:\s+(?:for|about|regarding)\s+(?P<topic>.+))?',
        r'what\s+(?:should|must|will)\s+happen\s+to\s+(?P<subject>.+)',
        r'what\s+remains?\s+before\s+(?P<subject>.+?)\s+(?:can|may|must|should)\s+be\s+.+',
        r'what\s+(?:replaced|replaces)\s+(?:it|them|these|those)',
        r'what\s+(?:remains?|survives?)',
        r'what\s+(?:must|should)\s+(?:still\s+)?be\s+done',
    )
    if not clauses:
        return None
    subjects = []
    for clause in clauses:
        match = next((match for pattern in patterns if (match := re.fullmatch(pattern, clause, re.I))), None)
        if match is None:
            return None
        subjects.extend(value for value in match.groupdict().values() if value)
    return re.sub(r'(?<=[A-Za-z])-(?=[A-Za-z])', ' ', ' '.join(subjects))


def _question_body(query: str) -> str:
    question = ' '.join(query.strip().rstrip('?.!').split())
    # A recognized environment is a retained scope constraint, not a topic.
    return re.sub(r'\s+in\s+(?:the\s+)?(?:local|staging|production|development)(?:\s+environment)?$', '', question, flags=re.I)


def _completion_subject(query: str) -> str | None:
    if _EXPLICIT_AS_OF.search(query):
        return None
    question = _question_body(query)
    frames = (
        r'(?:has|have)\s+(?:the\s+)?(?P<subject>.+?)\s+(?:actually\s+)?(?:received|completed|delivered|installed|published|submitted|transferred)\s+(?:the\s+)?(?P<object>.+)',
        r'(?:has|have)\s+(?:the\s+)?(?P<subject>.+?)\s+(?:actually\s+)?(?:been\s+)?(?:demonstrated|established|verified|confirmed|completed|delivered|finished)',
        rf'(?:does|did|can)\s+(?:the\s+)?(?:record|evidence)\s+{_EVIDENCE_VERB}\s+that\s+(?:the\s+)?(?P<subject>.+?)\s+(?:happened|occurred|(?:is|was)\s+(?:complete|finished))',
        r'(?:can|may|should)\s+(?:the\s+)?(?P<subject>.+?)\s+be\s+treated\s+as\s+(?:completed|published|delivered|finished)',
        r'(?:is|are)\s+(?:the\s+)?(?P<subject>.+?)\s+(?:established|verified|demonstrated|confirmed)\s+by\s+(?:the\s+)?evidence',
    )
    match = next((match for frame in frames if (match := re.fullmatch(frame, question, re.I))), None)
    if match:
        subject = ' '.join(value for value in match.groupdict().values() if value)
        return re.sub(r'\b(?:finished|completed|final|updated)\b', '', subject, flags=re.I).strip()
    return None


def _assessment_subject(query: str) -> str | None:
    """Separate present evidence assessment from reporting what was said as of a time."""
    if _EXPLICIT_AS_OF.search(query):
        return None
    question = _question_body(query)
    credibility = r'(?:accurate|credible|correct|supported|justified|valid|still\s+valid)'
    match = re.fullmatch(rf'(?:is|was|does)\s+{_ASSISTANT_ASSERTION}'
        rf'(?:\s+(?:about|for)\s+(?P<before>.+?))?\s+(?:(?:remain|remains)\s+)?{credibility}'
        r'(?:\s+for\s+(?P<after>.+))?', question, re.I)
    if match:
        return ' '.join(value for value in (match.group('before'), match.group('after')) if value)
    if re.fullmatch(rf'how\s+(?:does|did)\s+{_ASSISTANT_ASSERTION}\s+compare\s+(?:with|against)\s+{_EVIDENCE_AGENT}', question, re.I):
        return ''
    if re.fullmatch(rf'does\s+{_ASSISTANT_ASSERTION}\s+(?:still\s+)?hold\s+up', question, re.I):
        return ''
    about = re.fullmatch(rf'what\s+(?:does|did)\s+{_ASSISTANT_ASSERTION}\s+{_EVIDENCE_VERB}\s+(?:about|regarding|for)\s+(?P<subject>.+)', question, re.I)
    if about:
        return about.group('subject')
    if re.fullmatch(rf'what\s+(?:does|did)\s+{_ASSISTANT_ASSERTION}\s+{_EVIDENCE_VERB}', question, re.I):
        return ''
    match = re.fullmatch(rf'(?:did|does|can)\s+{_ASSISTANT_ASSERTION}\s+{_EVIDENCE_VERB}\s+(?P<subject>.+)', question, re.I)
    if match:
        subject = re.sub(r'\s+(?:existed|exists|(?:was|were|is|are)\s+(?:complete|finished))$', '', match.group('subject'), flags=re.I)
        return '' if re.fullmatch(rf'(?:the\s+)?{_GENERIC_OBJECT}', subject, re.I) else subject
    return None


def _reason_subject(query: str) -> str | None:
    """Recognize reason questions while keeping the named action's object."""
    question = _question_body(query)
    frames = (
        r'why\s+(?:does|do)\s+(?P<subject>.+?)\s+(?:matter|apply)(?:\s+(?:here|now))?',
        r'why\s+(?:is|are)\s+(?P<subject>.+?)\s+(?:required|needed|necessary|important)',
        r'why\s+(?:keep|retain|preserve)\s+(?P<subject>.+)',
    )
    match = next((match for frame in frames if (match := re.fullmatch(frame, question, re.I))), None)
    if match is None:
        passive = re.fullmatch(r'why\s+(?:was|were|is|are)\s+(?P<subject>.+?)\s+(?:requested|required|chosen|needed)', question, re.I)
        match = passive
    if match is None:
        return None
    subject = match.group('subject')
    if re.fullmatch(r'(?:that|this|such|the same)\s+(?:choice|decision|restriction|requirement|constraint|rule|grouping|visibility|sequence|scope)', subject, re.I):
        return ''
    return subject


def _execution_question(query: str) -> tuple[str, Literal['earliest', 'latest'] | None] | None:
    """Parse an assessment's operation identity, never its truth or polarity."""
    if _EXPLICIT_AS_OF.search(query):
        return None
    question = _question_body(query)
    frames = (
        r'(?:did|does|can)\s+(?P<event>.+?)\s+(?:pass|succeed|fail)',
        r'how\s+should\s+(?P<event>.+?)\s+be\s+(?:recorded|classified|reported|assessed)',
        r'(?:did|does|can)\s+(?P<event>.+?)\s+(?:preserve|retain|maintain|verify|confirm|support)\s+(?P<object>.+)',
        r'(?:was|were|is|are)\s+(?P<event>.+?)\s+(?:successful|supported|verified|correct)(?:\s+by\s+the\s+check)?',
        r'what\s+(?:contradicted|supported|confirmed|refuted)\s+(?P<event>.+)',
        r'what\s+(?:did|does)\s+(?P<event>.+?)\s+(?:show|establish|confirm|support|prove)',
    )
    match = next((match for frame in frames if (match := re.fullmatch(frame, question, re.I))), None)
    if match is None:
        return None
    event = re.fullmatch(
        r'(?:the\s+)?(?:assistant[\'’]s\s+)?'
        r'(?:(?P<ordinal>first|initial|original|earliest|last|latest|most recent)\s+)?'
        r'(?:(?:captured|recorded|observed)\s+)?'
        r'(?:(?P<subject>.+?)\s+)?(?P<operation>check|test|run|execution|operation|validation|comparison|claim|report|statement|[a-z]{3,}(?:tion|ment))'
        r'(?:\s+(?:for|about|regarding)\s+(?P<topic>.+))?', match.group('event'), re.I)
    if event is None:
        return None
    subject = event.group('subject') or ''
    operation = event.group('operation').casefold()
    if operation not in {'check', 'test', 'run', 'execution', 'operation', 'validation', 'comparison', 'claim', 'report', 'statement'}:
        subject = ' '.join(value for value in (subject, operation) if value)
    # Completion is an assertion facet; other modifiers remain topical.
    if re.fullmatch(r'(?:complet(?:e(?:ness)?|ion)|passing)', subject, re.I):
        subject = ''
    obj = match.groupdict().get('object') or ''
    if re.fullmatch(_ASSISTANT_ASSERTION, obj, re.I):
        obj = ''
    subject = ' '.join(value for value in (subject, event.group('topic'), obj) if value)
    ordinal = (event.group('ordinal') or '').casefold()
    order = 'earliest' if ordinal in {'first', 'initial', 'original', 'earliest'} else 'latest' if ordinal else None
    return subject, order


def _soft_subject(subject: str | None, anchors: Sequence[str], versions: Sequence[str],
                  environments: Sequence[str]) -> str | None:
    """Hard scopes are checked separately, so they cannot satisfy a soft topic."""
    if subject is None:
        return None
    original = subject
    for anchor in sorted(anchors, key=len, reverse=True):
        subject = re.sub(r'(?<![\w./:-])(?:[`"\']?' + re.escape(anchor) + r'[`"\']?)(?![\w./:-])', '', subject, flags=re.I)
    for version in versions:
        subject = re.sub(r'(?<![\w.])(?:versions?\s+)?v?' + re.escape(version) + r'(?![\w.])', '', subject, flags=re.I)
    for environment in environments:
        subject = re.sub(r'\b(?:in|for|excluding|exclude)\s+(?:the\s+)?' + re.escape(environment) +
                         r'(?:\s+environment)?\b', '', subject, flags=re.I)
    subject = _ENVS.sub(lambda match: '' if _environment_name(match.group()) in environments else match.group(), subject)
    if subject != original:
        subject = re.sub(r'^(?:(?:for|in|on|about|regarding|of|from|to)\s+)+|'
                         r'(?:\s+(?:for|in|on|about|regarding|of|from|to))+$', '', subject.strip(), flags=re.I)
    subject = re.sub(r'\bduring\b', ' ', subject, flags=re.I)
    return re.sub(r'(?<=[A-Za-z])-(?=[A-Za-z])', ' ', ' '.join(subject.split()))


def requests_source_evidence(query: str) -> bool:
    """A grammatical evidence facet changes allocation, never source eligibility."""
    return bool(re.search(
        r'\bwhat\s+(?:(?:does|did|can)\s+[^?;]{1,180}?\s+(?:establish|confirm|show|prove|support)\b'
        r'|(?:establishes|confirms|shows|proves|supports)\s+(?:whether|the\s+claim)\b)'
        r'|\b(?:did|does|was|were)\s+[^?;]{1,180}?\b(?:established|verified|prove|confirm|support)\b',
        query, re.I))


def _generic_facet(query: str) -> tuple[bool, bool]:
    question = _question_body(query)
    relation = bool(_GENERIC_REQUEST_RELATION.fullmatch(question))
    boundary = re.fullmatch(r'(?:does|do)\s+(?:the\s+)?(?:requirements?|constraints?|permissions?)\s+'
                            r'(?:carry over|apply)(?:\s*\?\s*explain\s+the\s+project\s+boundary)?', question, re.I)
    return bool(_GENERIC_OUTCOME.fullmatch(question) or _GENERIC_EVIDENCE.fullmatch(question) or
                _GENERIC_ROLE_ATTRIBUTION.fullmatch(question) or
                _requirement_state_subject(query) == '' or boundary) or relation, relation


def environment_names(text: str) -> tuple[str, ...]:
    return tuple(dict.fromkeys(_environment_name(match.group()) for match in _ENVS.finditer(text)))

def _environment_name(value: str) -> str:
    value = value.casefold()
    for stem, canonical in (('локальн', 'local'), ('стейджинг', 'staging'), ('продакш', 'production')):
        if value.startswith(stem):
            return canonical
    return {'locally': 'local', 'prod': 'production', 'dev': 'development'}.get(value, value)


def _negative_spans(query: str) -> tuple[tuple[int, int], ...]:
    operator = re.compile(r'\b(?:exclude|excluding|ignore)\b|\bnot\s+(?:a\s+continuation\s+of\s+)?|\b(?:separate|different)\s+from\b|\bunrelated\s+to\b', re.I)
    spans = []
    for match in operator.finditer(query):
        remainder = query[match.end():]
        boundary = re.search(r'[,;.!?](?![0-9])|\b(?:with|versus|vs|but|for)\b', remainder, re.I)
        end = match.end() + (boundary.start() if boundary else min(100, len(remainder)))
        spans.append((match.start(), end))
    return tuple(spans)


def _inside(position: int, spans: tuple[tuple[int, int], ...]) -> bool:
    return any(start <= position < end for start, end in spans)


@dataclass(frozen=True)
class QueryPlan:
    natural: bool
    historical: bool
    continuation: bool
    projection: str
    task_ids: tuple[str, ...]
    identifiers: tuple[str, ...]
    versions: tuple[str, ...]
    excluded_versions: tuple[str, ...]
    environments: tuple[str, ...]
    excluded_environments: tuple[str, ...]
    candidate_queries: tuple[str, ...]
    topic_query: bool
    generic_project_state: bool
    assesses_current_evidence: bool
    content_query: str
    scope_aliases: tuple[str, ...]
    requirement_subject: str | None
    exact: Mapping[str, Any]
    answer_subject: str | None = None
    answer_facets: tuple[Literal['requirements', 'current_revision', 'reason', 'execution_assessment'], ...] = ()
    source_order: Literal['earliest', 'latest'] | None = None
    complete_subject: bool = False

    def matches(self, item: Mapping[str, Any]) -> bool:
        # Primary subject fields carry identity. A rationale saying that a
        # different task is unrelated to this ID cannot establish membership.
        primary = '\n'.join(str(item.get(key) or '') for key in ('text', 'summary', 'goal', 'outcome'))
        if not self.natural:
            if self.exact['identifiers'] or self.exact['versions']:
                return bool(prompt_match_score(primary + '\n' + str(item.get('scope') or {}), self.exact))
            return True
        scope = item.get('scope') or {}
        scope_env = str(scope.get('environment') or '') if isinstance(scope, Mapping) else ''
        if any(value in scope_env.casefold().split() for value in self.excluded_environments):
            return False
        if self.task_ids and not any(prompt_match_score(primary, {
            'identifiers': [task], 'versions': [], 'terms': [], 'minimum': 0}) for task in self.task_ids):
            return False
        all_text = primary + '\n' + str(item.get('scope') or {})
        if any(prompt_match_score(all_text, {'identifiers': [], 'versions': [value], 'terms': [], 'minimum': 0})
               for value in self.excluded_versions):
            return False
        if self.identifiers and not prompt_match_score(all_text, {
                'identifiers': self.identifiers, 'versions': [], 'terms': [], 'minimum': 0}):
            return False
        if self.versions and not any(prompt_match_score(all_text, {
            'identifiers': [], 'versions': [version], 'terms': [], 'minimum': 0}) for version in self.versions):
            return False
        return True


def plan_query(query: str, project: str | Path, *,
               selected_project_scopes: Sequence[str | Path] | None = None) -> QueryPlan:
    query, scope_aliases = scope_content_query(query, project, selected_project_scopes)
    requirement_subject = _requirement_state_subject(query)
    exact = exact_constraints(query, project)
    explicit = {match.group()[1:-1].casefold() for match in
                re.finditer(r'`[^`\s]+`|"[^"\s]+"|\'[^\'\s]+\'', query)}
    selected_names = {Path(value).name.casefold() for value in (project, *(selected_project_scopes or ()))}
    artifact_names = {match.group(1).casefold() for match in re.finditer(
        r'\b(?:function|method|symbol|field|table|column|module|variable|identifier|artifact|file)\s+'
        r'(?:(?:named|called)\s+)?([A-Za-z_][A-Za-z0-9_.:-]*)', query, re.I)
        if match.group(1).casefold() in selected_names or re.search(r'[_./:-]|\d|[a-z][A-Z]', match.group(1))}
    # Explicit content positions remain literal even when a name happens to
    # equal a selected project's ordinary alphabetic or hyphenated basename.
    exact = dict(exact, identifiers=list(dict.fromkeys([*exact['identifiers'], *sorted(explicit | artifact_names)])))
    natural = bool(_NATURAL.search(query.strip()))
    continuation = bool(_CONTINUE.search(query))
    compare = bool(_COMPARE.search(query))
    past_original = re.search(r'\b(?:what|which)\s+(?:was|were)\b[^?;.!]{0,120}\boriginal\b', query, re.I)
    historical = bool((_HISTORY.search(query) or historical_query(query) or past_original) and not compare)
    tasks = list(dict.fromkeys(value.casefold() for value in _TASK.findall(query)))
    all_tasks = set(tasks)
    negative = _negative_spans(query) if natural else ()
    if natural:
        excluded = {match.group().casefold() for match in _TASK.finditer(query) if _inside(match.start(), negative)}
        tasks = [task for task in tasks if task not in excluded]
        if all_tasks and not tasks:
            raise ValueError('no positive task subject')
    identifiers = []
    for value in exact['identifiers']:
        if natural and value in all_tasks:
            continue
        if natural and any(match.group().casefold().strip('`') == value and _inside(match.start(), negative)
                for match in re.finditer(r'`[^`]+`|[\w./:-]+', query)):
            continue
        if not natural or value in explicit or value in artifact_names or re.search(r'[_./:]|\d', value) or any(
                token.casefold() == value and re.search(r'[a-z][A-Z]', token)
                for token in re.findall(r'\w+', query)) or (
                '-' in value and re.search(r'\b(?:for|in|from|to|on|about|regarding|within|of|project|projects|compare|versus|vs|between|with|and)\s+'
                                          + re.escape(value) + r'(?![\w.-])', query, re.I)):
            identifiers.append(value)
    # A literal task lookup still requires every identifier together. Natural
    # multi-task questions are a union of scoped windows, never an empty query.
    excluded_versions = {match.group().casefold().lstrip('v') for match in re.finditer(r'v?\d+(?:\.\d+)+', query, re.I) if _inside(match.start(), negative)}
    versions = tuple(value for value in exact['versions'] if not natural or value.casefold().lstrip('v') not in excluded_versions)
    environments = tuple(dict.fromkeys(_environment_name(match.group()) for match in _ENVS.finditer(query)
        if not _inside(match.start(), negative)))
    excluded_envs = tuple(dict.fromkeys(_environment_name(match.group()) for match in _ENVS.finditer(query)
        if _inside(match.start(), negative)))
    generic_frame, generic_relation = _generic_facet(query)
    if len(scope_aliases) > 1 and re.fullmatch(r'compare\s*(?:with|and|versus|vs)?[?.!]*', query, re.I):
        generic_frame = True
    execution = _execution_question(query) if natural else None
    completion_subject = _completion_subject(query) if natural else None
    assessment_subject = execution[0] if execution is not None else completion_subject if completion_subject is not None else _assessment_subject(query) if natural else None
    reason_subject = _reason_subject(query) if natural else None
    answer_subject = requirement_subject if requirement_subject is not None else assessment_subject if assessment_subject is not None else reason_subject
    answer_subject = _soft_subject(answer_subject, [*tasks, *identifiers], [*versions, *excluded_versions], [*environments, *excluded_envs])
    answer_facets = ('requirements', 'current_revision') if requirement_subject is not None else ('execution_assessment',) if assessment_subject is not None else ('reason',) if reason_subject is not None else ()
    if requirement_subject is not None and re.search(r'\band\s+why[?.!]*$', query, re.I):
        answer_facets += ('reason',)
    source_order = execution[1] if execution is not None else None
    assesses_current_evidence = assessment_subject is not None
    if assesses_current_evidence:
        historical = False
        generic_frame = generic_frame or not assessment_subject
    generic_project_state = bool(natural and not (tasks or identifiers or versions) and generic_frame)
    if generic_project_state and generic_relation:
        continuation = True
        historical = False
    projection = 'continuity' if natural and continuation and re.search(r'\b(?:chats?|sessions?|link|statements?|same task)\b|чат|сесси|связ', query, re.I) else 'state'
    if natural and re.search(r'\b(?:why|rationale|reconsider(?:ed)?)\b|почему|причин|пересмотр', query, re.I):
        projection = 'rationale'
    if natural and compare and (historical_query(query) or _HISTORY.search(query)):
        projection = 'history'
    elif natural and (environments or excluded_envs):
        projection = 'environments'
    if generic_project_state and generic_relation:
        projection = 'continuity'
    if not natural:
        candidates = (query,)
        identifiers = list(exact['identifiers'])
        tasks = []
        topic = False
    elif tasks or identifiers or versions:
        windows = []
        for task in tasks or ('',):
            for version in versions or ('',):
                windows.append(' '.join([*('`' + value + '`' for value in [task, *identifiers] if value), version]).strip())
        if len(windows) > 8 or not windows or any(not value for value in windows):
            raise ValueError('unbounded subject windows')
        candidates = tuple(windows)
        topic = False
    elif generic_project_state:
        candidates = ('',)
        topic = False
    else:
        subject_query = answer_subject if answer_subject is not None else query
        topical = resume_topic_plan(subject_query) or prompt_query_plan('How ' + subject_query)
        terms = topical['terms'] if topical else []
        if answer_subject and not terms:
            terms = [(word, word) for word in re.findall(r'[^\W_]{2,}', answer_subject)]
            if not terms:
                raise ValueError('unrepresentable topical subject')
        candidates = (' '.join(stem for _, stem in terms),)
        topic = bool(terms)
    return QueryPlan(natural, historical, continuation, projection, tuple(tasks), tuple(identifiers),
                     versions, tuple(excluded_versions), environments, excluded_envs, candidates, topic,
                     generic_project_state, assesses_current_evidence, query, scope_aliases, requirement_subject, exact,
                     answer_subject=answer_subject if natural else None,
                     answer_facets=answer_facets if natural else (), source_order=source_order,
                     complete_subject=completion_subject is not None)
