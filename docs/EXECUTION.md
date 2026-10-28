# Background execution profiles

An execution profile selects a runtime, provider, exact model, reasoning level, and timeout. The interactive host is a separate choice. The shared contract connects `NativeProcessorRunner` to Codex and `PiProcessorRunner` to Pi for both processors. Observation extraction, summaries, project memory, reconsideration, and consolidation use the configured profile.

Each processor resolves its own override. A Pi profile activates Pi for observations, summaries, project memory, reconsideration, and consolidation. The processors can use different runtimes. Profile changes do not reprocess successful sources.

## Configuration contract

These optional keys accept a complete JSON object or `null`:

| Key | Meaning |
| --- | --- |
| `processing_profile` | Default for both processors |
| `observation_profile` | Override for observation extraction and summaries |
| `project_memory_profile` | Override for project memory, reconsideration, and consolidation |

The resolver chooses the processor override first, then `processing_profile`. If both are absent or `null`, it returns the existing Codex profile. Its provider is `openai`, model is `gpt-6-luna`, and reasoning level is `medium`. The default timeout is 240 seconds. The existing processing timeout argument remains effective on this legacy path.

Every profile requires these fields:

| Field | Accepted value |
| --- | --- |
| `runtime` | `codex` or `pi` |
| `provider` | Exact, nonempty provider identifier |
| `model` | Exact, nonempty model identifier |
| `reasoning_effort` | `null`, `none`, `minimal`, `low`, `medium`, `high`, `xhigh`, `max`, or `ultra` |
| `timeout` | A finite number from 1 to 600 seconds |

Identifiers retain their case and spelling. Whitespace and wildcard selection are rejected. Unknown fields, including credential fields, are rejected. A runtime must establish that its provider, exact model, and reasoning level are supported before dispatch. A recognized reasoning label does not prove model support. `null` means that the profile requests no reasoning setting.

The Codex adapter pins `model_provider="openai"` before reading the native `model/list` catalog. It matches the exact returned `model` field and an advertised `supportedReasoningEfforts` value. The catalog `id` is a picker identity, not evidence of a dispatch model. Discovery follows at most 16 pages under the attempt deadline. Malformed catalogs, disconnects, repeated cursors, and timeouts fail closed.

The Codex boundary cannot verify custom providers or a `null` reasoning setting. These profiles fail before model dispatch, as does an unavailable exact model or effort. Pi uses its separate pinned RPC adapter for both processors. See [Pi runtime](PI_RUNTIME.md) for supported providers and restrictions. No runtime, provider, model, or reasoning substitution occurs. The job remains pending as failed work with a diagnostic and can be retried after correction. Catalog evidence establishes advertised support; it does not guarantee a successful live call.

## Queue and attempt identity

The logical job belongs to its processor and exact source set, independent of its execution profile. Source revisions retain their existing IDs and custody checks. A retry or expired lease reuses the job. Successful processed or skipped sources do not become new work when a profile changes. Explicit maintenance recovery keeps its separate fingerprint, legacy-profile opt-in, and one-shot guards. An explicit `retry_failed` with a different processor ID can create a distinct processor-owned job, as before. It cannot bypass one-shot recovery or replay successful sources.

Observation claims store an immutable requested profile with the lease. Project memory records the profile before dispatch after checking its live lease. A correction or extra generation call uses that profile and the original deadline. A later attempt reloads current configuration, including in a long-lived service. Observation jobs retain their creation model. Project memory fingerprints retain their existing source, revision, deletion-generation, and processor-version fields.

Existing observation ledgers migrate requested identity from the original job and mark it as `legacy_requested_job_profile`. Project memory migrates the original attempt fields and marks them as `legacy_requested_profile`. Historical values remain unchanged. Unknown actual identity and historical timeout stay unknown. New attempts record requested and verified runtime, provider, model, reasoning, execution IDs, and usage provenance separately. Failed, rejected, canceled, and expired attempts retain measured usage. Unknown token counts stay unknown. Source deletion, lease replacement, and revision checks still prevent partial or stale commits.

## CLI format

Use the existing `config --set key=JSON` format. For example, this stores the legacy choice explicitly:

```sh
codex-mem config --set 'processing_profile={"runtime":"codex","provider":"openai","model":"gpt-6-luna","reasoning_effort":"medium","timeout":240}'
```

To remove an override, set it to `null`:

```sh
codex-mem config --set 'observation_profile=null'
```

`codex-mem config` shows the saved configuration. Invalid updates fail before the file is replaced. An invalid existing file remains disabled under the existing fail-closed configuration policy. Project memory returns `configuration_error` with `invalid_configuration` for an invalid existing file. Unsupported runtime capabilities retain failed work without consuming its invalid-output quarantine policy. Processing switches, project selection, privacy rules, Jev, and embeddings retain their separate controls.

## Runtime boundary

`ExecutionRequest` carries bounded prepared input, an output schema, job type, profile, monotonic deadline, and attempt identifier. `ExecutionResult` separates observed identity from requested configuration. It exposes normalized failure reasons and retains the existing usage provenance. Unknown token counts stay unknown.

The Codex adapter preserves completion evidence, tool restrictions, model checks, strict decoding, one correction, and cumulative usage. Each processor still validates source roles and references before its atomic commit. A failed or expired attempt cannot commit a partial packet. Routine tests use deterministic external executables. The optional installed-Pi test uses a local provider and establishes CLI/protocol compatibility only. Neither establishes live model quality.

## Diagnostics and usage

`codex-mem doctor` shows the default profile, processor overrides, effective profiles, executable availability, and recent attempt identities. It reads local metadata only. It does not start a runtime. The dashboard shows the same profiles and the latest saved failure. Failure order uses the saved receipt time, then the finish time. Historical rows without either use the attempt start time and label that fallback. Requested and observed model identities remain separate. An absent observed identity is unknown.

Run `codex-mem doctor --check-execution` to test native capability discovery without sending input to a model. Each distinct profile has a maximum ten-second check. Checks use temporary working directories and the same preparation code as processing. They do not claim a completed generation, valid remote credentials, or live model quality. Ordinary dashboard requests never run these checks.

Before dispatch, Codex reads `account/read` with `refreshToken: false`. Missing required native authorization fails with `authentication_unavailable`. Unsupported account metadata or a missing required native method fails with `unsupported_codex_protocol`. The check establishes required protocol support, not compatibility with every feature of an arbitrary Codex version. Native account presence does not establish that a remote service will accept it. The official [Codex account interface](https://learn.chatgpt.com/docs/app-server#auth-endpoints) defines these fields.

Pi checks version 1.4.0, then native provider authorization, exact catalog identity, and available reasoning levels. Missing executables, authorization, providers, models, reasoning levels, and unsupported Pi versions have separate saved reasons and corrective actions. No check changes the chosen runtime or starts a fallback. Authentication errors retain the source work for a later retry.

Usage reports accept `runtime`, `provider`, and `execution` in `--group-by`, in addition to the existing dimensions. An execution group combines runtime, provider, model, and reasoning. Reports prefer observed identity and retain requested-only provenance when no observed identity exists. Error, outcome, model-source, and usage-source counts accompany the groups. Project memory and observation attempts both contribute, including failed attempts.

Generation and each mandatory review own separate call metrics and worker identifiers. A later reviewer failure cannot replace completed generation counters or IDs. Measured review tokens add once; unavailable review counters keep the combined total unknown. The opt-in comparison recorder preserves one generation execution and up to five ordered review executions in distinct stage paths. It rejects extra calls before execution.

Every token counter has `token_totals` metadata. `known_subtotal` sums measured values. `total` is null when any included event lacks that counter or has incomplete usage. `known_events` and `unknown_events` disclose coverage. Existing top-level token fields remain measured subtotals for compatibility. A measured zero remains zero. The dashboard labels partial subtotals and unavailable totals in each token column and in Standard/Fast cost scenarios. Scenario coverage includes priced events, unpriced events, and incomplete attempts. It shows the processing share only when both token totals are complete and the main Codex total is positive.

API-equivalent estimates remain separate from `actual_billing`, which is unavailable from local records. Pi usage never receives a Codex-credit estimate. A model name from another provider does not inherit an OpenAI price. Historical provenance remains intact during migration, and worker identifiers from different runtimes do not suppress each other's records.

For a synthetic dashboard, run `python3 scripts/demo_execution_dashboard.py` from the repository. The script prints a temporary data directory, start command, and cleanup command. Stop that dashboard before cleanup. It does not start the background service or read user history.


## Hook capture deadline

The hook supervisor retains its two-second worker deadline. Ordinary ASCII text without any possible redaction marker skips the expensive replacement passes. Text with a possible marker and all non-ASCII text use the existing full redactor. This filter preserves private-block, credential, token and URL rules. Redaction still precedes truncation, and the optimization requires no bytecode cache. Cold-source public-hook tests exercise capture and privacy at the real deadline. Deadline failure still returns the existing notice that capture or context delivery is unconfirmed or skipped.

## Session summary support

Every retained material note and summary field receives mandatory original-source support review through the selected Codex or Pi profile. This also applies when Jev quality is disabled. `summary-support-v7` supplies complete nonempty field units and disclosed current/history evidence with source roles. Code requires all units, consistent coverage, one to eight comparisons per unit, at most 64 rows across 47 units, and valid source spans. Typed note arrays stay intact. Classification and labels need scoped support for any factual status they imply. Supported tool-only notes remain allowed without a summary.

Support rows for `title`, `investigated`, `learned`, `completed` and summary `notes` also need owned candidate spans and explicit original-report bindings. Code owns the report catalog and anchor membership. `report_text` and `reported_content` separate text attributes from reported events. `other` excludes a claim from report comparison. `unresolved` blocks acceptance. Historical `request`, future `next_steps` and all material note units use `outside_outcome`. They still need original support. Span ownership does not prove complete assertion extraction or correct semantic binding.

After support passes for a summary, `summary-source-v4` interprets only the latest current original assistant report. The model receives only its verbatim body as evidence. Provenance stays in code-owned validation context. The request contains no candidate, user request, history or derived memory. At most eight content propositions and both language/organization slots become at most ten source items. Unknown, planned and unavailable items retain their identities. Extraction is explicitly nonexhaustive. Jev lifecycle-only envelopes are excluded. Note-only packets skip derived review. Without a current original report, code records `not_assessed`, with no historical fallback.

`summary-outcome-v6` compares each bound claim anchored to that report with every source item from it. No semantic property or target filter prunes those pairs. Code enforces at most 50 claim/item relations before dispatch and requires each exactly once. This is not a five-field by ten-item bound. It supplies the complete original body once after a strict join of selected report identity, provenance, SHA-256 and complete contiguous owned spans. The body bound is 100,000 characters. The prompt bound is 1,250,000 characters. A failed join or exceeded bound denies dispatch.

Typed applicability, scope and value determine the result. An absent property or disjoint scope is unused. Unresolved applicability or scope is uncertain. Aligned available values become checked, contradicted or uncertain. Aligned unavailable evidence becomes uncertain, without establishing conflict. Unknown and planned items stay in required relations. A known plan with a resolved target can be compared as an attributed proposal. Compatibility must preserve its scope and unfinished status. The plan proves neither execution nor nonexecution. Unknown values remain unavailable. Only an empty relation set skips outcome with `unused`. Contradiction takes priority over uncertainty. Either blocks storage. Compatibility does not establish support or external truth. A later acknowledgement cannot establish attributes of an earlier answer. Full original context fixes evidence lost in bounded extraction, but semantic bindings and judgments remain fallible.

The response root contains only `version` and `units`. The model-facing `summary-support-v7` schema requires every exact input unit as a property of the response `units` object. Each property contains one to eight comparison rows. Every inner `unit_id` must equal its outer key. The decoder rejects missing or extra keys, empty groups, malformed arrays and mismatched owners before flattening. It preserves input-unit order and response order within each group. It never reassigns a row or repairs incomplete coverage. Bound claim IDs remain `unit#ordinal`.

The total remains at most 64 comparisons, enforced by the decoder while it collects the groups. Per-group schema limits do not guarantee that sum. A schema-valid answer with 65 rows still fails with `bounds`. Each outcome unit retains only its owned candidate spans. Excluded units require `outside_outcome` with empty binding arrays. Report text and reported content retain their distinct binding rules. Shared catalogs still use `$defs` and `$ref`. Downstream text, reference, owner, binding, citation-subset and coverage checks remain unchanged. Fallible model review assesses semantic support. Schema validity does not establish it. Each row declares one `citations` list. Each citation selects an existing span as `evidence` or `report_anchor`. An anchor explicitly serves both roles. Code derives the canonical evidence and anchor lists from those choices and applies the existing strict checks. It never adds an unselected reference. The v7 wire rejects old v5 and v6 replies. Historical diagnostics remain readable.

Generation and correction share the field-evidence rules in [ADR 0014](adr/0014-preserve-plans-and-single-review-citations.md). An unsupported original request stays null. Next steps stay empty without an explicit plan, proposal or unfinished requested work. A pending external result alone does not create a follow-up commitment.

Two initial failures can use the existing single generation correction. The first decoded `processed` candidate can request it when validation reports `missing_required_summary`. That reason does not establish that the candidate has no other errors. Separately, the first observation or summary generation with `invalid_response` and `invalid_json` can request one complete replacement under the original full schema. The strict decoder rejects the malformed bytes. It does not repair braces, continue incomplete text or accept a partial result. Unknown source handles, other failures and review jobs do not gain this path.

JSON replacement, structural correction, mandatory support and optional Jev quality share one correction allowance, immutable profile and original deadline. There are at most two generation turns. The processor checks source custody and remaining time before feedback, and the dispatch guard checks custody again before submission. The replacement must pass strict decoding, normal shape and source checks, mandatory support and all applicable summary and optional quality reviews. The final custody and lease checks still guard the atomic write. A rejected second candidate cannot trigger a third generation. No partial notes or summaries are written.

The returned receipt's `generation_correction` records `initial_reason_code` as `missing_required_summary` or `invalid_json`. Only the missing-summary case includes `candidate_fingerprint`, which identifies the parsed, source-resolved canonical candidate. Malformed bytes have no canonical candidate fingerprint, so their correction receipt contains only the initial reason. For the missing-summary case, the durable generation `review_diagnostic` retains its five keys: `version`, `candidate_fingerprint`, `evidence_fingerprint`, `status` and `invalid_subtype`. It records `invalid` and `structure`, without the exact initial reason. These diagnostics contain no raw candidate, source or reviewer prose.

The generation call ledger retains at most two sanitized per-generation rows with execution/turn identifiers, outcomes, fixed failure codes and measured usage. The original `invalid_json` failure remains when a later custody or deadline check denies replacement. The rows are diagnostic details and never add to aggregate totals. Codex derives per-turn usage only from measured cumulative boundaries, while Pi retains direct per-generation usage. Missing counters and unmeasured per-generation durations stay null. Invalid optional counters become invalid or unknown without discarding the original failure. Independent generation and review call metrics contribute once to the attempt total.

Generation and review instructions preserve the supplied execution scope of an observed result. They do not invent local, regression, synthetic or production scope. Ambiguous candidate references can use context only within the same note or session-summary record. This does not narrow permitted original-source or historical context. Explicit claims cannot borrow attribution or weaker wording from another record. Evidence-limit wording must also support any implied event or success. Incidental descriptions of candidate formatting are omitted. The candidate remains non-evidence. These wording changes leave schemas, validators, limits and storage unchanged.

Generation, reviews and optional Jev quality share one immutable profile, one monotonic deadline and one generation correction. The default remains 240 seconds. The maximum is two generation turns, two support calls, one source call and two outcome calls, totaling seven native model calls. Changed corrections repeat applicable support and outcome reviews and reuse fresh source interpretation. Exact rejected records cannot ask for another vote with unchanged evidence, interpretation, profile and contract versions. Reordering notes or changing another record does not reset rejection. The cache is local to the active attempt and can preserve a mistaken first rejection. Shape, summary preservation and freshness checks precede reuse.

Failure reasons include `summary_support_invalid`, `summary_support_unavailable`, `summary_support_uncertain`, `summary_support_rejected` and `summary_support_stale`. Store captures retained-record identities with the exact current and historical claim views, before historical excerpting. Identities include metadata, source links and hydrated current tool I/O. Store repeats the identity comparison inside the existing output write transaction. Deletion or mutation blocks the whole packet. No model or processor callback runs inside that transaction. No database lock spans a model call. The separate `project_context` remains a generation and optional-quality reference. Its existence checks do not establish unchanged content. It cannot replace support and stays outside this fingerprint.

Freshness guards run before Jev filtering, cache reuse, live chunks after HTTP-slot acquisition, selection, and every native initial, correction or review submission. Denied submissions add no fabricated calls or usage. Later denial retains earlier measured usage. Guards detect changes visible at each check and cannot revoke submitted requests. The final transaction fence prevents stale output writes. Late review after lease expiry retains its typed failure and owned metrics. A replaced lease preserves `summary_support_stale` without changing its replacement. Malformed native records remain protocol errors.

Semantic rejection and uncertainty quarantine the failed batch while fresh work continues across service restart. Malformed, unavailable, stale and unknown operational failures keep the project-blocking policy. Raw work remains available. No failed packet writes partial output. Fixed invalid subtypes are `structure`, `unit_coverage`, `unknown_span`, `verdict`, `bounds`, `unknown_proposition` and `duplicate_reference`. Diagnostic statuses are `pending`, `supported`, `rejected`, `uncertain`, `invalid`, `unavailable`, `interpreted`, `checked`, `unused` and `not_assessed`. Diagnostics retain versions and fingerprints, without raw source or reviewer prose. Historical attempts and stored summary shapes remain unchanged, including `verification=not_assessed`.

`Store.observation_attempts` retains generation and each review in the additive `execution_calls` column. Every call keeps requested and observed identity, outcome and measured metrics. Cumulative generation snapshots replace earlier snapshots. Independent measured counters add once. Missing counters remain null. Attempt duration counts nested reviews once. A running label after a crash means no final receipt was observed. An injected generator without a separate reviewer cannot start a live reviewer.

The following results describe the October 25, 2026 snapshot after the second alternative-profile comparison and before final CI4. The earlier fourteen Sol stages remain historical public evidence. Luna/high/240 later persisted an isolated false subtitle in both runtimes. In the completed second alternative comparison, Codex/openai/gpt-6-luna/max/600 and Pi/openai-codex/gpt-6-luna/max/600 each passed seven public stages. Each preserved two positive outputs exactly and rejected five false targets with zero writes. Pi/zai/glm-5.3/high/600 passed six stages. Its required language positive timed out after 600.040 seconds with zero output, so that profile failed by time. Other GLM reasoning levels were not tested.

The second comparison used twenty-nine native review calls and thirty-six synthetic generation invocations. It did not run native generation. Two automatic concept matches remained `unproven`, with separate manual semantic passes. Known usage is 364,186 tokens across twenty-eight calls. One unknown call keeps the aggregate total null. These public controls do not establish unseen private quality or installed memory injection. They compare full profiles, without isolating a model-only effect. The last full local CI at this snapshot was CI3, with one failure among 2,189 tests. Sixteen focused Jev tests passed after the contention-test repair. CI4 and final private acceptance were pending.

At that historical snapshot, the revised installation proposal set only `observation_profile` to Codex/openai/gpt-6-luna/max/600 after final private acceptance and merge/install. It covers future observations and session summaries across all projects using the existing shared data directory. `processing_profile` and `project_memory_profile` stay unchanged. Interactive host models are separate, and historical records are not reprocessed. The earlier Sol proposal is superseded. Repository defaults remain Luna/medium/240. No installed profile changed at this snapshot. See [the decision and history](adr/0012-review-session-summary-support.md) and [safe evidence](evidence/summary-support-2026-10-25.json).

Pi `openai-codex` reviews use the trusted bundled schema helper, exact helper/schema proof and temporary SSE/cache-off/retry-zero configuration. Missing proof or schema failure ends the review without a prompt-schema fallback. Generation and other Pi providers retain their existing behavior. The strict decoder never repairs returned text. See [Pi runtime](PI_RUNTIME.md) for the acceptance boundary.

## Post-acceptance6 snapshot

This section records October 26, 2026, after three narrow repairs and before final validation of the new candidate. Earlier October 25 snapshots remain historical.

The historical acceptance6 run on commit `49a49793ff8f3e028485d4112628741a3b862d8a` completed and failed. It produced five of ten required useful outputs and both optional supported outputs. All four positive controls and all ten actual-target negative controls passed. No materially false persisted claim was established within the finite supplied context. Five missing required useful outputs still fail acceptance. These results do not establish general accuracy, model equivalence or real native host capture and injection.

The run used Codex/openai/gpt-6-luna/max/600 and Pi/openai-codex/gpt-6-luna/max/600. The aggregate records 65 native submissions, 61 independent executions and 24 synthetic submissions. It reports 558,193 input tokens and 301,900 output tokens, totaling 860,093. Actual cost and internal HTTP-attempt counts are unknown. These counts belong to the historical source, not the later uncommitted repairs.

The subsequent owner-schema, missing-summary correction and generic wording repairs passed 34, 25 and 22 focused tests respectively. Four prepared public packets were read back after the wording change. The checks use authored inputs and verdicts and do not establish semantic improvement. The latest pre-document source freeze is `62b972b54dcb3337eab26a5a27a95427e42bac7307ecfbdc68cb3a1c53ed7bea`. The failed historical run did not exercise these new source bytes. Final full CI and independent review for this candidate remain pending at this October 26 snapshot.

The Luna/max/600 `observation_profile` proposal remains conditional and unapproved. It covers future notes and summaries across the existing shared data directory. Other processor profiles and interactive host models remain separate. These repairs did not change installed configuration, historical user records or production jobs. Synthetic disposable test jobs are outside that statement. Installed configuration was not reread in this documentation stage. Acceptance6 authorization was consumed. No fresh native batch is authorized. A future protocol needs separate direct authorization. Merge, installation, startup-timeout verification, actual Pi-language validation and next-session native memory injection remain open.

## Historical v6 verification snapshot

The earlier v6 keyed contract had 157 unique focused offline tests green, with zero skips. It repairs deterministic unit coverage. No semantic-overrejection improvement or native compatibility with the exact v6 schema is demonstrated, including its maximum size. At that checkpoint, source remained `summary-source-v4` and outcome remained `summary-outcome-v5`. Current contract changes are recorded in [ADR 0014](adr/0014-preserve-plans-and-single-review-citations.md). Helper bytes, execution profiles, call budgets, correction limits and deadlines are unchanged.

Historical acceptance7 on commit `1deb4737f12b178c6307bd48da11baeba5dcce21` used v5. It produced eight of ten required useful outputs and both optional outputs. All four positive controls and ten actual-target negative controls passed. Acceptance still failed. No materially false persisted field was established within that constructed regression set. Its 79 native submissions and 1,095,461 reported tokens do not establish v6 quality, general accuracy or model ranking. Earlier v5 runs remain historical.

At this snapshot, full CI, independent reviews, native v6 compatibility and semantic validation, and installation remain pending. Preparation covers Luna/max through Codex and Pi and the configured Pi GLM 5.3 alternative. No next native run is authorized. No setting, installed copy or profile destination was newly selected or changed. The conditional observation-profile proposal is not release approval or global-profile consent.
