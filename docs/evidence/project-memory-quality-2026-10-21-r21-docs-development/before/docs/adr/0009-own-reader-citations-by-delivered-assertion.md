# Own reader citations by delivered assertion

The experimental reader previously returned a delivered answer, a factual checklist and one flat citation list. A citation could support only the checklist. The frozen evaluator correctly rejected that citation when the delivered answer contained no corresponding statement. The [R18 citation diagnosis](../evidence/project-memory-quality-2026-10-21-r19-citation-diagnosis/diagnosis.md) found four such answers. Those grades remain unchanged.

The optional experimental contract `assertion-owned-citations-v1` gives each delivered assertion its own text and citation references. Code joins the texts in their original order and collects citations only from those assertions. Checklist fields have separate references. Their text and references never enter the delivered answer or earn answer credit. A reader assertion is an output clause, not a stored project-memory claim.

Every nonempty assertion must own at least one valid reference from the displayed source catalog. An empty assertion list requires explicit uncertainty and produces a fixed citation-free abstention. Bounds and malformed ownership reject the whole packet. Code neither clips text nor repairs references. Nonempty text retains its exact whitespace. Saved ownership receipts record the exact delivered intervals and are checked against the raw response before grading.

## Consequences

Ownership establishes an address and a delivered text interval. It does not establish semantic support, truth, causality, task identity or current applicability. An irrelevant reference attached to an assertion can still pass the mechanical decoder. The evaluator must still assess support. This change adds no semantic filter and does not remove a citation merely because it concerns information outside the gold facts. Additional supported status information can remain in the delivered answer.

The [experiment helper](../../scripts/project_memory_consolidation_experiment.py) enables the new contract only through an explicit protocol value. Existing typed reader contracts retain their behavior. The [public ownership tests](../../scripts/consolidation_experiment/test_reader_assertion_ownership.py) compare old requests against the hash-pinned R18 helper and check raw-response, per-question and combined ownership receipts. They also demonstrate that structural ownership alone accepts a semantically unsupported assertion.

The same reader contract must apply to both arms of any future comparison. A new protocol and fresh independently authored cases are required. Previous inputs, answers and grades are not rescored. The quality gates and missing-answer denominators remain unchanged. The [combined offline checks](../evidence/project-memory-quality-2026-10-21-r19-validation/protocol-suite-receipt.json) do not replace native calibration or an independent quality result.
