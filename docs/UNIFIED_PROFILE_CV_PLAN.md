# Unified Profile · Skills · Competencies · Experience · CV — Upgrade / Expand / Harden Plan

> Grounded in a 4-agent ground-truth map of the live code (2026-06-19), not the docs.
> Companion to `docs/ai-framework.md` (the AI/CV technical map — **keep it current as this ships**),
> `docs/INTERACTIVE_CV.md` (CV-portal build), `AI_ENGINE_IMPROVEMENT_PLAN.md` (chat/RAG engine),
> and `docs/HR_VALUE_PLAN.md` (HR-side competency framework — this plan **unifies with it**, see §8).

---

## 1. North star & the A→C thesis

**North star (chosen): better AI recommendations.** Every change here serves one outcome —
the AI understands each learner well enough to recommend and sell the *right gap-closing*
courses and learning paths. Profile richness, interactivity, and 3D polish are means, not ends:
they exist to feed a cleaner signal into the match.

The work is the learner's journey, **A → C**:

| Stage | What it is | Today's state | The fix this plan makes |
|------|-----------|---------------|--------------------------|
| **A — Capture** | Get profile/CV/skills *in* (CV upload, AI profiler, manual edit) | 3 uncoordinated surfaces; add-only; CV apply fire-and-forget; no edit | One shared, editable, accessible capture system that all three surfaces share |
| **B — Structure & Understand** | Turn raw input into a real competency signal + gaps vs goal | **Missing.** Free-text skills, no taxonomy, **no gap computation** | The keystone: normalize/canonicalize skills, add categories, compute skill-gap-vs-`target_role`, on one scale shared with HR |
| **C — Act & Measure** | Recommend gap-closing courses/paths; prove uplift | Recommends on "low-level skills", no grounding, no uplift loop | Gap-grounded recommendations + learning paths + wired `skill_history` uplift, unified with the HR assign/skill-gap board |

**The single most important insight from the mapping:** the platform has a strong *A* and a
strong *C* bolted to a **missing *B***. There is no competency model and **no skill-gap engine**
(`target_role` exists at `user_profile_db.py:75` but is only a completeness *signal*, never used to
compute what the learner lacks). Recommendations therefore reason over "skills you rated low"
(`tools.py:3254–3267`) instead of "the gap between where you are and where you want to be."
Building *B* is the highest-leverage move for the north star.

### Success metrics
- **Gap coverage:** % of AI recommendations that target a *computed* gap vs `target_role`/company target (today: 0% — no engine).
- **Recommendation grounding:** every recommended course carries a verifiable `match_reason` naming the gap it closes (extend the existing `_course_match_reason` contract).
- **Profile→order conversion** from profile-conditioned recommendations (ties to `PLATFORM_ROADMAP` conversion metric).
- **Signal cleanliness:** duplicate-skill rate → ~0; % skills canonicalized + categorized.
- **Completeness lift** (`weighted_pct`) per profiler session; CV-apply success rate (no silent failures); cross-worker CV-parse reliability.

---

## 2. Scope decisions (locked)

1. **North star:** better AI recommendations — leads prioritization.
2. **Competency layer:** *normalize + gap-map* — canonicalize/dedupe skills, add categories, compute gaps by reusing catalog metadata + RAG. **Not** a full ESCO ontology.
3. **HR bridge:** *fully unify now* — one competency scale, employee skills feed the HR skill-gap board, wire `skill_history` uplift. Overlaps `HR_VALUE_PLAN`; §8 defines the lockstep seams.
4. **UI:** *unify + lighter 3D accent* — one shared component system (single ring, single editable card, shared tokens), editable/accessible/mobile everywhere, one performant 3D engine as progressive enhancement (not two heavy separate ones).

---

## 3. Target architecture — the unified spine

Four structural unifications collapse whole classes of the bugs the mapping found:

- **One competency module** (`competency.py`, new): canonical skill names + alias map + categories + the `compute_skill_gaps()` engine + the canonical level scale. Every writer (manual REST, CV apply, chat `update_user_profile`) and every reader (recommender, profiler, HR skill-gap board) goes through it. Kills "Python ≠ python ≠ JS/JavaScript" at the source.
- **One profile-write path** with validation + normalization + propose→confirm parity + audit. Today writes are scattered (`api.py` REST, `app1/__init__.py:1372` confirm, `cv/apply`) with asymmetric validation (languages validate, skills don't — `user_profile_db.py:527` vs `:287`).
- **One shared front-end component set**: a single completeness ring (today **4 implementations** — `my_profile.html:24`, `ai_profiler.html`, `chat.js:191`, `mind_map.html:79`), one read→edit→save profile card, shared `--fm-*` tokens (today `cv_upload.html` hardcodes `#0c6b62`, `chat.css` remaps to terracotta, `mind_map.html` has its own palette), one Three.js version (today r0.160 npm + r128 CDN).
- **One durable async store for CV parse** replacing the process-local `_cv_parse_results` dict (`api.py:7`) that is broken across gunicorn workers and leaks memory.

---

## 4. Phase 0 — Harden the foundation (load-bearing prerequisites)

These are correctness/safety bugs that make capture unreliable; the north star can't stand on them. Scoped tight to what blocks the rest.

### 0.1 — Fix the multi-worker CV-parse store *(Harden, Critical)*
- **Problem:** `_cv_parse_results = {}` (`api.py:7`) is **process-local**. The parse thread runs in worker B (`api.py:512`) while the SSE poller runs in worker A (`api.py:535` `.pop()`), so the result is invisible → the stream times out at 60s on real multi-worker deploys. Orphaned entries also never get purged on the timeout path (`api.py:550`) → memory leak.
- **Change:** Persist parse results in a shared store — a small MySQL table (`ai_cv_parse_jobs(session_id, status, proposal_json, hint, created_at)`) mirroring the proven `confirm_store.py` MySQL-backed + in-process-fallback pattern (`ai-framework.md §6`). TTL-sweep stale rows. SSE polls the table.
- **Why:** CV upload is a primary *A* surface for the north star (rich profile fast). Today it silently fails in production.

### 0.2 — Real (or honest) CV parse progress; emit `profiler_progress` *(Harden)*
- **Problem A:** parse-stream stages fire on a **hardcoded 6s/13s schedule** (`api.py:521`) regardless of actual progress. **Problem B:** `profiler_progress` is in `sse_events.py:47` and referenced by `MODE_PROFILES`, but the producer is **not found in `agent.py`** — the profiler ring may never update mid-session (producer/consumer drift).
- **Change:** Drive parse stages off real callbacks from `cv_ingest` (extract done → analyse → propose). Actually emit `profiler_progress` from `stream_generator` after each profiler turn (it already computes the completeness snapshot at `agent.py:1859`). Add the drift guard test alongside the existing `KNOWN_EVENT_TYPES` test.

### 0.3 — Upload safety: size, type, OCR cost, PII *(Harden)*
- **Problem:** no `MAX_CONTENT_LENGTH` on `/api/cv/parse` (`api.py:496`); type check is extension + a single PDF magic-byte (`cv_ingest.py:216`); GPT-4o vision OCR has no `timeout`/budget (`cv_ingest.py:91`); raw CV (name/email/phone) is sent to OpenAI verbatim and stored as free text.
- **Change:** enforce a byte cap (e.g. 8 MB) + MIME/magic validation; add `timeout` + a per-user OCR rate/budget cap; fence CV text with `grounding.delimit_untrusted` before the extraction prompt (`cv_ingest.py:427`, prompt-injection mitigation); add CV skills/experience to the `gdpr_service.py` export/erase surface; sanitize extracted strings (skill names render in `my_profile.html` — stored-XSS vector).

### 0.4 — Profile-write validation parity + propose→confirm hardening *(Harden)*
- **Problem:** manual `POST /api/profile/skills` returns `{success:true}` even when the enum insert is silently rejected (`api.py:130`, no rowcount check); skills don't validate level but languages do (`user_profile_db.py:287` vs `:527`); date fields accept nonsense (`start_year>end_year`); confirm tokens for `update_user_profile` **never expire** and aren't idempotent (`app1/__init__.py:1372`); only *add* actions go through propose→confirm — *remove/update* mutate immediately (`tools.py:3041–3157`).
- **Change:** centralize validation in the write path (valid level, date sanity, length); return real errors. Give confirm tokens a TTL + single-use idempotency + a minimal audit row (what the AI proposed vs. what the user confirmed). Route destructive *remove* (and level-lowering *update*) through propose→confirm too.

---

## 5. Phase 1 — The competency keystone (B) *(Expand — highest leverage for the north star)*

This is the missing middle. Build `competency.py` and make every surface use it.

### 1.1 — Canonicalize & categorize skills
- **Problem:** `user_skills.skill_name` is free-text `VARCHAR(255)`, UNIQUE is **case-sensitive** (`user_profile_db.py:33`); CV import adds "JS" beside manual "JavaScript"; no categories. Same for languages/courses.
- **Change:** a curated **alias map** `{canonical_name → [aliases], category}` seeded from existing catalog `skill_category` data + common DA/EN synonyms (lightweight, reversible, conservative — *not* an ontology). Normalize on **every** write (manual REST, `cv/apply`, chat `update_user_profile`): case-fold, trim, alias→canonical, **source-merge** instead of duplicate (keep the higher level; surface a "you already have X at level Y — update?" instead of silently adding). Add a `category` column to `user_skills`. Make the unique key effectively case-insensitive.
- **Why:** dirty signal in = noise out. Canonicalization is the cheapest multiplier on recommendation quality.

### 1.2 — The skill-gap engine `compute_skill_gaps(username)`
- **Problem:** **no gap computation exists.** `recommend_for_profile` proxies "skills rated begynder/mellem" for "gaps" (`tools.py:3254`), which can recommend "SQL for beginners" to someone whose target role is "HR Manager."
- **Change:** `compute_skill_gaps()` derives *required* competencies from three sources and diffs against current skills+levels:
  1. `target_role` → required skills (lightweight role→skill map, bootstrapped from catalog courses tagged to that role + a small curated seed),
  2. **`company_skill_targets`** (HR side — the unify-now bridge),
  3. `learning_goals`.
  Output: structured `[{skill, current_level, target_level, source, priority}]`. Each gap resolves to real catalog courses via the existing RAG path (reuse `suggest_learning_path`'s grounded search, `tools.py:3632`), with a verification step so a gap maps to a course that *actually teaches it* (today it's title-substring only).
- **Why:** this is the engine the north star is built on. It turns "here are some courses" into "here's how to close the gap to your goal."

### 1.3 — One canonical competency scale (lockstep with HR)
- **Problem:** employee skills use a 4-tier enum (`begynder/mellem/avanceret/ekspert`, `user_profile_db.py:30`); the HR matrix uses **1–5** (`HR_VALUE_PLAN` item #5, the "af 5 mulige" radar). Merged naively, gaps **flip sign**.
- **Change:** define one canonical numeric scale; map the 4-tier employee labels and the HR 1–5 matrix onto it **in lockstep**, documented in `competency.py` with a one-line canon comment. **This is shared with `HR_VALUE_PLAN` #5 — do it once, in one PR, as the single source of truth** (their risk register flags this exact sign-flip). The exact label→number mapping is a design decision to settle with the HR-matrix owner before writing.

---

## 6. Phase 2 — Unified interactive capture (A) *(Upgrade — unify + lighter 3D accent)*

Make the three capture surfaces feel like one editable product.

### 2.1 — Shared component system
- **Problem:** 4 completeness-ring implementations, 3 DOM structures; per-surface color tokens; 2 Three.js versions; no shared profile card.
- **Change:** one ring component, one `--fm-*` token source threaded through `cv_upload.html`/`mind_map.html`/`chat.css`, one Three.js version lazy-loaded. One **read→inline-edit→save** profile card reused by the profile page and the chat `profileConfirm`/`uiCard` renderers (`chat.js:485–581`).

### 2.2 — Edit everywhere (kill add-only)
- **Problem:** the profile page is **add-only** — skills/experience/education/certs/languages/links can be added and deleted but **never edited** (`my_profile.html`). To fix a parsed job title you must delete and re-add. `target_role` and add-memory use raw `window.prompt()` (`my_profile.html:581`, `mind_map.html:711`).
- **Change:** inline edit on every section (wire the existing `PUT /api/profile/*` endpoints that already exist for experience/education/certs/languages — `api.py:144–406` — but have no UI). Replace `window.prompt()` with the shared inline editor. Add proper loading/disabled-on-submit/error states (today forms can be double-clicked → duplicates, failures are silent).

### 2.3 — Bind the CV portal to the profile (close the A loop)
- **Problem:** `cv/apply` is fire-and-forget (`cv_upload.html:994`), toast "Profil gemt ✓" shows before the server confirms, and the profile page doesn't reflect changes without a manual refresh. No conflict handling on re-import.
- **Change:** await + validate the apply response; on conflict (canonicalizes to an existing skill) show a merge affordance (1.1); refresh completeness on success; surface real errors. The CV review cards become the same shared editable card as the profile/chat.

### 2.4 — Accessibility & mobile, lighter 3D
- **Problem:** the 3D scenes are mouse-only, no keyboard/SR path, no touch; impressive but isolated.
- **Change:** every 3D scene gets an accessible DOM mirror (the CV review list, the mind-map node list) as the base layer; 3D is progressive enhancement over it. Consolidate to one engine/version, lazy-load, add touch. Keep the "wow," lose the inaccessibility and the duplication.

---

## 7. Phase 3 — AI integration: how they work together *(Upgrade — directly serves the north star)*

With B in place, make the AI *use* it, and tighten the tool/feedback loop.

### 3.1 — Gap-grounded recommendations & learning paths
- `recommend_for_profile` and `suggest_learning_path` consume `compute_skill_gaps()` instead of "low-level skills." Each card's `match_reason` cites the specific gap ("lukker dit gap i *X* fra mellem→avanceret mod din målrolle *Y*") — extend the existing verifiable `match_reason` contract (`ai-framework.md §4`), never an LLM guess. Add a grounding check so a "skill" claim in the answer is backed by a real gap/profile term (reuse `grounding.py` chain-of-custody).
- Verify gap→course relevance beyond title-substring (`tools.py:3254`, `:3641`); skip gaps with no catalog coverage *and say so* rather than silently dropping (`tools.py:3641` `continue`).

### 3.2 — Profiler that uses gaps + role
- **Problem:** the profiler asks about the lowest-`strength` empty section (`agent.py:1859`), role-agnostic; no handoff state machine; `profiler_progress` not emitted (0.2).
- **Change:** prioritize the next question by **gap priority + role** (a Data Analyst and an HR Manager get different questions), not just empty sections. Make the `AI_PROFILER_HANDOFF_PCT` handoff (`agent.py:297`, default 70) gap-aware: hand off to gap-closing recommendations, not generic search. Emit `profiler_progress`.

### 3.3 — Tool reachability & new competency tools
- **Problem:** specialised tools sit off the core menu and the semantic fallback threshold (0.6, `ai_tool_registry.py:749`) misses paraphrases/English ("jeg vil gerne certificeres som data analyst" scores ~0.25 → `find_certification_path` never surfaces).
- **Change:** add `show_skill_gaps` (inline gap card → CTA to a gap-closing path) and a `compute_skill_gaps` read tool, registered the full **three-step** way (`ai-framework.md §4`: schema → executor/dispatch → menu/`_TOOL_TRIGGERS`/`_EMPLOYEE_META`) with reachability tests. Tune the fallback (lower threshold + lemmatize/compound-split + EN synonyms) so clear intents reach their tool.

### 3.4 — UI feedback during AI work
- Real CV parse progress (0.2); unified **multi-change** propose→confirm (one card when the AI proposes skill+experience+goal together, instead of 3 collapsing pills that lose context — `chat.js:485`); optimistic profile writes with ring refresh (the `refreshRing()` hook exists); honest tool chips (the live chip infra at `chat.js:713` already supports progress/error — feed it real states).

---

## 8. Phase 4 — Act & measure (C), unified with HR *(Expand — the unify-now bridge)*

### 4.1 — Gap → learning path → assign → nudge (one loop, two seats)
- Employee self-builds a gap path (3.1); the **same** `compute_skill_gaps()` + path structure feeds the HR `assign_learning_path` bulk-assign (`HR_VALUE_PLAN` item on the remediation loop). One gap model, surfaced to both the learner and the manager. Fix learning-path **versioning** (today `save_learning_path` overwrites by title with no history — `user_profile_db.py:1003`).

### 4.2 — Wire `skill_history` uplift
- `skill_history.py` (append-only trajectory) already exists but isn't written. Snapshot a row on every skill-level change (through the unified write path, §3 spine) so uplift = before/after-training gap closure becomes measurable. This is the capture pipeline `HR_VALUE_PLAN` #19 (skill-uplift loop) needs — **build it here, on the employee write path, so the HR ROI/uplift view can read it.** Respect k-anon/GDPR on any people-level uplift viz.

### 8-bridge — overlap discipline with `HR_VALUE_PLAN`
"Fully unify now" means three artifacts are **shared, single-source-of-truth**, not duplicated:
the **1-4↔1-5 scale** (their #5 / our 1.3), **`skill_history` uplift** (their #19 / our 4.2),
and **`company_skill_targets`** as a gap source (our 1.2). Land these on **one branch**, coordinate
sequencing so we don't both edit `insights_engine.py`/`skill_gaps.html`/`skill_history.py` in
conflicting ways, and keep the canonical scale defined in exactly one place (`competency.py`).

---

## 9. Cross-cutting

- **Keep the technical doc current.** `docs/ai-framework.md` explicitly asks to be updated when AI surfaces change (§5 profile/completeness, §3 SSE vocab, §4 tools, §10 file map). Update it + `INTERACTIVE_CV.md` as each phase lands — it's the map the next agent reads.
- **Tests & eval (safe env, never prod DB — `ai-framework.md §9`).** Extend `tests/test_cv_ingest_apply.py` (multi-worker store, canonicalization round-trip, conflict-merge), `tests/test_ai_copilot_upgrade.py` (reachability guards for `show_skill_gaps`/`compute_skill_gaps`); new `tests/test_competency.py` (gap engine, scale mapping, source-merge). Add `ai_eval/golden_set.json` cases for gap-grounded recommendations + the profiler→gap handoff; re-baseline once (`run_eval.py --set-baseline`) after the intentional quality shift.
- **Data-viz standard.** Any skill-gap / uplift / radar visual obeys the first-class data-viz bar (token-bound, dark-mode/white-label safe, k-anon safe) per the project standard.
- **GDPR/PII.** CV PII + structured competency data flow into `gdpr_service.py` export/erase.

---

## 10. Sequencing & dependencies

```
Phase 0 (harden)  ─┬─ 0.1 multi-worker CV store ─┐  blocks reliable A
                   ├─ 0.2 progress/profiler_progress
                   ├─ 0.3 upload safety/PII
                   └─ 0.4 write validation + confirm hardening ─┐ blocks AI writes (3.4)
                                                                │
Phase 1 (B keystone) ── 1.1 canonicalize ──▶ 1.2 gap engine ──▶ 1.3 scale (lockstep w/ HR #5)
        │  (canonicalize MUST precede the gap engine; scale precedes HR-fed gaps)
        ▼
Phase 3 (AI) ── 3.1 gap-grounded rec ── 3.2 profiler ── 3.3 tools ── 3.4 feedback
        │           (depends on 1.2 gaps + 0.2/0.4)
        ▼
Phase 4 (C) ── 4.1 gap→path→assign (unify HR) ── 4.2 skill_history uplift
        (depends on 1.2 + HR assign path + skill_history)

Phase 2 (UI) ── runs largely in parallel; 2.2 shared editable card should land with 3.4;
               2.3 CV↔profile binding depends on 0.1 + 1.1 (merge/conflict).
```

**Critical path for the north star:** 0.1 → 1.1 → 1.2 → 3.1. That's the shortest line from "dirty, siloed data" to "AI recommends real gap-closers." Everything else hardens or amplifies it.

---

## 11. Risk register

- **Scale unification flips gap signs** — map both sources in lockstep, one source of truth in `competency.py`, communicate as a correction (mirrors `HR_VALUE_PLAN` risk).
- **Canonicalization over-merges distinct skills** — curated, conservative, source-tracked, reversible alias map; surface merges to the user rather than silently collapsing.
- **Multi-worker CV store change touches a live flow** — keep the no-JS server-render fallback (`futurematch_ui.py` `/profil-upload`) working throughout.
- **HR overlap churn** — §8-bridge: one branch, coordinated edits to shared files, single canonical scale.
- **3D consolidation regresses the "wow"** — accessible DOM base first, 3D as progressive enhancement; ship behind the existing fallback.
- **Prompt injection / PII via CV** — fence (`delimit_untrusted`) + validate + sanitize + GDPR surface (0.3).
- **Eval numbers drop on the intentional shift** — expected; single `--set-baseline` after gap-grounding lands, with the new golden cases in place.

---

## 12. File map (where the work lands)

| Concern | Files |
|--------|-------|
| **New competency module** (canon, aliases, categories, gap engine, scale) | `competency.py` (new) |
| Profile store + completeness + canonicalize-on-write + `category` column | `app1/user_profile_db.py` |
| REST validation parity, multi-worker CV store, gap/upload safety | `api.py`, new `ai_cv_parse_jobs` table |
| CV extract/parse: fence, OCR timeout, real progress callbacks | `cv_ingest.py` |
| Confirm-token TTL/idempotency/audit, propose→confirm parity, `profiler_progress` | `app1/__init__.py`, `app1/agent.py`, `app1/confirm_store.py` |
| Gap-grounded tools + new `show_skill_gaps`/`compute_skill_gaps` (+ reachability) | `app1/tools.py`, `ai_tool_registry.py`, `app1/sse_events.py` |
| Shared ring/card/tokens, edit-everywhere, CV↔profile binding, a11y/3D | `templates/fm/my_profile.html`, `cv_upload.html`, `mind_map.html`, `ai_profiler.html`, `static/futurematch/assets/chat.js`, `chat.css`, shared CSS tokens |
| HR bridge (scale, uplift, targets) | `skill_history.py`, `insights_engine.py`, `hr_dashboard/__init__.py`, `templates/fm/skill_gaps.html` — **coordinate with `HR_VALUE_PLAN`** |
| Tests / eval | `tests/test_competency.py` (new), `tests/test_cv_ingest_apply.py`, `tests/test_ai_copilot_upgrade.py`, `ai_eval/golden_set.json` |
| Docs to keep current | `docs/ai-framework.md`, `docs/INTERACTIVE_CV.md` |
