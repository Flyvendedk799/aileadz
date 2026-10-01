# Runbook: Deploy & Operations Index

Single entrypoint for operating aileadz. The app is a Flask app-factory
(`create_app()` in `run.py`) deployed on the VPS by ServerHoster (`gunicorn
wsgi:application`) plus a separate worker service (`python drain_worker.py --loop`,
see JOB_RUNNER.md). Tables self-create/extend at boot from the single definitions in
`enterprise_tables.py` + `schema_registry.py`; data fixes run once (flag rows in
`schema_meta`). Alembic (`migrations/`) is the forward path: dump the live schema
with `scripts/dump_schema_baseline.py`, review it, then `alembic stamp 0002_performance_indexes`
and `alembic upgrade head`. `wsgi_pythonanywhere.example.py` is legacy.

## Runbooks

| Runbook | When to use |
|---|---|
| [SECRET_ROTATION.md](SECRET_ROTATION.md) | Rotate `SECRET_KEY`, MySQL password, SSH password, OpenAI key. **Run now** — secrets leaked. |
| [GIT_HISTORY_PURGE.md](GIT_HISTORY_PURGE.md) | Scrub leaked secrets from git history. **Owner go-ahead required**; rewrites public history. |
| [JOB_RUNNER.md](JOB_RUNNER.md) | Set up the scheduled outbox-drain task (reliable webhook delivery, digests, retention, compliance reminders). |
| [EMAIL_SETUP.md](EMAIL_SETUP.md) | Configure an EU ESP/SMTP so branded order-confirmation / welcome emails actually send. |
| [CATALOG_REBUILD.md](CATALOG_REBUILD.md) | Rebuild the RAG catalog index (`app1/build_index.py`) — restores full hybrid search. |

## Environment variables the app reads

Set these in the WSGI file (and in scheduled-task / console environments where
relevant). Use `wsgi_pythonanywhere.example.py` as the template. **Never commit
real values.**

| Var | Purpose | Read at |
|---|---|---|
| `SECRET_KEY` | Flask session signing key. **Mandatory:** the app refuses to boot without a real value (S-1.4); only `SANDBOX=1` may fall back. | `run.py` (`resolve_secret_key`) |
| `TWOFA_ENFORCE_ADMIN` | `1` (default): platform admins must enrol TOTP 2FA. `0` is break-glass only. | `two_factor.py` |
| `TWOFA_ENFORCE_COMPANY_ADMIN` | `1` makes 2FA mandatory for company admins too (default off). | `two_factor.py` |
| `TWOFA_FERNET_KEY` | Optional dedicated key for stored TOTP secrets (else derived from `SECRET_KEY`). | `two_factor.py` |
| `CSP_ENFORCE` | `1` (default) enforces the Content-Security-Policy; `0` = report-only break-glass. | `security_headers.py` |
| `HSTS_MAX_AGE` / `HSTS_INCLUDE_SUBDOMAINS` | Strict-Transport-Security on HTTPS responses (default 1 year, subdomains off; `HSTS_MAX_AGE=0` disables). | `security_headers.py` |
| `HEALTH_TOKEN` | Shared secret a monitor sends as `X-Health-Token` to see the detailed `/readyz` body. | `health.py` |
| `DSR_NOTIFY_EMAIL` | Address that is e-mailed when a user requests erasure (falls back to `SUPPORT_EMAIL`). | `gdpr_routes.py` |
| `RETENTION_<NAME>_DAYS` | Override a retention rule (`0`/`off` disables), e.g. `RETENTION_EMAIL_LOG_DAYS=90`. See `retention_service.POLICIES`. | `retention_service.py` |
| `LOGIN_MAX_FAILURES` / `LOGIN_WINDOW_SECONDS` / `LOGIN_LOCKOUT_SECONDS` | Login lockout tuning (5 failures / 15 min / 15 min). | `login_guard.py` |
| `MAX_CONTENT_LENGTH_MB` | Global request body cap (default 26). | `run.py` |
| `MYSQL_HOST` / `MYSQL_USER` / `MYSQL_PASSWORD` / `MYSQL_DB` | Database connection | `run.py:156-159` |
| `MYSQL_PORT` | Optional DB port override | `run.py:165-166` |
| `OPENAI_API_KEY` | OpenAI access (chat, RAG, CV, insights, index build) | `ai_runtime.py:89`, `app1/__init__.py:59`, `app1/build_index.py:13`, `cv_ingest.py:196`, `insights_engine.py:181`, `app3/__init__.py:10` |
| `OUTBOX_DRAIN_TOKEN` | Shared secret for the outbox-drain endpoint | `enterprise_api/__init__.py:1881` |
| `MAIL_SERVER` / `MAIL_DEFAULT_SENDER` (+ Flask-Mail `MAIL_*`) | Transactional email backend | `email_service.py:15`, `:75-76`, `:205-206` |
| `SSO_FERNET_KEY` | SSO token encryption key (else derived from `SECRET_KEY`) | `enterprise_sso/__init__.py:86` |
| `AI_*` | AI runtime tuning — model selection, token/TPM budgets, embeddings, rate-limit/retry, cross-encoder, tracing, warmup | read across `ai_runtime.py` etc.; production defaults in `wsgi_pythonanywhere.example.py:14-44` |

`AI_*` set includes: `AI_MAIN_MODEL`, `AI_FAST_MODEL`, `AI_RUNTIME`,
`AI_MAX_INPUT_TOKENS`, `AI_MAX_OUTPUT_TOKENS`, `AI_MAX_TOOL_ITERATIONS`,
`AI_TPM_BUDGET`, `AI_RATE_LIMIT_RETRY_SECONDS`, `AI_RATE_LIMIT_COOLDOWN_SECONDS`,
`AI_OPENAI_TIMEOUT_SECONDS`, `AI_FEW_SHOT`, `AI_SUMMARY_MODE`,
`AI_EMBEDDING_MODEL`, `AI_EMBEDDING_DIMENSIONS`, `AI_RAG_CROSS_ENCODER`,
`AI_RAG_CROSS_ENCODER_MAX_CANDIDATES`, `AI_TRACE_SAMPLE_RATE`,
`AI_WARMUP_ON_IMPORT`, `AI_CONTEXT_ASSEMBLER`, `AI_CONTEXT_MAX_TOKENS`,
`AI_STEERING_PLACEMENT`, `AI_TOKEN_CHARS_PER_TOKEN`, `AI_SESSION_SUMMARY_MODE`,
`AI_USER_KNOWLEDGE`, `AI_USER_KNOWLEDGE_EMBEDDINGS`, `AI_LEARNER_HR_CONTEXT`,
`AI_LEARNER_HR_GOALS`, `AI_HELP_KB` (see `docs/ai-framework.md` §8).

> **Token budgets (2026-09 AI framework overhaul):** the code defaults rose to
> `AI_MAX_INPUT_TOKENS=36000` / `AI_TPM_BUDGET=42000`, but a WSGI file that pins
> the old `20000`/`26000` keeps the old, much tighter context. Update the deploy
> env deliberately after checking the OpenAI org TPM tier (and Anthropic ITPM if
> Claude is enabled); watch `ai_agent_runs` for 429s after the change.

> The sandbox uses `SANDBOX=1` plus `MYSQL_*` and the AI/OUTBOX envs; with
> `SANDBOX=1` the mandatory-`SECRET_KEY` check, secure cookies, HSTS and CSRF
> enforcement are relaxed. **Never set `SANDBOX=1` in production.**

## Health & readiness probes

| Probe | Path | Meaning |
|---|---|---|
| Liveness | `GET /healthz` | `{"status":"ok"}` 200; touches nothing (`health.py:91-95`). |
| Readiness | `GET /readyz` | Reports `db` / `catalog` / `openai` (+ optional `features`). **200 when `db` is true, 503 otherwise** (`health.py:97-116`). |

`/readyz` interpretation:
- `"db": false` → DB credentials/connection broken (the only thing that returns 503).
- `"catalog": false` → RAG index file missing → see `CATALOG_REBUILD.md`.
- `"openai": false` → `OPENAI_API_KEY` unset → AI features degraded.

## Deploy checklist

1. Pull/deploy code to the PythonAnywhere host.
2. Confirm all required env vars are set in the WSGI file (table above).
3. Reload the web app (Web tab → Reload).
4. `GET /healthz` → 200.
5. `GET /readyz` → 200 with `db: true`; check `catalog` / `openai` flags.
6. If `catalog: false` → run `CATALOG_REBUILD.md`.
7. Confirm the outbox-drain scheduled task exists and returns 200
   (`JOB_RUNNER.md`).
8. If secrets were ever leaked/rotated, confirm `SECRET_ROTATION.md` is complete.
9. **One-off (AI framework overhaul):** after the first boot has created the new
   conversation-state columns, add the unique conversation key in a quiet window
   — it can rebuild `conversation_history`, which is why it is not done at boot:
   `python scripts/migrate_conversation_unique.py --dry-run`, then without
   `--dry-run`. Duplicates are renamed (`…#dup<id>`), never deleted. The app runs
   correctly before this step; the key only closes a rare double-insert race.
10. Optional: build the platform-help embedding index with
    `python -m app1.help_kb --build` (keyword search works without it).
