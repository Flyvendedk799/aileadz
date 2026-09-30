# Futurematch / aileadz: Completion Plan

> Written 2026-09-30 from a six-part code audit of the whole repo: learner journey, AI systems, HR/company, platform admin/vendor/catalog/orders, enterprise/infra/security/tests, and a mechanical link/navigation sweep.
> **Rule:** complete, don't remove. When two things overlap, merge them into one canonical version and redirect the other. Nothing is dropped.
> Sizes: **S** ≈ ≤ ½ day · **M** ≈ 1–3 days · **L** ≈ 1–2 weeks. `file:line` anchors are approximate. If one has drifted, search for the symbol.
> This plan replaces the March-era `PHASE_PLAN.md`, `INTEGRATION_PLAN.md`, `plan.md`, `AI_ENGINE_IMPROVEMENT_PLAN.md` and `app1/UX_AUDIT.md`. Their open items are merged in below.
>
> **Structure:**
> - **Part A: Security & privacy.** Access control, secrets, auth, identity, data protection. IDs `S-x.y`.
> - **Part B: Everything else.** Functionality, flows, experience, AI, data plumbing, ops, polish. IDs `N-x.y`.
>
> The parts run in parallel, but **Part A tier S-1 goes before anything ships wider**. Where an item depends on the other part, it says so.

---

## Diagnosis in one paragraph

Each piece is well built on its own. Nothing is broken at the link level: all 819 `url_for` calls resolve, every template exists, every fetch path has a route, and every AI tool has a dispatcher. What's missing is the **hand-offs between the pieces**:

- An approved order becomes "Afventer betaling".
- Completing a course never updates skills.
- HR assignments are invisible to the learner.
- The vendor never hears about an order.
- Vendor-uploaded courses never reach the AI.
- Emails probably never send.
- Background jobs run inside user requests.
- Around 15 finished pages have no link to them.
- Three to four parallel versions exist of several core ideas: order status, catalog source, notifications, analytics/report pages, goals, and department views.

Separately, there is a set of real security holes: an unauthenticated debug console, a backdoor admin route, a committed Shopify token, an insecure default secret key, colleague PII visible to employees, and no CSRF protection.

---

# PART A: SECURITY & PRIVACY

Ordered by urgency. S-1 is "this week". The later tiers can interleave with Part B.

## S-1: Critical, fix now · ~3–5 days

| # | Item | Evidence | Done looks like | Size |
|---|---|---|---|---|
| S-1.1 | **Rotate the committed Shopify admin token**, delete `app1/count.py` and `app1/Pypy`, and purge them from git history. Old MySQL/SSH passwords are also in the `run.py` history, and `.gitleaks.toml` allowlists `run.py`. | `app1/count.py:5`, `app1/Pypy:6` (verified) | Token rotated. Sync reads the token from env (N-3.1). History purged. Any old DB/SSH passwords still in use are rotated. Allowlist removed. | S |
| S-1.2 | **The AI debug console needs no login.** Anyone can read every user's chat debug log or wipe it. `/app1/dashboard` (observability) is open too. | `app1/__init__.py:1818-1895`, `:911` (verified) | `@require_role('admin')` on all of them, plus tests. | S |
| S-1.3 | **Backdoor route:** `GET /admin/make-superadmin/<username>` lets the hardcoded user `'Mastek123'` promote anyone. Because it's a GET, a crafted link is enough. | `admin_dashboard.py:936-946` (verified) | POST only, real admin only, audited. Or a CLI command instead. | S |
| S-1.4 | **Insecure default SECRET_KEY** (`'your_secret_key_here'`). If it's unset, sessions can be forged (`role='admin'`), and the AI-key and SSO-secret encryption keys derive from it. | `run.py:158` (verified), `ai_secrets.py:121`, `enterprise_sso:98` | Boot fails without the key unless `SANDBOX=1`. Confirm it is set on the VPS. If it ever wasn't, rotate it and re-encrypt the stored secrets. | S |
| S-1.5 | **Employees can see colleagues' orders and PII** (names, emails, phone numbers) via `/multitenant-reports/*`, which only checks company membership. `/hr/learning-paths` GET is similar. `/api/search` lets any employee list colleagues' emails. | `multitenant_reports.py:112,533`, `hr_dashboard:3330`, `search_api.py:41-62` | Role-gated to HR roles. Search limited by role. | S |
| S-1.6 | **Department heads can do too much.** They can reset *any* user's password (the new password is flashed in plaintext), deactivate users, edit their own budget, approve for any department, and edit suppliers, the chatbot and the widget token. | `hr_dashboard:46,914,1046,2425,2483` | Interim fix: restrict these actions to `hr_manager`/`company_admin`. Full fix: S-2.3. | S |
| S-1.7 | **Cross-tenant prompt injection.** Thumbs-up'd Q&A from *any* company is put into every user's prompt, and the text is sent by the client. | `agent.py:1389`, `memory_store.py:210`, `chat.js:405` | Scoped by `company_id`. Text taken from the server-side transcript. Admin review gate before reuse. | S |
| S-1.8 | **SAML login can be forged**: only the *presence* of `<ds:Signature>` is checked. The LDAP filter is injectable. SSO puts `company_users.id` into `session['user_id']`, which the app treats as `users.id`, so an SSO user can take on another user's identity. | `enterprise_sso:424-428,543,650` | **SAML and LDAP/AD are disabled server-side** (routes refuse, and existing configs are kept but inactive) until the deferred item S-D.1. OAuth2/OIDC stays off until S-3.1 fixes the user-id and nonce handling. | S |
| S-1.9 | **Stored XSS**: `n.message\|safe` renders HR-authored notification text raw. The widget loader interpolates the HR-authored title into JS unescaped. | `notifications.html:52`, `app1/__init__.py:2431` | Sanitised (bleach) and escaped. | S |
| S-1.10 | **Deactivated or SCIM-removed users stay logged in**, because the decorators trust the session alone. | `auth_decorators.py:138-275` | Status re-check in the decorators (cached ~60 s). Sessions revoked on deactivate. | S |
| S-1.11 | **`/app1/voice` has no auth or rate limit**, so anyone can spend the Whisper budget. | `app1/__init__.py:1013` | Login required plus a per-user rate limit. | S |

**Exit:** no unauthenticated sensitive routes, secrets rotated, SSO disabled or safe, and a regression test for each of the items above.

## S-2: Authentication & access control · ~2–3 weeks

| # | Item | Done looks like | Size |
|---|---|---|---|
| S-2.1 | **CSRF protection:** none of the 78 POST forms has a token, and SameSite=Lax is the only defence. | Flask-WTF `CSRFProtect`, the token in the `fm_base` meta, sent by `fetch` helpers. API/SCIM (key-authenticated) exempt. | M |
| S-2.2 | **Login hardening:** no rate limiting or lockout, plaintext-password fallback accepted (`auth/__init__.py:66`), logout is a GET, no password policy (the UI promises ≥10 characters). | Rate limit + lockout, plaintext fallback removed after forcing a rehash, POST logout, policy enforced on register, reset and settings. | M |
| S-2.3 | **One role → capability matrix** in `auth_decorators`, used by the decorators *and* the sidebar (N-2.3):<br>• `company_admin ⊃ hr_manager ⊃ department_head (scoped to own dept) ⊃ employee`<br>• department scoping on approvals, budgets (read-only own), employees, analytics<br>• the stored per-role `permissions` JSON (`companies:518-541`) is folded in as company overrides<br>• fix the HR agent's audience filter, which uses non-existent role names (`hr_tools.py:3950`) | Tests for every role × sensitive route. | L |
| S-2.4 | **No more plaintext passwords handled by humans:**<br>• forgot-password/reset for users *and* vendors (tokenised email; the `password_reset` template already exists; the dead link is `login.html:109`)<br>• HR "reset password" becomes "send reset link" (`hr_dashboard:2425,2475`)<br>• employee invite with set-password, reusing the vendor invite-token pattern (`admin_dashboard.py:604-680`) | Tokens expire and are single-use. The UX side is N-2.1. | M |
| S-2.5 | **Impersonation:** viewing a company detail page silently switches the admin's acting company without saving their own context, and it isn't audited. | Explicit "act as" with a banner, an "exit" that restores, and an audit log entry. `companies/__init__.py:981`, `admin_dashboard.py:1083` | S |
| S-2.6 | **2FA** (TOTP) for platform admins and company admins. | Enforced for `admin` and optional for others. | M |
| S-2.7 | **Vendor auth:**<br>• suspending a vendor doesn't end their session (`vendor_auth.py:377`)<br>• `vendor` is taken from the CSV, not the session (`catalog_service.py:1030`)<br>• handle collisions let one vendor overwrite another's courses (`:369-379`) | Status checked in `vendor_login_required`, vendor forced from the session, collisions blocked. | S–M |

## S-3: Enterprise identity & integrations · ~2 weeks (after S-2.3)

| # | Item | Done looks like | Size |
|---|---|---|---|
| S-3.1 | **OAuth2/OIDC SSO done properly** (SAML/LDAP are deferred, see S-D.1):<br>• validate the nonce, the `id_token` signature, the issuer and the audience<br>• `provision_user` also creates the `users` row, and the session carries `users.id` (`enterprise_sso:299,650`) | Re-enable OAuth2/OIDC. Tests for the provider flow. The UX side is N-7.1. | M |
| S-3.2 | **API keys:**<br>• stored in plaintext next to the hash column<br>• accepted as `?api_key=` in the URL, so they end up in logs (`enterprise_api:620,1308`) | Hash only (migrate, then drop the column), header-only, keys shown once, last-used tracked. | S |
| S-3.3 | **Webhooks:**<br>• delivery follows redirects, which bypasses the SSRF check (`event_bus.py:304`)<br>• the signature has no timestamp, so deliveries can be replayed | No redirect following, re-check the resolved IP, timestamped HMAC plus a verification doc for customers. | S |
| S-3.4 | **SCIM:** creates `company_users` rows with no identity. | Creates a proper login identity (or an SSO-only one), consistent with S-3.1. | M |

## S-4: Privacy & GDPR · ~1–2 weeks

| # | Item | Done looks like | Size |
|---|---|---|---|
| S-4.1 | **Erasure coverage:**<br>• keyed by username, so SSO and SCIM users are missed<br>• skips the `employee_*` tables, `email_log`, the CV store, `audit_log` and `api_request_logs`<br>• the SQLite AI store is written on every turn but called "orphaned" (`gdpr_service.py:198`) | Keyed by user id/email. Covers every table, with a test that fails when a new table with personal data isn't covered (extend `test_gdpr_table_coverage`). Audit log pseudonymised, not deleted. | M |
| S-4.2 | **Self-service data rights:**<br>• `/mine-data` export exists but nothing links to it<br>• erasure is only possible by emailing support | Linked from settings and privacy. An "Anmod om sletning" request creates an admin DSR ticket with an SLA. | S |
| S-4.3 | **Retention job** for chat transcripts, debug logs, API logs and email logs. | Configurable per-table retention, run by the worker (N-8.1). | M |
| S-4.4 | **HR-written goals: per-goal sharing** (decided). Each HR goal (`employee_goals`) gets a **"Del med medarbejder" toggle**, off by default:<br>• **Shared** goals are visible to the learner on `/mine-maal` ("Mål fra din leder") *and* in the learner AI's context.<br>• **Unshared** goals stay HR-only. They never reach the learner UI, the learner AI, learner exports or the learner's GDPR export view.<br>• The HR goal view is split into two sections, **"Delt med medarbejderen"** and **"Kun synligt for HR"**, and a goal can be moved between them.<br>• The global `AI_LEARNER_HR_GOALS` flag becomes a company-level setting (default on), with per-goal sharing inside it.<br>• Sharing and unsharing is audit-logged. | Migration adds `employee_goals.shared_with_employee` (default 0) and `shared_at`. `learner_context.py` and every learner-facing query filter on it. Tests prove unshared goals never leak into the learner view, the AI context or learner-facing exports. The UX side is N-3.5. | M |
| S-4.5 | **AI data residency:** the SQLite AI store (feedback reasons, anonymous profiles) is per-server and outside backups and the DSR process. | Covered by N-3.3's move to MySQL. Verify it with S-4.1's coverage test. | (N-3.3) |

## S-5: Platform hardening & verification · ongoing, ~1 week total

| # | Item | Size |
|---|---|---|
| S-5.1 | Enforce the CSP (it is report-only today) and add HSTS. | S |
| S-5.2 | `/readyz` publicly exposes the feature matrix. Keep liveness public and put the details behind admin login or a token. | S |
| S-5.3 | CI: full-history gitleaks on a schedule, `pip-audit`, and dependency pinning review. | S |
| S-5.4 | **Security test suite**, which is missing today:<br>• auth/login/reset<br>• **tenant isolation** (a user can't read another tenant's or colleague's data, per route family)<br>• role matrix<br>• SSO/SCIM/API-key auth<br>• webhook signing<br>• CSRF presence | M |
| S-5.5 | Upload type and size checks for admin broadcasts and CV upload (`admin_notifications.py:91-125`). | S |
| S-5.6 | Widget embedding: the origin allowlist is enforced against our own host, so it never actually protects anything (`app1/__init__.py:2021,2301`). The fix is in N-5.6. Verify here that the allowlist actually rejects foreign origins once it lands. | (N-5.6) |

## S-D: Deferred (decided 2026-09-30)

| # | Item | Condition to pick up | Size |
|---|---|---|---|
| S-D.1 | **SAML and LDAP/Active Directory SSO**:<br>• verify the SAML signature against the stored certificate (`python3-saml`/`xmlsec`), and validate audience, destination and time windows<br>• escape the LDAP filter (`enterprise_sso:424-428,543`)<br>• add the SAML config fields (ACS/metadata URL) | When an enterprise customer requires SAML or LDAP. Until then S-1.8 keeps them disabled server-side and N-7.1 hides them in the UI. No code is removed. | L |

---

# PART B: FUNCTIONALITY, EXPERIENCE & OPERATIONS

## Part B status (branch `part-b-functionality`)

Legend: [x] done in code and tests, [~] done with a named gap, [ ] not done. Steps that only the owner can perform are listed in `docs/USER_ACTIONS_PART_B.md`; nothing below is ticked on the strength of a production check.

- [x] N-0.1 CI signal (timeout, pytest-timeout, `testpaths`, hang fixed)
- [~] N-0.2 E-mail: dependency, config loading, honest probe and test button done; **one real delivery from the VPS is an owner action**
- [x] N-0.3 Danish 404 and JSON 404
- [x] N-1.1 One order lifecycle (`order_lifecycle.py`, `order_service.py`, `booked` step, history)
- [x] N-1.2 Learner sees and acts on orders (`/min-ordre/<id>`, cancel, calendar)
- [x] N-1.3 One completion path
- [x] N-1.4 Completion grows skills
- [x] N-2.1 Account lifecycle UX (adapts to Part A tokens when merged)
- [x] N-2.2 First run per role
- [x] N-2.3 Capability-based navigation (`capabilities.py` is the seam for the S-2.3 matrix)
- [~] N-3.1 One catalog: single source, index, admin rebuild, scheduled sync, DB vendor profiles, stale exclusion, provider-aware categorisation done; **the Shopify token and moving the JSON file are owner actions**
- [x] N-3.2 One notification system
- [x] N-3.3 One AI analytics store
- [x] N-3.4 One schema story (single definitions, fingerprint boot stamp, Alembic `b001_part_b_lifecycle`)
- [x] N-3.5 Goals, budgets and team view (goal sharing delegates to Part A `goal_sharing` when present)
- [~] N-4.1 Analytics and reports consolidated; Virksomhedsrapporter kept as a drill-down and the personal credit pages are two tabs, not one
- [x] N-4.2 Scheduled reports
- [x] N-4.3 Compliance and certifications
- [x] N-4.4 Learning paths create real work
- [x] N-4.5 Actionable insights
- [x] N-4.6 Settings hub
- [x] N-4.7 Approvals UX
- [x] N-4.8 Small HR items
- [x] N-5.1 Profiler: say it and do it
- [x] N-5.2 Chat UI completeness (team orders follow the company policy)
- [x] N-5.3 HR assistant parity
- [x] N-5.4 Vendor assistant parity
- [x] N-5.5 Runtime correctness
- [~] N-5.6 Widget: parent origin enforced through a signed iframe-held session token plus `frame-ancestors`, real preview, suggestions event, tests done; **confirm cards are not rendered in the widget** (anonymous visitors have no write tools) and **the browser check on a real embedding site is an owner action**
- [x] N-5.7 Eval coverage (70 cases, fluency scorer, HR/vendor scopes, nightly on both providers; needs repo secrets)
- [x] N-5.8 Guest memory migrates on login (`anon_migration.py`); prompt A/B through `AI_PROMPT_VARIANTS`
- [x] N-6.1 Vendors receive and act on orders
- [x] N-6.2 Submission flow
- [x] N-6.3 Billing management (payment off-platform)
- [x] N-6.4 Credits as AI-usage metering
- [x] N-6.5 Admin polish
- [x] N-7.1 OIDC-only SSO screen (SAML hidden)
- [x] N-7.2 API keys and SCIM groups
- [x] N-7.3 Webhooks
- [~] N-8.1 Worker: heartbeat, `/readyz` block, opportunistic switch done; **creating the ServerHoster worker service is an owner action**
- [x] N-8.2 Observability (request id, JSON logs, Sentry hook, ops alerts, Redis cache)
- [x] N-8.3 CI (ruff, coverage, bootstrap test, nightly eval)
- [~] N-8.4 Language: HR, vendor, admin, SSO, settings, API messages Danish; long-tail strings in untouched large files remain
- [~] N-8.5 Empty/error states on every list page touched; loading skeletons not added (pages are server-rendered)
- [x] N-8.6 Repo hygiene


## N-0: Unblock the pipeline & silent prod failures · ~3–5 days

| # | Item | Evidence | Done looks like | Size |
|---|---|---|---|---|
| N-0.1 | **CI gives no signal.** Every run is cancelled at the 6 h limit because `test_fabricated_price_triggers_grounding_disclaimer` hangs. Three tests fail: the `asset_version` Jinja global is missing in the test env, and one file is opened without an encoding. Bare `pytest` collects `sandbox/test_ai.py`, which calls `sys.exit`. | `tests/test_ask_sse_offline.py`, `ci.yml:107` (verified) | Job `timeout-minutes: 20`, `pytest-timeout`, `pytest.ini` with `testpaths = tests`, the hang fixed, green. | S |
| N-0.2 | **Email is probably not sending.** `flask_mail` is missing from `requirements.txt`, the `MAIL_*` env vars are never copied into `app.config`, and the health probe says "available" whenever `smtplib` imports. | `email_service.py:15,367,377`, `feature_status.py:176` | The dependency is added, config is loaded, the probe checks real config, and admin gets a "send test email" button. One real delivery from the VPS verified. | S |
| N-0.3 | **404 handler** returns a redirect with status 404, so the browser shows "Redirecting…". | `run.py:417` | A Danish 404 page, and JSON 404 for API callers. | S |

## N-1: Close the core loop: the order lifecycle · ~2–3 weeks

This is the product's spine: **discover → request → approve → book → attend → complete → skills grow → next step**. It breaks at every arrow today. Fix it as one piece of work.

### N-1.1 One canonical order lifecycle (L, the keystone)
- Define `ORDER_STATUSES` plus an allowed-transition map in `order_service`, with Danish labels and a learner "bucket" for each status. Every label map imports it:
  - `order_handler.py:21`
  - `futurematch_ui.py:295`
  - `mt_order_detail.html:26`
  - `hr_dashboard:670`
  - `multitenant_reports.py:596`
- Lifecycle (**decided**): `pending_approval → approved → booked → completed`, with the side exits `rejected` and `cancelled`.
  - `booked` is a real step for every vendor: the vendor has confirmed the seat.
  - For vendors without portal access, HR or a platform admin can mark an order `booked` on the vendor's behalf, and the actor is logged.
  - Learner labels: "Afventer godkendelse" → "Godkendt – afventer booking" → "Booket" → "Gennemført".
- **Billing is a separate dimension**: `billing_status = not_invoiced | invoiced | paid`. Billing happens off-platform (see N-6.3), so this is status management only. Migrate existing `invoiced`/`paid` rows.
- `set_status` enforces transitions. Side effects are keyed on transitions: budget charge/refund, email, in-app notification, webhook, `approved_by`.
- **Fix the approval bug.** Approving via the queue *and* the HR AI tool sets `pending`, so the learner sees "Afventer betaling", gets no email, and no `order.approved` webhook fires.
  - Where: `hr_dashboard/__init__.py:976`, `hr_tools.py:2677` (verified).
  - Fix: target `approved`, with the approval row and status change in one transaction. Today the approval commits even if `set_status` fails.
- **Route every writer through `set_status`**:
  - the raw SQL in `multitenant_reports.py:574-650` (cancel doesn't refund the budget)
  - the HR inline completion in `hr_dashboard:738+`
  - the dead `order_handler.update_order_status`
  - billing updates
- **Migration** for the billing columns the billing page queries but no DDL creates (`billing_status`, `invoice_date`, `payment_method`, `payment_reference`, `billing_note`, which vs `billing_notes` needs one name). Where: `hr_dashboard:2585-2600` vs `enterprise_tables.py:376`.
- **Order creation honesty.** `order_handler.create_order`:
  - returns `success: True` when the DB write failed;
  - sends a duplicate confirmation email;
  - returns fake payment details (MobilePay "12345", bank "1234-567890", phone "+45 12 34 56 78").

  Fix: fail truthfully, send one email (from `order_service`), and fix `_parse_price("1.995,00") → 0`. Where: `order_handler.py:81-233`.
  - The fake payment block is replaced by an honest off-platform message, e.g. "Du modtager faktura fra [leverandør/Futurematch]". No payment details are ever shown in the app.
- **Idempotency guard.** No duplicate open order for the same user + product + variant within N minutes. This closes the chat double-confirm path: a "ja" plus a stale Bekræft card.

### N-1.2 Learner sees and acts on their orders (M)
- Timeline rows become clickable and open a **learner order detail** page with status history, the vendor, the date, "Tilføj til kalender" (.ics, reusing `hr_ext.py:328`) and **Annullér** (the route exists at `order_routes.py:238`; it has no UI).
- Product page:
  - Show "Du har allerede anmodet om dette kursus".
  - Make the modal copy role-aware. It says "sendes til HR-godkendelse" even to users without a company.
- Confirmation banner: add "Se status på din tidslinje".
- Write `completion_deadline` from the path due date, the HR assignment or the variant date. It is read in 6 places and written nowhere.

### N-1.3 One completion path (M)
- Add `order_service.complete_order()` and use it from:
  - AI `mark_course_complete` (`app1/tools.py:5057`), which only sets `completion_status`, so the timeline still says "Godkendt";
  - the HR mark-complete;
  - `order_handler.py:308`.
- One function does all of the following:
  - updates `status`
  - writes `user_completed_courses` (the profile's course list is empty today)
  - updates `employee_learning_progress` and the counters
  - emits `course.completed`
  - notifies the manager
- A learner **"Markér som gennemført"** button.

### N-1.4 Completion grows skills (L, the main value gap)
- A completion moment with:
  - skills proposed from the course's catalog metadata (accept/adjust level);
  - a review prompt (reviews work at `catalog_routes.py:431`; nothing asks for one);
  - "næste skridt" suggestions;
  - "tal med AI om det".
- A manager task: "bekræft kompetenceløft" (the HR uplift flow is at `hr_dashboard:1450`).
- **Skill-history ID mismatch.** `record_user_snapshot` keys on `company_users.id`, but HR and `learner_context.py:26` use `users.id`. Standardise on `users.id`, add a helper, and backfill (`skill_history.py:185`).
- Route both CV-apply paths through one function. The no-JS fallback skips `record_user_snapshot` (`futurematch_ui.py:670`).

**Exit:** one scripted E2E test (request → approve → vendor confirms → complete → skill recorded → manager confirms) passes, and every status label comes from one constant.

## N-2: Onboarding, first run & navigation · ~2 weeks

### N-2.1 Account lifecycle UX (M), built on S-2.4
- Screens and emails for forgot-password, invite → set password, and a **bulk CSV invite UI** (the backend is at `enterprise_api:1837`).
- Registration: `?tenant=` joins the company, and the flash messages are in Danish.

### N-2.2 First run, per role (M)
- **Employee:**
  - land on `/min-laering`, not `/dashboard` (`auth/__init__.py:75,81`);
  - a welcome card: "Fortæl AI'en om dig", "Upload CV", "Udforsk kataloget";
  - record `first_login_completed`.
- **HR:**
  - render the onboarding checklist that is **already computed** (`hr_dashboard:460-478`) but never read by `hr.html`;
  - make it dismissible.
- **Company registration:**
  - also create a `company_admin` (only `hr_manager` exists today, so admin-only screens are unreachable);
  - send a welcome email with a set-password link.
- `/dashboard` for anonymous users: redirect to login or the public catalog.

### N-2.3 Navigation per role (M), reads the S-2.3 matrix
- Every learner (employees, managers, solo users) gets **"Min læring"**.
- The **Virksomhed** block is shown by capability. Today employees see 11 links that all bounce with an English error (`fm_base.html:30,112`).
- **Link the orphaned pages:**
  - company settings, branding, SSO config, webhooks, API keys (settings hub, N-4.6)
  - HR chatbot/session history, chatbot settings, widget builder
  - `/hr/my-department`
  - `/admin/agreements`
  - `/mine-data` (S-4.2)
  - the vendor-portal login in the footer
  - learner: orders/timeline, learning paths, goals, calendar
- Add the HR subnav, and with it the embedded AI panel, to the ~13 HR pages that lack it: employees, add/edit employee, employee_details, benchmarking, company_analytics, company_reports, company_settings, branding, webhooks, chatbot_settings, widget_creator, order_details.
- Merge the duplicate "CV-portal" and "Upload CV" entries.
- One `_admin_subnav.html` instead of 7 variants.
- **White-label bug:** `shell.js:203 applyBrand()` overwrites the server-rendered company name and logo with localStorage defaults. Server values must win.
- `manager_user_id` settable on add-employee.

## N-3: Single sources of truth (data plumbing) · ~2–3 weeks

### N-3.1 One catalog (L)
- Today there are three readers:
  - `catalog_service`
  - `rag.load_augmented_products` (Shopify only)
  - `app1.load_products` (cached forever)

  So vendor/CSV courses and AI category overrides **never reach AI search or chat ordering** (`tools.py:5174`, `order_routes.py:66`).
- **Done:**
  - `catalog_service.get_products()` feeds everything.
  - RAG becomes an index over it.
  - An incremental embed runs on import or category confirm.
  - Admin gets "Genopbyg indeks" with status.
  - A scheduled Shopify sync (env token, S-1.1).
  - The 17 MB JSON is untracked.
- Admin **product browser**: list, search, edit, unpublish.
- Freshness: hide/archive actions; stale courses are excluded from recommendations.
- Vendor profile: the DB is the source, not `app1/vendor_profiles.json`.
- AI categorisation respects the provider toggle (`catalog_service.py:1199`).
- Chat orders price the chosen variant, not `variants[0]` (`tools.py:5186`). Catalog-request `notes` are persisted (`catalog_routes.py:541`).

### N-3.2 One notification system (M)
- Today there are two tables (`notifications` with a username as `user_id`, and `company_notifications`). The page, the KPI and the badge each count differently. Reading a broadcast marks it read for the whole company.
- **Done:**
  - one table with per-user read state;
  - `action_url` on every notification;
  - cert-expiry and broadcasts migrated in;
  - the internal dedup marker hidden (`deadline_service.py:231`);
  - `/hr/notifications` fixed (it passes the wrong variable, so it always renders empty; `hr_dashboard:2554`);
  - one mark-read endpoint;
  - broadcasts can target a company.
- The email-notifications preference is stored but ignored by every sender. Honour it (transactional emails excepted).

### N-3.3 One AI analytics store (M)
- The SQLite `app1/ai_memory.db` (feedback reasons, debug logs, latency, anonymous profiles) moves to MySQL. It is per-server and lost on redeploy today.
- **Feedback fix:**
  - `chat.js` sends ±1;
  - admin counts only `>0`, so thumbs-down is invisible (`admin_reports.py:222`);
  - HR treats `>0 AND <=2` as "low", so thumbs-up reads as dissatisfaction (`hr_tools.py:1786`);
  - the rating lands on the session's latest row, not the rated message (`app1/__init__.py:1143`).

  **Done:** one scale, a `message_index`, one MySQL feedback table read by both dashboards.
- Chat → order conversion: populate `chatbot_session_id` / `chatbot_queries_before_order` on orders, and verify it.

### N-3.4 One schema story (L), before the big migrations in N-1.1 and N-3.2
- Today:
  - Alembic has never been run;
  - there are ~90 scattered `ensure_*` calls;
  - `users`, `brands`, `notifications`, `credit_usage`, `app_usage` and `social_metrics` are created by no code;
  - `course_orders`/`order_approvals` are defined twice with different shapes (`futurematch_ui.py:321` vs `enterprise_tables.py:357`);
  - `audit_log` has duplicate column pairs;
  - schema sync is skipped for 6 h via a `/tmp` stamp.
- **Done:**
  - an Alembic baseline from the prod schema;
  - all changes go through Alembic, and `ensure_*` becomes a boot-time verify;
  - one definition per table;
  - a fresh-DB bootstrap test in CI.

### N-3.5 One goals view, one department budget, one team view (M)
- **Goals** (UX side of S-4.4):
  - The learner's `/mine-maal` shows "Mine mål" and "Mål fra din leder". The second section shows **shared** HR goals only.
  - The HR goal view per employee has two sections, **"Delt med medarbejderen"** and **"Kun synligt for HR"**, with a toggle on each goal. The toggle moves the goal between sections, with an optional note to the employee when a goal is shared.
  - The learner is notified when a goal is shared with them.
  - The profile's Karrieremål links to `/mine-maal`.
- **Budgets:** merge `learning_budget_per_employee` (saved, never used) into `department_budgets`. Department rename cascades to budgets, policies, skill targets and compliance.
- **Team:** `/hr/team`, `/hr/my-department` and `multitenant_reports.department_analytics` become one "Mit team" page with a direct-reports / whole-department toggle.

## N-4: HR workspace completion · ~2–3 weeks

### N-4.1 Consolidate analytics & reports (M–L)
Eight overlapping surfaces become three canonical ones, with redirects. Every block gets a home:
- **Oversigt** (`/hr`): KPIs, onboarding, the actionable insights feed. The "Uddannelses-ROI" tile, which actually shows spend, is relabelled.
- **Læringsanalyse** absorbs:
  - Virksomheds-BI's department/chatbot blocks
  - Virksomhedsrapporter's chatbot analytics
  - an "Avanceret" tab for the orphaned `enterprise_analytics` ML dashboard (behind a flag until its tables are written)
- **Rapporter & eksport** becomes the export hub:
  - CSV with a UTF-8 BOM (æøå break in Excel today)
  - filters
  - HTML empty states (not raw JSON 404)
  - budget/approvals/compliance/ROI exports
  - the unused `ui.export_buttons` macro used on analytics pages
- Personal "Analyse" / "Rapporter" (the credit pages) move under Konto → "Mit forbrug".
- Benchmarking: keep `/hr/benchmarking` and redirect the `companies` copy.
- Order detail: keep the `/hr` version and redirect the `multitenant_reports` copy.
- Showcase-only mocks (`admin_chatbot`, `mt_dashboard`, `report_detail`, `fm/profile`, `fm/sso_login`, `fm/widget_chat`): wire each to its real data or mark it clearly as gallery-only. `mt_dashboard` links into the gallery; fix that.

### N-4.2 Scheduled reports (M)
The AI tool writes `company_report_schedules`, but nothing reads it (`hr_tools.py:3774`). Needed: a worker job that emails the reports, plus a list/pause/cancel UI.

### N-4.3 Compliance & certifications (M)
- Edit and delete requirements.
- "Tildel påkrævet kursus", via N-1.1.
- CSV export.
- The matrix includes `user_certifications`.
- Cert-expiry alerts also go to managers (`cert_expiry_service.py:68-151`).

### N-4.4 Learning paths create real work (M)
- A path's paid steps create `pending_approval` orders via N-1.1. Today they bypass budget and approval (`hr_dashboard:4912`).
- **Learners see HR assignments**: "Tildelt af HR" on `/min-laering` with due dates. Today only the AI knows about them, while reminder emails reference paths the learner can't see.
- Skill gaps → training plan → assign becomes one linked flow.
- Path saves are versioned (`user_profile_db.py:~1301`).

### N-4.5 Actionable insights (M)
Each insight gets an `action_url` and a CTA ("Tildel kursus", "Justér budget"). `insight_card` has none today (`_macros.html:46`).

### N-4.6 Settings hub (M)
One `/virksomhed/indstillinger` with tabs:
- Virksomhed
- Branding
- Chatbot & widget
- Webhooks
- SSO
- API-nøgler
- Integrationer
- Bestillingspolitik: team-order policy, with a company default and per-vendor overrides (see N-5.2)

It merges `companies.settings`, `enterprise_settings`, `companies.branding`, `hr.chatbot_settings`, `hr.widget` and `sso_config`. Also:
- Fix the "Support-email" field, which is silently ignored (`companies:880`).
- Make "Deaktiver konto" a real request flow.
- Wire up the orphaned `preview-theme` and `export-settings`.

### N-4.7 Approvals UX (S)
- Remaining department budget shown per request.
- Rejection note included in the learner email.
- Bulk approve.

### N-4.8 Small HR items (S each)
- ROI year picker.
- `/dashboard` sparklines, search and "Seneste aktivitet / Populære kurser" get real data (`fm/index.html:209-226`).
- The "Medarbejdere" tab reaches the employees page, which gets an add button.

## N-5: AI completion (north star: fluent helper with a toolbox, never a checklist) · ~3 weeks

### N-5.1 Profiler: say it, and do it (M)
- **The profiler claims "Noteret på din profil" but only proposes a "Gem" card per fact** (`tools.py:3396`, `agent.py:334,450,2662`). Additions should save immediately with an inline "Fortryd"; keep confirmation for removals and edits; put several changes on one card.
- **Remove the checklist residue:**
  - `SYSTEM_PLAYBOOK_PROFILE_SAVE` ("FORETRUKKEN METODE … form") is added on every turn (`agent.py:250-260,422`).
  - The banner's "X/8 felter / Mangler: … / Profilen er komplet 🎉" (`ai_profiler.html:67-148`).
  - The "Fortæl om min {missing}" chip (`agent.py:1444`).

  Replace them with need-driven framing: what the AI can now do, and what it noticed.
- One shared completeness component (`my_profile.html:24`, `ai_profiler.html:39`), used as context, not as a goal.
- The profiler knows when a CV was just applied.

### N-5.2 Chat UI completeness (S)
- `ui_card` choice/form cards render empty, because `chat.js` ignores `ui_type`/`choices` (`chat.js:541-551,1442`).
- **Team orders follow company policy** (decided). Today "Bestil til team" shows even on self-orders, and team orders book **one seat** (`chat.js:293`, `tools.py:660-700`).
  - HR sets a **team-order policy** in the approval-policies / settings hub (N-4.6):
    - a company default;
    - optional **per-vendor overrides** ("for denne udbyder: …").
  - Policy options:
    - **`linked_orders`**: the chat collects participants and creates N linked orders (one per person, sharing a `group_order_id`), each going through N-1.1 approval and budget.
    - **`hr_bulk_assign`**: the chat hands off to HR bulk-assign with the course and participants pre-filled. A learner's request becomes one HR task.
    - **`not_allowed`**: team ordering is hidden for that company or vendor, and the AI explains that each person requests the course themselves.
  - `create_course_order` gets an optional `participants` argument. The tool reads the effective policy (vendor override → company default) and the AI phrases the next step fluently, not as a rule recital.
  - The card label is role-aware: "Anmod om plads" for self-orders, "Bestil til team" only when policy and role allow it.
  - Stored in a new `company_team_order_policy` table (`company_id`, `vendor_id NULL`, `mode`). Tests cover default, vendor override and each mode.
- The token-budget path emits `type:'done'` instead of `[DONE]` (`agent.py:2437`).
- Mind-map:
  - edit (PUT exists at `api.py:588`)
  - honour the delete response
  - immediate index resync on delete
  - a clearly labelled demo fallback

### N-5.3 HR assistant parity (L)
- **Confirm cards are dropped** by the HR panel and page (`hr_agent.py:383`, `_ai_panel.html:127`, `chatbot.html:127`). Render them and post to `/app1/confirm_tool_action`.
- The panel shows raw `<suggestions>[…]` and no markdown. Parse server-side, and render chips plus markdown.
- **Durable HR memory:** today it is a per-process dict (`hr_agent.py:58`). Use `conversation_state` and the MySQL transcripts, and give the panel the shared sidebar.
- Move the HR prompt onto `ai_context_layers`, and drop the "Assistent: [budget]" few-shot prefixes (`hr_agent.py:251`).
- HR turns logged to `chatbot_interactions` with feedback.
- `/hr/chatbot` becomes the full-screen version of the panel plus session history.

### N-5.4 Vendor assistant parity (M)
- Durable memory (`vendor_portal.py:577`).
- A grounding check, suggestions and few-shot.
- Fence the vendor name (`:707-790`).

### N-5.5 Runtime correctness (M)
- **The Anthropic → OpenAI fallback replays the whole tool loop**, so mutating tools can run twice (`ai_runtime.py:2858-2897`). No fallback after a side-effect tool has executed; or carry its results forward.
- The `AI_TOOLER2` flag is dead: `ai_tooler2_enabled()` is only called from a test. Either gate the tools as documented or document them as GA.

### N-5.6 Embeddable widget, made real (M) (verify in browser first)
- The origin check compares our own host, so any allowlist returns 403 (`app1/__init__.py:2021,2301`). Pass and verify the parent origin. S-5.6 verifies the security side.
- No memory between turns, because of `SameSite=Lax` in a third-party iframe (`run.py:172`). Use an iframe-held session token.
- Event parity with `chat.js`: products, ui_card, suggestions, confirm.
- A real preview in the creator (`widget_creator.html:131`).
- Widget tests.

### N-5.7 Eval coverage (M)
- A `scope` field in `run_eval.py` so the HR/vendor cases hit `/hr/chatbot/ask` and `/vendor/ask`. The HR cases can never pass today.
- More profiler cases (2 of 58 today) and vendor cases (0 today).
- A **fluency scorer** that flags checklist phrasing, field enumeration and form-first behaviour.
- Nightly on both providers.

### N-5.8 Remaining AI plan items
- Anonymous → logged-in memory migration (M).
- Prompt A/B via `prompt_version` (M).

## N-6: Vendor loop & money · ~2–3 weeks

### N-6.1 Vendors receive and act on orders (L), right after N-1.1 and N-3.1
- Orders carry a `vendor_id` (from the handle, via the one catalog).
- On `approved`, the vendor gets an email and an in-app notice.
- A **vendor orders page**: confirm (`booked`), decline (with a reason, which notifies HR and the learner), mark attended/completed.
- Emails on submission approve/reject.
- "Resend invite" and vendor forgot-password.

### N-6.2 Submission flow (S)
Admin "Godkend" on a submission actually imports it, or is clearly labelled "Godkend & importér" and goes through the preview (`admin_dashboard.py:749-788`). The security side of submissions is S-2.7.

### N-6.3 Billing management, with payment off-platform (M) (decided)
No payment provider and no card or bank flows in the app. Money moves off-platform, and the platform **manages and tracks** it:
- On N-1.1's `billing_status` (`not_invoiced → invoiced → paid`, plus `credited` for corrections), with transition rules and an audit trail. Who changed what, and when, is visible on the order.
- Admin and HR billing screen (the existing `/hr/billing`, fixed by the N-1.1 migration):
  - **mark as invoiced** (off-platform invoice number, invoice date, due date, optional reference/attachment);
  - **confirm payment received** (date, reference);
  - **credit/correct**;
  - bulk actions per company/period;
  - filters for overdue, unpaid and paid.
- **Billing overview and export** (CSV, plus a printable summary per company/period) for reconciliation with the external accounting system. No invoice PDF generation. The invoice itself lives in the external system, and we store its number and reference.
- Order detail shows the billing state read-only to the learner ("Faktureres eksternt") and in full to HR/admin.
- Overdue flag: `invoiced` past its due date shows up in the HR/admin overview and as a notification.
- Solo users (no company): no in-app payment. The order confirmation says an invoice will follow, and the order enters the same billing management queue for the platform admin.
- Admin revenue KPIs exclude cancelled and rejected orders, and split invoiced from paid (`admin_dashboard.py:78,89`).
- Remove the `hr_dashboard:670` status options `invoiced`/`paid` from the *order status* dropdown; they live in `billing_status` now.

### N-6.4 Credits become AI-usage metering (M) (decided)
Credits are shown in three places today but **never deducted**, and the admin grant skips the ledger (`admin_dashboard.py:369`).
- Every AI turn deducts credits via the ledger (`credit_usage`), computed from `ai_cost_model` (tokens × model price → credits). Applies to the employee advisor/profiler, HR assistant, vendor assistant and widget.
- Credits are held **per company**, with an optional per-user view. Solo users have a personal balance.
- **Every grant goes through the ledger**, including the admin JSON grant at `admin_dashboard.py:369`, with the reason and actor recorded.
- Low-balance behaviour: warn HR at a configurable threshold. At zero, apply the company setting: soft limit (keep working, flag it) or hard limit (AI paused with a friendly Danish message). Soft is the default.
- Screens:
  - the header chip shows the company or personal balance;
  - "Mit forbrug" (the old `/analytics` and `/reports/` pages, N-4.1) shows personal usage;
  - HR sees company usage per user and per assistant;
  - admin sees all companies, top-ups and burn rate.
- Top-ups are admin-granted. Purchasing credits is off-platform, like the rest of billing (N-6.3).
- Tests: deduction per turn, ledger integrity (balance equals the sum of the ledger), soft and hard limits.

### N-6.5 Admin polish (S–M)
- Pagination and search for companies, vendors, agreements and the audit log.
- User deactivate and "send reset link".
- Platform-level AI quality view.
- Chatbot BI "Detaljer" works across tenants (`multitenant_reports.py:553`).
- Broadcasts can target a company.

## N-7: Enterprise features, made usable · ~1–2 weeks (security side in S-3)

- **N-7.1 SSO UX (M):**
  - the config form offers **OAuth2/OIDC only** (the Microsoft Entra and Google presets map onto it), with the correct fields;
  - SAML and LDAP are hidden until S-D.1. Existing SAML/LDAP configs show as "inaktiv" with a short explanation. Today `generate_saml_request` raises KeyError on `acs_url`;
  - the login page's "SSO"/"Microsoft" buttons route via email-domain discovery to `/sso/login/<slug>/<provider>` (they are `href="#"` today, `login.html:116`);
  - the `sso_login` template is migrated to fm.
- **N-7.2 API keys & SCIM UX (M):**
  - an API-key screen in the settings hub (today a key can only be created *with* an existing admin key);
  - SCIM `/Groups` mapped to departments.
- **N-7.3 Webhooks (M):**
  - `employee.added`/`updated` fire from the HR screens and CSV import;
  - `budget.overrun` is subscribable;
  - per-subscriber delivery state (one failure resends to everyone today, `event_bus.py:315`);
  - a delivery log with "gensend".

## N-8: Operations, quality & polish · ongoing, ~1 week to finish

- **N-8.1 Background worker (M):** all 8 scheduler jobs run in `after_request`, on the visitor's time (`run.py:397`).
  - Run `drain_worker.py --loop` as its own ServerHoster service, and disable the in-request runner in prod.
  - `/readyz` shows each job's last run and the outbox backlog.
  - Land this by the end of N-1, because emails and notifications depend on it.
- **N-8.2 Observability (M):** structured logs with a request id, error tracking, and alerts on failed email or outbox delivery. `perf_cache` goes to Redis if it is available.
- **N-8.3 CI (S):** ruff, coverage, the fresh-DB bootstrap test, the nightly eval, and actions off Node 20.
- **N-8.4 Language & copy (S–M):**
  - 85 English `flash()` messages and the English JSON errors shown in toasts, translated to Danish (hr_dashboard 29, companies 20, enterprise_sso 10);
  - about 35 ASCII-fied strings ("paakraevet", "Prov") restored to æøå;
  - support copy matched to the real labels.
- **N-8.5 Empty/loading/error states (S–M):**
  - the recommendations empty state is a permanent skeleton (`employee_home.html:118`);
  - the home page gets goals and deadlines cards;
  - every list page gets an empty state with a next-step CTA.
- **N-8.6 Repo hygiene (S).** Delete, after checking imports:
  - `update_*.py`
  - the throwaway second `Flask()` app in `app1/__init__.py:22,2457`
  - `app1/node_modules` and `app1/package.json`
  - tracked `.pyc` files
  - the dead `shell.js:4-135` sidebar builder
  - the unused `futurematch.js`/`.css` and `chat.css`, after migrating `mark_read`

  Also refresh `README.md` (currently UTF-16 filler), `.env.example` (~40 missing vars, PythonAnywhere header) and the VPS runbooks. Archive the March plan docs to `docs/archive/`, and keep `docs/ai-framework.md` current.

---

## Sequencing

```
PART A                                   PART B
S-1 critical (wk 1) ───────────────────► N-0 CI/email/404 (wk 1, parallel)
   │                                        │
S-2 auth & roles (wk 2–4) ──┐            N-3.4 schema baseline (wk 2) ─► N-1 order lifecycle (wk 3–5) + N-8.1 worker + N-3.2 notifications
   │                        └──────────► N-2 onboarding & nav (wk 6–7, reads S-2.3 matrix, uses S-2.4 tokens)
S-4 privacy (wk 5–6)                     N-3.1 catalog ─► N-6.1–6.2 vendor loop (wk 8–9)
S-3 identity/integrations (wk 8–9) ────► N-7 enterprise UX (wk 14)
S-5 hardening (continuous)               N-4 HR (wk 10–12) ∥ N-5 AI (wk 10–13)
                                         N-6.3–6.5 money/admin (wk 14–15) ─► N-8 polish (wk 16)
```

Roughly **4 months to "done"** for one engineer plus Claude. Part A totals about 5–6 weeks (SAML/LDAP deferred) of work and interleaves with Part B instead of blocking it. The one exception is S-1, which is non-negotiable in week 1.

## Definition of done (per item)
1. Wired end to end: UI → route → service → DB → side effects (email, notification, webhook, event).
2. Reachable from the nav for the right roles, and blocked for the wrong ones, with a test for the permission boundary.
3. Danish copy, with real empty, loading and error states and a next-step CTA.
4. A test covering the happy path.
5. Docs updated (`ai-framework.md` for AI, this file's checkbox).

## Decisions log (2026-09-30)
1. **Billing** (N-6.3): **off-platform.** The platform only manages it: statuses, confirming invoiced/paid, references, overview and export. No payment provider.
2. **Credits** (N-6.4): **yes, they become AI-usage metering**, deducted per AI turn through the ledger.
3. **HR-written goals** (S-4.4, N-3.5): **yes, per-goal toggle.** The HR view is split into "Delt med medarbejderen" and "Kun synligt for HR". Only shared goals reach the learner and the AI.
4. **SAML/LDAP**: **deferred** (S-D.1). Disabled server-side and hidden in the UI. OAuth2/OIDC is done in S-3.1.
5. **Order lifecycle** (N-1.1): **yes, `booked` is its own step** for all vendors. HR or admin can book on behalf of vendors without portal access.
6. **Team orders from chat** (N-5.2): **company policy set by HR**, with optional per-vendor overrides. The modes are `linked_orders`, `hr_bulk_assign` and `not_allowed`.
