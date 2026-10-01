# Runbook: Secret Rotation

How to rotate every credential aileadz depends on. Run it whenever a secret may have
leaked. Old credentials (a DB password, an SSH password, a Shopify Admin token, the
placeholder session key) were committed to git in the past and are still in history
(see `GIT_HISTORY_PURGE.md`), so treat anything that was ever valid as public. No real
values appear here; set them in the ServerHoster environment of BOTH the web and the
worker service (`.env.example` lists every variable).

## What the app reads

| Secret | Variable | Read by | Notes |
|---|---|---|---|
| Flask session key | `SECRET_KEY` | `run.py` (`resolve_secret_key`) | Boot fails if unset or a known placeholder. Several Fernet keys derive from it. |
| DB credentials | `DATABASE_URL` or `MYSQL_HOST/PORT/USER/PASSWORD/DB` | `run.py` | No hardcoded fallback any more. |
| OpenAI key | `OPENAI_API_KEY` | `ai_secrets.py`, `app1/`, `cv_ingest.py`, `insights_engine.py`, `ai_runtime.py` | Resolution is **DB first, env second**. |
| Anthropic key | `ANTHROPIC_API_KEY` | `ai_secrets.py`, `ai_provider_anthropic.py` | Same DB-then-env resolution. |
| AI-key encryption key | `AI_SECRET_KEY` | `ai_secrets.py` | Fernet key for keys stored via `/admin/ai-settings` (`ai_secrets` table); else derived from `SECRET_KEY`. |
| SSO client-secret key | `SSO_FERNET_KEY` | `enterprise_sso/__init__.py` | Else derived from `SECRET_KEY`. |
| TOTP-secret key | `TWOFA_FERNET_KEY` | `two_factor.py` | Else derived from `SECRET_KEY`. |
| Shopify Admin token | `SHOPIFY_ADMIN_TOKEN` | `shopify_sync.py` | Plus `SHOPIFY_STORE`. |
| SMTP password | `MAIL_PASSWORD` / `SMTP_PASSWORD` | `email_service.py` | |
| Monitor / drain tokens | `HEALTH_TOKEN`, `OUTBOX_DRAIN_TOKEN` | `health.py`, `enterprise_api` | Shared secrets: change on both sides. |
| Sentry / Redis | `SENTRY_DSN`, `REDIS_URL` | `observability.py`, `perf_cache.py` | Credentials may be embedded in the URL. |
| Dev SSH tunnel | `SSH_PASSWORD` / `SSH_PKEY` | `run.py` `main()` | Local development only. |
| CI | repo secrets `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, `EVAL_VENDOR_EMAIL`, `EVAL_VENDOR_PASSWORD` | `.github/workflows/ai-eval-nightly.yml` | GitHub -> Settings -> Secrets. |

## Recommended order

1. Database password (direct data access).
2. Third-party API keys: OpenAI, Anthropic, Shopify, SMTP, Sentry, GitHub Actions secrets.
3. `SECRET_KEY` last (forces one re-login for everybody; see below).

Each step: change the secret at the provider, set the variable on web AND worker,
restart both, verify.

### 1. Database password

Change the password of the DB user (ServerHoster-managed MySQL or your own), then update
`DATABASE_URL` (the URL-encoded password) or `MYSQL_PASSWORD` on both services and restart.
Check that `/readyz` shows `"db": true` (admin session or `X-Health-Token`).

### 2. API keys

- OpenAI / Anthropic: create the new key, revoke the old one. **Check `/admin/ai-settings`
  first:** a key stored there takes precedence over the env var, so updating only the env
  var changes nothing. Replace the key in the UI (or clear it there so the env var applies).
  `/readyz` `openai: true` and `ai` block confirm the key resolves.
- Shopify: rotate the Admin API token in Shopify, update `SHOPIFY_ADMIN_TOKEN`; confirm with
  "Synkronisér fra Shopify" in Katalogadmin.
- SMTP: change at the provider, update `MAIL_PASSWORD` / `SMTP_PASSWORD`, use "Send test-mail"
  on Admin -> Systemstatus.

### 3. SECRET_KEY (last)

1. Generate: `python -c "import secrets; print(secrets.token_hex(32))"`.
2. **Before** starting the app on the new key, re-encrypt what is encrypted with a key derived
   from `SECRET_KEY` (AI provider keys, SSO client secrets, TOTP secrets) with the same DB
   environment the app uses:
   ```
   OLD_SECRET_KEY=<old> NEW_SECRET_KEY=<new> python scripts/rotate_secret_key.py          # dry run
   OLD_SECRET_KEY=<old> NEW_SECRET_KEY=<new> python scripts/rotate_secret_key.py --apply
   ```
   The report lists `seen/rotated/unreadable` per family and never prints a secret. A family
   whose dedicated key is set (`AI_SECRET_KEY`, `SSO_FERNET_KEY`, `TWOFA_FERNET_KEY`) is
   skipped because it does not depend on `SECRET_KEY`.
3. Set `SECRET_KEY` on web and worker, restart both.

> Rotating `SECRET_KEY` invalidates every session cookie: all users must sign in again. Pick a
> quiet window. To decouple the stored-secret families from it for good, set the dedicated
> Fernet keys; rotating a dedicated key has no script (re-save the AI keys in
> `/admin/ai-settings`, re-save SSO client secrets, users re-enrol 2FA).

## Verify

1. `GET /healthz` returns 200. `GET /readyz` with `X-Health-Token` (or as admin) returns 200
   with `db: true`, `openai: true`; `worker.heartbeat_ok` true again after the worker restart.
2. A fresh login works (the new `SECRET_KEY` signs cookies); an SSO login round-trips if SSO is
   in use; an admin with 2FA still passes the code prompt.
3. Chat answers (AI key), a catalog sync and a test mail succeed.

## Done criteria

- Every affected provider-side secret changed and the old one revoked.
- Variables updated on the web AND worker service, GitHub Actions secrets updated.
- Checks above pass.
- Rotation does not remove old values from git history: that is `GIT_HISTORY_PURGE.md`.
