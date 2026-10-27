"""Small, dependency-free privacy helpers used before memory is persisted.

The redactor deliberately targets high-confidence secret shapes.  It is not a
general data-loss-prevention system, but it makes the common accidental cases
safe to put in a local memory database: credentials in prose, JSON, dotenv
snippets, URLs, and explicitly private blocks.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping


REDACTED = "[REDACTED]"


# An unfinished private block is treated as private through the end of the
# string.  This matters for interrupted tool or chat output.
_PRIVATE_ANGLE_TOKEN_RE = re.compile(
    r"<\s*(?P<closing>/?)\s*(?P<tag>private|secret|sensitive)\b[^>]*>",
    re.IGNORECASE,
)
_PRIVATE_BRACKET_TOKEN_RE = re.compile(
    r"\[\s*(?P<closing>/?)\s*(?P<tag>private|secret|sensitive)\s*\]",
    re.IGNORECASE,
)
_PRIVATE_UNFINISHED_ANGLE_RE = re.compile(
    r"<\s*(?:private|secret|sensitive)\b[^>]*$", re.IGNORECASE | re.DOTALL
)
_PRIVATE_UNFINISHED_BRACKET_RE = re.compile(
    r"\[\s*(?:private|secret|sensitive)\b[^\]]*$", re.IGNORECASE | re.DOTALL
)
_PRIVATE_PEM_RE = re.compile(
    r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----.*?(?:-----END [A-Z0-9 ]*"
    r"PRIVATE KEY-----|$)",
    re.DOTALL,
)

_SECRET_NAME = (
    r"(?:[A-Za-z][A-Za-z0-9]*[_-])*"
    r"(?:api[_-]?key|apikey|access[_-]?token|refresh[_-]?token|auth(?:entication)?"
    r"[_-]?token|authorization|credential|client[_-]?secret|private[_-]?key|secret(?:[_-]?key)?|"
    r"password|passwd|token|key)"
    r"(?:[_-][A-Za-z0-9]+)*"
)

_SECRET_FIELD_RE = re.compile(_SECRET_NAME, re.IGNORECASE)


def is_secret_field(name: str) -> bool:
    """Recognize structured credential fields with the shared secret-name policy."""
    return _SECRET_FIELD_RE.fullmatch(name) is not None


_JSON_QUOTED_SECRET_RE = re.compile(
    rf"([\"']{_SECRET_NAME}[\"']\s*:\s*)"
    r"(\"(?:\\.|[^\"\\])*\"|'(?:\\.|[^'\\])*')",
    re.IGNORECASE,
)
_JSON_BARE_SECRET_RE = re.compile(
    rf"([\"']{_SECRET_NAME}[\"']\s*:\s*)(?![\"'])([^,\]\}}\s]+)",
    re.IGNORECASE,
)

# dotenv / shell assignment lines.  Keep the name, because it is often useful
# context when debugging a setup without retaining the secret value.
_ENV_SECRET_RE = re.compile(
    rf"(?im)^(\s*(?:export\s+)?{_SECRET_NAME}\s*=\s*)"
    r"(?!\[REDACTED\])(\"(?:\\.|[^\"\\])*\"|'(?:\\.|[^'\\])*'|[^\s#]+)",
)
_INLINE_SECRET_RE = re.compile(
    rf"(?i)(\b{_SECRET_NAME}\s*[:=]\s*)"
    r"(?!\[REDACTED\])(\"(?:\\.|[^\"\\])*\"|'(?:\\.|[^'\\])*'|[^\s,;\]\}}]+)",
)

_URL_USERINFO_RE = re.compile(r"(?i)\b([a-z][a-z0-9+.-]*://)[^\s/@]+@")
_URL_QUERY_SECRET_RE = re.compile(
    rf"(?i)([?&;]{_SECRET_NAME}=)([^&#\s]*)"
)
_BEARER_RE = re.compile(r"(?i)\bbearer\s+[a-z0-9._~+/=-]{8,}")

_TOKEN_PATTERNS = (
    re.compile(r"\bapikey_[A-Fa-f0-9]{24,}_[A-Fa-f0-9]{32,}\b"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9_]{20,}\b"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b"),
    re.compile(r"\bglpat-[A-Za-z0-9_-]{15,}\b"),
    re.compile(r"\bxox(?:b|p|a|r|s)-[A-Za-z0-9-]{10,}\b"),
    re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b"),
    re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b"),
    re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9_-]{20,}\b"),
    re.compile(r"\bsk-ant-[A-Za-z0-9_-]{20,}\b"),
    re.compile(r"\b(?:sk|rk)_(?:live|test)_[A-Za-z0-9]{16,}\b"),
    re.compile(r"\bhf_[A-Za-z0-9]{20,}\b"),
    re.compile(r"\bnpm_[A-Za-z0-9]{20,}\b"),
    re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b"),
)


# This is only a no-match filter for ordinary ASCII output rows. Private tags
# require < or [, PEM keys contain "key", URL userinfo requires ://, and
# query/assignment names contain one of the credential-name fragments below.
# Bearer values and every standalone token family have the listed markers.
# False positives run the unchanged redactor. Non-ASCII text always runs it too,
# preserving Python IGNORECASE's Unicode behavior. New pattern families must
# retain a necessary marker here or bypass this filter.
_REDACTION_MARKERS = (
    "<", "[", "://", "key", "token", "auth", "credential", "secret",
    "password", "passwd", "bearer", "ghp_", "gho_", "ghu_", "ghs_", "ghr_",
    "github_pat_", "glpat-", "xoxb-", "xoxp-", "xoxa-", "xoxr-", "xoxs-",
    "akia", "asia", "aiza", "sk-", "sk_", "rk_", "hf_", "npm_", "eyj",
)


def _replace_quoted(match: re.Match[str]) -> str:
    """Preserve JSON/shell quoting while replacing the value."""

    value = match.group(2)
    if value == REDACTED or value in {f'"{REDACTED}"', f"'{REDACTED}'"}:
        return match.group(0)
    quote = value[0] if value and value[0] in {"'", '"'} else ""
    return f"{match.group(1)}{quote}{REDACTED}{quote}"


def _redact_tagged_blocks(
    text: str,
    token_pattern: re.Pattern[str],
    replacement: str,
) -> str:
    """Remove nested or unfinished explicit-private blocks without leaks."""

    output: list[str] = []
    cursor = 0
    while True:
        opening = token_pattern.search(text, cursor)
        if opening is None:
            output.append(text[cursor:])
            break
        if opening.group("closing"):
            output.append(text[cursor : opening.end()])
            cursor = opening.end()
            continue

        output.append(text[cursor : opening.start()])
        depth = 1
        scan_from = opening.end()
        closing_end = len(text)
        while True:
            token = token_pattern.search(text, scan_from)
            if token is None:
                break
            if token.group("closing"):
                depth -= 1
                if depth == 0:
                    closing_end = token.end()
                    break
            else:
                depth += 1
            scan_from = token.end()
        output.append(replacement)
        cursor = closing_end
    return "".join(output)


def redact_text(value: str) -> str:
    """Return *value* with obvious credentials and private blocks removed.

    The function intentionally accepts only text.  Callers that have a tool
    payload or another structured object must first create a human summary;
    serializing opaque objects here would make accidental raw-payload storage
    too easy.
    """

    if not isinstance(value, str):
        raise TypeError("redact_text accepts text only")

    if value.isascii():
        lowered = value.lower()
        if not any(marker in lowered for marker in _REDACTION_MARKERS):
            return value

    text = value
    text = _redact_tagged_blocks(
        text, _PRIVATE_ANGLE_TOKEN_RE, f"<private>{REDACTED}</private>"
    )
    text = _redact_tagged_blocks(
        text, _PRIVATE_BRACKET_TOKEN_RE, f"[private]{REDACTED}[/private]"
    )
    text = _PRIVATE_UNFINISHED_ANGLE_RE.sub(f"<private>{REDACTED}</private>", text)
    text = _PRIVATE_UNFINISHED_BRACKET_RE.sub(f"[private]{REDACTED}[/private]", text)
    text = _PRIVATE_PEM_RE.sub(REDACTED, text)
    text = _URL_USERINFO_RE.sub(r"\1" + REDACTED + "@", text)
    text = _URL_QUERY_SECRET_RE.sub(r"\1" + REDACTED, text)
    text = _BEARER_RE.sub("Bearer " + REDACTED, text)
    text = _JSON_QUOTED_SECRET_RE.sub(_replace_quoted, text)
    text = _JSON_BARE_SECRET_RE.sub(r"\1" + REDACTED, text)
    text = _ENV_SECRET_RE.sub(_replace_quoted, text)
    text = _INLINE_SECRET_RE.sub(_replace_quoted, text)
    for pattern in _TOKEN_PATTERNS:
        text = pattern.sub(REDACTED, text)
    return text


def redact_tags(tags: Iterable[str]) -> list[str]:
    """Redact, trim, and de-duplicate user-provided tags in input order."""

    if isinstance(tags, str):
        raise TypeError("tags must be an iterable of text values")

    result: list[str] = []
    seen: set[str] = set()
    for tag in tags:
        if not isinstance(tag, str):
            raise TypeError("tags must contain text values")
        cleaned = redact_text(tag).strip()
        if cleaned and cleaned not in seen:
            result.append(cleaned)
            seen.add(cleaned)
    return result


# Search previews are a discovery surface. Contact evidence remains available
# through an explicit record read, but is unnecessary in routine topic results.
_SEARCH_EMAIL_RE = re.compile(r"(?i)(?<![\w.+-])[\w.+-]+@[\w.-]+\.[a-z]{2,}(?![\w.-])")
_SEARCH_PHONE_RE = re.compile(r"(?<![\w])\+\d[\d ()-]{7,}\d(?!\w)")
_SEARCH_CYRILLIC_FULL_NAME_RE = re.compile(
    r"(?<![А-Яа-яЁё])(?:[А-ЯЁ][а-яё]{2,}[ \t]+){1,2}[А-ЯЁ][а-яё]{2,}(?![А-Яа-яЁё])")
_SEARCH_PERSON_LABEL_RE = re.compile(
    r"(?im)(\b(?:full[_ ]?name|display[_ ]?name|ФИО|полное имя|"
    r"telegram(?:[_ ]user)?[_ ]?id|chat[_ ]?id|user[_ ]?id)\s*[:=]\s*)"
    r"([^\n,;]+)")
_SEARCH_PERSON_FIELDS = frozenset((
    "full_name", "display_name", "first_name", "last_name", "phone",
    "phone_number", "email", "email_address", "telegram_id", "chat_id",
    "user_id", "contact_name", "participant_name", "candidate_name",
))
_SEARCH_IDENTITY_FIELDS = frozenset((
    "id", "project", "session_id", "turn_id", "source_ids", "source_id",
    "source_path", "event_id", "later_summary_id", "later_context_id",
    "superseded_by", "files_read", "files_modified",
))


def redact_search_text(value: str) -> str:
    """Hide high-confidence contact shapes in search, without changing storage."""
    text = redact_text(value)
    text = _SEARCH_EMAIL_RE.sub("[CONTACT]", text)
    text = _SEARCH_PHONE_RE.sub("[CONTACT]", text)
    text = _SEARCH_PERSON_LABEL_RE.sub(r"\1[PERSONAL]", text)
    return _SEARCH_CYRILLIC_FULL_NAME_RE.sub("[PERSONAL]", text)


def redact_search_value(value: object, *, field: str = "") -> object:
    """Copy presentation data, preserving record identity and evidence lineage."""
    if field in _SEARCH_IDENTITY_FIELDS:
        return value
    if field.casefold() in _SEARCH_PERSON_FIELDS:
        return "[PERSONAL]"
    if isinstance(value, str):
        return redact_search_text(value)
    if isinstance(value, Mapping):
        return {key: redact_search_value(item, field=str(key))
                for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact_search_value(item) for item in value]
    return value
