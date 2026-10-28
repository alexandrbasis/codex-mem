# Isolate malformed review responses to their batch

## Problem

The outcome review schema allowed combinations of applicability, scope and value that the local validator rejected. One malformed review then blocked all observation processing for its project. The dashboard displayed a retry date even when the project was blocked and no retry was scheduled.

## Decision

Keep the existing outcome decisions and the `summary-outcome-v5` wire format. Constrain the response schema to the combinations that the validator already accepts. Bind the allowed values to the availability of each relation's source item. Schema compliance does not establish that a model's judgment is correct.

Record fixed outcome rule codes and code-owned field names in the existing diagnostic receipt. Preserve older receipts. Do not retain source text, candidate text or model explanations in these diagnostics.

A timely response from the expected reviewer that fails typed response validation is a malformed model review. Fail that batch without saving its proposed notes or summary. Quarantine its source snapshot and continue independent fresh work. Do not retry the failed batch automatically.

Preparation errors, changed sources, missing execution identity, unavailable runtimes and unexpected internal exceptions remain blocking failures. The broad historical `summary_support_invalid` reason is not sufficient evidence to unblock a project.

Explicit recovery of a historical review block requires the exact current failed job and attempt, matching requested and actual execution identities, and a durable successful reviewer call with an invalid verdict from `summary-support-v6` or `summary-outcome-v5`. The receipt must belong to the final applicable review. An inconsistent later source receipt cannot authorize recovery. Recovery queues independent pending work. It preserves the failed job, its attempt count, source snapshot and diagnostics. It does not migrate all historical blocks or bypass another active blocker.

The dashboard shows a retry date only for a waiting retry. A blocked project says that no retry is scheduled. An earlier error on an unblocked project is labelled as a past error.

## Consequences

Malformed model responses no longer stop unrelated work within the same project. Strict validation and atomic storage remain in place. Runtime, provider, model, reasoning level, deadlines and call budgets remain unchanged.

Offline tests can establish schema consistency, queue isolation and exact recovery boundaries. They do not establish native provider acceptance or semantic accuracy. Existing native test results keep their original source and profile identities.

Implementation: [outcome review](../../codex_mem/summary_outcome.py), [diagnostics](../../codex_mem/summary_support.py), [processing](../../codex_mem/processor.py), [service](../../codex_mem/service.py), and [dashboard](../../codex_mem/dashboard_data.py).
