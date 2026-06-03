# Runbook: Secret Rotation

How to rotate every credential the aileadz app depends on. Run this whenever a
secret may have leaked (it has — see `GIT_HISTORY_PURGE.md` — so this rotation
is **mandatory**, not optional). No real secret values appear in this document;
it only names env vars and the source lines that read them.

> **Why this matters:** the old credentials were hardcoded as fallback defaults
> directly in `run.py` and in now-deleted duplicate entrypoints (`run_old.py`,
> `run_b4570bd.py`, `checkout_run.py`). Those duplicates have been removed, but
> the values are still in git history and therefore must be treated as public.

## What the app reads, and where

| Secret | Env var | Read at | Insecure fallback in source |
|---|---|---|---|
| Flask session secret | `SECRET_KEY` | `run.py:134` | `'your_secret_key_here'` |
| MySQL password | `MYSQL_PASSWORD` | `run.py:158` | a hardcoded literal |
| MySQL host/user/db | `MYSQL_HOST` / `MYSQL_USER` / `MYSQL_DB` | `run.py:156-159` | hardcoded literals |
| SSH tunnel password (local dev only) | (none — hardcoded) | `run.py:305` (`ssh_password=`) | a hardcoded literal |
| OpenAI API key | `OPENAI_API_KEY` | `ai_runtime.py:89`, `app1/__init__.py:59`, `app1/build_index.py:13`, `cv_ingest.py:196`, `insights_engine.py:181`, `app3/__init__.py:10` | none (no key = AI degraded) |
| SSO token encryption key | `SSO_FERNET_KEY` | `enterprise_sso/__init__.py:86` | derived from `SECRET_KEY` (weaker) |

## Recommended rotation order

Rotate in this order so the app is never left pointing at a stale credential:

1. **MySQL password** (the most damaging if leaked — direct DB access).
2. **SSH password** (PythonAnywhere account password / SSH access).
3. **OpenAI API key** (billing + data exposure).
4. **SECRET_KEY** last — see the cookie-invalidation note below; doing it last
   means you only force one re-login event after everything else is stable.

Each step is: change the secret at the provider, then set the matching env var
on PythonAnywhere, then reload the web app, then verify.

---

### 1. MySQL password

1. PythonAnywhere → **Databases** tab → set a new password for the MySQL
   instance (user `TobiasMastek`, database `TobiasMastek$AiLead`).
2. Set the env var the app reads (`run.py:158`):
   - **Web app (WSGI):** add `os.environ["MYSQL_PASSWORD"] = "..."` to the WSGI
     file *before* the app is imported (mirror the pattern in
     `wsgi_pythonanywhere.example.py`). Never commit the real value.
   - **Scheduled tasks / consoles:** export `MYSQL_PASSWORD` in the task command
     or in `~/.bashrc` so background jobs use the same credential.
3. Reload the web app.

> Because `MYSQL_PASSWORD` (and host/user/db) have non-secret-safe **fallbacks**
> baked into `run.py`, an unset env var silently uses the old leaked value — so
> always confirm the env var is actually set, don't rely on the default.

### 2. SSH password

The SSH tunnel (`run.py:296-320`) is used for **local development only** — it
connects through `ssh.pythonanywhere.com` with `ssh_password=` hardcoded at
`run.py:305`. On the PythonAnywhere host itself the app connects to MySQL
directly and this code path is not used.

1. Change your PythonAnywhere account password (Account tab) — this is the SSH
   password.
2. Update local dev: do **not** re-hardcode it. The hardcoded literal at
   `run.py:305` should be replaced with `os.environ.get('SSH_PASSWORD')` and the
   value supplied via a local `.env` / shell export. Until that code change
   lands, local devs must edit the literal locally and must never commit it.

### 3. OpenAI API key

1. OpenAI dashboard → revoke the leaked key, create a new one.
2. Set `OPENAI_API_KEY` in the WSGI file (see
   `wsgi_pythonanywhere.example.py:11`) and in any console/scheduled-task
   environment that runs `app1/build_index.py`, drains, or digests.
3. Reload the web app.

If the key is unset the app still boots; chat/RAG/CV features degrade and
`/readyz` reports `"openai": false` (`health.py:35`, `health.py:103`).

### 4. SECRET_KEY (do this last)

1. Generate a fresh random key (e.g. `python -c "import secrets; print(secrets.token_urlsafe(48))"`).
2. Set `SECRET_KEY` in the WSGI environment (read at `run.py:134`).
3. Reload the web app.

> **Rotating `SECRET_KEY` invalidates every existing session cookie — this is a
> feature, not a bug.** Any cookie that may have been forged with the leaked key
> stops being accepted immediately. All users are logged out and must sign in
> again. Schedule it for a low-traffic window and tell users to expect a
> re-login.
>
> **Also consider `SSO_FERNET_KEY`:** if it is unset, the SSO token encryption
> key is *derived from* `SECRET_KEY` (`enterprise_sso/__init__.py:115-116`), so
> rotating `SECRET_KEY` also rotates the derived SSO key and invalidates
> outstanding SSO tokens. To decouple the two, set an explicit `SSO_FERNET_KEY`
> (a urlsafe-base64 32-byte key) — then rotate it on its own cadence.

---

## Setting env vars on PythonAnywhere

There is no single "secrets" UI. Set each var in **both** places that run code:

- **Web app:** the WSGI configuration file (Web tab → WSGI config). Assign
  `os.environ[...]` at the top, before the Flask app is imported. Use
  `wsgi_pythonanywhere.example.py` as the template — copy it, fill in real
  values, and keep the filled copy out of git.
- **Scheduled tasks & consoles:** export the vars in the task command line or in
  `~/.bashrc`, so the outbox drain, digests, and `build_index.py` see the same
  credentials as the web app.

## Verify

1. Reload the web app (Web tab → Reload).
2. Hit the probes:
   - `GET /healthz` → `{"status":"ok"}`, HTTP 200 (liveness; touches nothing).
   - `GET /readyz` → HTTP 200 with `"db": true` confirms the new MySQL
     credentials work; `"openai": true` confirms the OpenAI key is set
     (`health.py:97-116`). A 503 / `"db": false` means the DB credential is
     wrong — fix before considering rotation complete.
3. Confirm a fresh login works (proves the new `SECRET_KEY` is signing cookies).
4. If you set/rotated `SSO_FERNET_KEY`, confirm an SSO login round-trips.

## Done criteria

- All four provider-side secrets changed.
- Matching env vars set in WSGI **and** scheduled-task environments.
- `/readyz` returns 200 with `db: true` and `openai: true`.
- Fresh login + (if applicable) SSO login succeed.
- Git-history purge tracked separately (`GIT_HISTORY_PURGE.md`) — rotation does
  **not** remove the leaked values from history.
