# TypeSafe memory checks

Codex Mem can use TypeSafe Jev to screen captured events, check generated memory against its sources, and rank search results. Each stage is optional. The model returns typed judgments; it does not generate memory text or establish that a claim is true outside the supplied evidence.

## Enable the checks for a project

You need a TypeSafe API key in a local file. Keep the key out of command arguments and configuration. The configuration stores only its absolute file path; `TYPESAFE_API_KEY` can also supply the key through the environment.

Run this command from the checkout. Replace `/absolute/path/to/key` with the key file and `/absolute/path/to/project` with the project root:

```sh
python3 scripts/codex-mem.py config \
  --jev-filter-key-file /absolute/path/to/key \
  --jev-filter-enabled \
  --jev-filter-project /absolute/path/to/project \
  --jev-quality-enabled \
  --jev-quality-project /absolute/path/to/project \
  --jev-retrieval-enabled \
  --jev-retrieval-project /absolute/path/to/project
```

The filter sends retained source fragments to TypeSafe before the memory generator runs. Quality checking sends generated candidates and their retained evidence. Retrieval sends the query and bounded excerpts of curated memory. Explicit private prompts and excluded projects bypass remote retrieval. The existing secret redactor runs before requests; it is not a general detector of all sensitive information.

All three flags default to `false`. Quality checking follows `jev_filter_projects`; `jev_quality_projects` can narrow that scope further. Retrieval has its own `jev_retrieval_projects` scope. An empty list imposes no additional project restriction; enabling the input filter alone does not enable the other stages.

To disable the new stages while keeping the existing input filter:

```sh
python3 scripts/codex-mem.py config --no-jev-quality-enabled --no-jev-retrieval-enabled
```

## What happens to a generated record

After structural validation, Jev evaluates source support and overclaiming independently. The initial policy requires support of at least `0.8` and overclaiming of at most `0.2` for every note and session summary. A result is accepted only when every item passes.

An uncertain aggregate judgment gets one narrower check of the candidate's factual fields against the same complete evidence. Every field must pass the same thresholds. A clearly rejected candidate is not reconsidered. The audit preserves both the initial judgment and the field judgments; both stages share the 20-second budget.

The check preserves distinctions between a request, an assistant's report, a tool result, and an inherited note. Same-session history can resolve references in a new note; it cannot supply a new completion result absent from current sources. Session summaries can also describe historical evidence within its original scope.

Rejection, uncertainty, an unavailable API, or oversized evidence quarantines the generated batch with an explicit `jev_quality_*` reason. Raw sources remain available. Automatic recovery does not repeatedly regenerate that batch. Rejected, uncertain and oversized batches do not block independent new sources. An unavailable API also stops processing for that project to avoid spending more generator calls during an outage. Inspect the sources and failure reason before an explicit retry.

Complete candidate evidence must fit the quality stage's `96,000`-byte request limit. Input filtering and retrieval retain their separate `24,000`-byte limits. These are local operational bounds, not token counts: the API also enforces the model's [context limits](https://docs.typesafe.ai/models). The implementation does not silently truncate evidence to obtain a passing answer. The whole quality check has a 20-second budget. Oversized or provider-rejected requests remain quarantined.

## What changes in retrieval

Both automatic prompt context and `memory_search` can evaluate up to 12 candidates in one request. A bounded recent-memory pool can supply a relevant record when the query and note use different languages or wording. This cannot recover every older record outside that pool.

Code enforces project, current-session, activity, source and metadata boundaries, and literal identifiers. Jev evaluates relevance. New candidates require a relevance probability of at least `0.8` and a relevance Score of at least `2` on the four-level rubric. A Score's distribution concentration does not prove that the record is true.

The optional retrieval stage has a 1.5-second budget. Automatic hooks skip it when local work leaves insufficient time to return context within the hook deadline. A service error or invalid response restores the original result. Exact and historical queries preserve their ordering constraints. Existing results are not deleted solely because the model rates them poorly.

## Measure quality and cost

Run the frozen synthetic fixture against the configured API:

```sh
python3 scripts/jev_integration_eval.py --live --configured-key --output /tmp/jev-integration.json
```

The report separates completed execution from acceptance. It includes bilingual source-support and retrieval cases, cold and warm requests, reported tokens, latency, and model, policy and fixture hashes. It uses temporary stores and does not send production memory. It does not run the memory generator or establish production accuracy or generator savings.

The [recorded live run](typesafe-integration-acceptance.json) on September 22, 2026 blocked all 15 unsupported candidates and accepted 13 of 15 supported candidates; two supported candidates remained uncertain. Strict fixture acceptance therefore failed. Retrieval improved in four of eight scenarios, with no regressions. These results support a project-scoped trial, not a claim of production-wide accuracy.

The exact-input cache is scoped by project, full redacted input, questions, policy and pinned model. A cache hit has no new API usage. Content-free audit receipts retain typed answers, probabilities, routes, reported token usage and timing. Missing usage remains unknown. Search diagnostics are available in the CLI envelope and MCP retrieval metadata.

The older eligibility telemetry report now names its partial timing `eligibility_and_generator_duration_ms`. Its `full_pipeline_duration_ms` remains unknown because those receipts do not measure every stage or total wall time.

## TypeSafe references

The integration pins `jev-1.13.0`. Jev's Noul is the probability of a yes answer; Choice and Score confidence summarize the returned distribution. Thresholds need evaluation on the intended workload. See the current [API contract](https://docs.typesafe.ai/api), [confidence semantics](https://docs.typesafe.ai/confidence), [model limits](https://docs.typesafe.ai/models), [citation checks](https://docs.typesafe.ai/cookbooks/citation_check), and [reranking example](https://docs.typesafe.ai/cookbooks/rerank_typesafe).
