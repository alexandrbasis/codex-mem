"""Connect explicit writes and lifecycle capture to optional background work."""
from __future__ import annotations

from .config import automatic_capture_enabled, load_config, hooks_disabled


def enqueue_project(project, data_dir=None, *, retry_failed=False, wait_for_start=True, coalesce_seconds=0):
    config = load_config(data_dir)
    if hooks_disabled() or not config.get("service_enabled") or not automatic_capture_enabled(project, config):
        return {"status": "disabled"}
    from .service import enqueue, start_service
    enqueue_kwargs = {"retry_failed": retry_failed}
    if coalesce_seconds and config.get("processor_enabled"):
        enqueue_kwargs["coalesce_seconds"] = coalesce_seconds
    queued = enqueue(project, data_dir, **enqueue_kwargs)
    if queued.get("status") == "queued":
        try:
            service = start_service(data_dir) if wait_for_start else start_service(data_dir, startup_timeout=0)
        except Exception as exc:
            # The durable enqueue already succeeded. Keep that receipt even
            # when detached startup fails so a later worker can drain it.
            from .hook_diagnostics import trace_error
            trace_error("service_start_unavailable", exc)
            service = {"status": "failed", "code": "service_start_unavailable"}
        return {**queued, "service": service}
    return queued


def after_write(project, data_dir=None, *, wait_for_start=True, coalesce_seconds=0):
    """A queue failure must not turn an already committed note into an error."""
    try:
        queue_kwargs = {"wait_for_start": wait_for_start}
        if coalesce_seconds:
            queue_kwargs["coalesce_seconds"] = coalesce_seconds
        return enqueue_project(project, data_dir, **queue_kwargs)
    except Exception as exc:
        # Diagnostics retain safe exception classes and code locations, never
        # the exception text, which can contain paths or captured content.
        from .hook_diagnostics import trace_error
        trace_error("queue_unavailable", exc)
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
    # The established search runs first: its query/limit/filter validation and
    # lexical fallback remain authoritative. Project state only augments resume.
    from .retrieval import historical_query
    historical = historical_query(query)
    if historical:
        from .project_memory_query import plan_query
        try:
            historical = plan_query(query, project).historical
        except ValueError:
            historical = True
    if (intent == "resume" and not historical and
            all(value is None for value in (kinds, types, concepts, files))):
        from .project_memory_retrieval import build_project_brief
        brief = build_project_brief(store, project, query)
        if brief["enabled"]:
            baseline = result["results"]
            project_records = brief["records"]
            merged = []
            seen = set()
            # Keep both established search evidence and ledger state available.
            # An old open requirement can lead even when many new notes exist.
            for index in range(max(len(project_records), len(baseline))):
                for lane in (project_records, baseline):
                    if index < len(lane) and lane[index]["id"] not in seen:
                        merged.append(lane[index])
                        seen.add(lane[index]["id"])
                    if len(merged) >= limit:
                        break
                if len(merged) >= limit:
                    break
            result["results"] = merged
            result["project_memory"] = {
                "mode": brief["mode"], "coverage": brief["coverage"],
                "pending": brief["pending"], "revision": brief.get("revision"),
                "candidate_count": len(project_records),
                "included_count": sum(row.get("source") == "project_memory" for row in merged),
                "freshness": "not_verified",
            }
    from .freshness import freshness_snapshot
    result["freshness"] = freshness_snapshot(store, project)
    from . import __version__
    settings = load_config(store.data_dir)
    result["runtime"] = {
        "codex_mem_version": __version__,
        "source": "search_process_and_current_configuration",
        "jev_flags": {key: settings.get(key) for key in (
            "jev_filter_enabled", "jev_quality_enabled", "jev_retrieval_enabled")},
    }
    if not result["results"]:
        from .project_scopes import discover_project_scopes
        result["scope_discovery"] = discover_project_scopes(
            store, project, config=load_config(store.data_dir))
    return result
