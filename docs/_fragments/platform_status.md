# Part B – platform fork status (vendor, enterprise, settings hub)

Branch: `part-b-platform` (from `part-b-functionality`). Commits: `ff01748`, `5435dae`, plus the final tests/i18n commit.

| ID | Status | Notes |
|----|--------|-------|
| N-6.1 | done | Vendor `/vendor/orders` (list, confirm/decline/complete through `order_service`), nav badge for orders awaiting booking, dashboard KPI, suspended vendors locked out per request. Submission result emails (`vendor_submission_result`). Admin "send invitation again" on the vendors page. Forgot/reset password reuses Part A (`password_tokens`); `account_tokens.py` is only a thin shim with a fallback. |
| N-6.2 | done | Approve now redirects to the catalog import preview; the submission is only marked approved when the import is confirmed. Reject discards the draft and mails the vendor with the note. |
| N-6.5 | done | Search + pagination on vendors, companies, users, agreements, audit log (`admin_lists.py`, `fm/_admin_list.html`). User deactivate/reactivate (login blocked) and "send reset link". Platform AI-quality view `/admin/ai-quality` (all tenants). Cross-tenant, read-only order Detaljer `/admin/orders/<id>`. |
| N-4.6 | done | `/virksomhed/indstillinger` hub with tabs Virksomhed, Branding, SSO, API, Webhooks, Chatbot, Widget, Politik. Old URLs redirect in (GET); existing view functions are delegated to, nothing removed. Tabs filtered by `capabilities.can()`. Deactivation request from the hub. |
| N-7.1 | done | OIDC-only SSO screen with presets (Entra, Google, Okta, custom), email discovery at `/sso/discover`, `sso_login.html` migrated to `fm_base`. SAML UI is not offered (OIDC only, per plan). |
| N-7.2 | done | API-key screen (create with permission presets, list by prefix, revoke). Key shown once; only hash + prefix stored. SCIM `/Groups` (list/get/create/PUT/PATCH/delete) mapped to `company_departments`, tenant-scoped. |
| N-7.3 | done | Webhooks: events `employee.added`, `employee.updated`, `budget.overrun` (plus order events). Per-subscriber delivery state in `webhook_deliveries`; a retry only re-sends to subscribers that have not received the event. Delivery log on the webhooks page with "Gensend" (HR-only, company-scoped). Signing secret kept when the field is left blank. |
| N-8.4 (mine) | partial | Flashes/JSON errors in `enterprise_api`, vendor portal, admin dashboard, SSO, settings hub are Danish with æøå restored. Not swept: long-tail English strings in large files I did not otherwise touch (e.g. deep `admin_dashboard` AI-settings helpers, internal log messages – intentionally English). |
| N-8.5 (mine) | partial | Empty states on vendor orders, delivery log, API keys, AI quality, admin lists (“ingen resultater” + clear-search). Error states on pages that query optional tables. Loading skeletons were not added – pages are server-rendered, only the AI-quality snapshot can be slow. |

## Tests
`tests/test_vendor_orders.py`, `test_admin_polish.py`, `test_settings_hub.py`, `test_webhooks_scim.py` on the in-memory SQLite harness (`tests/sqlite_platform.py`). Happy paths plus permission boundaries (non-admin, employee vs HR, cross-tenant).

## Merge notes (shared files)
- `run.py`: registers `settings_hub_bp` and `sso_discovery_bp` (one import + two `register_blueprint`).
- `companies/__init__.py`: admin list search/paging, `employee.added/updated` emission, `hub_redirect` in settings/branding, support-email fix.
- `admin_dashboard.py`, `vendor_portal.py`: additive routes and helpers; Part A edits are in separate functions (forgot/reset, invite) so conflicts should be textual only.
- `event_bus.py`: per-subscriber delivery rewrite of `_deliver_to_subscribers`, new `resend_delivery`.
- `schema_registry.py` `REGISTRY_DDL`: adds `account_tokens`, `webhook_deliveries`, `account_requests`, `users.status`. If Part A also adds a token table, keep Part A's and drop `account_tokens` (the shim falls back only when `password_tokens` is missing).
- `scim_api.py`: one appended `import scim_groups`.
- `auth/__init__.py`: deactivated-user check at login.
