# Whitelabel Guide

This document describes how tenant branding works in Futurematch / AiLeadZ.

## Overview

Whitelabel lets enterprise tenants replace platform branding with their own identity (name, logo, colors) across the web app, login, chat, and embeddable widget.

**Single source of truth:** `company_settings` (+ `company_brand_assets` for uploads), accessed via `branding_service.py`.

## Tenant URLs (slug-based)

Custom domains are **not** supported. Use company slugs:

| Purpose | URL pattern |
|---------|-------------|
| Branded login | `/login/<company_slug>` |
| Login query param | `/login?tenant=<company_slug>` |
| SSO (LDAP form) | `/sso/login/<company_slug>/<provider>` |

After tenant registration, HR receives a slug login URL like `/login/acme-corp`.

## HR admin setup

1. Platform admin creates tenant at `/companies/register`
2. Platform admin enables **custom branding** (toggle in Branding hub, or `companies.features.custom_branding`)
3. HR admin opens **Branding** at `/companies/branding`
4. Configure tabs: Identity → Visuals → Assets → Advanced → Widget
5. **Save draft** to preview internally, then **Publish** to go live

### Entitlements

| Feature | Tier |
|---------|------|
| Identity, colors, widget link | When `custom_branding` enabled |
| Hide platform branding, custom CSS/JS, theme templates, audit history | Enterprise |

## Runtime behavior

- Branding injects via Flask context processor when user is a company user **or** on pre-login slug routes
- Gating: `custom_branding` feature flag + `enable_white_label` in `company_settings`
- `hide_platform_branding` removes Futurematch references in footer/sidebar

## Embeddable widget

Configure at `/hr/widget`. Colors sync from branding hub; optional tenant logo on widget header and loader button.

Embed snippet (from Branding → Widget tab):

```html
<script src="https://YOUR-HOST/app1/widget/YOUR_TOKEN/loader.js" async></script>
```

## API

```
GET /api/v1/company/branding
Authorization: Bearer <api_key>
Scope: read:branding
```

Returns: `company_slug`, colors, logo URL, login path, widget token.

Webhooks include `company_slug` in payload and `X-Company-Slug` header.

## Email (transactional)

Use `email_service.send_branded_email()` with templates: `welcome`, `password_reset`, `order_confirmation`.

Requires `MAIL_SERVER` env configuration. From-name uses tenant display name.

## Files

| File | Role |
|------|------|
| `branding_service.py` | Read/write/gate/publish |
| `white_label_global_integration.py` | Template context |
| `templates/companies/branding.html` | Unified admin hub |
| `email_service.py` | Branded emails |

## Troubleshooting

- **Branding not visible:** Check `custom_branding` in `companies.features` and `enable_white_label` in settings
- **Draft not live:** Publish from branding hub (`branding_status` must be `live`)
- **Logo not showing:** Ensure `logo_url` in `company_settings` or upload via Assets tab
