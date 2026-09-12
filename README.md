<p align="center">
  <img src="assets/logo.png" alt="Codex Mem logo" width="160">
</p>

# Codex Mem

**Inspired by [Claude Mem](https://github.com/thedotmack/claude-mem).**

Codex Mem is local, project-scoped memory for Codex CLI and Codex desktop on macOS and Linux. It captures bounded observations, keeps explicit notes, and makes earlier project context searchable in later sessions. It is an independent implementation; it is not a fork and does not provide full Claude Mem feature parity.

## Install with your agent

**Copy this prompt** and send it to your coding agent:

```text
Install Codex Mem on this machine for Codex: https://github.com/alexandrbasis/codex-mem

Read the repository's current README, check prerequisites, and use its supported installer. Reuse an existing clone or installation when possible; preserve existing memory, settings, and other plugins.

For a fresh installation, enable automatic capture for the project I am working on, not the plugin's source folder. If no project is open, leave capture disabled and explain how to select one. Preserve the capture scope and exclusions on upgrades.

Verify plugin registration, MCP tools, hook discovery and trust, and the background observation processor using Luna with medium reasoning. Report what you verified and any remaining manual action, such as allowing hooks or opening a new Codex task. Do not report automatic capture as working without checking it.
```

## Current release

Version `1.6.0` adds compact search previews, history around an exact record, queue recovery fixes, priority for fresh usage data, and verified service refresh during upgrades. The observer remains Luna with medium reasoning. See the verification record for tested behavior and the limits of the Claude Mem comparison.

[Русская версия](README.ru.md) · [Verification record](docs/VERIFICATION.md) · [Upstream and implementation choices](UPSTREAM.md)

## What it does

- Captures bounded evidence from approved projects through native Codex lifecycle hooks. Raw hook records remain available for explicit audit, but are excluded from search, automatic context, and semantic indexing.
- Exposes `memory_search`, `memory_get`, `memory_timeline`, `memory_remember`, `memory_consolidate`, `memory_forget`, `memory_status`, and `memory_get_tool_uses` through a local MCP server.
- Uses SQLite FTS5 for lexical search. Optional local multilingual semantic search uses `intfloat/multilingual-e5-base` with FastEmbed and ONNX Runtime.
- Keeps project and session provenance, source links for consolidated notes, and bounded execution receipts.
- Runs a durable local queue. A worker processes small observation batches in fresh `gpt-5.6-luna` sessions with `medium` reasoning, then updates the local semantic index.
- Retains redacted tool input and response in a local side index, up to 64 KiB per field. Oversized fields keep marked head/tail excerpts inside valid JSON. The observer receives the retained payload, including its middle; raw records stay outside normal memory search. Images and transcript files are not copied.
- Writes observations with type, facts, narrative, concepts, and read/modified files. Substantive Stop events produce a separate session summary with the request, investigation, learning, completion, and next steps.

Automatic capture is privacy-preserving by default: the `selected` scope starts with an empty project list. Add a project explicitly before hooks can capture it. Lexical search and explicit memory tools do not require the optional semantic runtime.

## Requirements

- Python 3.10 or newer.
- SQLite with FTS5 support.
- Codex CLI with current authentication if you enable automatic observation processing. The native integration in this release was checked against Codex CLI `0.153.4`; other CLI versions are outside this verification boundary.
- macOS or Linux for the native hook and local service workflow.

The semantic layer is optional. It supports Python 3.10 through 3.13 and installs `fastembed==0.8.0` plus `onnxruntime==1.23.2`. The pinned E5-base model and tokenizer require about 300 MB and are downloaded only when you run the explicit setup command.

## Install the plugin

Clone the repository and run these commands from its root:

```sh
git clone https://github.com/alexandrbasis/codex-mem.git
cd codex-mem
python3 scripts/codex-mem.py doctor
python3 scripts/install.py
python3 scripts/install.py --apply
```

The first `install.py` invocation previews the changes. `--apply` copies the plugin, registers it in the personal marketplace, and invokes Codex plugin activation. Existing marketplace entries are preserved and the installer creates a backup before changing the registry.

Open a new Codex task after installation. Review the plugin hooks in `/hooks` and allow them according to your host policy. Installation alone does not establish hook trust. Check the registered plugin with:

```sh
codex plugin list --marketplace personal --json
```

The installer retains managed caches of older versions and updates their launchers to forward to the current managed installation. Open tasks keep working through the hook paths they already loaded; original launchers are backed up. A running Codex desktop process can keep an older catalog until it is restarted; a successful CLI activation does not prove that an existing desktop connection has refreshed.

On a managed upgrade, the installer also attempts a cooperative restart of an already running memory service. It verifies the prior owner, released lock, new owner and runtime version. Inspect `runtime_refresh` in the result: `restarted` confirms the new worker, while `restart_pending` means files were installed but runtime refresh could not be confirmed. A previously stopped service stays stopped. This service check is separate from the desktop plugin catalog.

## Select projects for capture

The default capture scope is `selected` with no included projects. Add one absolute project path:

```sh
python3 scripts/codex-mem.py config \
  --scope selected \
  --include-project /absolute/path/to/project
```

Provide the complete list in one invocation: repeat `--include-project` for each allowed project. The provided list replaces the previous include list. Included subdirectories are covered, while separate worktrees keep separate histories. To capture in every project except explicit exclusions, use `all`:

```sh
python3 scripts/codex-mem.py config \
  --scope all \
  --exclude-project /absolute/path/to/private-project
```

To keep only explicit memory writes, use `manual`:

```sh
python3 scripts/codex-mem.py config --scope manual
```

Skip named tools with `config --skip-tool TOOL_NAME` (repeat the flag for the complete replacement list). Memory tools and session-memory files are excluded automatically. A private prompt suppresses subsequent tool capture for its session until a public prompt clears the gate.

The capture setting does not interpret every natural-language request to avoid saving. Use `manual`, an excluded project, or `CODEX_MEM_DISABLED=1` when you need a hard opt-out.

## Skills

The plugin includes two skills:

| Skill | When to use it |
| --- | --- |
| [memory](skills/memory/SKILL.md) | Ask why a past decision was made, recover an earlier fix with its evidence, save a checked result, or consolidate related notes. Search previews lead to full source records before the agent relies on them. |
| [maintenance](skills/maintenance/SKILL.md) | Ask whether memory is working, why new notes are delayed, or request a repair. It checks storage, recent activity, queue/processor state, semantic coverage, and native hook discovery, then verifies authorized repairs. |

Both skills use `allow_implicit_invocation: false`; invoke `$codex-mem:memory` or `$codex-mem:maintenance` explicitly. Automatic hooks collect observations during work. The `memory` skill guides deliberate recall and curation. `maintenance` diagnoses the system that captures, processes, and retrieves those records.

For example: "Why did we choose this architecture?", "Remember the verified cause and fix", or "Check Codex Mem for this project and explain anything stuck in the queue."

The maintenance helper also runs directly from the plugin root:

```sh
python3 skills/maintenance/scripts/health_check.py --project /absolute/path/to/project
python3 skills/maintenance/scripts/health_check.py --all-projects --deep
```

It emits metadata-only JSON and leaves the memory database and configuration unchanged. `--deep` adds a SQLite integrity check. Use `--data-dir` for a custom store. Native hook trust and actual execution are separate evidence; the skill uses the installed host check for discovery. Read-only diagnosis does not run a model, retry jobs, or repair storage. Repair commands have their own effects and scope.

The report includes recorded token usage and the age of its latest event. Missing usage data remains unavailable; it is never treated as zero. If process visibility is restricted, worker liveness is `unknown`.

## Use memory in Codex

In a Codex task, ask for earlier project decisions, ask to save a decision with its evidence, or ask for a compact summary with unresolved questions. The MCP tools require an absolute project path so records remain isolated.

For a direct CLI search, use `auto` to use the available semantic index with lexical fallback:

```sh
python3 scripts/codex-mem.py search \
  --project /absolute/path/to/project \
  --query "why did the payment fail" \
  --mode auto
```

Search also accepts repeatable `--type`, `--concept`, and `--file` filters, applied before ranking in every search mode. Read retained raw tool evidence with `tool-uses --project /absolute/path/to/project --limit 5`; this command returns data and never executes captured commands.

Since 1.6.0, CLI and MCP search/timeline responses default to compact previews. They retain record IDs, excerpts, provenance and supersession without repeating full structured text. Use `--detail full`, or MCP `detail="full"`, to restore the previous preview metadata. `get` still returns complete records, including bodies and source links. Search ranking and filters are unchanged.

To inspect events around a result, replace `MEMORY_ID` with its exact ID:

```sh
python3 scripts/codex-mem.py timeline \
  --project /absolute/path/to/project \
  --anchor-id MEMORY_ID --before 3 --after 3
```

Anchor windows include the selected record and chronological neighbors, with `is_anchor` identifying it. `before` and `after` default to 5, and their sum plus the anchor cannot exceed 100. An optional `--session-id` narrows the window. A missing or out-of-scope ID returns an empty list. Anchor mode uses these counts instead of `--limit`; without an anchor, timeline lists recent history newest first. Raw and superseded records remain visible in timeline for audit.

Read the full record after selecting an ID from search results:

```sh
python3 scripts/codex-mem.py get \
  --project /absolute/path/to/project \
  --id RECORD_ID
```

`lexical` works without the optional model. Explicit `semantic` and `hybrid` modes require a ready local semantic runtime and model. Stored records are historical evidence; verify current facts before relying on them.

## Set up semantic search

Run the explicit setup command with Python 3.13 (or another supported Python from 3.10 through 3.13):

```sh
python3 scripts/semantic_setup.py --python python3.13
python3 scripts/codex-mem.py semantic status --project /absolute/path/to/project
python3 scripts/codex-mem.py semantic index --project /absolute/path/to/project
```

`semantic index` processes one bounded batch and reports the number of pending records. Run it again until `pending` reaches zero, or let the enabled queue index approved projects. The model runs on the local CPU; normal search and indexing do not upload memory text. Model setup is the one explicit download path.

## Manage the local queue

After each retained tool result and completed response, hooks queue an approved project and wake one local worker when needed. Calls return without waiting for model processing. The queue stores project scheduling state; observation text remains in SQLite. It does not install a login item.

```sh
python3 scripts/codex-mem.py service status
python3 scripts/codex-mem.py service enqueue --project /absolute/path/to/project
python3 scripts/codex-mem.py service start
python3 scripts/codex-mem.py service stop
```

Automatic processing uses the current Codex account's allowance. Disable model processing while retaining captured observations with:

```sh
python3 scripts/codex-mem.py config --no-processor-enabled
```

Use `config --no-semantic-enabled` to disable automatic indexing or `config --no-service-enabled` to disable the detached queue. A failed project remains blocked while other approved projects can continue. Inspect the failure before explicitly retrying it:

```sh
python3 scripts/codex-mem.py service retry --project /absolute/path/to/project
```

The worker uses only bounded, redacted observations from the selected project. It requests an empty environment and disables connected MCP servers individually for its processing session. This is layered isolation, not a universal switch over every native utility. The worker validates the model response and source attribution and records model and execution provenance. Memory failures return control to the main Codex task.

## Protect and manage data

The default data directory is `~/.local/share/codex-mem`. Set `CODEX_MEM_HOME` or pass `--data-dir` to use another local directory. Codex Mem does not modify Codex's built-in memory, `AGENTS.md`, Claude Mem settings, or the Claude Mem database.

The redaction filter covers `<private>...</private>` blocks and common token, password, and key formats. It cannot identify every secret. Keep the data directory accessible only to trusted local processes; project isolation does not replace operating-system permissions.

```sh
python3 scripts/codex-mem.py status
python3 scripts/codex-mem.py backup /absolute/path/codex-mem-backup.sqlite
python3 scripts/codex-mem.py prune --days 90
```

`prune` removes old records from all projects. Backups and context already sent to a chat are separate copies, so review your retention policy before pruning.

## Import selected Claude Mem records

The optional importer reads one named project from a legacy Claude Mem SQLite database. Without `--apply`, it only previews the import:

```sh
python3 scripts/codex-mem.py import-claude \
  --database /absolute/path/claude-mem.db \
  --legacy-project exact-old-project-name \
  --project /absolute/path/to/project
```

Add `--apply` only after reviewing the preview. The source database is opened read-only, repeated imports skip records already transferred, and an unknown schema stops the import. The importer does not scan every legacy project automatically.

## Verify or remove the plugin

Run the local unit suite and doctor check from the repository root:

```sh
python3 -m unittest discover -s tests -v
python3 scripts/codex-mem.py doctor
```

`doctor` checks Python, SQLite FTS5, and local plugin files. Hook trust and host catalog state require inspection in Codex. See [Verification](docs/VERIFICATION.md) for the tested boundaries and known partial result.

Remove the registered plugin with:

```sh
codex plugin remove codex-mem@personal
```

The local database remains on disk after plugin removal. Delete it separately only after reviewing backups and retention needs.

## Known limitations

Codex Mem does not provide full Claude Mem parity. There is no Web UI, HTTP API, cloud synchronization, or non-Codex host integration. The E5 comparison covered a small fixed set of multilingual and paraphrase cases; it is not a universal retrieval-quality claim. One older contradictory recall fixture remains partial or undetermined in the verification record. The usage ledger does not yet establish the token cost of the ephemeral observation worker, so it cannot establish net memory-system savings.

Read [Upstream and implementation choices](UPSTREAM.md) for provenance and design differences. The project is licensed under the MIT license.
