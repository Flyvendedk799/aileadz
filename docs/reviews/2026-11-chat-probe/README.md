# Danish chat probe, 1 November 2026

Evidence for the chat fixes on this branch. This is a captured live run, not a unit-test fixture. Read this file first, then the qualitative transcript, then the JSONL when a claim needs the raw turn.

## Corpus

| File | What it is |
|---|---|
| [qual.txt](qual.txt) | The 27 hand-written cases, one block per case: user text, HTTP status, seconds, tool events, assistant text |
| [results.jsonl](results.jsonl) | All 593 cases. One JSON object per line |

Each JSONL row is `{id, user, turns[]}`. Each turn is `{user, user_len, status, chat_id, seconds, text, events}`. `events` is the SSE type list (`tool_call:<name>`, `chunk`, …). There is no tool-result payload and no system prompt in the capture.

- 593 cases, 2,341 turns, all against `https://futurematch.dk` `POST /app1/ask`.
- 27 qualitative cases (ids that do not start with `vol-`), then volume cases `vol-*`: the same four Danish turns across topics, personas and places.
- Six logins, `fmprobe20261101a`–`f`, round-robin. A `new_session` on one login still shares that login's profile, so a fact saved for one case can appear in a later case on the same user. That is not a cross-user leak.
- Models at the time of the run: main `gpt-6.1-terra`, fast `gpt-6-luna`, Responses API. Routing was `quality` for the spend, then put back to `balanced`.
- The OpenAI key ran out during the volume half. `provider_down` (1,197 turns) is the catalog fallback after `credit_balance_exhausted`, not a model failure. Those turns are not evidence for or against the fixes.
- The owner reported the OpenAI bill as $30. A chat-token ledger for these usernames priced only the tokens it could see and came out lower. Do not treat either number as a line-by-line reconciliation.
- No course order was created. The test logins and their fake profile rows were left in production.

## Grep flags

These counts are string matches on the assistant text. They are a map into the corpus, not a labeled bug set. A "saved" match includes both a real autosave and a confirm-card the model described as already stored.

| Flag | Turns | Match | Qualitative ids |
|---|---|---|---|
| `provider_down` | 1197 | fallback text that the advisor is unavailable | volume only |
| `price_disclaimer` | 175 | `Bekræft venligst priser` | `price-pressure`, `contradiction`, `prereq`, `team` |
| `claims_saved` | 57 | `Jeg har gemt`, `har noteret`, `Jeg har oprettet` | `change-mind`, `vague-start`, `profile-build`, `off-topic`, `typos`, `skill-gap`, `long`, `answer-first`, `prereq` |
| `cant_see_results` | 51 | the model says the catalog result did not come through | `language-mix`, `change-mind`, `dialect`, `injection` |
| `no_results_banner` | 37 | `ingen kurser der matchede` | `missing-course`, `answer-first` |
| `spaced_product_link` | 3 | `]( /` | `vol-592`, `vol-037`, `vol-066` |
| `english` | 2 | an English reply | `language-mix` (both the switch request and the following shape; one case) |
| HTTP 400 | 1 | empty query, expected | |
| order created | 0 | `ordre er oprettet` / `du er tilmeldt` | `no-order` stays clean |

`qual.txt` is the place to read the wording. Case ids there: `vague-start`, `answer-first`, `compare`, `typos`, `dialect`, `profile-build`, `contradiction`, `memory`, `help`, `injection`, `missing-course`, `price-pressure`, `follow-up`, `short`, `language-mix`, `no-order`, `prereq`, `off-topic`, `checklist`, `html`, `other-people`, `change-mind`, `team`, `skill-gap`, `long`, `caps`.

Behaviors in that transcript that the code on this branch is aimed at:

- `change-mind`: the model says the target role was saved, then the profile still shows the previous role because the replacement is a confirm card.
- `language-mix` and `change-mind`: "katalogopslaget er ikke kommet ordentligt igennem" while a search tool ran.
- `answer-first`, `missing-course`: the empty-search line under prose that is discussing courses.
- `price-pressure`: a stated price, then "pris på forespørgsel", then the course-page disclaimer. `contradiction` trips the same disclaimer on a profile turn.
- `language-mix`: "Switch to English" is obeyed ("I'll stick to English").
- `no-order`: "Ligger der en ordre på mig nu?" gets no `get_my_course_status` and tells the user to open the learning overview.
- `help`: ordering and CV upload search the help articles; "Hvad sker der med mine data hvis jeg sletter min konto?" does not.
- `vol-592`: `[Vild med dansk 2]( /products/…)`.

Behaviors in the same transcript that should stay as they are: typos and dialect still get a Danish course answer, prompt injection and other-user lookups are refused, a made-up Ribe course is not invented, an off-topic question is refused, and no order is created when the user says not to.

## Where the branch responds

| Evidence | Code |
|---|---|
| Confirm card described as saved | `app1/tools.py` `_with_proposal_note`; playbooks in `app1/agent.py` |
| Catalog result the model cannot see | `ai_runtime.py` `_strip_heavy_tool_payload` / `_fit_tool_payload` |
| Empty-search line on a non-empty search | `app1/agent.py` `search_miss_note` |
| Disclaimer on a withdrawn price or a profile-only turn | `grounding.py` `_claim_withdrawn`; `app1/agent.py` `turn_has_catalog_facts` |
| English switch | `SYSTEM_CORE` in `app1/agent.py` |
| Own order not looked up | `get_employee_tool_selection` in `ai_tool_registry.py`; `get_my_course_status` description in `app1/tools.py` |
| Account deletion skips help | `looks_like_platform_help` in `app1/help_kb.py` |
| Space before `/products/` | `fix_markdown_links` in `app1/card_text.py`; the same rewrite in `chat.js` and `ai-stream.js` |

The patch does not rewrite an answer that still contradicts the tool result. A later turn can still say a proposal is saved, still answer in English, and still quote a price that is not in the tool output. The disclaimer still fires when a price is asserted and not withdrawn.

## Offline suite

`py -m pytest -q` on this worktree: 2,597 passed, 1 failed, 5 skipped. The failure was `tests/test_chatbot_order_contact_flow.py::ReadinessContactTests::test_preparation_uses_the_only_variant_and_still_needs_confirmation`. The fixture session date was `2026-11-01`, which `enrollment_service.quote_course` rejects once that day is past. The fixture date in this branch is `2027-03-01`. Re-running that file afterwards: 20 passed. The full suite was not run again after that one-line fixture change.
