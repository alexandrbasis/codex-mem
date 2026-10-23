# Host and runtime verification

The interactive host runs the user's conversation. The background runtime processes captured memory. These choices are independent.

The required combinations are Codex/Codex, Codex/Pi, Pi/Codex, and Pi/Pi. The first name identifies the host. The second name identifies the background runtime. This guide separates deterministic tests, installed Pi compatibility, and live provider execution. A passing deterministic test does not establish a passing live combination.

## Install and select a runtime

Use the [Codex installation instructions](../README.md#install-the-plugin) or the [Pi project installer](PI_INSTALL.md). Both integrations must select the same memory directory and canonical project. Pi requires Python 3.10 or later and Pi 1.4.0. The installer preserves shared memory and the other host's integration.

An execution profile selects the runtime, provider, exact model, reasoning level, and timeout. Follow [execution configuration](EXECUTION.md#cli-format) to select the default profile or processor overrides. Without a profile, existing configuration selects the legacy Codex runtime. A Pi-only installation must explicitly select Pi. Authenticate through the selected runtime's native tools.

Run `codex-mem doctor` to inspect effective profiles, executable availability, and saved failures. Run `codex-mem doctor --check-execution` for native capability discovery. This command does not send evidence to a model. A successful discovery does not prove that remote authorization works.

## Routine matrix and supporting evidence

The [shared matrix](../tests/test_host_runtime_matrix.py) submits events through Codex hooks or the public Pi local protocol. Each case uses a real temporary SQLite Store and durable queues. Observation cases drain the service queue through a fresh service process. Project-memory cases invoke the public explicit processor in a fresh process. They drain its durable queue without bypassing the service's quiet period. Only the external model executable is replaced. Context returns through the selected host's public interface. Pi/Pi uses an executable-only `PATH` without Codex.

The matrix does not replace the native Pi event extension. Installed Pi tests exercise that extension separately. They use an installed Pi CLI and a deterministic local HTTP provider. These tests establish behavior at the installed protocol boundary. They do not establish live provider behavior or equivalent memory quality.

The table names the owning tests. A test's presence describes coverage, not the result of a particular run. Retain the command, revision, and result when reporting a run.

| Behavior | Owning evidence |
| --- | --- |
| All four pairs, observation queue, duplicate delivery, next-request context, and project scope | [HostRuntimeMatrixTests.test_observation_queue_delivers_next_request_for_every_pair](../tests/test_host_runtime_matrix.py) |
| All four pairs, project memory, source roles and references, actual runtime, shared context, and no replay | [HostRuntimeMatrixTests.test_project_memory_queue_preserves_role_reference_and_selected_runtime](../tests/test_host_runtime_matrix.py) |
| All four pairs, private turns, tools, and injected context excluded before generation | [HostRuntimeMatrixTests.test_privacy_and_injected_context_never_become_processing_sources](../tests/test_host_runtime_matrix.py) |
| All four pairs, protocol failure, atomic project-memory writes, retained source, and retry in a fresh process | [HostRuntimeMatrixTests.test_failed_project_memory_is_atomic_and_retry_survives_restart](../tests/test_host_runtime_matrix.py) |
| All four pairs, source deletion during generation prevents commit | [HostRuntimeMatrixTests.test_source_deletion_during_generation_prevents_every_pair_from_committing](../tests/test_host_runtime_matrix.py) |
| Concurrent Codex/Pi events, two worker slots, immutable active profile, and changed profile after restart | [HostRuntimeMatrixTests.test_mixed_hosts_share_worker_limit_and_active_profile_after_restart](../tests/test_host_runtime_matrix.py) |
| Privacy, tools, branch isolation, copied history, and injected memory | [PiHostTests.test_private_turn_settings_secret_filtering_tool_provenance_and_explicit_writes](../tests/test_pi_host.py), `test_branch_session_host_and_activation_privacy_remain_separate`, `test_injected_memory_cannot_clear_a_private_turn`; [HostContractTests.test_codex_branch_start_excludes_its_own_saved_context](../tests/test_host_contract.py) |
| Native resume, fork, tree navigation, compaction, and final settlement | [InstalledPiHostTests.test_installed_resume_and_tree_navigation_keep_branch_identity_and_context](../tests/test_pi_host.py), `test_installed_fork_replacement_and_compaction_keep_old_sources_historical`, `test_installed_tool_and_continuation_wait_for_final_settlement` |
| Native memory tools, explicit write policy, project scope, and tool failure | [InstalledPiMCPTests.test_enabled_writes_search_full_read_management_privacy_and_project_partition](../tests/test_pi_mcp.py), `test_default_denies_mutation_even_when_model_attempts_it`, `test_unavailable_mcp_is_a_safe_tool_failure_and_pi_continues` |
| Active profile, next attempt, service reload, and deletion before commit | [ProjectMemoryProfileTests.test_active_profile_is_fixed_and_next_attempt_loads_new_configuration](../tests/test_project_memory_profiles.py), `test_retained_service_reloads_profile_after_failure`, `test_source_deletion_during_profile_change_fences_commit_and_keeps_usage`; [ObservationProfileTests.test_claim_persists_profile_before_worker_can_crash](../tests/test_observation_profiles.py) |
| Pi profile retry, lease recovery, and deletion before commit | [PiProjectMemoryTests.test_active_profile_change_and_source_deletion_fence_commit](../tests/test_pi_project_memory.py), `test_service_reloads_pi_profile_on_next_attempt`; [PiObservationTests.test_expired_lease_reclaims_exact_job_with_current_profile](../tests/test_pi_observations.py) |
| Invalid JSON/schema, provider error, cancellation, disconnect, timeout, atomic failure, and retained-source retry | [ProcessorExecutionPathTests.test_failed_execution_cannot_write_partial_memory_in_either_processor](../tests/test_processor_execution_path.py); [PiProjectMemoryTests.test_failed_packets_leave_no_memory_keep_measured_usage_and_can_retry](../tests/test_pi_project_memory.py); [PiObservationTests.test_failures_are_atomic_retain_sources_usage_and_cleanup](../tests/test_pi_observations.py) |
| Worker limits and service recovery | [Service parallel tests](../tests/test_service_parallel.py), including `test_stop_waits_for_all_active_calls_and_preserves_concurrent_enqueue`; [QueueRecoveryTests.test_default_timeout_backoff_stops_after_two_retries_and_survives_restart](../tests/test_queue_recovery.py) |
| Existing configuration, queued work, historical usage, and legacy Codex selection | [ObservationProfileTests.test_existing_queue_and_accounting_migrate_without_new_jobs_or_rewritten_history](../tests/test_observation_profiles.py); [ProjectMemoryProfileTests.test_existing_configuration_queue_and_usage_migrate_without_rewriting_history](../tests/test_project_memory_profiles.py); [ExecutionProfileTests.test_legacy_default_override_and_reset_through_configuration_cli](../tests/test_execution_profiles.py) |
| Unknown usage, failed-attempt usage, historical provenance, and combined reports | [ExecutionUsageReportTests](../tests/test_execution_usage_reports.py); [PiObservationTests.test_optional_usage_missing_after_correction_does_not_erase_measured_totals](../tests/test_pi_observations.py) |

Run the routine matrix from the repository root:

```sh
python3 -m unittest discover -s tests -p test_host_runtime_matrix.py -v
```

Run installed Pi compatibility separately:

```sh
CODEX_MEM_PI=/absolute/path/to/pi python3 -m unittest discover -s tests -p 'test_pi_*.py' -k installed -v
```

An absent `CODEX_MEM_PI` skips installed compatibility tests. Record that outcome as unavailable evidence. Routine executable fixtures cannot replace it.

## Live results

On October 24, 2026, all four public-adapter smoke cases passed with genuine remote background execution. The installed versions were Pi 1.4.0 and Codex CLI 0.163.0. Every case used `gpt-6-luna`, reasoning `low`, and a 60-second timeout. Pi used provider `openai-codex`. Codex used provider `openai`.

| Interactive host | Background runtime | Live status |
| --- | --- | --- |
| Codex | Codex | Passed, one generated observation delivered |
| Codex | Pi | Passed, one generated observation delivered |
| Pi | Codex | Passed, one generated observation delivered |
| Pi | Pi | Passed, one generated observation delivered with Codex unavailable |

The [final live results](evidence/host-runtime-matrix-2026-10-24/live-final.json) record the exact profiles, observed identities, usage, versions, and per-case checks. Each case started with an empty Store and one synthetic user decision. A real service worker processed the durable queue. Public readback checked the committed output, its source and project references, and its ID in the next public context. These checks ran before temporary data was removed. The report retains their results and counts, but does not retain the output/source ID inventory for later inspection.

The result field `repository_revision` identifies baseline HEAD `b3fde3a50ddd694146e2a40e171cbb8dc5ff8ded`. The run also included the uncommitted Pi notification repair. Its exact runtime hash and scope are in [snapshot provenance](evidence/host-runtime-matrix-2026-10-24/snapshot-provenance.json). Null `requested_*` aliases in the helper projection do not mean that the requested profile was lost. Each pair retains its complete `profile`, and the attempt projection retains `runtime`, `provider`, `reasoning_effort`, and `requested_model`.

This live batch exercised observation extraction only. It did not exercise live session summaries, project memory, reconsideration, or episode consolidation. The routine matrix and owning processor suites cover those other paths with deterministic external model executables. The host boundary was the public Codex hook or Pi local adapter. Native interactive event emission remains separate installed-Pi evidence with a local provider.

The Pi/Pi process environment contained Pi, Node, Python, and `ps`, with no Codex executable on `PATH`. Each case used temporary HOME, native configuration directories, project, and Store. Native authorization files were linked through the existing runtime mechanism. No user history was read. A real native call can refresh its own authorization.

Earlier [restricted-environment failures](evidence/host-runtime-matrix-2026-10-24/sandbox-unpassed.json) and the [valid skipped Codex result](evidence/host-runtime-matrix-2026-10-24/network-and-skip-history.json) remain recorded as unpassed attempts. Network-enabled execution succeeded. The skipped task was refined into one explicit decision, then the same decision was used across all four fresh cases. This refinement does not establish memory quality.

The optional [live helper](../scripts/live_host_runtime_smoke.py) requires explicit invocation and makes bounded remote calls:

```sh
python3 scripts/live_host_runtime_smoke.py --live --timeout 60 --output /tmp/host-runtime-live.json
```

Use the recorded provider/model profile only where the native runtime supports it. Missing authorization, model support, or network access produces an unpassed check. The final helper includes additional diagnostic fields added after the recorded live batch. See the evidence notes for the executed helper reconstruction and this audit limit. No extra live success is inferred from those later fields. On failure, the current helper keeps completed progress, the failed stage, and a fixed cause code. It can include numeric exit, timeout, file-error, or JSON-position details. It excludes exception messages, commands, output, and paths.

Live execution establishes compatibility for the recorded environment and profile. The separate [memory quality comparison](MEMORY_PROFILE_COMPARISON.md) records issue #14's exact profiles, failures, semantic judgments, and evidence limits. It does not establish equivalent quality. Schema validity alone does not establish memory quality.

## Limits and recovery

Read [Pi host limits](PI_HOST.md#context-limits-and-failure) before relying on capture. The adapter captures completed current-session activity after settlement. It does not import historical sessions. A crash before settlement or failed batch transfer can lose pending capture. Resume and branch activation require a fresh user message before assistant or tool capture.

Read [Pi runtime restrictions](PI_RUNTIME.md#attempt-boundary) for isolated attempts and disabled resources. The background process has no direct memory-write interface. The core validates results and commits atomically. These restrictions do not provide an operating-system sandbox. Provider extensions remain unsupported.

If processing fails, inspect the saved reason with `codex-mem doctor` and the effective profile in [execution diagnostics](EXECUTION.md#diagnostics-and-usage). Correct the missing executable, native authorization, provider, exact model, or reasoning level before retry. The system does not silently select another runtime. Failed work remains available for a later permitted attempt. An active attempt retains its original profile.

Use the [maintenance workflow](../skills/maintenance/SKILL.md) for inspected queue recovery. Use [project-memory operations](PROJECT_MEMORY.md) for project-memory retry and quarantine rules. Start or inspect the shared service through the commands in [Pi installation](PI_INSTALL.md#select-background-execution). Preserve the memory directory during reinstallation or independent host disablement.

Unknown usage stays unknown. Failed attempts retain measured counters. See [execution reporting](EXECUTION.md#diagnostics-and-usage) for partial totals and separate cost estimates. Changing the model does not replay successful sources.
