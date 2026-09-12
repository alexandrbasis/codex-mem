"""Small retrieval previews for CLI/MCP; full records stay available by ID."""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any


_PREVIEW_FIELDS = (
    "id", "project", "title", "kind", "created_at", "session_id", "preview",
    "source", "provenance", "superseded_by", "superseded_at", "is_anchor",
    "score", "lexical_score", "semantic_score", "rrf_score",
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
