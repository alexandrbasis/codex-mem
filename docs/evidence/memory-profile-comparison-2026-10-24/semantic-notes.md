# Blind semantic diagnostic

All 12 cells were graded from bundle.json only. Ratings in grades.json use the frozen five dimensions. Top-level ratings measure full submitted-history coverage; input_conditioned_verdict separately measures the actual prepared prompt, including session history.

| Cell | Full history | Actual prompt | Main finding |
| --- | --- | --- | --- |
| C01 | Unpassed | Unpassed | Preview remains a proposed requirement after cancellation; readiness conflict is incomplete. |
| C02 | Unpassed | Unpassed | Initial button obligation becomes possible removal. |
| C03 | Unpassed | Unpassed | Typed episode retains requirements and cancellation, but loses assistant readiness conflict. |
| C04 | Passed | Passed | Initial obligations, reason, scope, and uncertainty retained. |
| C05 | Unpassed | Unpassed | Summary omits reason and scope; later test/cancellation were not supplied. |
| C06 | Unpassed | Unpassed | Correct requirement prose is labeled change despite no observed change. |
| C07 | Unpassed | Unpassed | Current obligations and failed-test gate retained; assistant conflict and no-release evidence omitted. |
| C08 | Unpassed | Unpassed | Summary loses scope; later test/cancellation were not supplied. |
| C09 | Unavailable | Unavailable | No parsed candidate and no stored output; cause not inferred. |
| C10 | Unpassed | Unpassed | Summary loses scope; later test/cancellation were not supplied. |
| C11 | Unpassed | Unpassed | Summary loses scope and explicit initial button requirement; later test/cancellation were not supplied. |
| C12 | Passed | Passed | Initial obligations, reason, scope, and uncertainty retained. |

The strict all-dimensions rule yields 2 passed cells, 9 available but unpassed cells, and 1 unavailable cell. This is a descriptive count, not an estimate of equivalence or general accuracy.

All four summary prompts contain the initial user history and an assistant report, but no later tool result or cancellation. Missing later facts are full-history coverage gaps. They are not model omissions conditional on the actual input. The actual-input grades therefore improve for status and evidence even where the cell remains unpassed for another dimension.

PM is judged through substantive typed episode fields and claims. Source custody does not substitute for representation. Empty claims alone do not fail an episode that explicitly preserves the requirement. C01 has a separate surviving preview claim with a status inconsistent with cancellation.

Timeline readback confirms previews and complete structured metadata. It does not confirm complete stored bodies. The complete raw parsed packets provide additional candidate evidence. No runtime, latency, schema, or missing-output-cause judgment is included.

This is new synthetic wording over disclosed regression themes: a diagnostic comparison, not an independent holdout. No cross-project distractor text was actually supplied, so absence of leakage does not demonstrate resistance to a visible distractor.
