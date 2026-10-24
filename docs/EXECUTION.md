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

Every generated session summary receives mandatory original-source support review through the selected Codex or Pi profile, even when Jev quality is disabled. The `summary-support-v3` request supplies complete nonempty field units and full disclosed current/history evidence with source roles. Its flat response compares candidate and evidence meanings, values, attribution and scope. Code requires every unit, consistent coverage, one to eight comparisons, bounded text and valid source-span references. Mechanical coverage does not establish semantic truth.

After support passes, `summary-source-v2` interprets only the latest original assistant report in the current batch. It receives no candidate, user request, history or derived memory. At most eight content propositions and two observations about the report's language and organization retain subject, time and scope. Observed slots require values; unknown and inapplicable slots cannot become propositions. Jev lifecycle-only envelopes are excluded. No current original report means no derived calls and `not_assessed`, with no historical fallback.

The separate `summary-outcome-v2` call compares each complete outcome-capable field with every retained proposition. Code owns all pair IDs and joins, at most fifty rows. Only an explicit conflict about the same report, subject, time and scope vetoes a candidate. A later acknowledgement cannot establish attributes of an earlier answer. Missing, unknown or planned evidence cannot support or veto. A no-conflict result is not independent support. All-unavailable propositions skip this call; mixed unavailable pairs require `not_assessed`.

Generation, reviews and the optional quality gate share one deadline and one generation correction. Every call retains the same runtime, provider, exact model and reasoning. The default timeout remains 240 seconds. Changed corrections repeat applicable support and outcome review while reusing fresh source interpretation. An exact rejected summary with unchanged evidence, interpretation, profile and contract versions retains its first rejection without another model call. The cache is local to the active attempt. Notes outside the summary do not reset it. Shape, summary preservation and lease/source freshness are checked before reuse. This conservative rule can preserve a mistaken first rejection.

Failure reasons include `summary_support_invalid`, `summary_support_unavailable`, `summary_support_uncertain`, `summary_support_rejected` and `summary_support_stale`. Store captures retained-record identities with the exact current and historical claim views, before historical excerpting. The processor checks those identities before generation and on every freshness guard. Metadata, source links and hydrated current tool I/O remain part of the identity. Store repeats the comparison inside the output write transaction, with no model or processor callback. Deletion or mutation before the initial check or after the final check blocks the whole packet. The separate cross-session `project_context` is a reference for generation and optional quality. It keeps its existing existence checks and cannot replace missing support. It is excluded from this content fingerprint and mandatory support evidence. Failed packets retain raw work and measured usage, with no partial output. Semantic rejection and uncertainty quarantine their batch while fresh later work continues, including after retained-service restart. Malformed, unavailable, stale and unknown operational failures keep the existing project-blocking policy.

The processor checks captured identities before Jev filtering, before cached decisions, before each live chunk after its HTTP-slot wait, and after selection. Both native runtimes check selected evidence immediately before initial and correction turns and each review submission. Optional Jev quality forwards the same identity check through its existing callback and HTTP-slot wait. Denied initial submission records no fabricated call or usage. A later denial retains earlier measured usage. Successful late review after lease expiry keeps the typed expiry outcome in both runtimes, retains owned call metrics and identifiers, and leaves ordinary retained-service recovery nonblocking. Actual malformed native records still fail as protocol errors. Local dispatch errors keep their fixed reason instead of becoming transport errors.

These guards use the existing absolute deadlines. They detect changes visible at each check. They do not revoke an already submitted request or prevent changes after a guard. The final write fence remains atomic. No database lock or transaction spans a model call.

Fixed invalid subtypes are `structure`, `unit_coverage`, `unknown_span`, `verdict`, `bounds`, `unknown_proposition` and `duplicate_reference`. Receipts and per-call diagnostics contain these categories, protocol version, status and fingerprints. They contain no raw source or review prose. Old stored summaries keep their shape and `verification=not_assessed`. Old attempt records remain unchanged. Legitimate relevance skips remain valid, and useful summaries do not require a new durable note.

`Store.observation_attempts` exposes the additive `execution_calls` JSON column. It holds generation, up to two original-support calls, one source interpretation and two outcome calls. Cached rejection or unavailable derived evidence uses fewer calls. Every call keeps requested and observed identity, outcome and measured metrics separately. Cumulative generation snapshots replace earlier snapshots. Independent counters add once, missing counters remain null, and attempt duration counts nested review time once. A running call label after a crash means no final receipt was observed.

For deterministic tests, pass separate `runner` and `summary_reviewer` implementations. An injected generator without a reviewer cannot start a live reviewer. Canned executable and installed-Pi local-provider checks prove transport and lifecycle behavior only. The third known-pair native development smoke rejected two false variants and stored two supported variants. Original support alone still missed both false claims. Earlier failed runs remain failures. These development results do not establish final private acceptance. Integration and deployment require a separate private acceptance run. See [the decision, evidence and limits](adr/0012-review-session-summary-support.md).
