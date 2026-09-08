# Upstream and implementation choices

Codex Mem is an independent implementation inspired by [Claude Mem](https://github.com/thedotmack/claude-mem). It is not a GitHub fork and does not include copied Claude Mem source. This repository's implementation uses the MIT license in `LICENSE`.

Version `1.3.1` separates captured hook evidence from searchable notes and improves observation filtering and source attribution; see the [public verification record](docs/VERIFICATION.md) for the evidence and compatibility boundary.

The reviewed upstream is [v13.24.1, commit f6f72747e1298aef37b1baccefc11ce7305d7bf8](https://github.com/thedotmack/claude-mem/tree/f6f72747e1298aef37b1baccefc11ce7305d7bf8). Review date: 2026-09-08. Its package metadata declares Apache-2.0. If future changes copy upstream code, retain the applicable upstream license, NOTICE, attribution, and modification notices for that code.

The observation-quality review for `1.3.1` additionally checked [observer rules](https://github.com/thedotmack/claude-mem/blob/fd0ecf023336ce631c8a5cd7b70cdeca8f0e82e0/plugin/modes/code.json) and [prompt construction](https://github.com/thedotmack/claude-mem/blob/fd0ecf023336ce631c8a5cd7b70cdeca8f0e82e0/src/sdk/prompts.ts) at upstream commit `fd0ecf023336ce631c8a5cd7b70cdeca8f0e82e0`. The adopted principle is to retain durable findings and skip routine activity before retrieval. Raw evidence stays local for audit; failed jobs are preserved for explicit retry. No upstream source was copied.

## What carries over

The product workflow remains capture, compact notes, retrieval, and context for a later session. Search first returns previews; full records and source history are fetched when needed. Projects and sessions label every captured event.

## What changes

| Area | This implementation |
| --- | --- |
| Agent integration | Native Codex lifecycle hooks, an MCP stdio server, and memory/maintenance skills. |
| Runtime | Python 3.10+ and SQLite FTS5; optional FastEmbed/ONNX Runtime on Python 3.10–3.13. A local background service drains a durable queue. No HTTP listener or database daemon. |
| Compression | A local queue service processes bounded observation batches in fresh Luna/medium sessions. Explicit user-authored notes remain available through MCP. Source records and execution receipts are retained. |
| Retrieval | Unicode full-text, local multilingual embeddings, cosine similarity and reciprocal rank fusion; project scope and bounded previews. |
| Privacy | Common credential patterns and private blocks are redacted before persistence. Tool result excerpts are redacted and capped at 2,000 characters; transcript scanning is omitted. |
| Data control | Exact-ID deletion, local backups, explicit retention, and optional import from a selected legacy project. |
| Failure handling | Memory failures do not block a coding task. Failed queue work remains blocked until explicit retry; completed work and pending jobs survive a service restart. |

The runtime needs no separate AI provider configuration. Automatic processing uses the current Codex account's allowance in separate Luna/medium sessions; explicit MCP notes can be authored in the main task. There is no measured claim that lexical search beats vector search, or that this implementation preserves every upstream feature.

## Codex contract

The integration was checked against Codex CLI 0.153.4 and [official source at 3d2ee51ca2d5db578f328aa75e20aa22c0197c9a](https://github.com/openai/codex/tree/3d2ee51ca2d5db578f328aa75e20aa22c0197c9a).

The [hook documentation](https://learn.chatgpt.com/docs/hooks) describes lifecycle input and output. The plugin uses default `hooks/hooks.json` discovery and requires review of the installed hook definitions. It reads event payloads directly because transcript file formats are not a stable hook contract.

The [pinned MCP parser](https://github.com/openai/codex/blob/3d2ee51ca2d5db578f328aa75e20aa22c0197c9a/codex-rs/codex-mcp/src/plugin_config.rs) resolves explicit relative `cwd` against the plugin root. The native plugin configuration therefore uses `cwd: "."` with a relative launcher path. This avoids relying on environment substitution that is not implemented by this parser.

Codex [clears the MCP child environment](https://github.com/openai/codex/blob/3d2ee51ca2d5db578f328aa75e20aa22c0197c9a/codex-rs/rmcp-client/src/stdio_server_launcher.rs#L269-L279). The server explicitly forwards `CODEX_MEM_HOME` and `CODEX_MEM_DISABLED` through the supported `env_vars` setting, keeping MCP and hooks on the same data configuration.

The observation worker uses the native app-server protocol. It checks the available model's reasoning levels, pins Luna/medium at thread and turn creation, and records the accepted thread parameters and execution IDs. It rejects reroutes, tool use, incomplete turns, and invalid structured output before writing notes.

The worker passes an empty environment list, which the [pinned tool planner](https://github.com/openai/codex/blob/3d2ee51ca2d5db578f328aa75e20aa22c0197c9a/codex-rs/core/src/tools/spec_plan.rs#L1073-L1083) uses when selecting environment tools. Connected MCP servers are disabled individually and their resulting inventory must be empty before observations are sent. This is layered isolation; it does not claim that Codex has a universal switch that removes every native utility tool.

The desktop upgrade test exposed a cached old plugin path in a newly created task, while an independent app-server loaded the new version successfully. The [native catalog handler](https://github.com/openai/codex/blob/rust-v0.153.4/codex-rs/app-server/src/request_processors/catalog_processor.rs#L483) clears plugin and skill caches when `skills/list` requests `forceReload`. That refresh must reach the existing process; a separate CLI process does not refresh the running desktop connection. The test report distinguishes these two hosts.

## Local semantic model

The 1.2.x implementation uses [FastEmbed 0.8.0](https://github.com/qdrant/fastembed/tree/v0.8.0) with ONNX Runtime 1.23.2 and the MIT-licensed [multilingual E5-base model](https://huggingface.co/intfloat/multilingual-e5-base). The quantized ONNX files are downloaded separately from [the original publisher at revision d128750597153bb5987e10b1c3493a34e5a4502a](https://huggingface.co/intfloat/multilingual-e5-base/tree/d128750597153bb5987e10b1c3493a34e5a4502a). Model and tokenizer SHA-256 values are pinned in `codex_mem/semantic.py`; the model is not redistributed in the plugin archive. Runtime dependencies retain their own licenses.

The encoder produces 768-dimensional vectors. Documents use the required `passage: ` prefix and queries use `query: `, including Russian input. Token-aware chunks respect the model's 512-token limit. Chunk vectors are averaged and L2-normalized; this is an initial document-level retrieval implementation, without a separate passage index or reranker. Normal inference reads verified local files; only explicit model setup downloads artifacts. Index version v2 embeds redacted title, body and tags without machine-format labels; existing v1 vectors become stale and are rebuilt.

Model selection used the same three English/Russian paraphrase queries and six records. MiniLM, E5-small and MPNet did not rank all intended records first. E5-base improved this small comparison; it is not evidence of universally correct ranking. The [public verification record](docs/VERIFICATION.md) summarizes this bounded comparison without publishing raw host receipts.

There is still no full Claude Mem feature parity: no Web UI, HTTP API, cloud sync, rich structured session-summary schema, or non-Codex host integrations. The [public verification record](docs/VERIFICATION.md) states this limitation together with the tested behavior and known partial result. Raw host receipts and the private feature comparison are intentionally omitted from the public export.
