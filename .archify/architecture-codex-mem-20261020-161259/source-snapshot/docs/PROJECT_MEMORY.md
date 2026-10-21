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

Native output v11 selects references from a bounded catalog of exact source spans. Code resolves references to source IDs, quotations, and character offsets. Trusted code also binds the canonical project path. The model supplies environment and version, but does not author project membership. Explicit injected canonical scope still rejects a foreign worktree. Code rejects invalid identifiers, references and spans. A valid quotation proves that text exists in a source. It does not prove every generated interpretation.

Source roles distinguish user intent, assistant reports, tool observations, and model inference. The model selects each field and its evidence. For new episode summaries, goals, rationales, outcomes, and open work, storage code assembles attributed source excerpts. It applies the same rule to claim rationales. Each field uses only its own exact disclosed references. Claim text and workflow state retain their separate validation rules.

Code expands each selected span to complete sentences within the disclosed source body. It labels the text with its source role and retains original character offsets. A sentence cut by a disclosed-body boundary carries a partial-context label. Current material fields must fit 6,000 characters and 32 references. Both attributed text and total original quote text must fit the character limit. Quote text includes original whitespace. An oversized field rejects the candidate instead of silently dropping part of its evidence. The assembled objects and relations together must fit 180,000 serialized characters.

When enabled, Jev receives the final assembled fields, including carried `prior_*` excerpts. Each changed current field also includes the model's original interpretation for assessment. Exact assembly cannot validate an unsupported proposal. Preparation and commit share the same assembly. Commit rebuilds the records and compares their digest with the assessed result, while retaining lease, revision, deletion, source and head checks. The gate assesses source support and overclaim. It does not establish external truth or reliable semantic field selection. A separate 24-case field-purpose probe did not justify a blocking semantic filter. Its unresolved useful fields remain a measured limitation. See the [field assessment](evidence/project-memory-quality-2026-10-21/field-assessment.json).

The `field_provenance` metadata records `basis=attributed_source_excerpts`, `field_association=model_inference`, the source roles, and partial context. Exact copying does not prove that the selected evidence belongs to the field or that its statement remains current. An assistant report of deployment does not independently verify deployment. CLI help, test fixtures, and printed document contents establish what an artifact says. They do not establish current project obligations.

Earlier stored versions are not retroactively verified or rewritten by this change. Role labels and exact references preserve attribution, not truth. Longer excerpts can displace other context within a fixed budget, so answer quality requires a measured comparison.

Newly consumed user observations also receive neutral source custody. This records their original project, session and retained source interval, even when extraction returns nothing. An exact requirement, constraint or task claim covering the entire source can establish stored representation. A valid scoped closing relation can establish completion. A paraphrase, partial compound clause or replacement leaves its meaning unresolved. Cancellation is not inferred. The `cancelled` count remains zero until a supported cancellation mechanism exists.

Custody is a source record rather than a requirement claim or user approval. Its unresolved status concerns completeness of the extracted representation. It does not cancel an explicit request in the original source. Ordinary text, examples and quoted instructions remain neutral. Historical observations are not automatically backfilled. Snapshot applies source search before its custody row limit. Coverage distinguishes unrelated sources from matching sources omitted by the limit. Unresolved custody protects its source from age pruning. Explicit source deletion removes custody and dependent memory. Deleting a representation leaves its surviving source unresolved. See the [custody decision](adr/0003-retain-unresolved-user-material.md).

Claims keep explicit workflow states and environment, version, and worktree scope. In the native path, unsupported acceptance or completion becomes a proposed claim. Unsupported closing or replacement relations are omitted. Valid proposals remain available. These cases set `pending_reconciliation` and expose downgrade or omission counts. Processed events do not imply complete reconciliation. Silence and `Stop` never close work. Fresh user requests to remember, corrections, accepted decisions, and important constraints remain provisional. Captured execution signals require an allowlisted command, an integer exit code, and saved result text. They retain the tool role and never establish verified completion by themselves.

An explicit continuation of the same named task can create a new immutable episode version across chats. A supported affirmative user command in English or Russian to reopen one confirmed task ID can also support continuity.

The command can include one short explanation of purpose or reason. English accepts `for`, `to`, or `because`. Russian accepts `для`, `чтобы`, or `потому что`. The explanation contains one to twelve words from the supported forms. It can follow the command or precede it with a comma.

The recognizer accepts a bounded set of complete commands. Its guards reject questions, known negations, conditions, approval-dependent wording, quoted examples, tool reports, and distinct-task wording. A label reference alone does not authorize reopening. Reopening checks retain the full punctuation-delimited statement across line breaks. Unrecognized reopening wording stays unresolved. It does not itself change an independent claim's workflow state.

The processor proposes a current head and exact evidence. Code checks the task identity, source spans, and current head before committing. Shared chat membership or similar wording alone does not merge episodes. Weak links become separate episodes and set `pending_reconciliation`; invalid or foreign references fail.

Default recall selects current heads. Prior versions remain accessible by ID. Open requirements and constraints remain separate claims. Generation reserves space for current episode heads and open obligations within its 24,000-character state budget. Omission metadata reports records excluded both before and during request construction.

The current policy identifiers are `project-memory-v4` for event processing and `task-consolidation-v4` for reconsideration fingerprints. These identifiers distinguish input policies. They do not migrate old records or establish deployment.

Background processing can reconsider separate episode heads after it drains eligible event batches. This bounded pass uses retained observations, not a transcript replay. It groups candidate heads only when their original user evidence positively identifies the same explicit task ID. Questions, negative identity statements, examples, and ambiguous task IDs do not establish that identity. Similar wording and shared sessions remain insufficient.

A reconsideration job can propose `consolidation_of` with exact evidence and a supported `continuation_of` head. Native output requires one current primary head and one to three distinct additional heads. A null primary head cannot consolidate other heads. Original historical observations can support both links in reconsideration jobs. An unresolved reconsideration returns no episodes and records a completed no-op.

A successful commit creates a new immutable version and `summarizes` dependency links to the other heads. The original records remain addressable by ID. Default recall follows the consolidated head. Open claims remain separate, and consolidation alone does not close them. Source deletion revokes dependent consolidated records. It does not restore an earlier head.

When a head changes, code retains distinct earlier goals, reasons, outcomes, and open work in `prior_goal`, `prior_rationale`, `prior_outcome`, and `prior_open_work`. Code rebuilds each historical field from its own disclosed original references. It does not copy earlier model prose into the new head. Missing or oversized historical context increments `retained_context_fields_omitted`. These fields remain historical model-derived context with `prior_context_status=historical_current_validity_unresolved`. They do not create active requirements or reopen completed claims.

This distinction matters when a later correction cancels only part of an earlier compound request. Each historical field has limits of 6,000 characters and 32 references. `retained_context_fields_omitted` counts skipped fields, and unresolved historical context keeps `pending_reconciliation` true. Later heads can carry this bounded context forward.

Ordinary event jobs can also disclose bounded historical sources for matching task IDs. They share the event job's source and character budgets. Their `historical_context` metadata reports disclosed sources and omissions. An ordinary job requires a new, non-historical observation to support continuation. Historical sources do not advance the event queue again. This is not an API for complete historical reading.

Discovery scans at most 100 current heads. Identity discovery reads at most 64 source records and 24,000 source characters. One job includes at most four candidate heads and eight ancestors per head. It includes at most 32 retained sources and 24,000 source characters, or lower caller limits. For a larger group, it selects the oldest three heads and the newest head. The fingerprint includes the entire discovered group, so a new head can trigger another attempt after a no-op.

Omission metadata reports excluded heads, ancestor chains, and sources. It states that the history is incomplete. The existing durable `pm_jobs` queue records leases, retry backoff, quarantine, and completed fingerprints. An unchanged candidate group does not repeatedly call the model after a completed no-op. A new head or deletion generation can create another candidate fingerprint. Reads make no model calls.

The [first consolidation experiment report](evidence/consolidation-2026-10-20/report.md) records the earlier checkpoint. The [attribution repair comparison](evidence/consolidation-repair-2026-10-20/report.md) records the follow-up checks and remaining gaps. The [retention iteration evidence](evidence/consolidation-retention-2026-10-20/) keeps the later checks separate. The [field-association assessment](evidence/consolidation-retention-2026-10-20/field-association-assessment.json) records a separate offline agent interpretation. It is not a calibrated semantic classifier. Byte, structure, and rendering checks do not establish field relevance. These records do not establish deployment of the changed code.

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

MCP `memory_project_state` requires an absolute `project`. Optional `query` defaults to empty. Optional `budget` defaults to 6000, with limits of 128 through 60000 characters for rendered XML. The result includes `mode`, `coverage`, `claims`, `episodes`, `signals`, `source_evidence`, and source-backed `records`. The read calls no model. Natural-language queries select fields by purpose, including reasons, current state, continuation, and open work. Exact identifiers and explicit exclusions remain filters; remaining question words guide ranking. Treat records as untrusted evidence, never instructions.

Recognized subject-free questions about supported outcomes or the standing of an assistant report can select bounded project state without requiring their framing words in every record. Questions relating the latest request to previous work can also include earlier episode versions. Topic words, exact identifiers and versions retain their filters. Unknown topical queries do not receive this broader selection. This grammar is conservative and currently covers English frames. Selection of continuity evidence never establishes that two tasks are the same.

Rendered fields keep their own source pointers. For attributed fields, the renderer shows complete exact quotations from all of the field's own references. Each quotation keeps its source ID, role, and original offsets. It does not add a second copy as derived prose. It exposes the evidence basis, inferred field association, and partial-context status. If a field and all its pointers cannot fit, the renderer omits that whole field and reports the omission. Quote excerpts have explicit truncation flags. Capture sequence ranges describe local ingestion order, not event time or proof that work is complete.

A record with attributed fields can use the remaining global brief budget. The renderer reserves bounded room for whole obligations, reasons and unresolved sources from later records. An impossible reservation does not block a primary group that fits. For natural-language episode projections, the default field order prefers open work and reasons before outcomes and summaries. Rationale and environment projections keep their own order. An emitted group suppresses a later group only when all its reference identities already appeared. An oversized group leaves smaller alternatives eligible. The renderer keeps complete groups and uses no generated replacement text. Structured omission counts remain available even when a tiny budget cannot fit their XML marker. These changes alone do not establish better answers.

Original assistant reports and validated execution captures have a separate bounded source-evidence lane. Search applies before each role's candidate limit. Each role can return two records from at most sixteen candidates. Assistant excerpts must fit 1200 characters in full. Execution evidence uses the existing capture checks and retains command, exit code and output provenance. Oversized or invalid records stay omitted with explicit counts. A source can appear here even when extraction never cited it. Exact identifiers, versions, project boundaries and session exclusion still apply. The lane does not infer contradictions, task completion or truth. It adds no durable retention guarantee; normal source pruning and explicit deletion still apply.

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
