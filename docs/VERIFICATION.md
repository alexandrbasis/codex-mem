# Verification

Version `1.4.0` adds retained tool I/O, structured observations, session summaries, and structured retrieval filters. Tests below have bounded synthetic scope; they do not establish universal note quality.

## Version 1.4.0 verification

The release comparison targets Claude Mem commit [`fd0ecf023336ce631c8a5cd7b70cdeca8f0e82e0`](https://github.com/thedotmack/claude-mem/commit/fd0ecf023336ce631c8a5cd7b70cdeca8f0e82e0), rather than assuming it is identical to stable v13.24.1. A direct Claude-provider A/B was unavailable because Claude CLI authentication and a subscription were absent.

- The native observation processor uses `gpt-5.6-luna` with `medium` reasoning.
- Synthetic hook/MCP acceptance passed 11 hook invocations. Official hook-schema validation was not enabled.
- The full unit suite passed 217 tests. Ten isolated native Luna/medium scenarios passed, covering useful fixes, intent without an outcome, routine noise, contradictory verification, injection, continuity, long output, and project/session boundaries.
- A fresh native Codex task executed a UNIQUE-constraint regression, captured a 30,796-character shell result through `PostToolUse`, and retained a fact absent from command input. The observer received that middle fact, created a structured note, then produced a Stop summary from the earlier note. The harness explicitly drains processing after native capture; it does not replace the separate detached-service baseline.
- The pinned upstream prompt builder and parser were executed offline against eight synthetic events and three parser samples. A Codex-provider decision case additionally passed real hook-handler capture, hydration, and Luna processing. This is not a Claude-provider A/B.
- Earlier native runs exposed identifier loss, noise in summaries, and missing summaries after asynchronous note writes. Corrections and fixture-boundary changes are recorded in the comparison JSON; earlier failed runs remain failures.
- The installed host discovered six enabled, trusted hook definitions at version 1.4.0. Database migration to schema 4 passed SQLite integrity checks, with a pre-upgrade backup.
- Older managed cache paths are preserved; their launchers forward to the current managed installation. Both skills remain explicit-only.

See [the machine-readable comparison](observation-comparison.json) for individual criteria and the preserved earlier evidence. The current comparison does not claim complete product parity: UI, HTTP API, cloud synchronization, and non-Codex integrations are outside this implementation.

To reproduce the local checks from the repository root:

```sh
python3 -m unittest discover -s tests
python3 scripts/acceptance.py
python3 scripts/observation_quality_acceptance.py --native --output /tmp/observation-quality.json
python3 scripts/native_tool_capture_test.py --native --output /tmp/native-capture.json
```

Native commands use the signed-in Codex account and its allowance. For the upstream contract check, supply a checkout at the pinned commit and install Bun, then run `python3 scripts/compare_observers.py --upstream /absolute/path/to/claude-mem`. The default run does not invoke an AI provider; `--codex-live` explicitly enables the Codex-only cases.

## Historical verification

The following records apply to their named versions, not automatically to the current release.

## Observation quality checks (1.3.1)

- The complete unit suite passed 166 tests. The synthetic hook/MCP acceptance passed nine hook invocations, including native string-shaped Bash results; official schema validation was not enabled for this run.

- A native Luna/medium replay reproduced rejection of an otherwise structured response after a long source ID was transcribed incorrectly. The new model-facing request uses short handles constrained by the response schema; the local resolver rejects unknown handles without guessing attribution.
- Four isolated native Luna/medium cases passed: routine maintenance produced no notes; a verified-fix fixture mixed with routine activity produced one attributed fix note; an unfulfilled user request produced no completion note; a memory queue error snapshot without a diagnosed cause produced no note. These are bounded synthetic checks, not a statistical quality benchmark.
- Raw hook records are retained for explicit audit but excluded from default search, automatic context, semantic candidates, and semantic health totals. Existing raw vectors cannot bypass the retrieval filter.
- Processed batches may omit unrelated evidence. Known, disjoint source attribution remains mandatory for every note; unused evidence remains available through explicit audit and is not reprocessed after the batch completes.
- Bash output handling matches [CLI 0.153.4 `ExecCommandToolOutput::post_tool_use_response`](https://github.com/openai/codex/blob/rust-v0.153.4/codex-rs/core/src/tools/context.rs#L376-L383), which emits a JSON string. Object-shaped output remains supported. Result excerpts are redacted before truncation.
- Private diagnostic receipts and user records are not included in this repository.

## Maintenance skill checks

The `1.3.0` maintenance skill combines diagnosis and authorized repair guidance. Its JSON helper is scoped to metadata and uses read-only storage access; native hook discovery and execution remain separate evidence.

- The final `1.3.0` unit suite passed all 150 tests, including seven maintenance tests.
- The skill frontmatter and resource structure passed the bundled skill validator.
- Seven isolated maintenance tests passed: absent/existing store reads without primary data changes, corrupt database/config/service diagnostics, expired and blocked jobs, real enqueue metadata without worker startup, metadata privacy, and project isolation.
- An independent skill exercise on a fictional manual-mode project preserved the check-only scope and correctly left native execution unverified. The repair guidance preserves a project-only request when a global worker could process other projects.
- The local synthetic acceptance driver passed again with nine hook invocations. Official hook-schema validation was not enabled for this invocation.
- The existing native host-check script discovered all six enabled, trusted Codex Mem definitions on CLI `0.153.4`. This establishes discovery and trust only; it does not claim a new end-to-end processing run for `1.3.0`.

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
