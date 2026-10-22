"""Pure bookkeeping for already admitted original quotations.

This module does not select candidates or interpret source text. Container
metadata groups only within an exact recorded qualifier context. A quote_ref
resolves exact quotation provenance in the source-only ID namespace. It never
inherits the source container's scope, history, custody, or relationship.
An association keeps its own qualifiers and derived text. Quotation sharing
does not establish truth, current applicability, closure, or task identity.
"""
from collections.abc import Mapping
import json
from typing import Any


_QUALIFIERS = ('scope', 'session_applicability', 'disposition',
               'is_requirement_claim', 'meaning', 'uncertainty',
               'context_episode_version', 'context_is_current_head',
               'selection_basis', 'relationship_to_query')


def source_qualifiers(record: Mapping[str, Any]) -> dict[str, Any]:
    """Preserve absence separately from every explicitly recorded value."""
    values = {key: record[key] for key in _QUALIFIERS if key in record}
    if record.get('lane') == 'intent_custody' or record.get('selection_basis') == 'unassociated_user_context':
        if 'source_role' in record:
            values['source_role'] = record['source_role']
    return values


def qualifier_key(record: Mapping[str, Any]) -> str:
    return json.dumps(dict(record), sort_keys=True, ensure_ascii=False)


class OriginalSourcePlan:
    """Pack cached whole nodes and register only their displayed originals."""
    def __init__(self, available: int) -> None:
        self.entries: list[tuple[str, dict[str, Any], str]] = []
        self.remaining = available
        self.nodes: list[str] = []
        self.container_count = 0

    def offer(self, node: str) -> bool:
        if not node or len(node) > self.remaining:
            return False
        self.nodes.append(node)
        self.remaining -= len(node)
        return True

    def add(self, pointer: Mapping[str, Any], qualifiers: Mapping[str, Any]) -> str:
        identifier = 's' + str(len(self.entries) + 1)
        self.entries.append((identifier, dict(pointer), qualifier_key(qualifiers)))
        return identifier

    def find(self, pointer: Mapping[str, Any], qualifiers: Mapping[str, Any], *,
             same_context: bool = True) -> tuple[str, bool] | None:
        context = qualifier_key(qualifiers)
        for identifier, shown, shown_context in self.entries:
            if (same_context and context != shown_context) or any(pointer.get(key) != shown.get(key)
                    for key in ('source_id', 'source_role', 'tool_use_id')):
                continue
            if (pointer.get('source_field') or 'body') != (shown.get('source_field') or 'body'):
                continue
            start, end, left, right = (pointer.get('start'), pointer.get('end'),
                                       shown.get('start'), shown.get('end'))
            if not all(type(value) is int for value in (start, end, left, right)):
                continue
            quote, original = pointer.get('quote_excerpt'), shown.get('quote_excerpt')
            if (not isinstance(quote, str) or not isinstance(original, str) or
                    pointer.get('quote_truncated') or shown.get('quote_truncated') or
                    not left <= start < end <= right or original[start-left:end-left] != quote):
                continue
            return identifier, start == left and end == right
        return None
