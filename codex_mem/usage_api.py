"""Shared CLI/MCP boundary for cached reports and explicit bounded refreshes."""
from __future__ import annotations

from .cost_report import build_cost_report, resolve_period


def usage_refresh(data_dir=None, *, from_date=None, to_date=None, timezone="UTC",
                  project=None, session_id=None, codex_home=None,
                  max_files=32, max_bytes=8 * 1024 * 1024):
    period = resolve_period(from_date, to_date, timezone)
    if isinstance(max_files, bool) or not isinstance(max_files, int) or not 1 <= max_files <= 128:
        raise ValueError("max_files must be between 1 and 128")
    if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or not 1 <= max_bytes <= 8 * 1024 * 1024:
        raise ValueError("max_bytes must be between 1 and 8388608")
    from .usage import UsageCollector
    collector = UsageCollector(data_dir, codex_home=codex_home)
    try:
        result = collector.refresh_period(period["from"], period["to"],
                                          max_files=max_files, max_bytes=max_bytes,
                                          project=project, session_id=session_id)
    finally:
        collector.close()
    return {"period": period, **result}


def usage_report(data_dir=None, *, from_date=None, to_date=None, timezone="UTC",
                 project=None, session_id=None, group_by=("day", "project", "task", "agent", "model"),
                 refresh=False, codex_home=None, max_files=32, max_bytes=8 * 1024 * 1024):
    # Freeze the cutoff before optional collection so both phases describe the
    # same time window. The normal report path never constructs a writer.
    if (not isinstance(group_by, (tuple, list)) or not 1 <= len(group_by) <= 5
            or any(not isinstance(item, str) or item not in {"day", "project", "task", "agent", "model"} for item in group_by)
            or len(set(group_by)) != len(group_by)):
        raise ValueError("group_by must contain distinct supported dimensions")
    period = resolve_period(from_date, to_date, timezone)
    receipt = {"status": "not_requested"}
    if refresh:
        receipt = usage_refresh(data_dir, from_date=period["from"], to_date=period["to"],
                                timezone=timezone, project=project, session_id=session_id,
                                codex_home=codex_home, max_files=max_files, max_bytes=max_bytes)
    result = build_cost_report(data_dir, from_date=period["from"], to_date=period["to"],
                               timezone=timezone, project=project, session_id=session_id,
                               group_by=group_by, codex_home=codex_home)
    result["refresh"] = receipt
    return result
