---
name: maintenance
description: Check Codex Mem health and troubleshoot or repair its installation, capture, database, background queue, observation processor, or semantic index. Use when memory seems inactive, delayed, broken, or needs maintenance.
---

Maintain Codex Mem using evidence from the current host and the requested project. For recalling or authoring project knowledge, use the sibling `memory` skill.

## Check

Resolve `<skill-dir>` from this file and `<plugin-root>` two directories above it. Use the user's exact absolute project path, otherwise the current working directory. Keep the same project and data directory throughout diagnosis, repair, and verification. Worktrees have separate histories. A request to check all projects permits the helper's `--all-projects` report; it does not authorize changes to all projects.

Run the bundled read-only helper:

```sh
python3 "<skill-dir>/scripts/health_check.py" --project "<absolute-project>"
```

Pass `--data-dir` when the user selected a custom store; otherwise the helper respects `CODEX_MEM_HOME`. Add `--deep` for an explicit database integrity check. The helper reads metadata, not note bodies. It must not create or migrate a database, retry jobs, start workers, or run a model. Use this helper before normal storage commands, whose initialization can create or migrate storage.

Interpret the report against actual workload:

- Separate the last captured observation from the last curated note and the last successful processing job. No recent notes alone does not prove capture is broken.
- An idle worker and an empty queue are normal. An old inactive project is normal. Judge backlog against job age, current ownership, configured timeouts, and observed progress. Distinguish delayed, failed, blocked, and merely pending work.
- Preserve intentional capture scope and disabled optional features. An excluded project is not an installation failure. Inspect the recorded processor provenance separately from the expected Luna/medium configuration.
- Keep database health, worker liveness, index coverage, and native integration as separate findings. Missing telemetry stays unknown; a running PID does not prove successful processing.

For hook registration and trust, run the bundled host check from the same installed plugin:

```sh
python3 "<plugin-root>/scripts/host_check.py" --cwd "<absolute-project>"
```

This starts a local app-server for discovery, without a model turn or a trust change. Confirm six Codex Mem definitions, their enabled state, trust, and existing launcher paths. If the host check is unavailable, keep native integration unverified and inspect `/hooks` in Codex when possible. Successful discovery does not prove execution. On the verified CLI 0.153.4, `SessionStart` runs with the first user turn; opening an empty CLI window is not an execution test, and context can be supplied without a visible banner.

## Repair and verify

A check-only request ends with findings and proposed actions. When repair is requested or already authorized, use [repair.md](references/repair.md) for the matching issue. Preserve existing authorization and settings; do not ask again for an already authorized bounded repair. Perform only the operation justified by the finding, then recheck the affected state. If the same failure repeats, stop retrying and report the remaining cause or evidence gap.

A full capture-to-recall probe is a separate test, not part of read-only diagnosis. It creates test data and uses Codex allowance. For an authorized probe, isolate the memory home and project, use fresh sessions, and verify capture, processing provenance, and recall separately. An explicit `memory_remember` write cannot prove that automatic capture or processing works. Retain metadata receipts and clean up only the test resources created by that probe.

Report the project and data-directory scope, a plain verdict, last observation/note/processing times, queue failures or delays, index coverage, and native checks that were or were not observed. For repairs, give the before/after evidence and any unresolved problem. Keep private record contents out of the report unless the user explicitly asks to inspect particular records.
