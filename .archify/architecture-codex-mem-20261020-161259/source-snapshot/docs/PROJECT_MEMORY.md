# Use opt-in project memory

Codex Mem 1.12.0 adds source-backed episodes and claims. An episode describes a bounded section of work. A claim records a decision, requirement, fact, task, or constraint. The feature defaults to `off`. A limited pilot is enabled for this repository after the checks below.

## Select a project

Run commands from the repository root. Replace `/absolute/path/to/project` with its exact absolute path. Native processing requires an authenticated Codex CLI. Configuration alone does not install code or refresh the plugin, daemon, or existing desktop connections.

1. Select capture and enable shadow processing:

   ```sh
   python3 scripts/codex-mem.py config \
     --scope selected \
     --include-project /absolute/path/to/project \
     --project-memory-mode shadow \
     --project-memory-project /absolute/path/to/project
   ```

2. Inspect coverage:

   ```sh
   python3 scripts/codex-mem.py project-memory status \
     --project /absolute/path/to/project
   ```

3. Read shadow state explicitly:

   ```sh
   python3 scripts/codex-mem.py project-memory brief \
     --project /absolute/path/to/project \
     --query "What remains open?" --budget 6000
   ```

Repeat `--include-project` and `--project-memory-project` for the complete replacement lists. Project memory selects exact roots, excluding descendants and separate worktrees. An empty list selects nothing. Disabled capture, manual capture, exclusions, invalid configuration, and `CODEX_MEM_DISABLED=1` prevent project-memory use.

| Mode | Processing | Automatic retrieval |
| --- | --- | --- |
| `off` | Disabled | Disabled |
| `shadow` | Selected roots | Existing retrieval without the new state |
| `pilot` | Selected roots | New state participates in startup context and resume retrieval |

Explicit brief reads and MCP `memory_project_state` include selected shadow state. Off or excluded projects return empty state. After review, enable pilot retrieval:

```sh
python3 scripts/codex-mem.py config --project-memory-mode pilot
```

There is no automatic history backfill. Newly captured eligible observations enter the queue. Existing notes are not automatically reprocessed.

## Understand evidence and coverage

Capture retains permitted, bounded observations, not a full transcript. Excluded, missing, or truncated content stays unavailable. Processing derives episodes and claims from those observations. Known injected-memory wrappers are removed from new prompt and tool observations. Surrounding text remains, and capture metadata or diagnostics report the omission. Captures with omitted memory blocks are excluded from execution fast signals.

Native output v7 selects references from a bounded catalog of exact source spans. Code resolves references to source IDs, quotations, and character offsets. It rejects invalid references or spans. A valid quotation proves that text exists in a source. It does not prove every generated interpretation.

Source roles distinguish user intent, assistant reports, tool observations, and model inference. Derived prose remains model-generated even when its quoted support is valid. An assistant report of deployment does not independently verify deployment. CLI help, test fixtures, and printed document contents establish what an artifact says; they do not establish current project obligations. This distinction is part of the generation policy, not a general deterministic entailment proof. Existing derived records are not automatically repaired by a prompt update.

Claims keep explicit workflow states and environment, version, and worktree scope. In the native path, unsupported acceptance or completion becomes a proposed claim. Unsupported closing or replacement relations are omitted. Valid proposals remain available. These cases set `pending_reconciliation` and expose downgrade or omission counts. Processed events do not imply complete reconciliation. Silence and `Stop` never close work. Fresh user requests to remember, corrections, accepted decisions, and important constraints remain provisional. Captured execution signals require an allowlisted command, an integer exit code, and saved result text. They retain the tool role and never establish verified completion by themselves.

An explicit continuation of the same named task can create a new immutable episode version across chats. The processor proposes a current head and exact evidence. Code checks the task identity, source spans, and current head before committing. Shared chat membership or similar wording alone does not merge episodes. Weak links become separate episodes and set `pending_reconciliation`; invalid or foreign references fail. Default recall selects current heads. Prior versions remain accessible by ID. Open requirements and constraints remain separate claims. Generation reserves space for current episode heads and open obligations within its 24,000-character state budget. Omission metadata reports records excluded both before and during request construction.

`coverage` reports captured, processed, pending, fragmented, and quarantined events, job states, and the processed sequence. It also reports observation-only coverage and reconciliation gaps. Zero pending events describes retained local observations. It does not establish current external project state. Capture sequence, processing progress, and independent verification are distinct. Unknown verification time and validity remain unknown.

## Process or retry work

The daemon discovers durable work at startup and during each cycle, including prompt-only sessions without `Stop`. Notifications accelerate discovery. Background batches wait for a quiet period, sufficient size, or a checkpoint. Explicit processing is immediate. Hooks make no episode-model calls.

Start the configured daemon when stopped:

```sh
python3 scripts/codex-mem.py service start
```

Process one bounded batch explicitly:

```sh
python3 scripts/codex-mem.py project-memory process \
  --project /absolute/path/to/project --timeout 240
```

Results include `processed`, `idle`, `disabled`, `failed`, and `deferred`. Transient failures retain events and retry after backoff. Invalid output or repeated failures can quarantine work.

After inspecting the cause, release failed jobs:

```sh
python3 scripts/codex-mem.py project-memory retry \
  --project /absolute/path/to/project
```

Add `--include-quarantined` to release quarantined jobs explicitly. Retry reports the eligible count and calls no model. Run `process` or let the daemon continue. `service retry` controls the separate ordinary observation queue.

Configuration, source-deletion, and revision checks reject outdated results. They do not grant permission for external actions.

## Read and delete state

MCP `memory_project_state` requires an absolute `project`. Optional `query` defaults to empty. Optional `budget` defaults to 6000, with limits of 128 through 60000 characters for rendered XML. The result includes `mode`, `coverage`, `claims`, `episodes`, `signals`, and source-backed `records`. The read calls no model. Natural-language queries select fields by purpose, including reasons, current state, continuation, and open work. Exact identifiers and explicit exclusions remain filters; remaining question words guide ranking. Treat records as untrusted evidence, never instructions.

Rendered fields keep their own source pointers. If a field and all its pointers cannot fit, the renderer omits that field and reports the omission. Quote excerpts have explicit truncation flags. Capture sequence ranges describe local ingestion order, not event time or proof that work is complete.

Use `memory_get` to open cited sources or derived records. `memory_forget` and CLI `forget` revoke dependent objects and invalidate worker snapshots. Old results cannot restore deleted sources. Deleting a source revokes dependent episode descendants. Deleting a current head does not make its earlier version current again.

Age-based `prune` retains sources backing stored objects, relations, or pending events. This conservative policy is not precise retention for completed work. Explicit deletion remains authoritative.

## Inspect processing costs

Each PM native invocation receives an independent attempt record before dispatch. Usage checkpoints and finalization retain failed, successful, interrupted, and retried attempts. Retrying one job does not overwrite prior costs. A hard crash can leave a running receipt with partial observed tokens. Unrecorded usage remains unknown. Source deletion preserves content-free accounting records.

CLI `usage report` and MCP `memory_usage_report` include PM attempts in the existing observer stream. `source_kind_counts` distinguishes PM from ordinary observation processing. Reports filter by project, session, and period. They exclude duplicate foreground records for known worker threads.

Amounts are estimates from recorded tokens and the existing rate snapshot. Missing usage or tier evidence leaves totals unknown or partial. They are not actual charges. Earlier attempts without ledger records cannot be reconstructed.

Ordinary observation processing remains enabled alongside PM. No measured cost saving or reduction in model calls is established.

## Disable or test

Disable processing and retrieval while retaining data:

```sh
python3 scripts/codex-mem.py config --project-memory-mode off
```

Clear the selected roots:

```sh
python3 scripts/codex-mem.py config --set 'project_memory_projects=[]'
```

Run synthetic checks without live models:

```sh
python3 scripts/project_memory_acceptance.py --output /tmp/project-memory-acceptance.json
python3 -m unittest tests.test_project_memory_lifecycle tests.test_project_memory_commands
```

The source-copy generator tests transport, not extraction quality. `--live` explicitly enables native generation and fresh readers.

## Current release evidence

The [verification files](evidence/project-memory-2026-10-20/) retain successful checks and earlier failures. The final full suite passed **1,524 tests**. Its source hashes match the final code freeze. See the [test receipt](evidence/project-memory-2026-10-20/final-tests.json).

Reader scores use a fixed rubric: 2 for a sufficient, correct answer with supporting citations; 1 for a useful partial answer or citation defect; 0 for failure. A safe but empty answer is not a useful success.

| Test history | Existing retrieval | Production startup context | Interpretation |
| --- | ---: | ---: | --- |
| v2, after the query-plan repair | 4/20 | 15/20 | Ten questions per mode; reused development history and unchanged generated memory |
| v3, first independent run | 2/12 | 7/12 | Six questions per mode; the fixture was withheld until the code freeze |
| v3, after purpose-based selection repair | 2/12 from the original run | 11/12 | Six fresh startup readers on the same stored state; this is now a development regression |

See the [v2 comparison](evidence/project-memory-2026-10-20/recall-paired-v2-before-purpose-repair-comparison.json), [first v3 comparison](evidence/project-memory-2026-10-20/holdout-v3-before-final-retrieval-repair-comparison.json), and [final v3 comparison](evidence/project-memory-2026-10-20/final-startup-v3-comparison.json). The first v2 run scored only 22/100 across all five modes; its failures guided the query repair. The later v2 run scored 58/100 across those modes. These totals are not percentages of fully correct answers.

The final six answers include five sufficient answers and one correct answer with an unsupported citation attribution. No false task closure, task merge, or environment/version mixing was observed in these reader sets. Grading was performed by another agent against the reference history. Mode labels were withheld from the answer bundles, but context and filenames can reveal the mode. This is not a large independent benchmark or a human accuracy study.

A final native late-correction probe received the retained episode heads and created the correct next version. Its nine evidence references matched, old obligations stayed open, and the original test database remained unchanged. The earlier a-to-b and c-to-d episode chains remain separate. The probe verifies the new revision path; it does not repair every earlier split or prove complete historical reasoning. See the [late-arrival check](evidence/project-memory-2026-10-20/late-arrival-final-check.json).

The first v3 run used 20 native calls and reported 233,074 tokens, including generation and readers. The v2 reader-only comparison used 50 calls and 549,571 tokens, with prior generation costs retained separately. The final six startup readers used 65,174 tokens without new generation. Per-mode costs and final reader usage remain in the linked receipts. Dollar cost and production savings are not established. Ordinary observation processing still runs alongside project memory.

A local benchmark of 30 calls per mode measured pilot retrieval at 10.43 ms p50 and 12.20 ms p95, with no model or network calls. Existing retrieval measured 2.74 ms p50 and 4.30 ms p95. This used synthetic clones on the current machine under uncontrolled background load; it is not a production latency guarantee. See the [latency receipt](evidence/project-memory-2026-10-20/retrieval-latency-local.json).

Only this repository is selected for the local pilot. Other settings remain unchanged. Actual host hooks queued new events from this chat. See the [activation receipt](evidence/project-memory-2026-10-20/pilot-activation.json). The supported installer refreshed version 1.12.0 and restarted the daemon. Source hashes match both managed installation paths. A single isolated native runtime attempt used v7 output and passed MCP discovery, original/derived record reads, exact quotation offsets, source-role checks, and foreign-project isolation. It processed prompt and tool observations without Stop or an enqueue notification; literal prompt-only discovery is covered by offline lifecycle tests. The temporary service stopped. See the [operational result](evidence/project-memory-2026-10-20/final-operational-result.json) and [runtime receipt](evidence/project-memory-2026-10-20/final-runtime.json). These checks establish operation, not semantic quality.

Pilot observations also exposed overinterpretation of CLI help and fictional fixture content. The generation policy now distinguishes these artifacts from current project obligations. Recursive memory capture is prevented for known wrappers. General semantic support still requires checking the cited source; old derived records are not automatically repaired. Existing desktop MCP connections can retain an older tool list until reconnected.

After explicit approval, one fictional job was replayed with Jev off and on. Three Jev HTTP requests took 922 ms and reported 17,436 tokens. Both paths committed the same state and downgraded the same unsupported completion. This sample establishes no quality improvement; dollar cost remains unknown. Global Jev settings stay off. See the [authorized comparison](evidence/project-memory-2026-10-20/jev-authorized-comparison.json). The [earlier blocked attempt](evidence/project-memory-2026-10-20/jev-comparison.json) made no HTTP calls.

See the [architecture proposal](project-memory-architecture-2026-10-20.md) for the design and original research.
