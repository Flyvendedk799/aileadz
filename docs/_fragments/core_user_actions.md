## A. E-mail that actually leaves the server (N-0.2)

Code is done (Flask-Mail in requirements, `MAIL_*` copied into the app config, honest health probe, test button). What only you can do:

1. On the VPS, in ServerHoster, open the **web service** environment and set: `MAIL_SERVER`, `MAIL_PORT` (587 with `MAIL_USE_TLS=1`, or 465 with `MAIL_USE_SSL=1`), `MAIL_USERNAME`, `MAIL_PASSWORD`, `MAIL_DEFAULT_SENDER` (an address your SMTP account may send as).
2. Set the same variables on the **worker service** (see B) - scheduled mails are sent from there.
3. Set `APP_BASE_URL` (for example `https://app.futurematch.dk`) on both services. Every e-mail link (order status, invite, reset) is built from it.
4. Redeploy so `Flask-Mail` is installed.
5. Log in as platform admin, open **Admin -> Systemstatus**, find the **E-mail** card: it must say "Sat op". Type your own address and press **Send test-mail**. Confirm the mail arrives (check spam) and that the sender and links look right. If it fails, the card shows the SMTP error text.
6. Optional but recommended: SPF/DKIM for the sending domain at your DNS/SMTP provider, otherwise the mails land in spam.

## B. Dedicated background worker (N-8.1)

1. In ServerHoster create a **second service** from the same repository and branch as the web app. Start command: `python drain_worker.py --loop`. Copy every environment variable from the web service.
2. Within about a minute **Admin -> Systemstatus -> Baggrundsjobs** must show "Worker kører" and recent "sidst kørt" times. `/readyz` also shows the `worker` block.
3. Only then set `SCHEDULER_OPPORTUNISTIC=0` on the **web service** and redeploy it (this stops background jobs from running inside visitors' requests). If you skip step 1, leave it at `1`, otherwise emails and webhooks stop.

## C. Database: deploy steps and baseline (N-3.4, N-1.1, N-3.2)

1. **Take a database backup/snapshot before the first deploy of this branch.** On first boot the app adds the new order/notification columns and runs two one-time data fixes (flagged in `schema_meta`): legacy order statuses are mapped (`pending` -> `approved`, `confirmed`/`processing` -> `booked`, `invoiced`/`paid` move into the separate billing status) and old `company_notifications` are copied into the unified `notifications` table.
2. After the deploy, open **Admin -> Systemstatus**: it should list no missing tables/columns. If anything is missing, check the web service log for `Table creation warning`.
3. Baseline for Alembic (optional, recommended): on the VPS run `python scripts/dump_schema_baseline.py > docs/schema_baseline.sql` with the production `MYSQL_*` variables (read-only). Review the "drift report" at the bottom. Then `alembic stamp 0002_performance_indexes` and `alembic upgrade head`. The Part A branch may add its own Alembic revision; if `alembic heads` shows two heads, run `alembic merge heads -m "merge part a and b"`.

## D. Nice-to-have environment variables

- `SENTRY_DSN` (error tracking; create a Sentry project and paste the DSN), `LOG_FORMAT=json` (structured logs), `REDIS_URL` (shared cache between workers).
- `SUPPORT_EMAIL`, `SUPPORT_PHONE`, `SUPPORT_HOURS`, `SUPPORT_SLA`: the support page only shows a phone line/response time when you set them.

## E. Browser checks that need real accounts

Please click through once on the VPS with an employee, an HR manager and a platform admin:

1. Employee: log in -> lands on "Min læring" with the welcome card; request a course; open the order from "Mine bestillinger"; check the sidebar has no "Virksomhed" block.
2. HR manager: approve the request (queue or order page) -> the learner's status reads "Godkendt - afventer booking" and they get an e-mail; book it from the order page; the learner sees "Booket" and can download the calendar file.
3. Learner: "Markér som gennemført" -> skill chips appear; add one; HR/manager gets a "Bekræft kompetenceløft" notification.
4. White-label tenant: the company name and logo in the sidebar must stay (the old localStorage overwrite is gone).
5. "Glemt adgangskode?" on the login page sends an e-mail and the link works once.
6. (Needs the account-token storage from Part A, S-2.4, or the platform-fork module `account_tokens.py`; see that section of the merged notes.)

## F. CI

Nothing to do; the workflow now has a lint job, coverage upload and a 20-minute limit. The `gitleaks/gitleaks-action@v2` step still runs on Node 20 (no newer release of that action exists yet); GitHub will show a deprecation notice until it is updated upstream.
