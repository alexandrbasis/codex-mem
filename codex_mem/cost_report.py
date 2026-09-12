"""Read-only period reports with explicit pricing and collection gaps."""
from __future__ import annotations

import sqlite3
from collections import Counter
from contextlib import closing
from datetime import date, datetime, timedelta, timezone as datetime_timezone
from decimal import Decimal, localcontext
from pathlib import Path
from typing import Any, Iterable, Mapping
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .config import data_dir_path
from .pricing import TOKEN_FIELDS, decimal_text, has_partial_counters, price_event, snapshot_metadata
from .store import project_key

UTC = datetime_timezone.utc
DIMENSIONS = ("day", "project", "task", "agent", "model")
METRICS = ("api_equivalent_usd", "estimated_codex_credits")


def _timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return result.astimezone(UTC) if result.tzinfo is not None else None
    except (ValueError, OverflowError):
        return None


def _utc_text(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def resolve_period(from_date: str | None = None, to_date: str | None = None, timezone: str = "UTC", now: datetime | str | None = None) -> dict[str, Any]:
    """Resolve local calendar dates or explicit-offset instants to [from, to).

    The default is yesterday at local midnight through now. Explicit end dates
    are exclusive, so 2026-09-11 to 2026-09-13 covers two complete local days.
    Naive date-times are rejected rather than guessing during DST transitions.
    """
    try:
        zone = ZoneInfo(timezone)
    except (ZoneInfoNotFoundError, ValueError, TypeError):
        raise ValueError("timezone must be an IANA time zone") from None
    current = _timestamp(now) if isinstance(now, str) else now or datetime.now(UTC)
    if not isinstance(current, datetime) or current.tzinfo is None:
        raise ValueError("now must include a UTC offset")
    current = current.astimezone(UTC)

    def boundary(value: str | None, default: datetime) -> datetime:
        if value is None:
            return default
        if not isinstance(value, str):
            raise ValueError("period boundaries must be ISO dates or offset date-times")
        try:
            if len(value) == 10:
                day = date.fromisoformat(value)
                midnight = datetime.combine(day, datetime.min.time(), zone)
                # Some zones skip midnight. Reject a non-existent boundary.
                if midnight.astimezone(UTC).astimezone(zone).replace(tzinfo=None) != midnight.replace(tzinfo=None):
                    raise ValueError
                return midnight.astimezone(UTC)
            parsed = _timestamp(value)
            if parsed is not None:
                return parsed
        except ValueError:
            pass
        raise ValueError("period boundaries must be valid ISO dates or offset date-times")

    yesterday = current.astimezone(zone).date() - timedelta(days=1)
    start = boundary(from_date, datetime.combine(yesterday, datetime.min.time(), zone).astimezone(UTC))
    end = boundary(to_date, current)
    if start >= end:
        raise ValueError("from_date must be before exclusive to_date")
    return {
        "from": _utc_text(start), "to": _utc_text(end), "timezone": timezone,
        "local_from": start.astimezone(zone).isoformat(), "local_to": end.astimezone(zone).isoformat(),
        "interval": "[from,to)", "default_to_now": to_date is None,
        "observer_attribution": "entire_attempt_assigned_to_started_at",
    }


class _Accumulator:
    def __init__(self) -> None:
        self.events = 0
        self.first_at: datetime | None = None
        self.last_at: datetime | None = None
        self.tokens = Counter({name: 0 for name in TOKEN_FIELDS})
        self.gaps = Counter({name: 0 for name in (
            "invalid_or_unavailable_usage_events", "unknown_tier_events",
            "requested_tier_events", "confirmed_tier_events", "legacy_usage_events",
            "partial_usage_events", "partial_counter_events", "running_attempts",
            "observed_only_events", "model_not_provider_confirmed_events", "unknown_model_events",
            "observer_session_unmapped_attempts",
        )})
        self.models = Counter()
        self.observer_source_sessions = Counter()
        self.observer_session_bases = Counter()
        self.money = {name: {"confirmed_tier_subtotal": Decimal(0), "requested_tier_subtotal": Decimal(0), "standard_scenario_subtotal": Decimal(0), "fast_scenario_subtotal": Decimal(0)} for name in METRICS}
        self.metric_counts = {name: Counter() for name in METRICS}

    def add(self, row: Mapping[str, Any], priced: Mapping[str, Any], moment: datetime) -> None:
        self.events += 1
        self.first_at = min(self.first_at, moment) if self.first_at else moment
        self.last_at = max(self.last_at, moment) if self.last_at else moment
        if priced["tokens"] is None:
            self.gaps["invalid_or_unavailable_usage_events"] += 1
        else:
            self.tokens.update(priced["tokens"])
        self.gaps["unknown_tier_events"] += priced["tier"]["status"] == "unknown"
        self.gaps["requested_tier_events"] += priced["tier"]["status"] == "requested"
        self.gaps["confirmed_tier_events"] += priced["tier"]["status"] == "confirmed"
        self.gaps["legacy_usage_events"] += row.get("source_kind") == "legacy"
        self.gaps["partial_usage_events"] += row.get("usage_status") == "partial"
        self.gaps["partial_counter_events"] += has_partial_counters(row)
        self.gaps["running_attempts"] += row.get("outcome") == "running"
        self.gaps["observed_only_events"] += row.get("usage_status") == "partial" or row.get("outcome") == "running"
        if row.get("_observer_attempt"):
            basis = row.get("session_attribution_basis") or "unmapped_source_session"
            self.observer_session_bases[basis] += 1
            self.gaps["observer_session_unmapped_attempts"] += basis == "unmapped_source_session"
            if row.get("source_session_id"):
                self.observer_source_sessions[row["source_session_id"]] += 1
        self.gaps["model_not_provider_confirmed_events"] += row.get("model_source") not in {"response", "provider", "token_usage_record"}
        self.gaps["unknown_model_events"] += not priced["model_rate_known"]
        if not priced["model_rate_known"]:
            self.models[str(row.get("model") or "unknown")] += 1
        for name in METRICS:
            amount, counters, money = priced[name], self.metric_counts[name], self.money[name]
            selected = amount["selected"]
            if selected is not None:
                counters["selected_events"] += 1
                key = "confirmed_tier_subtotal" if priced["tier"]["status"] == "confirmed" else "requested_tier_subtotal"
                money[key] += Decimal(selected)
                money["standard_scenario_subtotal"] += Decimal(selected)
                money["fast_scenario_subtotal"] += Decimal(selected)
            else:
                counters["unpriced_events"] += 1
                if amount["standard"] is not None and amount["fast"] is not None:
                    money["standard_scenario_subtotal"] += Decimal(amount["standard"])
                    money["fast_scenario_subtotal"] += Decimal(amount["fast"])
                    counters["scenario_only_events"] += 1
                else:
                    counters["scenario_unpriced_events"] += 1

    def result(self) -> dict[str, Any]:
        unknown_models = sorted(self.models.items(), key=lambda item: (-item[1], item[0]))
        result: dict[str, Any] = {"event_count": self.events, "observed_from": _utc_text(self.first_at) if self.first_at else None, "observed_through": _utc_text(self.last_at) if self.last_at else None, **self.tokens, "completeness": dict(self.gaps), "unknown_models": dict(unknown_models[:20]), "unknown_models_omitted_count": max(0, len(unknown_models) - 20)}
        for name in METRICS:
            money, counts = self.money[name], self.metric_counts[name]
            selected = money["confirmed_tier_subtotal"] + money["requested_tier_subtotal"]
            result[name] = {
                **{key: decimal_text(value) for key, value in money.items()},
                "selected_subtotal": decimal_text(selected),
                "total": decimal_text(selected) if counts["unpriced_events"] == 0 and not (self.gaps["partial_usage_events"] or self.gaps["running_attempts"]) else None,
                "observed_only_events": self.gaps["observed_only_events"],
                "selected_events": counts["selected_events"], "unpriced_events": counts["unpriced_events"],
                "scenario_only_events": counts["scenario_only_events"], "scenario_unpriced_events": counts["scenario_unpriced_events"],
            }
        if self.observer_session_bases:
            source_sessions = sorted(self.observer_source_sessions)
            result["session_attribution"] = {"basis_counts": dict(self.observer_session_bases), "source_session_ids": source_sessions[:20], "source_session_ids_omitted_count": max(0, len(source_sessions) - 20)}
        return result


def _group_row(value: Any, accumulator: _Accumulator, dimension: str) -> dict[str, Any]:
    """Keep breakdowns compact; full confidence details remain in totals."""
    detailed = accumulator.result()
    result = {"value": value, "event_count": detailed["event_count"], **{field: detailed[field] for field in TOKEN_FIELDS}}
    amount_fields = ("selected_subtotal", "total", "standard_scenario_subtotal", "fast_scenario_subtotal", "unpriced_events", "scenario_unpriced_events")
    for name in METRICS:
        result[name] = {field: detailed[name][field] for field in amount_fields}
    if dimension == "task" and "session_attribution" in detailed:
        result["session_attribution"] = detailed["session_attribution"]
    return result


def _dedupe(rows: Iterable[Mapping[str, Any]], observer: bool = False) -> tuple[list[dict[str, Any]], int]:
    unique: dict[Any, dict[str, Any]] = {}
    duplicates = 0
    for index, value in enumerate(rows):
        row = dict(value)
        if observer and row.get("job_id") is not None and row.get("attempt_count") is not None:
            key = (row["job_id"], row["attempt_count"])
        elif not observer and row.get("response_id") and row.get("thread_id"):
            key = (row["thread_id"], row["response_id"])
        else:
            key = row.get("event_key") or ("unidentified", index)
        if key in unique:
            duplicates += 1
            if observer:
                old = unique[key]
                if (row.get("usage_updates") or 0, row.get("finished_at") or "") >= (old.get("usage_updates") or 0, old.get("finished_at") or ""):
                    unique[key] = row
            continue
        unique[key] = row
    return list(unique.values()), duplicates


def build_report(events: Iterable[Mapping[str, Any]], observer_attempts: Iterable[Mapping[str, Any]] = (), *, from_date: str | None = None, to_date: str | None = None, timezone: str = "UTC", now: datetime | str | None = None, project: str | None = None, session_id: str | None = None, group_by: Iterable[str] = DIMENSIONS, max_groups: int = 100, observer_thread_ids: Iterable[str] = (), coverage: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Build JSON-safe aggregates from observed metadata without modifying it."""
    period = resolve_period(from_date, to_date, timezone, now)
    dimensions = tuple(dict.fromkeys(group_by))
    if any(dimension not in DIMENSIONS for dimension in dimensions):
        raise ValueError("group_by supports day, project, task, agent, model")
    if isinstance(max_groups, bool) or not isinstance(max_groups, int) or not 1 <= max_groups <= 1000:
        raise ValueError("max_groups must be between 1 and 1000")
    start, end, zone = _timestamp(period["from"]), _timestamp(period["to"]), ZoneInfo(timezone)
    main_rows, duplicate_events = _dedupe(events)
    observer_rows, duplicate_attempts = _dedupe(observer_attempts, observer=True)
    workers = set(observer_thread_ids) | {row["worker_thread_id"] for row in observer_rows if row.get("worker_thread_id")}
    native_threads = {row.get("thread_id") for row in main_rows if row.get("source_kind") == "response"}
    overlap, dropped_legacy, unknown_time = 0, 0, Counter()
    totals = {stream: _Accumulator() for stream in ("main", "observer", "combined")}
    grouped: dict[str, dict[str, dict[Any, _Accumulator]]] = {stream: {dimension: {} for dimension in dimensions} for stream in ("main", "observer")}

    with localcontext() as context:
        context.prec = 50
        for stream, rows in (("main", main_rows), ("observer", observer_rows)):
            for original in rows:
                row = dict(original)
                if project is not None and row.get("project") != project:
                    continue
                if session_id is not None and row.get("session_id") != session_id:
                    continue
                moment = _timestamp(row.get("started_at") if stream == "observer" else row.get("recorded_at"))
                if moment is None:
                    unknown_time[stream] += 1
                    continue
                if not start <= moment < end:
                    continue
                if stream == "main" and row.get("thread_id") in workers:
                    overlap += 1
                    continue
                if stream == "main" and row.get("source_kind") == "legacy" and row.get("thread_id") in native_threads:
                    dropped_legacy += 1
                    continue
                if stream == "observer":
                    row["_observer_attempt"] = True
                    row.setdefault("source_session_id", row.get("session_id"))
                    row["model_source"] = row.get("model_source") or "requested_job_profile"
                    if row.get("usage_status") not in {"reported", "partial"}:
                        for counter in TOKEN_FIELDS:
                            row[counter] = None
                priced = price_event(row)
                totals[stream].add(row, priced, moment)
                totals["combined"].add(row, priced, moment)
                values = {"day": moment.astimezone(zone).date().isoformat(), "project": row.get("project"), "task": row.get("session_id"), "agent": row.get("worker_thread_id") if stream == "observer" else row.get("thread_id"), "model": row.get("model")}
                for dimension in dimensions:
                    accumulator = grouped[stream][dimension].setdefault(values[dimension], _Accumulator())
                    accumulator.add(row, priced, moment)

        output_groups = {}
        for stream, dimension_groups in grouped.items():
            output_groups[stream] = {}
            for dimension, values in dimension_groups.items():
                rows = [_group_row(value, accumulator, dimension) for value, accumulator in values.items()]
                if dimension == "day":
                    rows.sort(key=lambda row: str(row["value"] or ""))
                else:
                    rows.sort(key=lambda row: (-Decimal(row["api_equivalent_usd"]["standard_scenario_subtotal"]), str(row["value"] or "")))
                output_groups[stream][dimension] = {"rows": rows[:max_groups], "total_groups": len(rows), "omitted_groups": max(0, len(rows) - max_groups), "limit": max_groups}

        result = {
            "schema_version": 1, "period": period, "scope": {"project": project, "session_id": session_id, "source": "local_usage_ledger"},
            "pricing": snapshot_metadata(), **{name: value.result() for name, value in totals.items()}, "groups": output_groups,
            "actual_billing": {"status": "unavailable", "amount": None, "reason": "Local usage logs do not contain invoices, purchased credits, or subscription allocation."},
            "completeness": {"duplicate_response_rows_removed": duplicate_events, "duplicate_attempt_rows_removed": duplicate_attempts, "observer_overlap_events_excluded_from_main": overlap, "legacy_events_replaced_by_native": dropped_legacy, "unknown_time_events_excluded": {"main": unknown_time["main"], "observer": unknown_time["observer"]}, "collection": dict(coverage or {"status": "not_supplied"})},
            "refresh": {"status": "not_requested"},
            "caveats": [
                "USD is an API-equivalent estimate. Codex credits are a separate public-rate estimate. Neither is an actual charge.",
                "Known subtotals cover only priced observations. Null totals and nonzero unpriced counts mean missing amounts, not zero cost.",
                "Standard/Fast scenario subtotals keep known or requested tiers and change only unknown tiers. They are hypothetical alternatives, not guaranteed billing bounds.",
                "Partial and running observer receipts include only observed tokens; their unrecorded remainder is unknown. Historical attempts without a receipt cannot be assigned a token cost.",
                "Observer attempts are assigned in full to their start time, including attempts crossing the period boundary. Worker responses are excluded from main usage by thread identity.",
                "Legacy usage rows lack precise response boundaries; their context-based pricing is an estimate.",
            ],
        }
    return result


def _columns(connection: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}


def build_cost_report(data_dir: str | Path | None = None, *, from_date: str | None = None, to_date: str | None = None, timezone: str = "UTC", now: datetime | str | None = None, project: str | Path | None = None, session_id: str | None = None, group_by: Iterable[str] = DIMENSIONS, max_groups: int = 100, codex_home: str | Path | None = None) -> dict[str, Any]:
    """Read a consistent SQLite snapshot. Does not initialize or migrate it."""
    period = resolve_period(from_date, to_date, timezone, now)
    workspace = project_key(project) if project is not None else None
    path = data_dir_path(data_dir) / "memory.sqlite3"
    rows, attempts, workers = [], [], []
    coverage: dict[str, Any] = {"status": "database_missing", "collection_complete": False}
    if path.is_file():
        with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=2)) as connection:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA query_only=ON")
            connection.execute("BEGIN")
            tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            coverage = {"status": "available", "collection_complete": False, "usage_ledger_available": {"usage_events", "usage_sessions"} <= tables, "observer_ledger_available": {"observer_usage_attempts", "observation_jobs"} <= tables}
            filters, args = [], []
            if workspace is not None:
                filters.append("s.project=?")
                args.append(workspace)
            if session_id is not None:
                filters.append("e.session_id=?")
                args.append(session_id)
            if {"usage_events", "usage_sessions"} <= tables:
                columns = _columns(connection, "usage_events")
                # Even a migrated database can receive a final old-daemon
                # write with a different offset or fractional precision.
                # Use the expression index, with a guard for SQLite's
                # millisecond rounding, then compare exact Python instants.
                time_filters = ["julianday(e.recorded_at)>=julianday(?)", "julianday(e.recorded_at)<julianday(?)"]
                bounds = [_utc_text(_timestamp(period["from"]) - timedelta(seconds=1)), _utc_text(_timestamp(period["to"]) + timedelta(seconds=1))]
                predicate = " AND ".join([*time_filters, *filters])
                query = f"SELECT e.*,s.project,s.parent_thread_id,s.agent_path,s.agent_role,s.agent_nickname FROM usage_events e JOIN usage_sessions s ON s.thread_id=e.thread_id WHERE {predicate}"
                rows = [dict(row) for row in connection.execute(query, [*bounds, *args])]
                missing_time = " AND ".join(["julianday(e.recorded_at) IS NULL", *filters])
                coverage["unknown_timestamp_rows_in_scope"] = connection.execute(f"SELECT count(*) FROM usage_events e JOIN usage_sessions s ON s.thread_id=e.thread_id WHERE {missing_time}", args).fetchone()[0]
                if "requested_service_tier" not in columns:
                    for row in rows:
                        row["requested_service_tier"] = row.pop("service_tier", None)
                        row["requested_service_tier_source"] = "legacy_usage_schema"
                coverage["legacy_tier_schema"] = "requested_service_tier" not in columns
            if {"observer_usage_attempts", "observation_jobs"} <= tables:
                can_map_sessions = "usage_sessions" in tables and {"thread_id", "project", "session_id"} <= _columns(connection, "usage_sessions")
                session_join = " LEFT JOIN usage_sessions source_session ON source_session.thread_id=j.session_id AND source_session.project=j.project" if can_map_sessions else ""
                observer_session = "COALESCE(source_session.session_id,j.session_id)" if can_map_sessions else "j.session_id"
                attribution_basis = "CASE WHEN source_session.thread_id IS NOT NULL THEN 'usage_session_mapping' ELSE 'unmapped_source_session' END" if can_map_sessions else "'unmapped_source_session'"
                predicate = ["julianday(a.started_at)>=julianday(?)", "julianday(a.started_at)<julianday(?)"]
                attempt_args = [_utc_text(_timestamp(period["from"]) - timedelta(seconds=1)), _utc_text(_timestamp(period["to"]) + timedelta(seconds=1))]
                if workspace is not None:
                    predicate.append("j.project=?")
                    attempt_args.append(workspace)
                if session_id is not None:
                    predicate.append(f"{observer_session}=?")
                    attempt_args.append(session_id)
                query = f"SELECT a.*,j.project,j.model,{observer_session} AS session_id,j.session_id AS source_session_id,{attribution_basis} AS session_attribution_basis,j.reasoning_effort FROM observer_usage_attempts a JOIN observation_jobs j ON j.id=a.job_id{session_join} WHERE " + " AND ".join(predicate)
                attempts = [dict(row) for row in connection.execute(query, attempt_args)]
                # Across all dates: a worker that started yesterday can emit a
                # response today. Never count that response as foreground work.
                workers = [row[0] for row in connection.execute("SELECT DISTINCT worker_thread_id FROM observer_usage_attempts WHERE worker_thread_id IS NOT NULL")]
                coverage["observer_first_receipt_at"] = connection.execute("SELECT MIN(started_at) FROM observer_usage_attempts").fetchone()[0]
                # Old jobs have only their last update time. These indicate a
                # gap but are not assigned to a date as if it were call time.
                job_predicate = ["j.attempt_count>0"]
                job_args = []
                if workspace is not None:
                    job_predicate.append("j.project=?")
                    job_args.append(workspace)
                if session_id is not None:
                    job_predicate.append(f"{observer_session}=?")
                    job_args.append(session_id)
                gap_query = "SELECT COALESCE(SUM(MAX(0,j.attempt_count-COALESCE(a.receipts,0))),0) FROM observation_jobs j LEFT JOIN (SELECT job_id,COUNT(*) receipts FROM observer_usage_attempts GROUP BY job_id) a ON a.job_id=j.id" + session_join + " WHERE " + " AND ".join(job_predicate)
                coverage["historical_attempts_without_receipts_in_scope_all_dates"] = connection.execute(gap_query, job_args).fetchone()[0]
            connection.rollback()
        # Optional metadata-only helper provided by the collection module.
        from . import usage
        if hasattr(usage, "coverage_period"):
            coverage["files"] = usage.coverage_period(data_dir, start_at=period["from"], end_at=period["to"], project=workspace, session_id=session_id, codex_home=codex_home)
    report = build_report(rows, attempts, from_date=period["from"], to_date=period["to"], timezone=timezone, project=workspace, session_id=session_id, group_by=group_by, max_groups=max_groups, observer_thread_ids=workers, coverage=coverage)
    report["period"] = period
    # A missing ledger means no observations are available, not zero spending.
    for stream, available in (("main", coverage.get("usage_ledger_available", False)), ("observer", coverage.get("observer_ledger_available", False))):
        if not available:
            for metric in METRICS:
                report[stream][metric]["total"] = None
                report["combined"][metric]["total"] = None
    return report
