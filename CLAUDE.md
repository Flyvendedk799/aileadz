# CLAUDE.md

Futurematch / aileadz: Flask + MySQL learning/HR platform with AI assistants. Danish product, English code and docs. Start with [docs/README.md](docs/README.md) (index) and [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) (module map).

## Commands

```bash
py -m pytest -q                       # whole suite, offline, ~1 min (conftest sets SANDBOX=1 etc.)
py -m pytest tests/test_x.py -q -k name
py -m ruff check .                    # CI lint gate: syntax, undefined names, unused imports/variables
SANDBOX=1 AI_WARMUP_ON_IMPORT=0 py -c "from run import create_app; create_app()"   # boot smoke
```

Run the narrowest test that covers your change while iterating; run the full suite once before finishing. Test harnesses and which to pick: [docs/TESTING.md](docs/TESTING.md). Never write a test that needs network, an API key or MySQL.

## Working cheaply in this repo

- Several files are huge (`app1/tools.py` 6.5k lines, `hr_dashboard/__init__.py` 5.3k, `hr_tools.py` 4.3k, `app1/agent.py` 3.5k, `ai_runtime.py` 3k). Grep for the symbol, then read only that range. Do not read them whole.
- Every service module opens with a docstring stating its contract; read that first.
- Ids like `N-1.1` / `S-2.3` / `plan #16` in comments and docstrings refer to a finished roadmap that was removed from the tree (`git log --diff-filter=D -- docs/COMPLETION_PLAN.md`). They are provenance, not links. Open work is in [docs/ROADMAP.md](docs/ROADMAP.md).
- Dead code is removed rather than commented out; git has the history. Ruff enforces no unused imports/variables.
- `templates/fm/*.html` are also rendered by the admin-only design gallery (`/ui/<page>`), so a template without a route is not necessarily dead.

## Invariants (do not break; details in [docs/DECISIONS.md](docs/DECISIONS.md))

- **Orders:** all status/billing/money writes go through `order_service`; labels and transitions only from `order_lifecycle`. Never `UPDATE course_orders SET status` elsewhere.
- **Permissions:** guards and the role matrix live in `auth_decorators.py`; UI asks `capabilities.can("...")`. Do not compare role strings in views or templates. Vendors are an isolated session and never get `session['user']`.
- **Tenancy:** every query on company data is scoped by `company_id`. There are no foreign keys to rely on.
- **Privacy:** people-level analytics go through `kanon.py` (k-anonymity floors). A new per-user table must be covered by `gdpr_service` (export + erase/anonymise); `tests/test_gdpr_table_coverage.py` enforces it for the AI profile tables.
- **Schema:** one table definition, in `enterprise_tables.py` / `schema_registry.py`; creation is idempotent and runs lazily. No second copy.
- **Boot safety:** `create_app()` must never crash because an optional subsystem failed; register it in `try/except` and log. It must refuse to start without `SECRET_KEY` outside `SANDBOX=1`.
- **Catalog:** one source, `catalog_service`. Do not read the Shopify export directly.
- **Language:** user-facing strings are Danish (templates, flash messages, AI prompts, errors). Code, comments, docs, commit messages: English.
- **Secrets:** never commit keys; `.env.example` lists variables with empty values. gitleaks runs in CI (`.gitleaks.toml`).

## AI changes

The AI (employee advisor, profiler, HR and vendor assistants; OpenAI and Claude providers) must read as a fluent expert who reaches for tools when needed, never as a form-filler working through a checklist. Prefer need-driven phrasing over field-coverage phrasing in prompts, playbooks and chips, on **both** providers. Judge a change by whether a transcript reads like a conversation with an expert who happens to look things up. Architecture, env flags and tool registry: [docs/ai-framework.md](docs/ai-framework.md). A prompt may only name tools the selector can serve (`tests/test_prompt_tool_name_drift.py`).

## Git and deploy

- Branch from `main`, open a PR; CI = gitleaks + ruff + pytest on MySQL 8. Use conventional prefixes (`fix(scope):`, `feat(scope):`, `chore:`, `docs:`).
- Production is ServerHoster on a VPS behind a Cloudflare tunnel. Deploy, env vars, worker service, e-mail, secret rotation: [docs/runbooks/](docs/runbooks/DEPLOY.md). Manual owner-only steps: [docs/USER_ACTIONS.md](docs/USER_ACTIONS.md).
- Keep docs true: when a change alters a module's contract, an env var, a route or an invariant, update the matching doc in the same PR. Add a new doc only with an entry in `docs/README.md`.
