"""Connect explicit writes and lifecycle capture to optional background work."""
from __future__ import annotations

from .config import automatic_capture_enabled, load_config, hooks_disabled


def enqueue_project(project, data_dir=None, *, retry_failed=False, wait_for_start=True):
    config = load_config(data_dir)
    if hooks_disabled() or not config.get("service_enabled") or not automatic_capture_enabled(project, config):
        return {"status": "disabled"}
    from .service import enqueue, start_service
    queued = enqueue(project, data_dir, retry_failed=retry_failed)
    if queued.get("status") == "queued":
        service = start_service(data_dir) if wait_for_start else start_service(data_dir, startup_timeout=0)
        return {**queued, "service": service}
    return queued


def after_write(project, data_dir=None, *, wait_for_start=True):
    """A queue failure must not turn an already committed note into an error."""
    try:
        return enqueue_project(project, data_dir, wait_for_start=wait_for_start)
    except Exception:
        return {"status": "failed", "code": "queue_unavailable"}


def index_project(project, data_dir=None, *, retry_failed=False):
    if not load_config(data_dir).get("semantic_enabled"):
        return {"status": "disabled", "indexed": 0, "pending": 0}
    from .semantic import index_pending
    result = index_pending(project, data_dir, retry_failed=retry_failed)
    if result.get("status") == "unavailable" and result.get("code") not in {
        "model_not_ready", "dependency_missing",
    }:
        # An optional component not installed yet is normal. A present but
        # invalid runtime/model must not be acknowledged as successfully drained.
        return {**result, "status": "failed"}
    return result


def search_memory(
    store,
    project,
    query,
    *,
    mode="auto",
    intent="lookup",
    limit=10,
    kinds=None,
    types=None,
    concepts=None,
    files=None,
):
    from .semantic import SemanticError, search
    enabled = load_config(store.data_dir).get("semantic_enabled")
    if not enabled and mode in {"semantic", "hybrid"}:
        raise SemanticError("semantic_disabled")
    search_kwargs = {
        "mode": "lexical" if not enabled else mode,
        "limit": limit,
        "kinds": kinds,
    }
    if intent != "lookup":
        search_kwargs["intent"] = intent
    # Omit new filter keywords when they are unused so older embedding/search
    # adapters remain callable during a rolling upgrade.
    if types is not None:
        search_kwargs["types"] = types
    if concepts is not None:
        search_kwargs["concepts"] = concepts
    if files is not None:
        search_kwargs["files"] = files
    result = search(store, project, query, **search_kwargs)
    if not enabled and mode == "auto":
        result.update(requested_mode="auto", fallback_reason="semantic_disabled")
    return result
