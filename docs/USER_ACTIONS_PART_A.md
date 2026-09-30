# Part A: things only you can do

Everything in `docs/COMPLETION_PLAN.md` Part A that could be done in code is done
on the branch. This file lists the steps that need your accounts, your servers or
your decision. They are ordered: do them top to bottom. Nothing here blocks the
code from being merged, but **items 1 to 4 must happen before the branch goes to
production**, because the new code refuses to start without them.

Legend: **[before deploy]**, **[at deploy]**, **[after deploy]**.

---

## 1. Set `SECRET_KEY` on the VPS  [before deploy]  (S-1.4)

The app now refuses to boot without a real `SECRET_KEY` (only `SANDBOX=1` may
fall back). If you deploy without it, the service crash-loops with
`RuntimeError: SECRET_KEY is not set`.

1. Generate a value on your machine:
   `python -c "import secrets; print(secrets.token_hex(32))"`
2. ServerHoster: open the aileadz app, **Environment**, add `SECRET_KEY=<value>`.
   (Skill `serverhoster` has the exact menu; the VPS is `85.190.100.23`.)
3. **Was a `SECRET_KEY` ever set before?** Check the current environment of the
   running service.
   * **It was set to a real value:** keep it. Nothing else to do.
   * **It was NOT set** (so the app ran on the hard-coded placeholder, which is
     public in git): every session cookie, and every secret encrypted with a key
     derived from it, is compromised.
     a. Put the **old** value (`your_secret_key_here`) in `OLD_SECRET_KEY` and the
        new one in `NEW_SECRET_KEY`.
     b. `OLD_SECRET_KEY=your_secret_key_here NEW_SECRET_KEY=<new> python scripts/rotate_secret_key.py`
        (dry run, prints counts only), then the same with `--apply`. This
        re-encrypts AI provider keys, SSO client secrets and TOTP secrets.
     c. Start the app on the new key. Everyone is logged out once. That is intended.

## 2. Rotate the Shopify token  [before deploy]  (S-1.1)

The Admin API token that was committed in `app1/count.py` and `app1/Pypy` (it
starts `shpat_5389cc68`) is public and must be treated as stolen.

1. Shopify admin of `kursuszonen-grafikr-dk.myshopify.com`, **Settings**,
   **Apps and sales channels**, **Develop apps**, open the custom app,
   **API credentials**. Uninstall the app or rotate the Admin API access token so
   the old one stops working. Confirm: the old token returns 401.
2. Install/rotate and copy the **new** token.
3. Put it in the VPS environment as `SHOPIFY_ACCESS_TOKEN` (and
   `SHOPIFY_STORE_URL` if it differs). The scheduled sync that reads it is
   N-3.1 (Part B); until that lands nothing reads the variable, which is fine.

## 3. Rotate the database and SSH passwords  [before deploy]  (S-1.1)

Old MySQL and SSH passwords are still in git history (`run.py`). If any of them
is still in use, rotate it now. Follow `docs/runbooks/SECRET_ROTATION.md`
sections 1 and 2, in that order:

1. MySQL: set a new password for the DB user, update `MYSQL_PASSWORD` in the
   ServerHoster environment, restart, check `/readyz` (log in as admin, or send
   `X-Health-Token`, see item 9) shows `"db": true`.
2. SSH: change the account password (or move to key login); update `SSH_PASSWORD`
   or `SSH_PKEY` wherever the dev tunnel is used.
3. OpenAI key: only if you have reason to think it leaked (it was never in git).

## 4. Purge the history and force-push  [your decision]  (S-1.1)

**This rewrites public history and breaks every clone, fork and open PR.** Only
do it after items 1 to 3, and only when you have picked a quiet window.
`docs/runbooks/GIT_HISTORY_PURGE.md` has the full procedure; the additions for
this branch:

* also purge the two deleted files: `git filter-repo --path app1/count.py --path app1/Pypy --invert-paths`
  (or add the Shopify token as another `literal:` rule in `replacements.txt`);
* after the force-push, re-clone on your machine and on the VPS;
* verify with `gitleaks detect --config .gitleaks.toml` (the allowlist for
  `run.py` is already removed, so it will report the old findings until the
  purge is done; that is expected).

## 5. First deploy of this branch  [at deploy]

Take a **database backup first**; two migrations are one-way:

| What happens on first start | Why it matters |
|---|---|
| Every remaining **plaintext password** in `users` is hashed in place (log line `S-2.2: hashed N legacy plaintext password(s)`). | Logins still work; there is no way back to plaintext. |
| The **plaintext API-key column is nulled and dropped** on the first API request (`company_api_keys.api_key`). | Keys keep working (the SHA-256 was already stored). Keys are never shown again. |
| New tables/columns are created: `password_reset_tokens`, `user_2fa`, `dsr_requests`, `auth_login_attempts`, `employee_goals.shared_with_employee` (+3), `company_settings.ai_learner_hr_goals`, `company_api_keys.key_prefix`. | Additive. |

Also on this deploy:

* `pip install -r requirements.txt` runs as usual (adds `Flask-WTF`).
* **Everybody is logged out once** (sessions from before the 2FA/liveness change
  are not trusted for admins; users may also have to sign in again).
* **Platform admins must enrol TOTP at their next login.** Have an authenticator
  app ready (Microsoft/Google Authenticator, 1Password). Save the 8 backup codes.
  If an admin is locked out, set `TWOFA_ENFORCE_ADMIN=0` in the environment,
  log in, enrol, then remove the variable. Do not leave it at 0.
* Customers using the Enterprise API or webhooks must be told (see item 10).

## 6. Smoke-test the security headers in a real browser  [after deploy]  (S-5.1)

I could not run a browser. The Content-Security-Policy is now **enforced** and
allows exactly the CDNs found in the templates (cdnjs, jsdelivr, unpkg, plot.ly,
Google Fonts). Check once:

1. Open the dashboard, HR dashboard, chat (`/app1`), catalog, profile and the
   admin pages with the browser console open. Any "Refused to load ..." line
   means a page needs another origin.
2. `curl -sI https://<your-domain>/login` shows `Content-Security-Policy:` and
   `Strict-Transport-Security: max-age=31536000`.
3. If something is blocked and you need it working right now: set
   `CSP_ENFORCE=0` (falls back to report-only), tell me which origin, and add it
   to `security_headers.py`. Violations are also logged by `/csp-report`.
4. HSTS pins HTTPS for a year. Before relying on it, make sure **all** your
   subdomains you may later add are HTTPS (`HSTS_INCLUDE_SUBDOMAINS` is off on
   purpose).

## 7. Decide who gets the `/readyz` details  [after deploy]  (S-5.2)

`/readyz` now answers the public with only `{"status": "ready"}` (or
`degraded`, HTTP 503). The feature matrix, provider and key-presence details need
an admin login or the header `X-Health-Token: <HEALTH_TOKEN>`.

* If an uptime monitor parses fields other than `status`, set `HEALTH_TOKEN` in
  the environment and add the header to the monitor.
* The load-balancer behaviour (200 / 503) is unchanged.

## 8. Email must work for reset and invite links  [after deploy]  (S-2.4)

Password reset, "HR sends reset link", employee invites and company-registration
invites all go out by email. Email sending is fixed in Part B (N-0.2:
`flask_mail` dependency, `MAIL_*` config, a "send test email" button).

* Until that is live on the VPS, reset/invite mails are silently not sent. The
  screens say so and point users to "Glemt adgangskode" (which also needs mail).
* After N-0.2: send one real test email from the VPS, then try "Glemt
  adgangskode" on a test account.
* Optional: set `DSR_NOTIFY_EMAIL` (or `SUPPORT_EMAIL`) to get an email when a
  user requests erasure.

## 9. Backfill SCIM identities  [after deploy, only if you use SCIM]  (S-3.4)

SCIM used to create `company_users` rows with no login. Link the existing ones:

```
python -c "from run import create_app; import scim_api; a=create_app(); \
with a.app_context(): print(scim_api.backfill_identities(a.mysql.connection))"
```

It prints how many rows were linked. Safe to re-run.

## 10. Tell API, webhook and SSO customers  [after deploy]  (S-3.1, S-3.2, S-3.3)

* **API keys in the URL (`?api_key=`) are now rejected with HTTP 400.** Keys must
  be sent in the `X-API-Key` header. Any key that was ever sent in a URL is in
  somebody's logs: rotate it.
* **Webhook signatures changed.** `X-Webhook-Signature` is now
  `t=<timestamp>,v1=<hmac>` over `"<t>." + body`, and redirects are no longer
  followed. Send customers `docs/WEBHOOK_VERIFICATION.md`. The secret is unchanged.
* **OAuth2/OIDC SSO is live; SAML and LDAP/AD stay off** (deferred, S-D.1).
  Configs now need `issuer` and `jwks_uri` (https). Test with one real identity
  provider (for Microsoft Entra: issuer `https://login.microsoftonline.com/<tenant-id>/v2.0`,
  JWKS `https://login.microsoftonline.com/<tenant-id>/discovery/v2.0/keys`) before
  announcing it. Existing SAML/LDAP configs stay in the database, inactive.
  There is no screen for the new fields yet (N-7.1, Part B); until then they are
  posted to `/admin/sso/config/<company_id>` by the settings form fields
  `issuer`, `jwks_uri`, `allowed_domains`.

## 11. Review the retention defaults  [after deploy]  (S-4.3)

A daily job now deletes or scrubs old personal data. Defaults: e-mail log 365
days, API logs 180, AI run logs 180, HR-chat text 365, learner-chat text
(`chatbot_interactions`) scrubbed after 730 days while the counts stay,
saved conversations 730, AI debug/latency logs 14. The full table is
`retention_service.POLICIES`. The job only runs where the scheduler runs (today
inside web requests; properly with the N-8.1 worker).

1. Look at the numbers, and change any you disagree with in the environment:
   `RETENTION_<NAME>_DAYS=<days>` (`0` or `off` switches a rule off).
2. Optional dry run (counts, deletes nothing):
   `python -c "from run import create_app; import retention_service as r; a=create_app(); \
   with a.app_context(): print(r.run_retention(a.mysql.connection, dry_run=True))"`

## 12. Decide on GDPR wording and the 30-day promise  [your decision]  (S-4.2)

The settings page and privacy policy now promise that erasure requests are
handled within 30 days. Someone has to watch the queue at
**Admin, GDPR** (`/admin/gdpr`): overdue tickets are marked. Confirm that is
workable, and let your DPO/lawyer read the new wording in
`templates/fm/privacy.html` and the confirmation text in
`templates/fm/settings.html`. Also confirm the retention periods in item 11.

## 13. GitHub settings  [optional]  (S-5.3)

The new workflow `.github/workflows/security.yml` runs weekly and on changes to
`requirements.txt`. If the repository belongs to a GitHub **organization**, the
gitleaks action needs a licence key in the secret `GITLEAKS_LICENSE`. Personal
repositories work as they are. Consider turning on Dependabot alerts and branch
protection for `main`.

---

## Not testable here (please check once in production)

* Everything that needs a browser: CSP, the `csrf.js` helper on every page,
  the QR code on the 2FA page (loaded from cdnjs), the widget iframe.
* A real OIDC login against your identity provider.
* Real MySQL behaviour of the migrations (I tested the SQL with recording fakes
  and the logic with unit tests; there was no MySQL in my environment).
* Email delivery (depends on Part B, see item 8).
