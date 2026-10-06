# Runbook: E-mail / SMTP setup

All transactional mail goes through `email_service.send_branded_email()` (Flask-Mail,
in `requirements.txt`). Templates (`render_branded_email`): `welcome`, `password_reset`,
`password_invite`, `order_confirmation`, `order_approval_needed`, `order_approved`,
`order_booked`, `order_cancelled`, `vendor_new_order`, `vendor_submission_result`,
`vendor_invite`, `scheduled_report`, `budget_overrun_alert`, `compliance_recert_alert`,
`manager_weekly_digest`, `announcement`. Callers are best-effort wrappers that never
raise, so the app works whether or not mail is configured. Keep mail data with an
EU-resident SMTP provider that signs a DPA (the users are Danish).

## Behaviour

- `run.py` calls `email_service.load_mail_config(app)` at boot: the environment is
  copied into `app.config` (Flask-Mail reads config only).
- A send needs BOTH a server and a sender (`send_branded_email`). Otherwise it returns
  `False` without raising and writes an `email_log` row with status `skipped_no_backend`.
- Other `email_log` statuses: `sent`, `error` (SMTP failure or render failure; the
  message is in the `error` column), `skipped_opt_out` (digests/alerts/announcements are
  not sent to users who disabled e-mail notifications; order, account and invite mail
  always is). `dedupe_key` stamps stop time-sensitive alerts repeating.
- From-name is the tenant's display name (`Futurematch` without branding); reply-to is the
  tenant `support_email`, else the default sender.
- `email_log` rows are deleted after 365 days (`retention_service`, `RETENTION_EMAIL_LOG_DAYS`).

## Configuration

Set on the web service AND the worker service (the worker sends digests, reports, alerts).
`load_mail_config` resolves each setting in this order:

| Setting | Env, first non-empty wins | Default |
|---|---|---|
| server | `MAIL_SERVER`, `SMTP_HOST`, `SMTP_SERVER` | none (mail off) |
| port | `MAIL_PORT`, `SMTP_PORT` | `587` |
| SSL | `MAIL_USE_SSL` (`1/true/yes/on`) | on when unset and port is `465` |
| STARTTLS | `MAIL_USE_TLS` | `1`, or `0` when SSL is on |
| username | `MAIL_USERNAME`, `SMTP_USER` | none |
| password | `MAIL_PASSWORD`, `SMTP_PASSWORD` | none |
| sender | `MAIL_DEFAULT_SENDER`; else `SMTP_FROM` (with `SMTP_FROM_NAME` becomes `Name <addr>`); else the username | none (mail off) |

`APP_BASE_URL` is used for the links in the mails. Never commit `MAIL_PASSWORD`.
Redeploy / restart both services after changing the variables.

## Verify

1. Admin -> Systemstatus (`/admin/system-health`) shows the mail status and lists what is
   missing (`MAIL_SERVER`, `MAIL_DEFAULT_SENDER`, `flask_mail`).
2. Use "Send test-mail" on that page (`POST /admin/system-health/test-email`,
   `email_service.send_test_email`). It reports the SMTP error text on failure and logs
   `test` rows in `email_log`.
3. Place a test order, run the worker and inspect `/hr/leveringer`. The queue must
   move to `sent`; verify actual receipt separately. Failed/uncertain deliveries
   have explicit recovery actions (see [LAUNCH_WORKFLOWS.md](LAUNCH_WORKFLOWS.md)).
4. Before SMTP setup, queued business messages remain pending and later failed
   after bounded attempts. Existing direct-send helpers, such as welcome/reset
   mail, still log `skipped_no_backend`; check that path separately.

## Done criteria

- Server + sender configured on both services; test mail delivers.
- A test order confirmation and a test welcome mail deliver and log `sent`.
- The provider is EU-resident with a DPA.
