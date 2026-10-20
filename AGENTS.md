# UI guidance

For UI work, follow [GUI.md](GUI.md) and use `graphical-ui`, `graphical-convert`, or `graphical-audit` from `.agents/skills/` as appropriate. The local dashboard uses the Untitled theme in both modes.

# Architecture

The current [Codex Mem architecture](.archify/architecture-codex-mem-20261020-161259/codex-mem.html) and its `candidate.json` share one directory.

When event capture, processing, storage, context retrieval, or provider boundaries change, update the diagram and source links in the same task. Run `python3 .archify/architecture-codex-mem-20261020-161259/maintain.py check`. If hashes differ, review the affected connections and descriptions.

After reviewing the architecture descriptions, update `candidate.json` and run `maintain.py snapshot`. Run the full Archify `finalize architecture` command with the returned `--repo-root`. Then run `maintain.py seal` and `maintain.py check`. The snapshot preserves current files separately from the project history. Hash checks detect changes but do not prove that the architecture is correct.

## Agent skills

For engineering workflows, skill invocation in Codex, authorization boundaries, review scope, and installation checks, read [docs/agents/engineering-skills.md](docs/agents/engineering-skills.md). Before code review, read [CODING_STANDARDS.md](CODING_STANDARDS.md).

### Issue tracker

Issues and specs use GitHub Issues in `alexandrbasis/codex-mem`. Before reading or publishing tickets, follow [docs/agents/issue-tracker.md](docs/agents/issue-tracker.md).

### Triage labels

Before triage, read [docs/agents/triage-labels.md](docs/agents/triage-labels.md). The `needs-info` role uses the existing `question` label.

### Domain docs

Before exploring domain concepts, follow the single-context rules in [docs/agents/domain.md](docs/agents/domain.md). Create glossary entries and ADRs when terms or decisions are resolved.
