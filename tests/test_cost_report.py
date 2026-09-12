"""Period boundaries, read-only behavior, deduplication, and report additivity."""
import json
import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from codex_mem.cost_report import build_cost_report, build_report, resolve_period
from codex_mem.usage_store import UsageStore

def event(**changes):
    row = dict(model="gpt-6-astra", model_source="turn_context", service_tier="standard", service_tier_source="token_usage_record", input_tokens=1000, cached_input_tokens=800, cache_write_input_tokens=0, output_tokens=100, reasoning_output_tokens=50, total_tokens=1100)
    row.update(changes)
    return row


PERIOD = {"from_date": "2026-09-11", "to_date": "2026-09-13", "timezone": "Asia/Jerusalem"}


def response(identifier="r1", **changes):
    row = event(event_key=identifier, response_id=identifier, thread_id="main-thread", session_id="task", project="/project", source_kind="response", recorded_at="2026-09-11T21:00:00.000000Z")
    row.update(changes)
    return row


def observer(**changes):
    row = event(job_id="job", attempt_count=1, worker_thread_id="observer-worker", session_id="task", project="/project", model="gpt-5.6-luna", started_at="2026-09-11T22:00:00Z", finished_at="2026-09-11T22:00:10Z", usage_status="reported", outcome="processed", usage_updates=1)
    row.update(changes)
    return row


class CostReportTests(unittest.TestCase):
    def test_local_dates_and_exclusive_end_use_exact_microseconds(self):
        rows = [response("before", recorded_at="2026-09-10T20:59:59.999999Z"), response("start", recorded_at="2026-09-10T21:00:00Z"), response("last", recorded_at="2026-09-12T20:59:59.999999Z"), response("end", recorded_at="2026-09-12T21:00:00Z")]
        report = build_report(rows, **PERIOD)
        self.assertEqual(2, report["main"]["event_count"])
        self.assertEqual(["2026-09-11", "2026-09-12"], [row["value"] for row in report["groups"]["main"]["day"]["rows"]])

    def test_dst_local_calendar_day_is_not_always_24_hours(self):
        spring = resolve_period("2026-03-08", "2026-03-09", "America/New_York")
        fall = resolve_period("2026-11-01", "2026-11-02", "America/New_York")
        for period, expected in ((spring, 23), (fall, 25)):
            start = datetime.fromisoformat(period["from"].replace("Z", "+00:00"))
            end = datetime.fromisoformat(period["to"].replace("Z", "+00:00"))
            self.assertEqual(expected * 3600, (end - start).total_seconds())

    def test_default_is_yesterday_to_now_in_selected_zone(self):
        period = resolve_period(timezone="Asia/Jerusalem", now="2026-09-12T11:57:06Z")
        self.assertEqual("2026-09-10T21:00:00.000000Z", period["from"])
        self.assertEqual("2026-09-12T11:57:06.000000Z", period["to"])

    def test_invalid_dates_zones_and_naive_dst_times_are_rejected(self):
        for args in (("2026-09-13", "2026-09-12", "UTC"), ("2026-02-30", "2026-03-01", "UTC"), ("2026-11-01T01:30:00", "2026-11-02", "America/New_York"), (None, None, "bad/timezone")):
            with self.subTest(args=args), self.assertRaises(ValueError):
                resolve_period(*args)

    def test_per_response_long_context_pricing_is_not_applied_to_aggregate(self):
        rows = [response(str(i), input_tokens=200000, cached_input_tokens=0, total_tokens=200100) for i in range(2)]
        report = build_report(rows, **PERIOD)
        self.assertEqual(Decimal("4.01"), Decimal(report["main"]["api_equivalent_usd"]["total"]))

    def test_duplicates_and_legacy_fallback_do_not_double_charge(self):
        rows = [response(), response(event_key="different"), response("legacy", response_id=None, source_kind="legacy")]
        report = build_report(rows, **PERIOD)
        self.assertEqual(1, report["main"]["event_count"])
        self.assertEqual(1, report["completeness"]["duplicate_response_rows_removed"])
        self.assertEqual(1, report["completeness"]["legacy_events_replaced_by_native"])

    def test_observer_overlap_is_excluded_even_when_attempt_started_before_period(self):
        report = build_report([response(), response("worker", thread_id="observer-worker")], [observer(started_at="2026-09-10T20:00:00Z")], **PERIOD)
        self.assertEqual(1, report["main"]["event_count"])
        self.assertEqual(0, report["observer"]["event_count"])
        self.assertEqual(1, report["completeness"]["observer_overlap_events_excluded_from_main"])

    def test_observer_partial_cost_is_observed_only_not_complete_total(self):
        report = build_report([], [observer(usage_status="partial", outcome="lease_expired")], **PERIOD)
        cost = report["observer"]["api_equivalent_usd"]
        self.assertEqual(Decimal(".000176"), Decimal(cost["selected_subtotal"]))
        self.assertIsNone(cost["total"])
        self.assertEqual(1, report["observer"]["completeness"]["partial_usage_events"])

    def test_missing_observer_usage_is_not_silently_zero_priced(self):
        report = build_report([], [observer(usage_status="unavailable")], **PERIOD)
        self.assertIsNone(report["observer"]["api_equivalent_usd"]["total"])
        self.assertEqual(1, report["observer"]["api_equivalent_usd"]["scenario_unpriced_events"])

    def test_repeated_observer_snapshots_count_one_latest_attempt(self):
        earlier = observer(usage_status="partial", usage_updates=1)
        later = observer(usage_updates=2, input_tokens=2000, total_tokens=2100)
        report = build_report([], [earlier, later], **PERIOD)
        self.assertEqual(1, report["observer"]["event_count"])
        self.assertEqual(2000, report["observer"]["input_tokens"])

    def test_unknown_tier_scenarios_preserve_known_and_requested_tiers(self):
        rows = [response("confirmed"), response("requested", service_tier=None, requested_service_tier="fast"), response("unknown", service_tier=None)]
        cost = build_report(rows, **PERIOD)["main"]["api_equivalent_usd"]
        self.assertEqual(Decimal(".0234"), Decimal(cost["selected_subtotal"]))
        self.assertEqual(Decimal(".0312"), Decimal(cost["standard_scenario_subtotal"]))
        self.assertEqual(Decimal(".0390"), Decimal(cost["fast_scenario_subtotal"]))
        self.assertIsNone(cost["total"])

    def test_unknown_model_and_timestamp_have_explicit_coverage(self):
        report = build_report([response("unknown", model="codex-auto-review"), response("time", recorded_at=None)], **PERIOD)
        self.assertEqual({"codex-auto-review": 1}, report["main"]["unknown_models"])
        self.assertEqual(1, report["completeness"]["unknown_time_events_excluded"]["main"])
        self.assertIsNone(report["main"]["api_equivalent_usd"]["total"])

    def test_unknown_model_details_are_bounded_but_coverage_counts_all_models(self):
        report = build_report([response(str(i), model=f"unknown-{i}") for i in range(30)], **PERIOD)
        self.assertEqual(20, len(report["main"]["unknown_models"]))
        self.assertEqual(10, report["main"]["unknown_models_omitted_count"])
        self.assertEqual(30, report["main"]["completeness"]["unknown_model_events"])

    def test_groups_and_streams_add_up_and_group_cap_is_disclosed(self):
        rows = [response(str(i), project=f"/p{i}", session_id=f"task{i}", model="gpt-5.6-sol" if i % 2 else "gpt-6-astra") for i in range(5)]
        report = build_report(rows, [observer()], max_groups=2, **PERIOD)
        self.assertEqual(6, report["combined"]["event_count"])
        self.assertEqual(3, report["groups"]["main"]["project"]["omitted_groups"])
        subtotal = sum(Decimal(row["api_equivalent_usd"]["selected_subtotal"]) for row in report["groups"]["main"]["model"]["rows"])
        self.assertEqual(subtotal, Decimal(report["main"]["api_equivalent_usd"]["selected_subtotal"]))
        self.assertEqual(Decimal(report["combined"]["api_equivalent_usd"]["selected_subtotal"]), Decimal(report["main"]["api_equivalent_usd"]["selected_subtotal"]) + Decimal(report["observer"]["api_equivalent_usd"]["selected_subtotal"]))
        json.dumps(report)

    def test_project_and_task_scope_include_only_matching_rows(self):
        rows = [response(), response("other", session_id="other")]
        report = build_report(rows, [observer()], project="/project", session_id="task", **PERIOD)
        self.assertEqual(1, report["main"]["event_count"])
        self.assertEqual(1, report["observer"]["event_count"])

    def test_observer_child_session_maps_to_root_only_inside_matching_project(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "memory.sqlite3"
            with sqlite3.connect(path) as connection:
                connection.execute("CREATE TABLE usage_sessions(thread_id TEXT,session_id TEXT,project TEXT)")
                connection.execute("INSERT INTO usage_sessions VALUES('child','root','/project')")
                connection.execute("CREATE TABLE observation_jobs(id TEXT,project TEXT,model TEXT,session_id TEXT,reasoning_effort TEXT,attempt_count INTEGER)")
                connection.executemany("INSERT INTO observation_jobs VALUES(?,?,'gpt-5.6-luna',?,'low',2)", [("mapped", "/project", "child"), ("foreign", "/other", "child"), ("unmapped", "/project", "outside")])
                example = {key: value for key, value in observer().items() if key not in {"project", "session_id", "model"}}
                connection.execute("CREATE TABLE observer_usage_attempts(" + ",".join(f"{key} {'INTEGER' if isinstance(value,int) else 'TEXT'}" for key, value in example.items()) + ")")
                for job_id in ("mapped", "foreign", "unmapped"):
                    values = dict(example, job_id=job_id)
                    connection.execute("INSERT INTO observer_usage_attempts VALUES(" + ",".join("?" for _ in values) + ")", tuple(values.values()))
                connection.commit()
            root_report = build_cost_report(directory, project="/project", session_id="root", **PERIOD)
            self.assertEqual(1, root_report["observer"]["event_count"])
            task = root_report["groups"]["observer"]["task"]["rows"][0]
            self.assertEqual("root", task["value"])
            self.assertEqual(["child"], task["session_attribution"]["source_session_ids"])
            self.assertEqual({"usage_session_mapping": 1}, task["session_attribution"]["basis_counts"])
            self.assertEqual(1, root_report["completeness"]["collection"]["historical_attempts_without_receipts_in_scope_all_dates"])
            foreign = build_cost_report(directory, project="/other", session_id="root", **PERIOD)
            self.assertEqual(0, foreign["observer"]["event_count"])
            unmapped = build_cost_report(directory, project="/project", session_id="outside", **PERIOD)
            self.assertEqual(1, unmapped["observer"]["completeness"]["observer_session_unmapped_attempts"])
            self.assertEqual("outside", unmapped["groups"]["observer"]["task"]["rows"][0]["value"])

    def test_missing_database_is_not_created_by_reporting(self):
        with tempfile.TemporaryDirectory() as directory:
            report = build_cost_report(directory, **PERIOD)
            self.assertEqual("database_missing", report["completeness"]["collection"]["status"])
            self.assertIsNone(report["main"]["api_equivalent_usd"]["total"])
            self.assertEqual([], list(Path(directory).iterdir()))

    def test_old_writer_variable_precision_and_offsets_work_after_schema_migration(self):
        with tempfile.TemporaryDirectory() as directory:
            with UsageStore(directory) as ledger:
                row = response(recorded_at="2026-09-12T00:00:00.123Z")
                ledger.commit_scan("fixture", None, {"offset": 0, "parser_state": {}}, {"thread_id": "main-thread", "session_id": "task", "project": directory}, [row])
                for timestamp in ("2026-09-12T00:00:00.123Z", "2026-09-12T03:00:00.123+03:00"):
                    ledger.store._connection.execute("UPDATE usage_events SET recorded_at=?", (timestamp,))
                    ledger.store._connection.commit()
                    report = build_cost_report(directory, from_date="2026-09-12T00:00:00.123000Z", to_date="2026-09-12T00:00:00.123456Z")
                    self.assertEqual(1, report["main"]["event_count"])
                details = [row[3] for row in ledger.store._connection.execute("EXPLAIN QUERY PLAN SELECT * FROM usage_events WHERE julianday(recorded_at)>=julianday(?) AND julianday(recorded_at)<julianday(?)", ("2026-09-11", "2026-09-13"))]
                self.assertTrue(any("usage_recorded_time" in detail for detail in details), details)

    def test_read_only_sqlite_report_and_old_tier_are_not_provider_confirmation(self):
        with tempfile.TemporaryDirectory() as directory:
            # Build the old schema directly. The reader must not migrate it.
            path = Path(directory) / "memory.sqlite3"
            with sqlite3.connect(path) as connection:
                connection.execute("CREATE TABLE usage_sessions(thread_id TEXT,project TEXT,parent_thread_id TEXT,agent_path TEXT,agent_role TEXT,agent_nickname TEXT)")
                connection.execute("INSERT INTO usage_sessions VALUES('main-thread','/project',NULL,NULL,NULL,NULL)")
                row = response()
                row.pop("service_tier_source")
                connection.execute("CREATE TABLE usage_events(" + ",".join(f"{key} {'INTEGER' if isinstance(value,int) else 'TEXT'}" for key, value in row.items() if key != "project") + ")")
                values = {key: value for key, value in row.items() if key != "project"}
                connection.execute("INSERT INTO usage_events VALUES(" + ",".join("?" for _ in values) + ")", tuple(values.values()))
                connection.commit()
            before = path.read_bytes()
            report = build_cost_report(directory, **PERIOD)
            self.assertEqual(1, report["main"]["event_count"])
            self.assertEqual(1, report["main"]["completeness"]["requested_tier_events"])
            self.assertEqual(0, report["main"]["completeness"]["confirmed_tier_events"])
            self.assertEqual(before, path.read_bytes())


if __name__ == "__main__":
    unittest.main()
