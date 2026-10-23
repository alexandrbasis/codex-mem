# Memory quality comparison for two execution profiles

This diagnostic comparison does not establish equivalent quality or sufficient quality for general use. It covers two synthetic tasks, three memory types, and two exact profiles. New wording uses previously disclosed regression themes. It is not an independent holdout.

The run used Codex CLI 0.163.0 with provider `openai` and Pi 1.4.0 with provider `openai-codex`. Both requested `gpt-6-luna`, reasoning `low`, and a 60-second timeout. These names do not establish identical backend model weights. Earlier Codex scores do not apply to this run or to Pi.

## Fixed design and execution

The [pre-run manifest](evidence/memory-profile-comparison-2026-10-24/freeze.json) fixed the source texts, rubric, profiles, versions, code hashes, and denominator before generation. Its SHA-256 is `a7db5de8b29fbd994d94799d3f96d4e8db84c145d530afb7eab4ad515a885101`. The `frozen/` directory retains the executed code and inputs. The baseline was `211a341d85ddc71722eb00e40d880dd983b3dc60`.

There were 12 planned public processor calls and 12 completed calls. Each call created one durable attempt and dispatched one native generation. There were no manual retries, corrective generations, excluded failures, or skipped calls. Each profile has six planned cells, including failures and incomplete coverage.

The [helper](../scripts/memory_profile_comparison.py) uses `process_pending` for observations and summaries, and `process_project_memory` for project memory. Capture uses the public Pi adapter for both profiles. Each task uses a temporary real Store. Project memory starts from a separate empty Store. A second synthetic project tests scope separation. The helper does not simulate a native interactive host.

The recorder observes the public execution request and response. It returns the same native adapter result to the processor. It does not replace storage, generation, validation, or commits. Injecting this recorder bypasses the default runner construction and its intermediate usage-checkpoint callback. Final attempts retain measured usage. This run does not establish crash-time usage recovery.

Both profiles receive the same external source history. A summary receives its own profile's preceding generated observations, so its prepared prompt is not byte-identical across profiles. This tests the sequence of processing steps. It does not isolate summary generation from earlier extraction.

Native processes used temporary HOME, Codex and Pi directories, and project folders. Only existing native authorization and Pi model files were linked. The helper did not read credentials, user history, or native session logs. A native runtime can refresh its linked authorization. No global installation, profile, or login changed.

## Runtime results

The [runtime analysis](evidence/memory-profile-comparison-2026-10-24/runtime-analysis.json) includes every cell. Totals below group attempts by requested profile. They do not assign a verified execution identity where the adapter did not retain one.

| Requested profile | Planned cells | Accepted by core | Rejected | Elapsed seconds | Reported tokens |
| --- | ---: | ---: | ---: | ---: | ---: |
| Codex / openai / gpt-6-luna / low | 6 | 6 | 0 | 34.95 | 74,265 |
| Pi / openai-codex / gpt-6-luna / low | 6 | 5 | 1 | 28.64 | 26,412 |

Elapsed time includes processor work and public readback for each invocation. All 12 attempts reported token totals. Costs are unknown because no billing data was collected. Token accounting comes from each runtime's own counters. Differences include prompts, native processing, and accounting conventions. These totals are not an efficiency benchmark.

The Harbor Pi project-memory result failed with `invalid_json`. Its 5,255 tokens and 8.08 seconds remain in the totals. Its five sources remain pending and quarantined, with no committed episodes or claims. The saved execution identity is null. Its receipt records verified identity during native execution, and the failure retains an execution ID. Neither field recovers the missing actual model/provider tuple. See [the original failure](evidence/memory-profile-comparison-2026-10-24/runs/harbor-pi-project_memory/result.json).

All four summary calls stopped at the first assistant completion event. The later tool result and user correction in the submitted batch remained outside the claimed input. Each summary received one new assistant source and earlier generated context. The complete submitted history contains five sources. A summary that omits the later cancellation therefore fails planned full-history coverage, but this does not establish a model omission of supplied evidence. The original inputs and rubric remain unchanged.

## Semantic results

An independent runtime-blinded reasoning agent applied the frozen rubric. The [original grades](evidence/memory-profile-comparison-2026-10-24/semantic-grades.json) retain dimension ratings, exact quotations, and source/output IDs for every cell. The [joined results](evidence/memory-profile-comparison-2026-10-24/semantic-joined.json) associate those judgments with the requested profiles.

| Requested profile | Planned cells | All dimensions pass | Available but unpassed | Output unavailable |
| --- | ---: | ---: | ---: | ---: |
| Codex / openai / gpt-6-luna / low | 6 | 1 | 5 | 0 |
| Pi / openai-codex / gpt-6-luna / low | 6 | 1 | 4 | 1 |

These are descriptive counts under a strict all-dimensions rule. They are not accuracy estimates or evidence of a tie. There are only two tasks. The single grader is fallible, and its judgments remain reviewable.

Orchid Codex observations and Harbor Pi observations retained all five dimensions. Harbor Codex observations weakened the original refresh-button obligation into possible removal. Orchid Pi observations used a change classification despite having only user requirements. This classification differs from the otherwise accurate requirement prose.

The Orchid Codex project-memory output retained a proposed preview requirement after the user's cancellation. The other available project-memory outputs retained current obligations and blockers, but the grader found incomplete accounts of conflicting assistant readiness reports. These judgments concern retained meaning and attribution. They do not follow from schema validation.

All four summaries omitted environment/version scope. Harbor Pi also omitted the cache rationale, and Harbor Codex did not affirm the original button requirement. These omissions concern input that the summary actually received. Later tool results and cancellations were absent from the prepared summary prompts, so their omission remains a separate processing-coverage failure. Full-history and input-conditioned dimension ratings remain separate in the original grades. The strict cell verdicts happen to remain unpassed under both views.

The other-project distractors were captured in a separate project and excluded from the prepared inputs. This tests project filtering. It does not establish resistance to misleading text inside the same prompt. Source custody alone did not count as a represented requirement, and output availability did not count as semantic success.

## Evidence and limits

The [canonical index](evidence/memory-profile-comparison-2026-10-24/index.json) names all 12 result files and their hashes. Each run retains the exact prepared request, parsed native execution result, timing, capture receipts, durable attempts, source IDs, generated record IDs, and public context. Source references and role labels remain available for inspection.

`execution.json` preserves the original parsed model packet before processor normalization. It does not preserve original JSON whitespace. Native adapters do not expose rejected raw text at this public boundary. The Harbor Pi invalid JSON text is therefore unavailable. The failure reason, usage, execution ID, and receipt are retained. The helper did not bypass the decoder or read native logs to recover that text.

Timeline readback contains source and output previews, plus complete structured observation and summary metadata. It does not contain complete stored body text. The full synthetic source text is recoverable through frozen fixtures and capture IDs. Full parsed model bodies remain in execution results. Project-memory snapshots retain complete typed episodes and claims. Semantic judgments must preserve these different evidence boundaries.

The [adjudication manifest](evidence/memory-profile-comparison-2026-10-24/adjudication-input-manifest.json) records the blinded grading input hash and method. The grader received source roles, actual IDs, exact prepared prompts, expected meaning, and output records. It did not receive runtime/model labels, runtime verdicts, token counts, or implementer scores. Runtime results are joined only after semantic grading.

The TypeSafe citation cookbook states that an exact quote can still support the wrong claim. This informed separate requirement, reason, status, evidence, and scope judgments. The rubric follows that guidance but this comparison made no TypeSafe API calls. No semantic routing or new production dependency was added. See [TypeSafe citation guidance](https://docs.typesafe.ai/cookbooks/citation_check).

This sample does not cover reconsideration, episode consolidation, long histories, native interactive quality, or production behavior. It cannot establish a general pass rate. The source-cutoff limit prevents a complete summary comparison for the planned later corrections. Further testing needs a new frozen design and must retain this run as earlier evidence.

## Reproduce a new measurement

Use a fresh output directory for each new measurement. The first command records versions and freezes inputs without generation. The second command makes up to 12 remote calls. Native capability validation runs inside each real attempt. An unavailable profile produces failed evidence rather than a substitute profile.

```sh
python3 scripts/memory_profile_comparison.py --prepare --output /tmp/new-memory-comparison
python3 scripts/memory_profile_comparison.py --live --output /tmp/new-memory-comparison
```

The exclusive start marker prevents silent repeats or resumes. Source hash changes stop execution before a new batch. A process or setup failure can leave a partial batch. Its missing planned cells remain unpassed and must not disappear from the denominator. No result from a later helper version changes the recorded historical run.
