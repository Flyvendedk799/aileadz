# AI Framework - Futurematch / aileadz

Canonical map of the AI platform. Goal: change it without re-reading the codebase.
Anchors are symbol names (grep them; line numbers rot). Keep this file in sync
when you change AI surfaces. Provider toggle / API keys: [AI_PROVIDER_TOGGLE.md](AI_PROVIDER_TOGGLE.md).
Eval harness: `ai_eval/README.md`. Mind-map page: [MIND_MAP_V2.md](MIND_MAP_V2.md).

North star: the AI is a **helper with a toolbox, never a form-filler**. Tools are
offered, the model decides; nothing here may add a mandatory step.

## 0. Where to look

| Concern | File |
|---|---|
| Employee agent: prompts, `MODE_PROFILES`, stage/intent, SSE stream (`handle_agentic_ask` -> `stream_generator`) | `app1/agent.py` |
| `/app1/*` routes (`ask`, `new_session`, `confirm_tool_action`, `memory`, `feedback`, widget), course-card serialisation | `app1/__init__.py` |
| Tool schemas (`OPENAI_TOOLS`, `PROFILE_TOOLS`) + executors + `execute_tool` dispatch | `app1/tools.py` |
| Per-turn tool menu, `ToolMeta`, labels, provider schema converters, forced-tool rules | `ai_tool_registry.py` |
| Model loops (chat / responses), fallback, routing, budgets, live tool events, run logging | `ai_runtime.py` |
| Claude runtime + adapter (`run_anthropic_agent`, shadow) | `ai_provider_anthropic.py` |
| Provider + model selection, `ai_settings` | `ai_provider.py` |
| API keys (Fernet, `ai_secrets`) | `ai_secrets.py` |
| Context layers + budgeted assembly | `ai_context_layers.py` (wired in `ai_runtime.prepare_messages_for_turn`) |
| Few-shot, pruning, session summaries, iteration caps, warmup | `ai_context.py` |
| Cost model (USD/DKK, price table) | `ai_cost_model.py` |
| `<suggestions>` parse/strip + SSE helper for HR/vendor/widget | `ai_reply.py` |
| SSE vocabulary, `UI_ACTIONS`, `HR_DESTINATIONS` (canonical) | `app1/sse_events.py` |
| Confirm/audit helper for write tools | `tool_confirm.py` |
| Confirm-token store (MySQL `ai_confirm_tokens`) | `app1/confirm_store.py` |
| Grounding circuit-breaker + `delimit_untrusted` fence | `grounding.py` |
| Conversation state (sessions, pointers, `rev`, digests) | `app1/conversation_state.py` |
| Semantic user index + `recall_about_user` | `app1/user_knowledge.py` |
| Platform help KB + `search_platform_help` | `app1/help_kb.py`, `app1/help_kb/*.md` |
| Profile store (all `user_*` tables), completeness, memories | `app1/user_profile_db.py` |
| Weekly heartbeat: check-in queue, candidates, layer, `resolve_checkin` / `record_learning_outcome` | `profile_checkins.py` (+ `user_profile_checkins` in `user_profile_db.py`, job in `scheduler.py`) |
| Skill canon/categories/gaps | `competency.py` |
| RAG (hybrid retrieval, rerank, profile boost) / offline index build | `app1/rag.py` / `app1/build_index.py` |
| AI analytics store (sessions, debug/latency logs, feedback, anon profiles; MySQL, SQLite dev fallback) | `app1/memory_store.py` |
| HR learner context / HR-goal sharing | `learner_context.py` / `goal_sharing.py` |
| HR advisor loop, prompt, page context | `hr_agent.py` (+ `hr_conversations.py`) |
| HR tools + compliance primitives | `hr_tools.py` |
| Vendor assistant | `vendor_portal.py` (`POST /vendor/ask`), `vendor_tools.py`, `vendor_conversations.py` |
| CV text/image extraction + LLM parse; CV parse-job store | `cv_ingest.py`; `cv_parse_store.py` |
| Profile REST API, CV parse/stream/apply/improve, mind-map graph (`mindmap_payload`), workspace summary | `api.py` |
| Cross-surface handoff context (`{from, focus}` -> `surface_context` layer + read tools) | `app1/surface_context.py` |
| Page shells (`/chat`, `/mind-map`, `/profil/cv`, `/min-laering`, ...; `/ai-profiler` is a 301 to `/chat`, `/profil-upload` a 301 to `/profil#cv`) | `futurematch_ui.py` |
| Orders / policy / credits | `order_lifecycle.py`, `order_service.py`, `team_order_policy.py`, `credit_service.py` |
| Guest -> user memory migration | `anon_migration.py` |
| Chat frontend (SSE dispatch, renderers) | `static/futurematch/assets/chat.js`, `chat.css` |
| Templates | `templates/fm/{chat,my_profile,_cv_import,my_cv,mind_map,_ai_panel}.html`, `templates/fm_base.html` |
| CV import on the profile page (upload/paste, review, apply) | `static/futurematch/assets/profile-cv.{js,css}` + `templates/fm/_cv_import.html` |
| GDPR export/erase coverage | `gdpr_service.py` (drift test `tests/test_gdpr_table_coverage.py`) |

## 1. One engine, one assistant

The employee AI is **one agentic chat engine** (OpenAI or Claude, admin toggle) with one
persona. The former AI Profiler is built into the assistant; there is no mode switch and no
separate page. `ask()` (`app1/__init__.py`) ignores the client's `mode` and picks it from the
session:

| Mode | Who | What |
|---|---|---|
| `assistant` | every logged-in user (`/chat`) | `SYSTEM_PLAYBOOK_ASSISTANT` as `core_playbook` (never trimmed), the advisor's stage hints, all flow playbooks (`buying, profile_save, cv_onboarding, search, situation`), the profiler's need-driven `profiler_state` layer, `profiler_progress`, the full profile/gap/path/goal toolbox on every non-chit-chat turn, 12 memories |
| `default` | anonymous visitors | the plain course advisor (there is no profile to build on) |
| `profiler` | legacy / tests only | the old persona, still resolvable in `MODE_PROFILES`; no request reaches it any more |

`PROFILING_MODES = ("assistant", "profiler")` gates every profile-aware branch in `agent.py`
and the tool selector. The assistant **asks follow-ups only when the answer would change its
advice** and answers a concrete request first; profile questions come after and only if they
help. Everything else about the engine (one endpoint `POST /app1/ask`, one frontend
`chat.js`, one toolset) is unchanged. Per-mode policy lives in `MODE_PROFILES`
(`app1/agent.py`): core playbook, flow playbooks, few-shot set, stage hints, memory limit,
`prefer_quality`, handoff thresholds.

**One surface.** `conversation_state.surface_for_mode` maps every mode to `chat`, so there is
one open conversation, one digest and one session id. Conversations that began in the old
profiler (`conversation_history.mode = 'profiler'`) resume in `/chat`; the sidebar shows them
all under one "Assistent" badge. `/ai-profiler[?from&focus&intent&c]` redirects (301) to
`/chat` with the same query string, so bookmarks and old links keep working.

`/app1/ask` also accepts `kind: "seed"` for UI-generated openers (profiler
Start/Fortsæt, `window.fmSendSeed`): the regex intent classifier is skipped
(intent `profiler_resume`), so an opener's wording can never pull in a playbook.

### 1b. One learner workspace: handoffs between surfaces

The assistant, the Mind-Map and the profile page (`/profile`, which hosts the CV import)
share one profile and one memory, and hand the user to each other with context:

- **URL contract:** `?from=<surface>&focus=<ref>&intent=<text>` on `/chat`. `chat.js bootChat()` strips the params, then sends `context: {from,
  focus}` with the **first** message only (a retry re-sends it). `intent` goes in as
  the user's message; a `focus` without words sends a neutral seed; a bare `from` waits
  for the first message (or the profiler's own opener, which no longer fires on top of a
  handoff: `bootChat` resolves `true` when it sent something).
- **Vocabulary** (`app1/surface_context.py`): `from` in `SURFACES` (`chat, profiler,
  mind_map, profile, cv_upload, my_learning, goals, timeline`); `focus` is `section:<key>`
  (`SECTIONS`) or a Mind-Map node id (`skill:42`, `exp:7`, `edu:`, `cert:`, `lang:`,
  `goal:`, `link:`, `path:`, `course:`, `mem:`), the same ids as `open_mind_map` and
  `#n=`. `normalize_context` whitelists in `ask()`; `resolve_focus` looks the ref up in
  the user's **own** profile/memories (an unknown ref is dropped, so the client can only
  point, never inject text).
- **Effect:** a `surface_context` layer (trusted header; quoted profile text fenced as
  data) phrased as a natural place to start, and `origin_tool_names` -> the selector's
  `context_tools` (read tools only, plus the proposal-only `forget_about_user` for a
  memory focus; side-effect tools are filtered out like `_HR_PAGE_TOOLS`).
- **Entry points:** Mind-Map inspector "Spørg AI om dette" / "Uddyb med AI" and the gap
  CTA; "Uddyb med AI" on each profile section; the CV import's "Gennemgå med
  AI-assistenten" / "Find kurser til mine gab"; `open_in_app(open_profiler|open_advisor,
  section|node, intent)` (`_handoff_url`).
- **One number, one fetch:** `GET /api/profile/workspace` (completeness + Mind-Map
  counts, built by `api.mindmap_payload(with_gaps=False)` so "datapunkter" cannot drift).
  `chat.js refreshWorkspace()` debounces every refresh into one request and dispatches
  `fm:workspace`; the profiler banner listens instead of fetching the graph. Every
  surface shows the depth-aware `weighted_pct` as "profilstyrke" with `next_help`,
  never a "Mangler: ..." list.

## 2. Request lifecycle

```
chat.js run() -POST {query,mode,kind}-> /app1/ask  (credit_service.guard first)
  handle_agentic_ask (agent.py)
    - conversation_state.resolve_sid; rebuild CHAT_MEMORY from conversation_history if missing/stale (rev)
    - _classify_intent_local (+ ai_runtime.classify_intent_llm router only on the 'discovery' catch-all)
    - _detect_conversation_stage -> reconcile with intent
    - get_employee_tool_selection (ai_tool_registry) -> per-turn tool menu + optional forced tool
    - stream_generator:
        tagged context layers (profile, memories, digests, recall, HR, company rules, playbooks ...)
        run_agent_with_fallback (ai_runtime) -> model loop, tool_choice=auto -> execute_tool (tools.py)
        map tool results -> SSE events; stream the final answer (<suggestions> parsed out)
        grounding circuit-breaker (post-stream disclaimer); cards / profile / cross-surface events; suggestions
```

Tool state is passed per turn via module globals: `set_search_context(...)`
(`tools.py`) injects shown handles, prefs, blocked vendors, supplier agreements.

**Runtime behaviours worth knowing**
- **Tool evidence reaches the final answer.** `run_responses_agent` appends a
  synthetic chat-format assistant message (with `tool_calls`) before the `tool`
  messages, so `_sanitize_tool_sequence` keeps them and the streamed answer sees this
  turn's results.
- **One completion per tool turn** (`AI_CAPTURE_FINAL`, default on): the answer the
  model already wrote on the no-tool-calls iteration is captured and streamed
  (`iter_buffered_text_chunks`, 3 words/chunk). It is regenerated only if empty or
  truncated; once a tool has run, the output cap lifts from the 320-token tool-turn
  cap to `max_output_tokens()`.
- **Turn layout (`chat.js` `ZONES`):** every assistant turn is built from fixed zones, top to
  bottom, whatever order the stream delivers events in: `activity` (tool line), `text` (the
  answer), `rich` (course/comparison/path/profile cards, action buttons), `ask` (anything waiting
  for the user: confirm cards, choice/form/question sheets), `notes` (memory and saved-item
  notes), `foot` (suggestion chips, feedback, errors). A renderer places its element with
  `place(body, zone, el)`, never `body.appendChild`; a new SSE card must pick a zone. The tool
  line is one muted row ("Søger i kataloget" while live, "Brugte n værktøjer" when settled,
  `settleActivity`) that expands to the chips; technical meta (latency, cache, category) lives in
  the chip tooltip. While a card waits for a decision (`awaiting`), generic follow-up chips are
  not added. Visual language: `chat.css` "CALM PASS" block (one card surface, one button
  hierarchy: solid = do it, outline = alternative, text = escape hatch).
- **Tool chips say what happened:** besides the label, a finished chip shows the outcome in
  words (`TOOL_STATUS_NOTES` in `chat.js`: no results / awaiting your confirmation / needs your
  answer / saved), "1 resultat" vs "n resultater", and the server's one-line message as tooltip and
  `aria-label`. A test pins that every employee tool has a `TOOL_LABELS` entry.
- **Live tool chips** (`AI_LIVE_TOOL_EVENTS`, default on): `run_agent_with_fallback`
  runs in a worker thread (`iter_agent_with_live_tool_events`); start/finish
  `tool_call` events stream while it runs; drain timeout 0.5 s doubles as the SSE
  heartbeat (`ping`); hard bound `AI_LIVE_TOOL_EVENTS_TIMEOUT_SECONDS` (90).
  `_TOOL_CACHE` / `_ROUTER_CACHE` are lock-guarded; tenant cache scope is resolved in
  the request thread and passed as `company_scope`.
- **Provider fallback never double-executes writes.** Claude failure -> OpenAI Chat
  Completions (`anthropic-openai-fallback`); responses failure -> `chat-fallback`.
  Once a `side_effect` tool has run, the error is re-raised instead.
- **Model routing** (`AI_MODEL_ROUTING` `quality|balanced|cost`, default `balanced`):
  `_route_tier` sends tool-deciding turns and simple-intent answers to the fast model;
  synthesis intents (comparison, buying, team_buying, profile_and_search, ...) and
  `prefer_quality` stay on main; profile-mutation intents always stay on main (so the
  confirm card UX holds). A rate-limit cooldown forces the fast model.
- **Guards:** `AI_MAX_TOOL_CALLS` (12) per run, repeat-call circuit breaker,
  `AI_MAX_RUN_COST` / `AI_MAX_RUN_TOKENS` (0 = off), `choose_max_iterations`
  (`AI_MAX_TOOL_ITERATIONS`, base 4), per-turn token budget -> "start a new chat".
- **Intent routing:** employee regex classifier first; `AI_LLM_ROUTER` (default on,
  4 s timeout) refines only the ambiguous catch-all.

## 2b. Context assembly - `ai_context_layers.py`

**Why:** all dynamic system messages used to be merged into one `[SESSION KONTEKST]`
blob cut to 1800 chars; cheap layers (few-shot, playbooks) came first, so profile,
memories, profiler playbook and company rules rarely reached the model.

**How:** builders emit tagged layers `_ctx.layer(name, body, header=..., fence=...)`;
`prepare_messages_for_turn` assembles them. Untagged system messages count as `legacy`
(HR agent works unchanged). `LAYER_SPECS` (priority, cap, floor, zone):

| layer | prio | cap | zone |
|---|---|---|---|
| `mode_core_playbook` | 0 (never dropped) | 4500 | steering |
| `company_rules` | 5 | 2500 | knowledge |
| `assistant_context` (HR/vendor: who asks, for which tenant) | 6 | 1000 | knowledge |
| `profile` (`format_profile_for_ai(include_ids=True)`) | 10 | 4500 | knowledge |
| `guidance` / `turn_hint` / `assistant_page` | 12 / 13 / 13 | 1500 / 600 / 500 | steering |
| `surface_context` (cross-surface handoff, see 1b) | 13 | 900 | steering |
| `profiler_state` / `cv_just_applied` | 14 / 14 | 1500 / 500 | steering |
| `employee_info` / `learning_context` | 15 / 16 | 600 / 1500 | knowledge |
| `memories` | 20 | 2000 | knowledge |
| `session_summary` (in-session prune summary) | 22 | 2000 | knowledge |
| `flow_playbooks` | 25 | 3000 | steering |
| `checkins` (weekly heartbeat follow-ups, optional) | 26 | 900 | steering |
| `hr_learning` / `mode_digest` | 30 / 32 | 1800 / 1500 | knowledge |
| `shown_products` / `rejections` | 35 / 38 | 1500 / 800 | steering |
| `recall` | 40 | 2000 | knowledge |
| `returning_note` / `smart_context` / `other_mode_digest` | 45 / 50 / 52 | 600 / 800 / 1000 | knowledge / steering / knowledge |
| `legacy` / `few_shot` | 55 / 60 | 4000 / 1200 | steering |

Over budget: layers with priority >=30 shrink to floor -> are dropped -> layers 1-29
shrink to floor -> priority >=15 are dropped. Bodies are trimmed **before** fencing
(untrusted-data fence never cut open). Output `[static, knowledge, steering, *history]`.
Budget = `min(AI_CONTEXT_MAX_TOKENS, 40% of (input budget - tool schemas))`; tool
schemas count (`estimate_tools_tokens`). `compact_messages_for_api` never re-cuts a
budgeted layer and drops old history before context.

**Placement:** OpenAI - steering moves just before the last user message
(`AI_STEERING_PLACEMENT=trailing|leading`), private keys stripped. Anthropic -
`prepare_messages_for_turn(keep_zones=True)`; `_build_kwargs` keeps knowledge in
`system` behind a second cache breakpoint and trails steering as a `role:"system"`
message on models that allow it (Opus 5/4.8, Fable/Mythos; others fold it into `system`).
Per-turn report logged as debug step `context_assembly`. Rollback:
`AI_CONTEXT_ASSEMBLER=0` (legacy merge + flat per-message cut, fences still rendered).

## 3. SSE event vocabulary - canonical: `app1/sse_events.py`

Producers (`agent.py` et al.) and the consumer (`chat.js` dispatch) must agree on
`type` strings. `KNOWN_EVENT_TYPES` is the set; a drift test requires a chat.js branch
for every entry. Payloads (see producer for exact keys):

| Event | Handler (chat.js) | Payload / note |
|---|---|---|
| `ping`, `[DONE]` | skipped / end-of-turn | heartbeat |
| `meta` | stores `message_index` (feedback) | |
| `thinking` | status line | emitted when tools are on the menu ("Søger og analyserer…") |
| `chunk` | append + markdown re-render | `content` |
| `tool_call` / `tool_progress` | `renderToolCall` / `updateToolProgress` | built by `ai_runtime.build_tool_{call,start,progress}_event`: label/category/status/results_count/latency/side_effect/progress_label/partial_failure/safe_error |
| `course_cards` | `addCourses` | `items[]`, each may carry `why` |
| `product` | legacy HTML, only if no `course_cards` | |
| `comparison_card` | `renderComparisonCard` | `comparison[]`, `analysis{winners,verdict}` |
| `learning_path_card` | `renderLearningPathCard` | `path{title,steps[],total_cost,total_duration_days,id}` |
| `ui_action` | `renderActionCard` | `action,target,label,handle(s),section`; actions = `UI_ACTIONS` |
| `suggestions`, `notice` | chips / italic note | |
| `profile_update` / `profile_saved` / `profile_confirm_request` / `profile_confirm_batch` | note / saved card with undo / confirm card / batch card | `merge_profile_events` collapses saves and batches proposals |
| `ui_card` | form / choice card; `ui_type:"questions"` -> question sheet (`question-sheet.js`) | from `request_user_input` / `ask_user_questions` |
| `memory_used` / `memory_saved` | per-chip delete / inline delete via `id` | |
| `profiler_progress` | `window.onProfilerProgress` | `completeness{}` |
| `confirm_card` | `renderConfirmCard` | opaque `token`, summary, price |
| `cv_summary_card`, `mindmap_card`, `skill_gaps_card`, `agenda_card`, `compliance_card` | `render*Card` | from `show_cv_summary`, `show_mindmap_preview`, `show_skill_gaps`, `get_my_agenda`, `get_my_compliance` |

**Guidance guarantee:** a turn never dead-ends - if the model omits `<suggestions>`,
the server synthesises chips (`_fallback_suggestions`), and chat.js has a final net.

## 4. Tools & the registry

- **Definitions:** `OPENAI_TOOLS` (anonymous-safe) and `PROFILE_TOOLS` (login-only) in
  `tools.py`. **Dispatch:** flat if/elif in `execute_tool`. Tool errors never carry raw
  exception text to the model (`_internal_tool_error`: Danish message + `error_code`).
- **Per-turn menu:** `get_employee_tool_selection`. `catalog_search`, `open_in_app`
  and (logged in) `get_user_profile`/`request_user_input`/`update_user_profile`/
  `remember_about_user` are an always-on core; specialised/mutating tools are added by
  Danish keyword gates; at most one is force-chosen (`_resolve_forced_tool` - only when
  exactly one gate matched; ambiguous "hvem er", budget phrasing etc. never force).
  **Profiler mode** seeds all profile/gap/path/goal/recommendation tools
  unconditionally and disables the `chit_chat` fast-path.
  `drop_superseded_tools` removes legacy tools (`search_courses`, `filter_courses`,
  `get_course_details`, `compare_courses`, `get_vendor_info`) when their `catalog_*`
  successor is in the same menu.
- **Adding a tool is THREE steps.** The menu is built only from `names` inside the
  selector, so a tool defined and dispatched but never added is dead (no error, just
  silence). (1) schema in `OPENAI_TOOLS`/`PROFILE_TOOLS`; (2) executor + `execute_tool`
  branch; (3) reach the menu (core seed, keyword gate, or `_TOOL_TRIGGERS` semantic
  fallback) + register `_EMPLOYEE_META` and `_TOOL_LABELS`. Add a reachability test
  (e.g. `test_cv_summary_reachable_on_profile_query` in `tests/test_ai_copilot_upgrade.py`).
  `tests/test_prompt_tool_name_drift.py` guards tool names mentioned in prompts.
- **Semantic fallback:** `_semantic_tool_fallback` token-overlaps the query with
  `_TOOL_TRIGGERS` (Danish + English) and adds <=2 specialised tools for paraphrased /
  English / typo'd queries (`AI_TOOL_SEMANTIC_FALLBACK`, default on).
- **HR / vendor:** HR uses `get_hr_tool_selection` (core + keyword gates + `_HR_PAGE_TOOLS`
  page hints) and `_HR_META`; vendor has no selector - `VENDOR_TOOLS` is offered whole
  and the system prompt teaches use.
- **`ToolMeta`** (`_EMPLOYEE_META` / `_HR_META` / `_VENDOR_META`): `auth_required`,
  `company_required`, `side_effect`, `parallel_safe`, `cache_ttl`, `confirm_required`,
  `audit_action`, `manager_only`, `progress_label`, label/category/icon. chat.js has a
  parallel `TOOL_LABELS`; backend `label` wins, fallback = humanised name.
- **Write tools = propose -> confirm -> audit** (`tool_confirm.py`):
  `needs_confirmation_payload` (executor returns a preview, nothing runs),
  `manager_guard`, `audit_chat_mutation`. The agent stores args server-side in
  `confirm_store` and emits a `confirm_card` with an opaque token; the browser posts
  `POST /app1/confirm_tool_action`, which consumes the token, injects `confirm=True`
  and re-dispatches (double-confirm is idempotent). HR write tools (e.g.
  `send_company_email`, `create_order_for_employee`, `schedule_recurring_report`,
  `send_deadline_reminders`, `recheck_compliance`) are `manager_only` + confirm-gated;
  `send_company_email` previews recipient count; `create_order_for_employee` rejects
  cross-tenant targets. Employee action tools: `save_course_for_later`,
  `set_course_reminder` (immediate), `manage_my_order` (cancel own order),
  `request_manager_approval` (confirm, self-scoped).
- **Profile writes:** additions save immediately with an inline **Fortryd** (undo);
  removals/edits keep the confirm card; several changes can share one card
  (`AI_PROFILE_AUTOSAVE=0` restores propose-then-confirm). Removing or editing
  experience, education or a certification by name resolves the row id at proposal
  time (`_resolve_profile_entity_id`: several matches -> `choose`, none -> `not_found`
  with what exists), so the confirm click never fails on "id mangler". `set_target_role`
  saves a first direction at once but **proposes** replacing an existing one.

### Key tool behaviours
- **Budget filtering** uses the cheapest bookable variant (`_min_variant_price`,
  `_price_in_budget`), not `variants[0]`; both filter paths (`_filter_products_by_constraints`,
  `_apply_hard_filters`/`_product_passes_hard_filters`).
- **Language/difficulty facets** use `structured_metadata.language` (`dansk|engelsk|begge`)
  / `.difficulty`; unknown metadata is never excluded (`_LANGUAGE_ALIASES`, `_DIFFICULTY_ALIASES`).
- **One price view:** `price_view(raw_price, vendor)` applies the negotiated supplier
  discount; search, details and comparison all use it. Order tools price the **chosen
  variant**.
- **Expired dates** (`AI_FILTER_PAST_DATES`): `_upcoming_dates` drops strictly-past
  parseable dates, keeps unparseable ones (unknown != past); all past -> `dates: []` +
  `no_upcoming_dates: true`.
- **Hard filters in RAG fallback** (`AI_SEARCH_HARD_FILTERS`): `catalog_search` filters
  fallback results with the same predicates; an emptied set returns unfiltered results
  with `filters_relaxed: true` + `relaxed_filters`.
- **Per-card WHY:** `_course_match_reason` (verifiable: matched query/profile terms +
  attributes, never an LLM guess) -> `serialize_course_cards(reasons=...)` -> `card.why`.
- **`compare_courses` / `catalog_compare_products`**: `_comparison_analysis` computes
  per-axis winners + verdict -> `comparison_card`.
- **`recommend_for_profile` / `suggest_learning_path`** seed from `competency.compute_skill_gaps`;
  a recommendation's `match_reason` is the verifiable gap it closes. Paths ground each
  step in real courses, de-dup, skip completed, roll up cost/duration, persist via
  `save_learning_path` (`user_learning_paths`).
- **`open_in_app`** (always on, no mutation) -> `ui_action`. Validated against
  `sse_events.UI_ACTIONS` (a test pins the tool enum to it both ways); includes
  `open_cv_upload` (`/profil#cv`, the import on the profile page), `open_my_cv` (`/profil/cv`, the
  printable generated CV), `open_mind_map`, `open_my_learning`
  (`/min-laering`), `open_goals` (`/mine-maal`), `open_timeline` (`/min-tidslinje`).
  `open_profiler` / `open_advisor` take `section` or `node` + `intent` and build a
  handoff URL (1b); `open_catalog` URL-encodes its query; `open_profile` anchors only to ids that exist
  on the profile page (`_PROFILE_PAGE_SECTIONS`, test-pinned to the template).
- **`show_cv_summary` / `show_mindmap_preview` / `show_skill_gaps`** (profile-gated):
  read-only cards; reach the menu by keywords + semantic fallback.
  `show_skill_gaps` = `compute_skill_gaps` (1-5 scale) -> `skill_gaps_card`; its JSON is
  the data the model reasons over (chains to `recommend_for_profile`).
- **`get_my_agenda`** (profile-gated): cross-silo "what's on my plate" - course
  deadlines + pending approvals (`course_orders`, `order_approvals`), expiring
  certifications (`user_certifications`, `cert_expiry_service.parse_expiry`), dated
  goals; worst-first inside a clamped 7-365 day horizon; each source degrades
  independently (missing `order_approvals` -> plain `course_orders`). -> `agenda_card`.
- **`get_my_compliance`** (profile + company gated): learner mirror of HR's matrix.
  `hr_tools.compliance_requirement_applies` / `compliance_completion_matches` /
  `compliance_state_for_entries` are shared by `derive_company_compliance` and
  `derive_employee_compliance`, so learner and manager never get different answers.
  -> `compliance_card`.
- **`get_learning_context`** returns profile + company budget + supplier agreements +
  completed courses.
- **Orders:** `create_course_order` fails truthfully, sends ONE e-mail, is idempotent
  per person+course+date for 10 min (`order_service._find_recent_duplicate`), takes
  optional `participants` and follows team policy (below). `mark_course_complete`
  calls `order_service.complete_order` (the single completion path) and returns
  `skill_proposals`, `next_steps`, `review_url` for the model to offer in conversation.
  Billing is off-platform: never show payment details; copy is "Du modtager faktura".
- **Memory:** `remember_about_user` supersedes near-duplicates (`_find_supersedable_memory`:
  token-based, category-scoped - a substring rule once let "Java" overwrite
  "JavaScript"). `memory_saved` carries the row `id` for inline delete.
  REST: `GET /app1/memory`, `DELETE /app1/memory/<id>`. The `memories` layer prints
  `[#id]` per memory; **`forget_about_user`** (`memory_id` or the user's words) only
  proposes: it returns a `profile_confirm_request` card whose click posts
  `remove_memory` to `/app1/confirm_profile_update` (several matches -> `choose`).
  Reached on "glem / forget / husker forkert" phrases, in profiler mode, and from a
  memory focus. `get_learning_context` (profile + budget + agreements in one read) is
  reachable on budget/agreement phrases and the semantic fallback.

## 5. Profile, completeness, competency, CV, mind-map

- **Store:** `app1/user_profile_db.py` tables `user_skills` (+`category`), `user_experience`,
  `user_education`, `user_completed_courses`, `user_profile_summary` (+`target_role`),
  `user_certifications`, `user_languages`, `user_portfolio_links`, `user_memories`,
  `user_learning_goals`, `user_learning_paths`; `ensure_tables()` runs idempotent
  CREATE/ALTER every boot. All are covered by GDPR export/erase (drift-tested).
- **Competency** (`competency.py`): `canonical_skill()` + `skill_category()` run on every
  skill write (`add_skill`, `/api/cv/apply`, chat `update_user_profile`). The 1-5 scale
  is **reused from `hr_tools.SKILL_LEVEL_MAP`** (begynder=1..ekspert=5), never forked.
  `compute_skill_gaps(username)` diffs current skills vs `company_skill_targets` +
  `target_role` (`ROLE_SKILL_HINTS`) + goals; offline-safe. REST `GET /api/profile/skill-gaps`.
- **Completeness - one source:** `profile_completeness(username, profile=None)`. The
  8-section binary `pct`/`total`/`missing` contract is unchanged (tests depend on it);
  it also returns depth-aware `weighted_pct`, per-section `strength`, `weakest`,
  `target_role`. Consumed by `/api/profile/completeness`, `/api/profile/workspace`,
  the profile page, `futurematch_ui._home_skill_completeness`, the profiler ring, the
  chat status and the mind-map; every one of them displays `weighted_pct`.
- **Profiler -> suggester handoff (legacy `profiler` persona only; the assistant recommends
  courses itself via `recommend_for_profile` once it knows a direction):** fires when `weighted_pct >= AI_PROFILER_HANDOFF_PCT`
  (70) or a target role is set and `weighted_pct >= 40`: `recommend_for_profile` ->
  `course_cards` + CTA. Persisted in `conversation_history.state_json.handoff`; marked
  fired only when courses were shown, else retried up to 2 attempts.
- **No scripted opener:** the assistant does not auto-send a seed on an empty thread;
  `kind:"seed"` remains for UI-generated openers (e.g. a handoff with a `focus` but no words).
- **CV awareness:** `apply_cv_items` stores `session['cv_applied'] = {t, counts, gaps}`;
  `cv_applied_note(surface=...)` becomes the `cv_just_applied` layer once per surface
  within the hour (`mark_cv_note_seen` runs in the request phase: the cookie session is
  written before the SSE body streams, so a write inside `stream_generator` is lost).
- **Heartbeat check-ins** (`profile_checkins.py`): the weekly job `profile_checkin_heartbeat`
  queues up to 3 short, reasoned follow-ups per person who used the assistant in the last 90
  days (`user_profile_checkins`): a course completed 7-120 days ago (`progress`: did you get
  something out of it, use it, share it), an active goal untouched for 14+ days (`goal`), an
  unknown direction (`direction`), the weakest profile area (`gap`). The queue holds a topic and
  a *reason*, never wording. In an assistant conversation's first turn (not after a handoff or a
  CV apply) the `checkins` layer shows up to 2 open items as optional background with `[#c12]`
  ids; the model decides whether and how to raise one, and closes it with `resolve_checkin`
  (`answered|dismissed|snooze|stop_all`) or, when the person describes what a course led to,
  `record_learning_outcome` (one `kontekst` memory + closes the matching `progress` item).
  Showing an item counts as asking: 3 days rest, 3 asks max, 28 days TTL, re-armed only after
  90 days. `stop_all` mutes the person for good (`kind='optout'` row); `AI_PROFILE_CHECKINS=0`
  is the platform kill switch. Rows are the person's own (GDPR export + erase), HR never sees them.
- **Several questions at once -> a sheet, never a list to type back.** The assistant asks ONE question at a
  time in the text. When it truly needs 2-4 answers it calls `ask_user_questions` (core tool, anonymous-safe,
  writes nothing): the `ui_card` event with `ui_type:"questions"` renders a sheet in `chat.js` via
  `static/futurematch/assets/question-sheet.js` (a field per question, optional tappable answers, Ctrl+Enter),
  and the filled sheet goes back as ONE message (`Label: svar` per line + `Springer over: ...`; "Spring over"
  sends a neutral line). If the model writes a list of >=3 questions as prose anyway, `fromList()` reads the
  rendered list and puts the same sheet under it (skipped on a turn that already showed one). "Fra bunden"
  starts with one open question plus a nudge to upload a CV, never a field checklist. Pinned by
  `tests/test_question_sheet.py`.
- **Excerpt, not a silent cut:** the `profile` layer caps each section (`_PROFILE_LAYER_CAPS`) and says
  "(+N flere, hent alle med get_user_profile)" when it left rows out; `get_user_profile(full=true)`
  returns every row with full text (`format_profile_for_ai(full=True)`). Whatever the profile page can edit
  (skills, experience, education, certifications, languages, links, summary, target role) has an
  `update_user_profile` action, pinned by `tests/test_assistant_profile_access.py`.
- **Need-driven context:** the `profile` layer (with `[#id]`s) is what the model knows;
  `_build_profiler_state` adds depth, target role and <=3 unknowns *with the reason each
  matters* (strength >=0.5 never listed). No imperatives - guard tests
  (`tests/test_profiler_context.py`) fail on "DIT NÆSTE SPØRGSMÅL / SKAL VÆRE OM /
  SPØRG ALDRIG / ALLEREDE AFDÆKKET". Completeness is context, not a goal (no X/8 banner).
- **Playbooks:** `SYSTEM_PLAYBOOK_PROFILE_SAVE` (both modes) is split from
  `SYSTEM_PLAYBOOK_CV_ONBOARDING` (advisor only); the profiler gets search only on an
  explicit course request.
- **One profile fetch per turn:** `get_full_profile` runs once in `stream_generator` and is
  shared with completeness, gaps, preferences, memory ranking and the index.
- **Memories** are ranked per turn by the semantic index (`_select_memories_for_turn`,
  keyword fallback); only matched ones are `_relevant`, so `used_count` means "informed an answer".

**CV import on the profile page** (`#cv` card on `/profile`: `_cv_import.html` + `profile-cv.js`;
the separate 3D portal and its no-JS fallback are gone, `/profil-upload` 301s to `/profil#cv`;
own SSE pipeline in `api.py`, event names `stage`/`result`/`error` - NOT the chat vocabulary). The
script emits `fm:cv-applied` so the profile page reloads what the CV touched; the review list is
plain, editable and accessible (a checkbox per item, nothing written before "Gem til min profil"):
- `POST /api/cv/parse` (multipart, <=8 MB, whitelisted exts) -> `cv_ingest.extract_text`
  (PDF/text/image OCR via GPT-4o vision, timeouts, `delimit_untrusted` fence) -> LLM parse
  in a background thread that pushes its own `app_context`; result stored in
  `cv_parse_store` (MySQL `ai_cv_parse_jobs`, 5 min TTL, in-process fallback - the old
  process-local dict broke under multiple gunicorn workers).
- `GET /api/cv/parse-stream` polls the real `cv_parse_store.read_state`, emits one terminal
  `result` (`{proposal, hint}`) or `error`. Empty proposal -> the import resets with the `hint`.
- `POST /api/cv/apply` writes approved items (`add_skill/experience/education/certification/language`);
  `conflict_mode` `merge|replace|keep`; returns per-item outcomes. **Level vocab must go
  through `_SKILL_LEVEL_MAP` / `_LANG_PROF_MAP`** (case-insensitive; accept the import's labels
  Begynder/Øvet/... and parser lowercase) - a capitalised-only map once inflated every
  skill to `avanceret`. `POST /api/cv/improve` is a non-destructive, section-scoped coach
  that must not invent evidence. `GET /api/cv/summary` feeds `show_cv_summary`.
- **Futurematch-generated CV:** `GET /profil/cv` (`futurematch_ui.my_cv`, `my_cv.html`) lays the
  logged-in user's own profile out as a printable CV (browser print -> PDF); stored links are shown as
  text, never as hrefs.

**Mind-map** (`/mind-map` -> `mind_map.html` + `mind-map-support.js`, graph from
`GET /api/profile/mindmap` = `get_mindmap_api`): full detail in
[MIND_MAP_V2.md](MIND_MAP_V2.md), invariants pinned by `tests/test_mind_map_v2.py`.
Gotchas: the DC template uses `{{ }}`, so the block is wrapped in `{% raw %}`; never
write a literal `x-dc` open tag before the real element (even in a CSS comment) -
`parseDcText` regex-matches the FIRST one. Memory CRUD: `/api/profile/memories`
(DELETE `{id}`, POST `{label,detail,category,source}`, PUT `{id,label,detail,category}`
from the same inline composer; the page's category chips must mirror
`_MEMORY_CATEGORIES` - tested). A non-2xx graph response shows a retry card with a
Danish message, never a fake-empty profile.

**Discoverability:** `/mind-map` is in the employee sidebar (`fm_base.html`); CV upload and "Se dit CV" are
on the profile hero and its CV card, and CV upload is also on `employee_home.html`. The
AI entry points between surfaces are listed in 1b.

## 6. Trust spine

- **Grounding** (`grounding.py`): post-stream chain-of-custody check appends
  `GROUNDING_DISCLAIMER_DA` when the answer asserts a price/date/title absent from THIS
  turn's tool results. Threshold: >=1 unsupported price/date, or >=2 unsupported titles
  (TitleCase runs count only if they fuzzy-match a known catalog title, injected as
  `known_titles_fn` so the module stays dependency-free). Tool **arguments never count
  as evidence** (user-echoed prices can't "support" a claim). Price matching is
  token-boundary + cents-aware (`_canon_amount`/`_evidence_amounts`).
  `AI_GROUNDING_RECALL` (default off) = one bounded corrective re-generation on the
  buffered path; `AI_GROUNDING_DECOMPOUND` (default off) = Danish decompounding. HR and
  vendor answers are checked too.
- **Confirm store** (`confirm_store.py`): MySQL `ai_confirm_tokens` + in-process fallback,
  TTL 10 min, session-bound, single-use pop; tokens minted on one gunicorn worker resolve
  on another. Confirm tokens are tried against every session id the browser holds.
- **Untrusted data** is fenced with `grounding.delimit_untrusted` (tenant names, CV text,
  memories, vendor names ...).

## 6b. Conversation state & knowledge layer

**State - `conversation_state.py`.** Every conversation is a `conversation_history` row
keyed `(username, session_id)` with a `rev` counter (`user_conversations` is legacy,
read-only, still in GDPR).
- `resolve_sid(session, mode)` -> `session["session_ids"]["chat"]`: every mode maps to the one
  surface (`surface_for_mode`), so old `profiler` rows and pointers resume in the assistant.
- `load(username, sid)` reads exactly that row - no "latest" fallback on `/ask`.
- `save_turn(..., expected_rev=CHAT_MEMORY_REV[sid])` uses `WHERE rev = %s`; a stale worker
  merges (`merge_transcripts`) instead of overwriting; a worker with a different cached rev
  rebuilds at turn start.
- **Opening a surface starts a new chat:** `chat.js bootChat()` -> `newChat()` ->
  `/app1/new_session {mode}` saves + digests the old session and points the surface at a
  fresh id - nothing is deleted. Past chats live in the sidebar; the open one is pinned as
  `?c=<id>` (reload reopens via `/app1/conversations/<id>/resume`). Continuity = profile +
  memories + digests, not the transcript. `user_active_sessions` + `/load_conversation`
  remain as an API.
- The widget runs with `sid_override` + `company_override` and never touches the employee session.
- Durable HR (`hr_conversations.py`, mode `hr`) and vendor (`vendor_conversations.py`,
  mode `vendor`, owner `vendor:<id>` from session only) memory use the same table.

**Cross-session memory.** `ai_context.summarize_session` (fast model,
`AI_SESSION_SUMMARY_MODE=llm|rules`) writes `conversation_history.summary` and merges it
into the surface digest `user_conversation_summaries (username, mode)` via
`digest_session_async` on new chat/resume, with lazy catch-up (`latest_undigested`).
Injected as `mode_digest`; the other surface's as `other_mode_digest`. In-session pruning
(`prune_conversation_memory`) carries an earlier `SAMTALEOVERSIGT` summary into the next
one instead of losing it.

**Semantic index - `user_knowledge.py`.** `user_knowledge` rows (memory / profile_fact /
conversation) with normalised float32 embeddings (`rag.embed_texts`, same model as the
catalogue; keyword fallback offline). `sync_user` diffs content hashes (throttled 300 s,
forced after a profile write); `search` scores 0.8 cosine + 0.2 keyword. Consumers: memory
ranking, `recall` layer, `recall_about_user` tool. Embeddings always go to OpenAI, even on
Claude; `AI_USER_KNOWLEDGE_EMBEDDINGS=0` = keyword-only.

**HR learner context - `learner_context.py`.** The learner's assigned paths/progress, HR
skill-matrix gaps, department targets, HR-written goals; each source degrades
independently; 120 s per-worker cache. HR goals are **opt-in per goal**: only
`employee_goals.shared_with_employee = 1` (`goal_sharing.SHARED_PREDICATE`, carried by
every learner-facing query; a test scans the repo for unfiltered reads), plus company
switch `company_settings.ai_learner_hr_goals` (default on) and `AI_LEARNER_HR_GOALS=0`
platform kill switch. Unshared goals never reach the learner UI, AI context or data export.

**Platform help - `help_kb.py` + `help_kb/*.md`.** Curated Danish articles (front matter
`title/slug/url/keywords/audience`; each `url` must be a real route - drift test
`tests/test_help_kb.py`). `search_platform_help` reaches the menu only on a how/where
phrase AND a platform noun (`looks_like_platform_help`). Optional embedding index:
`python -m app1.help_kb --build`.

**Guests:** `anon_migration.migrate` turns the anonymous profile into the user's memories
(source `anonymous`) on login and deletes the guest row only after the copy succeeded.

## 7. RAG (course suggester)

`app1/rag.py`: offline enrich+embed build (`build_index.py`; also incremental embed of new
products, `shopify_sync` job, admin "Genopbyg indeks"), hybrid BM25+vector with RRF fusion,
a relative-to-top score floor (`_apply_score_floor`; below-threshold backoff returns the top
candidates flagged `below_threshold`), GPT-4o-mini rerank gated by ambiguity
(`AI_RAG_CROSS_ENCODER` auto, `AI_RERANK_*`), lexical short-circuit that skips the embedding
call (`AI_EMBED_SKIP_MARGIN`, `AI_DISABLE_EMBED_SKIP`), and profile-conditioned re-rank
(`hybrid_rank_products(profile_boost=...)`). Entry: `semantic_search_courses_detailed`.
Tool JSON is re-resolved to full products by handle (`resolve_products_for_ui`) and
serialised by `serialize_course_card[s]` (`app1/__init__.py`). Embedding model
`AI_EMBEDDING_MODEL` (text-embedding-3-small, 1024 dims) - **always OpenAI**.
**Catalog is one source:** `catalog_service.get_products()` feeds pages, search and chat;
`app1.rag` is an index over it. The raw Shopify JSON is not in git: `CATALOG_SOURCE_FILE`
(default `app1/shopify_products_all_pages.json`) points at it.

## 7b. Other chat surfaces

**HR advisor** (`hr_agent.py`, `POST /hr/chatbot/ask`, panel `templates/fm/_ai_panel.html`
auto-included from `_hr_subnav.html`). Own small SSE set: `ping, thinking, text, meta,
tool_call, confirm_card, ui_action, suggestions, error, done`. Credits are guarded before
the turn; confirm cards post to `/app1/confirm_tool_action`.
- **`hr_open_in_app`** (read-only, always on): opens a page by `sse_events.HR_DESTINATIONS`
  id (same ids as `_hr_subnav.html`), URL resolved server-side with `url_for` (literal
  path fallback); also `view_product`/`open_catalog`. The panel renders an
  `<a class="fm-aip-action">` and drops anything not a same-origin absolute path.
- **Page context:** the panel posts `page` (the `active_hr_page` id); `hr_chatbot_ask`
  whitelists it against `HR_DESTINATIONS` and passes it to `handle_hr_ask(page=)` (-> an
  `AKTUEL SIDE:` line) and `get_hr_tool_selection(page=)` (-> `_HR_PAGE_TOOLS`).
  Additive and **reads only** (`_hr_page_tool_names` filters `side_effect` tools):
  standing on the approvals page does not mean intent to approve.

**Vendor assistant** (`POST /vendor/ask`): every turn gets the whole `VENDOR_TOOLS`
list, executed with the SESSION vendor name - a vendor can never reach another vendor's
or any buyer's data. Prompt shares the HR tone examples and `<suggestions>` contract;
vendor name is fenced; figures pass grounding.
- **`vendor_catalog_health`** (read-only): audit of the vendor's OWN listings (missing
  price, no bookable dates, thin description, missing category/difficulty/language/
  duration/image) -> per-course `severity` + catalog `health_score` over (course x check)
  cells. **Honesty rule:** difficulty/language/duration come from `structured_metadata`
  (LLM enrichment, `build_index.extract_structured_metadata`), not vendor input; if no
  course carries it, the three derived checks are skipped, the denominator shrinks to
  `checks_applied`, and `enrichment_missing` + a Danish note say so. Unparseable variant
  dates are never counted as expired.

## 8. Orders, policy, credits, widget

- **One order lifecycle** (`order_lifecycle.py`): `pending_approval -> approved -> booked ->
  completed`, plus `rejected`/`cancelled`; legacy values normalised. Every tool reads these
  labels. Billing is a separate off-platform dimension.
- **Team orders follow company policy:** `team_order_policy.py` + `company_team_order_policy`
  (`linked_orders` | `hr_bulk_assign` | `not_allowed`; company default + per-vendor
  override). `create_course_order(participants=...)` reads the effective policy and returns
  `policy_guidance_da` for the model to phrase naturally; linked orders share a
  `group_order_id` and each passes approval + budget.
- **AI analytics store:** feedback, debug logs, latency, anonymous profiles live in MySQL
  (`ai_*` tables via `schema_registry`; SQLite only with `AI_MEMORY_BACKEND=sqlite`;
  `import_legacy_sqlite` copies old rows). One feedback scale everywhere: +1/-1 with
  `message_index` (`POST /app1/feedback`, from the chat.js thumbs; `meta` event supplies the index). Chat-to-order attribution fills `chatbot_session_id` /
  `chatbot_queries_before_order`. Run telemetry: `ai_agent_runs` (`runtime`, `runtime_path`),
  tool runs via `log_tool_run`.
- **Credits = AI-usage metering** (`credit_service.py`): every AI turn (advisor, profiler,
  HR, vendor, widget) is deducted via the `credit_usage` ledger; cost = `ai_cost_model` x
  `AI_CREDITS_PER_DKK` (10), min 1 credit. Per-company balances (`company_credit_accounts`),
  solo users `users.credits`; grants only via `credit_service.grant`. Low balance notifies HR;
  at zero the company setting decides `soft` (default) or `hard` (AI paused with
  `HARD_LIMIT_MESSAGE`); `/app1/ask` and HR ask call `credit_service.guard` first.
  `verify_ledger`: balance == -SUM(credits_used).
- **Widget** (`/app1/widget/<token>`): iframe document served only to an allowlisted parent
  and carries `frame-ancestors`; it gets a signed 12 h session token sent as
  `X-Widget-Session`, which `/ask` accepts instead of trusting `Origin`; the conversation id
  lives in the token (no third-party cookies). Stream carries `suggestions` like the others.
- **Prompt A/B:** `AI_PROMPT_VARIANTS=v2.0,v2.1` assigns variants deterministically by
  session id (first = control); `AI_PROMPT_ADDENDUM_<V>` (dots -> underscores) adds
  instructions to a non-control variant.
- **Self-eval:** `ai_eval.scorers.score_live` scores each live turn in-process (no LLM) from
  `agent.py`; stored as `ai_agent_runs.self_eval_score`.

## 9. Env flags (all verified in code; default in parentheses)

| Flag | Default | Effect |
|---|---|---|
| `AI_PROVIDER`, `AI_MAIN_MODEL`, `AI_FAST_MODEL`, `ANTHROPIC_MAIN_MODEL`, `ANTHROPIC_FAST_MODEL`, `AI_MODEL_ROUTING`, `AI_SHADOW_SAMPLE_RATE` | `openai`, `gpt-4o`, `gpt-4o-mini`, `claude-opus-5`, `claude-haiku-4-5`, `balanced`, `0.1` | admin-editable (`ai_settings` -> env -> default); see AI_PROVIDER_TOGGLE.md |
| `OPENAI_API_KEY` / `ANTHROPIC_API_KEY` / `AI_SECRET_KEY` | - | keys (DB -> env); `AI_SECRET_KEY` = Fernet key; OpenAI key always required (embeddings) |
| `ANTHROPIC_MIN_MAX_TOKENS` / `ANTHROPIC_ANSWER_MAX_TOKENS` / `ANTHROPIC_EFFORT` | 4096 / 16000 / auto | Claude `max_tokens` floors (tool / answer turns); effort override |
| `AI_ANTHROPIC_TIMEOUT_SECONDS` / `AI_OPENAI_TIMEOUT_SECONDS` / `AI_ANTHROPIC_MAX_RETRIES` | 45 / 45 / 1 | client timeouts / retries |
| `AI_RUNTIME` | `responses` | OpenAI loop: `responses` or `chat` |
| `AI_CAPTURE_FINAL` | on | one completion per tool turn (0 = discard + regenerate) |
| `AI_LIVE_TOOL_EVENTS` / `AI_LIVE_TOOL_EVENTS_TIMEOUT_SECONDS` | on / 90 | live tool chips from a worker thread / its hard bound |
| `AI_MAX_INPUT_TOKENS` / `AI_TPM_BUDGET` | 36000 / 42000 | input budget / soft TPM ceiling (set in deploy env too) |
| `AI_MAX_OUTPUT_TOKENS` / `AI_MAX_TOOL_TURN_OUTPUT_TOKENS` | 600 / 320 | answer cap / tool-deciding-turn cap |
| `AI_TOOL_TURN_TEMPERATURE` | 0.2 | OpenAI temperature on tool-deciding turns |
| `AI_TOKEN_CHARS_PER_TOKEN` | 4.0 | token estimator divisor (Danish tokenises worse; try 3.5) |
| `AI_MAX_TOOL_CALLS` / `AI_MAX_TOOL_ITERATIONS` | 12 / 4 | per-run tool-call ceiling / loop base |
| `AI_MAX_RUN_COST` / `AI_MAX_RUN_TOKENS` | 0 / 0 | per-run USD / token ceilings (0 = off) |
| `AI_RATE_LIMIT_RETRY_SECONDS` / `_BACKOFF_CAP_SECONDS` / `_COOLDOWN_SECONDS` | 2.5 / 20 / 90 | 429 handling; cooldown forces the fast model |
| `AI_LLM_ROUTER` / `AI_LLM_ROUTER_TIMEOUT_SECONDS` | on / 4 | LLM intent router on the ambiguous catch-all |
| `AI_TRACE_SAMPLE_RATE` | 1 | run trace sampling |
| `AI_CONTEXT_ASSEMBLER` / `AI_CONTEXT_MAX_TOKENS` / `AI_STEERING_PLACEMENT` | on / 10000 / trailing | layer assembly (0 = legacy flat cut) / ceiling / OpenAI steering placement |
| `AI_FEW_SHOT` / `AI_SUMMARY_MODE` / `AI_SESSION_SUMMARY_MODE` | compact / smart / llm | few-shot set / prune summary mode / per-session digest (`llm`\|`rules`) |
| `AI_TOOL_ROUTER_V2` | on | use the per-turn tool selectors (off = all tools) |
| `AI_TOOL_SEMANTIC_FALLBACK` | on | paraphrase/English tool reachability |
| `AI_TRIM_TOOL_DESCRIPTIONS` | off | trim verbose tool descriptions (tokens) |
| `AI_SEARCH_HARD_FILTERS` / `AI_FILTER_PAST_DATES` | on / on | hard filters in RAG fallback + relaxation / drop expired dates |
| `AI_PROFILE_AUTOSAVE` | on | additions save immediately with undo (0 = confirm card) |
| `AI_PROFILER_HANDOFF_PCT` | 70 | profiler -> suggester handoff threshold (legacy `profiler` persona only) |
| `AI_PROFILE_CHECKINS` | on | weekly heartbeat follow-ups and the `checkins` layer (0 = off) |
| `AI_GROUNDING_RECALL` / `AI_GROUNDING_DECOMPOUND` | off / off | corrective re-call / Danish decompounding in grounding |
| `AI_USER_KNOWLEDGE` / `AI_USER_KNOWLEDGE_EMBEDDINGS` | on / on | semantic user index / its OpenAI embeddings |
| `AI_LEARNER_HR_CONTEXT` / `AI_LEARNER_HR_GOALS` | on / on | HR learner context / kill switch for shared HR goals |
| `AI_HELP_KB` | on | platform help tool |
| `AI_EMBEDDING_MODEL` / `AI_EMBEDDING_DIMENSIONS` | text-embedding-3-small / 1024 | must match the built index |
| `AI_RAG_CROSS_ENCODER` / `AI_RERANK_MAX_CANDIDATES` / `AI_RERANK_AMBIGUITY_MARGIN` | auto / 4 / 0.08 | rerank gating |
| `AI_EMBED_SKIP_MARGIN` / `AI_DISABLE_EMBED_SKIP` | 1.75 / off | lexical short-circuit |
| `AI_SEARCH_CACHE_TTL`/`_MAX`, `AI_EMBEDDING_CACHE_TTL`/`_MAX` | 3600 / 1000, 21600 / 5000 | RAG caches |
| `AI_COST_MODEL_ENABLED` / `AI_USD_DKK` / `AI_CACHED_INPUT_DISCOUNT` | on / 7.0 / 0.5 (0.1 for `claude-*`) | cost model |
| `AI_CREDITS_PER_DKK` | 10 | credits metering |
| `AI_PROMPT_VARIANTS` / `AI_PROMPT_ADDENDUM_<V>` | v2.0 / - | prompt A/B |
| `AI_MEMORY_BACKEND` | auto | `sqlite` forces the dev fallback for the analytics store |
| `AI_WARMUP_ON_IMPORT` | on | warm RAG at import (set 0 in tests) |
| `AI_TOOLER2` | on | opt-out diagnostics label only; `ai_tooler2_enabled()` has no callers - tools are not gated on it |

## 10. Tests & eval - always use the safe env (never hit a real DB)

`tests/conftest.py` sets `SANDBOX=1`, `AI_WARMUP_ON_IMPORT=0`, `SCHEDULER_OPPORTUNISTIC=0`,
`AI_MEMORY_BACKEND=sqlite` and fails fast when no MySQL is reachable; `pytest.ini` sets
`testpaths=tests`, `timeout=120`. `run.py` has no production fallback: without
`MYSQL_*`/`DATABASE_URL` it points at localhost.

```bash
# Offline suite (no MySQL, no OpenAI, no network):
SANDBOX=1 MYSQL_HOST=127.0.0.1 MYSQL_PORT=3306 MYSQL_USER=none MYSQL_PASSWORD=none MYSQL_DB=none \
  OPENAI_API_KEY=sk-test python3 -m pytest tests/ -q
# Boot smoke (create_app does not connect; DB is lazy):
SANDBOX=1 AI_WARMUP_ON_IMPORT=0 MYSQL_HOST=127.0.0.1 MYSQL_USER=none MYSQL_PASSWORD=none MYSQL_DB=none \
  python3 -c "from run import create_app; create_app()"
```

- **AI-quality eval** (`ai_eval/`, see its README): drives the real endpoints against
  `golden_set.json` (employee, HR, vendor scopes); needs a live `OPENAI_API_KEY` + the Docker
  sandbox MySQL (`./sandbox/sandbox.sh up && init`, port 3307) - not offline. Nightly on both
  providers: `.github/workflows/ai-eval-nightly.yml`. Re-baseline once after an intentional
  quality shift: `python3 ai_eval/run_eval.py --set-baseline`.
- **Offline coverage by area:** runtime/provider `test_ai_runtime.py`, `test_ai_provider_toggle.py`,
  `test_ai_fallback_guard.py`; SSE pipeline `test_ask_sse_offline.py` (mocked LLM: no
  `<suggestions>` leak, one final completion, grounding disclaimer, `meta` before `[DONE]`);
  cross-surface handoff, forget tool, workspace summary `test_learner_ai_handoff.py`;
  registry/reachability `test_ai_tool_registry.py`, `test_ai_copilot_upgrade.py`,
  `test_tooler2_registry_grounding.py`, `test_prompt_tool_name_drift.py`; confirm/write tools
  `test_tool_confirm.py`, `test_hr_write_tools.py`, `test_hr_platform_tools.py`,
  `test_hr_blast_radius_tools.py`, `test_employee_action_tools.py`; platform empowerment
  (agenda, own compliance, HR navigation/page context, vendor catalog health, drift guards
  tool enums <-> `UI_ACTIONS`/`HR_UI_ACTIONS`, SSE events <-> `KNOWN_EVENT_TYPES` <-> chat.js)
  `test_platform_ai_empowerment.py`; CV `test_cv_ingest_apply.py`, `test_cv_parse_store.py`;
  `test_competency.py`; memory `test_memory_dedup.py`, `test_memory_routes.py`,
  `test_prune_summary_carryforward.py`; GDPR `test_gdpr_table_coverage.py`; eval scorers
  `test_eval_scorers.py`, `test_eval_fluency.py`, `test_tooler2_scorers.py`.
