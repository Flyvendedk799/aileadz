# ServerHoster handover: pending changes on the host

A self-contained task list for whoever (person or agent) has access to the ServerHoster
panel for the production VPS. Everything here is a change on the host, not in the repo.
Do the sections in order; each ends with a check. Stop and report if a check fails:
do not improvise around it. Remove a section from this file once it is done and verified.

Background reading, only if something is unclear: [DEPLOY.md](DEPLOY.md) (services and
variables), [JOB_RUNNER.md](JOB_RUNNER.md) (worker), [SECRET_ROTATION.md](SECRET_ROTATION.md).

## What exists on the host

Two services built from this repository, same branch, same environment variables:

| Service | Start command it should have |
|---|---|
| web | `gunicorn -c deploy/gunicorn.conf.py wsgi:application --bind 0.0.0.0:$PORT` |
| worker | `python drain_worker.py --loop` |

Never set `SANDBOX=1` on either. Never print or paste secret values into a report.

## 1. Change the web start command

**Why:** the web service was started with bare `gunicorn wsgi:application --bind 0.0.0.0:$PORT`.
That is one synchronous worker with a 30-second timeout: one open AI chat answer blocks
every other visitor, and a long answer is cut off. `deploy/gunicorn.conf.py` (in the repo
from the commit that added this file) switches to threaded workers.

**Do:**

1. Confirm the deployed commit contains `deploy/gunicorn.conf.py`. If not, deploy first.
2. Set the web service start command to exactly:
   `gunicorn -c deploy/gunicorn.conf.py wsgi:application --bind 0.0.0.0:$PORT`
3. Do not add these variables unless told to; the defaults are intended:
   `WEB_CONCURRENCY` (default `1`: keep it at 1, per-process rate limits and caches assume
   one process), `WEB_THREADS` (default `16`), `WEB_TIMEOUT` (default `120`),
   `WEB_MAX_REQUESTS` (default `2000`), `WEB_ACCESS_LOG` (default `0`).
4. Restart the web service.

**Check:**

- The service log shows a line with `Using worker: gthread`.
- `GET https://<domain>/healthz` returns 200 with `{"status":"ok"}`.
- `GET https://<domain>/readyz` returns 200.
- With a logged-in browser session: start an AI chat answer in two tabs at once and load
  a normal page in a third while both are streaming. The page loads at once and neither
  answer stops after about 30 seconds.

**Roll back:** restore the old start command and restart. Nothing else changed.

## 2. Confirm the worker service

**Why:** scheduled work (mail queue, webhooks, reminders, reports, retention) must not
depend on visitors' requests.

**Do:**

1. If no worker service exists, create one from the same repository and branch as web,
   start command `python drain_worker.py --loop`, with the same environment variables as
   web.
2. Open `https://<domain>/admin/system-health` as a platform admin.
3. Only when that page shows "Worker kører": set `SCHEDULER_OPPORTUNISTIC=0` on the
   **web** service and restart web. If it does not show it, leave the variable alone.

**Check:** Systemstatus still shows "Worker kører" ten minutes later and no job is marked
overdue.

## 3. Confirm the CI job still passes

**Why:** `.survhub/ci.yml` gained a step that downloads Node.js (version in `NODE_VERSION`)
into `/cache` so the browser transport tests run instead of being skipped. That step has
not run on the host yet.

**Do:** trigger the CI job for the branch `claude/zealous-cannon-iqva1c` (or the pull
request made from it) and read the "Run pytest suite" step.

**Check:** the step prints a Node version line (`v22...`) and the suite passes. If the
download fails (no outbound access to `nodejs.org`, or `/cache` not writable), report the
exact error; do not edit the workflow on the host.

## 4. Secrets that leaked into git history (needs the owner's go-ahead)

**Why:** an old Shopify admin token, a MySQL password and an SSH password are in the
public git history. They must be treated as compromised.

**Do, only after the owner says go, in this order:**

1. Rotate the Shopify token, then the MySQL password, then the SSH password, following
   [SECRET_ROTATION.md](SECRET_ROTATION.md).
2. Set the new values on **both** services: `SHOPIFY_ADMIN_TOKEN`, `SHOPIFY_STORE`
   (`<shop>.myshopify.com`), and the `MYSQL_*` / `DATABASE_URL` values. Restart both.
3. The history rewrite itself ([GIT_HISTORY_PURGE.md](GIT_HISTORY_PURGE.md)) is a separate,
   owner-run step; do not start it from the host.

**Check:** `/readyz` returns 200 after the restart (database reachable with the new
password); Admin, Katalog shows the Shopify sync working; the old Shopify token returns
401 when tried against the Shopify API.

## 5. Things to read off the host and report back

No change, just answers the repository cannot give:

- The current web start command before step 1 (to confirm what production was running).
- Whether a `/cache` directory persists between CI runs.
- Whether these paths are on a persisted volume that survives a redeploy: `instance/`,
  `static/uploads/`, `static/brand_assets/`, and the catalog file that
  `CATALOG_SOURCE_FILE` points at. They exist only on this one disk.
- Whether any backup of the MySQL database is scheduled, where it is stored and when it
  last ran. (No backup or restore procedure is documented yet; this answer decides how
  that runbook is written.)
- Whether the two GitHub Actions workflows that remain (`security.yml` weekly,
  `ai-eval-nightly.yml` nightly) are still running, and whether the repository secrets
  `OPENAI_API_KEY` / `ANTHROPIC_API_KEY` they need are set. (Read from GitHub, not the host.)

## Report format

For each section: done / not done, what the check showed, and any error text verbatim
(without secret values).
