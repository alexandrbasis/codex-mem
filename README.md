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

Version `1.8.0` adds period-based usage reports with separate API-equivalent cost and Codex credit estimates. Reports read the local ledger by default; an explicit refresh imports a bounded batch of usage metadata without model calls. The collector repairs missing Standard/Fast attribution while keeping requested and provider-confirmed settings separate. The observer now saves usage during a running attempt so recovery can retain a partial total. The observer remains Luna with medium reasoning. See the verification record for tested behavior and installation checks.

[Русская версия](README.ru.md) · [Verification record](docs/VERIFICATION.md) · [Upstream and implementation choices](UPSTREAM.md)

## What it does

- Captures bounded evidence from approved projects through native Codex lifecycle hooks. Raw hook records remain available for explicit audit, but are excluded from search, automatic context, and semantic indexing.
- Exposes `memory_search`, `memory_get`, `memory_timeline`, `memory_remember`, `memory_consolidate`, `memory_forget`, `memory_status`, `memory_get_tool_uses`, `memory_usage_report`, and `memory_usage_refresh` through a local MCP server.
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

Health report v2 uses `in_progress` for an active processing job, `quarantined` for retained failed jobs, `blocked` when service scheduling is explicitly blocked, and `stale` for expired work or overdue work with a stopped worker. Historical failures alone do not establish that the service is blocked. The report includes the last successful processing time and the ages of pending work and recorded session usage. If process visibility is restricted, worker liveness is `unknown`.

The separate `observer_usage` summary reports processing attempts, durations and token coverage. Missing usage stays unknown; it is never treated as zero. See [Inspect token usage](#inspect-token-usage) for the difference between session and observer accounting.

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

## Inspect token usage

### Report a period

Read usage and cost estimates for September 11 and 12 in Israel:

```sh
python3 scripts/codex-mem.py usage report \
  --from 2026-09-11 --to 2026-09-13 --timezone Asia/Jerusalem \
  --group-by day,project,model
```

`--from` is inclusive and `--to` is exclusive. Dates use the selected IANA timezone; timestamps must include a UTC offset. With no dates, the period starts at yesterday's midnight and ends at the current time. The default timezone is `UTC`.

Omitting `--project` includes all recorded local projects. Add `--project /absolute/path/to/project` to select a project and `--session-id ROOT_SESSION_ID` to select a root task and its agents. Replace `ROOT_SESSION_ID` with the task's session ID. `--group-by` accepts a comma-separated selection of `day,project,task,agent,model`; all five are returned by default.

Each dimension has its own breakdown under `groups.main` and `groups.observer`. Each breakdown returns up to 100 groups and the number omitted; totals still include all matching records.

MCP may shorten large breakdowns further to stay within its response-size limit. `transport.truncated` and the group omission counts disclose this; totals remain unchanged. Narrow the project, period, or grouping for more detail.

The default report reads the existing ledger without importing logs or calling a model. To import a bounded batch before reporting, add `--refresh`. You can also refresh separately:

```sh
python3 scripts/codex-mem.py usage refresh \
  --from 2026-09-11 --to 2026-09-13 --timezone Asia/Jerusalem
```

A refresh respects capture settings and updates usage metadata only. It can retain usage outside the requested period from a selected source file because attribution requires parsing that file in order. The default batch allows 32 files and 8 MiB. `--max-files` accepts 1–128 and `--max-bytes` accepts 1–8388608. Repeat the refresh if coverage remains partial.

MCP provides the same report through the read-only `memory_usage_report` tool:

```json
{
  "from_date": "2026-09-11",
  "to_date": "2026-09-13",
  "timezone": "Asia/Jerusalem",
  "group_by": ["day", "project", "model"]
}
```

Use `memory_usage_refresh` with the period and optional `project`, `session_id`, `max_files`, and `max_bytes` to import metadata, then call `memory_usage_report` again. The report tool does not refresh implicitly. Neither tool calls a model.

### Interpret costs and coverage

The shared calculator prices each response before adding totals. It applies the model, Standard/Fast setting, cached input, cache writes, and long-context rates from the versioned `openai-2026-09-12.1` snapshot. API-equivalent USD and estimated Codex credits are separate estimates based on [OpenAI API pricing](https://developers.openai.com/api/docs/pricing) and [Codex token rates](https://learn.chatgpt.com/docs/pricing#token-rates). Applying this snapshot to old usage does not reconstruct the historical invoice. Actual subscription payments, purchased credits, taxes, tool fees, and account discounts are unavailable.

Each cost metric exposes `selected_subtotal` for the priced portion. Its `total` is `null` if any included record is unpriced. Where rates are known but the tier is not, Standard and Fast scenarios describe hypothetical costs. Requested settings are labeled separately from provider-confirmed settings. Unknown rates and tiers never become zero-cost records. Cached input is already part of input tokens, and reasoning output is already part of output tokens. Older cumulative records lack precise response boundaries, so their context-based estimates remain approximate.

Read coverage with the estimate. It reports missing attribution and known files that still need reading or repair, along with scan limits and errors. File coverage is global even for a project report, because the project of an unread file is not yet known. A cached report cannot discover new files; bounded or incomplete discovery does not establish complete coverage.

Partial or running observer attempts also leave the complete cost `total` unknown; their recorded portion remains in the subtotals. The public Codex credit card does not specify cache-write pricing, so rows with cache writes have no credit estimate even when their API-equivalent cost can be calculated.

After an upgrade, the next usage refresh or collection pass reparses older checkpoints to recover nested Standard/Fast settings. Stable response IDs let it enrich existing records without adding the same response's tokens again. Missing source files and usage that was never recorded remain unknown.

### Inspect observer accounting

The period report separates main sessions, observer attempts, and their combined total. It excludes matching observer worker threads from the main stream to prevent double counting. An observer attempt belongs entirely to its start date, including when it crosses midnight or the selected period boundary. Historical attempts without receipts are shown as a coverage gap across all dates in the selected scope; they cannot be assigned a cost or an exact date.

Read the existing token summaries for a project:

```sh
python3 scripts/codex-mem.py usage status --project /absolute/path/to/project
python3 scripts/codex-mem.py status --project /absolute/path/to/project
```

`usage status` returns session JSONL accounting in `records` and separate processing accounting in `observer_usage`. The project `status`, MCP `memory_status`, and maintenance report also expose `observer_usage`. Adding `--session-id` to `usage status` filters only `records`; `observer_usage` still covers the entire project. The two ledgers are not merged.

The observer uses the latest valid app-server token total matching its fresh thread and turn. It saves intermediate snapshots while the attempt runs. Repeated updates replace the snapshot; they are not added together. Recovery of an expired attempt preserves its saved partial usage. Each retry has its own attempt receipt, so its reported usage is counted separately. Attempts also retain their outcome and available duration.

| Coverage | Meaning |
| --- | --- |
| `reported` | A valid usage snapshot was received and the native turn completed. This does not by itself mean its output passed memory validation. |
| `partial` | A valid snapshot was received before an interrupted or failed turn. It is counted separately from completed-turn usage. |
| `unknown` | The aggregate count of receipts without usable usage, including missing or invalid updates. Their token totals remain `null`. |
| `without_receipt` | Recorded job attempts with no accounting receipt, including historical attempts. Their cost is unknown. |

The optional SQLite ledger is created when processing records its first attempt. An absent ledger reports `unavailable`; it does not mean processing was free. Model groups use `model_basis="requested_job_profile"`: they describe the requested model and reasoning setting, and do not independently prove the backend that served a failed attempt. Cached input is part of input tokens, and reasoning output is part of output tokens; do not add these subsets again.

These measurements describe recorded work. Cost estimates do not establish actual money spent or net savings from memory. See [Verification](docs/VERIFICATION.md) for the tested accounting boundaries and [Upstream and implementation choices](UPSTREAM.md) for the protocol and comparison sources.

## Protect and manage data

The default data directory is `~/.local/share/codex-mem`. Set `CODEX_MEM_HOME` or pass `--data-dir` to use another local directory. Codex Mem does not modify Codex's built-in memory, `AGENTS.md`, Claude Mem settings, or the Claude Mem database.

The redaction filter covers `<private>...</private>` blocks and common token, password, and key formats. It cannot identify every secret. Keep the data directory accessible only to trusted local processes; project isolation does not replace operating-system permissions.

```sh
python3 scripts/codex-mem.py status
python3 scripts/codex-mem.py backup /absolute/path/codex-mem-backup.sqlite
python3 scripts/codex-mem.py prune --days 90
```

`prune` removes old records from all projects. Backups and context already sent to a chat are separate copies, so review your retention policy before pruning.

Observer accounting stores operational metadata, including job and attempt IDs, worker thread/turn IDs, counters and duration. It stores no prompts or model output. This metadata remains with observation jobs after `forget` or `prune` removes memory entries, and is included in database backups.

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

Codex Mem does not provide full Claude Mem parity. There is no Web UI, HTTP API, cloud synchronization, or non-Codex host integration. The E5 comparison covered a small fixed set of multilingual and paraphrase cases; it is not a universal retrieval-quality claim. One older contradictory recall fixture remains partial or undetermined in the verification record. A live Claude-versus-Luna quality and cost comparison remains unmeasured. Observer accounting does not backfill missing historical receipts or establish net memory-system savings.

Read [Upstream and implementation choices](UPSTREAM.md) for provenance and design differences. The project is licensed under the MIT license.
