"""Shared complete packet support, original-report and outcome review sequence."""


def review_packet(summary, notes, claim, *, support_count, source_interpretation,
                  review_stage, resolve, source_guard, append_diagnostic,
                  set_interpretation=lambda value: None):
    """Run existing typed validators; callers own dispatch, guards and receipts."""
    from .processor import ProcessorFailure
    from .summary_support import prepare, validate
    from .summary_outcome import (prepare_source, validate_source, prepare_outcome_chunks,
        validate_outcome, outcome_applicable, current_report, OUTCOME_VERSION)
    support_review, support_payload, mark = review_stage(dict(summary=summary, notes=notes), f"summary_support_{support_count}",
        lambda: prepare(summary, claim, notes), validate)
    feedback = resolve(support_review.issues, mark)
    if feedback:
        return feedback, source_interpretation
    if summary is None:
        return None, source_interpretation
    # Reuse only within this attempt and after source_guard confirms the
    # complete retained-evidence fingerprint captured before generation.
    if source_interpretation is None and current_report(claim) is not None:
        source_interpretation, _, mark = review_stage(summary, "summary_source_1",
            lambda: prepare_source(claim), validate_source)
        # Outcome rejection identities must see this interpretation before
        # resolve records them, including when it requests a correction.
        set_interpretation(source_interpretation)
        source_guard()
        mark("interpreted")
    if source_interpretation is None:
        from .summary_support import diagnostic
        source_guard()
        append_diagnostic(diagnostic(summary, claim,
            status="not_assessed", version=OUTCOME_VERSION))
        return None, source_interpretation
    from .summary_support import SupportError, diagnostic
    try:
        outcome_requests = prepare_outcome_chunks(summary, support_review, source_interpretation, support_payload, notes)
    except SupportError as exc:
        append_diagnostic(diagnostic(summary, claim, status="invalid",
            invalid_subtype=exc.subtype, version=OUTCOME_VERSION))
        raise ProcessorFailure("invalid_response", reason_code="summary_support_invalid") from None
    if not outcome_applicable(outcome_requests[0][2]):
        source_guard()
        append_diagnostic(diagnostic(summary, claim, status="unused", version=OUTCOME_VERSION))
        return None, source_interpretation
    rejected = []
    for index, outcome_request in enumerate(outcome_requests, 1):
        call_id = f"summary_outcome_{support_count}"
        if len(outcome_requests) > 1:
            call_id += f"_chunk_{index}"
        outcome_review, _, mark = review_stage(summary, call_id,
            lambda: outcome_request, validate_outcome)
        mark("rejected" if outcome_review.status == "contradicted" else outcome_review.status)
        rejected.extend(outcome_review.issues)
    # Every group has its own receipt; resolve the complete candidate
    # once so chunking cannot multiply generation corrections.
    return resolve(rejected, lambda status: None), source_interpretation
