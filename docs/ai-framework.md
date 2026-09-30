# AI Framework — app1 course-advisor & profiler

> **Purpose.** A durable, high-signal map of the app1 AI platform so a future
> agent can change it confidently **without re-reading the whole codebase**.
> Anchors are `file:line` (approximate — grep the symbol if it has drifted).
> Keep this file current when you change the AI surfaces.

---

## 1. The big picture: one engine, two "AIs"

aileadz / FutureMatch is a B2B, sales-led learning platform. The user-facing AI
is **one agentic chat engine** — OpenAI (gpt-4o / gpt-4o-mini) **or Claude**,
switched in admin (see `docs/AI_PROVIDER_TOGGLE.md`) — exposed as **two modes**
selected by a client-supplied `mode` string:

| Mode | "AI" | Shell route | Difference |
|------|------|-------------|------------|
| `default` | **Course suggester** | `/chat` (`futurematch_ui.py:25`) | the advisor/recommender |
| `profiler` | **AI Profiler** | `/ai-profiler` (`futurematch_ui.py:31`, login-gated) | mode core playbook (never trimmed) + need-driven profiler state, profiler few-shots, main model on every turn, `profiler_progress`, persisted handoff to course recommendations |

There is **one endpoint** (`POST /app1/ask`, `ask()` in `app1/__init__.py`),
**one frontend** (`static/futurematch/assets/chat.js`), and **one toolset**. The
mode is the only switch. Per-mode policy lives in `MODE_PROFILES`
(`app1/agent.py`, next to the system prompts) and is what `stream_generator`
reads: core playbook, which flow playbooks may be injected, few-shot set, stage
hints, memory limit, model preference and handoff thresholds.

`/app1/ask` also accepts `kind: "seed"` for UI-generated openers (the profiler's
Start/Fortsæt, `window.fmSendSeed`): the regex intent classifier is skipped
(intent `profiler_resume`), so an opener's wording can never pull in a playbook.

---

## 2. Request → tool → SSE lifecycle

```
chat.js run() ─POST {query,mode}─▶ /app1/ask (ask() at app1/__init__.py:932)
   └▶ handle_agentic_ask (app1/agent.py:1298)
        ├─ resolve the surface's session id (conversation_state.resolve_sid);
        │   rebuild CHAT_MEMORY from conversation_history when missing or stale (rev)
        ├─ _classify_intent_local (agent.py:594)  [+ gpt-4o-mini router only on 'discovery']
        ├─ _detect_conversation_stage (agent.py:341) → reconcile with intent
        ├─ get_employee_tool_selection (ai_tool_registry.py:803) → per-turn tool menu
        └─ stream_generator() (agent.py)
             ├─ build tagged context layers (profile, memories, digests, recall, HR,
             │   company rules, playbooks …) — fitted to the budget at prepare time
             ├─ run_agent_with_fallback (ai_runtime.py) → model loop, tool_choice=auto
             │     └─ execute_tool (app1/tools.py:5237) — flat if/elif dispatch
             ├─ map each tool result → SSE events (the big loop, agent.py ~2181–2400)
             ├─ stream final answer tokens (<suggestions> parsed out)
             ├─ grounding circuit-breaker (post-stream disclaimer)
             └─ emit cards / profile events / cross-surface events / suggestions
```

**Tooling state** is passed via module globals set per-turn:
`set_search_context(...)` (`tools.py`) injects shown-handles, prefs, blocked
vendors, supplier agreements before the model loop.

---

## 2b. Context assembly — `ai_context_layers.py`

**Why:** every dynamic system message used to be merged into one
`[SESSION KONTEKST]` blob and cut to **1800 chars** on every provider path. The
few-shot + stage playbooks came first, so the profile, memories, the profiler
playbook and company rules almost never reached the model — the real cause of
"the profiler doesn't continue from my profile".

**How:** builders emit tagged layers — `_ctx.layer(name, body, header=…, fence=…)`
— and `ai_runtime.prepare_messages_for_turn` assembles them. Untagged system
messages count as `legacy` (the HR agent works unchanged).

| layer | prio | cap | zone | source |
|---|---|---|---|---|
| `mode_core_playbook` | 0 (never dropped) | 4500 | steering | `MODE_PROFILES[mode]["core_playbook"]` |
| `company_rules` | 5 | 2500 | knowledge | `_company_context_layers` (5-min cache) |
| `profile` | 10 | 4500 | knowledge | `format_profile_for_ai(include_ids=True)` |
| `guidance` / `turn_hint` | 12 / 13 | 1500 / 600 | steering | intent, prefs, completed-course dedup |
| `profiler_state` | 14 | 1500 | steering | `_build_profiler_state` |
| `employee_info` / `learning_context` | 15 / 16 | — | knowledge | employee row, supplier agreements |
| `memories` | 20 | 2000 | knowledge | `_select_memories_for_turn` |
| `session_summary` | 22 | 2000 | knowledge | in-session pruning summary |
| `flow_playbooks` | 25 | 3000 | steering | `_build_playbook_messages(stage, intent, mode, query)` |
| `hr_learning` | 30 | 1800 | knowledge | `learner_context` |
| `mode_digest` | 32 | 1500 | knowledge | `user_conversation_summaries` |
| `shown_products` / `rejections` | 35 / 38 | — | steering | session caches |
| `recall` | 40 | 2000 | knowledge | `user_knowledge.search(types=["conversation"])` |
| `returning_note` / `smart_context` / `other_mode_digest` | 45 / 50 / 52 | — | mixed | |
| `legacy` / `few_shot` | 55 / 60 | 4000 / 1200 | steering | |

Over budget: priority ≥30 shrink to floor → dropped → 1–29 shrink to floor →
≥15 dropped. Bodies are trimmed **before** they are fenced, so an untrusted-data
fence is never cut open. Output: `[static, knowledge, steering, *history]`.
Budget = `min(AI_CONTEXT_MAX_TOKENS, 40% of input budget − tool schemas)`; tool
schemas are counted (`estimate_tools_tokens`). `compact_messages_for_api` never
re-cuts a budgeted layer and drops old history before context.

**Placement:** OpenAI — steering moves just before the last user message
(`AI_STEERING_PLACEMENT=trailing|leading`), private keys stripped. Anthropic —
`prepare_messages_for_turn(keep_zones=True)`; `_build_kwargs` keeps knowledge in
`system` behind a second cache breakpoint and trails steering as a
`role:"system"` message on models that support it (`system[2]` otherwise).
The per-turn report is logged as `context_assembly`. Rollback:
`AI_CONTEXT_ASSEMBLER=0` (legacy merge + flat cut, fences still rendered).

---

## 3. SSE event vocabulary — **single source of truth: `app1/sse_events.py`**

Producers (`app1/agent.py` et al.) and the consumer (`chat.js` dispatch, ~line
1219) MUST agree on these `type` strings. `KNOWN_EVENT_TYPES` in
`app1/sse_events.py` is the canonical set; the drift test guards it.

| Event | Producer | chat.js handler | Payload |
|-------|----------|-----------------|---------|
| `ping` | heartbeat | skipped | — |
| `meta` | agent | stores `message_index` (feedback) | `message_index` |
| `thinking` | agent (env-gated) | `thinkStatus` (status line) | `content` |
| `chunk` | agent | appends to answer, markdown re-render | `content` |
| `tool_call` | `ai_runtime.build_tool_call_event` | `renderToolCall` (chips) | label/category/status/results_count/latency/side_effect/… |
| `tool_progress` | runtime tool lifecycle | `updateToolProgress` | percent/note |
| `course_cards` | agent | `addCourses` (native cards) | `items[]` (each card may carry **`why`**) |
| `product` | agent | `injectProductHtml` (legacy HTML, only if no course_cards) | `html` |
| **`comparison_card`** | agent (new) | `renderComparisonCard` | `comparison[]`, `analysis{winners,verdict}` |
| **`learning_path_card`** | agent (new) | `renderLearningPathCard` | `path{title,steps[],total_cost,total_duration_days,id}` |
| **`ui_action`** | agent (new) | `renderActionCard` | `action,target,label,handle?,handles?,section?` |
| `suggestions` | agent (`<suggestions>` tag, or server fallback) | `addChips` | `items[]` |
| `notice` | agent | italic note | `content` |
| `profile_update` | agent | markdown note | `message` |
| `profile_confirm_request` | agent (update_user_profile proposed) | `profileConfirm` (.pcard) | `confirm{action,data}` |
| `ui_card` | agent (request_user_input) | `uiCard` (form) | fields/prefilled/save_action |
| `memory_used` | agent | `renderMemoryUsed` (per-chip delete) | `memories[]` |
| `memory_saved` | agent (remember_about_user) | `renderMemorySaved` (inline delete via **`id`**) | `label,category,id` |
| `profiler_progress` | agent (profiler mode) | `window.onProfilerProgress` (ring) | `completeness{}` |
| `confirm_card` | agent (needs_confirmation tools) | `renderConfirmCard` | opaque `token`, summary, price |
| **`cv_summary_card`** | agent (`show_cv_summary`) | `renderCvSummaryCard` | `sections{skills[],experience[],…}`, `counts{}`, `total`, `has_cv`, `focus` |
| **`mindmap_card`** | agent (`show_mindmap_preview`) | `renderMindmapCard` | `completeness{}`, `categories{}`, `counts{}`, `recent_memories[]` |
| **`skill_gaps_card`** | agent (`show_skill_gaps`) | `renderSkillGapsCard` | `gaps[]` (each `{skill,category,current_level,current_label,target_level,target_label,gap,source,priority}`), `target_role`, `has_gaps`, `reason` |
| **`agenda_card`** | agent (`get_my_agenda`) | `renderAgendaCard` | `items[]` (each `{kind,title,handle,order_id,date,days_left,overdue,detail}`), `count`, `urgent_count`, `horizon_days` |
| **`compliance_card`** | agent (`get_my_compliance`) | `renderComplianceCard` | `requirements[]` (each `{title,category,is_statutory,state,expires_on,days_left}`), `has_requirements`, `action_needed`, `is_compliant` |
| `[DONE]` | terminal | end-of-turn | — |

**Guidance guarantee (new):** a turn never dead-ends — if the model omits
`<suggestions>`, the server synthesises context-aware chips
(`_fallback_suggestions`, `agent.py`), and chat.js has a final client-side net.

---

## 4. Tools & the registry

- **Definitions:** `OPENAI_TOOLS` (`tools.py:451`+, anonymous-safe) and
  `PROFILE_TOOLS` (`tools.py:2546`+, login-only). **Dispatch:** flat if/elif in
  `execute_tool` (`tools.py:5237`).
- **Per-turn menu:** `get_employee_tool_selection` (`ai_tool_registry.py:803`).
  `catalog_search` + (logged-in) profile tools + **`open_in_app`** are an
  always-on, model-driven core; specialised/mutating tools are added by Danish
  keyword gates; at most one is force-chosen (`_resolve_forced_tool`).
  **Exception:** In `profiler` mode, all 16 profile/gap/recommendation tools
  are seeded unconditionally and the `chit_chat` fast-path is disabled, so
  the profiler can always save data and steer without needing keyword triggers.
- **⚠️ Adding a tool is THREE steps, not one.** A tool is only callable if its
  name is added to the `names` set inside `get_employee_tool_selection` (via the
  core seed, a keyword gate, or a `_TOOL_TRIGGERS` semantic-fallback entry). The
  menu is built **only** from `names` — so a tool that is defined in
  `OPENAI_TOOLS`/`PROFILE_TOOLS` **and** dispatched in `execute_tool` but never
  added to `names` is **dead**: the model can never select it (no error, just
  silence). Checklist for a new tool: (1) schema in `OPENAI_TOOLS`/`PROFILE_TOOLS`,
  (2) executor + `execute_tool` branch, (3) reach the menu in
  `get_employee_tool_selection` + register `_EMPLOYEE_META`/`_TOOL_LABELS`. Add a
  reachability test (see `test_cv_summary_reachable_on_profile_query`).
- **Reachability fallback (new):** `_semantic_tool_fallback`
  (`ai_tool_registry.py`) token-overlaps the query against `_TOOL_TRIGGERS`
  (Danish + English synonyms) and additively surfaces the best specialised tool
  for paraphrased / English / typo'd queries the exact-keyword gates miss.
  Bounded to ≤2, env-gated by `AI_TOOL_SEMANTIC_FALLBACK` (default on).
- **HR and vendor tools follow the same rule with different menus:** HR goes
  through `get_hr_tool_selection` (core seed + keyword gates + `_HR_PAGE_TOOLS`
  page hints) and `_HR_META`; the vendor path has no selector — `VENDOR_TOOLS`
  is offered whole — so there a tool is live as soon as it is in the list and
  the router, and the system prompt is what teaches the model to use it.
- **Metadata/labels:** `_EMPLOYEE_META` + `_TOOL_LABELS`
  (`ai_tool_registry.py`). chat.js has a parallel `TOOL_LABELS` map; the
  backend-supplied `label` wins, with a `_humanize` fallback.

### Key tool behaviours (post-upgrade)

- **Budget filtering** uses the **cheapest bookable variant**
  (`_min_variant_price` / `_price_in_budget`, `tools.py`) — not `variants[0]`.
  Both filter paths (`_filter_products_by_constraints` for `filter_courses`,
  `_product_passes_hard_filters`/`_apply_hard_filters` for `catalog_search`).
- **Language / difficulty facets** honour `structured_metadata.language`
  (`dansk|engelsk|begge`) and `.difficulty` (`beginner|intermediate|advanced`);
  unknown metadata is never excluded. Aliases in `_LANGUAGE_ALIASES` /
  `_DIFFICULTY_ALIASES`.
- **Per-card WHY**: search/filter/recommend executors attach a verifiable
  `match_reason` (`_course_match_reason`, derived from matched query/profile
  terms + concrete attributes — never an LLM guess). It's threaded to the card
  via `serialize_course_cards(reasons=...)` → `card.why` → chat.js `c.why`.
- **`compare_courses`** is analytical: `_comparison_analysis` computes per-axis
  winners (cheapest / shortest / certification / soonest) + a verdict, rendered
  as a `comparison_card`.
- **`recommend_for_profile`** anchors the query on `target_role` + low-level
  skills + goals (per-gap) and sets `match_reason`.
- **`suggest_learning_path`** grounds each step in real courses, de-dups across
  steps, skips completed, rolls up cost/duration, and **persists** via
  `save_learning_path`. Emitted as `learning_path_card`.
- **`get_learning_context`** now actually returns profile + company budget +
  supplier agreements + completed courses (it previously dropped them).
- **`open_in_app`** (always-on, no mutation) → `ui_action` SSE directive the
  SPA acts on. Actions: `view_product` / `open_compare` / `open_profile` /
  `open_catalog` / `open_mind_map` / `open_learning_path` / `start_order` /
  `open_profiler` / **`open_cv_upload`** (new — navigates to `/profil-upload`,
  the 3D drag-drop CV portal). Enumerated set lives in `sse_events.UI_ACTIONS`.
- **`show_cv_summary`** (new, profile-gated) — reads the user's saved profile
  sections and emits a `cv_summary_card` in chat showing skills/experience/
  education/certifications/languages counts + preview chips + "Upload CV" CTA
  → `/profil-upload`. Reaches the menu on profile/CV keywords + the semantic
  fallback (`_TOOL_TRIGGERS`); guarded by
  `test_cv_summary_reachable_on_profile_query`.
- **`show_mindmap_preview`** (new, profile-gated) — reads profile completeness,
  per-category node counts, and 3 recent memories; emits a `mindmap_card` with
  a progress bar + "Åbn 3D Mind-Map" link → `/mind-map`. Reaches the menu on
  mind-map / "hvad husker du om mig" keywords + semantic fallback; guarded by
  `test_mindmap_preview_reachable_on_memory_query`.
- **`show_skill_gaps`** (new, profile-gated) — the grounded bridge to
  recommendations. Calls `competency.compute_skill_gaps(username)` (current vs
  target on the canonical 1-5 scale, from `company_skill_targets` + `target_role`
  + learning goals) and emits a `skill_gaps_card` (current→target bars) whose
  CTA asks for gap-closing courses. The result JSON IS the data the model reasons
  over, so it can chain `show_skill_gaps → recommend_for_profile`. Reaches the
  menu on "hvad mangler jeg / hvilke kompetencer / what should I learn" keywords
  + semantic fallback; guarded by `test_skill_gaps_reachable_on_gap_query`.
- **Gap-grounded recommendations (new):** `recommend_for_profile` and
  `suggest_learning_path` now seed their query/plan from `compute_skill_gaps`
  (not "skills rated low"); a recommendation's `match_reason` is the verifiable
  gap it closes ("lukker dit gap i X (mellem→avanceret)") when the course
  actually mentions the gap skill — never an LLM guess.
- **Discoverability of the 3D surfaces** (non-chat): both `/profil-upload` and
  `/mind-map` are in the employee sidebar (`fm_base.html`, `page_id` drives the
  active state) and linked from the profile hero (`my_profile.html`); CV upload
  is also on `employee_home.html`. The AI reaches them via the inline cards
  above and `open_in_app(open_cv_upload|open_mind_map)`.
- **`save_learning_path` / `get_learning_path`** persist & recall paths.
- **`get_my_agenda`** (new, profile-gated) — the cross-silo "hvad har jeg på
  tavlen?" read. Before it, the learner's own commitments lived in four places
  the advisor could only reach one at a time (or not at all): course deadlines
  and pending manager approvals (`course_orders` + `order_approvals`), expiring
  certifications (`user_certifications`, parsed with
  **`cert_expiry_service.parse_expiry`** so chat and the reminder job agree on
  partial dates), and dated learning goals. Items are sorted worst-first
  (overdue → approvals → soonest) inside a clamped 7-365 day horizon, and every
  source degrades independently — a missing `order_approvals` table falls back
  to a plain `course_orders` read rather than losing the deadlines too. Emitted
  as an `agenda_card`.
- **`get_my_compliance`** (new, profile + company-gated) — the learner-side
  mirror of HR's compliance matrix: which mandatory/statutory requirements apply
  to *them*, which are met, missing, overdue or due for renewal. It does NOT
  fork the semantics: `derive_company_compliance`'s closures were extracted into
  the shared primitives `compliance_requirement_applies` /
  `compliance_completion_matches` / `compliance_state_for_entries`
  (`hr_tools.py`), and both the company matrix and the new
  `derive_employee_compliance` run on them — so a learner and their manager can
  never be told two different things about the same person. Returns only this
  user's own rows. Emitted as a `compliance_card`; the model is told to chase a
  missing requirement with a real course.
- **`open_in_app`** also reaches the learner's own surfaces now:
  `open_my_learning` (`/min-laering`), `open_goals` (`/mine-maal`) and
  `open_timeline` (`/min-tidslinje`). The executor validates against
  `sse_events.UI_ACTIONS` rather than a hand-copied set, and a test pins the
  tool's enum to that list in both directions.

---

## 5. Profile / completeness / learning paths

- **Store:** per-user MySQL tables in `app1/user_profile_db.py` (skills,
  experience, education, completed_courses, **summary** (+`target_role`),
  certifications, languages, portfolio_links, memories, learning_goals, and the
  new **`user_learning_paths`**). `ensure_tables()` runs idempotent
  CREATE/ALTER migrations every boot.
- **Competency layer — `competency.py` (the "B" in A→C):** the single home for
  turning free-text skills into a clean signal. `canonical_skill()` (alias map +
  acronyms; "js"→JavaScript, "python3"→Python) + `skill_category()` run on every
  skill write (`add_skill`, `/api/cv/apply`, chat `update_user_profile`), so
  storage is deduped + categorized (`user_skills.category` column). The canonical
  **1-5 scale is REUSED from `hr_tools.SKILL_LEVEL_MAP`** (begynder=1…ekspert=5),
  never forked — so employee self-reports and HR targets share one scale.
  `compute_skill_gaps(username)` diffs current skills against required ones
  (`company_skill_targets` + `target_role` via `ROLE_SKILL_HINTS` + goals) and is
  the grounding source for `show_skill_gaps` / `recommend_for_profile` /
  `suggest_learning_path`. Fully guarded + offline-safe. REST:
  `GET /api/profile/skill-gaps`.
- **Completeness — one source of truth:** `profile_completeness(username,
  profile=None)` (`user_profile_db.py:846`). The 8-section binary
  `pct`/`total`/`missing` contract is unchanged (tests depend on it); it now
  ALSO returns depth-aware `weighted_pct`, per-section `strength`, `weakest`
  (the profiler's next-best-question target), and `target_role`. Consumed by:
  `/api/profile/completeness` (api.py), `my_profile.html` `renderCompleteness`,
  `futurematch_ui._home_skill_completeness`, the profiler ring, and the
  mind-map — no per-surface divergence.
- **`target_role`** is the career-direction field that anchors gap reasoning;
  edited on the profile page (`editTargetRole`) or by the profiler via
  `update_user_profile`.
- **Profiler → suggester handoff:** fires when `weighted_pct ≥
  AI_PROFILER_HANDOFF_PCT` (70) **or** a target role is set and `weighted_pct ≥
  40` (`MODE_PROFILES["profiler"]["handoff"]`): `recommend_for_profile` →
  `course_cards` + CTA. Persisted in `conversation_history.state_json.handoff`
  (survives worker hops); marked fired only when courses were actually shown,
  otherwise retried up to 2 attempts.
- **Proactive profiler:** `ai_profiler.html` auto-sends a neutral **seed**
  ("Start profilsamtalen" / "Fortsæt profilsamtalen", `kind: "seed"`) once per
  browser session on an empty thread. The old section-scripted seeds matched
  `_PROFILE_UPDATE_PATTERNS`, injected the CV-onboarding playbook and produced
  the generic "hvad laver du til daglig?" opener.
- **Need-driven profiler context:** the profile layer (with `[#id]`s) is what the
  model knows; `_build_profiler_state` adds depth, the target role and ≤3
  unknowns that would sharpen the advice *with the reason each matters*
  (sections with strength ≥ 0.5 are never listed). No imperatives — a guard test
  fails on "DIT NÆSTE SPØRGSMÅL / SKAL VÆRE OM / SPØRG ALDRIG / ALLEREDE AFDÆKKET".
- **Mode-aware playbooks:** `SYSTEM_PLAYBOOK_PROFILE_SAVE` (save rules, both
  modes) is split from `SYSTEM_PLAYBOOK_CV_ONBOARDING` (advisor only); the
  profiler gets the search playbook only on an explicit course request.
- **One profile fetch per turn:** `get_full_profile` runs once in
  `stream_generator` and is shared with `profile_completeness(profile=…)`,
  `compute_skill_gaps(profile=…)`, preferences, memory ranking and the index.
- **Memories:** ranked per turn by the semantic index (`_select_memories_for_turn`,
  keyword fallback); only memories that actually matched are `_relevant`, so
  `used_count` means "informed an answer" in both modes.
- **Surfaces never share a conversation:** each surface has its own session id
  and active pointer (§6b), so there is no mode-mismatch reset in chat.js.

### 3D surfaces — CV portal & Mind-Map

Two Three.js pages render the profile data as interactive 3D experiences. They
are reached from the AI (cards + `open_in_app`), the employee sidebar, the
profile hero, and (CV) `employee_home`.

- **CV portal** (`/profil-upload` → `templates/fm/cv_upload.html`, Three.js +
  tween.js via ESM importmap). Has its **own SSE pipeline, separate from the
  chat vocabulary in §3** — built in `api.py`:
  - `POST /api/cv/parse` (multipart) — validates type/size (≤8 MB, whitelisted
    exts), extracts text (`cv_ingest.extract_text`, PDF/text/**image-OCR**, now
    with OCR timeouts + a `delimit_untrusted` prompt-injection fence) and runs
    the LLM parse in a **background thread that pushes its own `app_context`**
    (a raw daemon thread has no `current_app`), storing the result via
    **`cv_parse_store`**.
  - **`cv_parse_store.py` (durable, cross-worker)** replaces the old
    process-local `_cv_parse_results` dict, which was broken under multiple
    gunicorn workers (parse thread on worker B, SSE poller on worker A → never
    saw the result) and leaked. Mirrors `confirm_store`: MySQL `ai_cv_parse_jobs`
    + in-process fallback + TTL sweep. `start`/`finish`/`read`/`discard`.
  - `GET /api/cv/parse-stream` (SSE) — stage labels are driven by persisted
    extraction/parsing lifecycle updates and **completion is driven off the real `cv_parse_store.read_state`** result, then
    emits one terminal `result` (`{proposal, hint}`) or `error`. Event names here
    are `stage` / `result` / `error` — NOT the chat `type` strings.
  - `POST /api/cv/apply` (`api.py:598`, JSON `{session_id, accepted:[…]}`) —
    writes approved items via `add_skill/experience/education/certification/
    language`. **Level vocab must be normalised** through `_SKILL_LEVEL_MAP` /
    `_LANG_PROF_MAP` (lowercase-keyed, case-insensitive) — they accept BOTH the
    portal's display labels (Begynder/Øvet/…) and the parser's canonical
    lowercase output (begynder/mellem/…); the old capitalized-only map silently
    inflated every parsed skill to `avanceret`.
    It now preserves summary and full experience dates/descriptions, supports
    merge/replace/keep conflict policies, and returns per-item outcomes plus a
    CV→career action summary.
  - `POST /api/cv/improve` — a section-scoped, non-destructive CV coach. It can
    sharpen summary/experience language but is forbidden to invent evidence;
    missing evidence is returned as questions and suggestions require explicit accept.
  - Empty proposal → the portal resets to upload state and shows the `hint`
    toast rather than entering a blank 0-card review.
  - **No-JS fallback:** the `<form>` posts to `/profil-upload` +
    `/profil-upload/apply` (`futurematch_ui.py`), a self-contained server-render
    path (its own whitelist level validation, defaults to `mellem`).
- **Mind-Map v2** (`/mind-map` → `templates/fm/mind_map.html`, see
  **[docs/MIND_MAP_V2.md](MIND_MAP_V2.md)**): a DCLogic React runtime
  (`static/futurematch/assets/mind-map-support.js`) + Three.js globe,
  fed by `GET /api/profile/mindmap` (`api.py:758`, root→category→leaf graph from
  structured profile + `user_memories` + conversation summary). DC template
  bindings use `{{ }}`, so the block is wrapped in `{% raw %}`. **Gotcha:** never
  write a bracketed `x-dc` open tag before the real element (even in a CSS
  comment) — `parseDcText` regex-matches the FIRST one in the raw source.
  - **v2 = navigation + immersion.** Full keyboard traversal of the real tree
    (↑↓ siblings, ←→ parent/child, Home, Enter isolates a branch, Esc unwinds),
    search that *jumps* (a `3 / 12` stepper, Enter/N/P fly to each hit),
    a breadcrumb + sibling stepper + child navigator inside the inspector,
    branch focus mode, deep-linkable selection (`#n=<id>`), a top-down radar,
    and camera framing that keeps the focused node clear of the panel. The
    scene answers back: hover/selection states in 3D, the ancestor path lit
    end-to-end via per-edge vertex colours, screen-space label placement with
    overlap rejection, and `prefers-reduced-motion` / hidden-tab respect.
    Structural invariants are pinned by `tests/test_mind_map_v2.py`.
  - **Type-aware inspector panel:** clicking a node opens a side panel that
    renders **per category/type** rather than generically — skills show the 1-5
    level bar + skill category + a **gap callout** (current→target, from
    `compute_skill_gaps`, with a "find courses" CTA); experience shows
    period/duration/employer; certs a validity badge (Gyldig/Udløber snart/
    Udløbet) + verify link; languages a proficiency meter; goals a status badge;
    branches a category-specific **aggregate** (skills-by-level + gap count,
    cert validity counts, total years, …); the root a profile overview
    (completeness/depth/target-role/weakest + profiler/CV/add-memory actions).
    Each leaf's `meta` is enriched server-side in `get_mindmap_api`
    (`level_score`, `gap`, exp dates, cert dates, goal target/status, …).
  - Memory CRUD from the page: DELETE `/api/profile/memories` `{id}` (confirm
    first), POST `{label,detail,category,source}` from an inline composer whose
    category chips mirror `_MEMORY_CATEGORIES` — a test asserts they can't drift.
    Structured profile leaves carry stable entity metadata; users can jump to
    the canonical inline editor or remove supported facts directly. Portfolio
    links, completed courses, and saved learning paths are included in the graph.
    A non-2xx from the mindmap API shows a retry card (plus an explicit "show
    demo data"), never a fake-empty profile.

---

## 6. Trust spine (grounding · confirm · memory)

- **Grounding** (`grounding.py`): post-stream chain-of-custody check appends a
  disclaimer when the answer asserts a price/date/title not in this turn's tool
  results. Price matching is **token-boundary + cents-aware**
  (`_canon_amount` / `_evidence_amounts`) — a claimed `5000` is no longer
  "supported" by an evidence `15000`. `AI_GROUNDING_RECALL` (default off) is the
  optional pre-stream corrective re-call.
- **Confirm** (`app1/confirm_store.py`): side-effect tools return
  `needs_confirmation`; the agent stores args server-side and emits a
  `confirm_card` with an **opaque token**. The store is now **MySQL-backed
  (`ai_confirm_tokens`) with an in-process fallback**, so a token minted on one
  gunicorn worker is resolvable on another (fixes silent multi-worker mutation
  loss). Tokens are session-bound; pop is single-use.
- **Memory** (`user_profile_db.py` `user_memories`): `remember_about_user`
  stores free-form facts with near-duplicate supersede — token-based and
  category-scoped (a 4-char substring rule used to let "Java" overwrite a
  JavaScript memory). `memory_saved` carries the row `id` so chat.js renders an
  inline "Forkert / slet" affordance.
- **Tool errors** never carry raw exception text to the model
  (`tools._internal_tool_error`: Danish message + `error_code`; traceback in logs).

---

## 6b. Conversation state & knowledge layer

**Conversation state — `app1/conversation_state.py`.** Every conversation is a
`conversation_history` row keyed by `(username, session_id)` with a `rev`
counter; `user_conversations` is legacy (read-only, still in GDPR).
- `resolve_sid(session, mode)` → `session["session_ids"][surface]`
  (`chat` | `profiler`); `session["session_id"]` mirrors the last used one.
- `load(username, sid)` reads exactly that row — **no "latest" fallback** on
  `/ask`, which is how a new chat used to inherit the previous transcript.
- `save_turn(..., expected_rev=CHAT_MEMORY_REV[sid])` updates with
  `WHERE rev = %s`; a stale worker merges (`merge_transcripts`) instead of
  overwriting. At turn start a worker whose cached rev differs rebuilds.
- **Opening a surface starts a new chat** (like every mainstream AI chat):
  `chat.js bootChat()` calls `newChat()` → `/new_session {mode}`, which saves +
  digests the old session and points the surface at a fresh id — **nothing is
  deleted**. Past conversations live in the sidebar; the open one is pinned in the
  URL (`?c=<id>`), so a reload reopens it via `/conversations/<id>/resume`.
  Continuity comes from the profile, memories and digests, not the transcript.
  `user_active_sessions (username, mode)` + `/load_conversation` remain as an API
  (no longer used by the UI on boot).
- Confirm tokens are tried against every session id the browser holds.
- The widget runs with `sid_override` + the embedding company
  (`company_override`) and never touches the employee session.

**Cross-session memory.** `ai_context.summarize_session` (fast model,
`AI_SESSION_SUMMARY_MODE=llm|rules`) writes a per-session summary
(`conversation_history.summary`) and merges it into the surface digest
`user_conversation_summaries (username, mode)`, via
`conversation_state.digest_session_async` on new chat / resume, with a lazy
catch-up (`latest_undigested`) on the next new session. Injected as
`mode_digest`; the other surface's digest as `other_mode_digest`.

**Semantic index — `app1/user_knowledge.py`.** `user_knowledge` rows
(memory / profile_fact / conversation) with normalised float32 embeddings
(`rag.embed_texts`, same model as the catalogue; keyword fallback offline).
`sync_user` diffs content hashes (throttled 300 s, forced after a profile write),
`search` scores 0.8·cosine + 0.2·keyword. Consumers: memory ranking, the
`recall` layer and the **`recall_about_user`** tool. Embeddings always go to
OpenAI even when the chat runs on Claude; `AI_USER_KNOWLEDGE_EMBEDDINGS=0` keeps
it keyword-only.

**HR learner context — `learner_context.py`.** The learner's own assigned
paths/progress, HR skill-matrix gaps, department targets and HR-written goals,
each source degrading independently; 120 s cache. `employee_skills_matrix` /
`employee_goals` are keyed on `users.id` (= `company_users.user_id`).

*HR-written goals are opt-in per goal (S-4.4).* Only goals HR toggled **"Del med
medarbejder"** (`employee_goals.shared_with_employee = 1`) are ever read here; the
predicate lives in `goal_sharing.SHARED_PREDICATE` and every learner-facing query
carries it (a test scans the repo for unfiltered reads). On top of that sits a
company-level switch, `company_settings.ai_learner_hr_goals` (default on), and
`AI_LEARNER_HR_GOALS=0` as a platform-wide kill switch. Unshared goals never reach
the learner UI, the AI context or the learner's own data export.

**Platform help — `app1/help_kb.py` + `app1/help_kb/*.md`.** 13 curated Danish
articles, each URL pinned by a drift test. **`search_platform_help`** reaches the
menu only on a how/where phrase **and** a platform noun
(`looks_like_platform_help`). Optional embedding index:
`python -m app1.help_kb --build`.

---

## 7. RAG (course suggester)

`app1/rag.py`: offline enrich+embed build (`build_index.py`), hybrid BM25+vector
retrieval with RRF fusion + cross-encoder gate
(`semantic_search_courses_detailed`), and profile-conditioned re-rank
(`hybrid_rank_products`, accepting a `profile_boost` of target_terms /
completed). Tool JSON is re-resolved to full products by handle
(`resolve_products_for_ui`, `tools.py`) and serialised to cards
(`serialize_course_card[s]`, `app1/__init__.py`).

---

## 7b. The other two chat surfaces (HR advisor · vendor assistant)

The employee advisor is not the only chatbot. Two more run on the same
`ai_runtime` loop with their own toolsets, and both were recently given reach
beyond "answering in text".

**HR advisor** (`hr_agent.py`, `POST /hr/chatbot/ask`, panel
`templates/fm/_ai_panel.html` auto-included from `fm/_hr_subnav.html` → every HR
page). Its SSE vocabulary is its own and much smaller than §3: `ping`,
`thinking`, `text`, `tool_call`, `confirm_card`, **`ui_action`**, `error`,
`done`.

- **`hr_open_in_app`** (new, read-only, always on the menu) — the HR mirror of
  `open_in_app`. The advisor sat on 24 HR pages but could only ever *name* them
  ("det ligger på compliance-siden"); now it can open them. Destinations are the
  canonical `active_hr_page` ids from **`sse_events.HR_DESTINATIONS`** — the same
  vocabulary `_hr_subnav.html` uses — and the URL is resolved server-side with
  `url_for(endpoint)` (literal path as boot-safe fallback), so a renamed route
  fails in one place instead of shipping a dead link. It also reaches
  `view_product` / `open_catalog`. `hr_agent` emits the result as a `ui_action`
  frame; the panel renders it as an `<a class="fm-aip-action">` and drops
  anything that is not a same-origin absolute path.
- **Page context now reaches the server.** The panel had always posted `page`
  (the `active_hr_page` id) and the route had always dropped it — the only
  page-awareness was a client-side Danish prefix on the question. `page` is now
  whitelisted against `HR_DESTINATIONS` in `hr_chatbot_ask`, passed to
  `handle_hr_ask(..., page=…)` → an `AKTUEL SIDE:` context line, and to
  `get_hr_tool_selection(..., page=…)` → `_HR_PAGE_TOOLS`, which additively
  surfaces that view's tools (a test pins the map to every HR destination). So
  "hvem mangler her?" on the compliance page reaches the compliance tools
  without the manager naming the domain. Additive only: the keyword gates and
  `_resolve_forced_tool`'s TR-01 demotion are unchanged — and **reads only**:
  `_hr_page_tool_names` filters out any `side_effect` tool, because standing on
  the approvals page says what the manager is looking at, not that they intend
  to approve. Writes stay behind their explicit keyword gate + confirm card.

**Vendor assistant** (`vendor_portal.py` `POST /vendor/ask`, tools in
`vendor_tools.py`). Every turn is offered the whole (small) `VENDOR_TOOLS` list —
there is no per-turn selector — and executed with the SESSION vendor name, so a
vendor can never reach another vendor's or any buyer's data.

- **`vendor_catalog_health`** (new, read-only) — an actionable audit of the
  vendor's OWN listings: missing price, no bookable dates left, thin
  description, missing category / difficulty / language / duration / image.
  These are exactly the fields the platform's filters, search and AI
  recommendations read, so each finding is lost visibility rather than
  cosmetics. Findings are weighted (unpriceable/unbookable outrank a thin
  description) into a per-course `severity` and a catalog-wide `health_score`
  over (course × check) cells. Public catalog data only — no order, buyer or
  competitor data — and an unparseable variant date is never counted as expired.
  **Honesty rule:** difficulty / language / duration come from
  `structured_metadata`, which is an LLM enrichment pass over the vendor's own
  description (`app1/build_index.py:extract_structured_metadata`), NOT a field a
  vendor fills in. If not one of the vendor's courses carries it, that pass has
  not run for this catalog, so the three derived checks are skipped, the score's
  denominator shrinks to `checks_applied`, and `enrichment_missing` + a Danish
  note say so — rather than reporting "niveau mangler" on every course and
  blaming the vendor for our build. (On the live catalog this is the difference
  between "483 of 483 courses incomplete, score 62" and the truthful "24 of 483,
  score 99 — 14 unpriced, 6 unbookable".)

---

## 8. Env flags

| Flag | Default | Effect |
|------|---------|--------|
| `AI_TOOL_SEMANTIC_FALLBACK` | on | paraphrase/English tool reachability fallback |
| `AI_PROFILER_HANDOFF_PCT` | 70 | weighted-completeness threshold for the profiler→suggester handoff |
| `AI_SEARCH_HARD_FILTERS` | on | carry hard filters into RAG fallback + progressive relaxation |
| `AI_FILTER_PAST_DATES` | on | drop expired variant dates |
| `AI_GROUNDING_RECALL` | off | pre-stream corrective re-generation on a grounding violation |
| `AI_LIVE_TOOL_EVENTS` | on | stream tool start/finish chips live from a worker thread |
| `AI_CONTEXT_ASSEMBLER` | on | priority/budget context assembly (0 = legacy flat 1800-char cut) |
| `AI_CONTEXT_MAX_TOKENS` | 10000 | ceiling for the dynamic context layers |
| `AI_STEERING_PLACEMENT` | trailing | OpenAI: steering before the last user turn (`leading` = old layout) |
| `AI_MAX_INPUT_TOKENS` / `AI_TPM_BUDGET` | 36000 / 42000 | input budget / soft TPM ceiling — **set in the deploy env too** |
| `AI_TOKEN_CHARS_PER_TOKEN` | 4.0 (examples 3.5) | token estimator divisor (Danish tokenizes worse) |
| `AI_SESSION_SUMMARY_MODE` | llm | per-session digests: `llm` (fast tier) or `rules` |
| `AI_USER_KNOWLEDGE` / `AI_USER_KNOWLEDGE_EMBEDDINGS` | on / on | semantic user index / its OpenAI embeddings |
| `AI_LEARNER_HR_CONTEXT` / `AI_LEARNER_HR_GOALS` | on / on | HR learner context / kill switch for SHARED HR goals (per-goal sharing + a company setting decide what is actually read) |
| `AI_HELP_KB` | on | platform help tool |

---

## 9. Tests & eval — **always use the safe env (never hit prod DB)**

`run.py` defaults `MYSQL_HOST` to the production PythonAnywhere DB when unset,
and there is no `conftest.py`/`pytest.ini`, so the safe env MUST be on the
command line.

```bash
# Offline unit suite (no MySQL, no OpenAI, no network):
SANDBOX=1 AI_WARMUP_ON_IMPORT=0 SCHEDULER_OPPORTUNISTIC=0 \
  MYSQL_HOST=127.0.0.1 MYSQL_PORT=3306 MYSQL_USER=none MYSQL_PASSWORD=none MYSQL_DB=none \
  OPENAI_API_KEY=sk-test python3 -m pytest tests/ -q

# Boot smoke (create_app does not connect at construction — DB is lazy):
SANDBOX=1 AI_WARMUP_ON_IMPORT=0 MYSQL_HOST=127.0.0.1 MYSQL_USER=none MYSQL_PASSWORD=none MYSQL_DB=none \
  python3 -c "from run import create_app; create_app()"
```

- **AI-quality eval** (`ai_eval/run_eval.py`) drives `/app1/ask` against
  `ai_eval/golden_set.json` and scores with `ai_eval/scorers.py`. It needs a
  **live `OPENAI_API_KEY` + a Dockerized sandbox MySQL** (`./sandbox/sandbox.sh
  up && init`, port 3307) — NOT runnable offline. New behaviours have golden
  cases (search_paa_dansk, search_begynder_niveau, compare_best_two,
  english_prerequisites_reachability, learning_path_in_order). After an
  intentional quality shift, re-baseline once: `python3 ai_eval/run_eval.py
  --set-baseline`.
- The co-pilot upgrade's offline coverage is in
  `tests/test_ai_copilot_upgrade.py` (incl. tool-reachability guards for
  `show_cv_summary` / `show_mindmap_preview` / **`show_skill_gaps`**).
- CV ingestion + apply coverage (level-vocab round-trip, image-OCR routing) is
  in `tests/test_cv_ingest_apply.py`.
- **Competency layer** (canon/categories/scale/gap engine) in
  `tests/test_competency.py`; the **durable CV parse store** + CV-apply
  level-validity contract in `tests/test_cv_parse_store.py`.
- The **AI empowerment pass** (cross-silo learner agenda, own-compliance,
  HR navigation + page context, vendor catalog health) is covered offline by
  `tests/test_platform_ai_empowerment.py`: executor behaviour incl. the
  agenda's per-source degradation and its `order_approvals` fallback, the shared
  compliance primitives as pure functions, every `HR_DESTINATIONS` entry
  resolving, reachability for each new tool (Danish + English), and the drift
  guards (tool enums ↔ `UI_ACTIONS`/`HR_UI_ACTIONS`, new SSE events ↔
  `KNOWN_EVENT_TYPES` ↔ a chat.js branch, `HR_DESTINATIONS` ↔ the subnav).

---

## 10. File map

| Concern | File |
|---------|------|
| Agent orchestration, system prompts, SSE stream | `app1/agent.py` |
| **Context layers + budget assembly** | `ai_context_layers.py` (wired in `ai_runtime.prepare_messages_for_turn`) |
| **Conversation state (sessions, pointers, rev, digests)** | `app1/conversation_state.py` |
| **Semantic user knowledge + `recall_about_user`** | `app1/user_knowledge.py` (`rag.embed_texts`) |
| **HR learner context** | `learner_context.py` |
| **Platform help KB + `search_platform_help`** | `app1/help_kb.py`, `app1/help_kb/*.md` |
| Tool definitions + executors + dispatch | `app1/tools.py` |
| Per-turn tool selection + metadata + reachability fallback | `ai_tool_registry.py` |
| Shared model loop, tool-call events, model routing | `ai_runtime.py` |
| RAG retrieval / ranking | `app1/rag.py` |
| Profile store, completeness, learning paths | `app1/user_profile_db.py` (skills now canonicalized + categorized + level-validated on write) |
| **Competency layer (canon, categories, 1-5 scale bridge, gap engine)** | `competency.py` (reuses `hr_tools.SKILL_LEVEL_MAP`; `compute_skill_gaps`) |
| **CV parse-job store (durable, cross-worker)** | `cv_parse_store.py` (`ai_cv_parse_jobs` + in-proc fallback) |
| **Compliance derivation (shared primitives + company matrix + per-learner view)** | `hr_tools.py` (`compliance_requirement_applies` / `compliance_completion_matches` / `compliance_state_for_entries`; `derive_company_compliance`, `derive_employee_compliance`) |
| HR advisor loop, prompt, page context, `ui_action` | `hr_agent.py` |
| HR tool definitions + executors + dispatch (incl. `hr_open_in_app`) | `hr_tools.py` |
| Embedded HR AI panel (FAB, page id, action buttons) | `templates/fm/_ai_panel.html` (+ `.fm-aip-*` in `static/futurematch/assets/fm-pages.css`) |
| Vendor assistant tools (perf, demand, comparables, catalog health) | `vendor_tools.py` |
| Grounding / chain-of-custody | `grounding.py` |
| Confirm-token store | `app1/confirm_store.py` |
| SSE event vocabulary (canonical) | `app1/sse_events.py` |
| Routes (`/app1/ask`, confirm, profile) | `app1/__init__.py` |
| Page shells (`/chat`, `/ai-profiler`, `/mind-map`, `/profile`, `/profil-upload`) | `futurematch_ui.py` |
| Profile REST API + CV parse/stream/apply | `api.py` (level vocab → canonical via `_SKILL_LEVEL_MAP`/`_LANG_PROF_MAP`, case-insensitive, accepts both 3D-portal display labels and parser output) |
| CV text/image extraction + LLM profile parse | `cv_ingest.py` (PDF via pypdf/pdfplumber; images via GPT-4o vision OCR; never raises — degrades to a Danish hint) |
| Chat frontend (SSE dispatch, renderers) | `static/futurematch/assets/chat.js` |
| Chat styles | `static/futurematch/assets/chat.css` |
| Profile / profiler templates | `templates/fm/my_profile.html`, `ai_profiler.html`, `chat.html` |
| 3D surfaces (Three.js) | `templates/fm/cv_upload.html` (CV portal), `templates/fm/mind_map.html` + `static/futurematch/assets/mind-map-support.js` (DCLogic runtime) |
| Nav shell (sidebar links, `page_id` active state) | `templates/fm_base.html` |
| GDPR export/erase coverage | `gdpr_service.py` |

---

## 11. Part B changes (2026-09) - completion plan N-1 / N-3 / N-5 / N-6.4

North star unchanged: the AI is a **helper with a toolbox, never a form-filler**. Nothing below adds a
mandatory step; tools are offered, the model decides.

### Orders and completion (N-1)
- **One order lifecycle** (`order_lifecycle.py`): `pending_approval -> approved -> booked -> completed`, plus
  `rejected` / `cancelled`. Every tool that talks about an order reads these labels, never its own.
  Billing is a separate dimension and is **off-platform**: the AI must never show payment details
  (MobilePay/bank numbers were removed from `order_handler`). The honest copy is "Du modtager faktura".
- `create_course_order` fails truthfully (no fake success when the DB write fails), sends ONE e-mail, and is
  idempotent for the same person + course + date for 10 minutes (the chat "ja" + stale Bekræft card case).
  It prices the **chosen variant**, not `variants[0]`.
- `mark_course_complete` now calls `order_service.complete_order` - the single completion path. The result
  carries `skill_proposals`, `next_steps` and `review_url`; the model offers them **in conversation** (save the
  skills the user agrees to) rather than presenting a form. Completion also updates the profile's completed
  courses, learning progress and notifies the learner's manager ("Bekræft kompetenceløft").
- `check_order_approval_status` and the order tools see the canonical statuses; legacy values are normalised.

### Team orders follow company policy (N-5.2)
`team_order_policy.py` + table `company_team_order_policy` (`linked_orders` | `hr_bulk_assign` | `not_allowed`,
company default + per-vendor override). `create_course_order` takes optional `participants`; the tool reads the
effective policy and returns guidance (`policy_guidance_da`) that the model phrases naturally - it does not
recite the rule. Linked orders share a `group_order_id` and each goes through approval and budget.

### Catalog is one source (N-3.1)
`catalog_service.get_products()` feeds pages, search and the chat; `app1.rag` is an index over it (incremental
embed of new products, `shopify_sync` job, admin "Genopbyg indeks"). The raw Shopify JSON is **no longer in
git**: `CATALOG_SOURCE_FILE` (default `app1/shopify_products_all_pages.json`) points at it.

### Profiler (N-5.1)
Additions are saved immediately with an inline **Fortryd**; removals/edits keep the confirm card; several
changes can share one card. The "FORETRUKKEN METODE ... form" playbook, the X/8 banner and the
"Fortæl om min {missing}" chip are gone - completeness is context for the model, not a goal.

### HR assistant parity (N-5.3) and shared reply plumbing
- `hr_conversations.py`: durable HR memory in `conversation_history` (mode `hr`), per-user scoping, history and
  "open past conversation" routes.
- `ai_reply.py`: `<suggestions>` are parsed server-side and sent as their own event (HR, vendor, widget).
- HR confirm cards render and post to `/app1/confirm_tool_action`; HR turns are logged with feedback.

### Runtime correctness (N-5.5)
The Anthropic -> OpenAI provider fallback no longer replays the tool loop after a **mutating** tool has run
(it would have executed twice). `AI_TOOLER2` is documented as generally available (see
`docs/AI_TOOLER2_CHANGELOG.md`).

### AI analytics store (N-3.3)
Feedback, debug logs, latency and anonymous profiles moved from the per-server SQLite file to MySQL
(tables in `schema_registry`). One feedback scale everywhere: **+1 / -1**, stored with `message_index`; admin
and HR dashboards read the same table. Chat-to-order attribution fills `chatbot_session_id` /
`chatbot_queries_before_order` on the order.

### Credits are AI-usage metering (N-6.4)
`credit_service.py`: every AI turn (advisor, profiler, HR, vendor, widget) is deducted via the `credit_usage`
ledger; cost comes from `ai_cost_model` x `AI_CREDITS_PER_DKK` (default 10), min 1 credit. Balances are per
company (`company_credit_accounts`), solo users use `users.credits`. Grants go through `credit_service.grant`
(reason + actor). Low balance notifies HR at a threshold; at zero the company setting decides **soft** (default,
keep working, flag it) or **hard** (AI paused with `HARD_LIMIT_MESSAGE`). The `/app1/ask` and HR ask routes call
`credit_service.guard` before the turn. Ledger integrity: `verify_ledger` (balance == -SUM(credits_used)).

### Vendor assistant parity (N-5.4)
`vendor_conversations.py`: durable memory in `conversation_history` (mode `vendor`, owner `vendor:<id>` from the
session only). The vendor prompt has the same tone examples and `<suggestions>` contract as the HR assistant,
the vendor name is fenced as data, and figures are checked by the grounding circuit-breaker.

### Widget (N-5.6)
The iframe document is served only to an allowlisted parent (Referer host) and carries `frame-ancestors`. It
receives a signed, 12-hour session token (widget token, parent host, conversation id) that it sends as
`X-Widget-Session`; `/ask` accepts a token instead of trusting `Origin` (which is always our own host inside
the iframe) and the conversation id lives in the token, so memory works without third-party cookies.
The stream carries the `suggestions` event like the other surfaces.

### Guest memory and prompt A/B (N-5.8)
`anon_migration.migrate` turns the anonymous profile into the user's memories (source `anonymous`) on login and
deletes the guest row only after the copy succeeded. `AI_PROMPT_VARIANTS=v2.0,v2.1` assigns variants
deterministically by session id (first is control); `AI_PROMPT_ADDENDUM_<V>` adds instructions to a variant.

### Eval (N-5.7)
70 golden cases with HR/vendor scopes, a `fluency` metric (no form-filler patterns) and a nightly run on both
providers (`.github/workflows/ai-eval-nightly.yml`).
