# Verification

This page records the public evidence boundary for Codex Mem `1.2.1`. The public patch carries the tested `1.2.0` behavior; its branding, metadata, documentation, and packaging additions do not extend that behavior or compatibility boundary. Unit and local acceptance checks were rerun on the public export. Native E5, Luna, MCP, queue, and paired-flow checks below are the `1.2.0` behavioral baseline carried forward unchanged. The record describes tested scenarios and their limits; it is not a universal compatibility or retrieval-quality claim.

## Version and host boundary

- Tested implementation snapshot: `1.2.0`.
- Public release line: `1.2.1`.
- Native Codex integration boundary: Codex CLI `0.153.4`.
- Public export checks: 143 unit tests and the local synthetic acceptance driver passed.
- Native behavioral baseline: E5, Luna/medium, MCP, queue, capture, paired recall, and exclusion checks from the tested `1.2.0` snapshot.
- The evidence uses temporary fictional fixtures for native reads and automatic flows. It does not validate or publish a user's existing memory.
- Remote CI results are not part of this verification record.

## Recorded checks

The public export and its carried-forward behavioral baseline recorded the following results:

| Area | Result | Evidence scope |
| --- | --- | --- |
| Python unit suite | Passed, 143 tests | Public `1.2.1` export. Store, configuration, hooks, MCP, processor, semantic layer, service, importer, installer, and CLI behavior. |
| Local acceptance | Passed | Public `1.2.1` export. Nine synthetic hook invocations, no user-history reads, capture, deduplication, redaction, project isolation, MCP subprocess behavior, consolidation provenance, repeated compaction context, bounded input handling, backup, and forget. Official hook-schema validation was not enabled for this invocation. |
| Native MCP read | Passed | `1.2.0` behavioral baseline. A read-only native Codex call against a new fictional fixture. |
| Native semantic MCP read | Passed | `1.2.0` behavioral baseline. A local E5-base index and semantic search/read against a new fictional fixture. |
| Semantic acceptance | Passed | `1.2.0` behavioral baseline. Three fixed cross-language/paraphrase cases plus index lifecycle, project isolation, deletion, supersession, reindexing, reopen, and tokenizer-tail checks. |
| Observation processor | Passed | `1.2.0` behavioral baseline. A fresh `gpt-5.6-luna` / `medium` processing batch with source preservation and idempotence checks. |
| Local queue service | Passed | `1.2.0` behavioral baseline. Single-worker concurrency, queue drain, local embeddings, no AI calls for explicit fixture notes, durable enqueue, restart/resume, and cooperative stop. |
| Native capture, paired recall, and exclusion | Passed for the current fixture | `1.2.0` behavioral baseline. Automatic capture once per prompt/final answer, separate Luna/medium processing with complete source coverage, paired recall without the expected value in the recall prompt, and zero writes/jobs for an excluded project. |

The native capture and paired-flow result covers the current `SessionStart`, `UserPromptSubmit`, and `Stop` lifecycle exercise. `PostToolUse` and `PreCompact` have schema, unit, and isolated acceptance coverage; this record does not claim a current live native exercise for those two events.

## Known partial result

One older contradictory recall fixture remains partial or undetermined because accumulated historical notes did not produce consistent recall. That result stays partial in the evidence record and is not relabeled as passed. Later paired capture/recall and exclusion scenarios passed within their stated fictional fixture scope.

The E5 comparison also covered a small fixed set of English/Russian paraphrase cases. It supports the selected local profile for those cases; it does not establish universal retrieval quality or a general ranking guarantee.

## Evidence and privacy boundary

The native checks use persisted thread configuration and observed tool outcomes. Configuration alone is not backend telemetry. Automatic processing uses the current Codex account allowance and may consume Codex limits.

Raw host receipts are intentionally not published. They contain transient session and turn identifiers, temporary host paths, and environment details that are unnecessary for a public usage guide. This page keeps the useful test summaries and limitations without reproducing those receipts.

See [Upstream and implementation choices](../UPSTREAM.md) for the Codex contract, local semantic model, provenance, and known missing Claude Mem parity. Return to the [README](../README.md) for installation and supported commands.
