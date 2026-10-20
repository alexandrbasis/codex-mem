# Keep episode consolidation bounded and source-backed

Project memory can split one task across separate episodes. Reconsideration uses the existing durable job queue and retained original observations. It does not add a graph store or replay a full transcript.

Candidate episodes require the same explicit task ID in positive original user evidence. A consolidation proposal also requires evidence of continuation or correction. Shared topic, wording, or chat membership cannot establish identity. Ambiguous identity leaves the episodes separate.

A successful commit creates an immutable episode version and dependency links to the consolidated heads. The commit checks source text, revision, deletion generation, lease, and current heads. Dependency deletion revokes consolidated records and never restores an earlier head automatically.

Earlier goals, reasons, results, and open work can remain as explicitly historical context with original references. Their current validity stays unresolved. Code does not turn this context into active requirements, because partial completion or cancellation can change only part of a compound obligation. Current claims retain their own scoped transitions.

This choice limits automatic merges and preserves the distinction between source history and current obligations. Bounded head discovery, historical sources, and field retention can omit relevant context. Omission counts and reconciliation status disclose those gaps. A completed candidate fingerprint prevents repeated no-op generation for unchanged candidates. These controls do not establish semantic correctness, complete historical coverage, or deployment.

See [project memory behavior](../PROJECT_MEMORY.md), [the domain terms](../../GLOSSARY.md), and [the experiment report](../evidence/consolidation-2026-10-20/report.md).
