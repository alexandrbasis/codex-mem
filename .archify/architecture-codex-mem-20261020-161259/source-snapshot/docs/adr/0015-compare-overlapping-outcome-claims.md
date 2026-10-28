# Compare overlapping outcome claims

## Problem

Generation attributed an assistant's proposed practice to the user. The source established user feedback and an assistant commitment, but no user acceptance of that specific practice. Mandatory support correctly rejected the attribution.

After correction, support accepted the candidate. Outcome review then rejected distinct facts because one extracted fact did not establish the whole summary field. The complete original report supported those facts together. An omitted statement in one extracted item was mistaken for a conflict or uncertainty.

A later support response confused a reported statement inside tool evidence with an original assistant report. It selected a report binding without an eligible report anchor. Strict validation correctly rejected that response.

## Decision

Generation and its one allowed correction keep user feedback separate from an assistant interpretation or commitment. An assistant proposal does not establish a user preference, an accepted decision or a completed change in practice.

Generation also preserves where each statement was made. A link to a document or conversation does not place the user's message or assistant response inside that source.

Support bindings identify only original assistant reports in the supplied report catalog. A quoted message or reported statement inside a tool record remains ordinary evidence. A known assertion about that evidence uses the `other` binding. Each report binding requires an eligible report anchor. An unrelated assistant report cannot supply that anchor.

The response schema presents citation selection before the report binding. Binding descriptions repeat the distinction between tool evidence and original assistant reports. This order gives the model its selected references before it chooses the binding. The validator still rejects inconsistent choices.

Independent assertions with different sources or report bindings use separate comparison rows. A report-bound row identifies only the assertion about that report. Other assertions in the same field keep their own evidence. The complete candidate field and its spans remain unchanged.

Outcome review compares the particular proposition or attribute shared by each pair. A shared speaker, report or broad category alone does not establish that overlap. Distinct facts can coexist. An unasserted property is absent, and claims about separate portions of the report are disjoint.

For overlapping claims, the reviewer compares that overlap in the complete original report. A clause supported elsewhere in that report does not become uncertain because one extracted item omits it. An omission alone cannot justify an incompatible value. A real opposing value or unresolved overlapping claim still blocks storage.

The reviewer must read each bound assertion as written, including negation, attribution, modality and qualifiers. Earlier support approval does not prove agreement with the original report. Another field cannot replace or silently repair the assertion under review.

Mandatory support remains the source-support gate. Allowed typed states, validators, the complete relation set, original-report joins, source guards and atomic writes remain unchanged. The selected profile, deadline and single correction limit also remain unchanged.

## Verification limits

Replay uses the exact captured candidate, support bindings, source interpretation and outcome relations. A positive native control tests the corrected comparison instructions. A negative control reverses one attributed assertion and must reject that exact overlapping pair. The negative control tests outcome review alone and is not represented as support-approved.

A separate support replay tests the distinction between tool evidence and an original assistant report. It must retain ordinary evidence citations for the tool claim and a report anchor for the assistant claim. The original malformed response must remain rejected.

These controls establish behavior for the tested inputs. They do not establish accuracy for unseen material. The affected batch also requires a successful end-to-end recovery with its earlier failed attempts preserved.
