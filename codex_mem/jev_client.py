"""Bounded typed TypeSafe evaluations with project-scoped, content-free caching.

Only code-owned questions and normalized judgments are persisted. Input hashes
include the complete redacted state, questions, pinned model and policy version.
Cache/audit connections have a short busy timeout so they cannot stall a hook.
"""
from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import re
import sqlite3
import time
from typing import Any

from .config import is_excluded_project
from .jev_filter import MODEL, MAX_PAYLOAD_BYTES, JevFilterError, _post
from .privacy import REDACTED, _SECRET_NAME, redact_text
from .store import project_key

MAX_CACHE_ENTRIES = 10_000
MAX_AUDIT_ENTRIES = 2_000
# This operational byte budget preserves complete quality-check evidence. It is
# not a token estimate; provider context validation still applies. Retrieval and
# eligibility keep their separate 24 KB latency-sensitive input budget.
MAX_QUALITY_PAYLOAD_BYTES = 96_000
ERROR_CODES = frozenset({"jev_failure", "jev_credentials", "jev_timeout", "jev_transport",
                         "jev_invalid_response", "jev_invalid_input", "jev_input_limit",
                         "jev_source_unavailable"})
_NAME = re.compile(r"[A-Za-z][A-Za-z0-9_]{0,63}\Z")
_POLICY = re.compile(r"[a-z][a-z0-9_-]{0,95}\Z")
_SECRET_FIELD = re.compile(_SECRET_NAME + r"\Z", re.IGNORECASE)


class JevError(RuntimeError):
    def __init__(self, code: str, *, audit: Mapping[str, Any] | None = None) -> None:
        self.code = code if code in ERROR_CODES else "jev_failure"
        self.audit = dict(audit or {})
        super().__init__(self.code)


def enabled(settings: Mapping[str, Any], flag: str, project: str | Path) -> bool:
    """Each new remote use is opt-in; exclusions always win over its scope."""
    if (flag not in {"jev_quality_enabled", "jev_retrieval_enabled"}
            or not getattr(settings, "valid", True) or settings.get(flag) is not True
            or is_excluded_project(project, settings)):
        return False
    scope = "jev_retrieval_projects" if flag == "jev_retrieval_enabled" else "jev_filter_projects"
    scopes = [settings.get(scope, [])]
    if flag == "jev_quality_enabled":
        scopes.append(settings.get("jev_quality_projects", []))
    if any(not isinstance(roots, list) for roots in scopes):
        return False
    try:
        workspace = Path(project_key(project))
        return all(not roots or any(workspace == Path(project_key(root))
                   or Path(project_key(root)) in workspace.parents for root in roots) for roots in scopes)
    except (TypeError, ValueError, OSError):
        return False


def _encode(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True,
                      separators=(",", ":")).encode("utf-8")


def payload_bytes(state: Any, questions: Mapping[str, Any]) -> int:
    return len(_encode({"model": MODEL, "state": _safe_state(state), "questions": questions}))


def _safe_state(state: Any) -> Any:
    # Preserve valid JSON for numeric/structured credentials as well as strings.
    if isinstance(state, Mapping):
        return {key: REDACTED if isinstance(key, str) and _SECRET_FIELD.fullmatch(key)
                else _safe_state(value) for key, value in state.items()}
    if isinstance(state, list):
        return [_safe_state(value) for value in state]
    return redact_text(state) if isinstance(state, str) else state


def _prob(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not 0 <= value <= 1:
        raise JevError("jev_invalid_response")
    return float(value)


def _integer(value: Any) -> int:
    if type(value) is not int or not 0 <= value < 2**63:
        raise JevError("jev_invalid_response")
    return value


def _validate_questions(questions: Any) -> None:
    if not isinstance(questions, Mapping) or not 1 <= len(questions) <= 64:
        raise JevError("jev_invalid_input")
    for name, question in questions.items():
        if not isinstance(name, str) or not _NAME.fullmatch(name) or not isinstance(question, Mapping):
            raise JevError("jev_invalid_input")
        if not isinstance(question.get("instructions"), (str, list, Mapping)) or not question["instructions"]:
            raise JevError("jev_invalid_input")
        kind, criteria = question.get("type"), question.get("criteria")
        if kind == "noul":
            if criteria is not None and (not isinstance(criteria, Mapping) or set(criteria) != {"true", "false"}):
                raise JevError("jev_invalid_input")
        elif kind == "choice":
            if (not isinstance(criteria, Mapping) or not 2 <= len(criteria) <= 255
                    or any(not isinstance(key, str) or not _NAME.fullmatch(key) for key in criteria)):
                raise JevError("jev_invalid_input")
        elif kind == "score":
            if not isinstance(criteria, list) or not 2 <= len(criteria) <= 10:
                raise JevError("jev_invalid_input")
        else:
            raise JevError("jev_invalid_input")


def _validate_response(response: Any, questions: Mapping[str, Any], *, cached: bool = False) -> dict[str, Any]:
    """Rebuild allowed typed fields; never copy arbitrary provider text."""
    try:
        if not isinstance(response, Mapping) or response.get("model") != MODEL:
            raise ValueError()
        raw = response["answers"]
        if not isinstance(raw, Mapping) or set(raw) != set(questions):
            raise ValueError()
        clean = {}
        for name, question in questions.items():
            answer, kind = raw[name], question["type"]
            if not isinstance(answer, Mapping) or answer.get("type") != kind:
                raise ValueError()
            if kind == "noul":
                clean[name] = {"type": kind, "noul": _prob(answer["noul"])}
                continue
            criteria = question["criteria"]
            options = set(criteria) if kind == "choice" else {str(i) for i in range(len(criteria))}
            distribution = answer["probabilities"]
            if not isinstance(distribution, Mapping) or set(distribution) != options:
                raise ValueError()
            probabilities = {key: _prob(distribution[key]) for key in sorted(options)}
            if not math.isclose(sum(probabilities.values()), 1, abs_tol=len(options) * .005 + 1e-9):
                raise ValueError()
            value = {"type": kind, "probabilities": probabilities, "confidence": _prob(answer["confidence"])}
            if kind == "choice":
                choice = answer["choice"]
                if choice not in options or probabilities[choice] < max(probabilities.values()):
                    raise ValueError()
                value["choice"] = choice
            else:
                score = answer["score"]
                if (isinstance(score, bool) or not isinstance(score, (int, float))
                        or not math.isfinite(score) or not 0 <= score <= len(criteria) - 1):
                    raise ValueError()
                expected = sum(int(key) * probability for key, probability in probabilities.items())
                # The API rounds probabilities. Preserve its weighted score within that precision.
                if not math.isclose(score, expected, abs_tol=.01 + .005 * sum(range(len(criteria)))):
                    raise ValueError()
                if not cached:
                    legend = answer["legend"]
                    if not isinstance(legend, Mapping) or set(legend) != options:
                        raise ValueError()
                    if any(legend[str(i)] != level for i, level in enumerate(criteria)):
                        raise ValueError()
                value["score"] = float(score)
            clean[name] = value
        usage = response["usage"]
        if not isinstance(usage, Mapping):
            raise ValueError()
        return {"model": MODEL, "answers": clean, "usage": {
            key: _integer(usage[key]) for key in ("input_tokens", "output_tokens")}}
    except JevError:
        raise
    except Exception:
        raise JevError("jev_invalid_response") from None


def _connection(store: Any, *, readonly: bool = False) -> sqlite3.Connection:
    # A separate, short-lived connection keeps inference/audit off Store's lock.
    uri = Path(store.db_path).resolve().as_uri() + ("?mode=ro" if readonly else "?mode=rw")
    return sqlite3.connect(uri, uri=True, timeout=.02)


def _cache_get(store: Any, workspace: str, key: str, questions: Mapping[str, Any]) -> dict[str, Any] | None:
    if store is None:
        return None
    connection = None
    try:
        connection = _connection(store, readonly=True)
        row = connection.execute("SELECT response_json FROM jev_judgment_cache WHERE project=? AND cache_key=?",
                                 (workspace, key)).fetchone()
        if row:
            return _validate_response(json.loads(row[0]), questions, cached=True)
    except (sqlite3.Error, OSError, ValueError, AttributeError, JevError):
        pass
    finally:
        if connection is not None:
            connection.close()
    return None


def _cache_put(store: Any, workspace: str, key: str, response: Mapping[str, Any]) -> None:
    if store is None:
        return
    safe = {"model": MODEL, "answers": response["answers"], "usage": {"input_tokens": 0, "output_tokens": 0}}
    connection = None
    try:
        connection = _connection(store)
        with connection:
            connection.execute("CREATE TABLE IF NOT EXISTS jev_judgment_cache (project TEXT NOT NULL, "
                               "cache_key TEXT NOT NULL, response_json TEXT NOT NULL, created_at TEXT NOT NULL, "
                               "PRIMARY KEY(project,cache_key))")
            connection.execute("CREATE INDEX IF NOT EXISTS jev_judgment_cache_order "
                               "ON jev_judgment_cache(project,created_at,cache_key)")
            connection.execute("INSERT OR REPLACE INTO jev_judgment_cache VALUES (?,?,?,?)",
                               (workspace, key, _encode(safe).decode(), datetime.now(timezone.utc).isoformat()))
            connection.execute("DELETE FROM jev_judgment_cache WHERE project=? AND cache_key IN "
                               "(SELECT cache_key FROM jev_judgment_cache WHERE project=? "
                               "ORDER BY created_at DESC,cache_key DESC LIMIT -1 OFFSET ?)",
                               (workspace, workspace, MAX_CACHE_ENTRIES))
    except (sqlite3.Error, OSError, ValueError, AttributeError):
        pass
    finally:
        if connection is not None:
            connection.close()


def evaluate(*, state: Any, questions: Mapping[str, Any], policy_version: str,
             project: str | Path, store: Any = None, timeout: float = 20,
             key_file: str = "", evaluator: Callable | None = None,
             before_dispatch: Callable[[], bool] | None = None,
             max_payload_bytes: int = MAX_PAYLOAD_BYTES) -> tuple[dict[str, Any], dict[str, Any]]:
    """Evaluate independent questions once, or reuse a validated exact-input hit."""
    started = time.monotonic()
    audit: dict[str, Any] = {"model": MODEL, "policy_version": policy_version, "status": "failure",
        "evaluation_source": "none", "counts": {"requests": 0, "cache_hits": 0},
        "usage": {"input_tokens": 0, "output_tokens": 0}, "usage_status": "reported", "answers": {}}

    def finish() -> dict[str, Any]:
        audit["duration_ms"] = max(0, round((time.monotonic() - started) * 1000))
        return audit

    try:
        if (isinstance(timeout, bool) or not isinstance(timeout, (int, float))
                or not math.isfinite(timeout) or not 0 < timeout <= 60
                or not isinstance(policy_version, str) or not _POLICY.fullmatch(policy_version)
                or type(max_payload_bytes) is not int
                or not 1 <= max_payload_bytes <= MAX_QUALITY_PAYLOAD_BYTES):
            raise JevError("jev_invalid_input")
        deadline = started + timeout
        workspace = project_key(project)
        _validate_questions(questions)
        payload = {"model": MODEL, "state": _safe_state(state), "questions": questions}
        encoded = _encode(payload)
        if len(encoded) > max_payload_bytes:
            raise JevError("jev_input_limit")
        key = hashlib.sha256(_encode({"policy_version": policy_version, "payload": payload})).hexdigest()
        audit["cache_key"] = key
        cached = _cache_get(store, workspace, key, questions)
        if time.monotonic() >= deadline:
            raise JevError("jev_timeout")
        if before_dispatch is not None:
            try:
                permitted = before_dispatch() is True
            except Exception:
                permitted = False
            if not permitted:
                raise JevError("jev_source_unavailable")
            if time.monotonic() >= deadline:
                raise JevError("jev_timeout")
        if cached is not None:
            audit.update(status="success", evaluation_source="cache", answers=cached["answers"])
            audit["counts"]["cache_hits"] = 1
            return cached["answers"], finish()
        audit["counts"]["requests"] = 1
        audit["evaluation_source"] = "live"
        audit["usage_status"] = "unavailable"
        try:
            response = evaluator(payload) if evaluator else _post(payload, deadline, key_file)
        except JevFilterError as exc:
            raise JevError(exc.code.replace("jev_filter_", "jev_")) from None
        except JevError:
            raise
        except Exception:
            raise JevError("jev_transport") from None
        # Keep reported paid usage even if an answer or model fails validation.
        if isinstance(response, Mapping) and isinstance(response.get("usage"), Mapping):
            try:
                audit["usage"] = {key: _integer(response["usage"][key]) for key in ("input_tokens", "output_tokens")}
                audit["usage_status"] = "partial"
            except (KeyError, JevError):
                pass
        clean = _validate_response(response, questions)
        audit["answers"] = clean["answers"]
        if time.monotonic() >= deadline:
            raise JevError("jev_timeout")
        _cache_put(store, workspace, key, clean)
        if time.monotonic() >= deadline:
            raise JevError("jev_timeout")
        audit.update(status="success", usage_status="reported")
        return clean["answers"], finish()
    except JevError as exc:
        audit["error_code"] = exc.code
        raise JevError(exc.code, audit=finish()) from None
    except Exception:
        audit["error_code"] = "jev_invalid_input"
        raise JevError("jev_invalid_input", audit=finish()) from None


def _audit_answers(raw: Any) -> dict[str, Any]:
    """Whitelist independently of the caller; arbitrary fields never reach disk."""
    if not isinstance(raw, Mapping) or len(raw) > 64:
        raise ValueError("invalid judgment audit")
    result = {}
    for name, answer in raw.items():
        if not isinstance(name, str) or not _NAME.fullmatch(name) or not isinstance(answer, Mapping):
            raise ValueError("invalid judgment audit")
        kind = answer.get("type")
        if kind == "noul":
            result[name] = {"type": kind, "noul": _prob(answer["noul"])}
        elif kind in {"choice", "score"}:
            probabilities = answer["probabilities"]
            if not isinstance(probabilities, Mapping) or not 2 <= len(probabilities) <= 255:
                raise ValueError("invalid judgment audit")
            if any(not isinstance(key, str) or not re.fullmatch(r"[A-Za-z0-9_]{1,64}", key) for key in probabilities):
                raise ValueError("invalid judgment audit")
            value = {"type": kind, "confidence": _prob(answer["confidence"]),
                     "probabilities": {key: _prob(prob) for key, prob in probabilities.items()}}
            if kind == "choice":
                if answer["choice"] not in probabilities:
                    raise ValueError("invalid judgment audit")
                value["choice"] = answer["choice"]
            else:
                score = answer["score"]
                if isinstance(score, bool) or not isinstance(score, (float, int)) or not math.isfinite(score) or not 0 <= score <= 9:
                    raise ValueError("invalid judgment audit")
                value["score"] = score
            result[name] = value
        else:
            raise ValueError("invalid judgment audit")
    return result


def record_audit(store: Any, project: str | Path, audit: Mapping[str, Any], *, route: str,
                 owner_id: str | None = None) -> bool:
    """Save bounded metadata. Failure is visible to callers, never a long retry."""
    connection = None
    try:
        if (not isinstance(route, str) or not re.fullmatch(r"[a-z_]{1,64}", route)
                or not isinstance(audit.get("policy_version"), str) or not _POLICY.fullmatch(audit["policy_version"])
                or audit.get("model") != MODEL or audit.get("status") not in {"success", "failure"}
                or audit.get("evaluation_source") not in {"live", "cache", "none"}
                or audit.get("usage_status") not in {"reported", "partial", "unavailable"}):
            raise ValueError("invalid judgment audit")
        if owner_id is not None and (not isinstance(owner_id, str) or not re.fullmatch(r"[A-Za-z0-9._:-]{1,256}", owner_id)):
            raise ValueError("invalid judgment owner")
        clean = {key: audit[key] for key in ("model", "policy_version", "status", "evaluation_source", "usage_status")}
        clean.update(route=route, duration_ms=_integer(audit.get("duration_ms", 0)), answers=_audit_answers(audit.get("answers", {})))
        clean["counts"] = {key: _integer(audit["counts"][key]) for key in ("requests", "cache_hits")}
        clean["usage"] = {key: _integer(audit["usage"][key]) for key in ("input_tokens", "output_tokens")}
        if "cache_key" in audit:
            if not isinstance(audit["cache_key"], str) or not re.fullmatch(r"[0-9a-f]{64}", audit["cache_key"]):
                raise ValueError("invalid judgment key")
            clean["cache_key"] = audit["cache_key"]
        if "error_code" in audit:
            if audit["error_code"] not in ERROR_CODES:
                raise ValueError("invalid judgment error")
            clean["error_code"] = audit["error_code"]
        workspace = project_key(project)
        connection = _connection(store)
        with connection:
            connection.execute("CREATE TABLE IF NOT EXISTS jev_judgment_audits (id INTEGER PRIMARY KEY, "
                               "project TEXT NOT NULL, owner_id TEXT, audit_json TEXT NOT NULL, created_at TEXT NOT NULL)")
            connection.execute("CREATE INDEX IF NOT EXISTS jev_judgment_audits_project ON jev_judgment_audits(project,id)")
            connection.execute("INSERT INTO jev_judgment_audits(project,owner_id,audit_json,created_at) VALUES (?,?,?,?)",
                               (workspace, owner_id, _encode(clean).decode(), datetime.now(timezone.utc).isoformat()))
            connection.execute("DELETE FROM jev_judgment_audits WHERE project=? AND id IN "
                               "(SELECT id FROM jev_judgment_audits WHERE project=? ORDER BY id DESC LIMIT -1 OFFSET ?)",
                               (workspace, workspace, MAX_AUDIT_ENTRIES))
        return True
    except (KeyError, TypeError, ValueError, AttributeError, OSError, sqlite3.Error, JevError):
        return False
    finally:
        if connection is not None:
            connection.close()
