# Review session summary support before storage

Session summaries remain concise prose. Mandatory support review and a separate contradiction check run before the processor writes notes and a summary atomically. The checks use the same selected execution profile as generation. They are bounded, fallible judgments.

## Why the contract changed

Shape validation and valid source IDs did not prevent a completion claim from repeating the user's request instead of describing the answer. The first review contract, `summary-support-v1`, accepted a false attribute while citing contradictory original evidence. Quote ownership did not establish support.

The second contract supplied complete field units and code-owned source spans. A live check still accepted a false whole-field claim. A general comparison prompt and an added source ledger also missed the contradiction. The current design separates candidate-blind interpretation of one original report from comparison with the candidate. Mandatory review of all original evidence remains in place.

## Original-source support

The `summary-support-v3` request supplies every complete nonempty summary field, including its title, as a unit keyed by field name. It preserves text and punctuation. Field purposes distinguish historical intent, investigated work, findings, completed outcomes, future work and other context. A requested attribute does not become a completed attribute.

The response contains flat comparison rows. Each row names its unit and states coverage, candidate meaning, evidence meaning, value comparison, attribution comparison, scope comparison and verdict. The verdict is `supported`, `unsupported`, `contradicted`, or `insufficient_context`. Code groups rows by unit. Every unit needs one to eight comparisons with consistent coverage. Partial coverage yields uncertainty. A supported topic cannot cover a contradictory attribute.

Code creates contiguous source spans of at most 1,200 characters, with stable IDs, offsets and exact text. Every disclosed source retains its full parent body, role, order and current/history group. The model selects IDs without copying punctuation or calculating offsets. Supported and contradicted comparisons need a known span. Other verdicts can have no span. The decoder rejects unknown references, missing units, invalid shapes and exceeded bounds. Duplicate JSON keys and trailing data remain invalid in both native adapters.

Source spans establish text ownership. They are not semantic atoms. Historical derived records cannot override contradictory original evidence, and the candidate cannot support itself. Mechanical coverage does not establish that every assertion was identified or correctly interpreted.

## Isolated current-report interpretation

After original support passes, `summary-source-v2` receives only the latest original assistant report in the current claimed batch. It receives the full bounded body and its source, session, turn, timestamp and current order. It receives no candidate, competing user request, history or derived memory. There is no historical fallback. Jev lifecycle-only envelopes do not count as original report text. With no original report, the derived checks record `not_assessed` and make no model call.

The model returns at most eight content propositions. Each preserves statement, subject, time and whether the content is reported, observed text, planned or unknown. Code assigns proposition IDs. Plans and unknowns cannot support or contradict an outcome. The model also returns two mandatory observations about this exact report: language and organization. These free-text values describe the report's observable language, mixed regions, lists, headings or other structure, with section scope where needed.

An observed slot needs a nonblank value and scope, with a null reason. An unknown or inapplicable slot needs a null value and a nonblank scope and reason. Only observed slots become propositions, with code-owned IDs and the subject and time limited to this report text. The maximum is eight content propositions plus two observations. Code rejects duplicate content propositions. Native schemas omit the unsupported `uniqueItems` keyword, while the decoder retains duplicate checks.

A later acknowledgement's language or shape does not establish attributes of an earlier answer or external work. Report provenance identifies supplied text; it does not verify its claims. A complete coverage label does not mean that all possible facts were extracted. Partial coverage and unknown observations remain visible. The interpretation is reused only within this attempt while the full retained-evidence fingerprint remains current.

## Outcome consistency

The `summary-outcome-v2` request contains the complete nonempty `title`, `investigated`, `learned`, `completed` and `notes` fields and their purposes. Historical `request` and future `next_steps` are excluded. It receives the isolated interpretation and report identity, without the original competing request or full generation prompt.

Code supplies every field/proposition pair with a stable relation ID and exact joins. At most five fields and ten propositions produce fifty rows in one call. The decoder requires each pair exactly once. Each row states the candidate claim, scope comparison, relation and value comparison. Claims and qualifiers remain in full input fields. Models do not partition punctuation or copy exact quotes.

The relation is `conflict`, `no_conflict`, `not_related`, or `not_assessed`. Conflict and no-conflict rows require a nonblank candidate claim. The other relations accept null or bounded explanatory text. Unknown and planned propositions require `not_assessed`. If all propositions are unavailable or absent, the extra call is skipped with `not_assessed`. Mixed unavailable propositions retain their required rows.

Only an explicit conflict about the same subject, report, time and scope vetoes a candidate. Mismatched or unknown referents do not establish a conflict. A no-conflict result does not establish support. Full pair coverage does not prove that candidate assertions were identified correctly. Paraphrases can borrow source-only details, and the model can still miss a visible attribute.

## Correction, freshness and failure

Generation and all reviews share the immutable runtime, provider, exact model, reasoning level and monotonic deadline. The default remains 240 seconds. No new provider, key or model upgrade is required. Reviewers have no tools or correction callbacks. The optional Jev quality gate shares the existing single generation correction.

A changed correction must pass shape, summary preservation, source membership and both applicable review stages again. Source interpretation is reused after freshness checks. An exact rejected summary cannot ask the model for another vote. The attempt keeps a rejection key over the canonical complete summary, retained evidence, interpretation, immutable profile and contract versions. Notes outside the summary cannot reset it. Shape and lease/source checks run before reuse. Operational failures are not cached, and the cache never crosses attempts, jobs or leases.

This rule conservatively preserves the first semantic rejection, including an erroneous rejection. It does not compare meanings or normalize text heuristically. A changed summary can use the remaining correction. A correction cannot remove a previously generated summary. Cached rejection adds no fabricated call or usage.

Store captures per-record fingerprints while it builds the exact current-source and historical views for a claim. These identities include all retained fields, structured metadata, source links and hydrated current tool I/O. Historical identities use complete retained records before excerpting. The processor compares selected captured identities with retained records before the initial fingerprint and before any generation call. Changed or deleted evidence fails with `summary_support_stale`. An unchanged bounded excerpt remains valid.

The processor passes the reviewed retained-evidence IDs, current-source IDs and aggregate fingerprint to Store. Store reads the same project-scoped records and current tool I/O inside its existing write transaction, before any output is inserted. Deletion or mutation after the final processor check blocks the whole packet. Unrelated projects and undisclosed records do not affect this guard. No model or processor callback runs inside the transaction.

Before Jev eligibility filtering, the processor checks the unfiltered captured identities. It checks again before cache reuse, before each live chunk after HTTP-slot acquisition, and after selection. Selected evidence is checked before generation and each native initial or correction turn. Both runtime adapters also check before native support, interpretation and outcome submissions. Optional Jev quality uses the same retained-identity check through its existing callback, including after its HTTP-slot wait. These checks preserve the original absolute deadlines.

A change visible at a guard denies that submission. A guard does not revoke a request already submitted, and a source can change after a guard. The final transaction fence prevents stale output writes. No database lock or transaction spans a model call. Earlier measured usage survives a later denial. Successful late review after lease expiry keeps the typed expiry outcome in both runtimes, retains owned call metrics and identifiers, and leaves ordinary retained-service recovery nonblocking. Actual malformed native records still fail as protocol errors. `generator_started` is marked after freshness allows generation handoff, not by native preparation alone. It does not prove provider acceptance.

Each review owns its metrics and worker identifiers in the call ledger. A failed review propagates its failure category without replacing the generation receipt. Measured review tokens add once; missing review counters keep the combined total unknown. The synthetic comparison recorder retains up to five ordered review executions in separate paths while generation remains single-use.

The separate cross-session `project_context` is a reference for generation and optional Jev quality. Quality receives it as `project_reference` with `evidence_role=reference_only` and cannot use it to replace missing support. It is excluded from mandatory support evidence and this content fingerprint. Existing freshness checks establish its record existence, not unchanged content.

Late negative or malformed reviews quarantine only the exact still-owned attempt. A replacement lease cannot be changed by an old reviewer. Changed sources, expired leases, malformed replies, failed calls and unresolved support prevent atomic storage. Raw work remains available. Rejection and uncertainty quarantine the failed batch while fresh work continues across service restart. Malformed, unavailable, stale and unknown operational failures retain the project-blocking policy.

Notes and summaries have separate relevance decisions. A useful session summary does not require a new durable note. Routine documentation, greetings and other low-value exchanges can still be skipped when neither output is justified. Earlier accepted substantive work still requires a summary. Native and injected generation retain the existing prohibition on summaries without a Stop. Notes without a summary do not require these reviews.

## Accounting and compatibility

The additive `execution_calls` ledger contains generation, up to two original-support calls, one source interpretation and two outcome calls. All use the selected profile. Unchanged rejection or unavailable derived evidence uses fewer calls. Generation can include its single correction and keeps cumulative checkpoints. Independent measured counters add once. A missing counter stays null, while measured per-call values remain available. Whole-attempt elapsed time counts nested review time once.

Operational diagnostics retain protocol version, status, candidate/evidence fingerprints and fixed invalid subtypes: `structure`, `unit_coverage`, `unknown_span`, `verdict`, `bounds`, `unknown_proposition`, and `duplicate_reference`. They contain no raw source or reviewer prose. Bounded correction feedback is untrusted data, never replacement evidence. Historical attempts and stored summary shape remain unchanged, including `verification=not_assessed`.

Tests inject generation and review separately through `process_pending` and `recover_failed_batch`. An injected generator without an explicit reviewer cannot start a live reviewer. Canned executable and local-provider tests establish protocol, lifecycle and accounting behavior only.

## Evidence and remaining limits

Earlier live acceptance and the first two development smokes failed. Those outcomes remain failed. An isolated native probe identified unsupported `uniqueItems`; separate probes recovered text observations and exercised flat formatting. Their small samples did not establish semantic accuracy. An offline decoder experiment did not turn earlier invalid Pi outputs into successful native runs.

The third development smoke passed four exposed cases on October 24, 2026. Both runtime variants rejected the known false attribute and persisted the supported counterpart. The run used fixed generation, twelve real review calls and 86,166 reported tokens. Original support alone still accepted both false candidates. Isolated outcome comparison supplied the veto, and unchanged correction preserved it. This is known-pair evidence, not general accuracy, unseen acceptance or proof of later native memory injection. These development results do not establish final private acceptance. Integration and deployment require a separate private acceptance run.

Implementation: [original support](../../codex_mem/summary_support.py), [isolated interpretation and relations](../../codex_mem/summary_outcome.py), [processing](../../codex_mem/processor.py), and [accounting](../../codex_mem/observer_usage_store.py). Controls: [public summary tests](../../tests/test_summary_support.py), [contract tests](../../tests/test_summary_support_contract.py), [outcome tests](../../tests/test_summary_outcome.py), and [Jev lifecycle tests](../../tests/test_jev_processor.py).
