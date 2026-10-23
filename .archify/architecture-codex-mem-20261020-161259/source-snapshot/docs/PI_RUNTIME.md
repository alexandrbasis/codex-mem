# Pi for background memory

Both processors can use a separate Pi subprocess. Select `runtime: "pi"` in `processing_profile` for both, or use `observation_profile` and `project_memory_profile` independently. The observation override also controls session summaries.

The supported CLI package is `@earendil-works/pi-coding-agent` version **1.4.0**. The adapter checks this exact version under the attempt deadline. Other versions fail before the prepared input is sent. Pi is an optional executable on `PATH`; Codex Mem does not install or update it.

## Profile and authorization

Use the existing five-field execution profile. Supply exact provider and model identifiers. Before catalog discovery, the adapter runs `pi auth check --provider <provider> --json --no-refresh`. It never requests `--credentials`. A missing provider and unavailable native authorization have separate failure reasons. This read-only check does not establish remote acceptance of a credential. The adapter then reads Pi's available catalog, selects the exact pair with RPC `set_model`, and reads the model's available thinking levels. It sets the requested level and checks the returned session state before sending evidence. It repeats the state check after completion. There is no fuzzy model dispatch or fallback to Codex.

The profile value `none` means Pi's `off`. These are explicit names for disabled reasoning. Other levels must appear exactly in `get_available_thinking_levels`. A `null` level fails because it cannot establish an exact requested setting.

Credentials remain in Pi's native authorization system. The adapter uses `PI_CODING_AGENT_DIR`, or Pi's standard `~/.pi/agent` directory, only to locate `auth.json` and `models.json`. A temporary agent directory links these two files. Pi reads them through its normal authorization and model loading code. Codex Mem does not copy credentials into its configuration, database, arguments, receipts, or logs. Pi can refresh its own authorization during a real call.

Only built-in providers and providers registered by native `models.json` are supported. Provider extensions are not enabled. An extension-only provider fails catalog validation before dispatch. The adapter does not discover extensions or add a new extension field to the execution profile.

## Attempt boundary

Each attempt gets a new process, empty working directory, temporary agent directory, and in-memory session. The child receives the prepared evidence and output schema. It does not receive a project path as its working directory. It has no memory-write interface.

The adapter disables tools, MCP, all extension discovery, skills, prompt templates, themes, context files, automatic compaction, and automatic retries. It supplies a fixed system prompt. It never loads the capture extension. Temporary directories are removed and the owned child process group is terminated on success, failure, timeout, or cancellation. Both output pipes and the whole operation have finite limits. Cleanup also handles a disconnected child whose process group already exited.

These settings restrict model capabilities. They do not provide an operating-system sandbox. Native provider configuration and the installed Pi executable remain trusted local code.

A `prompt` response must say `started`. Acceptance alone does not establish completion. The adapter consumes the event stream until `agent_settled`, requires `agent_end` and one completed assistant response with `stopReason: "stop"`, then checks the idle session and its exact identity again. The native system frame must contain only the fixed preamble and temporary working directory. Added tools or later system instructions reject the candidate. Cancellation, provider failure, tool blocks or tool events, rerouting, automatic restarts, unexpected events, malformed records, output overflow, and disconnects reject the whole candidate.

The adapter permits one correction. The correction uses the same process, profile, overall deadline, and cumulative attempt usage. A second correction request fails. Existing observation and project-memory JSON, schema, reference, role, evidence, source-custody, revision, and atomic-commit checks still decide whether to store the candidate. Source deletion or a replaced lease can invalidate a completed answer.

## Receipts and usage

The attempt ledger adds `execution_receipt` without changing historical rows. It contains bounded Pi facts: checked CLI version, protocol, Pi session ID, local generation number, observed settlement and agent end, assistant stop reason, identity checks, and observed tool calls. `tools_policy: "cli_disabled"` records configured CLI restrictions. It does not claim a tool inventory was enumerated: Pi 1.4.0 RPC has no such command. A generation number is a local correlation number, not a Pi-issued turn ID. Runtime-reported identity does not reveal routing inside a remote provider.

No Codex thread-start or MCP-inventory receipt is manufactured for Pi. The original requested profile and separately observed identity remain in the existing attempt columns. Historical attempts have no Pi receipt. Receipt validation keeps arbitrary model text and diagnostic prose out of the ledger.

`pi_assistant_usage` identifies Pi's provider-reported assistant counters. The observation ledger records these as `pi_runtime_total`, its existing Pi provenance value. Streaming updates replace the current generation's cumulative counters. Completed generations are summed once. Pi's uncached input, cache-read input, and cache-write input form normalized input tokens. Reasoning tokens already belong to output tokens and are not added again. Missing generations make the total partial. Failed or rejected candidates retain measured usage. Omitted optional counters remain null, including when a correction omits a previously available counter. Late receipts after lease recovery follow the same rule. Measured zero remains zero. Observation checkpoints preserve measured totals before validation and commit.

Pi initializes absent provider usage with zero counters. Those all-zero placeholders remain unknown because this protocol cannot distinguish them from measured zero. Provider cost fields are not billing evidence and are not saved as charges. Existing pricing estimates remain separate.

## Evidence and limits

Installed documentation states: "A successful `prompt` response means the prompt was accepted, queued, or handled." It also states: "The top-level `usage` is the latest cumulative provider-reported usage for the assistant response." These support settlement-based completion and replacement of streaming counters.

The owning sources are Pi's [CLI reference](https://github.com/earendil-works/pi/blob/v1.4.0/packages/coding-agent/docs/cli.md), [RPC commands](https://github.com/earendil-works/pi/blob/v1.4.0/packages/coding-agent/docs/rpc-commands.md), [event stream](https://github.com/earendil-works/pi/blob/v1.4.0/packages/coding-agent/docs/json.md), and [message types](https://github.com/earendil-works/pi/blob/v1.4.0/packages/coding-agent/docs/message-types.md). Local package hashes and source locations are in [PI_RUNTIME_EVIDENCE.json](PI_RUNTIME_EVIDENCE.json). The installed package is the verified source. A documentation search returned no matching versioned result, so web availability of these URLs is not established here.

Ordinary tests replace only the external executable. They use real temporary stores, public host capture, processing, service, and context interfaces. Run the installed CLI tests separately:

```sh
CODEX_MEM_PI=/absolute/path/to/pi python3 -m unittest discover -s tests -p 'test_pi_*.py' -k installed -v
```

These tests use a deterministic local HTTP provider, the installed Pi CLI, and real observation and project-memory processing. They check actual protocol compatibility, disabled tools, resource exclusion, and unknown usage. They make no live provider call. They do not establish live model quality, the four-way host/runtime matrix, deployment, or provider-extension compatibility. Issues #13 and #14 own live matrix and quality evidence.
