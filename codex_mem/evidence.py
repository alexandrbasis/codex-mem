"""Read-only excerpts from explicit source links, never generated conclusions.

Text budgets count displayed characters. Source and depth budgets bound reads;
all excerpts remain historical, untrusted evidence rather than verification.
"""
from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from html import unescape
import json
import re
from typing import Any

from .privacy import redact_text
from .retrieval import prompt_query_plan, prompt_term_variants, prompt_variant_prefix

_QUALIFICATION = re.compile(
    r"unverified|unconfirmed|not\s+(?:a\s+|an\s+)?(?:database|atomic|verified|tested|deployed|confirmed|recommendation|proof|production|acceptance)|"
    r"remain\w*|pending|caveat|limitation|does\s+not|cannot|uncertain|"
    r"не\s+(?:подтверж|провер|гарантир|явля|обеспеч)|ограничен|неизвест|неопредел", re.I)
_BEHAVIOR = re.compile(
    r"reload|restor|preserv|persist|filter|publish|publication|instruction|"
    r"восстан|сохран|фильтр|публикац|инструкц", re.I)
_TEXT_KEYS = frozenset(("text", "output", "stdout", "stderr", "body", "description",
                        "message", "summary", "result", "content", "title"))
_PRIVATE_PREFIXES = ("private", "secret", "sensitive")
_NUMBER = re.compile(r"(?<![\w.-])\d+(?:\.\d+)?(?![\w.-])")
_RESULT_QUERY = re.compile(
    r"replay|recover|results?|outcomes?|counts?|completed|повтор|восстанов|"
    r"результат|итог|законч|сколько", re.I)
_AGGREGATE = re.compile(
    r"attempt_deltas|receipt_statuses|receipt_count|approved_jobs|"
    r"\b(?:one|single) attempt (?:per|each)|\ball \d+\b|"
    r"\b\d+ (?:completed|processed|skipped)\b|"
    r"одн[а-яё]* попытк[а-яё]* (?:на|для|кажд)", re.I)


def _private_record(record: Mapping[str, Any]) -> bool:
    """Private source/tag prefixes exclude the whole linked branch."""
    source = str(record.get("source") or "").strip().casefold()
    if source.startswith(_PRIVATE_PREFIXES):
        return True
    tags = record.get("tags", record.get("tags_json", [])) or []
    if isinstance(tags, str):
        try:
            tags = json.loads(tags)
        except ValueError:
            tags = [tags]
    if isinstance(tags, str):
        tags = [tags]
    return isinstance(tags, (list, tuple)) and any(
        isinstance(tag, str) and tag.strip().casefold().startswith(_PRIVATE_PREFIXES) for tag in tags
    )


def _safe_source_text(text: str) -> str:
    # Captured HTML and JSON may encode privacy delimiters. Decode only text
    # syntax, never evaluate it, before applying mandatory block redaction.
    for _ in range(2):
        text = unescape(text)
    text = re.sub(r"\\u00(3c|3e|5b|5d)", lambda m: chr(int(m[1], 16)), text, flags=re.I)
    return redact_text(text)


def _response_text(value: Any, *, depth: int = 0) -> list[str]:
    """Extract display text, never command inputs or arbitrary personal fields."""
    if depth > 8:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [text for item in value[:100] for text in _response_text(item, depth=depth + 1)]
    if isinstance(value, Mapping):
        parts = []
        for key, item in list(value.items())[:100]:
            if key in _TEXT_KEYS or isinstance(item, (Mapping, list)):
                parts.extend(_response_text(item, depth=depth + 1))
        return parts
    return []


def _capture_text(capture: Mapping[str, Any]) -> tuple[str, bool]:
    value = capture.get("tool_response")
    oversized = isinstance(value, str) and len(value) > 131_072
    if isinstance(value, str):
        value = value[:131_072]
        try:
            value = json.loads(value)
        except (ValueError, RecursionError):
            pass
    text = "\n".join(_response_text(value))
    metadata = capture.get("response_metadata")
    clipped = bool(isinstance(metadata, Mapping) and metadata.get("truncated"))
    return text[:131_072], clipped or oversized or len(text) > 131_072


def expand_source_evidence(
    records: Sequence[Mapping[str, Any]], query: str, project: str, *,
    load_records: Callable[[list[str]], Sequence[Mapping[str, Any]]],
    load_capture: Callable[[str], Mapping[str, Any] | None] | None = None,
    redact: Callable[[str], str] | None = None,
    max_total_chars: int = 6_000, max_record_chars: int = 1_800,
    max_excerpt_chars: int = 600, max_sources: int = 48,
    max_depth: int = 2, max_excerpts: int = 4,
) -> list[dict[str, Any]]:
    """Attach query-related excerpts without changing notes or source records.

    Callbacks must be project-scoped, read-only loaders. Results are checked again
    for scope and identity. A superseded source is allowed only when its
    replacement is on the explicit ancestry path, as with processed raw events.
    No later replacement, unrelated source, or command input is followed.
    Private source/tag prefixes reject roots and whole ancestor branches.
    Mandatory private-block redaction precedes the optional ``redact`` callback;
    callers supply their query
    privacy policy. Source text never changes these rules or calls any tools.
    """
    limits = (max_total_chars, max_record_chars, max_excerpt_chars, max_sources, max_depth, max_excerpts)
    if any(isinstance(n, bool) or not isinstance(n, int) or n < 1 for n in limits):
        raise ValueError("Evidence limits must be positive integers")
    if max_sources > 100 or max_depth > 4 or max_total_chars > 24_000:
        raise ValueError("Evidence limits exceed the bounded read budget")
    clean = redact or (lambda text: text)
    terms = set(re.findall(r"[\w-]{3,}", query.casefold()))
    plan = prompt_query_plan(query)
    topical_patterns = [re.compile(
        "|".join(r"(?<!\w)" + re.escape(variant) +
                 (r"[а-яё]*\b" if prompt_variant_prefix(term, stem, variant) else r"(?!\w)")
                 for variant in prompt_term_variants(term, stem)), re.I)
        for term, stem in (plan or {}).get("terms", [])]
    numbers = set(_NUMBER.findall(query))
    result_query = bool(_RESULT_QUERY.search(query))
    cache: dict[str, Mapping[str, Any] | None] = {}
    captures: dict[str, Mapping[str, Any] | None] = {}
    remaining = max_total_chars
    result = []
    for record in records:
        if _private_record(record):
            continue
        output = dict(record)
        output["source_evidence"] = []
        output["evidence_truncated"] = False
        result.append(output)
        if record.get("project") != project or record.get("superseded_by") or remaining <= 0:
            output["evidence_truncated"] = remaining <= 0
            continue
        root_id = record.get("id")
        if not isinstance(root_id, str):
            continue
        queue = [(source_id, [root_id], 1) for source_id in record.get("source_ids", [])
                 if isinstance(source_id, str)]
        seen = {root_id}
        candidates = []
        for_depth = 1
        while queue and for_depth <= max_depth:
            level, queue = queue, []
            missing = list(dict.fromkeys(i for i, _, _ in level if i not in cache and i not in seen))
            capacity = max(0, max_sources - len(cache))
            if len(missing) > capacity:
                output["evidence_truncated"] = True
            missing = missing[:capacity]
            if missing:
                loaded = load_records(missing)
                cache.update(dict.fromkeys(missing))
                for source in loaded:
                    if source.get("id") in missing and source.get("project") == project:
                        cache[source["id"]] = source
            for source_id, path, depth in level:
                if source_id in seen:
                    continue
                seen.add(source_id)
                source = cache.get(source_id)
                if (source is None or _private_record(source)
                        or (source.get("superseded_by") and source["superseded_by"] not in path)):
                    continue
                lineage = [*path, source_id]
                origin = str(source.get("source") or "")
                role = ("tool_record" if origin.startswith("hook:PostToolUse") else
                        "assistant_report" if origin.startswith("hook:Stop") else
                        "user_intent" if origin.startswith("hook:UserPromptSubmit") else "derived_note")
                text = str(source.get("body") or "")[:131_072]
                field, source_clipped = "body", False
                if role == "tool_record" and load_capture is not None:
                    if source_id not in captures:
                        captures[source_id] = load_capture(source_id)
                    capture = captures[source_id]
                    if (capture and capture.get("project") == project
                            and capture.get("entry_id") == source_id):
                        captured_text, source_clipped = _capture_text(capture)
                        if captured_text:
                            text, field = captured_text, "tool_response"
                text = clean(_safe_source_text(text))
                for position, piece in enumerate(re.split(r"\n+|(?<=[.!?])\s+(?=[A-ZА-Я])", text)):
                    piece = piece.strip()
                    if not piece:
                        continue
                    overlap = sum(term in piece.casefold() for term in terms)
                    topical_matches = [match for pattern in topical_patterns
                                       if (match := pattern.search(piece))]
                    numeric_overlap = len(numbers.intersection(_NUMBER.findall(piece)))
                    aggregate_match = _AGGREGATE.search(piece) if result_query and numeric_overlap else None
                    qualification_match = _QUALIFICATION.search(piece)
                    qualification = bool(qualification_match)
                    behavior = bool(_BEHAVIOR.search(piece))
                    if not (overlap or topical_matches or numeric_overlap or qualification or behavior):
                        continue
                    # Literal and translated forms represent the same topic;
                    # do not give same-language text a second lexical vote.
                    score = (5 * qualification + 2 * behavior
                             + max(min(overlap, 4), min(len(topical_matches), 4) * 2))
                    # Compact statements with several query concepts can carry
                    # the requested wording more directly than long prose.
                    if len(piece) <= 120 and len(topical_matches) >= 2:
                        score += min(len(topical_matches), 4)
                    # Result questions need the aggregate receipt, not merely
                    # a nearby historical warning. Exact numeric anchors also
                    # retain short counts without matching larger numbers.
                    score += min(numeric_overlap, 2) * (4 if result_query else 1)
                    if aggregate_match:
                        score += 8
                    piece_clipped = source_clipped
                    if len(piece) > max_excerpt_chars:
                        # Keep a tail qualification in a long JSON/log line,
                        # rather than clipping away the reason it was selected.
                        match = (aggregate_match or qualification_match or _BEHAVIOR.search(piece)
                                 or (topical_matches[0] if topical_matches else None))
                        start = max(0, match.start() - max_excerpt_chars // 3) if match else 0
                        piece = piece[start:start + max_excerpt_chars]
                        piece_clipped = True
                    candidates.append((score, position, {
                        "source_id": source_id, "source_path": lineage,
                        "source_role": role, "field": field,
                        "source_created_at": source.get("created_at"),
                        "untrusted": True, "verification": "not_assessed",
                        "text": piece, "truncated": piece_clipped,
                    }))
                children = source.get("source_ids") or []
                if children and depth == max_depth:
                    output["evidence_truncated"] = True
                elif depth < max_depth:
                    queue.extend((child, lineage, depth + 1) for child in children if isinstance(child, str))
            for_depth += 1
        candidates.sort(key=lambda item: (-item[0], len(item[2]["text"]), item[1]))
        record_remaining = min(max_record_chars, remaining)
        displayed = set()
        for _, _, evidence in candidates:
            dedupe_text = re.sub(r"^(?:# Subtest:\s*|(?:not )?ok \d+ -\s*)", "", evidence["text"])
            if dedupe_text in displayed:
                continue
            if len(output["source_evidence"]) >= max_excerpts or record_remaining <= 0:
                output["evidence_truncated"] = True
                break
            displayed.add(dedupe_text)
            count = min(len(evidence["text"]), max_excerpt_chars, record_remaining)
            evidence["truncated"] = evidence["truncated"] or count < len(evidence["text"])
            evidence["text"] = evidence["text"][:count]
            output["source_evidence"].append(evidence)
            output["evidence_truncated"] |= evidence["truncated"]
            record_remaining -= count
            remaining -= count
    return result
