"""Pure bookkeeping for already admitted original quotations.

This module does not select candidates or interpret source text. Container
metadata groups only within an exact recorded qualifier context. A quote_ref
resolves exact quotation provenance in the source-only ID namespace. It never
inherits the source container's scope, history, custody, or relationship.
The on-wire quote_refs="provenance_only" declaration gives the same rule:
association qualifiers stay local to each association.
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
             same_context: bool = True) -> tuple[str, bool, Mapping[str, Any]] | None:
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
            return identifier, start == left and end == right, dict(shown)
        return None


def compact_selected_xml(xml: str, incoming: set[str], *, native_tools: set[str] | frozenset[str]) -> str:
    """Compact a selected wire without changing any association or quotation.

    Native completeness is factored only after both full canonical fields prove
    the same captured execution. Unknown item metadata stays explicit. This
    function never selects sources, resolves references, or removes qualifiers.
    """
    import re
    from xml.etree import ElementTree as ET

    def compact(match: re.Match[str]) -> str:
        node = match.group()
        root = ET.fromstring(node)
        execution = root.find('execution')
        pointers = root.findall('source')
        valid = (execution is not None and root.get('lane') == 'source_evidence' and
                 root.get('field_association') == 'original_execution_capture' and len(pointers) == 2 and
                 execution.get('tool_name', '').lower() in native_tools and
                 bool(execution.get('tool_use_id')) and bool(root.get('id')) and
                 execution.get('exit_field') in {'tool_response.exit_code', 'tool_response.exitCode',
                     'tool_response.returncode', 'tool_response.return_code'} and
                 re.fullmatch(r'-?\d+', execution.get('exit_code', '')) is not None)
        if valid:
            valid = all(pointer.get('source_role') == 'tool_record' and
                pointer.get('source_id') == root.get('id') and
                pointer.get('tool_use_id') == execution.get('tool_use_id') and pointer.get('start') == '0' and
                pointer.findtext('quote_excerpt') is not None and
                pointer.get('end') == str(len(pointer.findtext('quote_excerpt'))) and
                pointer.get('quote_truncated') == 'False' and
                pointer.get('excerpt_status') == 'complete_capture_field' for pointer in pointers)
            commands = [pointer for pointer in pointers if pointer.get('source_field') in
                        {'tool_input.cmd', 'tool_input.command'}]
            outputs = [pointer for pointer in pointers if pointer.get('source_field') in
                       {'tool_response.output', 'tool_response.stdout', 'tool_response.stderr'}]
            valid = (valid and len(commands) == len(outputs) == 1 and
                     commands[0].findtext('quote_excerpt') == execution.get('command'))
        if valid:
            node = node.replace(' field_association="original_execution_capture"',
                ' field_association="original_execution_capture" complete_capture_fields="true"', 1)
            node = re.sub(r'<source\b[^>]*>', lambda opening: opening.group().replace(
                ' quote_truncated="False"', '').replace(' excerpt_status="complete_capture_field"', ''), node)
            # Only these two literal item values duplicate the validated pair.
            opening, rest = node.split('>', 1)
            if root.get('source_role') == 'tool_record':
                opening = opening.replace(' source_role="tool_record"', '', 1)
            if root.get('signal_kind') == 'observed_execution_result':
                opening = opening.replace(' signal_kind="observed_execution_result"', '', 1)
            node = opening + '>' + rest
        identifier = root.get('id', '')
        if (root.get('lane') == 'source_excerpts' and re.fullmatch(r'selected-source-\d+', identifier)
                and identifier not in incoming):
            opening, rest = node.split('>', 1)
            opening = re.sub(r' id=("[^"]*"|\x27[^\x27]*\x27)', '', opening, count=1)
            node = opening + '>' + rest
        # Complete assistant-source intervals already encode this exact length.
        def compact_report(opening: re.Match[str]) -> str:
            text = opening.group()
            if text.endswith('/>'):
                return text
            pointer = ET.fromstring(text[:-1] + '/>')
            if pointer.get('source_role') != 'assistant_report' or pointer.get('excerpt_status') != 'complete_source':
                return text
            original = next((source for source in root.iter('source') if source.attrib == pointer.attrib), None)
            quote = original.findtext('quote_excerpt') if original is not None else None
            if (quote is not None and pointer.get('start') == '0' and
                    pointer.get('end') == pointer.get('source_chars') == str(len(quote)) and
                    pointer.get('quote_truncated') in {None, 'False'}):
                text = re.sub(r' source_chars=("[^"]*"|\x27[^\x27]*\x27)', '', text, count=1)
            return text
        node = re.sub(r'<source\b[^>]*>', compact_report, node)
        return node

    compacted = re.sub(r'<item\b[^>]*>.*?</item>|<item\b[^>]*/>', compact, xml, flags=re.S)
    assert len(compacted) <= len(xml)
    return compacted
