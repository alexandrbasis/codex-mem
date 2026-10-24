"""Pinned Pi RPC execution from prepared input, with no memory-write interface."""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import selectors
import shutil
import signal
import subprocess
import tempfile
import time
from collections.abc import Mapping
from contextlib import contextmanager

from .execution import ExecutionFailure, ExecutionIdentity, ExecutionResult
from .store import ObservationLeaseExpired
from .processor import (MAX_SERVER_LINE_BYTES, MAX_SERVER_OUTPUT_BYTES, MAX_MODEL_OUTPUT_CHARS,
                        MAX_PROMPT_CHARS, ProcessorFailure, _UsageCheckpointer)

PI_VERSION = "1.4.0"
SYSTEM_PROMPT = "Process only the supplied evidence. Return only the requested JSON."
RESTRICTIONS = ("--no-session", "--no-tools", "--no-mcp", "--no-extensions",
                "--no-skills", "--no-prompt-templates", "--no-context-files", "--no-themes")


def _json(text):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("duplicate JSON key")
            result[key] = value
        return result
    def constant(_):
        raise ValueError("nonfinite JSON")
    try:
        value = json.loads(text, object_pairs_hook=pairs, parse_constant=constant)
    except (ValueError, UnicodeError, RecursionError):
        raise ProcessorFailure("invalid_response", reason_code="invalid_json") from None
    if not isinstance(value, dict):
        raise ProcessorFailure("invalid_response", reason_code="invalid_output_shape")
    return value


class _Process:
    """Bound both pipes, nonblocking input, deadline, and owned process group."""
    def __init__(self, args, cwd, env, deadline):
        self.deadline = deadline
        self.pending = bytearray()
        self.bytes = 0
        self.sequence = 0
        self.child = subprocess.Popen(args, cwd=cwd, env=env, stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
        self.selector = selectors.DefaultSelector()
        for name in ("stdin", "stdout", "stderr"):
            stream = getattr(self.child, name)
            os.set_blocking(stream.fileno(), False)
            if name != "stdin":
                self.selector.register(stream, selectors.EVENT_READ, name)

    def remaining(self):
        value = self.deadline - time.monotonic()
        if value <= 0:
            raise ProcessorFailure("timeout")
        return value

    def send(self, value):
        import select
        encoded = json.dumps(value, ensure_ascii=False).encode() + b"\n"
        if len(encoded) > MAX_SERVER_LINE_BYTES:
            raise ProcessorFailure("invalid_request")
        pending = memoryview(encoded)
        while pending:
            remaining = self.remaining()
            try:
                written = os.write(self.child.stdin.fileno(), pending)
                if written <= 0:
                    raise ProcessorFailure("protocol_error")
                pending = pending[written:]
            except BlockingIOError:
                select.select([], [self.child.stdin], [], min(remaining, .1))
            except OSError:
                raise ProcessorFailure("protocol_error") from None

    def line(self):
        while True:
            self.remaining()
            newline = self.pending.find(b"\n")
            if newline >= 0:
                if newline > MAX_SERVER_LINE_BYTES:
                    raise ProcessorFailure("protocol_error", reason_code="pi_output_limit")
                raw = bytes(self.pending[:newline])
                del self.pending[:newline + 1]
                return raw
            if len(self.pending) > MAX_SERVER_LINE_BYTES:
                raise ProcessorFailure("protocol_error", reason_code="pi_output_limit")
            if not self.selector.get_map():
                raise ProcessorFailure("protocol_error", reason_code="pi_disconnected")
            for key, _ in self.selector.select(min(self.remaining(), .1)):
                chunk = os.read(key.fd, 65536)
                if not chunk:
                    self.selector.unregister(key.fileobj)
                    continue
                self.bytes += len(chunk)
                if self.bytes > MAX_SERVER_OUTPUT_BYTES:
                    raise ProcessorFailure("protocol_error", reason_code="pi_output_limit")
                if key.data == "stdout":
                    self.pending.extend(chunk)
                # Drain and discard stderr: it can contain credentials or prompts.

    def message(self):
        raw = self.line()
        try:
            return _json(raw)
        except ProcessorFailure:
            raise ProcessorFailure("protocol_error", reason_code="pi_malformed_record") from None

    def request(self, kind, observe=None, **values):
        self.sequence += 1
        identifier = str(self.sequence)
        self.send(dict(id=identifier, type=kind, **values))
        while True:
            event = self.message()
            if event.get("type") != "response":
                if observe is None:
                    raise ProcessorFailure("protocol_error", reason_code="pi_unexpected_event")
                observe(event)
                continue
            if event.get("id") != identifier or event.get("command") != kind:
                raise ProcessorFailure("protocol_error")
            if event.get("success") is not True:
                raise ProcessorFailure("protocol_error", reason_code="pi_command_failed")
            data = event.get("data", {})
            if not isinstance(data, dict):
                raise ProcessorFailure("protocol_error")
            return data

    def close(self):
        # Kill the owned group even if its leader exited, to reap descendants.
        for sig in (signal.SIGTERM, signal.SIGKILL):
            try:
                if os.name == "posix":
                    os.killpg(self.child.pid, sig)
                elif self.child.poll() is None:
                    self.child.kill()
            except OSError:
                if self.child.poll() is None:
                    try:
                        self.child.kill()
                    except OSError:
                        pass
            try:
                self.child.wait(timeout=.2)
            except subprocess.TimeoutExpired:
                pass
        self.selector.close()
        for stream in (self.child.stdin, self.child.stdout, self.child.stderr):
            try:
                stream.close()
            except OSError:
                pass


def _environment(directory):
    """Expose native auth/models only, without loading the native user profile."""
    native = Path(os.environ.get("PI_CODING_AGENT_DIR", Path.home() / ".pi/agent")).expanduser().resolve()
    agent = directory / "agent"
    agent.mkdir(mode=0o700)
    for filename in ("auth.json", "models.json"):
        source = native / filename
        if source.is_file():
            (agent / filename).symlink_to(source)
    (agent / "settings.json").write_text(json.dumps({"compaction": {"enabled": False},
        "retry": {"enabled": False, "provider": {"maxRetries": 0}}, "enableAnalytics": False, "enableInstallTelemetry": False}))
    environment = dict(os.environ)
    environment.update(PI_CODING_AGENT_DIR=str(agent), CODEX_MEM_DISABLED="1", NO_COLOR="1")
    environment.pop("PI_CODING_AGENT_SESSION_DIR", None)
    return environment


def _check_auth(executable, directory, environment, deadline, provider):
    """Read native credential readiness without refreshing or returning secrets."""
    client = _Process([executable, "auth", "check", "--provider", provider,
                       "--json", "--no-refresh"], directory, environment, deadline)
    try:
        result = client.message()
        if result.get("provider") != provider or result.get("status") not in ("ready", "not_ready", "invalid"):
            raise ProcessorFailure("protocol_error", reason_code="pi_auth_check_unavailable")
        if result.get("reason") == "provider_not_found":
            raise ProcessorFailure("model_unavailable", reason_code="unsupported_provider")
        if result["status"] != "ready":
            raise ProcessorFailure("runner_failure", reason_code="authentication_unavailable")
    finally:
        client.close()


class _Run:
    def __init__(self, profile, checkpoint):
        self.profile = profile
        self.checkpoint = checkpoint
        self.session = None
        self.number = 0
        self.completed_usage = []
        self.current_usage = None
        self.updates = 0
        self.settled = False
        self.message = None
        self.started = False
        self.ended = False
        self.identity_verified = False
        self.post_state_checked = False
        self.tool_calls = 0
        self.version_verified = False
        self.launched = False
        self.finished = False
        self.system_count = 0
        self.cwd = None

    def usage(self):
        samples = self.completed_usage + ([self.current_usage] if self.number else [])
        known = [sample for sample in samples if sample]
        keys = set.intersection(*(set(sample) for sample in known)) if known else set()
        tokens = {key: sum(sample[key] for sample in known) for key in keys} or None
        required = {"input_tokens", "output_tokens", "total_tokens"}
        complete = bool(samples) and all(sample and required <= set(sample) for sample in samples)
        return dict(status="reported" if complete and self.finished else "partial" if tokens else "unavailable",
                    source="pi_assistant_usage", updates=self.updates, tokens=tokens)

    def record_usage(self, usage):
        if not isinstance(usage, dict):
            return
        fields = {"input": "input_tokens", "output": "output_tokens", "cacheRead": "cached_input_tokens",
                  "cacheWrite": "cache_write_input_tokens", "reasoning": "reasoning_output_tokens",
                  "totalTokens": "total_tokens"}
        values = {target: usage[key] for key, target in fields.items()
                  if type(usage.get(key)) is int and 0 <= usage[key] <= 2**53 - 1}
        if all(key in values for key in ("input_tokens", "cached_input_tokens", "cache_write_input_tokens")):
            values["input_tokens"] += values["cached_input_tokens"] + values["cache_write_input_tokens"]
        else:
            values.pop("input_tokens", None)
        # Pi seeds unreported provider usage with zero. It is not measured zero.
        if not any(values.values()):
            return
        self.current_usage = {key: max(value, (self.current_usage or {}).get(key, 0))
                              for key, value in {**(self.current_usage or {}), **values}.items()}
        self.updates += 1
        self.checkpoint()

    def state(self, state, *, initial=False):
        self.post_state_checked = False
        model = state.get("model", {})
        if (not isinstance(model, dict) or model.get("id") != self.profile.model
                or model.get("provider") != self.profile.provider
                or state.get("thinkingLevel") != ("off" if self.profile.reasoning_effort == "none" else self.profile.reasoning_effort)):
            raise ProcessorFailure("model_mismatch", reason_code="pi_identity_mismatch")
        session = state.get("sessionId")
        if not isinstance(session, str) or not re.fullmatch(r"[A-Za-z0-9._:-]{1,128}", session):
            raise ProcessorFailure("protocol_error")
        if self.session and self.session != session:
            raise ProcessorFailure("protocol_error", reason_code="pi_session_changed")
        if (state.get("isStreaming") is not False or state.get("isCompacting") is not False
                or state.get("pendingMessageCount") != 0 or state.get("autoCompactionEnabled") is not False
                or state.get("messageCount") != (0 if initial else self.number * 2 + self.system_count) or state.get("sessionFile")):
            raise ProcessorFailure("protocol_error", reason_code="pi_not_ephemeral_idle")
        self.session = session
        self.identity_verified = True
        self.post_state_checked = not initial

    def begin(self):
        if self.number:
            self.completed_usage.append(self.current_usage)
        self.number += 1
        self.current_usage = None
        self.settled = self.started = self.ended = False
        self.message = None
        self.post_state_checked = False

    def reject_tool(self):
        self.tool_calls += 1
        raise ProcessorFailure("tool_called")

    def observe(self, event):
        kind = event.get("type")
        message = event.get("message")
        if isinstance(message, dict) and message.get("role") == "system":
            if message.get("toolsAdded") or message.get("toolsRemoved"):
                self.reject_tool()
            expected = {"preamble": SYSTEM_PROMPT, "cwd": f"<cwd>\n{self.cwd}\n</cwd>"}
            if (self.number != 1 or self.system_count or message.get("content") != ""
                    or message.get("sections") != expected or kind not in ("message_start", "message_end")):
                raise ProcessorFailure("protocol_error", reason_code="pi_forbidden_event")
            if kind == "message_end":
                self.system_count += 1
            return
        if isinstance(message, dict) and message.get("role") not in ("user", "assistant"):
            if message.get("role") == "toolResult" or message.get("toolsAdded"):
                self.reject_tool()
            raise ProcessorFailure("protocol_error", reason_code="pi_forbidden_event")
        if isinstance(message, dict) and message.get("role") == "assistant":
            self.record_usage(message.get("usage"))
            if message.get("provider") != self.profile.provider or message.get("model") != self.profile.model:
                raise ProcessorFailure("model_mismatch", reason_code="pi_identity_mismatch")
            if any(block.get("type") == "toolCall" for block in message.get("content", []) if isinstance(block, dict)):
                self.reject_tool()
        if kind == "message_update":
            self.record_usage(event.get("usage"))
            update = event.get("assistantMessageEvent", {})
            if not isinstance(update, dict):
                raise ProcessorFailure("protocol_error")
            if str(update.get("type", "")).startswith("toolcall"):
                self.reject_tool()
        elif kind == "agent_start":
            if self.started:
                raise ProcessorFailure("protocol_error", reason_code="pi_automatic_restart")
            self.started = True
        elif kind == "message_end" and isinstance(message, dict) and message.get("role") == "assistant":
            if self.message is not None:
                raise ProcessorFailure("protocol_error", reason_code="pi_multiple_answers")
            self.message = message
        elif kind == "turn_end":
            if event.get("toolResults") != []:
                self.reject_tool()
        elif kind == "agent_end":
            self.ended = True
        elif kind == "agent_settled":
            if event.get("aborted") is True:
                raise ProcessorFailure("runner_failure", reason_code="pi_cancelled")
            if event.get("aborted") is not False or not self.started or not self.ended or self.message is None or self.system_count != 1:
                raise ProcessorFailure("protocol_error", reason_code="pi_incomplete_run")
            if self.message.get("stopReason") not in ("stop",):
                raise ProcessorFailure("runner_failure", reason_code="pi_cancelled" if self.message.get("stopReason") == "aborted" else "pi_provider_failure")
            self.settled = True
        elif kind in ("message_start", "turn_start"):
            pass
        elif kind == "response" or (kind not in ("message_update", "message_end") and kind not in ("agent_start", "agent_end")):
            if str(kind).startswith("tool_"):
                self.reject_tool()
            raise ProcessorFailure("protocol_error", reason_code="pi_forbidden_event")

    def output(self):
        content = self.message.get("content")
        if not isinstance(content, list) or any(not isinstance(b, dict) or b.get("type") not in ("text", "thinking") for b in content):
            raise ProcessorFailure("invalid_response")
        texts = [b.get("text") for b in content if b.get("type") == "text"]
        if not texts or any(not isinstance(text, str) for text in texts):
            raise ProcessorFailure("invalid_response")
        text = "".join(texts)
        if len(text) > MAX_MODEL_OUTPUT_CHARS:
            raise ProcessorFailure("invalid_response", reason_code="pi_output_limit")
        return _json(text)

    def receipt(self):
        return dict(runtime="pi", version=PI_VERSION if self.version_verified else None, protocol="rpc-jsonl-1.4.0", session_id=self.session,
            generation=self.number, settled=self.settled, identity_verified=self.identity_verified,
            tools_policy="cli_disabled" if self.launched else "not_launched", tool_calls_observed=self.tool_calls,
            extensions_policy="cli_disabled" if self.launched else "not_launched", state_checked=self.post_state_checked,
            agent_end_observed=self.ended, system_message_verified=self.system_count == 1, assistant_stop_reason=self.message.get("stopReason") if self.message else None)


@contextmanager
def _prepared(pi, profile, deadline, run):
    if profile.runtime != "pi":
        raise ProcessorFailure("runner_unavailable", reason_code="unsupported_runtime")
    if profile.reasoning_effort is None:
        raise ProcessorFailure("model_unavailable", reason_code="unsupported_reasoning")
    executable = shutil.which(pi)
    if not executable:
        raise ProcessorFailure("runner_unavailable", reason_code="pi_not_found")
    client = None
    with tempfile.TemporaryDirectory(prefix="codex-mem-pi-") as temporary:
        directory = Path(temporary).resolve()
        run.cwd = directory
        environment = _environment(directory)
        try:
            client = _Process([executable, "--version"], directory, environment, deadline)
            if client.line().decode().strip() != PI_VERSION:
                raise ProcessorFailure("runner_unavailable", reason_code="unsupported_pi_version")
            run.version_verified = True
        finally:
            if client:
                client.close()
                client = None
        _check_auth(executable, directory, environment, deadline, profile.provider)
        try:
            client = _Process([executable, "--mode", "rpc", *RESTRICTIONS,
                "--system-prompt", SYSTEM_PROMPT],
                directory, environment, deadline)
            run.launched = True
            available = client.request("get_available_models").get("models")
            if not isinstance(available, list) or any(not isinstance(m, dict) for m in available):
                raise ProcessorFailure("protocol_error")
            if sum(m.get("id") == profile.model and m.get("provider") == profile.provider for m in available) != 1:
                raise ProcessorFailure("model_unavailable", reason_code="exact_model_unavailable")
            selected = client.request("set_model", provider=profile.provider, modelId=profile.model)
            if selected.get("id") != profile.model or selected.get("provider") != profile.provider:
                raise ProcessorFailure("model_mismatch", reason_code="pi_identity_mismatch")
            level = "off" if profile.reasoning_effort == "none" else profile.reasoning_effort
            levels = client.request("get_available_thinking_levels").get("levels")
            if not isinstance(levels, list) or any(not isinstance(item, str) for item in levels):
                raise ProcessorFailure("protocol_error")
            if level not in levels:
                raise ProcessorFailure("model_unavailable", reason_code="unsupported_reasoning")
            def thinking_changed(event):
                # Pi 1.4.0 emits this before acknowledging a changed level.
                # Only our exact requested change is valid at this boundary.
                if event != {"type": "thinking_level_changed", "level": level}:
                    raise ProcessorFailure("protocol_error", reason_code="pi_unexpected_event")
            client.request("set_thinking_level", thinking_changed, level=level)
            run.state(client.request("get_state"), initial=True)
            yield client
        finally:
            if client:
                client.close()


class PiProcessorRunner:
    """One ephemeral Pi subprocess per attempt, at most one correction."""
    def __init__(self, *, pi="pi", usage_checkpoint=None, candidate_review=None, before_dispatch=None):
        self.pi = pi
        self.usage_checkpoint = usage_checkpoint
        self.candidate_review = candidate_review
        self.before_dispatch = before_dispatch

    def check_profile(self, profile, *, timeout=10):
        run = _Run(profile, lambda: None)
        with _prepared(self.pi, profile, time.monotonic() + timeout, run):
            pass

    def execute(self, request):
        started = time.monotonic()
        deadline = min(request.deadline, started + request.profile.timeout)
        checkpointer = _UsageCheckpointer(self.usage_checkpoint)
        run = _Run(request.profile, lambda: checkpoint())
        def metrics():
            return dict(duration_ms=round((time.monotonic() - started) * 1000), usage=run.usage(),
                        execution_receipt=run.receipt())
        def checkpoint():
            if run.session:
                checkpointer.save(dict(worker_thread_id=run.session, worker_turn_id=f"generation-{run.number}",
                    identity=dict(runtime="pi", provider=request.profile.provider, model=request.profile.model,
                                  reasoning_effort=request.profile.reasoning_effort), metrics=metrics()), force=True)
        try:
            if started >= deadline:
                raise ProcessorFailure("timeout")
            with _prepared(self.pi, request.profile, deadline, run) as client:
                checkpoint()
                prompt = "Output schema: " + json.dumps(dict(request.output_schema)) + "\n\n" + request.prompt
                for generation in range(2):
                    if self.before_dispatch is not None:
                        self.before_dispatch()
                    run.begin()
                    response = client.request("prompt", run.observe, message=prompt)
                    if response.get("disposition") != "started":
                        raise ProcessorFailure("protocol_error", reason_code="pi_prompt_not_started")
                    while not run.settled:
                        run.observe(client.message())
                    run.state(client.request("get_state"))
                    checkpoint()
                    output = run.output()
                    remaining = client.remaining()
                    correction = self.candidate_review(output, remaining) if self.candidate_review else None
                    client.remaining()
                    if correction is None:
                        run.finished = True
                        return ExecutionResult(output=output, identity=ExecutionIdentity("pi", request.profile.provider,
                            request.profile.model, request.profile.reasoning_effort, run.session, f"generation-{run.number}"), metrics=metrics())
                    if generation or not isinstance(correction, str) or not correction or len(correction) > MAX_PROMPT_CHARS:
                        raise ProcessorFailure("invalid_response", reason_code="jev_quality_uncertain")
                    prompt = correction
        except Exception as error:
            run.finished = True
            if isinstance(error, ObservationLeaseExpired):
                # Lease ownership is a caller policy failure, not malformed RPC.
                error = ProcessorFailure("lease_expired")
            if not isinstance(error, ProcessorFailure):
                error = (ProcessorFailure("runner_unavailable", reason_code="pi_launch_failed")
                         if isinstance(error, OSError) else ProcessorFailure("protocol_error", reason_code="pi_malformed_record"))
            reason = {"timeout": "timeout", "runner_unavailable": "runtime_unavailable", "model_unavailable": "configuration",
                "invalid_response": "invalid_output", "model_mismatch": "identity_mismatch", "protocol_error": "protocol",
                "tool_called": "forbidden_tools", "lease_expired": "cancelled"}.get(error.code, "runtime_failure")
            if error.reason_code == "pi_cancelled":
                reason = "cancelled"
            return ExecutionResult(metrics=metrics(), failure=ExecutionFailure(reason, error.code, error.reason_code,
                run.session, f"generation-{run.number}" if run.number else None))
        finally:
            checkpoint()
