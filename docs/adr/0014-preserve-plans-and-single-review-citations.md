# Preserve reported plans and declare review citations once

## Problem

A generated summary could invent the original user request from a sent message or add a future action because an external result was still unknown. Generation and correction did not give equally clear instructions for empty fields.

The support reviewer declared report anchors twice: once as evidence and once as report bindings. A model could select a valid report anchor but omit its duplicate evidence reference. Strict validation correctly rejected that inconsistent response.

Outcome review treated a known reported plan like an unknown value. This could reject an accurate statement that an assistant proposed work, even when the summary explicitly said the work was not completed.

## Decision

Generation, initial support review and the one allowed correction share the same field rules. The request is null without evidence of what the user asked. Next steps are empty unless the sources establish unfinished requested work, a plan or a proposal. A pending external result does not create a commitment to wait or follow up. Required summaries and their supported content remain required.

Field purposes define what a field means; they do not establish that its source exists. In particular, the request field needs evidence of an actual user request. A tool result, an assistant answer or the candidate itself cannot supply a missing request. If a nonempty request has no supporting evidence, review must return an unsupported or insufficient-context verdict instead of inventing a citation. Such verdicts can have no citations. Supported and contradicted verdicts still require evidence. The existing correction can set an unsupported request to null while preserving supported summary content.

In the `summary-support-v7` response, each comparison declares one list of citations. Each citation selects an existing span and its role: evidence or report anchor. A report anchor explicitly serves both roles. Code derives the canonical evidence and anchor lists from those choices, then applies the strict ownership and binding checks. It does not add references that the model did not select. Old malformed v6 responses remain rejected; they are not repaired or reinterpreted.

The `summary-outcome-v6` review can compare a known reported plan as a plan. A compatible judgment must preserve its attribution, scope and unfinished status. A plan does not prove execution. Conflicting values and unresolved applicable comparisons still block storage. Unknown source values remain unknown. No relation is silently removed or accepted.

Historical diagnostics remain readable. Runtime, provider, model, reasoning level, deadlines, one-correction limit, source guards and atomic writes remain unchanged. Wythm's isolated pending prompt is processed through an explicit ordinary queue request; this task does not change the general prompt scheduling policy.

## Verification limits

Structural tests cover citation ownership, strict decoding, typed outcome states and preserved rejection boundaries. They do not establish semantic model accuracy. Targeted native recovery must separately show the terminal result of each affected batch. Previous failed attempts remain in the ledger.
