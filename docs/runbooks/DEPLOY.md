# Runbook: Deploy & Operations Index

Single entrypoint for operating aileadz. Production is a VPS running ServerHoster,
which supervises two services from this repo:

| Service | Start command | Role |
|---|---|---|
| web | `gunicorn wsgi:application --bind 0.0.0.0:$PORT` | the Flask app (`create_app()` in `run.py`, wrapped by `wsgi.py`) |
| worker | `python drain_worker.py --loop` | scheduled jobs: webhooks, digests, retention, Shopify sync (see JOB_RUNNER.md) |

Both need the SAME environment (the worker also runs `create_app()`). The canonical
variable list, with defaults and comments, is `.env.example`: never commit real
values. `wsgi.py` loads a `.env` next to it without overriding variables the host
already set; ServerHoster injects env directly, so no `.env` is needed there.

`wsgi.py` also: trusts exactly one proxy hop (`ProxyFix`: the app sits behind a
Cloudflare tunnel that terminates TLS, so `https` URLs and the real client IP work),
and 301-redirects `www.<host>` to `<host>` (`CANONICAL_WWW_REDIRECT=0` disables).

## Runbooks

| Runbook | When to use |
|---|---|
| [SECRET_ROTATION.md](SECRET_ROTATION.md) | Rotate `SECRET_KEY`, DB password, API keys, Fernet keys. |
| [GIT_HISTORY_PURGE.md](GIT_HISTORY_PURGE.md) | Scrub old leaked secrets from git history. Not done yet. **Owner go-ahead required**; rewrites history. |
| [JOB_RUNNER.md](JOB_RUNNER.md) | The worker service, the job registry, `SCHEDULER_OPPORTUNISTIC`. |
| [EMAIL_SETUP.md](EMAIL_SETUP.md) | Configure SMTP so branded order/welcome/reset mails actually send. |
| [CATALOG_REBUILD.md](CATALOG_REBUILD.md) | Catalog source, search index and embeddings; the offline `app1/build_index.py`. |
| [STATIC_AND_PERF.md](STATIC_AND_PERF.md) | Static asset cache-busting, gzip, DB indexes, in-process cache, tuning vars. |
| [../WEBHOOK_VERIFICATION.md](../WEBHOOK_VERIFICATION.md) | Receiver-side webhook signature verification (customer-facing). |

## Variables that decide whether a deploy boots or works

| Var | Why it matters |
|---|---|
| `SECRET_KEY` | Mandatory: `run.py` (`resolve_secret_key`) raises at boot for an unset or placeholder value. Only `SANDBOX=1` may fall back. |
| `DATABASE_URL` or `MYSQL_*` | DB connection; `MYSQL_*` wins. With neither set the app falls back to `localhost`/`root`/db `aileadz` and logs a warning. |
| `OPENAI_API_KEY` | Needed in every AI configuration (embeddings). Can instead be stored encrypted via `/admin/ai-settings` (`ai_secrets.py`). |
| `MAIL_*` / `SMTP_*` | No mail leaves without a server and a sender (EMAIL_SETUP.md). |
| `APP_BASE_URL` | Absolute links in e-mails. |
| `SCHEDULER_OPPORTUNISTIC` | `0` on the web service once the worker runs (JOB_RUNNER.md). |
| `HEALTH_TOKEN` | Lets a monitor read the detailed `/readyz` body via `X-Health-Token`. |
| `TWOFA_ENFORCE_ADMIN`, `CSP_ENFORCE`, `HSTS_*` | Security switches; the defaults are the safe production values. |

> **Token budgets:** the code defaults are `AI_MAX_INPUT_TOKENS=36000` /
> `AI_TPM_BUDGET=42000`. A host that still pins the old `20000`/`26000` keeps the old,
> much tighter context. Change them deliberately after checking the OpenAI org TPM
> tier (and Anthropic ITPM if Claude is enabled); watch `ai_agent_runs` for 429s.

> `SANDBOX=1` relaxes the mandatory-`SECRET_KEY` check, secure cookies, HSTS and CSRF
> enforcement. **Never set it in production.**

## Schema lifecycle

- Tables self-create and extend from the single definitions in `enterprise_tables.py`
  and `schema_registry.py`, from `before_request` hooks in `run.py`, so the **web
  service creates the schema on its first request after a boot**. The worker does not
  run them: boot web first on a fresh database. The "already ensured" stamp lives in
  the OS temp dir keyed on the DDL fingerprint (`ENTERPRISE_TABLE_SYNC_TTL_SECONDS`,
  `ENTERPRISE_TABLE_SYNC_FORCE=1` to force a re-sync).
- One-time data fixes run once, flagged in `schema_meta` (`schema_registry.run_data_migrations`).
- Alembic (`migrations/`) is the forward path and is optional at runtime: see
  `migrations/README.md`. To adopt it on an existing DB, dump the live schema with
  `scripts/dump_schema_baseline.py`, review it, then `alembic stamp b001_part_b_lifecycle`.

## Runtime state that is not in git

Back these up; a fresh clone does not have them (`.gitignore`: `*.json`, `*.db`,
`instance/`, `static/uploads/`, `static/temp/`):

- `instance/`: catalog admin overlay, CSV imports, category overrides, import/AI-category
  drafts, the embeddings sidecar `catalog_embeddings.json`.
- `static/uploads/`: admin/user uploads (needs a persisted path on the host).
- `app1/shopify_products_all_pages.json` (or `CATALOG_SOURCE_FILE`) and
  `app1/shopify_products_augmented.json`: the catalog (CATALOG_REBUILD.md).
- `app1/ai_memory.db`: SQLite fallback of the AI store; production uses MySQL
  (`AI_MEMORY_BACKEND=auto`).

## Health & readiness probes

| Probe | Path | Meaning |
|---|---|---|
| Liveness | `GET /healthz` | `{"status":"ok"}` 200; touches nothing. |
| Readiness | `GET /readyz` | **200 when the DB answers, 503 otherwise.** The public body is only `{"status": "ready"\|"degraded"}`; a platform-admin session or `X-Health-Token` (= `HEALTH_TOKEN`) also gets `db`, `catalog`, `openai`, `ai` (active provider), `features` and `worker` blocks (`health.py`). |

Detailed `/readyz` interpretation:
- `"db": false`: DB credentials/connection broken (the only thing that returns 503).
- `"catalog": false`: neither the catalog source file nor an augmented index exists: see CATALOG_REBUILD.md.
- `"openai": false`: no OpenAI key resolvable (env or `/admin/ai-settings`): AI degraded.
- `worker`: `dedicated_worker`, `heartbeat_ok`, `outbox_pending` and per-job last run / `overdue`.

## Deploy checklist

1. Deploy the web and worker services from the same commit (ServerHoster).
2. Confirm the required variables above are set on both services.
3. `GET /healthz` returns 200.
4. `GET /readyz` (with `X-Health-Token` or as admin) returns 200 with `db: true`; check `catalog` / `openai`.
5. `catalog: false`: follow CATALOG_REBUILD.md.
6. Admin -> Systemstatus (`/admin/system-health`) shows "Worker kører"; then `SCHEDULER_OPPORTUNISTIC=0` on web (JOB_RUNNER.md).
7. Send a test mail from the same page (EMAIL_SETUP.md).
8. **One-off:** after the first boot has created the conversation-state columns, add the
   unique conversation key in a quiet window (it can rebuild `conversation_history`, which
   is why it is not done at boot): `python scripts/migrate_conversation_unique.py --dry-run`,
   then without `--dry-run`. Duplicates are renamed (`...#dup<id>`), never deleted. The app
   runs correctly before this step; the key only closes a rare double-insert race.
9. Optional: build the platform-help embedding index with `python -m app1.help_kb --build`
   (keyword search works without it).
