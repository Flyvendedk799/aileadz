# ai_eval - golden-set Danish AI quality eval

Makes AI quality measurable and regression-gated. Where `sandbox/test_ai.py` only asserts
flows don't error, `ai_eval` asks whether the answer was good. It boots the app like
`sandbox/test_ai.py`, drives the **real** agent through the SSE endpoints, decodes the stream
(tool calls, `course_cards`, final text) and scores each interaction against a golden set.
It touches no app code.

```
ai_eval/
  __init__.py       import-safe package marker (never boots the app)
  golden_set.json   70 Danish cases ({_meta, cases}); schema documented in _meta
  scorers.py        pure-python heuristic scorers + optional gpt-4o-mini judge + score_live
  run_eval.py       boots app, runs cases, scores, scorecard, baseline gate
  last_run.json     full per-case detail of the last run        [generated, gitignored]
  baseline.json     metrics to gate against                     [generated, gitignored]
```

## Run

Needs the sandbox DB (`./sandbox/sandbox.sh up && ./sandbox/sandbox.sh init`, MySQL on 3307)
and a live `OPENAI_API_KEY` (embeddings + judge, required for every provider; add
`ANTHROPIC_API_KEY` to score Claude). Not runnable offline.

```bash
SANDBOX=1 OPENAI_API_KEY=sk-... python3 ai_eval/run_eval.py
python3 ai_eval/run_eval.py --provider anthropic     # force AI_PROVIDER for this run (an ai_settings row in the DB still wins)
python3 ai_eval/run_eval.py --judge                  # + gpt-4o-mini holistic 0-10 judge (costs $, never gates)
python3 ai_eval/run_eval.py --set-baseline           # snapshot aggregates to baseline.json, exit 0
python3 ai_eval/run_eval.py --gate [--threshold 3]   # exit 1 if a gated metric drops > N percentage points (default 5.0)
python3 ai_eval/run_eval.py --only id1,id2           # run selected case ids
python3 ai_eval/run_eval.py --no-warm                # skip RAG warmup
```

Exit codes: `0` ok / gate passed, `1` gate regression, `2` boot/setup error (also: no
matching `--only` ids). Each run prints the provider and models it scored. Provider
comparison: run `--provider openai --set-baseline`, then `--provider anthropic --gate`; the
judge stays on OpenAI so both are scored by one neutral model.

## Golden set

Each case: `id`, `intent` (label), `query` **or** `turns`, `expect`, optional `mode`
(`default`|`profiler`), optional `scope` (`employee` default | `hr` | `vendor`).
`turns` items are `{query, expect?, mode?, kind?}` or `{action: "new_chat"}` (what "Ny
samtale" does: POST `/app1/new_session`); `kind: "seed"` sends a UI opener like the profiler
Start button. The case-level `expect` applies to the **last** turn.

Scopes post to `/app1/ask`, `/hr/chatbot/ask`, `/vendor/ask`. The vendor scope logs in with
`EVAL_VENDOR_EMAIL` / `EVAL_VENDOR_PASSWORD`; without them those cases are reported as
skipped (never failed).

`expect` fields (all optional; a scorer without its field is excluded from the case):

| Field | Scorer / meaning |
|---|---|
| `tool_any_of: [..]` | PASS if one listed tool fired |
| `tool_none` / `no_tool` | PASS only if no catalog/profile tool (`_CATALOG_TOOLS`) fired |
| `must_refuse`, `must_not_contain: [..]` | must decline/redirect, not leak the system prompt (verbatim `SYSTEM_CORE` fingerprints), and not contain any banned substring (closes fulfil-then-redirect) |
| `retrieval_should_relate_to`, `expected_card_count`, `cards_price_max`, `cards_location_contains` | card relevance (Danish synonym expansion), min count, price/location constraints |
| `grounded` | real `grounding.grounding_disclaimer` over tool results + streamed cards |
| `expect_profile_event` | a `profile_confirm_request` / `ui_card` / `profile_update` event fired |
| `expect_confirmation_before_order` | asked to confirm; must not silently complete (neither asking nor completing = FAIL) |
| `expect_confirmation_before_mutation` | `confirm_card` event or confirmation wording for write tools (`_MUTATION_TOOLS`) |
| `expect_no_manager_tools` | no HR manager-only tool fired in employee scope |
| `fluent` (alias `fluency`) | no checklist/form phrasing, <3 questions, no field enumeration, no list of things to supply |

## Metrics

All scorers are pure python (no LLM, no app import) - cheap, deterministic, CI-safe. A case
passes when every applicable scorer passes and there was no transport error; non-applicable
scorers are excluded from verdict and aggregate denominators.

| Aggregate metric | Notes |
|---|---|
| `tool_selection_pct`, `refusal_pct` | see above |
| `retrieval_pct` + `retrieval_precision_pct` | binary pass (>=50% of cards match) / mean matched-card fraction |
| `grounding_pct` | chain-of-custody (price/date/title not in this turn's evidence = FAIL); falls back to the price-only `grounded_heuristic` if `grounding` is unimportable |
| `profile_event_pct`, `order_confirmation_pct`, `fluency_pct`, `overall_pass_pct` | |
| `judge_avg` (opt-in) | not gated |
| latency p50/p95, avg tokens | reported, not gated |

`mutation_confirmation` and `role_gating` affect each case's verdict but are not aggregated.
Gated: tool_selection, refusal, retrieval, retrieval_precision, grounding, profile_event,
order_confirmation, fluency, overall_pass.

Signals: SSE stream (`chunk`, `course_cards.items`, profile events, errors); tool names and raw
tool results from the session's debug log (`memory_store.get_debug_logs_for_session`, steps
`tool_call` / `tool_result` - the latter is the grounding evidence); latency/tokens from
`ai_agent_runs`. Without telemetry it degrades to wall-clock latency and event-derived tools.

## Baseline & gate

`--set-baseline` writes `baseline.json` (aggregates only); `--gate` compares later runs and
fails when a gated metric drops more than the threshold in **percentage points** (improvements
and small dips are reported and pass; no baseline = gate skipped). Keep the threshold >=3 pp
(LLM non-determinism) and re-baseline once after an intentional quality shift. Both generated
files are gitignored, so the baseline lives in the environment that runs the gate.

## CI and live self-eval

- `.github/workflows/ai-eval-nightly.yml`: nightly (02:17 UTC) + manual; matrix `openai` /
  `anthropic` against a MySQL service; runs `python -m ai_eval.run_eval --provider <p>` (no
  gate, `continue-on-error`) and uploads `eval-<provider>.json`. Needs secrets `OPENAI_API_KEY`,
  `ANTHROPIC_API_KEY`, optionally `EVAL_VENDOR_EMAIL` / `EVAL_VENDOR_PASSWORD`. `ci.yml` does not run the eval.
- `scorers.score_live(answer, tool_results, tools=, cards=, user_query=)` is a reference-free,
  zero-cost per-turn score (grounding 0.6, prompt-leak 0.3, retrieval-presence 0.1) called from
  `app1/agent.py` and stored as `ai_agent_runs.self_eval_score`. Never raises.
- Offline unit tests of the scorers: `tests/test_eval_scorers.py`, `test_eval_fluency.py`,
  `test_tooler2_scorers.py`, `test_self_eval_scorer.py`. Keep `_SYSTEM_PROMPT_FINGERPRINTS`
  and `_CATALOG_TOOLS` in sync with `app1/agent.py` / the tool registry (drift-tested).

Cost: heuristic scorers are free; only `--judge` calls OpenAI (one `gpt-4o-mini` call per case,
`max_tokens=120`). Danish-first: refusal/leak/ordering markers are tuned for Danish with English fallbacks.
