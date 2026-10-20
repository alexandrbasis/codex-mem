"""Local read planning: separate exact subjects from answer instructions.

This planner does not judge evidence or infer task ownership. Literal lookups
keep the existing exact constraints; natural questions use bounded subject
windows and rank answer facets after those windows are scoped.
"""
from __future__ import annotations

from dataclasses import dataclass
import re
from pathlib import Path
from typing import Any, Mapping

from .jev_retrieval import exact_constraints
from .retrieval import historical_query, prompt_match_score, prompt_query_plan, resume_topic_plan

_TASK = re.compile(r'(?<![\w-])[A-Za-z][A-Za-z0-9]{1,15}-[0-9]{1,10}(?![\w-])')
_NATURAL = re.compile(r'\?|^(?:resume|continue|summari[sz]e|compare|explain|show|which|what|how|why|when|where|tell|list|describe|какой|какие|что|как|почему|продолж|сравни|покажи|расскажи)', re.I)
_COMPARE = re.compile(r'\b(?:compare|comparison|versus|vs|changed|evolved|difference|then)\b|сравн|изменил|отлич', re.I)
_CONTINUE = re.compile(r'\b(?:resume|continue|continuing|continuation)\b|pick\s+up|продолж[а-яё]*', re.I)
_HISTORY = re.compile(r'\b(?:originally|original|as[- ]of|historical|history)\b|первоначаль|истори|по состоянию на', re.I)
_ENVS = re.compile(r'(?<![\w.-])(?:production|prod|staging|development|dev|local(?:ly)?|локальн[а-яё]*|стейджинг[а-яё]*|продакш(?:е|ё)?н[а-яё]*)(?![\w-]|\.[\w])', re.I)

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
    exact: Mapping[str, Any]

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


def plan_query(query: str, project: str | Path) -> QueryPlan:
    exact = exact_constraints(query, project)
    natural = bool(_NATURAL.search(query.strip()))
    continuation = bool(_CONTINUE.search(query))
    compare = bool(_COMPARE.search(query))
    historical = bool((_HISTORY.search(query) or historical_query(query)) and not compare)
    tasks = list(dict.fromkeys(value.casefold() for value in _TASK.findall(query)))
    all_tasks = set(tasks)
    negative = _negative_spans(query) if natural else ()
    if natural:
        excluded = {match.group().casefold() for match in _TASK.finditer(query) if _inside(match.start(), negative)}
        tasks = [task for task in tasks if task not in excluded]
        if all_tasks and not tasks:
            raise ValueError('no positive task subject')
    explicit = {value.casefold() for value in re.findall(r'`([^`\s]+)`', query)}
    identifiers = []
    for value in exact['identifiers']:
        if natural and value in all_tasks:
            continue
        if natural and any(match.group().casefold().strip('`') == value and _inside(match.start(), negative)
                for match in re.finditer(r'`[^`]+`|[\w./:-]+', query)):
            continue
        if not natural or value in explicit or re.search(r'[_./:]|\d', value) or any(
                token.casefold() == value and re.search(r'[a-z][A-Z]', token)
                for token in re.findall(r'\w+', query)):
            identifiers.append(value)
    # A literal task lookup still requires every identifier together. Natural
    # multi-task questions are a union of scoped windows, never an empty query.
    excluded_versions = {match.group().casefold().lstrip('v') for match in re.finditer(r'v?\d+(?:\.\d+)+', query, re.I) if _inside(match.start(), negative)}
    versions = tuple(value for value in exact['versions'] if not natural or value.casefold().lstrip('v') not in excluded_versions)
    environments = tuple(dict.fromkeys(_environment_name(match.group()) for match in _ENVS.finditer(query)
        if not _inside(match.start(), negative)))
    excluded_envs = tuple(dict.fromkeys(_environment_name(match.group()) for match in _ENVS.finditer(query)
        if _inside(match.start(), negative)))
    projection = 'continuity' if natural and continuation and re.search(r'\b(?:chats?|sessions?|link|statements?|same task)\b|чат|сесси|связ', query, re.I) else 'state'
    if natural and re.search(r'\b(?:why|rationale|reconsider(?:ed)?)\b|почему|причин|пересмотр', query, re.I):
        projection = 'rationale'
    if natural and compare and (historical_query(query) or _HISTORY.search(query)):
        projection = 'history'
    elif natural and (environments or excluded_envs):
        projection = 'environments'
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
    else:
        topical = resume_topic_plan(query) or prompt_query_plan('How ' + query)
        terms = topical['terms'] if topical else []
        candidates = (' '.join(stem for _, stem in terms),)
        topic = bool(terms)
    return QueryPlan(natural, historical, continuation, projection, tuple(tasks), tuple(identifiers),
                     versions, tuple(excluded_versions), environments, excluded_envs, candidates, topic, exact)
