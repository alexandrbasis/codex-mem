"""Shared evidence requirements for generated observation classifications."""

OBSERVATION_CLASSIFICATION_CONTRACT = (
    "Observation type is an application classification inferred from the evidenced work or finding. "
    "The source need not literally assign or name the category. "
    "A finding or observed evidence limitation can be discovery without completion. "
    "The category bugfix requires evidence of an actual correction, not a still-failing test; "
    "record an unresolved failure as discovery with its open work. "
    "The category decision requires an accepted choice with rationale, not an unaccepted suggestion. "
    "The categories feature, refactor and change require an observed change, never just an implementation request. "
    "Generic topic and concept labels require topical fit. Any label or category implying "
    "completion, termination, failure, identity, quantity or another factual assertion requires "
    "the same scoped evidence, attribution and uncertainty as prose. "
    "No material field is exempt from factual support. "
)
