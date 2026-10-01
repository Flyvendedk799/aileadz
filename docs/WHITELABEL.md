# Whitelabel (tenant branding)

Enterprise tenants can replace platform branding (name, logo, colors, favicon, optional
CSS/JS) on the web app, the login page, e-mails and the embeddable chat widget.

**Source of truth:** `company_settings` (+ `company_brand_assets` for uploaded logos/favicons),
read and written only through `branding_service.py`. Legacy `companies` branding columns are
dual-written on a live save (`sync_legacy_companies_columns`).

## Tenant URLs (slug-based; no custom domains)

| Purpose | URL |
|---|---|
| Branded login | `/login/<company_slug>` or `/login?tenant=<company_slug>` (`auth/__init__.py`) |
| Self-register into the tenant | `/register?tenant=<company_slug>`: joins as `employee` only when the e-mail domain equals the company's `company_domain` |
| SSO | `/sso/login/<company_slug>/<provider>` (`enterprise_sso`). Only `oauth2` is enabled; SAML, LDAP and Active Directory are disabled server-side (`LIVE_SSO_PROVIDERS`). |

`company_slug` is generated from the company name when a platform admin registers the tenant at
`/companies/register`.

## Setup

1. Platform admin registers the tenant (`/companies/register`).
2. Platform admin enables branding: `POST /companies/branding/toggle-feature` (button on the
   branding hub) sets `companies.features.custom_branding` and `company_settings.enable_white_label`
   (`branding_service.set_custom_branding_feature`).
3. A `company_admin` / `hr_manager` opens `/companies/branding` (GET redirects into the settings
   hub, tab "branding"; a platform admin can use `/companies/branding/<company_id>`). Template:
   `templates/fm/branding.html`; tabs Identitet, Visuelt, Assets, Avanceret, Historik.
4. The form posts `action=save_draft` or `action=publish`. `save_draft` stores the payload as JSON in
   `company_settings.branding_draft` and sets `branding_status='draft'`; `publish` writes the live
   columns and sets `branding_status='live'`. Every change is logged in `company_settings_history`
   (the Historik tab).

Without the `custom_branding` feature (and not a platform admin) the hub drops
`hide_platform_branding`, `custom_css` and `custom_js` from what it saves. There is no separate
"enterprise tier" check beyond that flag.

## Runtime behavior

- `white_label_global_integration.register_white_label_context_processor` injects, into every
  template: `white_label_active`, `company_branding`, `platform_name`, `hide_platform_branding`,
  `tenant_slug` (`branding_service.get_template_context`). It uses the session company for logged-in
  company users and the slug from the URL (`slug`, `company_slug` or `?tenant=`) before login.
- Branding is active when `enable_white_label` is set AND the `custom_branding` feature is on
  (`branding_service.is_whitelabel_active`; a platform admin session only needs
  `enable_white_label`).
- `hide_platform_branding` only takes effect while branding is active; `fm_base.html` then drops
  the Futurematch references.
- Caveat: `_row_to_branding` reads `branding_draft` instead of the live columns whenever
  `branding_status='draft'`, so a saved draft is what `get_branding()` returns to everybody until
  a live save; it is not an internal-only preview.

## Embeddable chat widget

Configured at `/hr/widget` (settings hub, chatbot -> widget; `hr_dashboard.widget_creator`,
template `templates/fm/widget_creator.html`). Embed snippet loads
`<host>/app1/widget/<widget_token>/loader.js` (`app1/__init__.py`, `widget_loader_js`).

## API

`GET /api/v1/company/branding`, API key with scope `read:branding` (`enterprise_api`). Returns
`success` and `data`: `company_slug`, `active`, `company_name`, `logo_url`, `primary_color`,
`secondary_color`, `accent_color`, `login_url_path` (`/login/<slug>`), `widget_token`.

Webhook deliveries include `company_slug` in the body and the `X-Company-Slug` header
(`WEBHOOK_VERIFICATION.md`).

## E-mail

`email_service.send_branded_email()` takes a branding dict (resolved per company by
`email_service._resolve_branding`) and uses the tenant display name as the From name and
`support_email` as reply-to. Needs a configured mail backend: `runbooks/EMAIL_SETUP.md`.

## Files

| File | Role |
|---|---|
| `branding_service.py` | read / write / gate / publish / history |
| `white_label_global_integration.py` | template context processor |
| `templates/fm/branding.html` | branding hub |
| `companies/__init__.py` | `branding` and `toggle_branding_feature` routes |
| `enterprise_company_settings.py` | theme templates, asset uploads, theme preview |
| `email_service.py` | branded e-mails |

## Troubleshooting

- Branding not visible: check `features.custom_branding` on the company and `enable_white_label`
  in `company_settings`.
- Logo not showing: `logo_url` in `company_settings`, or an uploaded primary
  `company_logo_primary` asset in `company_brand_assets`.
