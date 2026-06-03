# Runbook: Deploy & Operations Index

Single entrypoint for operating aileadz. The app is a Flask app-factory
(`create_app()` in `run.py`) deployed on PythonAnywhere via a WSGI file
(template: `wsgi_pythonanywhere.example.py`). Enterprise tables self-create
idempotently on `before_request`. There is no separate migration step.

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
| `SECRET_KEY` | Flask session signing key | `run.py:134` |
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
`AI_WARMUP_ON_IMPORT`.

> The sandbox uses `SANDBOX=1` plus `MYSQL_*` and the AI/OUTBOX envs; with
> `SANDBOX=1` the insecure `SECRET_KEY` warning and secure-cookie enforcement are
> relaxed (`run.py:135`, `run.py:149`).

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
