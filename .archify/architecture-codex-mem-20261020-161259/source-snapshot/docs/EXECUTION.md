# Background execution profiles

An execution profile selects a runtime, provider, exact model, reasoning level, and timeout. The interactive host is a separate choice. The shared contract connects `NativeProcessorRunner` to Codex. Observation extraction, summaries, project memory, reconsideration, and consolidation use the configured profile.

Each processor resolves its own override. Saving a Pi profile does not activate Pi. Profile changes do not reprocess successful sources.

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

The current boundary cannot verify custom providers or a `null` reasoning setting. These profiles fail before model dispatch, as does an unavailable exact model or effort. Pi returns an unavailable-runtime failure. No runtime, provider, model, or reasoning substitution occurs. The job remains pending as failed work with a diagnostic and can be retried after correction. Catalog evidence establishes advertised support; it does not guarantee a successful live call.

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

The Codex adapter preserves completion evidence, tool restrictions, model checks, strict decoding, one correction, and cumulative usage. Each processor still validates source roles and references before its atomic commit. A failed or expired attempt cannot commit a partial packet. The new tests use a deterministic external app-server executable. They do not establish live model quality or Pi compatibility.
