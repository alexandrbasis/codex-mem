"""Strict test-side resolution of rendered original-source provenance.

Only source elements own IDs. References inherit quotation provenance, never
scope, workflow, custody or other association qualifiers from target wrappers.
"""
import re


def _bounds(node):
    values = [node.get(key) for key in ('start', 'end')]
    if any(value is None or re.fullmatch(r'[0-9]+', value) is None for value in values):
        raise AssertionError('source bounds must be explicit nonnegative integers')
    start, end = map(int, values)
    if not start < end:
        raise AssertionError('source interval must be nonempty and ordered')
    return start, end


class SourceRegistry:
    def __init__(self, root):
        self.sources = {}
        for source in root.iter('source'):
            identifier = source.get('id')
            if identifier is not None:
                if identifier in self.sources:
                    raise AssertionError('duplicate source-only ID: ' + identifier)
                self.sources[identifier] = source

    def resolve(self, pointer):
        if pointer.tag == 'source':
            source = pointer
        elif pointer.tag == 'quote_ref':
            source = self.sources.get(pointer.get('ref'))
            if source is None:
                raise AssertionError('missing source-only reference target')
        else:
            raise AssertionError('expected source or quote_ref')
        left, right = _bounds(source)
        quote = source.findtext('quote_excerpt')
        if quote is None or len(quote) != right - left or source.get('quote_truncated') not in (None, 'False'):
            raise AssertionError('target must contain its complete exact source interval')
        if not source.get('source_id'):
            raise AssertionError('source identity is required')
        start, end = left, right
        if pointer.tag == 'quote_ref':
            specified = [pointer.get(key) is not None for key in ('start', 'end')]
            if any(specified):
                if not all(specified):
                    raise AssertionError('reference must give both bounds or neither')
                start, end = _bounds(pointer)
            if not left <= start < end <= right:
                raise AssertionError('reference interval is outside its source target')
            for key in ('source_id', 'source_role', 'source_field', 'tool_use_id'):
                expected = source.get(key, 'body' if key == 'source_field' else None)
                if pointer.get(key) is not None and pointer.get(key) != expected:
                    raise AssertionError('reference changes original ' + key)
            if list(pointer):
                raise AssertionError('reference cannot supply a replacement quotation')
        return {'source_id': source.get('source_id'), 'source_role': source.get('source_role'),
                'source_field': source.get('source_field', 'body'), 'tool_use_id': source.get('tool_use_id'),
                'start': start, 'end': end, 'quote': quote[start-left:end-left],
                'quote_truncated': False, 'context_basis': pointer.get('context_basis')}

    def field_sources(self, field):
        return [self.resolve(child) for child in field if child.tag in ('source', 'quote_ref')]


def effective_capture_basis(root, item):
    return item.get('capture_order_basis') or root.get('capture_order_basis')


def omitted_field_count(root):
    return sum(int(item.get('fields_omitted', '0')) for item in root.iter('item')) + sum(
        int(marker.get('fields', '0')) for marker in root.findall('omitted'))
