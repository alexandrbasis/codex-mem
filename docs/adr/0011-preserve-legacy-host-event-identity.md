# Preserve legacy identity in the shared host contract

The shared host contract retains existing Codex session IDs and recognizes legacy source names. Other host and branch sessions use a separate namespace. The same source classifier serves Python validation and SQLite selection, so adding a host does not require rewriting history. During capture, the store can recognize a legacy dedupe key as an alias in the existing transaction. This keeps source IDs and completed processing state stable across an upgrade. New delivery identity uses host and event identifiers, never text alone. Adapters retain response formatting and service wake requests. The shared core owns capture policy, provenance, and bounded untrusted context.

See [the host contract](../HOSTS.md) and [host/runtime separation](0010-separate-host-and-background-execution.md).
