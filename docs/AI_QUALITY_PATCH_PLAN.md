# aileadz app1 — AI Quality Patch (Final Merged Plan)

Repo root: `/Users/tobiasmastek/aileadz/aileadz` (paths below relative to this root).

**Goal:** Maximize *felt* conversation & recommendation quality in one day of parallel agent work, with every behavioral change backed by an offline test or a real eval metric: answers grounded in this turn's tool results, one final completion instead of two, live tool progress instead of dead air, consistent negotiated prices, no expired dates, retrieval that survives Danish paraphrases, memory that survives pruning, GDPR coverage for the AI's dossier, and a frontend whose buttons are real.

**Hard constraints:**
- `pytest tests/ -q` stays green; app boots offline (no OpenAI calls at import/boot; every new LLM touch has an offline fallback). Use the SANDBOX no-MySQL env recipe for verification.
- Every behavioral runtime change is env-gated with current behavior as fallback (`AI_CAPTURE_FINAL`, `AI_LIVE_TOOL_EVENTS`), revertable by config.
- No paid-API tests in `pytest tests/` — all new tests use the `_FakeClient` mock seams (`tests/test_ai_runtime.py:23-69`).
- Re-baseline `ai_eval` once, at the end of the patch, never per-item. Tightened scorers will lower headline numbers by design (today's `grounding_pct` is null/vacuous).

**Verified claims (checked against code 2026-06-10):** orphaned tool messages dropped by `_sanitize_tool_sequence` (ai_runtime.py:1162-1165; tool msgs appended without parent assistant msg at 2141-2152); `defer_final_stream` discards the produced answer (2098-2114) while `agent.py:2174-2181` already has the buffered-stream branch; `_STAGE_HINTS` names legacy tools (agent.py:356-364) and rejection/attribution only match legacy names (483, 2475); `_catalog_compact_fields` skips the discount `_extract_compact_fields` applies; variant dates unfiltered (`[:4]`); prune protected markers exclude `SAMTALEOVERSIGT` (ai_context.py:161-173); GDPR `_EXPORT_QUERIES`/`_DELETE_TABLES` omit `user_memories` + 2026-06 profile tables; rag cache-hit comment promises a shown_handles re-apply the code never does (rag.py:~904-915); absolute `_MIN_COMBINED_SCORE = 0.12` vs RRF-scale scores; chat.js drops `meta`/`thinking` (line 634), `newChat()` skips `/app1/new_session` (769), feedback/copy are CSS theater while `POST /app1/feedback` (app1/__init__.py:1101) exists; `on_tool_event` is unused by `app1/agent.py` (dead callback).

---

## Parallel groups (file-disjoint; items inside a group run sequentially in order)

| Group | Items | Files owned |
|---|---|---|
| runtime-agent | RT-01 → RT-02 → AG-01 → AG-02 → AG-03 | ai_runtime.py, app1/agent.py, hr_agent.py, grounding.py, tests/test_ai_runtime.py, tests/test_prompt_tool_name_drift.py, tests/test_grounding_circuit_breaker.py |
| tools | TL-01 → TL-02 → TL-03 | app1/tools.py, catalog_service.py, tests/test_price_consistency.py, tests/test_upcoming_dates.py, tests/test_catalog_search_filters.py |
| retrieval | RG-01 → RG-02 | app1/rag.py, tests/test_rag_score_floor.py, tests/test_rag_cache.py |
| memory-compliance | MC-01 → MC-02 | ai_context.py, gdpr_service.py, tests/test_prune_summary_carryforward.py, tests/test_gdpr_table_coverage.py |
| frontend | FE-01 → FE-02 → FE-03 | static/futurematch/assets/chat.js |
| eval | EV-01 → EV-02 | tests/test_ask_sse_offline.py, ai_eval/scorers.py, ai_eval/run_eval.py, ai_eval/golden_set.json, tests/test_eval_scorers.py |
| registry | TR-01 | ai_tool_registry.py, tests/test_ai_tool_registry.py |

No file appears in two groups. Items that originally crossed groups were reshaped: AG-01's last-search-query capture moved into agent.py (no tools.py edit); the empty-result backoff is split between TL-03 (tools.py side) and RG-01 (rag.py side); AG-02's frontend half lives in FE-02 (frontend handles `phase:'start'` events whether or not the backend emits them yet); AG-03 keeps grounding.py dependency-free via an injected `known_titles_fn` callable defined in agent.py.

---

## P1 — Highest felt impact + safety net

### RT-01 — Preserve tool evidence in the responses-runtime transcript
In `run_responses_agent`, before appending `role:"tool"` messages (~ai_runtime.py:2141), append a synthetic chat-format assistant message built from the parsed `calls` list (`{'role':'assistant','content':'','tool_calls':[...]}`). `_sanitize_tool_sequence` then retains tool outputs in `stream_messages`, so the streamed final answer (agent.py:2174-2181, hr_agent.py, `_forced_final_completion`) finally sees this turn's tool results. Payloads already truncated by `_strip_heavy_tool_payload`. Single highest-leverage correctness fix — pre-fix, concrete prices/titles in final answers are hallucinations by construction.

### RT-02 — Capture the already-generated final answer instead of discarding + regenerating
On the no-tool-calls iteration, capture the text even when `defer_final_stream=True` (chat: `msg.content`; responses: `_response_text(resp)`); set `needs_final_stream=True` only when text is empty or `finish_reason in ('length','max_output_tokens')`. Once ≥1 tool executed, lift the next iteration's output cap from the 320-token tool-turn cap to `max_output_tokens()`. The buffered branch in agent.py:2179-2181 and hr_agent.py already streams `runtime_result.text`; add ~3-word chunking in `_stream_tokens_to_client` for typewriter feel. Env-gate `AI_CAPTURE_FINAL` (default 1). Saves +1 main-model completion and 1-3s per turn.

### AG-01 — Reconcile prompts, rejection tracking, order attribution with the v2 catalog_* toolset + drift guard
Rewrite the four `_STAGE_HINTS` (agent.py:356-364) to use `catalog_search`/`catalog_compare_products`/`catalog_get_product`+`check_course_readiness`/`request_user_input`+`catalog_search` with schema-accurate examples. Extend `_track_rejection` (agent.py:483-487) and the `_last_recommending_tool` whitelist (agent.py:2475) with `catalog_search`/`catalog_compare_products`. Capture the last `catalog_search` query in agent.py from the executed tool calls (NOT in tools.py — keeps groups disjoint) so rejection context regains its `Søgning: …` line. Drift-guard test: every tool name mentioned in `_STAGE_HINTS`/`_GOLD_STANDARD_EXAMPLES`/`SYSTEM_CORE` must exist in the registry's selectable names.

### TL-01 — One price-resolution helper: search, details, comparison quote the same negotiated price
Extract discount logic from `_extract_compact_fields` (tools.py:166-177) into `price_view(raw_price, vendor)` using `apply_discount` (tools.py:~794) + `_current_supplier_agreements`; call from `_catalog_compact_fields`, `_execute_catalog_compare_products`, `_execute_compare_courses`, `_execute_get_course_details`. Trust-critical for sales-led B2B: today the same course shows two prices depending on tool.

### TL-02 — Stop surfacing expired course dates
`_upcoming_dates(variants)` via `parse_danish_date` + `date.today()`: drop strictly-past parseable dates, keep unparseable strings (unknown ≠ past). Apply in `_extract_compact_fields`, `_catalog_compact_fields`, `_execute_get_course_details`, both compare executors, `add_to_calendar` fallback. All-past → `dates: []` + `no_upcoming_dates: true`. Optionally add `date_data_stale: true` when the product carries an `updated_at`/`published_at` >90 days old. No year-rolling heuristics (catalog re-sync out of scope).

### RG-01 — Relative-to-top retrieval score floor + graceful below-threshold backoff
Factor step 10 into `_apply_score_floor`: `max(0.02, top_score * 0.25)` when fused from both BM25+vector (RRF scale ~0.04 max vs absolute 0.12 today); keep absolute floor only for single-source cosine scale. Step 5: when zero candidates have lexical hits, retain top-3 vector candidates. When the floor filters everything but `pre_filter_count > 0`, return top 2 pre-filter candidates with `confidence='low'` + `below_threshold: true`. Remove/wire the dead `min_score` param. Fixes the systematic Danish-paraphrase recall hole.

### MC-01 — Stop pruning from destroying earlier conversation summaries
In `prune_conversation_memory` (ai_context.py:150-180), extract any pruned system message starting with `SAMTALEOVERSIGT`, feed its content into `summarize_pruned_messages_smart` as a leading pseudo-user line (`"Tidligere opsummering: …"`); new summary keeps the `SAMTALEOVERSIGT` prefix. Today any conversation that prunes twice irrecoverably loses its first third (budget, goals, rejections).

### MC-02 — GDPR export + erasure for user_memories and 2026-06 profile tables, with drift test
Add `user_memories`, `user_certifications`, `user_languages`, `user_portfolio_links` to `_EXPORT_QUERIES` (gdpr_service.py:147-164) and `_DELETE_TABLES` (304-315). Coverage test parses table names from `app1/user_profile_db.py` CREATE TABLE SQL and asserts membership in `_EXPORT_QUERIES ∪ _DELETE_TABLES ∪ _ANONYMISE_TABLES`; fix anything else it flags. Compliance defect — the profiler explicitly solicits personality/life-context.

### FE-01 — Honest "Ny samtale", real feedback loop, real copy button
`newChat()` (chat.js:769) POSTs `/app1/new_session` before painting welcome. Capture the `meta` event's `message_index` (currently dropped at chat.js:634) and return with `fullText`; `addFeedback` POSTs `{rating, message_index, query_text, assistant_response: fullText.slice(0,300), reason, comment}` to the already-built `/app1/feedback`. Copy → `navigator.clipboard.writeText(fullText)` with `execCommand` fallback. This is both felt quality and the measurement loop (thumbs ratio) for everything else.

### EV-01 — Offline mocked-LLM end-to-end test of the /app1/ask SSE pipeline
New `tests/test_ask_sse_offline.py`: boot under SANDBOX env, patch `ai_runtime.OpenAI` with `_FakeClient` and `iter_completion_stream` with scripted chunks ending in `<suggestions>[...]</suggestions>`. Assert the SSE contract: no `<suggestions>` leak + separate suggestions event; scripted `catalog_search` → `course_cards` event; fabricated price → grounding disclaimer; `meta` with `message_index` before `[DONE]`; exactly one final-answer completion (call counting, guards RT-02); `tool_call phase:'start'` before finish (skipif until `AI_LIVE_TOOL_EVENTS` wiring exists). Safety net for the most regression-prone ~500 lines in the repo.

## P2 — High value, after P1s within each group

### AG-02 — Live tool progress: stream tool start/finish events during the loop
Run `run_agent_with_fallback` in a worker thread with `on_tool_event` (currently dead code) feeding a bounded `queue.Queue`; resolve tenant/company scope in the request thread first; generator drains with 0.5s timeout (doubles as heartbeat), hard join ~60s. Track emitted call_ids; skip them in the post-hoc loop (keep it for telemetry + cards). Wrap `_TOOL_CACHE`/`_ROUTER_CACHE` access in a `threading.Lock` (prerequisite — caches are now touched from the worker thread too). Gate `AI_LIVE_TOOL_EVENTS` (default 1). Biggest perceived-latency win; frontend rendering is FE-02.

### AG-03 — Grounding precision: stop false disclaimers, close the argument-echo loophole
`_tool_results_to_text` flattens only `.output`, never `.arguments` (user-echoed prices can no longer "support" claims). TitleCase runs count as title claims only when fuzzy-matching a known course title, injected as optional `known_titles_fn` callable from agent.py (grounding.py stays dependency-free). Disclaimer threshold: ≥1 unsupported price/date OR ≥2 unsupported titles. Do alongside EV-02 so the new metric is trustworthy.

### TL-03 — Carry hard filters into catalog_search's RAG fallback + honest relaxation
Post-filter `rag_products` (tools.py:1121-1166) with `filter_courses`' predicates (variant prices, `_location_matches`, format) before slicing; emptied set → unfiltered results + `filters_relaxed: true`. `_execute_filter_courses`: progressive relaxation (drop date range → widen price 25% → drop location) returning `relaxed_filters: [...]`. Mark `previously_shown` on re-included fallback results. Same key naming as RG-01's flags so the model narrates consistently.

### RG-02 — RAG cache + diagnostics robustness batch
(a) Cache ranked candidates (handles+scores), not final products; on hit re-apply the −0.04 shown_handles penalty (the code comment claims this happens; it doesn't), re-sort/slice, deep-copy. (b) Record `cross_encoder_applied` at the actual rerank call site instead of post-hoc re-derivation. (c) Hoist `_expand_query_tokens` out of the per-product loop in `hybrid_rank_products` (O(products × synonyms × tokens) today); assert byte-identical ordering on a fixture set.

### FE-02 — Streaming polish, live tool chips, robust error paths
(a) Render AG-02's events: stop dropping `thinking` (chat.js:634); `phase:'start'` creates a `.running` chip (CSS exists, dead) upgraded in place by the finish event; remove dots on first event. (b) rAF-throttled re-render with dangling-markdown balancing. (c) Autoscroll only when near-bottom (<120px). (d) AbortController + 25s no-event watchdog (verify backend ping cadence first); on error append an error row instead of `innerHTML` wipe, one silent auto-retry when zero events received; on Stop append "— stoppet —" and still show the feedback row. (e) Clear `attached` refs after each send.

### FE-03 — Course-card CTAs drive the agent instead of dead-ending
Variant "Vælg" stores the selection; "Bestil til team" calls `ask(...)` with a composed order message so the existing confirm-gated `check_course_readiness → prepare_course_order` flow takes over (no new side-effect surface). Anonymous users get a login nudge toast.

### EV-02 — Make grounding measurement real + scorer hardening + drift guards
(a) `score_case` calls `grounding.grounding_disclaimer(answer, tool_results)`; violations → FAIL, no-checkable-claims → applies=True PASS bucket; populate `tool_jsons` in `read_session_telemetry` from `step=='tool_result'` debug logs; add 4-6 price/date-eliciting golden cases. (b) `refusal_correct` gains per-case `must_not_contain`; `confirmation_before_order` fails on "neither asked nor completed"; `retrieval_relevant` → `matched/len(cards)` ≥0.5 with `retrieval_precision_pct` aggregate. (c) Drift guards in `tests/test_eval_scorers.py`: `_SYSTEM_PROMPT_FINGERPRINTS` ⊆ agent prompt constants; `_CATALOG_TOOLS` ⊆ real tool names. Unit-test every scorer on canned interactions. **No re-baseline until the whole patch lands.**

### TR-01 — Tighten over-broad forced_tool gates
"hvem er" forces `catalog_get_vendor` only with a vendor-ish token (`udbyder|leverandør|vendor` or known vendor name), never on "hvem er du/I"; budget forced only when the query starts with a budget question. Keep tools on the menu (`names.add`), demote only `forced_tool` to None when ambiguous; collect candidates and force only when exactly one branch matched (fix the last-match-wins overwrite, incl. the HR selector instance).

---

## P3 — Explicitly OUT OF SCOPE this patch (next batch candidates)
- **Final-answer model split** (`choose_final_model`: strong model writes advisory answers on mini-routed turns) — measure RT-01/RT-02 effect first.
- **Runtime robustness batch**: tool-turn temperature pinning, Danish token-estimate divisor, `_approx_cost_usd` → `ai_cost_model.price_for` unification (prereq for any model-bump A/B; the bump itself needs paid eval).
- **TTFB/housekeeping batch**: post-turn `_extract_user_profile`, fast-model summarizer/extractor, router timeout 4s→1.5s + cache-key normalization, chitchat context slicing, profile TTL cache / injected-profile plumbing.
- **Tool payload trim** (~30-40% fewer tool-result tokens; keep `search_mode` for `_infer_embedding_skipped`); **get_learning_context / check_course_readiness honesty** (deliver promised profile/budget/approval data).
- **Danish decompounding** for unmatched query tokens; few-shot upgrade + stage-based fallback suggestion chips.
- **Memory dedup/supersede** (`add_memory` Jaccard merge + `replaces` param); **rolling per-session summary merge**; anonymous→login memory migration.
- **Conversation resume** (sidebar → rehydrate CHAT_MEMORY); voice input; accessibility pass; `request_user_input` enum extension.
- **Eval depth**: per-turn scoring for multi-turn cases, ~25 new golden cases with structured constraints, 56-tool parametrized matrix, CI eval job (`--repeat`, per-intent gates, TTFT).
- **Hard-deferred**: default model bump, text-embedding-3-large re-embed, live Shopify re-sync, semantic memory retrieval, `mark_course_complete` propose→confirm, HR-agent parity.

## Definition of done
1. `pytest tests/ -q` green offline (no OPENAI_API_KEY, no MySQL) including all new tests, via the SANDBOX env recipe.
2. App boots offline; all behavior changes revertable by env flag (`AI_CAPTURE_FINAL`, `AI_LIVE_TOOL_EVENTS`).
3. A typical tool turn issues exactly one final-answer completion whose prompt contains the turn's tool evidence (EV-01 call counting + RT-01 assertion).
4. Manual chat smoke: new-conversation reset, live tool chips, streamed answer references actual search results, feedback POST visible in network tab, card CTA sends an order message, prices/dates consistent across search vs details.
5. One full `ai_eval/run_eval.py --set-baseline` at the end: numeric `grounding_pct`, new `retrieval_precision_pct`; expect headline numbers to drop vs the vacuous baseline — that is the point.