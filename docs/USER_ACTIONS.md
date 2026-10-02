# Owner actions

Steps only the owner can do: they need accounts, servers, a browser with real logins, or a decision. Procedures that have their own runbook are linked, not repeated: [runbooks/DEPLOY.md](runbooks/DEPLOY.md) (service layout, required variables, deploy checklist, schema lifecycle), [runbooks/SECRET_ROTATION.md](runbooks/SECRET_ROTATION.md), [runbooks/GIT_HISTORY_PURGE.md](runbooks/GIT_HISTORY_PURGE.md), [runbooks/JOB_RUNNER.md](runbooks/JOB_RUNNER.md), [runbooks/EMAIL_SETUP.md](runbooks/EMAIL_SETUP.md), [runbooks/CATALOG_REBUILD.md](runbooks/CATALOG_REBUILD.md). The canonical variable list is `.env.example`. Nothing here blocks the code: unset items degrade gracefully (mail shows "Ikke sat op", jobs run opportunistically, Shopify sync is skipped).

## 1. Open security debt

1. **Purge old secrets from git history.** The Shopify admin token (in `app1/count.py` and `app1/Pypy`, deleted from the tree but still in history) and old MySQL/SSH passwords (`run.py` history) are public. Treat all as compromised. Rotate first (item 2), then follow `runbooks/GIT_HISTORY_PURGE.md`, adding `--path app1/count.py --path app1/Pypy --invert-paths` to the filter-repo run. This rewrites public history and breaks clones, forks and open PRs: pick a quiet window, re-clone everywhere afterwards. Until then `gitleaks` full-history scans (`.github/workflows/security.yml`, weekly) keep reporting the old findings; scans of new commits are clean.
2. **Rotate what leaked:** the Shopify token (Shopify admin, custom app, API credentials; confirm the old token returns 401), the MySQL password and the SSH password (`runbooks/SECRET_ROTATION.md`, in that order). Set the new Shopify token as `SHOPIFY_ADMIN_TOKEN` plus `SHOPIFY_STORE=<shop>.myshopify.com` on web and worker (`shopify_sync.py` reads exactly these; `SHOPIFY_STORE_URL` in `.env.example` is a different, display-side setting).
3. **`SECRET_KEY` history check.** The app refuses to boot without a real `SECRET_KEY` (`run.py`). If production ever ran on the old placeholder, every session and every secret encrypted with a key derived from it is compromised: run `scripts/rotate_secret_key.py` (dry run, then `--apply`, with `OLD_SECRET_KEY` and `NEW_SECRET_KEY`) before starting on the new key. It re-encrypts AI provider keys, SSO client secrets and TOTP secrets, and everyone is logged out once. See SECRET_ROTATION.md section 4.

## 2. First boot of v2.0 on an existing database

Back up the database first. These run once at boot and cannot be undone:

- Remaining plaintext passwords in `users` are hashed in place (log line `S-2.2: hashed N legacy plaintext password(s)`).
- The plaintext API-key column is nulled and dropped on the first API request; keys keep working (hash was already stored) and are never shown again.
- Legacy order statuses are mapped (`pending` to `approved`, `confirmed`/`processing` to `booked`, `invoiced`/`paid` move into `billing_status`) and old `company_notifications` are copied into `notifications` (`schema_registry.run_data_migrations`, flagged in `schema_meta`).

Afterwards open Admin, Systemstatus (`/admin/system-health`): no missing tables or columns should be listed (otherwise grep the web log for `Table creation warning`). The migrations were only tested on the in-memory SQLite harness (`tests/sqlite_platform.py`), never on real MySQL, so check the result by hand. Everybody is logged out once; **platform admins must enrol TOTP at next login** (keep the 8 backup codes). A locked-out admin can set `TWOFA_ENFORCE_ADMIN=0`, enrol, and must remove the variable again.

Alembic is optional at runtime; to adopt it, follow "Schema lifecycle" in DEPLOY.md.

## 3. Services and mail

- **Worker service:** create it in ServerHoster and verify "Worker kører" in Systemstatus before setting `SCHEDULER_OPPORTUNISTIC=0` on web (steps in JOB_RUNNER.md). Without it, scheduled reports, overdue-invoice notices, digests, retention, deadline and compliance reminders only run inside web requests.
- **Mail:** set `MAIL_*` (or ServerHoster's `SMTP_*`/`SMTP_FROM`) and `APP_BASE_URL` on both services, then use Systemstatus "Send test-mail" and confirm arrival, sender and links (EMAIL_SETUP.md). Add SPF/DKIM for the sending domain or mail lands in spam. Reset links, invites, approval mails and scheduled reports all depend on this.
- **Monitor:** if an uptime monitor reads anything beyond `status` from `/readyz`, set `HEALTH_TOKEN` and send it as `X-Health-Token`.
- **Optional:** `SENTRY_DSN`, `LOG_FORMAT=json`, `REDIS_URL`, `SUPPORT_EMAIL`/`SUPPORT_PHONE`/`SUPPORT_HOURS`/`SUPPORT_SLA` (the support page shows phone and response time only when set).

## 4. Catalog and AI

- **Catalog file:** `app1/shopify_products_all_pages.json` is untracked. Keep a copy outside the checkout and point `CATALOG_SOURCE_FILE` at it on web and worker, or a deploy can remove it (CATALOG_REBUILD.md). With the Shopify variables from 1.2 set, the `shopify_sync` job recreates it; check Admin, Katalog for index status.
- **AI keys:** `OPENAI_API_KEY` is needed in every configuration (embeddings); add `ANTHROPIC_API_KEY` for Claude and pick the provider in Admin, AI-indstillinger.
- **Optional:** `AI_CREDITS_PER_DKK` (default 10, `credit_service.py`); prompt A/B via `AI_PROMPT_VARIANTS=v2.0,v2.1` (first is control) plus `AI_PROMPT_ADDENDUM_V2_1="..."`, results in AI observability.
- **Nightly eval** (`.github/workflows/ai-eval-nightly.yml`): add repository secrets `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, and optionally `EVAL_VENDOR_EMAIL`/`EVAL_VENDOR_PASSWORD` (otherwise vendor cases are skipped). Run the workflow once manually and read the scorecards, especially fluency.

## 5. Privacy decisions

- **Retention:** review the defaults in `retention_service.POLICIES` against your DPA and change any with `RETENTION_<NAME>_DAYS` (`0` or `off` disables a rule). Dry run: `python -c "from run import create_app; import retention_service as r; a=create_app(); a.app_context().push(); print(r.run_retention(dry_run=True))"`. The job runs in the worker.
- **Advanced analytics (optional):** `ADVANCED_ANALYTICS_ENABLED=1` adds an "Avanceret" button on Læringsanalyse for HR managers (ML engagement clusters, anomalies, skill gaps; aggregates only, k-anonymity floors). It stays off until you decide the ML view fits your customers' works-council and DPA expectations. Performance reviews have no writer yet, so the prediction KPI shows "—".
- **Erasure promise:** the settings page and privacy policy promise erasure within 30 days. Someone must watch `/admin/gdpr` (overdue tickets are marked); optionally set `DSR_NOTIFY_EMAIL` (falls back to `SUPPORT_EMAIL`). Have your DPO or lawyer read `templates/fm/privacy.html` and the confirmation text in `templates/fm/settings.html`.

## 6. Customers

- **SSO (per customer):** OAuth2/OIDC only. Register the redirect URI `https://<domain>/sso/callback/<company-slug>/oauth2` with their identity provider and set the company email domain so `/sso/discover` resolves it. Configs need `issuer` and `jwks_uri` (https); for Entra: issuer `https://login.microsoftonline.com/<tenant-id>/v2.0`, keys `https://login.microsoftonline.com/<tenant-id>/discovery/v2.0/keys`. Test one real IdP before announcing. SAML/LDAP are off (ROADMAP R-1).
- **API and webhook customers:** keys in the URL (`?api_key=`) are rejected with HTTP 400, send `X-API-Key`, and rotate any key that was ever in a URL (it is in someone's logs). Webhook signatures are `t=<ts>,v1=<hmac>`, redirects are not followed: send them [WEBHOOK_VERIFICATION.md](WEBHOOK_VERIFICATION.md). New events `employee.added`, `employee.updated`, `budget.overrun` are opt-in. Pre-existing keys keep working; lost keys must be re-created.
- **SCIM customers:** once, link rows created before identities existed: `python -c "from run import create_app; import scim_api; a=create_app(); a.app_context().push(); print(scim_api.backfill_identities(a.mysql.connection))"` (safe to re-run).

## 7. GitHub

The `gitleaks` action needs a licence key in secret `GITLEAKS_LICENSE` only if the repository belongs to an organisation. Consider Dependabot alerts and branch protection on `main`.

## 8. Checks that need a browser and real accounts

Nothing below is verified by tests; do each once in production (employee, HR manager, platform admin).

1. **Security headers:** open the dashboard, `/hr`, the chat, catalog, profile and admin pages with the console open: any "Refused to load" means a CDN origin is missing from `security_headers.py`. `curl -sI https://<domain>/login` shows `Content-Security-Policy` and `Strict-Transport-Security`. If a page breaks, `CSP_ENFORCE=0` falls back to report-only (violations go to `/csp-report`); fix the allowlist and remove it. HSTS pins HTTPS for a year; subdomains are excluded (`HSTS_INCLUDE_SUBDOMAINS=0`).
2. **Employee flow:** login lands on "Min læring" with the welcome card; request a course; open it from the order list; no "Virksomhed" block in the sidebar; "Glemt adgangskode?" sends a mail and the link works once.
3. **HR flow:** approve the request, learner reads "Godkendt – afventer booking" and gets a mail; book it from the order page, learner sees "Booket" and can download the calendar file; learner marks it completed, skill chips appear, manager gets "Bekræft kompetenceløft".
4. **HR pages:** `/hr/billing` (mark invoiced then paid; CSV opens in Excel with correct æøå), `/hr/reports` (schedule a weekly report, confirm the mail with CSV arrives), `/hr/compliance` (Tildel kursus creates approvals), `/hr/employee/<id>/goals` (share a goal; the employee sees it under "Mål fra din leder").
5. **White-label tenant:** company name and logo in the sidebar stay.
6. **Widget:** embed the code from Virksomhed, Indstillinger, Widget on a real customer page: it works on an allowed domain, shows "ikke tilladt" on another, and remembers the conversation across page loads. "Tilladte domæner" drives both access and `frame-ancestors`.
7. **OIDC:** one real login through a customer identity provider.
