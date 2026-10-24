"""Shared background execution contracts, independent of the interactive host.

Profiles describe requested execution. Runtime adapters establish actual identity
and completion. Processors still own source validation and atomic memory writes.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict, dataclass
import math
from typing import Any

from .store import OBSERVATION_MODEL, OBSERVATION_REASONING_EFFORT

PROFILE_KEYS = ('processing_profile', 'observation_profile', 'project_memory_profile')
REASONING_LEVELS = frozenset({'none', 'minimal', 'low', 'medium', 'high', 'xhigh', 'max', 'ultra'})


@dataclass(frozen=True)
class ExecutionProfile:
    runtime: str
    provider: str
    model: str
    reasoning_effort: str | None
    timeout: float

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def normalize_profile(value: Any) -> dict[str, Any] | None:
    """Validate a complete profile without guessing identifiers or capabilities."""
    if value is None:
        return None
    if not isinstance(value, Mapping) or set(value) != {
        'runtime', 'provider', 'model', 'reasoning_effort', 'timeout'
    }:
        raise ValueError('execution profile requires runtime, provider, model, reasoning_effort, and timeout')
    if value['runtime'] not in ('codex', 'pi'):
        raise ValueError('execution profile runtime must be codex or pi')
    for key in ('provider', 'model'):
        identifier = value[key]
        if (not isinstance(identifier, str) or not 1 <= len(identifier) <= 256
                or any(character.isspace() or ord(character) < 32 for character in identifier)
                or any(character in identifier for character in '*?\x7f')):
            raise ValueError('execution profile requires exact provider and model identifiers')
    effort = value['reasoning_effort']
    if effort is not None and (not isinstance(effort, str) or effort not in REASONING_LEVELS):
        raise ValueError('execution profile reasoning_effort is invalid')
    timeout = value['timeout']
    if (isinstance(timeout, bool) or not isinstance(timeout, (int, float))
            or not 1 <= timeout <= 600 or not math.isfinite(timeout)):
        raise ValueError('execution profile timeout must be between 1 and 600 seconds')
    return dict(value, timeout=float(timeout))


def legacy_profile(timeout: float = 240) -> ExecutionProfile:
    return ExecutionProfile(**normalize_profile(dict(runtime='codex', provider='openai',
        model=OBSERVATION_MODEL, reasoning_effort=OBSERVATION_REASONING_EFFORT, timeout=timeout)))


def resolve_profile(settings: Mapping[str, Any], processor: str, *, timeout: float = 240) -> ExecutionProfile:
    """Resolve override, default, then legacy. Never replace an invalid profile."""
    if processor not in ('observation', 'project_memory'):
        raise ValueError('processor must be observation or project_memory')
    if not getattr(settings, 'valid', True):
        raise ValueError('execution profile configuration is unavailable')
    profiles = {key: normalize_profile(settings.get(key)) for key in PROFILE_KEYS}
    selected = profiles[processor + '_profile'] or profiles['processing_profile']
    return ExecutionProfile(**selected) if selected is not None else legacy_profile(timeout)


@dataclass(frozen=True)
class ExecutionRequest:
    prompt: str
    output_schema: Mapping[str, Any]
    job_type: str
    profile: ExecutionProfile
    deadline: float
    attempt_id: str
    context: Mapping[str, Any] | None = None

    def __post_init__(self) -> None:
        import copy
        import re
        if not isinstance(self.prompt, str) or len(self.prompt) > 1_250_000:
            raise ValueError('execution input exceeds the bounded contract')
        if not isinstance(self.output_schema, Mapping):
            raise ValueError('execution output_schema must be an object')
        if self.job_type not in ('observation', 'summary', 'summary_support', 'project_memory', 'reconsideration', 'consolidation'):
            raise ValueError('execution job_type is invalid')
        if not isinstance(self.profile, ExecutionProfile):
            raise ValueError('execution profile is invalid')
        normalize_profile(self.profile.to_dict())
        if (isinstance(self.deadline, bool) or not isinstance(self.deadline, (int, float))
                or not math.isfinite(self.deadline)):
            raise ValueError('execution deadline must be a monotonic timestamp')
        if not isinstance(self.attempt_id, str) or not re.fullmatch(r'[A-Za-z0-9._:-]{1,256}', self.attempt_id):
            raise ValueError('execution attempt_id is invalid')
        # Snapshot caller-owned containers before any external execution.
        object.__setattr__(self, 'output_schema', copy.deepcopy(dict(self.output_schema)))
        object.__setattr__(self, 'context', copy.deepcopy(dict(self.context or {})))

    @classmethod
    def from_legacy(cls, value: Mapping[str, Any], *, job_type: str, attempt_id: str,
                    deadline: float | None = None) -> ExecutionRequest:
        import time
        profile = legacy_profile(value['timeout'])
        return cls(prompt=value['prompt'], output_schema=value['output_schema'], profile=profile,
                   job_type=job_type, attempt_id=attempt_id,
                   deadline=deadline if deadline is not None else time.monotonic() + profile.timeout,
                   context={key: item for key, item in value.items()
                            if key not in {'prompt', 'output_schema', 'model', 'reasoning_effort', 'timeout'}})

    def legacy_request(self) -> dict[str, Any]:
        import copy
        return dict(copy.deepcopy(dict(self.context or {})), prompt=self.prompt,
                    output_schema=copy.deepcopy(dict(self.output_schema)), model=self.profile.model,
                    reasoning_effort=self.profile.reasoning_effort, timeout=self.profile.timeout,
                    job_type=self.job_type, attempt_id=self.attempt_id, deadline=self.deadline,
                    execution_profile=self.profile.to_dict())


@dataclass(frozen=True)
class ExecutionIdentity:
    runtime: str
    provider: str
    model: str
    reasoning_effort: str | None
    execution_id: str
    turn_id: str | None


@dataclass(frozen=True)
class ExecutionFailure:
    reason: str
    code: str
    reason_code: str | None = None
    execution_id: str | None = None
    turn_id: str | None = None


@dataclass(frozen=True)
class ExecutionResult:
    output: Mapping[str, Any] | None = None
    identity: ExecutionIdentity | None = None
    metrics: Mapping[str, Any] | None = None
    failure: ExecutionFailure | None = None

    @property
    def usage(self) -> Mapping[str, Any]:
        value = (self.metrics or {}).get('usage')
        return value if isinstance(value, Mapping) else {'status': 'unavailable', 'tokens': None}

    def require_success(self) -> None:
        """Bridge existing queue error handling without storing remote prose."""
        from .processor import ProcessorFailure
        if self.failure is not None:
            raise ProcessorFailure(self.failure.code, reason_code=self.failure.reason_code,
                worker_thread_id=self.failure.execution_id, worker_turn_id=self.failure.turn_id,
                metrics=self.metrics)
        if self.output is None or self.identity is None:
            raise ProcessorFailure('invalid_response', reason_code='invalid_runner_receipt', metrics=self.metrics)


def execute_codex(request: ExecutionRequest, runner: Any) -> ExecutionResult:
    """Adapt the strict Codex receipt, including failed-call usage, once."""
    import time
    from .processor import ProcessorFailure, _invoke_runner, _validate_runner_receipt
    metrics = None
    thread_id = turn_id = None
    try:
        profile = request.profile
        if profile.runtime != 'codex':
            raise ProcessorFailure('runner_unavailable', reason_code='unsupported_runtime')
        # The Codex catalog exposes no provider-qualified capability discovery.
        # Support its OpenAI catalog only, without guessing custom providers.
        if profile.provider != 'openai' or profile.reasoning_effort is None:
            raise ProcessorFailure('model_unavailable', reason_code=(
                'unsupported_provider' if profile.provider != 'openai' else 'unsupported_reasoning'))
        if time.monotonic() >= request.deadline:
            raise ProcessorFailure('timeout')
        value = _invoke_runner(runner, request.legacy_request())
        if isinstance(value.get('metrics'), Mapping):
            metrics = value['metrics']
        output, evidence, thread_id, turn_id = _validate_runner_receipt(value, profile=profile)
        if time.monotonic() >= request.deadline:
            raise ProcessorFailure('timeout')
        actual = evidence['thread_start']
        return ExecutionResult(output=output, identity=ExecutionIdentity(runtime='codex',
            provider=actual['model_provider'], model=actual['model'],
            reasoning_effort=actual['reasoning_effort'], execution_id=thread_id, turn_id=turn_id), metrics=metrics)
    except ProcessorFailure as error:
        reason = {'timeout': 'timeout', 'runner_unavailable': 'runtime_unavailable',
            'model_unavailable': 'configuration', 'invalid_request': 'configuration',
            'invalid_response': 'invalid_output', 'model_mismatch': 'identity_mismatch',
            'rerouted': 'identity_mismatch', 'tool_called': 'forbidden_tools',
            'tools_available': 'forbidden_tools', 'protocol_error': 'protocol',
            'lease_expired': 'cancelled', 'storage_failure': 'storage'}.get(error.code, 'runtime_failure')
        if error.reason_code == 'native_turn_cancelled':
            reason = 'cancelled'
        return ExecutionResult(metrics=error.metrics or metrics, failure=ExecutionFailure(reason,
            error.code, error.reason_code, error.worker_thread_id or thread_id, error.worker_turn_id or turn_id))


def execute(request: ExecutionRequest, runner: Any) -> ExecutionResult:
    """Dispatch a runtime adapter. Legacy injected runners retain the Codex contract."""
    method = getattr(type(runner), 'execute', None)
    if callable(method):
        return runner.execute(request)
    return execute_codex(request, runner)


def pi_execution_receipt(value: Any) -> dict[str, Any] | None:
    """Keep only bounded Pi execution facts, never native messages or prose."""
    fields = {"runtime", "version", "protocol", "session_id", "generation", "settled", "identity_verified",
              "tools_policy", "tool_calls_observed", "extensions_policy", "state_checked", "agent_end_observed", "system_message_verified", "assistant_stop_reason"}
    if not isinstance(value, dict) or set(value) != fields:
        return None
    import re
    if (value["runtime"] != "pi" or value["version"] not in (None, "1.4.0")
            or value["protocol"] != "rpc-jsonl-1.4.0"
            or value["tools_policy"] not in ("cli_disabled", "not_launched")
            or value["extensions_policy"] not in ("cli_disabled", "not_launched")
            or value["assistant_stop_reason"] not in (None, "stop", "length", "toolUse", "error", "aborted")
            or any(type(value[key]) is not bool for key in ("settled", "identity_verified", "state_checked", "agent_end_observed", "system_message_verified"))
            or any(type(value[key]) is not int or not 0 <= value[key] <= 2 for key in ("generation", "tool_calls_observed"))
            or (value["session_id"] is not None and (not isinstance(value["session_id"], str)
                or not re.fullmatch(r"[A-Za-z0-9._:-]{1,128}", value["session_id"])))):
        return None
    return dict(value)
