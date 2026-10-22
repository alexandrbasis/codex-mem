# R19 paired fact loss and quote-empty contexts

> Stored assistant report: “The complete caption check passes. I have not exported the public cut yet.”
>
> Question: “What did the assistant report about public-cut export after the complete caption review?”
>
> Candidate answer: “Unknown from supplied memory.”

R19H11/q3 contains the only paired fact loss in the frozen primary grades. Baseline recalled the assistant’s not-yet-exported report. Candidate omitted it. The original report exists in both databases, but Candidate rejects it before packing. Its query planner treats `public-cut` as a mandatory identifier because it follows `about`. The report uses `public cut` with a space.

This diagnosis uses the formally released R19 inputs. It does not regrade answers, change scores, or alter the completed trial. The full source chains, original outputs, frozen judge witnesses and input hashes are in `source-chain-diagnosis.json`. `candidate-trace.json` and `baseline-trace.json` retain the exact planner and acquisition traces.

## Exact source chain for the paired loss

The report is event `e09`, canonical source `767a238c3abf4e4d82232f99dd0c526e`. Its role is `assistant_report`, its field is `body`, and its complete stored interval is `[0,74)`. The released fact anchor is `[35,74)`: “I have not exported the public cut yet.” The active canonical row contains that exact text and belongs to `vesper_dome`.

The caller explicitly selected only the enabled `vesper_dome` project path. Candidate records that scope metadata as requested, supported and used. The question contains no project alias. The resulting plan is natural, nonhistorical, and has the single hard identifier `public-cut`. Its candidate window is the exact identifier window, rather than a soft description of export status.

The primary role query finds the assistant row: matched 1, candidates 1, returned 0, invalid 1. The trace reaches the role’s `scoped()` check, so the row already passed the preceding eligibility check. That check rejects the unchanged complete report body. The supplemental role pool also rejects the report and both captured tool results. No user original, role original or canonical record reaches final packing.

SQLite’s quoted FTS expression is `"public-cut"`; it finds the spaced report. The later exact identifier check rejects it. This is an inconsistency between lexical candidate acquisition and the hard content predicate. It is not source absence, deletion, a different read owner, a version/environment conflict, an oversized excerpt, or a renderer budget rejection.

Baseline exposes the full `[0,74)` body as a direct, independently citable assistant report. Its frozen reader answer attributes both the passing check and not-yet-exported status to the assistant. Its judge witness marks the fact recalled and reader-visible. Candidate’s per-question citation catalog is exactly empty. Its catalog hash is `4f53cda18c2baa0c0354bb5f9a3ecbe5ed12ab4d8e11ba873c2f11161202b945`.

Candidate’s XML occupies 504 characters of its 5900-character owner allowance. There are no admitted groups for the renderer to refuse. Increasing that budget cannot repair this admission decision. A pure planner probe changes only the query spelling from `public-cut` to `public cut`: the same unchanged report changes from rejected to accepted by `QueryPlan.matches`. That probe generates no replacement context or answer and establishes no quality result.

## All quote-empty views and counterevidence

> Stored acceptance: “Accept the normalized shelf codes and close that part. Keep the glaze-weight work open until the three missing weights are measured.”
>
> Stored continuing rule: “Do not substitute estimates for measurements.”
>
> Stored prerequisite: “Replace the hinge only after the empty-bin inspection passes.”
>
> Stored latest observation: “borrower language check: 1 preference supplied; 1 still unknown.”

These source bodies are active canonical rows. Their released body intervals and roles validate exactly in both arms. Nevertheless, four views stop at `skip_reason=historical_query`. They perform no snapshot or source-pool acquisition. The helper subsequently renders the returned empty brief, producing a metadata-only context.

| View | Candidate cause | Baseline comparison |
| --- | --- | --- |
| R19H02/q1, earlier accepted/open ledger parts | Historical guard before acquisition | Also quote-empty |
| R19H02/q5, present rule after removal of an earlier estimate | Historical guard before acquisition | Also quote-empty |
| R19H13/q1, original hinge prerequisite and inspection | Historical guard before acquisition | Also quote-empty |
| R19H14/q2, latest observation about previously missing preferences | Historical guard before acquisition | Also quote-empty |
| R19H11/q3, assistant report about public-cut export | Hard identifier rejection before packing | Baseline has 10 catalog entries and the needed report |

All five Candidate-empty views are already natural questions. None is the period-ended selected-project-prefix failure from the earlier disclosed development replay. The historical cases also differ: some ask for earlier state, while others ask for a current rule or latest observation with historical background. A single “remove the historical guard” change would conflate those requests and risk presenting current state as past truth.

Baseline has seven quote-empty views, Candidate five. Their intersection is the four historical-guard views above. Candidate also supplies quotes in three Baseline-empty views: R19H04/q1 and R19H15/q1 use existing current-evidence routing instead of the Baseline historical skip. R19H10/q2 has no admitted canonical records in either arm, but Candidate supplies bounded neutral originals. Those quotations retain their independent applicability qualifiers. Their visibility does not establish an active requirement.

The eight views in this union replay byte for byte in both frozen runtimes. Every original database remains byte-identical before and after its replay. Store constructors run only against disposable SQLite backups. Every reconstructed per-question citation catalog matches its frozen reader receipt hash. The 48 Candidate runtime files and frozen helper remain unchanged.

The two coarse constraint-recall losses are H11/q3, which overlaps this fact loss, and H12/q6. H12/q6 retains 2/2 facts in both frozen grades; its judge rationale concerns an unsupported causal assertion. Its semantic analysis belongs to the separate semantic probe. It is not an additional source-delivery loss inferred here.

## Bounded proposals for a later fresh round

These are design and test intents, not approved fixes.

First, separate an attributed-report request, its prose topic, and its explicit literal anchors. In a complete report-attribution frame, a pure alphabetic hyphenated modifier followed by a noun can describe a topic. A bare unknown scope name after a scope introducer remains different. Quoting, code calls, paths, files, commands, task IDs, versions, environments, explicit project labels and unknown project scope selectors must retain exact constraints. All prose words must remain in the soft topic; source text and intervals must remain unchanged.

The observed `public-cut export` construction motivates that grammatical distinction, not a `public-cut` exception. Public tests need unrelated compound modifiers, terminal unknown scope names, coordinated unknown scopes, and literal/artifact uses. A project name can also modify a noun. Syntax alone cannot resolve every such collision. Any relaxation needs an explicit bounded convention or trusted typed subject contract, plus a documented conservative ambiguity case. Simply deleting `about` from the hard-identifier heuristic is not an adequate design.

Second, separate requested time from historical background. A current-rule question after a removed estimate and a latest-observation question about previously missing data do not necessarily request a historical snapshot. Public tests must pair those forms with real as-of/original-state requests. Explicit historical reads need a bounded, qualified history path, or an explicit unsupported-operation result. They must not silently reuse current heads as earlier truth. Deletion, privacy, session, owner, task, version and environment guards remain mandatory.

Third, preserve the reason a read is unsupported on the wire. The current helper rerenders an early-return brief into ordinary coverage metadata. A specific unsupported-history marker could distinguish an unavailable operation from absent stored evidence. Such a marker would not itself restore facts or satisfy the quality gates.

A new evaluation must remain fresh. The frozen grades and disclosed intervals here are diagnostic evidence only. Exact quote visibility does not establish semantic support, and missing a gold interval does not prove that no sufficient paraphrase existed. The paired source exclusion is demonstrated; the answer that a future reader would produce after a repair is not.
