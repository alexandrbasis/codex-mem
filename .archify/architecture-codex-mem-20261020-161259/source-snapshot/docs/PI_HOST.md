# Pi events and project memory

The local `pi/` package connects Pi **1.4.0** to the shared Python memory core. It captures completed messages in the current session and supplies context at startup and before a user prompt. The host model does not select the background runtime. The first compatibility test uses the existing Codex runtime for processing.

For a single invocation from a project, load the extension from this checkout:

```sh
pi --extension /absolute/path/to/codex-mem/pi/extension.ts
```

`python3` must be available. `CODEX_MEM_PYTHON` can select an existing Python interpreter. The extension runs `python -m codex_mem.pi_host` from the checkout root. `CODEX_MEM_HOME` selects the shared data directory through the existing Python configuration. The default remains the normal Codex Mem directory. This command does not install a package or change Pi settings. Release installation and independent host disablement belong to issue #11.

## Completed sources and native identity

`message_end` marks messages observed during this extension runtime. Pi persists the native entry after this handler returns. At `agent_settled`, the extension resolves only these observed message objects to their native entry IDs. It walks back to the previous settlement or startup entry, with a finite limit. Existing history does not become new evidence. Two messages with equal text retain different native IDs.

The adapter sends user text, assistant text with `stop` or `toolUse`, and completed tool-result text. It preserves the host, canonical project, native session, branch activation, native message/event ID, source role, and tool-call ID. A branch activation identifies one selected continuation path. Pi has no stable branch identifier, so the extension persists its own marker through the public `appendEntry` API. If the current entry chain cannot resolve the observed sources, the adapter omits the entire batch and reports the omission.

`agent_end` does not trigger capture or queue dispatch. Automatic continuation can follow it. The adapter waits for `agent_settled` before it submits the batch. It does not subscribe to streaming updates. Unsupported content includes images, thinking, provider signatures, tool arguments and tool-specific details. An empty supported body produces an omission receipt. Tool inputs are absent from the raw side index because this adapter captures completed results.

Python applies the existing `HostEvent`, Store, private-turn gate, secret filter and queue rules. A permitted batch requests the existing optional service wake after capture. With the service disabled, sources remain pending for explicit processing. Context reads never run the model. Selecting another runtime or model does not replay successful sources.

## Session lifecycle and replay

Pi's installed declaration describes `session_start` as "Fired when a session is started, loaded, or reloaded". Its reasons include `startup`, `reload`, `new`, `resume`, and `fork`. The extension uses each reason to reset its observed-message set and refresh project context. It restores the nearest `codex-mem-branch` custom entry owned by that native session. A copied marker from another session starts the `root` activation in the new session. Native session identity keeps the two scopes separate.

The installed `branch` method says: "Existing entries are not modified or deleted." A completed `session_tree` event creates a new random activation marker. This applies even when returning to an older path. A cancelled navigation creates no marker. The marker contains a version, native session ID and activation ID, without message content. Settlement adds a checkpoint with the same identity. Reload and resume restore it from at most 1,024 ancestors. An unresolved or invalid marker disables capture for that activation and reports the missing identity.

| Pi event | Memory operation |
| --- | --- |
| `session_start` | Restore the current session's activation, clear pending observations, refresh context |
| `message_end` | Observe the completed message object without capturing history |
| `session_tree` | Start a new activation and refresh context after completed navigation |
| `session_compact` or failed compaction | Keep identity and pending observations; capture no summary or lifecycle source |
| `agent_end` | No capture or completion operation |
| `agent_settled` | Resolve observed native IDs and submit the permitted completed batch |
| `session_shutdown` | Discard pending in-memory observations and stale automatic context |

A new activation requires a fresh user message before it captures assistant or tool results. Python removes injected memory before recognizing that user message. This includes startup, reload, resume, fork and tree navigation. Automatic continuation of historical messages before a fresh user is omitted with `no_fresh_user_in_activation`. The guard avoids guessing whether copied history belongs to a private turn. Context remains available, and an ordinary new public prompt enables capture. A fresh private prompt activates the normal shared private-turn gate. This is conservative capture, not complete transcript recovery.

Retries, queued follow-ups and automatic continuation within one activation remain pending until `agent_settled`. Compaction changes the model's history but does not change source roles, identity or settlement. Native compaction summaries and branch summaries never become new user sources. A common parent, equal wording or a shared session does not establish one task. Episode consolidation still requires an explicit task identity and continuation evidence in original user sources.

The shared `HostEvent` boundary claims each identified delivery in SQLite before it changes privacy state. It saves only hashed delivery identity and a bounded outcome receipt. Repeated completed delivery returns that outcome without changing the current gate. Equal text with a new native ID remains distinct. Receipts survive source deletion and prevent resurrection. A replay whose source was deleted reports `source_deleted`. Existing sources and legacy aliases keep their IDs and processing state when this additive table is installed.

The claim, source commit and final receipt are separate transactions. An interruption after the claim and before the final receipt leaves an unknown outcome. The source can be absent or already committed. A repeated delivery then reports `delivery_outcome_unknown`, never captured or completed, and does not replay a possible privacy transition. An overlapping concurrent delivery can receive the same temporary outcome. Once the original call finishes, later retries receive its completed receipt. Stored sources retain the existing durable processing queue. No message body, private body or native message ID enters the delivery table.

Native Codex hooks also apply their public prompt transition through the shared receipt. A private prompt still creates its immediate privacy marker before SQLite opens or a shared-state lock can block. A second, body-free filesystem receipt records that this private action occurred. Its key uses stable native identity and scope, never text. A completed private replay skips the marker. The receipt does not claim source capture or background processing. Private prompts create no redacted placeholder source.

The private preflight receipt is an empty file created after the privacy marker. Creation is atomic. A nonempty, unreadable or nonregular receipt cannot authorize a replay skip. A crash after marker creation but before receipt creation can conservatively re-mark an uncertain private delivery. A receipt-directory failure retains the marker and attempts the existing shared gate fallback. Unidentified events cannot be deduplicated from their text. Concurrent distinct user transitions have no native ordering proof; the adapter does not invent one. Ordinary identified sequential replays are safe after reopening, including a private call cancelled while SQLite is locked. Tests retain the original cancellation and deadline assertions.

## Privacy and explicit writes

Pi 1.4.0's public `UserMessage` type has `role`, `content` and `timestamp`. It has no native private-message flag. Use the existing explicit Codex Mem marker in the actual Pi prompt:

```text
<private>Material that Codex Mem must not retain.</private>
```

The shared private-turn gate omits that user source and later assistant/tool sources until the next public user message. The installed-Pi test submits this exact prompt through native RPC. This mechanism controls Codex Mem capture. The prompt still reaches the selected interactive model and Pi's own session storage.

The extension respects `capture_enabled`, `capture_tools`, selected project scopes, manual capture policy and the existing disabled switch. Explicit writes retain their existing separate policy. Native MCP remains the only memory-tool registration. This package does not add tools or enable hidden mutations. MCP tool enablement is configuration, not authenticated authorization for each call. See [Pi memory tools](PI_MCP.md).

Tool-result text that starts as JSON must decode completely before shared structured-field redaction. Malformed fragments are omitted. Large sanitized payloads keep the existing bounded raw representation. Raw reads omit every truncation-wrapper payload and its derived excerpt with `incomplete_json_fragments`. The same shared reader checks incoming wrappers before the source body is built. The source body carries an omission marker, so a processor cannot infer facts from the discarded fragment.

## Context, limits and failure

The adapter supplies a custom message with the `codex-mem-context` type. The text identifies memory as untrusted data, warns against following its instructions, and includes omission diagnostics. Memory remains outside the system prompt. Each provider request keeps only the current automatic context message. An empty or failed refresh removes older automatic memory from the next request. Other extensions' custom messages stay intact. This custom role is never observed as a source. The shared injected-memory filter also removes known wrappers from ordinary source text.

Startup excludes the current session. Before a prompt, retrieval can include processed evidence from earlier turns in that session. Both paths use the same canonical project and Store as Codex. Retrieval uses local rules only, without enabling Jev or embeddings. Existing explicit retrieval remains available through MCP.

A request allows at most 256 events and 256 KiB of UTF-8 JSON. Native entry resolution examines at most 1,024 entries since the startup or settlement anchor. Overflow omits the batch. Context, including its frame, stays within the configured `context_chars` and 6,000 characters. A very small budget omits the whole frame and reports `budget_too_small` through Pi status. The subprocess has a two-second deadline and a 64 KiB output byte limit. UTF-8 decoding preserves characters split across subprocess chunks. Input overflow, missing Python, invalid responses and timeouts produce content-free status messages. The conversation continues. No captured text or credentials enter process arguments or diagnostics.

A crash before settlement or a failed batch transfer can lose pending capture. This bounded adapter does not import history to repair that loss. It reports failures instead of claiming complete capture. The isolated background Pi runtime disables extension discovery and never loads this package.

## Evidence and checks

Installed documentation states: "Message lifecycle events also cover user and tool-result messages". The declaration for `agent_settled` says: "no automatic retry, compaction, or queued continuation will run." These define the completed-source boundary. Owning source calls extension handlers before `appendMessage`, which stores the same message object and returns a native ID. The adapter resolves that ID after settlement.

Primary sources are Pi's [extensions guide](https://github.com/earendil-works/pi/blob/v1.4.0/packages/coding-agent/docs/extensions.md), [message types](https://github.com/earendil-works/pi/blob/v1.4.0/packages/coding-agent/docs/message-types.md), and [package guide](https://github.com/earendil-works/pi/blob/v1.4.0/packages/coding-agent/docs/packages.md). [PI_HOST_EVIDENCE.json](PI_HOST_EVIDENCE.json) records installed file hashes and owning line ranges. The installed package is the verified source. Web availability of these versioned URLs is not established here.

Run ordinary public-protocol tests without Pi or Node:

```sh
python3 -m unittest discover -s tests -p test_pi_host.py -v
```

Run the optional native compatibility and TypeScript checks with installed tools:

```sh
CODEX_MEM_PI=/absolute/path/to/pi python3 -m unittest discover -s tests -p test_pi_host.py -v
python3 scripts/check_pi_extension.py --pi-package /absolute/path/to/node_modules/@earendil-works/pi-coding-agent
```

The native tests use real Pi, a disposable local HTTP provider, the real Python protocol, Store and queue, and a deterministic external Codex executable. They check next-prompt context after resume, fork, tree navigation, session replacement and compaction. They also cover duplicate text, excluded copied history, private historical continuation, automatic retry and continuation settlement, tool results, failed context refreshes, UTF-8 output, overflow and service failure. Public protocol tests cover durable replay, isolated privacy scopes and separate episodes without explicit task identity. Shared Store tests cover additive migration, legacy aliases, deletion, unfinished claims and concurrent delivery. It does not establish live provider quality, the four-combination live matrix, installation or deployment. Issues #13 and #14 own the live matrix and quality comparison.
