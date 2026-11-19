# ServerHoster handover: pending changes on the host

A self-contained task list for whoever (person or agent) has access to the ServerHoster
panel for the production server (a Google Cloud VM since 2026-11-18). Everything here is a change on the host, not in the repo.
Do the sections in order; each ends with a check. Stop and report if a check fails:
do not improvise around it. Remove a section from this file once it is done and verified.

Background reading, only if something is unclear: [DEPLOY.md](DEPLOY.md) (services and
variables), [JOB_RUNNER.md](JOB_RUNNER.md) (worker), [SECRET_ROTATION.md](SECRET_ROTATION.md).

## What exists on the host

Two services built from this repository, same branch, same environment variables:

| Service | Start command it should have |
|---|---|
| web (`aileadz`) | `.venv/bin/python -m gunicorn -c deploy/gunicorn.conf.py wsgi:application --bind 0.0.0.0:$PORT` |
| worker (`aileadz-worker`) | `.venv/bin/python drain_worker.py --loop --interval 120` |

Never set `SANDBOX=1` on either. Never print or paste secret values into a report.

## Done on 2026-11-19 (removed from this list)

PR #85 deployed (`f7801af`) after a fresh backup. Web now runs
`.venv/bin/python -m gunicorn -c deploy/gunicorn.conf.py wsgi:application --bind 0.0.0.0:$PORT`
(log: `Using worker: gthread`, one worker), `/healthz` and `/readyz` return 200. The worker
heartbeat is current and `SCHEDULER_OPPORTUNISTIC=0` was already set on web. ServerHoster CI
ran the Node step (v22.12.0). `BACKUP_DIR` and `AI_EVAL_DIR` are set on web; the backup and
nightly-eval timers are enabled ([BACKUP_RESTORE.md](BACKUP_RESTORE.md),
[AI_EVAL_NIGHTLY.md](AI_EVAL_NIGHTLY.md)).

Still to check by hand, logged in: start an AI chat answer in two tabs at once and load a
normal page in a third while both stream; the page loads at once and neither answer stops
(ROADMAP T0-1).

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

## 6. Answers read off the host (2026-11-19)

- **Host:** ServerHoster on a Google Cloud VM (`serverhoster`, europe-west1-b) since
  2026-11-18. Services `aileadz` (web, port 3014, futurematch.dk) and `aileadz-worker`,
  both on branch `main` with auto-pull; MySQL is the ServerHoster database `aileadz-db`
  (credentials injected as `DATABASE_URL`).
- **Web start command before step 1:** `.venv/bin/python -m gunicorn wsgi:application
  --bind 0.0.0.0:$PORT --workers 2 --threads 4 --timeout 180 --graceful-timeout 30
  --access-logfile - --error-logfile -`. Two processes, so the per-process limiter and
  caches were already split (T4-1); step 1 brings it to one.
- **Worker:** `.venv/bin/python drain_worker.py --loop --interval 120`, running.
- **CI cache:** `/root/.survhub/ci-cache` persists between runs.
- **Runtime paths:** the service has no persisted paths and the checkout is a plain git
  working tree; `instance/`, `static/uploads/` and `static/brand_assets/` do not exist
  there yet. Nothing has been uploaded to disk so far, and `scripts/backup.sh` picks
  them up once they exist.
- **Backups:** none existed. Now scheduled, with the off-server copy and a rehearsed
  restore; see [BACKUP_RESTORE.md](BACKUP_RESTORE.md), "How production does it".
- **GitHub Actions:** `security.yml` ran and failed weekly on pip-audit (fixed with the
  Pillow and filelock bumps). `ai-eval-nightly.yml` "passed" nightly without running:
  neither `OPENAI_API_KEY` nor `ANTHROPIC_API_KEY` is set as a repository secret. It is
  replaced by a nightly job on the server ([AI_EVAL_NIGHTLY.md](AI_EVAL_NIGHTLY.md)).

## Report format

For each section: done / not done, what the check showed, and any error text verbatim
(without secret values).
