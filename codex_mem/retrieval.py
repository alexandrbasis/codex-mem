"""Small retrieval previews for CLI/MCP; full records stay available by ID."""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import re
import unicodedata
from typing import Any
from xml.sax.saxutils import escape


# Only prompt scaffolding belongs here. Domain words (including "memory",
# "release", "queue", and "error") must continue to constrain retrieval.
_PROMPT_WORDS = frozenset((
    "a an the and or to of for from in on at with about by as is are was were be been "
    "do does did can could would will should how what why when where which who whom "
    "i me my we us our you your it its this that these those please tell explain show "
    "describe investigate analyze analyse check review work works working worked "
    "happen happened happening handle handles doing now there then still "
    "как что почему когда где какой какая какие которое который которая ли и или а но "
    "в во на к ко по о об обо от из у с со за для до без это этот эта эти то так там "
    "здесь он она оно они его ее её их мы нас наш наша наши наше вы ваш я мне мой моя "
    "ты твой про пусть уже еще ещё же бы был была было были есть будет будут "
    "расскажи покажи объясни поясни проверь посмотри проанализируй проверить "
    "работает работают работаешь работал работать справляется справляются своей "
    "своим свою своими задачей пожалуйста сейчас теперь"
).split())
_PROMPT_TOKEN = re.compile(r"[vV]?\d+(?:\.\d+)+|[^\W_]+(?:[_.:/-][^\W_]+)*", re.UNICODE)
_PROMPT_VERSION = re.compile(r"[vV]?\d+(?:\.\d+)+\Z")
_RUSSIAN_WORD = re.compile(r"[а-яё]+\Z", re.IGNORECASE)
_RUSSIAN_ENDING = re.compile(r"(?:иями|ями|ами|ого|ему|ому|ий|ый|ой|ая|яя|ое|ее|ых|их|ов|ев|ам|ям|ах|ях|ом|ем|ы|и|а|я|у|ю|е|ь)\Z")


def prompt_query_plan(query: str) -> dict[str, Any] | None:
    """Bounded lexical fallback for questions, without loading an embedder.

    An ordinary exact query is never relaxed. Prompt words must actually be
    present; versions and code-shaped identifiers are always mandatory. This
    is lexical recall, not semantic or cross-language understanding.
    """
    raw = _PROMPT_TOKEN.findall(unicodedata.normalize("NFKC", query))
    words = [token.casefold() for token in raw]
    if not any(token in _PROMPT_WORDS for token in words):
        return None
    terms: list[str] = []
    identifiers: list[str] = []
    versions: list[str] = []
    for original, token in zip(raw, words):
        if token in _PROMPT_WORDS:
            continue
        if _PROMPT_VERSION.fullmatch(token):
            versions.append(token)
        elif re.search(r"[_./:-]|\d", token) or re.search(r"[a-z][A-Z]", original):
            identifiers.append(token)
        else:
            terms.append(token)
    terms = list(dict.fromkeys(terms))[:16]
    identifiers = list(dict.fromkeys(identifiers))
    versions = list(dict.fromkeys(versions))
    if len(identifiers) > 16 or len(versions) > 16:
        return None  # Never discard a mandatory exact constraint to fit a cap.
    if not (terms or identifiers or versions):
        return None
    stems = []
    for term in terms:
        stem = _RUSSIAN_ENDING.sub("", term) if _RUSSIAN_WORD.fullmatch(term) else term
        stems.append(stem if len(stem) >= 4 else term)
    return {"terms": list(zip(terms, stems)), "identifiers": identifiers, "versions": versions,
            "minimum": min(len(terms), max(1, math.ceil(len(terms) / 2)))}


def prompt_match_score(text: str, plan: Mapping[str, Any]) -> int:
    """Score topical coverage only after all exact constraints have matched."""
    normalized = unicodedata.normalize("NFKC", text).casefold()
    for identifier in plan["identifiers"]:
        if not re.search(r"(?<![\w.-])" + re.escape(identifier) + r"(?![\w.-])", normalized):
            return 0
    matched = sum(bool(re.search(r"(?<!\w)" + re.escape(stem) +
                                (r"[а-яё]*\b" if term != stem else r"(?!\w)"), normalized))
                  for term, stem in plan["terms"])
    if matched < plan["minimum"]:
        return 0
    return matched + len(plan["identifiers"]) + len(plan["versions"])


_TOPIC_NOISE = _PROMPT_WORDS | frozenset((
    "completed complete completion checked verified verification unverified verify pending "
    "blocked unfinished unresolved remaining proposal proposed suggestion suggested focused "
    "plan planned fix fixed result outcome later earlier new old current successful success "
    "failed fails failure passed passing tests test implemented implement implementation "
    "reported report found needs need changes change improvements improved improve "
    "project plugin memory codex mem repository code local "
    "готово выполнено выполнен проверено проверен проверены проверить исправлено исправлен "
    "нужно осталось результат предложено предложение"
).split())


TopicSignature = tuple[set[str], set[str], set[str], set[str], set[str]]


def historical_query(query: str) -> bool:
    """Explicit history requests must retain intermediate query matches.

    A request about work remaining after a change is still a resume request.
    Dates and version identifiers keep their existing exact-search behavior.
    """
    return bool(re.search(
        r"\b(?:history|historical|previously|formerly|originally|earlier|"
        r"истори[а-яё]*|раньше|ранее|прежде|первоначально)\b|\bused\s+to\b|"
        r"\bwhat\s+(?:did|was|were)\b[^?!.]{0,160}\bbefore\b|"
        r"\bчто\s+(?:было|были|решили|решено)\b[^?!.]{0,160}\bдо\b",
        unicodedata.normalize("NFKC", query).casefold(),
    ))


_PROJECT_STATE_WORDS = _PROMPT_WORDS | frozenset((
    "plugin plugins memory system project repository repo app application software "
    "codex mem progress status state condition result results improvement improvements "
    "improve improved change changes work task tasks done remaining remains unfinished "
    "unresolved pending outstanding open recent recently last latest current after "
    "before next left new up doing needs need fix fixes anything something "
    "release releases released rollout shipped shipping delivered delivery publish published "
    "verification verified unverified confirm confirmed included includes evidence well "
    "каково каковы над после перед осталось остались остается остаётся оставалось "
    "нужно нужны сделать сделано дальше далее чем вошло входит выкатили выкачен "
    "выпустили выпущено подтверждено подтверждён подтвержден подтвердили прошёл прошел"
).split())
_PROJECT_STATE_RUSSIAN = re.compile(
    r"(?:плагин|памят|систем|проект|репозитор|приложен|программ|работ|задач|"
    r"состоян|статус|результат|улучш|изменен|изменён|последн|текущ|незаверш|"
    r"незаконч|незакрыт|непровер|провер|открыт|нужн|нов|релиз|выпуск|подтвержд)[а-яё]*\Z"
)
_PROJECT_STATE_INTENT = re.compile(
    r"(?:current|latest|last|recent|recently|status|state|remaining|remains|left|unfinished|"
    r"unresolved|pending|outstanding|next|done|now|сейчас|теперь|остал[а-яё]*|оста[её]т[а-яё]*|"
    r"текущ[а-яё]*|последн[а-яё]*|состоян[а-яё]*|статус[а-яё]*|незаверш[а-яё]*|"
    r"незаконч[а-яё]*|незакрыт[а-яё]*|нужн[а-яё]*|дальше|далее)\Z"
)


def current_state_scope(query: str, project: str | Path | None = None) -> str | None:
    """Recognize bounded project/release questions, preserving topical limits.

    Generic project wording resolves to the already-scoped project; release
    wording resolves to a bilingual release topic. Specific subjects, versions,
    paths, issue IDs and code identifiers opt out and retain their constraints.
    A broad request must also express a current/open state or project assessment.
    """
    if historical_query(query):
        return None
    raw = _PROMPT_TOKEN.findall(unicodedata.normalize("NFKC", query))
    project_name = Path(project).name.casefold() if project is not None else None
    words: list[str] = []
    for original in raw:
        word = original.casefold()
        # A named current project is scope, not a code/path/version constraint.
        # Generic hyphenated project names such as codex-mem also retain the
        # same meaning as their already-supported spaced form.
        if not re.search(r"[_./:]|\d", word) and word == project_name:
            words.append("project")
        elif "-" in word and all(part in _PROJECT_STATE_WORDS for part in word.split("-")):
            words.extend(word.split("-"))
        elif re.search(r"[_./:-]|\d", word) or re.search(r"[a-z][A-Z]", original):
            return None
        else:
            words.append(word)
    if not words or not all(word in _PROJECT_STATE_WORDS or _PROJECT_STATE_RUSSIAN.fullmatch(word)
                            for word in words):
        return None
    explicit_state = any(_PROJECT_STATE_INTENT.fullmatch(word) for word in words)
    project_anchor = any(word in {"project", "plugin", "memory", "codex", "repository"}
                         or re.fullmatch(r"(?:плагин|проект|памят)[а-яё]*", word) for word in words)
    assessment = project_anchor and (
        any(word.startswith("справля") for word in words)
        or (any(word in {"how", "как"} for word in words)
            and any(word in {"doing", "работает", "работают", "проанализируй", "analyze", "review"}
                    for word in words))
    )
    if not (explicit_state or assessment):
        return None
    return "release" if any(re.fullmatch(r"releases?|released|rollout|релиз[а-яё]*|выпуск[а-яё]*", word)
                            for word in words) else "project"


def broad_current_state_query(query: str, project: str | Path | None = None) -> bool:
    return current_state_scope(query, project) is not None


def state_scope_plan(scope: str) -> dict[str, Any] | None:
    """Release paraphrases share one bounded bilingual topic constraint."""
    if scope != "release":
        return None
    return {"terms": [("release", "release"), ("released", "released"), ("rollout", "rollout"),
                      ("релиза", "релиз"), ("выпуска", "выпуск")],
            "identifiers": [], "versions": [], "minimum": 1}


def state_scope_matches(record: Mapping[str, Any], scope: str) -> bool:
    plan = state_scope_plan(scope)
    if plan is None:
        return True
    if not any(record.get(key) for key in ("title", "body", "preview", "session_summary")):
        return True  # ID-only adapters have already applied their query match.
    text = " ".join(str(record.get(key) or "") for key in
                    ("title", "body", "preview", "session_summary", "observation"))
    return bool(prompt_match_score(text, plan))


def resume_duplicate_key(record: Mapping[str, Any]) -> tuple[str, ...] | None:
    """Collapse only complete repeated descriptions from the same session.

    Shared topics or files alone do not establish redundancy. Keep different
    qualifications, source roles and truncated previews as separate evidence.
    This changes the bounded view, never durable records or supersession.
    """
    observation = record.get("observation")
    session = record.get("session_id")
    source = str(record.get("source") or "")
    if (not session or not source.startswith("processor:")
            or record.get("session_summary") or not isinstance(observation, Mapping)
            or observation.get("type") not in {"discovery", "security_note"}):
        return None
    body = record.get("body")
    if not isinstance(body, str):
        body = record.get("preview")
        if not isinstance(body, str) or body.rstrip().endswith(("…", "...")):
            return None
    if not body.strip():
        return None
    return (str(session), source, str(record.get("kind") or ""), " ".join(body.split()),
            json.dumps(dict(observation), sort_keys=True, ensure_ascii=False),
            json.dumps(record.get("provenance") or {}, sort_keys=True, ensure_ascii=False))


def topic_signature(record: Mapping[str, Any]) -> TopicSignature:
    """Extract once per bounded candidate, rather than reparsing each pair."""
    summary = record.get("session_summary") or {}
    observation = record.get("observation") or {}
    heading = str(record.get("title") or "") + " " + str(summary.get("request") or "")
    body = heading + " " + str(record.get("body") or record.get("preview") or "")
    body += " " + " ".join(str(summary.get(key) or "") for key in
                           ("investigated", "learned", "completed", "next_steps"))
    body += " " + str(observation.get("subtitle") or "")
    body += " " + " ".join(str(value) for value in observation.get("facts") or [])
    def tokens(text: str) -> set[str]:
        return {word for word in _PROMPT_TOKEN.findall(unicodedata.normalize("NFKC", text).casefold())
                if len(word) >= 3 and word not in _TOPIC_NOISE and not word.isdigit()
                and not _PROMPT_VERSION.fullmatch(word)}
    paths = set(observation.get("files_modified") or []) | set(observation.get("files_read") or [])
    concepts = {value for value in observation.get("concepts") or []
                if value not in {"how-it-works", "what-changed", "problem-solution", "gotcha", "pattern"}}
    versions = {token.lstrip("v") for token in _PROMPT_TOKEN.findall(body.casefold())
                if _PROMPT_VERSION.fullmatch(token)}
    identifiers = {token for token in _PROMPT_TOKEN.findall(heading.casefold())
                   if not _PROMPT_VERSION.fullmatch(token) and '/' not in token
                   and ('_' in token or (re.search(r"[a-z]", token) and re.search(r"\d", token)))}
    return tokens(heading), tokens(body), paths | concepts, versions, identifiers


def topic_followup_basis(earlier: TopicSignature, later: TopicSignature) -> str | None:
    """Conservative topic continuity; this function never determines truth.

    Two distinctive title/request terms or three content terms plus a shared
    file/concept are required. A mere common lifecycle word or common file
    does not identify the same task. Explicit version changes remain history.
    """
    old_heading, old_terms, old_scope, old_versions, old_ids = earlier
    new_heading, new_terms, new_scope, new_versions, new_ids = later
    if old_versions and new_versions and old_versions != new_versions:
        return None
    old_issues = {value for value in old_ids if re.fullmatch(r"[a-z][a-z0-9]*-\d+", value)}
    new_issues = {value for value in new_ids if re.fullmatch(r"[a-z][a-z0-9]*-\d+", value)}
    if old_issues and new_issues and old_issues != new_issues:
        return None
    if old_ids and new_ids and not old_ids.intersection(new_ids):
        return None
    shared = old_heading & new_heading
    if len(shared) >= 2 and len(shared) / max(1, min(len(old_heading), len(new_heading))) >= 0.6:
        return "shared_topic_terms"
    shared = old_terms & new_terms
    if old_scope & new_scope and len(shared) >= 3 and len(shared) / max(1, min(len(old_terms), len(new_terms))) >= 0.4:
        return "shared_scope_and_topic_terms"
    return None


_STATUS_TITLE = re.compile(
    r"\b(?:pending|unverified|unconfirmed|requested|proposal|plan|"
    r"not\s+(?:yet\s+)?(?:verified|confirmed|complete)|"
    r"план[а-яё]*|неподтвержд[а-яё]*|непровер[а-яё]*|запрош[а-яё]*)\b", re.IGNORECASE,
)


def _declared_event_instant(record: Mapping[str, Any]) -> datetime | None:
    if record.get("event_time_basis") not in {"source_event", "recorded_at"}:
        return None
    try:
        value = datetime.fromisoformat(str(record.get("event_at") or "").replace("Z", "+00:00"))
    except ValueError:
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def prefer_topic_followups(records: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Show matching followups first, retaining every earlier requirement.

    Shared topic and later evidence do not establish that open work was done.
    In particular, a diagnostic or documentation read can be a followup while
    leaving the earlier requirement unchanged. No inferred link deletes a row.
    """
    by_id = {record["id"]: record for record in records}
    annotated = []
    for record in records:
        item = dict(record)
        later = by_id.get(record.get("later_summary_id"))
        earlier_at = _declared_event_instant(record)
        later_at = _declared_event_instant(later) if later else None
        if (later and record.get("kind") == "note" and not record.get("session_summary")
                and (later.get("kind") == "session_summary" or later.get("session_summary"))
                and record.get("session_id") and record.get("session_id") == later.get("session_id")
                and earlier_at is not None and later_at is not None and earlier_at < later_at
                and _STATUS_TITLE.search(str(record.get("title") or ""))):
            # The pointer establishes chronology, not resolution. Even an
            # uncited or still-open finding stays in this selected window.
            item["earlier_status"] = True
        annotated.append(item)
    linked = lambda record: (2 if record.get("earlier_status")
                             else int(record.get("later_context_id") in by_id))
    # Display both records, with later evidence before the earlier plan when
    # both are already selected. Do not use this to rank retrieval candidates.
    return sorted(annotated, key=linked)


def defer_prior_status(records: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Put a source-linked later handoff ahead of its earlier status detail.

    This is a current-state reading order, never a truth/supersession decision.
    Require the same session, explicit source lineage, overlapping subject and
    a reported action outcome. A later inspection or unrelated summary cannot
    consume an open requirement's slot. Deferred records remain in the tail.
    """
    by_id = {record["id"]: record for record in records}
    outcome = re.compile(r"\b(?:committed|pushed|shipped|deployed|implemented|completed|passed|"
                         r"реализован[а-яё]*|выполнен[а-яё]*|опубликован[а-яё]*|пройден[а-яё]*)\b",
                         re.IGNORECASE)
    negation = re.compile(r"\b(?:not|never|no|не|нет)\b[^.;\n]{0,100}\b(?:"
                          + outcome.pattern.removeprefix(r"\b(?:").removesuffix(r")\b")
                          + r")\b", re.IGNORECASE)
    current, prior = [], []
    for record in records:
        later = by_id.get(record.get("later_summary_id"))
        summary = later.get("session_summary") if later else None
        subject = topic_signature(record)[0]
        completed = str(summary.get("completed") or "") if isinstance(summary, Mapping) else ""
        next_steps = str(summary.get("next_steps") or "") if isinstance(summary, Mapping) else ""
        later_subject = topic_signature({"body": completed})[1]
        remaining_subject = topic_signature({"body": next_steps})[1]
        earlier_at = _declared_event_instant(record)
        later_at = _declared_event_instant(later) if later else None
        deferred = bool(
            later and isinstance(summary, Mapping) and record.get("session_id")
            and record.get("session_id") == later.get("session_id")
            and record["id"] in (later.get("source_ids") or [])
            and earlier_at is not None and later_at is not None and earlier_at < later_at
            and _STATUS_TITLE.search(str(record.get("title") or ""))
            and outcome.search(completed) and not negation.search(completed)
            and not _STATUS_TITLE.search(completed)
            and len(subject & later_subject) >= 2
            and len(subject & remaining_subject) < 2
        )
        item = dict(record)
        if deferred:
            item["context_before_handoff"] = True
            prior.append(item)
        else:
            current.append(item)
    return current + prior


def freshness_markup(snapshot: Mapping[str, Any], *, budget: int) -> str:
    """Render telemetry before records, preserving XML and explicit omissions."""
    def value(key: str) -> str:
        item = snapshot.get(key)
        if item is None:
            return "unknown"
        if isinstance(item, bool):
            return str(item).lower()
        return str(item)

    full = "<freshness>" + escape(value("summary")) + "</freshness>\n"
    if len(full) <= budget:
        return full
    compact = (
        "<freshness>" + escape(
            f'{value("status")}; knowledge_incomplete={value("knowledge_incomplete")}; '
            f'pending={value("pending_capture_count")}; metrics omitted'
        ) + "</freshness>\n"
    )
    if len(compact) <= budget:
        return compact
    minimal = "<freshness>" + escape(value("status")) + "; metrics omitted</freshness>\n"
    return minimal if len(minimal) <= budget else ""


_PREVIEW_FIELDS = (
    "id", "project", "title", "kind", "created_at", "session_id", "preview",
    "source", "provenance", "superseded_by", "superseded_at", "is_anchor",
    "score", "lexical_score", "semantic_score", "rrf_score",
    "event_at", "event_id", "event_time_basis", "context_historical", "context_before_handoff", "earlier_status", "later_summary_id",
    "later_context_id", "later_context_relation", "later_context_basis",
)


def preview_records(
    records: Sequence[Mapping[str, Any]], *, detail: str = "compact"
) -> list[dict[str, Any]]:
    """Project after ranking so omitted metadata cannot change retrieval.

    Compact previews omit full narratives, facts, summary fields and their
    duplicate metadata view. They keep provenance and supersession visible.
    ``full`` preserves the previous preview schema; ``get`` reads full bodies.
    This never rewrites persisted records or the Store/processor contract.
    """
    if not isinstance(detail, str) or detail not in {"compact", "full"}:
        raise ValueError("detail must be compact or full")
    if detail == "full":
        return [dict(record) for record in records]
    result = []
    for record in records:
        preview = {key: record[key] for key in _PREVIEW_FIELDS if key in record}
        observation = record.get("observation")
        if isinstance(observation, Mapping) and observation.get("type"):
            preview["observation"] = {"type": observation["type"]}
        preview["source_count"] = len(record.get("source_ids") or [])
        preview["detail"] = "compact"
        preview["details_available"] = True
        result.append(preview)
    return result
