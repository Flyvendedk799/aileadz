# Architecture and module map

Single Flask app (`run.create_app()`), Python 3.12, MySQL via a PyMySQL shim (`flask_mysqldb` if installed). Flat layout: most modules live at the repo root, big surfaces are packages. All user-facing strings are Danish; code, comments and docs are English.

For the AI internals see [ai-framework.md](ai-framework.md). For standing constraints see [DECISIONS.md](DECISIONS.md). For tests see [TESTING.md](TESTING.md).

## Request lifecycle

`wsgi.py` -> `run.create_app()` -> blueprints (below) -> `before_request` hooks that lazily and idempotently create schema (`branding_service`, `enterprise_tables`, goal sharing / DSR / 2FA / password-token tables, `performance_indexes`) -> view -> `after_request` hooks (security headers, gzip, opportunistic `scheduler.run_due_jobs_safe`).

- **DB access:** `current_app.mysql.connection.cursor(MySQLdb.cursors.DictCursor)`, `%s` placeholders, `autocommit=False`, explicit `commit()`. `db_tx.py` has transaction/read-cursor context managers (used by tests only so far). Tenant isolation is by `company_id` in every query; there are no foreign keys.
- **Schema:** table definitions live in `enterprise_tables.py` (one definition per table) and `schema_registry.py`; `schema_registry.verify_schema` reports drift. `migrations/` is Alembic with a baseline; see [../migrations/README.md](../migrations/README.md).
- **Boot guards:** `create_app()` refuses to start without `SECRET_KEY` unless `SANDBOX=1`. Optional subsystems register inside `try/except` and log a warning instead of crashing boot (`feature_status.py` reports what degraded).
- **Auth:** `auth_decorators.py` is the single place for guards and the role matrix (`login_required`, `require_role`, `require_company`, `require_company_role`, `require_capability`, `requires_feature`). Roles: `admin` (platform), `company_admin`, `hr_manager`, `department_head`, learner. Templates and nav ask `capabilities.can("company.approvals")`, never compare role names. Vendors have an isolated session (`vendor_auth.py`) and never get `session['user']`.
- **Background work:** `scheduler.py` holds the `JOBS` registry and the `scheduled_job_runs` table. Driven by `drain_worker.py` (`--loop` worker or single pass) and, best-effort, an `after_request` hook (`SCHEDULER_OPPORTUNISTIC=0` turns it off). See [runbooks/JOB_RUNNER.md](runbooks/JOB_RUNNER.md).
- **Integration events:** `event_bus.emit_event` writes to the `event_outbox` table; `drain_outbox` delivers to company webhooks (HMAC-signed by `webhook_signing.py`, SSRF-guarded by `safe_http.py`).

## The core domain: orders

`pending_approval -> approved -> booked -> completed` (+ `rejected`, `cancelled`). All writers go through `order_service` (`set_status`, `complete_order`, `set_billing_status`); labels, tones and transitions come only from `order_lifecycle`. Billing is a separate axis (`not_invoiced -> invoiced -> paid`); payment happens off-platform. Completing an order feeds `completion_service` -> `competency` / `skill_history` (skills grow) and notifications.

## Module map

### App shell and cross-cutting
| Module | Role |
|---|---|
| `run.py`, `wsgi.py` | app factory, blueprint registration, hooks; WSGI entrypoint (ProxyFix, www -> apex redirect) |
| `auth_decorators.py`, `capabilities.py`, `login_guard.py`, `two_factor.py`, `password_policy.py`, `password_tokens.py`, `account_flows.py`, `csrf_protect.py`, `impersonation.py`, `identity.py`, `oidc.py` | authN/authZ, lockout, TOTP, reset/invite tokens, CSRF, admin impersonation |
| `security_headers.py`, `html_sanitize.py`, `upload_guard.py`, `safe_http.py`, `rate_limit.py`, `security_audit.py` | defensive headers, render-time sanitising, upload checks, SSRF-safe HTTP, in-process rate limiter |
| `error_pages.py`, `health.py` (`/healthz`, `/readyz`), `observability.py`, `feature_status.py` | error pages, probes, request ids / structured logs, degraded-subsystem status |
| `perf_cache.py`, `response_compression.py`, `asset_version.py`, `performance_indexes.py` | TTL cache, gzip, content-hash `?v=` for static files, idempotent DB indexes |
| `notification_service.py`, `email_service.py` | one notification table; branded transactional email (SMTP) |
| `scheduler.py`, `drain_worker.py`, `event_bus.py` | job registry + worker, webhook outbox |

### Learner experience
| Module | Role |
|---|---|
| `futurematch_ui.py`, `templates/fm/` | main learner/HR/admin UI blueprint and the design gallery (`/ui`, admin only; renders any `templates/fm/<page>.html`) |
| `catalog_service.py`, `catalog_routes.py`, `catalog_admin_routes.py`, `catalog_freshness.py`, `shopify_sync.py` | the ONE course catalog (source file `CATALOG_SOURCE_FILE`, vendor submissions, Shopify sync) |
| `learner_orders.py`, `learner_context.py`, `completion_service.py`, `competency.py`, `skill_history.py`, `learning_path_service.py`, `goal_sharing.py`, `goal_sharing_ui.py` | learner order detail, learner view of own HR data, completion moment, skills |
| `cv_ingest.py`, `cv_parse_store.py` | CV / job-ad ingestion for the profiler |
| `dashboard/`, `pages.py`, `api.py` | learner dashboard, static-ish pages, `/api/...` JSON endpoints for the UI (notifications, profile, credits, CV upload/parse, learner events) |

### HR, company and enterprise
| Module | Role |
|---|---|
| `hr_dashboard/` (largest file), `hr_ext.py`, `hr_course_assign.py`, `compliance_*.py`, `deadline_service.py`, `cert_expiry_service.py`, `digest_service.py`, `department_service.py`, `team_order_policy.py`, `bulk_invite.py` | HR workspace, assignments, compliance, reminders, weekly digest |
| `companies/`, `settings_hub.py`, `enterprise_company_settings.py`, `branding_service.py`, `seat_governance.py` | company admin, settings hub (`/virksomhed/indstillinger`), white-label, seats |
| `enterprise_api/`, `enterprise_sso/`, `scim_api.py`, `scim_groups.py`, `api_keys_ui.py` | public API v1 + OpenAPI, OIDC SSO, SCIM 2.0, API keys |
| `enterprise_analytics/`, `multitenant_reports.py`, `report_query.py`, `report_exports.py`, `scheduled_reports.py`, `reports.py`, `admin_reports.py`, `benchmarking.py`, `insights_engine.py`, `kanon.py` | analytics and reporting; `kanon.py` enforces k-anonymity floors |
| `order_service.py`, `order_lifecycle.py`, `billing_service.py`, `credit_service.py`, `credit_routes.py` | orders, billing view, AI-usage credits |
| `gdpr_service.py`, `gdpr_routes.py`, `dsr_service.py`, `retention_service.py` | GDPR export/erasure, data-subject requests, retention |

### Platform admin and vendors
`admin_dashboard.py`, `admin_notifications.py`, `admin_lists.py` (platform admin); `vendor_portal.py`, `vendor_auth.py`, `vendor_conversations.py`, `vendor_tools.py` (vendor self-service + vendor AI assistant).

### AI
| Module | Role |
|---|---|
| `app1/` | employee AI advisor and profiler: `agent.py` (prompts + turn loop), `tools.py` (tool implementations), `rag.py` (hybrid retrieval), `memory_store.py`, `user_knowledge.py`, `user_profile_db.py`, `conversation_state.py`, `help_kb.py` + `help_kb/*.md`, `order_handler.py`, `sse_events.py`; routes under `/app1` (`/ask` SSE, `/widget/<token>`) |
| `ai_runtime.py`, `ai_provider.py`, `ai_provider_anthropic.py` | shared tool-loop runtime; OpenAI <-> Claude provider switch (`AI_PROVIDER` setting) |
| `ai_tool_registry.py`, `anon_migration.py` (anonymous -> logged-in memory), `ai_context.py`, `ai_context_layers.py`, `ai_cost_model.py`, `ai_secrets.py`, `ai_reply.py`, `grounding.py`, `tool_confirm.py` | tool selection policy, context assembly, cost model, encrypted provider keys, grounding/prompt-injection hardening, confirm-before-mutate |
| `hr_agent.py`, `hr_tools.py`, `hr_conversations.py` | HR assistant and its tools |
| `ai_eval/` | golden-set quality eval harness (see [../ai_eval/README.md](../ai_eval/README.md)) |

## Big files: grep, do not read whole

| File | Lines |
|---|---|
| `app1/tools.py` | ~6.5k |
| `hr_dashboard/__init__.py` | ~5.3k |
| `hr_tools.py` | ~4.3k |
| `app1/agent.py` | ~3.5k |
| `enterprise_api/__init__.py` | ~3k |
| `ai_runtime.py` | ~3k |
| `app1/__init__.py` | ~2.4k |

Find a symbol with Grep, then read only that range. Module docstrings at the top of every service module state its contract.

## Static assets and templates

- `templates/fm/` — all current pages (extend `templates/fm_base.html`); `app1/templates/` — legacy chat shell, widget, admin log; `templates/*.html` — a few top-level pages.
- `static/futurematch/assets/` — `fm.css`, `fm-pages.css`, `chat.css`, `shell.js`, `chat.js`, `ai-stream.js`, `ai-sidebar.js`, `fm-charts.js`, ... Reference with `?v={{ asset_version('futurematch/assets/x.js') }}`.

## Tooling

- Lint: `ruff check .` (gate in CI: syntax, undefined names, redefinitions, unused imports/variables; see `ruff.toml`).
- CI: `.github/workflows/ci.yml` (gitleaks, ruff, boot smoke + pytest against MySQL 8), `security.yml`, `ai-eval-nightly.yml`.
- Local MySQL sandbox: [../sandbox/README.md](../sandbox/README.md).
