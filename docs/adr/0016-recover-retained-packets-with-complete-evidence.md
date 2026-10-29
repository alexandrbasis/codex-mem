# Recover retained packets with complete evidence

Status: accepted. Date: 2026-10-29.

## Problem

Several independent conditions left retained source events unprocessed. Summary review could cite only the display excerpt, although generation received the full retained tool input and response. A single available span could be emitted twice under different citation roles. A valid detailed summary could exceed the outcome review's fifty-relation limit. An empty summary did not receive the correction already available for a missing summary.

Project-memory retry operated on an entire project and reset job attempts. That operation could not safely recover one inspected quarantine while preserving its failed job. Older observation transport failures also retained uncertain worker identities that ordinary automatic retry must not ignore.

## Decision

Make full retained tool input and response citable with separate field-owned spans. Preserve the body excerpt, source custody, truncation metadata and existing total evidence limits. Tool-output quotes do not become original assistant-report anchors. Limit citation arrays to the number of available spans; keep duplicate rejection in the decoder.

Compare every outcome relation in groups of at most fifty. Preserve relation IDs and all comparisons across groups. Each call uses the same execution profile and shared deadline, with its own usage and diagnostic receipt. Any rejected, unresolved, stale or malformed group prevents the packet from being stored. Grouping does not add a generation correction or remove a relation.

Let a missing or empty required summary use the same single structural correction. A second invalid summary or a rejected correction still prevents all output writes. Generation instructions preserve file-operation evidence, claim scope and modality. Optional labels must describe the supported content; supplied label examples are not defaults. A local evidence limit is not a denial of an earlier event.

Expose exact project-memory recovery through `process_project_memory(recovery=selector)`. The selector fixes the quarantined job, attempts, fingerprint, current revision, deletion generation and complete blocked event set, including offsets, content hashes and provenance. Create a new successor and preserve the parent and its usage. Process only the normal bounded prefix. A successful guarded commit releases only the authorized unprocessed remainder; a failed successor retains that event set in quarantine. Retained originals already superseded by observation processing remain valid PM inputs when their pinned metadata is unchanged. Explicit expiry reconciliation preserves lineage and attempts without dispatching a model.

For an inspected legacy observation `protocol_error`, permit a separate successor only with exact old worker IDs, job, attempt and fingerprint, an old profile, a closed lease-free parent and one-shot execution. The old worker's outcome remains unknown. The old lease cannot commit. Automatic transport retry retains its existing restriction.

## Evidence and limits

Offline tests cover complete tool-I/O span ownership, duplicate references, one correction in both runtimes, bounded outcome groups, exact recovery ownership, deletion and provenance changes, expired successors, partial prefixes, siblings and preserved parents.

Private native replays compared the same rejected candidate and retained evidence before and after full tool-I/O citation support. The corrected review accepted that candidate. A separate scoped-absence control was accepted while an explicit false universal denial remained rejected. These finite checks do not establish general semantic accuracy. Historical failures without retained model outputs cannot supply their original semantic cause.

No retry operation changes the selected projects, processing profile or model settings. Raw diagnostic inputs and outputs remain outside the repository. Historical failure records remain visible after their source events are successfully covered.
