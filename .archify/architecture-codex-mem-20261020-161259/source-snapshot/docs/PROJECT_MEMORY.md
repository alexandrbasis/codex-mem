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

Native output v12 selects references from a bounded catalog of exact source spans. Code resolves references to source IDs, quotations, and character offsets. Trusted code also binds the canonical project path. The model supplies environment and version, but does not author project membership. Explicit injected canonical scope still rejects a foreign worktree. Code rejects invalid identifiers, references and spans. A valid quotation proves that text exists in a source. It does not prove every generated interpretation.

Source roles distinguish user intent, assistant reports, tool observations, and model inference. The model selects each field and its evidence. For new episode summaries, goals, rationales, outcomes, and open work, storage code assembles attributed source excerpts. It applies the same rule to claim rationales. Each field uses only its own exact disclosed references. Claim text and workflow state retain their separate validation rules.

Native v12 returns only `episodes` and `claims` at the top level. Every claim requires `outgoing_relations`, including an empty array when it proposes none. Each relation contains its type, an existing `to_claim_id` and evidence references. Code takes the origin from the containing claim, then flattens the result into the unchanged canonical `episodes`, `claims` and `relations` structure. Empty claims cannot express a native relation. Native reconciliation supports `contradicts`, `replaces` and `closes` against disclosed existing targets.

The native v11 indexed-relation format and mixed native/canonical layouts are rejected. Code neither invents a missing claim nor drops a malformed relation to salvage a batch. Canonical injected outputs and stored memory keep their existing format. There is no migration or history backfill. The native schema limits each owner's relation array; code also enforces eight relations across all owners. Target, evidence, role, scope and whole-batch validation remain required. The schema does not prevent every invalid proposal or prove semantic support. See [the ownership decision](adr/0004-native-claim-relation-ownership.md).

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

Only a supported continuation reserves its logical episode within a job. Unsupported requests remain standalone and report a continuity omission. They do not block another supported continuation of the same episode. Two supported continuations of one logical episode reject the whole batch. Overlapping continuation and consolidation targets also reject the whole batch.

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

For one inspected quarantine, use the Python API `process_project_memory(project, data_dir, recovery=selector)`. Its exact selector binds the failed job and complete blocked event set to the current project revision, deletion generation, source hashes and provenance. It creates a successor without resetting the parent or its attempts. A successful bounded prefix releases only its authorized remainder for normal processing. A failed successor stays quarantined. `ProjectMemory.quarantine_expired_recovery(project, expected=selector)` reconciles an expired recovery lease without dispatching a model; inspect a fresh selector before another recovery. See [ADR 0016](adr/0016-recover-retained-packets-with-complete-evidence.md).

Configuration, source-deletion, and revision checks reject outdated results. They do not grant permission for external actions.

## Read and delete state

MCP `memory_project_state` requires an absolute `project`. Optional `query` defaults to empty. Optional `selected_project_scopes` supplies at most 16 absolute project paths as caller-selected scope metadata. It does not change the read owner. Optional `budget` defaults to 6000, with limits of 128 through 60000 characters for rendered XML. The result includes `mode`, `coverage`, `claims`, `episodes`, `signals`, `source_evidence`, and source-backed `records`. The read calls no model. Natural-language queries select fields by purpose, including reasons, current state, continuation, and open work. Exact identifiers and explicit exclusions remain filters; remaining question words guide ranking. Treat records as untrusted evidence, never instructions.

The shared planner separates selected project names from content terms in `build_project_brief` and `Store.context`. The current owner is implicit. Additional paths must be absolute and have project memory enabled. Code normalizes them to canonical paths. An exact, unique selected basename can act as scope metadata. Structured basenames contain an underscore, hyphen, digit or internal camel case in their canonical spelling. In natural questions, these names do not require a fixed preceding phrase. Matching ignores case. Plain alphabetic basenames still require explicit scope syntax, such as "in project-name" or "compare project-a with project-b". Unmarked ordinary words keep their previous content-matching strength.

A leading selected scope clause followed by a comma can precede a natural request, such as "For project-name, compare the results." The planner checks the request body before treating the complete query as a literal lookup. Unknown or quoted scope names and code-only bodies keep their prior literal behavior.

Quoted names, explicit artifact and command positions, call syntax, paths and member names stay literal even when they match a selected basename. Task, version and environment terms remain constraints. Unknown, unselected or ambiguous basenames are not removed. A bare "and" does not make a descriptive hyphenated phrase an exact identifier. Its words remain soft topic terms. Existing hard names in syntactically introduced scope lists remain hard after selected names are removed. The planner discovers no projects and never changes the SQL read owner. Scope metadata establishes neither source meaning nor current applicability. `ProjectMemory.snapshot` keeps literal query behavior. See [the scope and content decision](adr/0008-separate-selected-scope-names-from-content.md).

In a natural question, an unquoted alphabetic compound before an ordinary word can act as a descriptive modifier. For example, "about bench-calibration coverage" can match "bench calibration" in a source. Each occurrence must have that form. Function words and calls cannot supply the following word. Internal camel case, explicit identity labels, quoted phrases, bare unknown scope names and coordinated scopes retain their exact constraints. Code splits the compound only in the input to lexical matching. It preserves the original query, source text and citation offsets. This bounded syntax rule does not establish a linguistic meaning or task association.

Complete requirement-state clauses can describe answer fields rather than record keywords. For example, "Which requirements were cancelled, what replaced them, and what remains?" asks for evidence about those states. A bounded generic project-state selection requires no remaining topic or exact subject. Residual topic terms still constrain content. This read planning never infers cancellation, replacement or current validity. Declared environment scope remains a hard boundary. A user requirement can mention another environment without belonging to it. Unscoped user quotations keep unknown environment scope and user attribution; they do not become execution evidence. Text-based environment checks apply to assistant reports and tool records. Inferred claims are checked against their own execution references. An environment projection rejects a complete quotation group containing incompatible execution evidence and does not restore the original fields when every group is filtered.

Recognized subject-free questions about supported outcomes, what evidence establishes, or the standing of an assistant report can select bounded project state without requiring their framing words in every record. A question assessing an earlier assistant claim against current evidence can include earlier episode versions and actual execution results. Comparison inflections such as "compared" and "comparing" select comparative evidence instead of an as-of historical slice. An explicit as-of question keeps its historical scope. Questions relating the latest request to previous work can also include earlier episode versions. Topic words, exact identifiers, versions and environments retain their filters. Unknown topical queries do not receive this broader selection. This grammar is conservative and currently covers English frames. Recognized clauses include authorized or unfinished work and what a continuation requires. Their remaining topic terms stay mandatory. Grammatical evidence questions change allocation, not source eligibility. Ordinary lowercase source words can use bounded prefix lookup, including common plural endings. Literal identifiers and versions remain exact. The adjective "original" alone does not request a historical slice. Explicit as-of wording and past-tense questions about the original state retain historical behavior. Selection of continuity evidence never establishes that two tasks are the same.

One render-owned plan allocates already-selected original user evidence and neutral originals together. It uses complete same-source intervals with their own role, field, scope and uncertainty. User intervals can join up to 1200 characters. Other selected episode intervals keep the 600-character bound. An inferred `prior_*` field cannot suppress an independently qualified whole original in source custody. The original keeps its disposition and unresolved applicability. Historical field associations keep their own qualifiers. Per-source capture sequence takes precedence over a multi-source episode range. Associated and neutral streams compete by their retained order. Lexical association gives an older source no unconditional priority over a later original. Novel source text receives a turn before duplicate association wrappers. Exact copying establishes neither a field purpose nor current applicability.

A displayed original can have a source-only `id`, such as `s1`. A `quote_ref` resolves its exact quotation provenance through that ID. Each original retains its canonical source ID, role, source field, offsets, literal quote and any tool-use ID. Reference bounds can select an exact subinterval. Different events, roles and source fields never become aliases. The root declares `quote_refs="provenance_only"` when references appear. Scope, history, custody, workflow and query relationship stay with each association. A reference keeps its own qualifiers, including `context_basis` and unknown pointer metadata. It inherits no association qualifiers from the original's container.

The plan uses capacity after bounded protection for original executions and reports. Unrecognized natural questions can protect one whole execution within `min(1800, budget // 3)` and one small report within `min(900, budget // 6)`. Recognized affirmative evidence requests can use expanded evidence protection. Unrecognized grammar does not establish a policy-only question. Omission counts remain explicit when all requested roles cannot fit the same 6000-character default budget. The read calls no model and does not enlarge the body window or storage schema.

Ordinal execution questions order each role's existing sixteen-candidate window by capture sequence before returning at most two records. The chosen execution records can precede other material. Explicit first/earliest and recognized original-operation assessments use ascending capture order; latest/last requests use descending order. The existing atomic command, exit and full-output rule still applies. Capture order cannot establish operation identity, external timing or a successful result.

Ordinary nonhistorical natural questions outside continuation and ordinal lookup can receive a finite neutral pool of originals without a soft topical match. A missing lexical match no longer requires empty transport. It does not establish relevance either. The existing hard project, task-ID, literal identifier, version, environment, session, privacy, deletion and history guards remain. No source acquires task association, requirement acceptance or current-state truth from this selection.

`project_memory_custody.snapshot(recent_context=True)` selects whole retained user originals of at most 600 characters before the newest-sixteen cap. It can include stored or completed custody as well as unresolved custody. Default ordering and `recent_unresolved=True` behavior remain available. Text and guard metadata return together, without an extra `Store.get` body fetch for these candidates. There is no FTS search across raw user history. Previously represented observations retain their disposition but show `meaning=original_user_observation` and `uncertainty=current_applicability_unresolved` when supplied without association.

The renderer considers neutral whole user originals from the bounded candidate windows, trying the latest capture and then the earliest before remaining candidates. It checks complete group and omission-marker costs rather than imposing a separate three-source cutoff. Their XML uses at most `min(1800, budget // 3)` and space left after protecting selected original executions and reports. Already selected material-source groups get a turn before optional neutral originals. Candidate-limit metadata reports admitted candidates before XML packing; it does not promise that all will fit. Only identical role, scope, disposition, meaning and uncertainty metadata can share a container. Checked original environment and version qualifiers remain on the rendered source even when the question has no scope. Code never substitutes the query scope or temporary environment guard. Known, unknown and differing original scopes stay separate without an applicability inference. `is_requirement_claim=false`, `selection_basis=unassociated_user_context` and `relationship_to_query=unresolved` stay explicit. Coverage counts selected and omitted sources. Capture order does not establish that an earlier observation and later revision concern the same event.

A separate neutral assistant/tool pool reads at most sixteen candidates per role and returns at most two per role. It does not require soft lexical overlap. Bodies exceeding 1200 characters are omitted before transport. Assistant text must fit in full. Tool records still require the existing validated capture shape and complete command, exit and output. These records keep `selection_basis=unassociated_source_context`, `relationship_to_query=unresolved` and no requirement-claim assertion. They fit only within remaining XML space after protecting already selected original records. Scope, privacy and source guards apply independently. A report remains a report; a capture does not prove completion of the surrounding work.

Complete original executions use one canonical serializer for primary admission. The actual command, exit code and complete output stay in one atomic group. Direct source pointers retain their own source ID, role, field, tool-use ID and offsets. Execution headers retain the literal command, tool name, tool-use ID, exit code and exit-field identity. After selection, a strictly checked complete pair can factor repeated completeness metadata into `complete_capture_fields="true"` on its item. Only exact redundant native item-role and signal-kind values can disappear. Unknown or conflicting values remain. Scope, uncertainty and current/history qualifiers never merge across records.

The renderer compacts selected XML, then appends complete complementary source groups that fit the verified savings. This step preserves the primary quotations and associations. It never evicts them to admit another group. Unreferenced synthetic display IDs and redundant full-report lengths can disappear. Adding an original quotation does not restore an omitted canonical field association. Its omission counters remain applicable. Source selection, neutral pool limits and storage do not change.

Both brief construction and the public renderer use the same final rendering step. If compaction cannot fund the mandatory reference declaration, primary packing retries once with its exact size reserved. The returned omission metadata belongs to that retry. Refill preserves this resulting primary selection. Conservative primary admission can omit a group whose compacted XML alone would fit. The renderer does not promise maximal packing. [ADR 0007](adr/0007-preserve-selection-during-source-compaction.md) records this tradeoff.

Final refill can also restore a complete omitted typed claim association when all its exact source spans are already displayed. Its own workflow, scope, field purpose and uncertainty remain explicit. References use the actual displayed source identifiers; source, role, field, interval and tool-call identity must match. Exact root-shared worktree and capture-order values can remain at the root, while unknown or conflicting qualifiers stay local. Each complete node must fit the final XML budget without evicting selected sources. This does not restore every inferred episode association or infer task continuity.

The renderer removes duplicate derived prose only when it exactly equals its same-source quotations in their original contiguous order. Source pointers, roles, field metadata and record qualifiers remain. Paraphrases and inferred interpretations keep their separate prose. These allocations trade space between eligible records. More complete evidence can displace incidental quotations; this is not a claim of overall compression or better answers. Private, deleted, project, task and environment guards remain. The validated execution allowlist and atomic command, exit and complete-output rule do not change.

The planner separates `answer_subject`, `answer_facets` and `source_order`. Recognized frames treat requested state, current revision, reasons and execution assessment as answer instructions. They keep the remaining topical subject for matching. Success or failure wording does not require matching evidence to repeat that polarity. Reasons and compound requirement questions retain their named object. Exact task IDs, artifact identifiers, versions and environment exclusions remain separate hard constraints. Unknown or unrepresentable topical subjects never become unrestricted material matches. Independently guarded neutral originals can still reach the transport with unresolved query relationship.

Positive complete questions about the fate of an earlier request or whether a later request belongs to earlier work can select current reference evidence. Incidental earlier/later words in these frames do not create a historical slice. A task-relation question uses the continuity projection without setting continuation or asserting task identity. Explicit as-of, dates, genuine history and exact versions retain their guards.

Recognized first, initial, earliest, latest and last operation questions can request source capture order. This orders retained observations, not verified event time. It does not prove that two captures describe the same operation. A current evidence assessment can include independent episode heads and earlier versions without converting their results into current truth. Explicit as-of wording keeps its existing behavior.

For neutral custody, declared environment metadata can constrain a read only through a current whole-source exact user claim. The binding requires the same original source ID, body field, user role, complete body span and exact text. Every reference must belong to that original user source. Partial claims, paraphrases, mixed sources, historical fields, episodes, reports and retired claims cannot bind it. Project, worktree, privacy, session and deletion guards still apply. This metadata is used only for the environment guard. It does not assign scope to the quote, supply content anchors or change custody state.

A source has a 64-claim association limit. Overflow omits the source and increments `custody_scope_binding_lookups_omitted`; it is not proof that no declaration exists. Conflicting whole-source declarations increment `custody_scope_conflicts`. An environment-scoped read omits that custody source. An unscoped read can retain its user quote with `conflicting_declared_environment_scopes` uncertainty.

Rendered fields keep their own source pointers or exact quotation references. For attributed fields, the renderer supplies complete exact quotations from the field's own references. Each quotation keeps its source ID, role, source field and original offsets. It does not add a second copy as derived prose. The field retains its evidence basis, inferred association and partial-context status. If a field and all its pointers cannot fit, the renderer omits that whole field and reports the omission. Truncated quotes retain an explicit warning. A redundant false flag can disappear only when exact interval or complete-capture metadata preserves its meaning. Capture sequence ranges describe local ingestion order, not event time or proof that work is complete.

A record with attributed fields can use the remaining global brief budget. The renderer reserves bounded room for whole obligations, reasons and unresolved sources from later records. An impossible reservation does not block a primary group that fits. For natural-language episode projections, the default field order prefers open work and reasons before outcomes and summaries. Rationale and environment projections keep their own order. Only complete source intervals actually emitted with the same source field and role can suppress repeated quotes. When this removes an inferred episode field association, the renderer counts it in `field_associations_omitted`. Available source text does not mean that every inferred field association remains represented. An oversized group leaves smaller alternatives eligible. The renderer keeps complete groups and uses no generated replacement text. Structured omission counts remain available even when a tiny budget cannot fit their XML marker. These changes alone do not establish better answers.

User-source fallbacks remain candidates until rendering. Only complete intervals actually emitted from the same source field and role can suppress a fallback. Candidate fields that later fail to fit do not count as displayed evidence. Compact source groups of at most 600 characters can receive a whole-group reservation. Their total reservation uses at most one third of the available brief, capped at 2000 characters. For sources overlapping material fields, this reservation protects reason evidence. Larger groups still fit in full or receive an omission count; the budget does not guarantee every relevant source will appear.

When at least two eligible unresolved user-source intervals remain, the reader can combine them into a neutral original-source bundle. Sources share a bundle only when their record metadata match, including role, disposition, meaning and uncertainty. Different uncertainty markers stay in separate containers, including single-source groups. A warning on one source is neither dropped nor applied to unrelated sources. Each selected interval stays whole and retains its source ID, role, offsets and local capture sequence. Selection tries the newest intervals first, then displays the selected intervals in capture order. At most eight intervals of at most 600 characters each enter a bundle. Its XML must fit 3200 characters and the remaining brief space after reserved execution and current-head evidence. Intervals that cannot fit receive omission counts. A partial consumed source keeps its explicit interval status. These limits do not promise a complete transcript or every relevant source.

Hard task, identifier, version, environment, project, privacy, session and deletion checks still apply. Soft lexical relevance can keep an original user source that eligible material already cites, after the hard query checks pass. Bundle text remains evidence supplied by the user. It does not create or discharge a requirement, infer task continuity or change source retention. Matching worktree and capture-order qualifiers can move to the XML root only when its opening and closing tags fit the budget. Otherwise qualifiers stay on individual records, which can be omitted if they do not fit. Each displayed source keeps its own capture sequence.

The renderer reserves bounded space for complete observed tool evidence when a capture has no validated native execution shape. After material rendering, it can add a compact identity for an omitted latest head. That marker keeps version and uncertainty metadata without claiming that omitted fields were shown. It can remove lower-priority coverage counters to fit the marker, but never removes a fitting quotation for it. Already displayed quotes can replace repeated source text, but omitted field associations remain explicit. The same caller-supplied brief budget still applies, with 6000 characters as the default.

Original assistant reports and validated execution captures have a separate bounded source-evidence lane. Search applies before each role's candidate limit. Each role can return two records from at most sixteen candidates. Assistant excerpts must fit 1200 characters in full. Execution evidence uses the existing capture checks and retains command, exit code and output provenance. Oversized or invalid records stay omitted with explicit counts. A source can appear here even when extraction never cited it. Exact identifiers, versions, project boundaries and session exclusion still apply. The lane does not infer contradictions, task completion or truth. It adds no durable retention guarantee; normal source pruning and explicit deletion still apply.

An original execution keeps its command, exit status and complete captured output together. The renderer omits the whole group if it cannot fit. Compact executions already cited by extracted material can move ahead of incidental material: their combined early allocation uses at most one third of the brief, capped at 1800 characters. User-source reservations still apply. Exact tool quotations already displayed need not repeat. These bounds protect small execution records without promising that every capture will fit.

Use `memory_get` to open cited sources or derived records. `memory_forget` and CLI `forget` revoke dependent objects and invalidate worker snapshots. Old results cannot restore deleted sources. Deleting a source revokes dependent episode descendants. Deleting a current head does not make its earlier version current again.

Age-based `prune` retains sources backing stored objects, relations, or pending events. This conservative policy is not precise retention for completed work. Explicit deletion remains authoritative.

## Inspect processing costs

Project memory resolves its processor override, default profile, then legacy Codex. Each invocation records an immutable requested profile before dispatch. The next attempt reloads configuration, including in a retained service. Active calls and corrections keep their original deadline. Usage checkpoints and finalization retain failed, successful, interrupted, and retried attempts. Retrying one job does not overwrite prior costs. A hard crash can leave a running receipt with partial observed tokens. Unrecorded usage remains unknown. Source deletion preserves content-free accounting records.

CLI `usage report` and MCP `memory_usage_report` include PM attempts in the existing observer stream. `source_kind_counts` distinguishes PM from ordinary observation processing. Reports filter by project, session, and period. They exclude duplicate foreground records for known Codex worker threads. Equal worker IDs from another runtime do not hide Codex usage.

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

## Experimental reader contracts

The comparison helper can use `assertion-owned-citations-v1`. Each delivered assertion owns its citation references. Code joins assertion text in its original order; auxiliary factual checks do not enter the answer or earn recall credit. Structural ownership does not prove support, current applicability or external truth.

The optional protocol field `reader_fidelity_contract="quantified-source-fidelity-v1"` selects a separate prompt and reuse identity. It requires assertion ownership, typed per-question isolation, a 6000-character context and a valid declared reader profile. The instructions preserve explicit quantifiers, negation, conditions, stated reasons and source roles in relevant delivered assertions, and discourage unrelated current-status claims. The decoder still accepts structurally valid semantic mistakes. Absent opt-in retains legacy requests; explicit invalid options reject before dispatch. Both comparison arms must use the same declared mode. This field does not change product settings, model profiles, ownership schemas or evaluator criteria. See [ADR 0009](adr/0009-own-reader-citations-by-delivered-assertion.md).

The experiment helper saves the latest bounded native usage checkpoint in a separate receipt. Final success or failure metrics remain authoritative. A checkpoint cannot turn unknown final usage into a complete total or authorize another attempt. This receipt does not restore episode queue checkpoints or identify the cause of a remote server error.

The optional experiment field `pipeline_receipt_contract="complete-shared-pipeline-v1"` requires shared-prefix comparison. Its report includes each physical generation once, candidate preparation, and required regeneration stages, including missing stages. Failed physical generation excludes stale grades from both reader arms. Native call failures and token accounting remain in the canonical call ledger. Without this field, the helper retains the earlier reporting behavior.

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

The opt-in experimental `clause-support-v2` reader contract belongs to the quality experiment helper. It does not change the production reader API, model profiles or service configuration.

The [earlier coverage decision](adr/0005-bounded-source-coverage-in-reader-context.md) records the R13 boundary. [ADR 0006](adr/0006-expanded-neutral-original-context.md) records its revision for finite neutral original context without a soft topical match.

## Pi background processing

A project-memory profile can select the pinned Pi 1.4.0 RPC runtime. Ordinary processing, reconsideration, consolidation, and one correction retain the same packet and atomic commit checks. Each attempt uses a separate ephemeral Pi session. The attempt ledger preserves the Pi receipt and `pi_assistant_usage` separately from historical Codex provenance. See [Pi runtime](PI_RUNTIME.md) for exact capability checks, authorization, limits, and installed-CLI evidence.
