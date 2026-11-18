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
| T0-6 | **Legacy data migrations on real MySQL.** The whole suite and the demo seed now pass on MySQL 8 from an empty database, so table creation and the additive columns are proven. What has still never run on MySQL is `schema_registry.run_data_migrations` against **legacy-shaped data** (old order statuses, plaintext passwords, `company_notifications`). | `tests/test_launch_mysql.py` | the migrations run against MySQL 8 in CI from a legacy-shaped fixture |
| T0-7 | **Backups running on the server** (owner). The procedure and `scripts/backup.sh` exist and were rehearsed on the sandbox ([runbooks/BACKUP_RESTORE.md](runbooks/BACKUP_RESTORE.md)). Still to do on the host: schedule it daily, copy it off the server, rehearse one restore there, and decide whether to run a staging copy. | [runbooks/SERVERHOSTER_HANDOVER.md](runbooks/SERVERHOSTER_HANDOVER.md) | a restore has been rehearsed on the production server and a missed backup is noticed |
| T0-8 | **Front-end supply chain.** CDN scripts carry no integrity hash; `marked` (legacy chat shell) and React (mind map) are unpinned, and the mind map loads two React versions. | `templates/fm/mind_map.html`, `app1/templates/index.html`, `static/futurematch/assets/mind-map-support.js` | every CDN tag is pinned with SRI, one React |

## Track 1: one domain spine

| ID | Item | Where | Done when |
|---|---|---|---|
| T1-5 | **One skills read model.** Self and manager levels are merged separately in `hr_dashboard`, `hr_tools`, `insights_engine`, `cv_service` and `app1/tools.py`; three label scales are in use (`competency.score_to_level`, `api._SKILL_LEVEL_MAP`, the 1-5 talent scale). | `competency_model.snapshot` as the only reader | one function answers "what level is this person at", one label set |
| T1-6 | **Finish the one goal record.** `goal_service.py` is the read model with owner and visibility, sharing works in both directions and HR goals have a status writer. Open: the learner pages still read the two stores directly (`futurematch_ui.learning_goals`, `learner_context.py`, `app1/user_profile_db.get_learning_goals`) instead of `goal_service.for_learner`; free-text `user_profile_summary.goals` and goal-like AI memory facts are still separate from goal records; `user_learning_goals` has no `company_id` of its own. | `goal_service.py`, `futurematch_ui.py`, `learner_context.py`, `app1/user_profile_db.py` | every surface and assistant reads goals through `goal_service` |
| T1-7 | **One learning-path model.** AI-built `user_learning_paths` run beside HR `learning_paths`/`employee_learning_progress`. | `app1/user_profile_db.py`, `learning_path_service.py` | a personal path and an assigned path are the same object with a different owner |
| T1-8 | **One KPI module.** The status sets are shared now (`order_lifecycle.SQL_NOT_COUNTED`, `SQL_IN_PROGRESS`; used by `hr_tools` and the admin company list), but most queries still spell the filter inline, and "completed" reads `completion_status` in `hr_dashboard` and the API analytics but `status` in `hr_tools` (`multitenant_reports` accepts either). Move the KPI queries themselves into one module. | new `kpi_service.py`; `hr_dashboard`, `hr_tools.py`, `insights_engine.py`, `admin_reports.py`, `multitenant_reports.py`, `enterprise_api` | each KPI has one SQL definition and a test that the pages and the HR assistant agree |
| T1-10 | **Schema invariant finished.** `CREATE TABLE` outside the registry: 17 in `app1/user_profile_db.py`, more in `scheduler`, `event_bus`, `enterprise_api`, `ai_runtime`, `login_guard`, `email_service`, `cv_parse_store`. `user_skills` and `user_learning_goals` have no `company_id`. | `schema_registry.py`, `enterprise_tables.py` | a test fails on a `CREATE TABLE` outside the two homes |
| T1-11 | **Dead report inputs.** `reports.py` reads `app_usage` and `social_metrics`, which nothing writes. | `reports.py` | the page shows real data or is merged into the reporting that has it |

## Track 2: one product experience (needs Track 1)

| ID | Item | Where | Done when |
|---|---|---|---|
| T2-1 | **The learner loop is linked end to end:** gap → course → order → completion → competency → CV → internal opportunity. Done: each unmet requirement in `/udvikling` links a catalog search, a completed order links back to the workspace, exploring a role offers the internal opportunities. Open: the learner home links none of talent, opportunities, mind map or colleague chat; the CV studio does not show role readiness or matching opportunities; a catalog course does not say which of the learner's gaps it closes. | `templates/fm/employee_home.html`, `my_cv.html`, `product_detail.html`, `cv_service.py` | each step offers the next one |
| T2-2 | **One home per role, complete sidebar.** `/dashboard` (manager landing) has no sidebar entry; `/muligheder` is reachable only from inside the talent workspace (a sidebar entry needs the company's mobility flag in the nav context); learners have no learning-path list page. | `futurematch_ui.py` (`SIDEBAR_BY_ENDPOINT`, `nav_state`), `templates/fm_base.html` | every page is reachable in two clicks and lights one entry |
| T2-4 | **Vendor portal on the shared shell.** Nine standalone pages without theme toggle, notifications or search; `vendor_course_edit.html` has no navigation. | `templates/fm/vendor_*.html`, `fm/_vendor_nav.html` | vendor pages extend one base |
| T2-5 | **Legacy pages retired** (merged and redirected, per "complete, don't remove"): `/brands` (`templates/profile.html`, `add_brand.html`), the `app1` demo shell and its second `chat.css`, the duplicate admin log, the duplicate SSO login page. | `auth/__init__.py`, `app1/templates/`, `templates/sso_login.html` | one template per page |
| T2-7 | **Theme pass.** The shell has a skip link and announces the current page; 207 hardcoded colours and 2,500 inline styles still make dark mode uneven, and no automated accessibility check runs. | `static/futurematch/assets/fm.css`, worst templates first, `tests/e2e/` | the journey pages pass an automated a11y check in both themes; no hardcoded colour in shared partials |
| T2-8 | **First-run guidance** for the talent workspace, the CV studio and vendors. | `fm/_macros.html` (`empty`) | a new user of each sees what to do first |

## Track 3: AI as the connective tissue (needs Track 1)

Read the "AI changes" section of `CLAUDE.md` first: every item must hold on both providers and read as an expert, not a form.

| ID | Item | Where | Done when |
|---|---|---|---|
| T3-1 | **One agent platform.** The vendor assistant has no context layers and no tool selector; some model calls bypass the provider layer with hard-coded models (`cv_ingest.py`, `insights_engine.py`, `app1/build_index.py`, RAG rerank). | `vendor_portal.py`, `ai_tool_registry.py`, `ai_provider.py` | all three assistants share layers, selector, grounding and confirm flow; one place names models |
| T3-2 | **Talent tools, remaining.** Done: the learner assistant reads the person's development (`get_my_development`) and shares a goal behind a confirm card (`share_learning_goal`); the HR assistant's employee lookup shows goals and role readiness. Open: learner writes (self-assessment, linking a goal to requirements, applying to an opportunity) and HR writes and overviews (role frameworks, hiring pipeline, capability scenarios), all confirm-gated through `ai_actions`; `/hr/talent` has no page hints (`_HR_PAGE_TOOLS`, `sse_events.HR_DESTINATIONS`); no chat card for the development overview. | `app1/tools.py`, `hr_tools.py`, `talent_service.py`, `ai_tool_registry.py` | the assistants can act in the talent workspace, not only read it |
| T3-3 | **Coverage of the remaining surfaces:** reports, mail recovery, onboarding readiness, billing and credits hints; contextual entry on talent, settings and admin pages; help articles for HR and vendors (all 13 are for employees). | `hr_tools.py`, `hr_dashboard` (`_HR_PAGE_TOOLS`), `app1/sse_events.py`, `app1/help_kb/` | the assistant on any page can explain and act on that page |
| T3-4 | **Answers that match the tools.** The November probe found a confirm card described as saved, prices not in tool output, and "I can't see results" after a search ran. Add a verification pass against the turn's tool results. | `grounding.py`, `ai_runtime.py`; [reviews/2026-11-chat-probe/README.md](reviews/2026-11-chat-probe/README.md) | those probe cases pass on both providers |
| T3-5 | **Company documents, remaining.** Upload, company-scoped search and the assistant tools exist (DECISIONS.md, item 32). Open: Word files (`.docx`) and scanned PDFs are refused; retrieval is lexical only, so a question phrased with none of the document's words finds nothing (measure on real customer documents before adding embeddings); no help article for HR on managing documents; the eval has no cross-tenant document case that runs against a live model. | `company_documents.py`, `ai_eval/golden_set.json`, `app1/help_kb/` | HR can upload the formats customers actually have, and retrieval quality is measured |
| T3-7 | **Cross-role case.** Learner request, HR approval and vendor booking are joined only by data; give each assistant the same view of where a request stands and who acts next. | `ai_entity_context.py`, `ai_followups.py` | any of the three can answer "hvor er den?" identically |
| T3-8 | **Evals that gate.** Golden set: employee 66, HR 4, vendor 3. Grow HR and vendor, run both providers, and turn reviewed thumbs-down turns into cases (`set_feedback_reviewed` feeds nothing today). | `ai_eval/` | a quality regression fails a build |
| T3-9 | **A default cost ceiling per run.** Turns are rate-limited per actor and a run stops after `AI_MAX_TOOL_CALLS`, but `AI_MAX_RUN_COST`/`AI_MAX_RUN_TOKENS` still default to unlimited. Pick defaults from real `ai_agent_runs` data so a normal long answer is never cut. | `ai_runtime.py` (`max_run_cost_usd`, `max_run_tokens`) | a single run cannot cost more than an agreed amount |

## Track 4: maintainability and reach

| ID | Item | Where | Done when |
|---|---|---|---|
| T4-1 | **Multi-worker state** (only if T0-1 ends with more than one process): `rate_limit._events` and `perf_cache._STORE` are per process. | `rate_limit.py`, `perf_cache.py` | limits and caches are shared (MySQL or Redis) |
| T4-2 | **Split the largest files.** `hr_dashboard/__init__.py` holds 91 routes as closures in one factory; `hr_tools.py` holds domain logic eight modules import (`SKILL_LEVEL_MAP`, compliance derivation). | those files | domain logic lives in service modules; route groups are separate files |
| T4-3 | **Data access.** 600+ raw `cursor()` sites; `db_tx.py` has no production caller. Adopt it in new and touched code. | `db_tx.py` | services use the context managers |
| T4-4 | **Wider lint.** `ruff.toml` selects seven rules. Add bugbear and the security subset; type hints on service contracts. | `ruff.toml` | CI enforces them |
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
