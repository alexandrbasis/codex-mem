# Pi for background memory

Both processors can use a separate Pi subprocess. Select `runtime: "pi"` in `processing_profile` for both, or use `observation_profile` and `project_memory_profile` independently. The observation override also controls session summaries.

The supported CLI package is `@earendil-works/pi-coding-agent` version **1.4.0**. The adapter checks this exact version under the attempt deadline. Other versions fail before the prepared input is sent. Pi is an optional executable on `PATH`; Codex Mem does not install or update it.

## Profile and authorization

Use the existing five-field execution profile. Supply exact provider and model identifiers. Before catalog discovery, the adapter runs `pi auth check --provider <provider> --json --no-refresh`. It never requests `--credentials`. A missing provider and unavailable native authorization have separate failure reasons. This read-only check does not establish remote acceptance of a credential. The adapter then reads Pi's available catalog, selects the exact pair with RPC `set_model`, and reads the model's available thinking levels. It sets the requested level and checks the returned session state before sending evidence. It repeats the state check after completion. There is no fuzzy model dispatch or fallback to Codex.

Pi can apply an automatic thinking level while `set_model` selects the model. The adapter permits at most one exact `thinking_level_changed` notification before that command responds. Its only fields must be `type` and `level`, with a known native level. After the response, that automatic level must appear in the newly selected model's advertised levels. The adapter rejects duplicate, extra or unknown notifications. This behavior applies to every provider, without provider-specific exceptions. The automatic level does not replace the profile's requested level.

Pi 1.4.0 can emit `thinking_level_changed` before the `set_thinking_level` response. The adapter accepts only the exact requested level at that command, then verifies session state. A mismatched notification or a reasoning change during generation rejects the attempt.

The profile value `none` means Pi's `off`. These are explicit names for disabled reasoning. Other levels must appear exactly in `get_available_thinking_levels`. A `null` level fails because it cannot establish an exact requested setting.

Credentials remain in Pi's native authorization system. The adapter uses `PI_CODING_AGENT_DIR`, or Pi's standard `~/.pi/agent` directory, only to locate `auth.json` and `models.json`. A temporary agent directory links these two files. Pi reads them through its normal authorization and model loading code. Codex Mem does not copy credentials into its configuration, database, arguments, receipts, or logs. Pi can refresh its own authorization during a real call.

Only built-in providers and providers registered by native `models.json` are supported. Provider extensions are not enabled. An extension-only provider fails catalog validation before dispatch. The adapter does not discover extensions or add a new extension field to the execution profile.

## Attempt boundary

Each attempt gets a new process, empty working directory, temporary agent directory, and in-memory session. The child receives the prepared evidence and output schema. It does not receive a project path as its working directory. It has no memory-write interface.

The adapter disables tools, MCP, all extension discovery, skills, prompt templates, themes, context files, automatic compaction, and automatic retries. It supplies a fixed system prompt. It never loads the capture extension. Temporary directories are removed and the owned child process group is terminated on success, failure, timeout, or cancellation. Both output pipes and the whole operation have finite limits. Cleanup also handles a disconnected child whose process group already exited.

These settings restrict model capabilities. They do not provide an operating-system sandbox. Native provider configuration and the installed Pi executable remain trusted local code.

A `prompt` response must say `started`. Acceptance alone does not establish completion. The adapter consumes the event stream until `agent_settled`, requires `agent_end` and one completed assistant response with `stopReason: "stop"`, then checks the idle session and its exact identity again. The native system frame must contain only the fixed preamble and temporary working directory. Added tools or later system instructions reject the candidate. Cancellation, provider failure, tool blocks or tool events, rerouting, automatic restarts, unexpected events, malformed records, output overflow, and disconnects reject the whole candidate.

Generation permits one correction. Mandatory review executions do not permit a correction. The correction uses the same process, profile, overall deadline, and cumulative attempt usage. A second correction request fails. Existing observation and project-memory JSON, schema, reference, role, evidence, source-custody, revision, and atomic-commit checks still decide whether to store the candidate. Source deletion or a replaced lease can invalidate a completed answer.

## Structured review output

Mandatory review calls using `openai-codex` load one trusted [bundled helper](../codex_mem/pi_schema_helper.js) with an explicit extension path. User and project extension discovery, tools, MCP, skills and context files remain disabled. The helper only adds `text.format` with the per-call schema supplied as data. It registers no tools or output handlers and performs no model calls. The selected model and reasoning level remain unchanged.

These review processes use temporary `transport=sse`, `cacheWarming=off` and provider `maxRetries=0` settings. This avoids the installed adapter's automatic WebSocket reconnect and SSE fallback paths during reviews. It does not change user/global settings or unrelated generation. Other Pi providers retain the existing prompt-schema behavior.

[SchemaContract](../codex_mem/pi_schema.py) requires exact helper and schema hashes, one application proof, unchanged remaining payload and matching model/provider identity. Missing or malformed proof rejects even valid JSON. There is no same-attempt fallback. The existing strict decoder still rejects duplicate keys and trailing text. Pi catches request-hook exceptions, so this is an acceptance fence, not a guarantee that a failed hook prevented an outbound request.

## Receipts and usage

The attempt ledger adds `execution_receipt` without changing historical rows. It contains bounded Pi facts: checked CLI version, protocol, Pi session ID, local generation number, observed settlement and agent end, assistant stop reason, identity checks, and observed tool calls. `tools_policy: "cli_disabled"` records configured CLI restrictions. It does not claim a tool inventory was enumerated: Pi 1.4.0 RPC has no such command. A generation number is a local correlation number, not a Pi-issued turn ID. Runtime-reported identity does not reveal routing inside a remote provider.

No Codex thread-start or MCP-inventory receipt is manufactured for Pi. The original requested profile and separately observed identity remain in the existing attempt columns. Historical attempts have no Pi receipt. Receipt validation keeps arbitrary model text and diagnostic prose out of the ledger. New receipts add structured-output mode and status. Schema reviews record `extensions_policy: "bundled_schema_only"`, exact schema/helper hashes and the per-review transport settings. Historical receipts without these fields remain readable.

`pi_assistant_usage` identifies Pi's provider-reported assistant counters. The observation ledger records these as `pi_runtime_total`, its existing Pi provenance value. Streaming updates replace the current generation's cumulative counters. Completed generations are summed once. Pi's uncached input, cache-read input, and cache-write input form normalized input tokens. Reasoning tokens already belong to output tokens and are not added again. Missing generations make the total partial. Failed or rejected candidates retain measured usage. Omitted optional counters remain null, including when a correction omits a previously available counter. Late receipts after lease recovery follow the same rule. Measured zero remains zero. Observation checkpoints preserve measured totals before validation and commit.

Pi initializes absent provider usage with zero counters. Those all-zero placeholders remain unknown because this protocol cannot distinguish them from measured zero. Provider cost fields are not billing evidence and are not saved as charges. Existing pricing estimates remain separate.

## Evidence snapshot before final validation

The results and completion statuses below describe the October 25, 2026 snapshot after the second alternative-profile comparison and before final CI4. They do not track later validation or installation.

Installed documentation states: "A successful `prompt` response means the prompt was accepted, queued, or handled." It also states: "The top-level `usage` is the latest cumulative provider-reported usage for the assistant response." These support settlement-based completion and replacement of streaming counters.

The owning sources are Pi's [CLI reference](https://github.com/earendil-works/pi/blob/v1.4.0/packages/coding-agent/docs/cli.md), [RPC commands](https://github.com/earendil-works/pi/blob/v1.4.0/packages/coding-agent/docs/rpc-commands.md), [event stream](https://github.com/earendil-works/pi/blob/v1.4.0/packages/coding-agent/docs/json.md), and [message types](https://github.com/earendil-works/pi/blob/v1.4.0/packages/coding-agent/docs/message-types.md). Local package hashes and source locations are in [PI_RUNTIME_EVIDENCE.json](PI_RUNTIME_EVIDENCE.json). The installed package is the verified source. A documentation search returned no matching versioned result, so web availability of these URLs is not established here.

Ordinary tests replace only the external executable. They use real temporary stores, public host capture, processing, service, and context interfaces. Run the installed CLI tests separately:

```sh
CODEX_MEM_PI=/absolute/path/to/pi python3 -m unittest discover -s tests -p 'test_pi_*.py' -k installed -v
```

These tests use a deterministic local HTTP provider, the installed Pi CLI, and real observation and project-memory processing. They check actual protocol compatibility, disabled tools, resource exclusion, and unknown usage. They make no live provider call. They do not establish live model quality, the four-way host/runtime matrix, deployment, or provider-extension compatibility. See the [host/runtime matrix](HOST_RUNTIME_MATRIX.md) for separate public-adapter smoke with genuine remote execution. Issue #14 owns the quality comparison.

A frozen native capability probe on October 24, 2026 passed one small-schema request using Pi/openai-codex/gpt-6-luna/low/240, with one prompt and 219 reported tokens. Its helper changed only `text.format`. That probe does not prove general schema enforcement, full review-schema compatibility or semantic quality.

On October 25, fourteen declared Sol development stages passed across Codex/openai and Pi/openai-codex, both with gpt-6.1-sol/medium/240. The shared run used eighteen native review calls, no native generation and 24 synthetic candidates. It recorded nine Pi helper proofs. Four positive outputs persisted exactly and ten negative targets rejected with zero writes. One Codex concept-negative automatic match remained unproven and needed manual semantic adjudication. These controls used `summary-support-v5`, `summary-source-v4` and `summary-outcome-v5`.

Earlier native failures remain failures. Luna passed twelve frozen controls but persisted an independently isolated false subtitle in both runtimes. Sol rejected that subtitle. These compare complete profiles, since Pi's reasoning level also changed. The first Sol diagnostic failed two supported summaries because outcome input lost original report detail. The later body-join repair passed the fourteen development stages. See [the decision](adr/0012-review-session-summary-support.md) and [safe evidence](evidence/summary-support-2026-10-25.json).

A later adapter repair handles the bounded automatic model-switch notification described above. Sixteen focused tests passed. Actual Pi/zai/glm-5.3/high metadata preparation passed in 0.481 seconds with zero prompts. The earlier `pi_unexpected_event` result remains a failed historical preflight. Metadata preparation does not establish generation, remote credential validity or semantic quality.

The first alternative comparison used Luna/high/240 in Codex/openai and Pi/openai-codex. Each profile preserved two correct outputs and rejected four false targets, but persisted the isolated false subtitle. Pi/zai/glm-5.3/high/240 rejected the false-language target, then timed out on its positive counterpart. A checker assertion about unavailable usage stopped that run after sixteen of twenty-one stages. Five stages did not run. Those failures remain recorded.

The second comparison completed all twenty-one public stages once on the repaired adapter. Codex/openai/gpt-6-luna/max/600 and Pi/openai-codex/gpt-6-luna/max/600 each passed seven stages. Each preserved two positive outputs exactly and rejected five false targets with zero writes. Pi/zai/glm-5.3/high/600 rejected five false targets and preserved the clean note. Its required language positive timed out after 600.040 seconds and wrote nothing. This exact GLM profile failed by time, without a demonstrated false persisted claim. Other GLM reasoning levels remain untested.

The run made twenty-nine native review calls and used thirty-six synthetic generation invocations, totaling sixty-five accounted executions. Twenty-eight raw replies passed strict decoding, with no structural invalidity. Two automatic concept checks remained `unproven`. Separate manual judgments satisfied their frozen semantic criteria without changing the automatic results. Known usage totals 364,186 tokens across twenty-eight calls. One call lacks usage, so the aggregate token total is null. GLM reviews retained prompt-schema decoding with no extension. Only `openai-codex` reviews used the bundled schema helper.

These are exposed controls with fixed generation and complete execution profiles. They do not establish a model-only effect, native generation, unseen private quality or installed memory injection. At this snapshot, final CI4, code review and a new private protocol were pending. At that historical snapshot, the proposal retained Luna, raised reasoning to `max` and allowed 600 seconds for observations and summaries after final acceptance and installation. The earlier Sol proposal is superseded. No installed profile changed. See [the current proposal and history](adr/0012-review-session-summary-support.md) and [safe evidence](evidence/summary-support-2026-10-25.json).

## Post-acceptance6 snapshot

The October 26 snapshot supersedes the earlier pending-acceptance status. Acceptance6 completed on commit `49a49793ff8f3e028485d4112628741a3b862d8a` and failed because five of ten required useful outputs were absent. Both optional outputs, four positive controls and ten actual-target negative controls passed. No materially false persisted claim was established within the finite supplied context. These aggregates do not prove real native host capture or later memory injection.

The earlier owner-conditioned support schema kept `summary-support-v5` and its local validator unchanged. That paragraph describes the pre-v6 repair snapshot. The first processed candidate whose reported validation reason is `missing_required_summary` can share the existing single correction with semantic repair. Other errors can remain in that initial candidate, so its correction must pass full validation. Both adapters retain at most two generation turns and the original deadline. All normal reviews still apply. Exact initial reason is returned in `generation_correction`, while durable diagnostics remain the existing five-key structural record and canonical fingerprint. No raw telemetry is added.

Later generic wording preserves supplied execution scope and candidate context within its own record. Focused tests and four prepared-packet readbacks are not native semantic evidence. The old acceptance did not exercise these uncommitted repairs. Live provider acceptance of the revised owner schema remains unverified. The profile proposal is conditional and unapproved. Acceptance6 consent is consumed and no fresh batch is authorized. Merge/install, startup-timeout verification, actual Pi-language validation and next-session native injection remain open. See [the current decision and limits](adr/0012-review-session-summary-support.md) and [safe aggregate evidence](evidence/summary-support-2026-10-25.json).

## Current support wire v7

Mandatory support now uses `summary-support-v7`. The response root has only `version` and `units`. Every exact input-unit key is required in `units`, with one to eight comparisons whose inner `unit_id` matches that key. Missing or extra keys, malformed or empty groups and mismatched owners reject the packet. The decoder sums groups and rejects totals above 64 before flattening. The schema's per-group bounds do not guarantee that sum. Existing span ownership, binding, citation, coverage and semantic-review rules remain active. Each comparison selects citations once, with an evidence or report-anchor role. A report anchor explicitly serves both roles. Code derives canonical references from the selected IDs before strict validation. Old v5 and v6 wire replies are rejected, while historical diagnostics remain readable. Outcome v6 can compare a known attributed plan without treating it as completed work. Unknown values and unresolved comparisons still block storage. See [ADR 0014](adr/0014-preserve-plans-and-single-review-citations.md).

Source-v4, the bundled helper and its proof protocol remain unchanged. Outcome now uses v6. Pi/openai-codex reviews still use the exact schema through the helper. Other providers retain prompt-schema behavior and strict decoding. Profiles, deadlines, the shared correction and seven-call ceiling are unchanged. The earlier v6 checkpoint had 157 focused offline tests, with no skips. Those tests establish only that historical contract behavior. They do not establish v7 native compatibility or semantic quality.

Historical native v5 results, including failed acceptance7, do not establish v7 compatibility or quality. At that earlier checkpoint, full CI, independent reviews, separately authorized native checks and installation remained pending. Preparation of Luna/max through Codex/Pi and configured Pi GLM 5.3 is not fresh native consent or a new model destination. No settings, authorization or installed configuration changed in this documentation stage. See [the v6 decision](adr/0012-review-session-summary-support.md) and [current contract evidence](evidence/summary-support-2026-10-25.json).
