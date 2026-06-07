# Runbook: Background Job Runner (Scheduler — Outbox Drain & Scheduled Maintenance)

aileadz has **no always-on worker** and **no cron/Celery/Redis** on
PythonAnywhere. Integration events (webhooks, etc.) are written to a durable
outbox inside the request; daily insights / agreement alerts / compliance
re-checks all need *something* to call them on a cadence. `scheduler.py` is that
"something": a tiny, dependency-free job runner.

## The scheduler (`scheduler.py`)

A small registry of jobs, each `{name, interval_seconds, fn(app)->summary,
enabled}`. The single entry point is `scheduler.run_due_jobs(app, only=None,
force=False)` — it figures out which jobs are due, claims each one (so two
workers don't double-run it), runs each wrapped in try/except, stamps the
outcome, and returns a per-job summary. **It never raises.**

### Registered jobs

| job | interval | what it does |
|-----|----------|--------------|
| `outbox_drain` | ~120s | `event_bus.drain_outbox()` — deliver pending integration events (webhooks). |
| `daily_company_insights` | 24h | iterate active companies → `insights_engine.generate_company_insights`. |
| `daily_agreement_alerts` | 24h | iterate active companies → `catalog_freshness.notify_expiring_agreements`. |
| `compliance_recheck` | 24h | thin guarded no-op pass today; clear extension point for per-company compliance re-derivation. |

"Active company" = `companies.status = 'active'` (falls back to all companies if
that column is unavailable), bounded to `DEFAULT_COMPANY_BATCH` (200) per pass.

### Bookkeeping table (`scheduled_job_runs`)

Created idempotently by `scheduler._ensure_table(app)` (self-contained in
`scheduler.py`, NOT in `enterprise_tables.py`; never raises):

```
scheduled_job_runs (
  job_name     VARCHAR(120) PRIMARY KEY,
  last_run_at  DATETIME NULL,
  last_status  VARCHAR(20) NULL,   -- 'ok' | 'error'
  last_summary TEXT NULL,          -- JSON summary of the last run
  updated_at   TIMESTAMP ...
)
```

`last_run_at` drives both the "is this due?" check (`now - last_run_at >=
interval`) and the **best-effort claim**: an atomic
`UPDATE ... SET last_run_at = NOW() WHERE last_run_at < (NOW() - interval)` —
the affected-row count tells the caller whether *it* won the claim, so two
workers won't run the same daily job in the same window.

## Three ways to drive it

1. **PythonAnywhere Scheduled Task** (single pass, recommended) — `drain_worker.py` once.
2. **Always-on-style worker loop** — `drain_worker.py --loop`.
3. **Opportunistic request hook** — an `after_request` hook in `run.py` runs the
   due jobs at most once per ~60s **per worker**, mirroring
   `event_bus.opportunistic_drain`. Fully guarded (cannot affect the response or
   raise); toggle with `SCHEDULER_OPPORTUNISTIC` (default on, set `0` to disable,
   e.g. under tests). This is best-effort only — if the app gets no traffic,
   nothing runs. **The reliable driver is mode 1 or 2.**

---

## Legacy: the token-protected HTTP outbox endpoint (still works)

Before the scheduler, the only driver was a PythonAnywhere **Scheduled Task**
that POSTed the token-protected drain endpoint. That endpoint still exists for
outbox-only drains; the sections below document it.

## The endpoint

`POST /api/v1/_internal/drain-outbox`
(defined at `enterprise_api/__init__.py:1871-1918`).

- **Auth:** shared secret in env `OUTBOX_DRAIN_TOKEN`, compared in constant time
  (`enterprise_api/__init__.py:1881-1898`). Pass it as the `X-Drain-Token`
  header, or as `Authorization: Bearer <token>`.
- **Disabled when unset:** if `OUTBOX_DRAIN_TOKEN` is not configured the endpoint
  returns **503** so an unconfigured deploy can't be drained anonymously
  (`enterprise_api/__init__.py:1882-1886`).
- **`limit` query param:** rows per call, clamped to `1..500`, default `50`
  (`enterprise_api/__init__.py:1908-1911`).
- **Response:** `200 {"status":"ok","counts":{...}}` on success.

## Option A (recommended): the `drain_worker.py` script — no token, no HTTP

`drain_worker.py` calls `scheduler.run_due_jobs(app)` directly in the app
context (which includes the `outbox_drain` job), so there is no token to manage
and no public endpoint to secure. It runs **all due jobs**, not just the outbox.

- **Scheduled Task** (single-shot, runs every due job then exits):
  ```
  cd ~/aileadz && python3 drain_worker.py
  ```
- **Always-on Task** (paid; runs due jobs continuously, near-real-time):
  ```
  cd ~/aileadz && python3 drain_worker.py --loop --interval 60
  ```
- **Restrict / force** (debugging):
  ```
  cd ~/aileadz && python3 drain_worker.py --only outbox_drain      # one job (comma list ok)
  cd ~/aileadz && python3 drain_worker.py --force                  # ignore intervals, run all now
  ```
  Use your virtualenv python if you have one (`~/.virtualenvs/<env>/bin/python3`).
  DB connection comes from `run.py`'s config, so no extra env is needed. The
  daily jobs only do real work once per 24h regardless of how often the task
  runs (the `scheduled_job_runs` claim enforces the cadence), so it is safe to
  run this every few minutes. This is the simplest setup — skip Option B unless
  you specifically want the HTTP endpoint.

  **Free PythonAnywhere account note:** with only one daily scheduled task, set
  it to the single command above. The `outbox_drain` job then runs at most once
  a day from the task — for more frequent webhook delivery rely on the
  opportunistic request hook, or upgrade to a paid plan for a higher cadence.

### Env toggles (scheduler)

- `SCHEDULER_OPPORTUNISTIC` — `1` (default) enables the `run.py` request-hook
  fallback; set `0`/`false`/`off` to disable (the worker sets this to `0` for
  its own process). Set to `0` in test environments.
- `OUTBOX_DRAIN_INTERVAL` — seconds between passes in `--loop` mode (default 60;
  floored at 5).
- `OUTBOX_DRAIN_TOKEN` — only for the legacy HTTP endpoint (Option B), not the
  scheduler.

## Option B: the token-protected HTTP endpoint

### 1. Set the token env var

Choose a strong random token:
```
python -c "import secrets; print(secrets.token_urlsafe(32))"
```
Set `OUTBOX_DRAIN_TOKEN` to that value in **both** environments:

- **Web app:** in the WSGI config (so the endpoint accepts the token) — see
  `wsgi_pythonanywhere.example.py` for the `os.environ[...]` pattern.
- **Scheduled task:** export it in the task command (the curl call reads it).

Keep the real value out of git.

### 2. Create the PythonAnywhere Scheduled Task

PythonAnywhere → **Tasks** tab → add a task. Command:

```
curl -fsS -X POST \
  -H "X-Drain-Token: $OUTBOX_DRAIN_TOKEN" \
  "https://<your-domain>/api/v1/_internal/drain-outbox?limit=200"
```

(If the task shell doesn't inherit `OUTBOX_DRAIN_TOKEN`, set it inline in the
command or in `~/.bashrc`. Do not paste the token into a committed file.)

### 3. Cadence

- Free PythonAnywhere accounts allow **one daily** scheduled task — pick the
  hour and accept up-to-24h delivery latency.
- Paid accounts can schedule **every few minutes**; every **5 minutes** is a good
  default for near-real-time webhook delivery. Raise `limit` if backlog builds.

---

## What the runner unlocks

Without the scheduled drain, the app still works but degrades to **best-effort,
opportunistic** outbox delivery only (pending integration events are flushed
opportunistically during normal request traffic via the event bus, with no
guarantee and no catch-up when traffic is quiet).

A reliable runner is what makes the following dependable rather than best-effort:

- **Outbox / integration-event delivery** — webhooks fire reliably and retry
  instead of waiting for the next coincidental request.
- **Digests** — periodic summary/notification digests get sent on schedule.
- **Data retention** — retention sweeps (pruning/aging out old records) run.
- **Compliance reminders** — scheduled compliance/training reminders go out.

These maintenance jobs need *something* to call them on a cadence; the same
scheduled-task mechanism (and token-protected internal endpoints) is the hook.

## Verify

1. Confirm the env var is live: with `OUTBOX_DRAIN_TOKEN` **unset**, the endpoint
   returns 503; once set + web app reloaded, it accepts the token.
2. Run the curl command manually once. Expect `200` with a JSON `counts` body.
3. A wrong/missing token returns `401`; a missing token env on the host returns
   `503`.
4. After the scheduled task's first run, check the task log shows a `200` and the
   outbox table has fewer pending rows.

## Done criteria

- `OUTBOX_DRAIN_TOKEN` set in WSGI + task env.
- Scheduled task created at the chosen cadence and returning `200`.
- Manual curl confirms drain works.
