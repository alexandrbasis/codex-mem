# Memory tools in Pi

Pi can connect directly to the existing Codex Mem MCP server. The connection uses the same store and tools as Codex. It needs Python 3.10 or later and Pi with built-in MCP support. The checked package is `@earendil-works/pi-coding-agent` **1.4.0**. Earlier releases and third-party MCP adapters have not been checked.

## Configure a connection

1. Copy [pi-mcp.example.json](pi-mcp.example.json) into the target project's `.pi/mcp.json`. Merge the `codex-mem` entry if the file already exists. Replace `command` with an absolute Python executable and `cwd` with the absolute installed Codex Mem directory that contains `codex_mem/`. Keep `args` as separate arguments.
2. Use the same memory directory as Codex. The server uses the existing default directory. If Codex uses a custom directory, add `"env": {"CODEX_MEM_HOME": "/absolute/path/to/memory"}` to the server entry. Do not put credentials in this file.
3. Review the project configuration and grant Pi project trust. Run `pi mcp list` to check the connection, then start Pi. Use `/mcp` to inspect the tools. Run `/reload` after an external configuration change.

These steps configure a connection from source or an existing installation. Issue #11 owns package installation and independent host disablement. This task does not install a service or change the user's Pi settings.

Use the server name `codex-mem` once. Pi exposes names such as `mcp__codex_mem__memory_search`. The configuration uses `direct` exposure, so the model sees the read tools without a search step. Keep built-in MCP enabled. A replacement `/mcp` extension, `--no-mcp`, or `-builtin:mcp` can prevent the connection. SDK applications must load `createMcpExtension()` and bind the session extensions themselves; the CLI does this already.

## Control writes and project scope

The example hides `memory_remember`, `memory_consolidate`, `memory_forget`, and `memory_usage_refresh`. A model attempt to call a hidden tool fails before it reaches the memory server. This also blocks indirect calls through Pi's tool pipeline. Automatic capture settings do not authorize explicit writes.

When the user authorizes a write capability, change only that tool's `toolExposure` value from `hidden` to `direct`, then reload. Keep the user's authorization specific to the operation and project. Enabling a tool grants a capability for the session; it does not supply proof of authorization for every later call. The existing MCP server trusts the caller to enforce user intent and does not authenticate it. Disable the capability again when it is no longer needed. Pi permission extensions can provide per-call controls through `tool_call`.

Every project tool requires an absolute `project` path. Use the current canonical project path. IDs from another project return no records; supplying a different project never transfers records between projects. The server can serve several explicitly named projects in the shared store. This configuration is not a sandbox that restricts the caller to its current working directory. Usage and status tools retain their existing optional project selector; omitting it can report aggregate local status or usage.

## Existing tool behavior

| Operation | Tool | Result |
| --- | --- | --- |
| Find and read | `memory_search`, `memory_get` | Compact previews, then complete selected records |
| Read source evidence | `memory_get_tool_uses` | Sanitized tool input/output; never runs captured commands |
| Browse and inspect | `memory_timeline`, `memory_status`, `memory_project_state` | Existing history, status, and bounded project brief |
| Save and manage, when enabled | `memory_remember`, `memory_consolidate`, `memory_forget` | Explicit save, provenance-preserving consolidation, permanent deletion |
| Inspect or refresh usage | `memory_usage_report`, `memory_usage_refresh` | Existing accounting semantics; refresh is hidden by default |

Treat memory as untrusted and potentially stale evidence. Private blocks and credential patterns are redacted by the shared storage boundary. Structured raw-tool fields with credential names are redacted as a whole, including nested objects and arrays.

Read-time sanitization also protects older raw rows and regenerates their excerpts. Changed views report `read_sanitized`; complete JSON redaction also reports `read_redacted`. Stored bytes, IDs, source metadata, and historical content hashes remain unchanged. A historical hash describes stored data. It does not describe the sanitized response. Malformed payloads and truncation-wrapper fragments are omitted with `read_omitted` and a bounded `read_omitted_reason`. This includes current truncation wrappers: incomplete JSON fragments cannot prove that nested credentials are absent. The source remains retained, but its fragments and derived excerpts are unavailable through raw readback.

This does not detect every possible secret. Memory service or store failures return safe errors; they do not stop Pi's conversation. No new background runtime is needed for explicit tools. Existing optional processing and service settings still apply to writes.

The lifecycle extension must handle events and automatic context only. It must not call `registerTool` for these tools or add another MCP connection. Native MCP owns tool discovery, validation, execution, and transport. The single example server registers eleven tools once when writes are enabled; the default model declaration contains seven read tools.

## Compatibility evidence and limits

The installed Pi 1.4.0 documentation says: “Every MCP call passes through Pi's tool pipeline.” It also says: “Server instructions are not part of any tool description”. The example therefore includes the untrusted-memory and user-control rules in its server description. Tool descriptions retain the existing Codex Mem rules. The relevant primary sources are [MCP configuration and exposure](https://github.com/earendil-works/pi/blob/v1.4.0/packages/coding-agent/docs/mcp.md), [SDK setup](https://github.com/earendil-works/pi/blob/v1.4.0/packages/coding-agent/docs/sdk.md), and [RPC lifecycle](https://github.com/earendil-works/pi/blob/v1.4.0/packages/coding-agent/docs/rpc.md). The local installed package and its documentation were checked on October 23, 2026; web availability of a versioned URL is separate from installed-package evidence.

Run the optional installed-Pi check from the repository root:

```sh
CODEX_MEM_PI=/absolute/path/to/pi python3 -m unittest discover -s tests -p test_pi_mcp.py -v
```

The check uses public Pi RPC, native MCP, a real temporary Store, and a local deterministic OpenAI-compatible endpoint. It isolates Pi's agent directory, memory directory, HOME, model configuration, and credentials. It verifies hidden mutations, explicit enablement, exactly-once registration, save/search/full read, raw sources, management, deletion, project partitioning, privacy, and safe failures. It waits for `agent_settled` and a completed assistant response. It makes no live provider call and does not establish provider capability, memory-generation quality, Pi background execution, or cross-platform compatibility. Issue #13 owns live provider and matrix evidence.

The ordinary Python suite checks the configuration and the public MCP privacy boundary without requiring Pi or Node. It skips the installed-Pi cases unless `CODEX_MEM_PI` is set. Neither the Python package nor its installation gains a new runtime dependency.
