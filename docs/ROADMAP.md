# Roadmap: open work only

The older completion plans are in git history. The implemented launch journeys and their operational limits are documented in [runbooks/LAUNCH_WORKFLOWS.md](runbooks/LAUNCH_WORKFLOWS.md). A feature existing in code does not prove a deployed customer can complete it: run that acceptance checklist with a sandbox customer, actual SMTP recipients and a supplier before commercial launch. Standing product constraints live in [DECISIONS.md](DECISIONS.md); owner-only operational work lives in [USER_ACTIONS.md](USER_ACTIONS.md).

## Commercial launch gate

- Validate the deployed employee → HR → supplier → attendance → outcome journey.
- Agree the actual customer offer, pilot success criteria, support ownership and supplier handling. Record them in the customer handover screen.
- Confirm actual mail receipt and worker health; do not use an SMTP-success counter as inbox-delivery evidence.
- Future supplier-specific live inventory/reservation integrations and binary certificate storage remain optional extensions, not claims made by the current UI.

## How to read the tracks

The work below came out of a four-part audit (UX cohesion, domain/data seams, AI layer, operations). It is ordered by dependency, not by date: Track 0 first, then Track 1, then Tracks 2 and 3 side by side, Track 4 as needed. A track item is removed from this file when it is done; durable "why" moves to [DECISIONS.md](DECISIONS.md). Production is one server; multi-host work is out of scope.

Owner decisions that shape the tracks:

- **Vocabulary** is broad: natural synonyms stay (ordre/bestilling, mail/e-mail, leverandør/udbyder). Only words that mislead are fixed (DECISIONS.md, "Product vocabulary").
- **Goals** become one cross-platform record with an owner and a per-goal visibility. A learner's goal is private until the learner shares it; an HR-written goal is hidden from the learner until HR shares it.
- **Company documents** may be uploaded by HR and used by the assistants for that company only.
- **Proactive check-ins** may be delivered as notifications, per company opt-in and per-user mute.

## Track 0: safety net

| ID | Item | Where | Done when |
|---|---|---|---|
| T0-1 | **Apply the web server configuration** (owner). `deploy/gunicorn.conf.py` exists (threaded workers, stream-safe timeout); production still runs the bare command until the ServerHoster start command is changed. | [runbooks/DEPLOY.md](runbooks/DEPLOY.md), [USER_ACTIONS.md](USER_ACTIONS.md) section 3 | two chat streams and a page load run at once without a worker timeout |
| T0-2 | **Rotate leaked secrets and purge history** (owner only). | [USER_ACTIONS.md](USER_ACTIONS.md) section 1 | weekly full-history gitleaks is clean |
| T0-3 | **The AI eval reports somewhere.** The nightly eval still lives in GitHub Actions, ungated and `continue-on-error`; confirm it runs (secrets present) or move it next to the CI. | `.github/workflows/ai-eval-nightly.yml`, `.survhub/` | a failing scorecard reaches a person |
| T0-4 | **Seeded demo tenant.** The sandbox has two logins and nothing else. Seed a company with departments, employees, budgets, courses, orders in every status, a path and talent data. | `sandbox/` | one command gives a tenant on which the whole pilot checklist can be walked |
| T0-5 | **Browser journey tests** for the 12 pilot-acceptance steps in [runbooks/LAUNCH_WORKFLOWS.md](runbooks/LAUNCH_WORKFLOWS.md), on the seeded tenant. | new `tests/e2e/` (kept out of the offline suite) | employee → HR → supplier → attendance → outcome runs green unattended |
| T0-6 | **Data migrations on real MySQL.** `schema_registry.run_data_migrations` has only run on the SQLite harness. | `tests/test_launch_mysql.py` | the migrations run against MySQL 8 in CI from a legacy-shaped fixture |
| T0-7 | **Backup, restore and rollback runbook**, including the local files that exist only on the one disk (catalog overlay and drafts, embeddings, `static/uploads`, `static/brand_assets`). No staging environment exists; decide whether the sandbox compose file becomes one. | new `docs/runbooks/BACKUP_RESTORE.md` | a restore has been rehearsed once |
| T0-8 | **Front-end supply chain.** CDN scripts carry no integrity hash; `marked` (legacy chat shell) and React (mind map) are unpinned, and the mind map loads two React versions. | `templates/fm/mind_map.html`, `app1/templates/index.html`, `static/futurematch/assets/mind-map-support.js` | every CDN tag is pinned with SRI, one React |

## Track 1: one domain spine

| ID | Item | Where | Done when |
|---|---|---|---|
| T1-1 | **Domain events get in-process subscribers.** `event_bus.emit_event` only feeds the webhook outbox. Add a subscriber registry (run post-commit, failure-isolated) and move cross-module reactions onto it. | `event_bus.py` | a new reaction is one registered function, not an edit inside `order_service` |
| T1-2 | **Completion reaches talent, certificates and compliance.** A completed order does not persist "done" on a linked development-goal step (the HR view never computes it), does not touch `user_certifications`, and compliance only refreshes on render or daily. | `order_service._record_completion_side_effects`, `talent_service`, `talent_routes.workspace`, `cert_expiry_service`, `compliance_service` | completing one order visibly updates goal step, competencies, compliance, CV and the HR views with no manual step |
| T1-3 | **Talent notifies.** No notification is sent for a shared goal, a new application, a stage change, a manager assessment or a placement. | `talent_service.py`, `talent_routes.py`, `notification_service.py` | each of those reaches the right person once, with an `action_url` |
| T1-4 | **Placement syncs the person.** `confirm_placement` only writes talent tables; `company_users.job_title`/`department` and `user_profile_summary.target_role` keep the old values. | `talent_service.confirm_placement` | profile, CV and HR employee list agree after a placement |
| T1-5 | **One skills read model.** Self and manager levels are merged separately in `hr_dashboard`, `hr_tools`, `insights_engine`, `cv_service` and `app1/tools.py`; three label scales are in use (`competency.score_to_level`, `api._SKILL_LEVEL_MAP`, the 1-5 talent scale). | `competency_model.snapshot` as the only reader | one function answers "what level is this person at", one label set |
| T1-6 | **One goal record** with owner and visibility (see decisions above). Today: `employee_goals` (no code writes its `status`), `user_learning_goals`, `talent_goal_links`, free-text `user_profile_summary.goals`, AI memory facts. | new goal service; `goal_sharing.py`, `app1/user_profile_db.py`, `talent_service.py`, `learner_context.py` | every surface and assistant reads one goal list; a private goal never reaches HR pages, HR tools, analytics or exports (test-pinned) |
| T1-7 | **One learning-path model.** AI-built `user_learning_paths` run beside HR `learning_paths`/`employee_learning_progress`. | `app1/user_profile_db.py`, `learning_path_service.py` | a personal path and an assigned path are the same object with a different owner |
| T1-8 | **One KPI module.** Spend excludes cancelled+rejected in `hr_dashboard`/`insights_engine` but only cancelled in `hr_tools`; revenue differs between `admin_reports` and `admin_dashboard`; "completed" reads `completion_status` in some places and `status` in others. | new `kpi_service.py`; callers above, `multitenant_reports.py`, `enterprise_api` | each KPI has one SQL definition and a test that the pages and the HR assistant agree |
| T1-9 | **All business mail through the outbox.** Direct `send_branded_email` remains in `compliance_service`, `digest_service`, `admin_dashboard`, `gdpr_routes`, `vendor_portal` and the three `send_employee_welcome` callers. Auth mail stays direct on purpose. | `mail_delivery.py` | a stopped SMTP server loses none of them |
| T1-10 | **Schema invariant finished.** `CREATE TABLE` outside the registry: 17 in `app1/user_profile_db.py`, more in `scheduler`, `event_bus`, `enterprise_api`, `ai_runtime`, `login_guard`, `email_service`, `cv_parse_store`. `user_skills` and `user_learning_goals` have no `company_id`. | `schema_registry.py`, `enterprise_tables.py` | a test fails on a `CREATE TABLE` outside the two homes |
| T1-11 | **Dead report inputs.** `reports.py` reads `app_usage` and `social_metrics`, which nothing writes. | `reports.py` | the page shows real data or is merged into the reporting that has it |

## Track 2: one product experience (needs Track 1)

| ID | Item | Where | Done when |
|---|---|---|---|
| T2-1 | **The learner loop is linked end to end:** gap → course → order → completion → competency → CV → internal opportunity. Today a gap links one generic catalog search, the order page has no way back to `/udvikling`, and exploring a role only offers "Lav et udviklingsmål". | `templates/fm/talent_workspace.html`, `my_order.html`, `my_cv.html`, `employee_home.html` | each step offers the next one |
| T2-2 | **One home per role, complete sidebar.** `/dashboard` (manager landing) has no sidebar entry; `/muligheder` and the hiring team are reachable only from inside the talent workspace; learners have no learning-path list; `talent.hiring`, `learning.assignment` and `dashboard.dashboard` are missing from `SIDEBAR_BY_ENDPOINT`. | `futurematch_ui.py`, `templates/fm_base.html` | every page is reachable in two clicks and lights one entry |
| T2-3 | **Vocabulary collisions fixed** (rule in DECISIONS.md). | templates, `shell.js` palette labels, `app1/sse_events.py` | the lint test passes |
| T2-4 | **Vendor portal on the shared shell.** Nine standalone pages without theme toggle, notifications or search; `vendor_course_edit.html` has no navigation. | `templates/fm/vendor_*.html`, `fm/_vendor_nav.html` | vendor pages extend one base |
| T2-5 | **Legacy pages retired** (merged and redirected, per "complete, don't remove"): `/brands` (`templates/profile.html`, `add_brand.html`), the `app1` demo shell and its second `chat.css`, the duplicate admin log, the duplicate SSO login page. | `auth/__init__.py`, `app1/templates/`, `templates/sso_login.html` | one template per page |
| T2-6 | **Search that finds things.** The palette has no visible trigger and `/api/search` covers only employees, companies and users. Add courses, own orders, goals and pages, permission-scoped. | `search_api.py`, `static/futurematch/assets/shell.js`, `templates/fm_base.html` | a learner finds a course and an order from the top bar |
| T2-7 | **Accessibility and theme pass.** Skip link and `aria-current` in the shell; 207 hardcoded colours and 2,500 inline styles make dark mode uneven. | `templates/fm_base.html`, `fm.css`, worst templates first | shell passes an automated a11y check; no hardcoded colour in shared partials |
| T2-8 | **First-run guidance** for the talent workspace, the CV studio and vendors. | `fm/_macros.html` (`empty`) | a new user of each sees what to do first |

## Track 3: AI as the connective tissue (needs Track 1)

Read the "AI changes" section of `CLAUDE.md` first: every item must hold on both providers and read as an expert, not a form.

| ID | Item | Where | Done when |
|---|---|---|---|
| T3-1 | **One agent platform.** The vendor assistant has no context layers and no tool selector; two vendor tools have no registry metadata; some model calls bypass the provider layer with hard-coded models (`cv_ingest.py`, `insights_engine.py`, `app1/build_index.py`, RAG rerank). | `vendor_portal.py`, `ai_tool_registry.py`, `ai_provider.py` | all three assistants share layers, selector, grounding and confirm flow; one place names models |
| T3-2 | **Talent tools** on the learner and HR assistants (readiness, roles, development goals, opportunities, applications, scenarios); writes confirm-gated through `ai_actions`. None exist today. | `app1/tools.py`, `hr_tools.py`, `talent_service.py` | "hvad mangler jeg for at blive X?" is answered from the role framework |
| T3-3 | **Coverage of the remaining surfaces:** reports, mail recovery, onboarding readiness, billing and credits hints; contextual entry on talent, settings and admin pages; help articles for HR and vendors (all 13 are for employees). | `hr_tools.py`, `hr_dashboard` (`_HR_PAGE_TOOLS`), `app1/sse_events.py`, `app1/help_kb/` | the assistant on any page can explain and act on that page |
| T3-4 | **Answers that match the tools.** The November probe found a confirm card described as saved, prices not in tool output, and "I can't see results" after a search ran. Add a verification pass against the turn's tool results. | `grounding.py`, `ai_runtime.py`; [reviews/2026-11-chat-probe/README.md](reviews/2026-11-chat-probe/README.md) | those probe cases pass on both providers |
| T3-5 | **Company documents.** HR uploads policies and handbooks; indexed per company; retrieved only for that company; answers cite the document; text is fenced as untrusted; tables covered by GDPR and retention; HR page to upload, replace and remove. | new `company_documents` service; `app1/rag.py`, `upload_guard.py`, `gdpr_service.py` | a learner asks about their own company's policy and gets a cited answer; a cross-tenant eval case passes |
| T3-6 | **Proactive check-ins as notifications**, opt-in per company (same pattern as `learning_deadline_reminders_enabled`), mute per user. Today they wait in a queue until the user opens the chat. | `profile_checkins.py`, `notification_service.py`, `scheduler.py` | an opted-in learner gets one relevant nudge that opens the chat on that topic |
| T3-7 | **Cross-role case.** Learner request, HR approval and vendor booking are joined only by data; give each assistant the same view of where a request stands and who acts next. | `ai_entity_context.py`, `ai_followups.py` | any of the three can answer "hvor er den?" identically |
| T3-8 | **Evals that gate.** Golden set: employee 63, HR 4, vendor 3. Grow HR and vendor, run both providers, and turn reviewed thumbs-down turns into cases (`set_feedback_reviewed` feeds nothing today). | `ai_eval/` | a quality regression fails a build |
| T3-9 | **Guardrails.** `AI_MAX_RUN_COST`/`AI_MAX_RUN_TOKENS` default to unlimited; logged-in `/app1/ask` and the HR/vendor assistants have no request rate limit. | `ai_runtime.py`, `rate_limit.py` | a runaway loop or a script cannot drain a company's credits |

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
