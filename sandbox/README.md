# Futurematch local sandbox

A throwaway, isolated environment: MySQL 8 in Docker plus the Flask app pointed at it, so you
can exercise the app (including the AI harness) against a real SQL database without touching
production. The schema is created by the app's own table-creation code and two test logins are
seeded.

## Prerequisites

- Docker running.
- The app's Python deps (`pip install -r requirements.txt`) in the interpreter `sandbox.sh` uses:
  `python3` by default, override with `PYTHON=python ./sandbox.sh ...` (e.g. on Windows).
- An OpenAI API key for live AI answers (DB-only flows work without it).

## Quick start

```bash
cd sandbox
export OPENAI_API_KEY=sk-...        # optional
export TWOFA_ENFORCE_ADMIN=0        # see "Gotchas": the seeded admin would be forced to enrol 2FA
./sandbox.sh up      # MySQL on 127.0.0.1:3307
./sandbox.sh init    # create all tables + seed
./sandbox.sh run     # http://127.0.0.1:5001
```

Open http://127.0.0.1:5001/login: `test` / `test` (platform admin, also `company_admin` of the
seeded company "Sandbox A/S", slug `sandbox`) or `medarbejder` / `test` (employee).

## Commands

| Command | What it does |
|---|---|
| `./sandbox.sh up` | start the DB container and wait until it answers |
| `./sandbox.sh init` | `python run_sandbox.py init`: ensure schema, seed company + membership |
| `./sandbox.sh run` | ensure schema, serve the app (debug, no reloader) on `SANDBOX_PORT` (5001) |
| `./sandbox.sh smoke` | login + profile + add-skill + add-education, and `/app1/ask` if `OPENAI_API_KEY` is set |
| `./sandbox.sh mysql` | SQL shell on the sandbox DB |
| `./sandbox.sh logs` | tail MySQL logs |
| `./sandbox.sh down` | remove the container, keep the `fm_sandbox_data` volume |
| `./sandbox.sh reset` | destroy the volume and recreate the DB (re-runs `init.sql`) |

## How it works

- `sandbox.sh` starts `mysql:8.0` with plain `docker run`; `docker-compose.yml` is an equivalent
  alternative (`docker compose up -d`). Both: host port 3307, db `futurematch_sandbox`, user
  `fm`/`fm` (root password `rootpw`), volume `fm_sandbox_data`, utf8mb4.
- `init.sql` runs only when the container is first created: it creates the legacy `users` and
  `brands` tables (the app does not create them) and inserts the two logins.
- `.env.sandbox` (tracked) sets `MYSQL_*` to that DB, `SANDBOX=1`, `ENTERPRISE_TABLE_SYNC_FORCE=1`,
  `AI_WARMUP_ON_IMPORT=0`, `SANDBOX_PORT`, and an empty `OPENAI_API_KEY`. `run_sandbox.py` loads it,
  then a repo-root `.env`; variables already exported in your shell win.
- `run_sandbox.py init` calls `enterprise_tables.ensure_enterprise_tables`,
  `branding_service.ensure_branding_schema` and `app1.user_profile_db.ensure_tables`, then seeds the
  company. The remaining ensure-hooks (security schema, performance indexes, ...) run on the first
  request, as in production.
- Production is untouched: `run.py` has no production DB fallback (it defaults to localhost), and the
  sandbox sets `MYSQL_*` explicitly. `SANDBOX=1` relaxes the mandatory `SECRET_KEY`, secure cookies,
  HSTS and CSRF enforcement; never use it in production.

## Gotchas

- Seed passwords are plaintext. Login only accepts hashed passwords; the legacy rows are hashed by
  `auth._migrate_plaintext_passwords_once` on the first request of a running (non-`TESTING`) app. Load
  `/login` once via `./sandbox.sh run` before relying on `smoke`: `smoke` sets `TESTING=True`, which
  skips that migration, and its login line prints OK for any 302.
- Platform admins must enrol TOTP 2FA (`TWOFA_ENFORCE_ADMIN`, default 1), so the seeded `test` admin is
  redirected to enrolment unless you set `TWOFA_ENFORCE_ADMIN=0` (export it or add it to `.env.sandbox`).
- The catalog files (`app1/shopify_products_all_pages.json`, `app1/shopify_products_augmented.json`)
  are git-ignored, so a fresh clone has an empty catalog: place an export there, point
  `CATALOG_SOURCE_FILE` at one, or import a CSV in the admin catalog.

## Tests

- The unit/regression suite lives in `tests/` and is run from the repo root: `python -m pytest -q`
  (`pytest.ini`: `testpaths = tests`; `tests/conftest.py` sets `SANDBOX=1` and other safe defaults, and
  makes DB connections fail fast when no MySQL is reachable). Install `pytest pytest-timeout` first;
  they are not in `requirements.txt`. CI (`.github/workflows/ci.yml`) runs it against the same MySQL
  image and credentials as this sandbox, with `MYSQL_HOST=127.0.0.1 MYSQL_PORT=3307 MYSQL_USER=fm
  MYSQL_PASSWORD=fm MYSQL_DB=futurematch_sandbox ENTERPRISE_TABLE_SYNC_FORCE=1`. Export those against
  `./sandbox.sh up` to also run the fresh-database bootstrap test in `tests/test_schema_baseline.py`.
- `sandbox/test_ai.py` and `sandbox/test_ai_edge.py` are standalone scripts (not collected by pytest):
  `python test_ai.py [query]` and `python test_ai_edge.py` drive `/app1/ask` through the real AI engine
  against the sandbox DB. They need `OPENAI_API_KEY`, a running sandbox DB and `./sandbox.sh init`.
