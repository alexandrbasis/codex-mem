"""Discover explicit alternate scopes without searching or moving their contents."""
from __future__ import annotations

from collections.abc import Mapping, Sequence
import json
from pathlib import Path
import sqlite3
from typing import Any

from .config import automatic_capture_enabled, load_config
from .store import StoreError, project_key

MAX_CANDIDATE_PROJECTS = 128
MAX_METADATA_ENTRIES = 256
MAX_PARENT_DEPTH = 4
MAX_SCOPE_HINTS = 10

# Private markers cannot establish or expose an alternate scope. Captures
# suppressed by the private prompt gate never reach the database at all.
_PUBLIC_ENTRY = """e.superseded_by IS NULL
    AND COALESCE(e.source, '') NOT LIKE 'private%'
    AND NOT EXISTS (SELECT 1 FROM json_each(e.tags_json) tag
                    WHERE lower(CAST(tag.value AS TEXT)) LIKE 'private%')"""


def _bounded_integer(value: Any, name: str, maximum: int, *, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}")
    return value


def discover_project_scopes(
    store: Any,
    project: str | Path,
    *,
    config: Mapping[str, Any] | None = None,
    candidate_projects: Sequence[str | Path] | None = None,
    limit: int = 5,
    parent_depth: int = 2,
) -> dict[str, Any]:
    """Return bounded metadata hints, keeping the requested scope unchanged.

    A parent is suggested only when the exact scope has no retained public
    entries. A nonparent needs an absolute file attribution under the requested
    project in structured observation metadata. Neither proximity nor a project
    name in prose establishes an association. Every hint requires the caller to
    explicitly select the actual stored scope for a separate retrieval.

    An omitted candidate list uses a bounded inventory of existing project keys.
    Only config-authorized keys have their entry metadata inspected. No bodies,
    titles, file contents, model calls, config changes or Store writes are used.
    """
    limit = _bounded_integer(limit, 'limit', MAX_SCOPE_HINTS, minimum=1)
    parent_depth = _bounded_integer(parent_depth, 'parent_depth', MAX_PARENT_DEPTH)
    if candidate_projects is not None and (
            isinstance(candidate_projects, (str, bytes)) or not isinstance(candidate_projects, Sequence)
            or len(candidate_projects) > MAX_CANDIDATE_PROJECTS):
        raise ValueError('candidate_projects must contain at most 128 paths')
    requested = project_key(project)
    result: dict[str, Any] = {
        'requested_project': requested,
        'selected_project': requested,
        'status': 'unavailable',
        'exact_entry_count': None,
        'candidates': [],
        'discovery_limited': False,
    }
    settings = config if config is not None else load_config(store.data_dir)
    if not automatic_capture_enabled(requested, settings):
        result['status'] = 'scope_disabled'
        return result

    def allowed(path: str) -> bool:
        return automatic_capture_enabled(path, settings)

    def count(connection: sqlite3.Connection, scope: str) -> int:
        return store._read(lambda: connection.execute(
            f'SELECT COUNT(*) FROM entries e WHERE e.project=? AND {_PUBLIC_ENTRY}',
            (scope,),
        ).fetchone()[0])

    try:
        with store._lock:
            store._require_open()
            connection = store._connection
            exact_count = count(connection, requested)
            result['exact_entry_count'] = exact_count
            candidates: list[dict[str, Any]] = []
            parent_keys = []
            for parent in list(Path(requested).parents)[:parent_depth]:
                # A filesystem root or user home does not name an owning app.
                if parent == Path(parent.anchor) or parent == Path.home():
                    break
                key = str(parent)
                parent_keys.append(key)
                if exact_count == 0 and allowed(key):
                    retained = count(connection, key)
                    if retained:
                        candidates.append({'project': key, 'reason': 'parent_has_history',
                                           'evidence_count': retained, 'requires_explicit_selection': True})
            if candidate_projects is None:
                rows = store._read(lambda: connection.execute(
                    'SELECT DISTINCT project FROM entries ORDER BY project LIMIT ?',
                    (MAX_CANDIDATE_PROJECTS + 1,),
                ).fetchall())
                result['discovery_limited'] = len(rows) > MAX_CANDIDATE_PROJECTS
                inventory = [row[0] for row in rows[:MAX_CANDIDATE_PROJECTS]]
            else:
                inventory = [project_key(path) for path in candidate_projects]
            seen = {requested, *parent_keys}
            for scope in inventory:
                try:
                    if scope != project_key(scope):
                        continue
                except (ValueError, OSError, RuntimeError):
                    continue
                if scope in seen or not allowed(scope):
                    continue
                seen.add(scope)
                rows = store._read(lambda: connection.execute(
                    f'''SELECT m.observation_json FROM entries e
                        JOIN entry_metadata m ON m.entry_id=e.id
                        WHERE e.project=? AND {_PUBLIC_ENTRY}
                            AND m.observation_json IS NOT NULL
                        ORDER BY e.updated_at DESC, e.id LIMIT ?''',
                    (scope, MAX_METADATA_ENTRIES + 1),
                ).fetchall())
                if len(rows) > MAX_METADATA_ENTRIES:
                    result['discovery_limited'] = True
                evidence_count = 0
                for row in rows[:MAX_METADATA_ENTRIES]:
                    observation = json.loads(row[0])
                    if not isinstance(observation, dict):
                        continue
                    references = [*observation.get('files_read', []), *observation.get('files_modified', [])]
                    for reference in references:
                        if not isinstance(reference, str) or not Path(reference).is_absolute():
                            continue
                        target = Path(project_key(reference))
                        if target == Path(requested) or Path(requested) in target.parents:
                            evidence_count += 1
                            break
                if evidence_count:
                    candidates.append({'project': scope, 'reason': 'file_attribution',
                                       'evidence_count': evidence_count, 'requires_explicit_selection': True})
            result['candidates'] = candidates[:limit]
            result['discovery_limited'] = result['discovery_limited'] or len(candidates) > limit
            result['status'] = 'candidates_available' if candidates else ('exact_scope' if exact_count else 'empty_scope')
    except (StoreError, sqlite3.Error, OSError, ValueError, TypeError, RuntimeError, AttributeError):
        result.update(status='unavailable', exact_entry_count=None, candidates=[])
    return result
