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

SUMMARY_FIELD_EVIDENCE_CONTRACT = (
    "Populate request only from an evidenced actual user request; otherwise use null. "
    "A drafted or sent artifact, tool action or assistant report of work does not by itself "
    "establish the original user request or authorization. "
    "Use an empty string for next_steps unless the sources establish explicitly requested "
    "unfinished work or a plan or proposal. A bounded remainder of an evidenced request "
    "can remain without inventing an actor commitment. Record a relevant unresolved external "
    "condition in learned or notes at its evidenced scope; it does not by itself establish "
    "a plan to wait, review, approve or follow up. Label a proposal only when the sources "
    "propose it. Preserve attribution, uncertainty and planned-versus-completed scope. "
    "Keep all required keys, any required summary and its supported fields. Use permitted "
    "null or empty values for unsupported fields instead of inventing content to fill them. "
)
