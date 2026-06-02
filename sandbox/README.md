# Futurematch local sandbox

A throwaway, **isolated** environment for running and testing the app — including
a real SQL database — without touching the production PythonAnywhere DB.

It spins up MySQL 8 in Docker, points the Flask app at it via env vars, creates
the full schema with the app's own table-creation code, and seeds a test login.
Use it to actually exercise the AI harness (chat, tool calls, profile saves).

## Prerequisites
- Docker Desktop running
- The app's Python deps already installed (the same `python3` that runs the app)
- An OpenAI API key for live AI calls (the DB-only flows work without it)

## Quick start
```bash
cd sandbox
export OPENAI_API_KEY=sk-...        # optional, only needed for live AI answers
./sandbox.sh up      # start MySQL on 127.0.0.1:3307
./sandbox.sh init    # create all tables + seed test data
./sandbox.sh run     # http://127.0.0.1:5001  (login: test / test)
```
Then open http://127.0.0.1:5001/login and sign in:
- **test / test** — admin
- **medarbejder / test** — employee

## Non-interactive end-to-end check
```bash
./sandbox.sh smoke
```
Logs in as the test user and exercises: profile fetch, add-skill, add-education,
and (if `OPENAI_API_KEY` is set) a real `/app1/ask` chat call.

## How it works
- `docker-compose.yml` — MySQL 8 on host port **3307**, db `futurematch_sandbox`,
  user `fm` / `fm`. Data persists in the `fm_sandbox_data` volume.
- `init.sql` — creates the legacy `users` + `brands` tables (the app doesn't
  auto-create these) and seeds the two test logins.
- `.env.sandbox` — sets `MYSQL_*` to the sandbox DB. `run.py` reads these env
  vars and falls back to production only when they're unset, so **production is
  never affected** by the sandbox.
- `run_sandbox.py` — loads the env, builds the app (no SSH tunnel), runs the
  app's `ensure_*_tables()` to create the ~50 managed tables, seeds a company,
  and serves / smoke-tests.

## Common commands
| Command | What it does |
|---|---|
| `./sandbox.sh up` | start the DB container |
| `./sandbox.sh init` | create schema + seed |
| `./sandbox.sh run` | run the app (port 5001) |
| `./sandbox.sh smoke` | automated login + profile + chat check |
| `./sandbox.sh mysql` | open a SQL shell on the sandbox DB |
| `./sandbox.sh logs` | tail MySQL logs |
| `./sandbox.sh down` | stop (keeps data) |
| `./sandbox.sh reset` | wipe + recreate the DB |

## Notes
- The catalog/RAG uses the `app1/shopify_products_*.json` files in the repo, so
  course search works without seeding products.
- Passwords are stored plaintext in seed data on purpose — the auth layer accepts
  plaintext and upgrades to a hash on first login.
- Set `OPENAI_API_KEY` in your shell or in `.env.sandbox` (shell wins).
- Nothing here connects to production. To talk to prod, run the normal `run.py`.
