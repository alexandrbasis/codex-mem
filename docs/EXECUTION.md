# Background execution profiles

An execution profile selects a runtime, provider, exact model, reasoning level, and timeout. The interactive host is a separate choice. The shared contract currently connects only `NativeProcessorRunner` with the existing Codex model and reasoning level.

Profile configuration and its resolver are available in this transition. Processor selection remains on legacy Codex until the queue and accounting changes in issues #5 and #6. Saving a Pi profile does not activate Pi in this version. No database migration or automatic reprocessing occurs here.

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

The current Codex adapter accepts only the legacy provider, model, and reasoning combination. It returns a configuration failure for other combinations and an unavailable-runtime failure for Pi. It does not silently substitute another runtime or model.

## CLI format

Use the existing `config --set key=JSON` format. For example, this stores the legacy choice explicitly:

```sh
codex-mem config --set 'processing_profile={"runtime":"codex","provider":"openai","model":"gpt-6-luna","reasoning_effort":"medium","timeout":240}'
```

To remove an override, set it to `null`:

```sh
codex-mem config --set 'observation_profile=null'
```

`codex-mem config` shows the saved configuration. Invalid updates fail before the file is replaced. An invalid existing file remains disabled under the existing fail-closed configuration policy. Processing switches, project selection, privacy rules, Jev, and embeddings retain their separate controls.

## Runtime boundary

`ExecutionRequest` carries bounded prepared input, an output schema, job type, profile, monotonic deadline, and attempt identifier. `ExecutionResult` separates observed identity from requested configuration. It exposes normalized failure reasons and retains the existing usage provenance. Unknown token counts stay unknown.

The Codex adapter preserves completion evidence, tool restrictions, model checks, strict decoding, one correction, and cumulative usage. Each processor still validates source roles and references before its atomic commit. A failed or expired attempt cannot commit a partial packet. The new tests use a deterministic external app-server executable. They do not establish live model quality or Pi compatibility.
