# Roadmap: open work only

The older completion plans are in git history. The implemented launch journeys and their operational limits are documented in [runbooks/LAUNCH_WORKFLOWS.md](runbooks/LAUNCH_WORKFLOWS.md). A feature existing in code does not prove a deployed customer can complete it: run that acceptance checklist with a sandbox customer, actual SMTP recipients and a supplier before commercial launch. Standing product constraints live in [DECISIONS.md](DECISIONS.md); owner-only operational work lives in [USER_ACTIONS.md](USER_ACTIONS.md).

## Commercial launch gate

- Validate the deployed employee → HR → supplier → attendance → outcome journey.
- Agree the actual customer offer, pilot success criteria, support ownership and supplier handling. Record them in the customer handover screen.
- Confirm actual mail receipt and worker health; do not use an SMTP-success counter as inbox-delivery evidence.
- Future supplier-specific live inventory/reservation integrations and binary certificate storage remain optional extensions, not claims made by the current UI.

## How to read the tracks

The work below came out of a four-part audit (UX cohesion, domain/data seams, AI layer, operations). It is ordered by dependency, not by date: Track 0 first, then Track 1, then Tracks 2 and 3 side by side, Track 4 as needed. A track item is removed from this file when it is done; durable "why" moves to [DECISIONS.md](DECISIONS.md). Production is one server; multi-host work is out of scope.

## Track 0: safety net

| ID | Item | Where | Done when |
|---|---|---|---|
| T0-1 | **Apply the web server configuration** (owner). `deploy/gunicorn.conf.py` exists (threaded workers, stream-safe timeout); production still runs the bare command until the ServerHoster start command is changed. | [runbooks/DEPLOY.md](runbooks/DEPLOY.md), [USER_ACTIONS.md](USER_ACTIONS.md) section 3 | two chat streams and a page load run at once without a worker timeout |
| T0-2 | **Rotate leaked secrets and purge history** (owner only). | [USER_ACTIONS.md](USER_ACTIONS.md) section 1 | weekly full-history gitleaks is clean |
| T0-3 | **The AI eval reports somewhere.** The nightly eval still lives in GitHub Actions, ungated and `continue-on-error`; confirm it runs (secrets present) or move it next to the CI. | `.github/workflows/ai-eval-nightly.yml`, `.survhub/` | a failing scorecard reaches a person |
| T0-5 | **Browser journey, remaining steps.** `tests/e2e/journey.test.cjs` covers request, approval, booking with an agreed date deviation, attendance report, verification, completion and goal privacy on the demo tenant (`sandbox/demo_seed.py`). Not yet covered from the pilot checklist in [runbooks/LAUNCH_WORKFLOWS.md](runbooks/LAUNCH_WORKFLOWS.md): group price, stale assignment preview, learning-path assignment, internal course, compliance renewal, cancellation/reschedule/substitution with fee, manager outcome assessment, mail recovery with SMTP stopped, vendor edit approval, seat/support request. It also does not run in CI yet (the CI image has no Chromium). | `tests/e2e/`, `.survhub/ci.yml` | the whole checklist runs green unattended on every pull request |
| T0-7 | **Backups running on the server** (owner). The procedure and `scripts/backup.sh` exist and were rehearsed on the sandbox ([runbooks/BACKUP_RESTORE.md](runbooks/BACKUP_RESTORE.md)). Still to do on the host: schedule it daily, copy it off the server, rehearse one restore there, and decide whether to run a staging copy. | [runbooks/SERVERHOSTER_HANDOVER.md](runbooks/SERVERHOSTER_HANDOVER.md) | a restore has been rehearsed on the production server and a missed backup is noticed |
| T0-8 | **One React in the mind map.** External scripts are now pinned with integrity hashes and the common libraries are served by the app, but the mind map still loads React 17 in the template and React 18 plus Babel (3 MB, compiling JSX in the browser) from `mind-map-support.js`. Build the mind map once, ship it as a static bundle, and drop the three CDN hosts from the CSP. | `templates/fm/mind_map.html`, `static/futurematch/assets/mind-map-support.js`, `security_headers.py` | the mind map loads no script from another host |

## Track 1: one domain spine

| ID | Item | Where | Done when |
|---|---|---|---|
| T1-5 | **One skills read model.** Self and manager levels are merged separately in `hr_dashboard`, `hr_tools`, `insights_engine`, `cv_service` and `app1/tools.py`; three label scales are in use (`competency.score_to_level`, `api._SKILL_LEVEL_MAP`, the 1-5 talent scale). | `competency_model.snapshot` as the only reader | one function answers "what level is this person at", one label set |
| T1-6 | **Finish the one goal record.** `goal_service.py` is the read model with owner and visibility, sharing works in both directions and HR goals have a status writer. Open: the learner pages still read the two stores directly (`futurematch_ui.learning_goals`, `learner_context.py`, `app1/user_profile_db.get_learning_goals`) instead of `goal_service.for_learner`; free-text `user_profile_summary.goals` and goal-like AI memory facts are still separate from goal records; `user_learning_goals` has no `company_id` of its own. | `goal_service.py`, `futurematch_ui.py`, `learner_context.py`, `app1/user_profile_db.py` | every surface and assistant reads goals through `goal_service` |
| T1-7 | **One learning-path model.** AI-built `user_learning_paths` run beside HR `learning_paths`/`employee_learning_progress`. | `app1/user_profile_db.py`, `learning_path_service.py` | a personal path and an assigned path are the same object with a different owner |
| T1-8 | **KPI queries in one module.** The definitions are shared and test-enforced (which orders count, what is completed, in progress and still open: `order_lifecycle`, `tests/test_kpi_definitions.py`). The queries themselves are still written out in each dashboard, report and tool; move the recurring ones (spend per department, completion rate, overdue) into one module that pages and the HR assistant both call. | new `kpi_service.py`; `hr_dashboard`, `hr_tools.py`, `insights_engine.py`, `multitenant_reports.py`, `enterprise_api` | a KPI shown on a page and said by the assistant comes from the same function |
| T1-10 | **Schema invariant finished.** `tests/test_schema_single_home.py` stops new table definitions outside the registry and lists the 42 that remain in 17 older files (17 of them in `app1/user_profile_db.py`). Move them into `schema_registry.py` file by file, lowering the list as you go. `user_skills` and `user_learning_goals` still have no `company_id` of their own. | `schema_registry.py`, the files in that test's `LEGACY` | the `LEGACY` list is empty |

## Track 2: one product experience (needs Track 1)

| ID | Item | Where | Done when |
|---|---|---|---|
| T2-1 | **The learner loop, last links.** Gap → course → order → completion → competency → workspace → internal opportunity is linked, and the learner home points to the rest of the workspace. Open: the CV studio does not show role readiness or matching opportunities; a catalogue course does not say which of the learner's gaps it closes. | `templates/fm/my_cv.html`, `cv_service.py`, `templates/fm/product_detail.html` | a course page and the CV both point at the learner's own gaps |
| T2-2 | **A list of the learner's learning paths.** Every role now lands on a page that is in its sidebar (managers and admins have "Forside"). Learners still see their assigned and personal paths only as cards on the home page and the profile. | `learning_routes.py`, `templates/fm/` | a learner can open one page with all their paths |
| T2-4 | **Vendor portal, remaining.** The signed-in vendor pages share one base (`fm/vendor_base.html`, `vendor-portal.css`): one head, the tab bar on every page including the course editor, a skip link and a remembered light/dark choice. Open: each page still carries most of its own inline CSS (only identical rules were lifted out); vendors have no notification list and no search; the login, set-password and invite pages are still standalone. | `templates/fm/vendor_*.html`, `static/futurematch/assets/vendor-portal.css` | no vendor page defines chrome styles of its own |
| T2-5 | **The legacy chat demo shell.** The unlinked `/brands` pages are retired (redirect to the profile). What is left is `/app1/demo` (`app1/templates/index.html`, 2,650 lines with its own sidebar and a second `static/futurematch/chat.css`), reachable only on localhost or with `FUTUREMATCH_ENABLE_DEMO_CHAT`. Decide whether developers still use it; if not, delete it and the second stylesheet. (`app1/templates/adminlog.html` is the AI debug log from [runbooks/CHAT_DEBUG.md](runbooks/CHAT_DEBUG.md) and `templates/sso_login.html` is the disabled LDAP form: neither is a duplicate.) | `app1/__init__.py` (`/demo`), `app1/templates/index.html` | one chat page |
| T2-7 | **Theme pass.** The shell has a skip link and announces the current page; 207 hardcoded colours and 2,500 inline styles still make dark mode uneven, and no automated accessibility check runs. | `static/futurematch/assets/fm.css`, worst templates first, `tests/e2e/` | the journey pages pass an automated a11y check in both themes; no hardcoded colour in shared partials |
| T2-8 | **First-run guidance** for the talent workspace, the CV studio and vendors. | `fm/_macros.html` (`empty`) | a new user of each sees what to do first |

## Track 3: AI as the connective tissue (needs Track 1)

Read the "AI changes" section of `CLAUDE.md` first: every item must hold on both providers and read as an expert, not a form.

| ID | Item | Where | Done when |
|---|---|---|---|
| T3-1 | **One agent platform.** The vendor assistant has no context layers and no tool selector; some model calls bypass the provider layer with hard-coded models (`cv_ingest.py`, `insights_engine.py`, `app1/build_index.py`, RAG rerank). | `vendor_portal.py`, `ai_tool_registry.py`, `ai_provider.py` | all three assistants share layers, selector, grounding and confirm flow; one place names models |
| T3-2 | **Talent tools, remaining.** Done: the learner assistant reads the person's development (`get_my_development`) and shares a goal behind a confirm card (`share_learning_goal`); the HR assistant's employee lookup shows goals and role readiness. Open: learner writes (self-assessment, linking a goal to requirements, applying to an opportunity) and HR writes and overviews (role frameworks, hiring pipeline, capability scenarios), all confirm-gated through `ai_actions`; `/hr/talent` has no page hints (`_HR_PAGE_TOOLS`, `sse_events.HR_DESTINATIONS`); no chat card for the development overview. | `app1/tools.py`, `hr_tools.py`, `talent_service.py`, `ai_tool_registry.py` | the assistants can act in the talent workspace, not only read it |
| T3-3 | **Coverage of the remaining surfaces.** The HR assistant can open and take hints from the talent page. Open: tools for reports, mail recovery and onboarding readiness; hints for billing and credits pages (`_HR_PAGE_TOOLS`); a contextual assistant entry on settings and admin pages; help articles for HR and vendors (all 14 are for employees). | `hr_tools.py`, `ai_tool_registry.py` (`_HR_PAGE_TOOLS`), `app1/sse_events.py`, `app1/help_kb/` | the assistant on any page can explain and act on that page |
| T3-4 | **Answers that match the tools.** The November probe found a confirm card described as saved, prices not in tool output, and "I can't see results" after a search ran. Add a verification pass against the turn's tool results. | `grounding.py`, `ai_runtime.py`; [reviews/2026-11-chat-probe/README.md](reviews/2026-11-chat-probe/README.md) | those probe cases pass on both providers |
| T3-5 | **Company documents, remaining.** Upload, company-scoped search and the assistant tools exist (DECISIONS.md, item 32). Open: Word files (`.docx`) and scanned PDFs are refused; retrieval is lexical only, so a question phrased with none of the document's words finds nothing (measure on real customer documents before adding embeddings); no help article for HR on managing documents; the eval has no cross-tenant document case that runs against a live model. | `company_documents.py`, `ai_eval/golden_set.json`, `app1/help_kb/` | HR can upload the formats customers actually have, and retrieval quality is measured |
| T3-7 | **Cross-role case.** Learner request, HR approval and vendor booking are joined only by data; give each assistant the same view of where a request stands and who acts next. | `ai_entity_context.py`, `ai_followups.py` | any of the three can answer "hvor er den?" identically |
| T3-8 | **Evals that gate.** The golden set is employee 67, HR 13, vendor 7, and the drift guard knows all three tool lists. None of the cases added since the audit has run against a live model (no API key in the build environment). Still to do: run both providers and re-baseline, make a regression fail a build, and turn reviewed thumbs-down turns into cases (`set_feedback_reviewed` feeds nothing today). | `ai_eval/`, `.github/workflows/ai-eval-nightly.yml` | a quality regression fails a build |
| T3-9 | **A default cost ceiling per run.** Turns are rate-limited per actor and a run stops after `AI_MAX_TOOL_CALLS`, but `AI_MAX_RUN_COST`/`AI_MAX_RUN_TOKENS` still default to unlimited. Pick defaults from real `ai_agent_runs` data so a normal long answer is never cut. | `ai_runtime.py` (`max_run_cost_usd`, `max_run_tokens`) | a single run cannot cost more than an agreed amount |

## Track 4: maintainability and reach

| ID | Item | Where | Done when |
|---|---|---|---|
| T4-1 | **Multi-worker state** (only if T0-1 ends with more than one process): `rate_limit._events` and `perf_cache._STORE` are per process. | `rate_limit.py`, `perf_cache.py` | limits and caches are shared (MySQL or Redis) |
| T4-2 | **Split the largest files.** `hr_dashboard/__init__.py` holds 91 routes as closures in one factory; `hr_tools.py` holds domain logic eight modules import (`SKILL_LEVEL_MAP`, compliance derivation). | those files | domain logic lives in service modules; route groups are separate files |
| T4-3 | **Data access.** 600+ raw `cursor()` sites; `db_tx.py` has no production caller. Adopt it in new and touched code. | `db_tx.py` | services use the context managers |
| T4-4 | **Types on service contracts.** The lint gate now runs all of pyflakes, pylint's error class and the bugbear checks that find real mistakes (`ruff.toml`). Open: type hints on the service modules' public functions and a type check in CI; the string-built-SQL rule (155 hits, all identifiers or placeholders so far) needs triage before it can gate. | `ruff.toml`, service modules | CI type-checks the service layer |
| T4-5 | **API and webhooks for the newer domains:** talent, learning paths, fulfilment changes, goals, certifications. | `enterprise_api/`, `enterprise_company_settings.WEBHOOK_EVENT_CHOICES` | an integration can follow a booking change and a placement |

## Identity

Sizes: **XS** under 1 h, **S** up to 1/2 day, **M** 1 to 3 days, **L** 1 to 2 weeks. Line numbers drift; search for the symbol.

| ID | Item | Where | Size |
|---|---|---|---|
| R-1 | **SAML and LDAP/AD SSO** (deferred by decision, pick up only when an enterprise customer requires it). Needs: SAML signature verified against the stored certificate plus audience/destination/time-window checks (new dependency such as `python3-saml`; none in `requirements.txt` today), LDAP filter escaping, ACS/metadata URL fields in the config form (`generate_saml_request` raises `KeyError` on `acs_url`), and a settings-hub UI option (it offers OIDC only). | `enterprise_sso/__init__.py`: `SSO_DISABLED_MESSAGE`, `_has_signature` (presence-only check, forgeable), LDAP filter, `generate_saml_request`; `settings_hub.py` | L |

## Not on the roadmap, on purpose

- Payment providers, card or bank flows, invoice PDFs: billing is tracked, not processed (see DECISIONS.md).
- Self-serve upgrade or trial paywall copy (sales-led).
- Confirm cards in the embeddable widget: visitors are anonymous and write tools require a logged-in user (`auth_required` in `ai_tool_registry.py`).
- Loading skeletons: pages are server-rendered.
