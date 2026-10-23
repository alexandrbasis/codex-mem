"""Small retrieval previews for CLI/MCP; full records stay available by ID."""
from __future__ import annotations

from .host import is_capture_source

from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from functools import lru_cache
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
    "decide decided choose chose chosen select selected "
    "happen happened happening handle handles doing now there then still "
    "как что почему когда где какой какая какие которое который которая ли и или а но "
    "в во на к ко по о об обо от из из-за из-под у с со за для до без это этот эта эти то так там "
    "здесь он она оно они его ее её их мы нас наш наша наши наше вы ваш я мне мой моя "
    "ты твой про пусть уже еще ещё же бы был была было были есть будет будут "
    "расскажи покажи объясни поясни проверь посмотри проанализируй проверить "
    "работает работают работаешь работал работать справляется справляются своей "
    "решили решено выбрали выбран приняли принято "
    "своим свою своими задачей пожалуйста сейчас теперь нужно можно"
).split())
_PROMPT_TOKEN = re.compile(r"[vV]?\d+(?:\.\d+)+|[^\W_]+(?:[_.:/-][^\W_]+)*", re.UNICODE)
_PROMPT_VERSION = re.compile(r"[vV]?\d+(?:\.\d+)+\Z")
_RUSSIAN_WORD = re.compile(r"[а-яё]+\Z", re.IGNORECASE)
_RUSSIAN_ENDING = re.compile(r"(?:иями|ями|ами|ого|ему|ому|ий|ый|ой|ая|яя|ое|ее|ых|их|ов|ев|ам|ям|ах|ях|ом|ем|ы|и|а|я|у|ю|е|ь)\Z")
# Narrow bilingual aliases for literal software terms that commonly occur in
# English source records and inflected Russian questions. Each term still
# contributes at most one match to the topical coverage requirement.
_SOFTWARE_ALIASES = {
    "хук": ("hook", "hooks"), "hook": ("хук",), "hooks": ("хук",),
    "таймаут": ("timeout", "timeouts"), "timeout": ("таймаут",),
    "timeouts": ("таймаут",),
    "disabled": ("disable", "disabling", "отключен", "отключён", "отключ"),
}

# Literal, bilingual software vocabulary. These aliases improve recall without
# calling a model, inventing a translation or replacing mandatory identifiers.
_TOPICAL_EQUIVALENTS = (
    (r"(?:тест[а-яё]*|tests?)", ("test", "tests", "тест")),
    (r"(?:сервер[а-яё]*|servers?)", ("server", "servers", "сервер")),
    (r"(?:очеред[а-яё]*|queues?)", ("queue", "queues", "очеред")),
    (r"(?:завис[а-яё]*|stalls?|stalled|hangs?|hung)", ("stall", "stalled", "hung", "blocked", "завис")),
    (r"(?:исправ[а-яё]*|fix(?:ed|es)?)", ("fix", "fixed", "fixes", "исправ")),
    (r"(?:событ[а-яё]*|events?)", ("event", "events", "событ")),
    (r"(?:ход[а-яё]*|turns?)", ("turn", "turns", "ход")),
    (r"(?:задани[а-яё]*|jobs?)", ("job", "jobs", "задани")),
    (r"(?:объедин[а-яё]*|группир[а-яё]*|накоп[а-яё]*|batch(?:ing|es)?|coalesc[а-яa-z]*)",
     ("batch", "batching", "group", "coalescing", "объедин", "группир", "накоп")),
    (r"(?:обработ[а-яё]*|process(?:ing|ed|es)?)", ("processing", "processed", "process", "обработ")),
    (r"(?:эконом[а-яё]*|econom(?:y|ic|ics))", ("economics", "economy", "economic", "эконом")),
    (r"(?:вознагражд[а-яё]*|rewards?)", ("reward", "rewards", "вознагражд")),
    (r"(?:начисл[а-яё]*|credit(?:ing|ed)?|payouts?)", ("credit", "crediting", "payout", "payouts", "начисл")),
    (r"(?:мгновенн[а-яё]*|instant(?:ly)?)", ("instant", "instantly", "мгновенн")),
    (r"(?:финансир[а-яё]*|fund(?:ing|ed)?)", ("funding", "funded", "финансир")),
    (r"(?:уведомлен[а-яё]*|notifications?)", ("notification", "notifications", "уведомлен")),
    (r"(?:регистрац[а-яё]*|registrations?)", ("registration", "registrations", "регистрац")),
    (r"(?:расписан[а-яё]*|schedules?|scheduled)", ("schedule", "scheduled", "расписан")),
    (r"(?:географ[а-яё]*|geograph[а-яa-z]*)", ("geography", "geographic", "географ")),
    (r"sla", ("sla",)),
    (r"(?:доступ[а-яё]*|credentials?|access)", ("credentials", "credential", "access", "доступ")),
    (r"(?:аккаунт[а-яё]*|accounts?)", ("account", "accounts", "аккаунт")),
    (r"(?:сет(?:ь|и|ей|ям|ях|ями|ев[а-яё]+)|networks?)", ("network", "networks", "сет")),
    (r"(?:реализац[а-яё]*|implement(?:ation|ed|ing)?)",
     ("implementation", "implemented", "implement", "реализац")),
    (r"(?:согласова[а-яё]*|agre(?:e|ed|ement)|decisions?)",
     ("agreed", "agreement", "decision", "decisions", "unconfirmed", "unresolved", "undecided", "согласова")),
    (r"(?:подключ[а-яё]*|connect(?:ion|ed|ing)?|permissions?|allowed)",
     ("connect", "connected", "connection", "allowed", "permission", "permissions", "integrated", "подключ")),
    (r"(?:владел[а-яё]*|ответствен[а-яё]*|ownership|owners?)",
     ("ownership", "owner", "owners", "владел", "ответствен")),
    (r"(?:сурс[а-яё]*|источник[а-яё]*|sources?)", ("source", "sources", "сурс", "источник")),
    (r"(?:проверк[а-яё]*|верификац[а-яё]*|verif(?:y|ied|ication))",
     ("verification", "verified", "verify", "check", "checks", "checked", "test", "tests", "ci", "проверк", "верификац")),
    (r"(?:ожидани[а-яё]*|waiting|awaiting)", ("waiting", "awaiting", "ожидани")),
)


@lru_cache(maxsize=2048)
def prompt_term_variants(term: str, stem: str) -> tuple[str, ...]:
    topical = [variant for pattern, variants in _TOPICAL_EQUIVALENTS
               if re.fullmatch(pattern, term, re.IGNORECASE) for variant in variants]
    return tuple(dict.fromkeys((stem, *_SOFTWARE_ALIASES.get(stem, ()), *topical)))


def prompt_variant_prefix(term: str, stem: str, variant: str) -> bool:
    return bool(_RUSSIAN_WORD.fullmatch(variant) and len(variant) >= 3
                and (variant != term or stem != term))


def _prompt_tokens(query: str, *, natural_compounds: bool | None = None) -> list[str]:
    normalized = unicodedata.normalize("NFKC", query)
    matches = list(_PROMPT_TOKEN.finditer(normalized))
    if natural_compounds is None:
        natural_compounds = any(match.group().casefold() in _PROMPT_WORDS for match in matches)
    quoted = [match.span() for match in re.finditer(r"`[^`\n]*`|\"[^\"\n]*\"|'[^'\n]*'", normalized)]
    result: list[str] = []
    for match in matches:
        token = match.group()
        # In Russian prose, an acronym joined to an inflected noun is an
        # ordinary phrase. Preserve explicit quotes and all code/path shapes.
        if (natural_compounds and re.fullmatch(r"[A-Za-z][A-Za-z0-9]+-[А-Яа-яЁё]+", token)
                and not any(start <= match.start() < end for start, end in quoted)):
            result.extend(token.split("-", 1))
        else:
            result.append(token)
    return result


def prompt_query_plan(query: str, *, natural_compounds: bool | None = None) -> dict[str, Any] | None:
    """Bounded lexical fallback for questions, without loading an embedder.

    An ordinary exact query is never relaxed. Prompt words must actually be
    present; versions and code-shaped identifiers are always mandatory. This
    is lexical recall, not semantic or cross-language understanding.
    """
    raw = _prompt_tokens(query, natural_compounds=natural_compounds)
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
    terms = list(dict.fromkeys(terms))
    identifiers = list(dict.fromkeys(identifiers))
    versions = list(dict.fromkeys(versions))
    if len(terms) > 16 or len(identifiers) > 16 or len(versions) > 16:
        return None  # Never discard a mandatory exact constraint to fit a cap.
    if not (terms or identifiers or versions):
        return None
    stems = []
    for term in terms:
        stem = _RUSSIAN_ENDING.sub("", term) if _RUSSIAN_WORD.fullmatch(term) else term
        stems.append(stem if len(stem) >= 3 else term)
    return {"terms": list(zip(terms, stems)), "identifiers": identifiers, "versions": versions,
            "minimum": len(terms) if versions else min(len(terms), max(1, math.ceil(len(terms) / 2)))}


def prompt_match_score(text: str, plan: Mapping[str, Any]) -> int:
    """Score topical coverage only after all exact constraints have matched."""
    normalized = unicodedata.normalize("NFKC", text).casefold()
    for version in plan["versions"]:
        number = version.lstrip("v")
        if not re.search(r"(?<![\w.])v?" + re.escape(number) + r"(?!\w|\.[0-9])", normalized):
            return 0
    for identifier in plan["identifiers"]:
        if not re.search(r"(?<![\w.-])" + re.escape(identifier) + r"(?![\w.-])", normalized):
            return 0
    matched = 0
    for term, stem in plan["terms"]:
        if any(re.search(r"(?<!\w)" + re.escape(variant) +
                         (r"[а-яё]*\b" if prompt_variant_prefix(term, stem, variant)
                          else r"(?!\w)"), normalized)
               for variant in prompt_term_variants(term, stem)):
            matched += 1
    if matched < plan["minimum"]:
        return 0
    return matched + len(plan["identifiers"]) + len(plan["versions"])


def resume_subject_plan(query: str, project: str | Path | None = None) -> dict[str, Any] | None:
    """Extract explicit names for bounded resume recall, never inferred topics.

    Names in a question written in another alphabet are especially useful:
    vector proximity alone does not establish evidence about that name. Code
    identifiers and versions retain their existing all-required semantics.
    """
    natural_compounds = prompt_query_plan(query) is not None
    plan = prompt_query_plan("How " + query, natural_compounds=natural_compounds)
    if plan is None:
        return None
    tokens = _prompt_tokens(query, natural_compounds=natural_compounds)
    non_latin = bool(re.search(r"[а-яё]", query, re.IGNORECASE))
    names = []
    for index, token in enumerate(tokens):
        folded = token.casefold()
        if (folded in _PROJECT_STATE_WORDS or folded in {"pr", "sdk", "api"}
                or not re.fullmatch(r"[A-Za-z]+", token)):
            continue
        if (non_latin or (token[0].isupper()
                and (index > 0 or len(tokens) == 1 or token.isupper()
                     or re.search(r"[a-z][A-Z]", token)))):
            names.append(folded)
    project_words = set(re.split(r"[-_]", Path(project).name.casefold())) if project is not None else set()
    # A product name already present in the scoped project's slug need not
    # occur again as a standalone word in each version receipt. Paths, issue
    # IDs, symbols and numeric constraints are never removed by this rule.
    scoped_names = project_words if plan["versions"] else set()
    return {"names": [name for name in dict.fromkeys(names) if name not in scoped_names],
            "identifiers": [word for word in plan["identifiers"]
                            if not (word.isalpha() and word in scoped_names)],
            "versions": plan["versions"]}


def resume_topic_plan(query: str) -> dict[str, Any] | None:
    """Separate continuation wording from a literal task subject.

    Requested answer facets such as passed/open are ranking hints, not words
    that every relevant claim must repeat. Exact anchors remain mandatory.
    """
    if historical_query(query) or not re.search(
        r"\b(?:resume|continue|continuing|continuation|requirements?|obligations?)\b|pick\s+up|продолж[а-яё]*|требован[а-яё]*", query, re.I):
        return None
    plan = prompt_query_plan("How " + query)
    if plan is None:
        return None
    framing = _PROJECT_STATE_WORDS | {"resume", "continue", "continuing", "continuation",
                                     "passed", "passing", "failed", "completed", "complete",
                                     "requirement", "requirements", "obligation", "obligations", "independent", "remain"}
    terms = [(term, stem) for term, stem in plan["terms"]
             if term not in framing and not _PROJECT_STATE_RUSSIAN.fullmatch(term)
             and not re.fullmatch(r"(?:продолж|требован|обязательств|независим|отдельн)[а-яё]*", term)]
    return dict(plan, terms=terms, minimum=len(terms))


def project_lexical_text(text: str) -> str:
    """Small English inflection variants for task-topic recall, never anchors."""
    words = re.findall(r"[A-Za-z]{5,}", text)
    variants = []
    for word in words:
        word = word.casefold()
        if word.endswith("ing") and len(word) > 6:
            variants.extend((word[:-3], word[:-3] + "e"))
        elif word.endswith("s") and not word.endswith("ss"):
            variants.append(word[:-1])
    return text + "\n" + " ".join(variants)


def resume_evidence_text(record: Mapping[str, Any]) -> str:
    """Search evidence fields, excluding source IDs and chronology notices."""
    parts = [str(record.get(key) or "") for key in ("title", "body", "preview")]
    for key in ("observation", "session_summary"):
        value = record.get(key)
        if isinstance(value, Mapping):
            parts.extend(str(value.get(field) or "") for field in (
                "request", "investigated", "learned", "completed", "next_steps",
                "decisions", "notes", "narrative", "facts", "subtitle"))
    return "\n".join(parts)


def resume_subject_matches(record: Mapping[str, Any], plan: Mapping[str, Any]) -> bool:
    text = resume_evidence_text(record)
    exact = {"terms": [], "minimum": 0, "identifiers": plan["identifiers"],
             "versions": plan["versions"]}
    if (exact["identifiers"] or exact["versions"]) and not prompt_match_score(text, exact):
        return False
    names = plan["names"]
    return not names or any(re.search(r"(?<!\w)" + re.escape(name) + r"(?!\w)",
                                     text, re.IGNORECASE) for name in names)


# These are bounded lexical evidence hints, not judgments that a deployment
# succeeded or an open condition was resolved. They only rank existing facts.
_ANSWER_FACETS = (
    (r"\bwhy\b|почему", r"\bbecause\b|\breasons?\b|\brationale\b|\bdue to\b|причин|потому|из-за"),
    (r"disabl|отключ", r"disabl|отключ"),
    (r"verif|\btests?\b|\bchecked\b|провер|тест", r"verif|\btests?\b|\bchecked\b|\bpassed\b|провер|тест"),
    (r"deploy|install|выклад|выкат|установ|разв[её]р", r"deploy|install|выклад|выкат|установ|разв[её]р"),
    (r"remain|unresolved|unconfirmed|pending|\bleft\b|остал|согласован|непровер|неизвест", r"remain|unresolved|unconfirmed|unverified|pending|unknown|not agreed|not confirmed|остал|не согласован|не подтверж|непровер|неизвест"),
    (r"notification|уведомлен", r"notification|уведомлен"),
    (r"не\s+(?:реш[а-яё]*|согласова[а-яё]*)|open\s+decisions?|unresolved",
     r"unresolved|undecided|unknown|pending|not\s+(?:decided|agreed)|remain|не\s+(?:реш[а-яё]*|согласова[а-яё]*)"),
    (r"согласова|agre(?:e|ed|ement)|decisions?",
     r"\bagreed\b|\bagreement\b|\bdecisions?\b|\ballowed\b|\bonly\b|согласова|решен|решён"),
)


def resume_answer_score(record: Mapping[str, Any], query: str) -> tuple[int, int]:
    """Prefer requested answer facets over repeated mentions of the subject."""
    text = resume_evidence_text(record).casefold()
    title = str(record.get("title") or "").casefold()
    requested = [evidence for request, evidence in _ANSWER_FACETS
                 if re.search(request, query, re.IGNORECASE)]
    topics = [variants for pattern, variants in _TOPICAL_EQUIVALENTS
              if any(re.fullmatch(pattern, token, re.IGNORECASE)
                     for token in _prompt_tokens(query))]
    def coverage(value: str) -> int:
        return sum(any(re.search(r"(?<!\w)" + re.escape(word) +
                                 (r"[а-яё]*\b" if _RUSSIAN_WORD.fullmatch(word) else r"(?!\w)"), value)
                           for word in variants) for variants in topics)
    if topics and not coverage(text):
        # A generic verification caveat cannot substitute for the requested
        # event/queue/reward topic merely because both mention unfinished work.
        return (0, 0)
    concrete = 0
    timing_words = re.search(r"\bwhen\b|когда|задерж|\bdelay\b|\bdeadline\b", query, re.IGNORECASE)
    processing_timing = re.search(r"обработ|событ|очеред|накоп|задани|\bprocess|\bevents?\b|"
                                  r"\bqueue|\bbatch|\bcoalesc|\bjobs?\b|задерж|\bdelay\b|\bdeadline\b",
                                  query, re.IGNORECASE)
    if timing_words and processing_timing:
        # A concrete duration answers "when" more fully than another mention
        # of processing or an immediate flush trigger. Counts without time
        # units, such as passing tests, cannot supply this evidence.
        duration = (r"\b(?:\d+(?:\.\d+)?|zero|one|two|three|four|five|six|seven|eight|nine|ten|"
                    r"eleven|twelve|twenty|thirty|sixty|одну|один|два|две|три|четыре|пять|десять)"
                    r"[ -]+(?:milliseconds?|seconds?|minutes?|hours?|мс|секунд[а-яё]*|минут[а-яё]*)\b")
        concrete += 4 * bool(re.search(duration, text))
        concrete += bool(re.search(r"\bimmediate(?:ly)?\b|\bflush(?:es|ed)?\b|немедлен|сразу", text))
        concrete += 3 * bool(re.search(r"\bfirst\s+deadline\b|\b(?:cannot|not|never)\s+postpone\b|"
                                       r"перв[а-яё]*\s+(?:срок|дедлайн)|не\s+откладыва", text))
    if re.search(r"разн[а-яё]*\s+ход[а-яё]*|\b(?:different|distinct|known|unknown|same)[ -]+"
                 r"(?:known[ -]+)?turns?\b|\bturn[ -]+boundar", query, re.IGNORECASE):
        # Concrete boundary qualifications and their legacy exception are
        # complementary evidence. Generic event/job vocabulary supplies neither.
        concrete += 3 * bool(re.search(r"\b(?:known|different|distinct|same)[ -]+(?:known[ -]+)?turn"
                                       r"|известн[а-яё]*\s+ход|разн[а-яё]*\s+ход", text))
        concrete += 3 * bool(re.search(r"\b(?:unknown|legacy)[ -]+(?:turn|batch|boundar)|"
                                       r"\blegacy[^.!?\n]{0,64}\bturn[ _-]+ids?\b|"
                                       r"\b(?:without|missing|unknown)[ -]+turn[ _-]+ids?\b"
                                       r"|неизвестн[а-яё]*\s+ход", text))
    if re.search(r"стади|этап|начина|\bstages?\b|\bphases?\b|\bstart", query, re.IGNORECASE):
        # Starting-stage questions need the proposed work and its dependency
        # order, not only repeated descriptions of the commercial model.
        concrete += 3 * bool(re.search(r"\bresearch\b|\btechnical study\b|исследован|рес[её]рч", text))
        dependency = re.search(r"\bbackend\b|\bdependencies\b|\bprerequisites?\b|зависим|серверн", text)
        sequence = re.search(r"\bbefore\b|\bparallel\b|\bindependent\b|паралл|до\s+|независим", text)
        concrete += 2 * bool(dependency and sequence)
    checks_requested = re.search(r"проверк|тест|\bchecks?\b|\btests?\b|\bci\b|verification", query, re.IGNORECASE)
    outcome_requested = re.search(r"прош[её]л|прошли|результат|\bpassed\b|\bresults?\b|\boutcomes?\b", query, re.IGNORECASE)
    if checks_requested and outcome_requested:
        # Answering which checks passed requires outcomes, not another generic
        # "verification pending" heading. These remain reported claims with
        # their original provenance and execution limits, never truth labels.
        outcome = re.search(r"\b(?:passed|passing|successful|success)\b", text)
        if outcome and not re.search(r"\bnot\s+$|\bне\s+$", text[max(0, outcome.start() - 12):outcome.start()]):
            concrete += 3
        concrete += 2 * bool(re.search(r"\b\d+\s+(?:(?:integration|e2e|pending|passing|existing)\s+)?"
                                       r"(?:tests?|suites?|skips?)\b|\b\d+\s+тест[а-яё]*", text))
    if re.search(r"провайдер|\bproviders?\b", query, re.IGNORECASE):
        execution_scope = re.search(r"\breal[ -][^.!?\n]{0,90}(?:tests?|suites?|providers?)[^.!?\n]{0,90}"
                                    r"(?:disabled|not\s+(?:run|executed|enabled))|"
                                    r"реальн[а-яё]*\s+провайдер[^.!?\n]{0,90}(?:отключ|не\s+запуск)", text)
        concrete += 3 * bool(execution_scope)
    return (concrete + sum(bool(re.search(pattern, text)) for pattern in requested) + coverage(text),
            sum(bool(re.search(pattern, title)) for pattern in requested) + coverage(title))


def retrieval_query_hints(query: str) -> list[str]:
    """Bounded lexical probes for explicit software topics in the question.

    Callers must reapply original literal constraints and project/filter scope
    to every candidate. Hints alone are never authority to widen that scope.
    """
    groups = [variants for pattern, variants in _TOPICAL_EQUIVALENTS
              if any(re.fullmatch(pattern, token, re.IGNORECASE)
                     for token in _prompt_tokens(query))]
    english = [next(word for word in variants if word.isascii()) for variants in groups]
    # Pairing explicit topics avoids filling a window with generic "events".
    probes = [" ".join(english[index:index + 2]) for index in range(0, len(english), 2)]
    preferred: list[str] = []
    if re.search(r"разн[а-яё]*\s+ход[а-яё]*|\b(?:different|distinct|known|unknown)[ -]+turns?\b",
                 query, re.IGNORECASE):
        preferred.extend(("known turn", "unknown turn"))
    if any("coalescing" in variants for variants in groups):
        # A bare "batch" misses inflected "batching" and "coalescing" in
        # unicode FTS. Probe the explicit accumulation topic separately.
        preferred.append("coalescing")
        if re.search(r"\bwhen\b|когда|задерж|\bdelay\b|\bdeadline\b", query, re.IGNORECASE):
            preferred.append("deadline")
    # Keep variant probes bounded. Original question literals and caller
    # filters are reapplied by the candidate loader before these can rank.
    return list(dict.fromkeys([*preferred, *probes]))[:6]


def current_installation_question(query: str, project: str | Path | None = None) -> bool:
    """Recognize installation ordering intent even with configuration subquestions.

    This only changes ordering within scoped candidates. It does not grant
    recent-project expansion or relax a named feature, path, issue or version.
    """
    if historical_query(query):
        return False
    if re.search(r"\bwas\s+installed\b|\bбыла\s+установлена\b", query, re.IGNORECASE):
        return False
    words = _PROMPT_TOKEN.findall(query.casefold())
    if any(_PROMPT_VERSION.fullmatch(word) or re.search(r"[_./:]|\d", word) for word in words):
        return False
    version_word = re.search(r"\b(?:version|верси[а-яё]*)\b", query, re.IGNORECASE)
    if not version_word:
        return False
    installed_word = re.search(r"\b(?:install(?:ed|ation)?|установ[а-яё]*)\b",
                               query[version_word.end():], re.IGNORECASE)
    if not installed_word:
        return False
    # The installed-version subject is before the installed verb. A project
    # named later in "Redis is installed in Atlas" is a location, not the
    # software whose version the user requested.
    subject_text = query[version_word.end():version_word.end() + installed_word.start()]
    prefix = re.split(r"[:,;?!]", query[:version_word.start()])[-1].strip()
    owner_pattern = None
    if project is not None:
        owner = re.sub(r"[-_]public$", "", Path(project).name.casefold())
        parts = [re.escape(part) for part in re.split(r"[\W_]+", owner) if part]
        if parts:
            owner_pattern = r"(?<!\w)" + r"[\W_]*".join(parts) + r"(?!\w)"
            subject_text = re.sub(owner_pattern, " ", subject_text, flags=re.IGNORECASE)
    plan = prompt_query_plan("How " + subject_text)
    if plan and any(term not in _PROJECT_STATE_WORDS and not _PROJECT_STATE_RUSSIAN.fullmatch(term)
                    for term, _ in plan["terms"]):
        return False
    prefix_words = _PROMPT_TOKEN.findall(prefix.casefold())
    if prefix_words and not (owner_pattern and re.search(owner_pattern + r"\s*$", prefix, re.IGNORECASE)):
        previous = prefix_words[-1]
        if previous not in _PROJECT_STATE_WORDS and not _PROJECT_STATE_RUSSIAN.fullmatch(previous):
            return False
    return True


def linked_handoff_matches(origin: Mapping[str, Any], later: Mapping[str, Any],
                           query: str, project: str | Path) -> bool:
    """Accept only a topical explicit continuation in the same project/session."""
    if historical_query(query) or not origin.get("session_id"):
        return False
    if (origin.get("later_summary_id") != later.get("id")
            or later.get("project") != str(project) or later.get("superseded_by")
            or later.get("session_id") != origin.get("session_id")
            or is_capture_source(later.get("source"))):
        return False
    plan = resume_subject_plan(query, project)
    if plan and any(plan.values()):
        return resume_subject_matches(origin, plan) and resume_subject_matches(later, plan)
    topical = prompt_query_plan(query)
    if not topical:
        return False
    broad = dict(topical, minimum=1)
    return bool(prompt_match_score(resume_evidence_text(origin), broad)
                and prompt_match_score(resume_evidence_text(later), broad))


def issue_discovery_association(origin: Mapping[str, Any], candidate: Mapping[str, Any],
                                query: str) -> bool:
    """Link pre-ID discovery through an already exact-matched task record.

    Same session alone is insufficient. Both records must name the same scoped
    task file, and all non-issue literal constraints remain mandatory. This is
    an evidence association, never a claim that the issue text contains it.
    """
    project = origin.get("project")
    if (not isinstance(project, str) or candidate.get("project") != project
            or not origin.get("session_id") or candidate.get("session_id") != origin.get("session_id")
            or candidate.get("superseded_by") or is_capture_source(candidate.get("source"))
            or historical_query(query)):
        return False
    plan = resume_subject_plan(query, project)
    if not plan or not resume_subject_matches(origin, dict(plan, names=[])):
        return False
    issues = [word for word in plan["identifiers"] if re.fullmatch(r"[a-z][a-z0-9]*-\d+", word)]
    if not issues:
        return False
    candidate_issues = set(re.findall(r"(?<![\w.-])[a-z][a-z0-9]*-\d+(?![\w.-])",
                                     resume_evidence_text(candidate).casefold()))
    if candidate_issues - set(issues):
        return False
    remaining = dict(plan, identifiers=[word for word in plan["identifiers"] if word not in issues])
    if not resume_subject_matches(candidate, remaining):
        return False
    def task_files(record: Mapping[str, Any]) -> set[str]:
        metadata = record.get("observation")
        if not isinstance(metadata, Mapping):
            return set()
        paths: set[str] = set()
        for field in ("files_read", "files_modified"):
            values = metadata.get(field)
            if not isinstance(values, list):
                continue
            for value in values:
                if not isinstance(value, str):
                    continue
                path = Path(value)
                if path.is_absolute():
                    try:
                        path = path.relative_to(project)
                    except ValueError:
                        continue
                parts = tuple(part.casefold() for part in path.parts)
                if ".." in parts or path.suffix.casefold() not in {".md", ".rst", ".txt"}:
                    continue
                shared = {"shared", "common", "template", "templates", "_templates",
                          "boilerplate", "example", "examples", "general", "misc", "global"}
                if any(part in shared for part in parts[:-1]) or path.stem.casefold() == "readme":
                    continue
                exact_issue_path = any(re.search(r"(?<![\w])" + re.escape(issue) + r"(?![\w])", path.as_posix(), re.IGNORECASE)
                                       for issue in issues)
                task_root = bool(parts and parts[0] in {"tasks", "issues", "tickets"})
                categories = {"backlog", "grooming", "complete", "completed", "archive", "active", "open", "closed"}
                owned_folders = [part for part in parts[1:-1] if part not in categories]
                if exact_issue_path or (task_root and owned_folders):
                    paths.add(path.as_posix())
        return paths
    return bool(task_files(origin) & task_files(candidate))


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
    "version versions installed installation install readme differs difference compare comparison "
    "каково каковы над после перед осталось остались остается остаётся оставалось "
    "нужно нужны сделать сделано дальше далее чем вошло входит выкатили выкачен "
    "выпустили выпущено подтверждено подтверждён подтвержден подтвердили прошёл прошел отличается различается"
).split())
_PROJECT_STATE_RUSSIAN = re.compile(
    r"(?:плагин|памят|систем|проект|репозитор|приложен|программ|работ|задач|"
    r"состоян|статус|результат|улучш|изменен|изменён|последн|текущ|незаверш|"
    r"незаконч|незакрыт|непровер|провер|открыт|нужн|нов|релиз|выпуск|подтвержд|"
    r"подтверд|верси|установ|исправ)[а-яё]*\Z"
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
    installed_version = project_anchor and any(
        re.fullmatch(r"install(?:ed|ation)?|установ[а-яё]*", word) for word in words
    ) and any(re.fullmatch(r"versions?|верси[а-яё]*", word) for word in words)
    if not (explicit_state or assessment or installed_version):
        return None
    if any(re.fullmatch(r"install(?:ed|ation)?|установ[а-яё]*", word) for word in words):
        return "installation"
    if any(re.fullmatch(r"versions?|верси[а-яё]*", word) for word in words):
        return "version"
    return "release" if any(re.fullmatch(r"releases?|released|rollout|релиз[а-яё]*|выпуск[а-яё]*", word)
                            for word in words) else "project"


def broad_current_state_query(query: str, project: str | Path | None = None) -> bool:
    return current_state_scope(query, project) is not None


def continuation_state_plan(query: str) -> dict[str, Any] | None:
    """Match a named disablement outcome after maintenance, without widening history.

    This narrow topical route permits explicit same-session continuation links.
    Both the named subject and the disabled state must occur in the later
    handoff. It does not authorize project-wide recent-record expansion.
    """
    if historical_query(query):
        return None
    words = _PROMPT_TOKEN.findall(unicodedata.normalize("NFKC", query).casefold())
    if not (any(word in {"after", "после"} for word in words)
            and any(_PROJECT_STATE_INTENT.fullmatch(word) for word in words)
            and any(re.fullmatch(r"disable[ds]?|отключ[а-яё]*", word) for word in words)):
        return None
    plan = prompt_query_plan(query)
    if not plan or plan["identifiers"] or plan["versions"]:
        return None
    topics = [(term, stem) for term, stem in plan["terms"]
              if term not in _PROJECT_STATE_WORDS
              and not _PROJECT_STATE_RUSSIAN.fullmatch(term)
              and not re.fullmatch(r"disable[ds]?|maintenance|problems?|отключ[а-яё]*|"
                                   r"обслужив[а-яё]*|проблем[а-яё]*", term)]
    if not topics:
        return None
    return {"terms": topics + [("disabled", "disabled")], "identifiers": [],
            "versions": [], "minimum": len(topics) + 1}


def state_scope_plan(scope: str) -> dict[str, Any] | None:
    """Current-state paraphrases retain their bounded bilingual subject."""
    if scope == "installation":
        terms = [("install", "install"), ("installed", "installed"), ("installation", "installation"),
                 ("установлена", "установлен"), ("установки", "установк")]
    elif scope == "version":
        terms = [("version", "version"), ("versions", "versions"), ("runtime", "runtime"),
                 ("версии", "верс"), ("release", "release"), ("релиза", "релиз")]
    elif scope == "release":
        terms = [("release", "release"), ("released", "released"), ("rollout", "rollout"),
                 ("релиза", "релиз"), ("выпуска", "выпуск")]
    else:
        return None
    return {"terms": terms,
            "identifiers": [], "versions": [], "minimum": 1}


def software_subject_matches(title: str, project: str | Path, evidence: str = "") -> bool:
    """Require the project subject or a generic project-runtime heading.

    The latter keeps existing source-bound install reports usable without
    accepting arbitrary package names. This positive boundary is independent
    of package name, word order, language and whether a version is present.
    """
    words = _PROMPT_TOKEN.findall(unicodedata.normalize("NFKC", title).casefold())
    project_name = re.sub(r"[-_]public$", "", Path(project).name.casefold())
    own = re.sub(r"\W|_", "", project_name)
    if own and own in re.sub(r"\W|_", "", " ".join(words)):
        return True
    if evidence:
        # A platform heading need not repeat the application's name. Require
        # an actual installation assertion with this project as its subject;
        # mentioning a project alongside an installed dependency is weaker.
        parts = [re.escape(part) for part in re.split(r"[\W_]+", project_name) if part]
        subject = r"(?<!\w)" + r"[\W_]*".join(parts) + r"(?!\w)"
        version = r"(?:\s+(?:version|верси[а-яё]*)\s*v?\d+(?:\.\d+)*|\s+v?\d+(?:\.\d+)+)?"
        copula = r"(?:\s+(?:runtime|is|was|has|been|now|currently|был|была|сейчас))*\s+"
        installed = r"(?:installed|установлен[а-яё]*)\b"
        if parts and (re.search(subject + version + copula + installed, evidence, re.IGNORECASE)
                      or re.search(installed + r"\s+" + subject, evidence, re.IGNORECASE)):
            return True
    noise = _PROJECT_STATE_WORDS | _TOPIC_NOISE | frozenset((
        "three two one roots root report reports reported showed shows matching matched across "
        "installer package packaging files file source passed passing completed checked verified runtime "
        "verification local captured command parity refreshed refresh restarted restart "
        "проверка отчет отчёт показал сообщила установлена установлено"
    ).split())
    return bool(words) and all(word in noise or word.isdigit() or _PROMPT_VERSION.fullmatch(word)
                               or _PROJECT_STATE_RUSSIAN.fullmatch(word) for word in words)


def state_scope_matches(record: Mapping[str, Any], scope: str, project: str | Path | None = None) -> bool:
    plan = state_scope_plan(scope)
    if plan is None:
        return True
    if not any(record.get(key) for key in ("title", "body", "preview", "session_summary")):
        return True  # ID-only adapters have already applied their query match.
    if scope in {"installation", "version"} and project is not None and not software_subject_matches(
        str(record.get("title") or ""), project, resume_evidence_text(record)
    ):
        return False
    text = " ".join(str(record.get(key) or "") for key in
                    ("title", "body", "preview", "session_summary", "observation"))
    return bool(prompt_match_score(text, plan))


def continuation_state_matches(record: Mapping[str, Any], plan: Mapping[str, Any]) -> bool:
    text = " ".join(str(record.get(key) or "") for key in
                    ("title", "body", "preview", "session_summary", "observation"))
    return bool(prompt_match_score(text, plan))


def installation_receipt(record: Mapping[str, Any]) -> bool:
    """Prefer records about installation over text merely quoting its query.

    This is an evidence-subject preference, not verification of the report.
    Source inspection, test definitions and version-file edits remain available
    after installation reports even if captured more recently.
    """
    heading = str(record.get("title") or "").casefold()
    if re.search(r"\b(?:regressions?|diagnostics?|test\s+(?:cases?|definitions?)|"
                 r"регресси[а-яё]*|диагност[а-яё]*)\b", heading):
        return False
    return bool(re.search(r"\b(?:installed|installation|runtime|install\s+report|"
                          r"release\s+verification|установ[а-яё]*)\b", heading))


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
    "source_evidence", "evidence_truncated", "query_relevant_evidence",
    "issue_association",
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
    from .privacy import redact_search_value
    if detail == "full":
        return [redact_search_value(dict(record)) for record in records]
    result = []
    for record in records:
        preview = {key: record[key] for key in _PREVIEW_FIELDS if key in record}
        if record.get("source") == "project_memory":
            # Status, scope and provisional provenance are part of the meaning
            # of a project-memory hit, including in the default compact mode.
            for key in ("workflow_state", "requested_state", "source_role", "evidence_basis", "lane",
                        "project_memory_kind", "capture_order_basis", "logical_episode_id", "parent_episode_id",
                        "continuity_basis", "signal_kind", "selection_basis"):
                if isinstance(record.get(key), str):
                    preview[key] = record[key][:100]
            for key in ("provisional", "inferred", "is_current_head"):
                if isinstance(record.get(key), bool):
                    preview[key] = record[key]
            for key in ("revision", "source_capture_seq_min", "source_capture_seq_max", "episode_version"):
                if type(record.get(key)) is int:
                    preview[key] = record[key]
            scope = record.get("scope")
            if isinstance(scope, Mapping):
                preview["scope"] = {key: value[:512] for key, value in scope.items()
                                    if key in {"environment", "version", "worktree"}
                                    and isinstance(value, str)}
            evidence = record.get("evidence")
            if isinstance(evidence, list):
                preview["evidence"] = [{key: value[:512] if isinstance(value, str) else value
                    for key, value in item.items() if key in {"source_id", "start", "end", "source_role", "field", "quote_excerpt", "quote_truncated", "tool_use_id", "source_field", "context_basis"}
                    and (isinstance(value, (str, bool)) or type(value) is int)}
                    for item in evidence[:8] if isinstance(item, Mapping)]
            execution = record.get("execution")
            if isinstance(execution, Mapping):
                preview["execution"] = {key: value[:512] if isinstance(value, str) else value
                    for key, value in execution.items() if key in {
                        "tool_use_id", "tool_name", "command", "exit_code", "result_field", "capture_recorded_at"}
                    and (isinstance(value, str) or type(value) is int)}
            fields = record.get("fields")
            if isinstance(fields, list):
                preview["fields"] = []
                for field in fields[:6]:
                    if not isinstance(field, Mapping) or not isinstance(field.get("name"), str):
                        continue
                    evidence = field.get("evidence")
                    if not isinstance(evidence, list) or not evidence:
                        continue
                    bounded = {"name": field["name"][:32], "text": str(field.get("text") or "")[:700],
                               "evidence": [{key: value[:512] if isinstance(value, str) else value
                                    for key, value in pointer.items()
                                    if key in {"source_id", "start", "end", "source_role", "field", "quote_excerpt", "quote_truncated", "tool_use_id", "source_field", "context_basis"}
                                    and (isinstance(value, (str, bool)) or type(value) is int)}
                                    for pointer in evidence[:8] if isinstance(pointer, Mapping)]}
                    preview["fields"].append(bounded)
            source_ids = record.get("source_ids")
            if isinstance(source_ids, list):
                preview["source_ids"] = [value[:512] for value in source_ids[:8] if isinstance(value, str)]
        observation = record.get("observation")
        if isinstance(observation, Mapping) and observation.get("type"):
            preview["observation"] = {"type": observation["type"]}
        preview["source_count"] = len(record.get("source_ids") or [])
        preview["detail"] = "compact"
        preview["details_available"] = True
        result.append(redact_search_value(preview))
    return result
