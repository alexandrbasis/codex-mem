# Repair by finding

Read only the relevant branch after diagnosis. Replace `<plugin-root>` and `<absolute-project>` with resolved paths. Commands below use the default data directory; for a custom store, put `--data-dir "<data-dir>"` before the CLI subcommand. Inspect the installed command's `--help` if its supported arguments differ.

## Native installation or hooks

If discovery points to a missing launcher or an older package, compare the active plugin root with the current installed source. Use the repository's `scripts/install.py` preview and apply workflow for an authorized installation repair. It preserves previous managed caches for open sessions. Avoid deleting those caches while sessions still reference them.

After installation, inspect native discovery again and use a fresh Codex task. A running desktop process can retain an older catalog. Report the stale connection before asking the user to restart an app with active work. Review changed hook definitions through the host's normal trust mechanism; preserve existing trust and do not fabricate trusted hashes. An unrelated MCP authentication warning is not proof that Codex Mem hooks failed.

## Queue is pending with no active worker

Confirm capture, processor, and service settings match the user's intent. With authorized processing, enqueue the affected project if it is missing, then start the existing worker:

```sh
python3 "<plugin-root>/scripts/codex-mem.py" service enqueue --project "<absolute-project>"
python3 "<plugin-root>/scripts/codex-mem.py" service start
```

The worker and its start/stop operations are global and can process other already queued, allowed projects. Disclose that scope before an operation if the user's repair authorization covers only one project. Prefer a bounded project operation when that satisfies the request. Verify job transitions or completed work, not just a successful start command. Do not run a second manual processor concurrently for the same project.

## Failed or blocked processing

Inspect the error code, timestamps, and processor receipt before retrying. Fix the demonstrated cause, such as unavailable Codex authentication, an incompatible installed version, or denied storage access. A model timeout can leave an uncertain outcome; inspect the durable job state first.

For an authorized retry of the affected service project:

```sh
python3 "<plugin-root>/scripts/codex-mem.py" service retry --project "<absolute-project>"
```

This requeues work; inspect worker status and start it if required and within scope. When the service is intentionally disabled and no worker owns the project, `process --project "<absolute-project>" --retry-failed` processes one bounded batch. Processing consumes Codex allowance. Preserve Luna/medium unless the user changes that choice. Do not clear queue files, delete locks, or rewrite job rows to make a report look healthy.

## Semantic index is incomplete

Optional semantic search can be disabled intentionally; lexical search remains usable. If indexing is wanted and the installed model is ready, use the existing service or one bounded local index batch:

```sh
python3 "<plugin-root>/scripts/codex-mem.py" semantic index --project "<absolute-project>"
```

Use `--retry-failed` only for a diagnosed failed indexing batch. Recheck pending/stale counts and stop if a bounded attempt makes no progress or repeats the error. Model setup downloads files and installs the optional runtime; use the README's explicit setup workflow when that setup is requested. Never delete the database to rebuild the semantic index.

## Database, backups, and retention

A failed integrity check requires preservation and diagnosis before writes. For a healthy source database, the CLI `backup "<destination.sqlite>"` creates a consistent SQLite backup. Record the destination and verify the backup before any authorized destructive maintenance. If SQLite cannot make a readable backup, preserve the original database and its WAL companions through a coordinated recovery procedure instead of repeatedly opening it for writes.

Ordinary maintenance does not authorize deleting project knowledge. Use explicit record IDs for requested `forget` operations and verify their absence through the memory tools. `prune --days N` affects every project, so establish the intended retention and global scope before using it. Backups and earlier chat context remain separate copies. There is no supported automatic restore command; do not overwrite a live database while a worker or Codex session may be using it.
