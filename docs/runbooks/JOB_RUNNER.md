# Runbook: Background Job Runner (scheduler + worker)

Background work (webhook delivery, e-mails, reminders, digests, reports, catalog
sync, retention) must not run inside visitors' requests. The repo ships a
dependency-free job registry (`scheduler.py`) and a worker entry point
(`drain_worker.py`). Production runs the worker as its own ServerHoster service.

## Run it as its own service (recommended)

1. In ServerHoster create a second service from the same repository and branch as the
   web app, with the start command `python drain_worker.py --loop`
   (`--interval N` or `OUTBOX_DRAIN_INTERVAL`, default 60 s, floored at 5).
2. Give it the SAME environment variables as the web service (`DATABASE_URL`,
   `SECRET_KEY`, `MAIL_*`, `APP_BASE_URL`, `OPENAI_API_KEY`, `SHOPIFY_*`, ...).
3. The worker does not create the schema (the `run.py` `before_request` hooks do, on
   the web service's first request): boot the web service first on a fresh DB.
4. Once Admin -> Systemstatus (`/admin/system-health`) shows "Worker kører", set
   `SCHEDULER_OPPORTUNISTIC=0` on the WEB service and redeploy it. Until then leave
   it at `1` so nothing stops running.
5. `/readyz` (`worker` block, visible to admins or with `X-Health-Token`) and the
   Systemstatus page show each job's last run, `overdue` flags and the outbox backlog
   (`event_outbox` rows with `status='pending'`). The `ops_alerts` job notifies
   platform admins when e-mails or webhooks keep failing.

## Three ways to drive the scheduler

1. **Dedicated worker (use this):** `python drain_worker.py --loop`. Stamps a
   heartbeat (`_worker_heartbeat` row, stale after 300 s) every pass.
2. **Single pass:** `python drain_worker.py` runs every due job once and exits (any
   cron-like runner; no heartbeat is written).
3. **Opportunistic request hook (fallback):** an `after_request` hook in `run.py`
   runs the due jobs at most once per 60 s per web worker
   (`scheduler.run_due_jobs_safe`). Default on; `SCHEDULER_OPPORTUNISTIC=0|false|no|off`
   disables it (the worker and the test suites set it to 0). Best effort: with no
   traffic nothing runs.

`drain_worker.py` flags: `--only outbox_drain,ops_alerts` (comma list of job names),
`--force` (ignore intervals, run now; still claims the row), `--loop`, `--interval N`.
It also sets `AI_WARMUP_ON_IMPORT=0` and `SCHEDULER_OPPORTUNISTIC=0` for its own process.
The daily jobs do real work at most once per interval however often the worker passes
(the `scheduled_job_runs` claim enforces it).

## The scheduler (`scheduler.py`)

`JOBS` is a list of `{name, interval_seconds, fn(app)->summary dict, enabled}`.
`scheduler.run_due_jobs(app, only=None, force=False)` decides which jobs are due,
claims each (so two workers never double-run one), runs each in its own try/except,
stamps the outcome and returns `{'ran', 'skipped', 'errors', 'results'}`. It never raises.

### Registered jobs (`scheduler.JOBS`, in run order)

| job | interval | what it does |
|---|---|---|
| `mail_delivery` | 60 s | durable order/report/customer mail with bounded retries and explicit ambiguous-outcome recovery. |
| `launch_followup` | 24 h | stalled booking/change requests and approaching account review, pilot or renewal dates. |
| `outbox_drain` | 120 s | `event_bus.drain_outbox()`: deliver pending integration events (webhooks). |
| `ops_alerts` | 15 min | `observability.check_ops_alerts`: alert platform admins on failing e-mails / webhooks. |
| `daily_company_insights` | 24 h | per active company: `insights_engine.generate_company_insights`. |
| `daily_agreement_alerts` | 24 h | per active company: `catalog_freshness.notify_expiring_agreements`. |
| `compliance_recheck` | 24 h | re-derive compliance per company, raise recertification nudges. |
| `learning_deadline_reminders` | 24 h | warn about approaching/lapsed learning-path due dates. |
| `cert_expiry_reminders` | 24 h | nudge learners whose certifications are expiring/lapsed. |
| `company_analytics_rollup` | 24 h | write the per-company daily KPI snapshot (`company_analytics`). |
| `scheduled_reports` | 1 h | e-mail HR's scheduled reports (`company_report_schedules`; each has its own cadence). |
| `billing_overdue` | 24 h | one HR notification per invoice past its due date. |
| `shopify_sync` | 24 h | `shopify_sync.sync()`; skips cleanly without `SHOPIFY_STORE` / `SHOPIFY_ADMIN_TOKEN`. |
| `catalog_embed` | 6 h | `rag.embed_missing()`: embed catalog products that have no vector yet. |
| `data_retention` | 24 h | `retention_service.run_retention`: apply the per-table retention policy. |
| `profile_checkin_heartbeat` | 7 d | per person who used the AI assistant in the last 90 days: queue short follow-ups (`profile_checkins.run_heartbeat`); `AI_PROFILE_CHECKINS=0` turns it off. |
| `weekly_manager_digest` | 7 d | per active company: `digest_service.send_company_digest`. |

"Active company" = `companies.status = 'active'` (all companies if that column is
unavailable), at most `DEFAULT_COMPANY_BATCH` (200) per pass.

### Bookkeeping table `scheduled_job_runs`

Created idempotently by `scheduler._ensure_table` (self-contained in `scheduler.py`,
not in `enterprise_tables.py`): `job_name` (PK), `last_run_at`, `last_status`
(`ok`/`error`), `last_summary` (JSON), `updated_at`. `last_run_at` drives the due check
(`now - last_run_at >= interval`) and the claim: an atomic
`UPDATE ... SET last_run_at = NOW() WHERE last_run_at < NOW() - interval` whose
affected-row count says whether this process won the window.

## Legacy: token-protected HTTP outbox drain

`POST /api/v1/_internal/drain-outbox` (`enterprise_api/__init__.py`, `drain_outbox_endpoint`)
drains only the outbox, for an external cron. The worker makes it unnecessary.

- Auth: env `OUTBOX_DRAIN_TOKEN`, constant-time compare, sent as `X-Drain-Token` or
  `Authorization: Bearer <token>`. Unset token: the endpoint answers **503** (disabled);
  wrong/missing token: **401**.
- `?limit=` rows per call, clamped to 1..500, default 50. Success:
  `200 {"status":"ok","counts":{...}}`.
- Example: `curl -fsS -X POST -H "X-Drain-Token: $OUTBOX_DRAIN_TOKEN" "https://<host>/api/v1/_internal/drain-outbox?limit=200"`.
- Separately, ~5 % of `/api/v1/` requests piggy-back a tiny, time-boxed drain
  (`OUTBOX_OPPORTUNISTIC_FRACTION`, default 0.05). Best effort only.

## What the worker makes reliable

Without it the app still runs, but everything above is best effort (the request
hook, plus the API piggy-back drain for webhooks): nothing fires while the site is
quiet, and each web worker process runs its own pass. With it: webhooks deliver and
retry on schedule, digests/reports/reminders go out, retention sweeps and the Shopify
sync + embedding jobs run.

## Verify

1. Admin -> Systemstatus shows "Worker kører" and every job with a recent `last_run_at`
   and no `overdue` flag (overdue = last run older than 2x interval + 120 s, or never).
2. `GET /readyz` with `X-Health-Token`: `worker.heartbeat_ok` is true,
   `worker.dedicated_worker` is true once `SCHEDULER_OPPORTUNISTIC=0` on web,
   `worker.outbox_pending` is not growing.
3. Debug one job on the worker host: `python drain_worker.py --only outbox_drain --force`.
