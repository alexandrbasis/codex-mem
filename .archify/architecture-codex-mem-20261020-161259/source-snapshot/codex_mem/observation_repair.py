"""Exact, provenance-preserving maintenance of completed observer outputs.

Explicit replacement text is a proposal. It passes the same packet validators
and semantic review sequence as ordinary processing before a guarded commit.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict
import copy
import time

from .store import Store, StoreError, ObservationEvidenceChanged, project_key
from .summary_support import _fingerprint


def _settings_guard(settings, profile):
    return _fingerprint(dict(profile=profile.to_dict(), settings=dict(settings),
                             valid=getattr(settings, "valid", True)))


def _model_note(value):
    """Represent only storage-normalized empty metadata as empty wire text."""
    if not isinstance(value, Mapping):
        return value
    value = copy.deepcopy(dict(value))
    if value.get("observation") is None:
        value.pop("observation", None)
    elif isinstance(value["observation"], Mapping):
        for field in ("subtitle", "narrative"):
            if value["observation"].get(field) is None and field in value["observation"]:
                value["observation"][field] = ""
    return value


def _model_summary(value):
    return ({key: ("" if item is None else item) for key, item in value.items()}
            if isinstance(value, Mapping) else value)


def inspect_processed_repair(project, data_dir=None, *, job_id, expected_attempt_count, output_ids):
    """Return a body-free exact guard to review before authorizing a repair."""
    from .config import load_config
    from .execution import resolve_profile
    settings = load_config(data_dir)
    if not getattr(settings, "valid", True):
        raise ValueError("configuration is unavailable")
    profile = resolve_profile(settings, "observation")
    with Store(data_dir) as store:
        snapshot = store.observation_repair_snapshot(project, job_id,
            expected_attempt_count=expected_attempt_count, output_ids=output_ids)
    return dict(snapshot["guard"], settings_fingerprint=_settings_guard(settings, profile))


def repair_processed_outputs(project, data_dir=None, *, guard, replacements, timeout=240,
                             codex="codex", summary_reviewer=None,
                             jev_evaluator=None, jev_quality_evaluator=None):
    """Review explicit replacements and supersede exactly the guarded outputs.

    Replacement keys are old output IDs. Values are complete note objects or
    complete session_summary objects, preserving their original kind. No model
    generator or explicit-memory tool is used. Identical successful calls reuse
    their separate repair receipt and never reopen the completed queue job.
    """
    from .config import load_config, automatic_capture_enabled
    from .execution import resolve_profile, execute, ExecutionRequest
    from .processor import (ProcessorFailure, NativeProcessorRunner,
        _validate_timeout, _validate_model_output, _filtered_lifecycle_claim)
    from .store import _validate_observation_notes
    from .summary_support import SupportError, diagnostic, safe_diagnostic
    from .summary_outcome import SOURCE_VERSION, OUTCOME_VERSION
    from .observation_review import review_packet
    from .jev_client import enabled
    workspace = project_key(project)
    timeout = _validate_timeout(timeout)
    guard, replacements = copy.deepcopy(dict(guard)), copy.deepcopy(dict(replacements))
    settings = load_config(data_dir)
    if not getattr(settings, "valid", True):
        return dict(status="blocked", code="configuration_unavailable")
    profile = resolve_profile(settings, "observation", timeout=timeout)
    if guard.pop("settings_fingerprint", None) != _settings_guard(settings, profile):
        return dict(status="blocked", code="repair_settings_changed")
    if not settings.get("processor_enabled", True) or not automatic_capture_enabled(workspace, settings):
        return dict(status="blocked", code="repair_not_selected")
    if set(replacements) != set(guard["selected_output_ids"]):
        raise ValueError("replacements must match selected output IDs")
    repair_key = _fingerprint(dict(project=workspace, guard=guard,
                                   replacements=replacements, profile=profile.to_dict()))
    diagnostics, calls = [], []
    receipt = dict(execution_profile=profile.to_dict(), summary_support=diagnostics,
                   execution_calls=calls, proposal_fingerprint=_fingerprint(replacements))
    with Store(data_dir) as store:
        previous = store.observation_repair_receipt(workspace, repair_key)
        if previous is not None:
            return dict(previous, deduplicated=True)
        try:
            snapshot = store.observation_repair_snapshot(workspace, guard["job_id"],
                expected_attempt_count=guard["expected_attempt_count"], output_ids=guard["selected_output_ids"])
            if snapshot["guard"] != guard:
                raise ObservationEvidenceChanged("Processed observation evidence changed")
            claim = snapshot["claim"]
            if profile.to_dict() != guard["execution_profile"]:
                return dict(receipt, status="blocked", code="repair_profile_changed")
            deadline = time.monotonic() + profile.timeout

            def settings_guard():
                if _settings_guard(load_config(data_dir), profile) != _settings_guard(settings, profile):
                    raise ObservationEvidenceChanged("Observation repair settings changed")

            def source_guard(*_):
                if time.monotonic() >= deadline:
                    raise ProcessorFailure("timeout")
                settings_guard()
                store.check_observation_repair(workspace, guard)

            def current():
                source_guard()
                return True

            source_guard()
            records = snapshot["outputs"]
            notes, summary = [], None
            note_ids, summary_id = [], None
            for record in records:
                old_id = record["id"]
                if record["kind"] == "note":
                    value = replacements.get(old_id, {key: record[key] for key in
                        ("title", "body", "tags", "source_ids", "observation") if key in record
                        and (key != "observation" or isinstance(record[key], Mapping))})
                    notes.append(_model_note(value))
                    note_ids.append(old_id)
                elif record["kind"] == "session_summary":
                    summary = _model_summary(replacements.get(old_id, record["session_summary"]))
                    summary_id = old_id
                else:
                    raise ValueError("unsupported observation repair kind")
            packet = dict(disposition="processed", notes=notes, session_summary=summary)
            notes, _, summary = _validate_model_output(packet, claim["sources"])
            # Apply the same privacy and storage normalization before any
            # semantic dispatch. Model-output validation only trims note prose.
            notes = _validate_observation_notes(notes)
            if enabled(settings, "jev_filter_enabled", workspace):
                from .jev_filter import filter_claim, JevFilterError
                try:
                    filtered, audit = filter_claim(claim, timeout=min(60, deadline-time.monotonic()),
                        evaluator=jev_evaluator, key_file=settings.get("jev_filter_key_file", ""),
                        before_dispatch=source_guard)
                except JevFilterError as exc:
                    raise ProcessorFailure("invalid_response", reason_code=exc.code) from None
                claim = _filtered_lifecycle_claim(claim, filtered, audit)
                receipt["jev_filter"] = audit
            # Reapply the existing ownership validator to the filtered source
            # set, using the already redacted canonical packet.
            notes, _, summary = _validate_model_output(dict(disposition="processed",
                notes=[_model_note(note) for note in notes], session_summary=_model_summary(summary)),
                claim["sources"])
            # Commit exactly the canonical values reviewed below, after all
            # redaction, whitespace and structured-metadata normalization.
            selected = {old_id: dict(kind="note", value=value)
                        for old_id, value in zip(note_ids, notes) if old_id in replacements}
            if summary_id in replacements:
                selected[summary_id] = dict(kind="session_summary", value=summary)
            if receipt.get("jev_filter"):
                lifecycle = set(receipt["jev_filter"].get("lifecycle_only_ids", []))
                if any(lifecycle.intersection(note["source_ids"]) for note in notes):
                    raise ProcessorFailure("invalid_response", reason_code="source_attribution_conflict")

            def review_stage(candidate, call_id, prepare_call, validate_call):
                source_guard()
                version = (SOURCE_VERSION if call_id.startswith("summary_source") else
                    OUTCOME_VERSION if call_id.startswith("summary_outcome") else None)
                detail = diagnostic(candidate, claim, **({"version": version} if version else {}))
                diagnostics.append(detail)
                call = dict(call_id=call_id, profile=profile.to_dict(), outcome="pending")
                calls.append(call)

                def mark(status, invalid_subtype=None, *, error=None):
                    detail.update(status=status, invalid_subtype=invalid_subtype)
                    if isinstance(error, SupportError) and error.rule_code is not None:
                        detail.update(rule_code=error.rule_code, unit_id=error.unit_id)
                    safe_diagnostic(detail)

                try:
                    prompt, schema, payload = prepare_call()
                    reviewer = summary_reviewer
                    if reviewer is None:
                        if profile.runtime == "pi":
                            from .pi_runtime import PiProcessorRunner
                            reviewer = PiProcessorRunner(before_dispatch=source_guard)
                        else:
                            reviewer = NativeProcessorRunner(codex=codex, timeout=profile.timeout,
                                                             before_dispatch=source_guard)
                    request = ExecutionRequest(prompt=prompt, output_schema=schema, profile=profile,
                        deadline=deadline, job_type="summary_support",
                        attempt_id=f"repair:{repair_key}:{call_id}", context=payload)
                    source_guard()
                    result = execute(request, reviewer)
                    call.update(outcome="failed" if result.failure else "success",
                        metrics=result.metrics, identity=asdict(result.identity) if result.identity else None,
                        failure=asdict(result.failure) if result.failure else None)
                    result.require_success()
                    if result.identity is None or any(getattr(result.identity, key) != getattr(profile, key)
                            for key in ("runtime", "provider", "model", "reasoning_effort")):
                        raise ProcessorFailure("model_mismatch")
                    source_guard()
                    validated = validate_call(result.output, payload)
                    return validated, payload, mark
                except SupportError as exc:
                    mark("invalid", exc.subtype, error=exc)
                    raise ProcessorFailure("invalid_response", reason_code="summary_review_malformed") from None
                except (TypeError, ValueError, KeyError):
                    mark("invalid", "structure")
                    raise ProcessorFailure("invalid_response", reason_code="summary_support_invalid") from None
                except ProcessorFailure:
                    call["outcome"] = "failed"
                    mark("unavailable")
                    raise
                except ObservationEvidenceChanged:
                    call["outcome"] = "failed"
                    mark("unavailable")
                    raise
                except Exception:
                    call["outcome"] = "failed"
                    mark("unavailable")
                    raise ProcessorFailure("invalid_response", reason_code="summary_support_unavailable") from None

            def resolve(issues, mark, accepted_status="supported"):
                reason = ("summary_support_uncertain" if issues and all(
                    issue["verdict"] == "insufficient_context" for issue in issues)
                    else "summary_support_rejected" if issues else None)
                mark("uncertain" if reason == "summary_support_uncertain" else "rejected" if issues else accepted_status)
                source_guard()
                if reason:
                    raise ProcessorFailure("invalid_response", reason_code=reason)
                return None

            review_packet(summary, notes, claim, support_count=1, source_interpretation=None,
                review_stage=review_stage, resolve=resolve, source_guard=source_guard,
                append_diagnostic=diagnostics.append)
            if enabled(settings, "jev_quality_enabled", workspace):
                from .jev_quality import quality_gate, JevQualityError, MAX_GATE_SECONDS
                try:
                    receipt["jev_quality"] = quality_gate(notes, summary, claim, project=workspace,
                        timeout=min(MAX_GATE_SECONDS, deadline-time.monotonic()),
                        key_file=settings.get("jev_filter_key_file", ""), evaluator=jev_quality_evaluator,
                        source_guard=current)
                except JevQualityError as exc:
                    receipt["jev_quality"] = exc.audit
                    raise ProcessorFailure("invalid_response", reason_code=exc.code) from None
            source_guard()
            return store.commit_observation_repair(workspace, guard, repair_key=repair_key,
                replacements=selected, receipt=receipt, settings_guard=settings_guard)
        except ObservationEvidenceChanged:
            return dict(receipt, status="blocked", code="repair_evidence_changed")
        except ProcessorFailure as exc:
            return dict(receipt, status="failed", code=exc.code, reason_code=exc.reason_code)
        except StoreError as exc:
            from .observation_diagnostics import storage_failure_detail
            return dict(receipt, status="failed", code="storage_failure",
                        failure_detail=storage_failure_detail(exc, "finalize", preclaim=False))
