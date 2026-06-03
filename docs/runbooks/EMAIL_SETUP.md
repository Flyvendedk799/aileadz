# Runbook: Email / ESP Setup

The branded transactional emails — **order confirmations** and **employee
welcome/invite** mails — are implemented in `email_service.py` but **no-op
safely until a mail backend is configured**. This runbook turns them on with an
EU-resident ESP / SMTP relay (GDPR: the app and its users are Danish, so keep
mail data in the EU).

## Current behaviour (before setup)

- `send_branded_email()` renders the email, then **gates on configuration**: it
  needs *both* a mail server **and** a default sender, or it returns `False`
  quietly and logs at debug — it never raises
  (`email_service.py:148-200`, gate at `email_service.py:191-200`).
- `_mail_configured()` checks for `MAIL_SERVER` (env or app config) —
  `email_service.py:15`.
- `_default_sender()` reads `MAIL_DEFAULT_SENDER` (env or app config) —
  `email_service.py:75-76`.
- When unconfigured, every attempt is logged in `email_log` with status
  `skipped_no_backend` (`email_service.py:196`), so you can audit what *would*
  have been sent.
- Callers `send_order_confirmation()` (`email_service.py:245`) and
  `send_employee_welcome()` (`email_service.py:284`) are best-effort wrappers
  that never raise — so the rest of the app is unaffected whether or not mail is
  configured.

The actual send uses **Flask-Mail** (`from flask_mail import Message, Mail` —
`email_service.py:205-206`), so the standard `MAIL_*` config keys apply.

## Setup

### 1. Pick an EU-resident ESP / SMTP relay

Choose a provider whose sending infrastructure is in the EU and that signs a DPA
(e.g. an EU-region SMTP relay). You need: SMTP host, port, username, password,
and TLS settings.

### 2. Set the mail env vars

The two the code explicitly checks are **required**:

| Env var | Purpose | Read at |
|---|---|---|
| `MAIL_SERVER` | SMTP host — presence of this is the on/off switch | `email_service.py:15` |
| `MAIL_DEFAULT_SENDER` | From-address for all branded mail | `email_service.py:75-76` |

Flask-Mail also honours the standard keys (set whichever your relay needs):

| Env var | Typical value |
|---|---|
| `MAIL_PORT` | `587` (STARTTLS) or `465` (SSL) |
| `MAIL_USE_TLS` | `true` for port 587 |
| `MAIL_USE_SSL` | `true` for port 465 |
| `MAIL_USERNAME` | SMTP user |
| `MAIL_PASSWORD` | SMTP password (secret — keep out of git) |

Set these in the WSGI environment (see `wsgi_pythonanywhere.example.py` for the
`os.environ[...]` pattern). If you set them as Flask config instead of env vars,
make sure they land in `app.config` before the first email send — the helpers
fall back to `current_app.config.get(...)`.

### 3. Reload the web app.

## Verify

1. With `MAIL_SERVER` **or** `MAIL_DEFAULT_SENDER` missing, a send attempt logs
   `skipped_no_backend` in `email_log` and returns `False` — confirm this is what
   you saw *before* setup.
2. After setting both + reloading, trigger an order confirmation (place a test
   order) or add a test employee (welcome mail).
3. Confirm the recipient receives the branded mail and that the `email_log` row
   shows status `sent` (`email_service.py:213-215`). A `error` status row means
   the SMTP credentials/host are wrong — fix and retry.

## Done criteria

- `MAIL_SERVER` + `MAIL_DEFAULT_SENDER` (and any auth/TLS vars) set in WSGI env.
- A test order confirmation and a test welcome email both deliver and log `sent`.
- Provider is EU-resident with a DPA in place.
