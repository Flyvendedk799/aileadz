# AI Framework — app1 course-advisor & profiler

> **Purpose.** A durable, high-signal map of the app1 AI platform so a future
> agent can change it confidently **without re-reading the whole codebase**.
> Anchors are `file:line` (approximate — grep the symbol if it has drifted).
> Keep this file current when you change the AI surfaces.

---

## 1. The big picture: one engine, two "AIs"

aileadz / FutureMatch is a B2B, sales-led learning platform. The user-facing AI
is **one OpenAI-backed agentic chat engine** (gpt-4o / gpt-4o-mini — NOT
Anthropic) exposed as **two modes** selected by a client-supplied `mode` string:

| Mode | "AI" | Shell route | Difference |
|------|------|-------------|------------|
| `default` | **Course suggester** | `/chat` (`futurematch_ui.py:25`) | the advisor/recommender |
| `profiler` | **AI Profiler** | `/ai-profiler` (`futurematch_ui.py:31`, login-gated) | appends `SYSTEM_PLAYBOOK_PROFILER`, injects a live completeness snapshot, emits `profiler_progress`, and (new) deterministically hands off to course recommendations at high completeness |

There is **one endpoint** (`POST /app1/ask`, `ask()` at `app1/__init__.py:932`),
**one frontend** (`static/futurematch/assets/chat.js`), and **one toolset**. The
mode is the only switch. Per-mode policy is centralised in `MODE_PROFILES`
(`app1/agent.py:297`, near the system prompts).

---

## 2. Request → tool → SSE lifecycle

```
chat.js run() ─POST {query,mode}─▶ /app1/ask (ask() at app1/__init__.py:932)
   └▶ handle_agentic_ask (app1/agent.py:1298)
        ├─ resolve session, lazy-load memory, build fenced system context
        ├─ _classify_intent_local (agent.py:594)  [+ gpt-4o-mini router only on 'discovery']
        ├─ _detect_conversation_stage (agent.py:341) → reconcile with intent
        ├─ get_employee_tool_selection (ai_tool_registry.py:803) → per-turn tool menu
        └─ stream_generator() (agent.py:1764)
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
| `tool_progress` | (currently unused) | `updateToolProgress` | percent/note |
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
- **Profiler → suggester handoff (new):** when profiler completeness
  `weighted_pct ≥ AI_PROFILER_HANDOFF_PCT` (default 70), `stream_generator`
  deterministically calls `recommend_for_profile`, emits `course_cards` + a
  CTA `ui_action`, instead of only flipping a UI tag.
- **Proactive profiler (new):** `ai_profiler.html` auto-asks the first targeted
  question once per browser session (guarded by `sessionStorage` + empty
  thread) instead of waiting for a Start click.

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
  - `GET /api/cv/parse-stream` (SSE) — stage labels advance on a gentle schedule
    but **completion is driven off the real `cv_parse_store.read`** result, then
    emits one terminal `result` (`{proposal, hint}`) or `error`. Event names here
    are `stage` / `result` / `error` — NOT the chat `type` strings.
  - `POST /api/cv/apply` (`api.py:598`, JSON `{session_id, accepted:[…]}`) —
    writes approved items via `add_skill/experience/education/certification/
    language`. **Level vocab must be normalised** through `_SKILL_LEVEL_MAP` /
    `_LANG_PROF_MAP` (lowercase-keyed, case-insensitive) — they accept BOTH the
    portal's display labels (Begynder/Øvet/…) and the parser's canonical
    lowercase output (begynder/mellem/…); the old capitalized-only map silently
    inflated every parsed skill to `avanceret`.
  - Empty proposal → the portal resets to upload state and shows the `hint`
    toast rather than entering a blank 0-card review.
  - **No-JS fallback:** the `<form>` posts to `/profil-upload` +
    `/profil-upload/apply` (`futurematch_ui.py`), a self-contained server-render
    path (its own whitelist level validation, defaults to `mellem`).
- **Mind-Map** (`/mind-map` → `templates/fm/mind_map.html`): a DCLogic React
  runtime (`static/futurematch/assets/mind-map-support.js`) + Three.js globe,
  fed by `GET /api/profile/mindmap` (`api.py:665`, root→category→leaf graph from
  structured profile + `user_memories` + conversation summary). DC template
  bindings use `{{ }}`, so the block is wrapped in `{% raw %}`. Memory CRUD from
  the page: DELETE `/api/profile/memories` `{id}`, POST `{label,detail,category,
  source}`.

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
  stores free-form facts with near-duplicate supersede. `memory_saved` now
  carries the row `id` so chat.js renders an inline "Forkert / slet"
  affordance.

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

## 8. Env flags

| Flag | Default | Effect |
|------|---------|--------|
| `AI_TOOL_SEMANTIC_FALLBACK` | on | paraphrase/English tool reachability fallback |
| `AI_PROFILER_HANDOFF_PCT` | 70 | weighted-completeness threshold for the profiler→suggester handoff |
| `AI_SEARCH_HARD_FILTERS` | on | carry hard filters into RAG fallback + progressive relaxation |
| `AI_FILTER_PAST_DATES` | on | drop expired variant dates |
| `AI_GROUNDING_RECALL` | off | pre-stream corrective re-generation on a grounding violation |
| `AI_LIVE_TOOL_EVENTS` | on | stream tool start/finish chips live from a worker thread |

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

---

## 10. File map

| Concern | File |
|---------|------|
| Agent orchestration, system prompts, SSE stream | `app1/agent.py` |
| Tool definitions + executors + dispatch | `app1/tools.py` |
| Per-turn tool selection + metadata + reachability fallback | `ai_tool_registry.py` |
| Shared model loop, tool-call events, model routing | `ai_runtime.py` |
| RAG retrieval / ranking | `app1/rag.py` |
| Profile store, completeness, learning paths | `app1/user_profile_db.py` (skills now canonicalized + categorized + level-validated on write) |
| **Competency layer (canon, categories, 1-5 scale bridge, gap engine)** | `competency.py` (reuses `hr_tools.SKILL_LEVEL_MAP`; `compute_skill_gaps`) |
| **CV parse-job store (durable, cross-worker)** | `cv_parse_store.py` (`ai_cv_parse_jobs` + in-proc fallback) |
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
