# Handlinger kun du kan udføre (Part B)

Al kode for Part B er færdig og testet mod en in-memory SQLite-harness. Det nedenfor kræver nøgler, miljøvariabler på VPS'en, en ny service i ServerHoster eller rigtige konti, og kan derfor ikke gøres fra koden. Ingen af punkterne blokerer hinanden i koden: uden dem degraderer appen pænt (e-mail vises som "ikke sat op", jobs kører opportunistisk, sync springes over).

Anbefalet rækkefølge: 0 (backup) -> 1 (AI og katalog) -> A (e-mail) -> B (worker) -> C (database) -> D -> E (browsertjek) -> 2 (AI-tjek).

## 0. Før første deploy af grenen `part-b-functionality`

1. Tag en database-backup/snapshot.
2. **Sikkerhedskopiér Shopify-kataloget.** Den 17 MB store `app1/shopify_products_all_pages.json` er ikke længere i git. Ligger filen kun som en utracket fil i checkout'et på VPS'en, kan deploy fjerne den. Kopiér den først til et sted uden for checkout'et, fx `cp app1/shopify_products_all_pages.json /data/shopify_products_all_pages.json`, og sæt `CATALOG_SOURCE_FILE=/data/shopify_products_all_pages.json` på web- og worker-servicen. Når Shopify-synkroniseringen (punkt 1.2) kører, genskaber den filen selv.
3. Hvis Part A (`part-a-security`) også skal ind: flet begge grene; ved Alembic-konflikt (`alembic heads` viser to hoveder) kør `alembic merge heads -m "merge part a and b"`. Overlap mellem grenene (rolle-matrix, nulstillingstokens, mål-deling) er lavet som adaptere, der automatisk overlader til Part A's moduler når de findes.

## 1. AI og katalog (N-3.1, N-5.x, N-6.4)

1. **API-nøgler** (hvis ikke allerede sat på web- og worker-service): `OPENAI_API_KEY` (kræves til embeddings uanset udbyder) og `ANTHROPIC_API_KEY` hvis Claude skal bruges. Vælg udbyder i Admin -> AI-indstillinger.
2. **Shopify-synk** (valgfri, anbefalet): i Shopify-admin opret en custom app med læseadgang til produkter og kopiér Admin API access token (`shpat_...`). Sæt på web og worker: `SHOPIFY_STORE=<butik>.myshopify.com`, `SHOPIFY_ADMIN_TOKEN=shpat_...`. Jobbet `shopify_sync` kører i workeren. Kontrollér under Admin -> Katalog at "Genopbyg indeks" viser status og seneste synk.
3. **Kredit-omregning** (valgfri): `AI_CREDITS_PER_DKK` (standard 10). Sæt den hvis en kredit skal svare til en anden DKK-værdi. Grænsen (blød/hård) vælges pr. virksomhed i indstillingerne.
4. **Prompt A/B** (valgfri): `AI_PROMPT_VARIANTS=v2.0,v2.1` (første er kontrol) og ekstra instruktion til varianten i `AI_PROMPT_ADDENDUM_V2_1="..."`. Uden variablerne kører alt som før. Resultatet ses i AI-observability (`ab_versions`).
5. **Nightly eval** (GitHub -> Settings -> Secrets and variables -> Actions): tilføj `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`. Valgfrit `EVAL_VENDOR_EMAIL` og `EVAL_VENDOR_PASSWORD` for en testleverandør, så leverandør-assistentens sager også køres (ellers markeres de som sprunget over). Kør workflowet "AI eval (nightly)" manuelt én gang (Actions -> Run workflow) og gennemse scorecards, især målet "fluency".
6. **Widget-tjek i browser**: på en rigtig kundeside indsæt embed-koden fra Virksomhed -> Indstillinger -> Widget. Kontrollér (a) widget'en virker på et tilladt domæne, (b) samme kode på et ikke-tilladt domæne viser "ikke tilladt", (c) samtalen huskes ved sideskift. Bemærk at "Tilladte domæner" nu styrer både visning og `frame-ancestors`; tomt felt betyder åben widget (kun rate-begrænset).


# Core platform (A-F)

### A. E-mail that actually leaves the server (N-0.2)

Code is done (Flask-Mail in requirements, `MAIL_*` copied into the app config, honest health probe, test button). What only you can do:

1. On the VPS, in ServerHoster, open the **web service** environment and set: `MAIL_SERVER`, `MAIL_PORT` (587 with `MAIL_USE_TLS=1`, or 465 with `MAIL_USE_SSL=1`), `MAIL_USERNAME`, `MAIL_PASSWORD`, `MAIL_DEFAULT_SENDER` (an address your SMTP account may send as).
2. Set the same variables on the **worker service** (see B) - scheduled mails are sent from there.
3. Set `APP_BASE_URL` (for example `https://app.futurematch.dk`) on both services. Every e-mail link (order status, invite, reset) is built from it.
4. Redeploy so `Flask-Mail` is installed.
5. Log in as platform admin, open **Admin -> Systemstatus**, find the **E-mail** card: it must say "Sat op". Type your own address and press **Send test-mail**. Confirm the mail arrives (check spam) and that the sender and links look right. If it fails, the card shows the SMTP error text.
6. Optional but recommended: SPF/DKIM for the sending domain at your DNS/SMTP provider, otherwise the mails land in spam.

### B. Dedicated background worker (N-8.1)

1. In ServerHoster create a **second service** from the same repository and branch as the web app. Start command: `python drain_worker.py --loop`. Copy every environment variable from the web service.
2. Within about a minute **Admin -> Systemstatus -> Baggrundsjobs** must show "Worker kører" and recent "sidst kørt" times. `/readyz` also shows the `worker` block.
3. Only then set `SCHEDULER_OPPORTUNISTIC=0` on the **web service** and redeploy it (this stops background jobs from running inside visitors' requests). If you skip step 1, leave it at `1`, otherwise emails and webhooks stop.

### C. Database: deploy steps and baseline (N-3.4, N-1.1, N-3.2)

1. **Take a database backup/snapshot before the first deploy of this branch.** On first boot the app adds the new order/notification columns and runs two one-time data fixes (flagged in `schema_meta`): legacy order statuses are mapped (`pending` -> `approved`, `confirmed`/`processing` -> `booked`, `invoiced`/`paid` move into the separate billing status) and old `company_notifications` are copied into the unified `notifications` table.
2. After the deploy, open **Admin -> Systemstatus**: it should list no missing tables/columns. If anything is missing, check the web service log for `Table creation warning`.
3. Baseline for Alembic (optional, recommended): on the VPS run `python scripts/dump_schema_baseline.py > docs/schema_baseline.sql` with the production `MYSQL_*` variables (read-only). Review the "drift report" at the bottom. Then `alembic stamp 0002_performance_indexes` and `alembic upgrade head`. The Part A branch may add its own Alembic revision; if `alembic heads` shows two heads, run `alembic merge heads -m "merge part a and b"`.

### D. Nice-to-have environment variables

- `SENTRY_DSN` (error tracking; create a Sentry project and paste the DSN), `LOG_FORMAT=json` (structured logs), `REDIS_URL` (shared cache between workers).
- `SUPPORT_EMAIL`, `SUPPORT_PHONE`, `SUPPORT_HOURS`, `SUPPORT_SLA`: the support page only shows a phone line/response time when you set them.

### E. Browser checks that need real accounts

Please click through once on the VPS with an employee, an HR manager and a platform admin:

1. Employee: log in -> lands on "Min læring" with the welcome card; request a course; open the order from "Mine bestillinger"; check the sidebar has no "Virksomhed" block.
2. HR manager: approve the request (queue or order page) -> the learner's status reads "Godkendt - afventer booking" and they get an e-mail; book it from the order page; the learner sees "Booket" and can download the calendar file.
3. Learner: "Markér som gennemført" -> skill chips appear; add one; HR/manager gets a "Bekræft kompetenceløft" notification.
4. White-label tenant: the company name and logo in the sidebar must stay (the old localStorage overwrite is gone).
5. "Glemt adgangskode?" on the login page sends an e-mail and the link works once.
6. Account links (reset/invite) use Part A's token storage when that branch is merged, otherwise the built-in `account_tokens` fallback.

### F. CI

Nothing to do; the workflow now has a lint job, coverage upload and a 20-minute limit. The `gitleaks/gitleaks-action@v2` step still runs on Node 20 (no newer release of that action exists yet); GitHub will show a deprecation notice until it is updated upstream.

# HR workspace

1. **Email for scheduled reports and overdue-invoice mails** (depends on N-0.2): make sure `MAIL_SERVER`, `MAIL_PORT`, `MAIL_USE_TLS`, `MAIL_USERNAME`, `MAIL_PASSWORD`, `MAIL_DEFAULT_SENDER` are set on the VPS, then use *Admin -> Systemstatus -> Send test-mail*.
2. **Worker service**: the `scheduled_reports` (hourly) and `billing_overdue` (daily) jobs run in the `drain_worker.py --loop` process. Until that service exists (N-8.1), they only run opportunistically inside web requests.
3. **Schema**: the new tables/columns (`learning_path_steps`, `learning_path_versions`, `user_learning_path_versions`, `company_report_schedules.last_sent_at`, `employee_goals.shared_*`, `learning_paths.version`, `course_orders` billing columns) are created by the boot-time sync on the next deploy. No manual SQL. Optionally run `python scripts/dump_schema_baseline.py` afterwards to confirm no drift.
4. **Verify in the browser with a real HR account** (cannot be done without real accounts): `/hr/billing` (mark invoiced -> paid, CSV opens in Excel with correct æøå), `/hr/reports` (schedule a weekly report, confirm the mail with CSV arrives), `/hr/compliance` (Tildel kursus creates approvals), `/hr/employee/<id>/goals` (share a goal, log in as the employee and see it under "Mål fra din leder").

# Platform (vendor, SSO, API, webhooks)

1. **Database**: run the normal schema sync on deploy (`schema_registry` / `enterprise_tables` create `account_tokens`, `webhook_deliveries`, `account_requests` and add `users.status`). No manual SQL.
2. **Email**: make sure the branded email sender is configured in production (same SMTP settings already used for order mails). The new `vendor_submission_result` template is sent through it.
3. **SSO (optional, per customer)**: for each customer using SSO, register this redirect URI with their identity provider (shown on the SSO tab): `https://<your-domain>/sso/callback/<company-slug>/oauth2`, and set the company's email domain under Virksomhed so `/sso/discover` can find it.
4. **Existing API keys**: keys created before Part A remain valid; new keys are shown once and cannot be recovered. Customers who lost a key must create a new one.
5. **Webhook consumers**: announce the new event names `employee.added`, `employee.updated`, `budget.overrun` to customers who want them; existing subscriptions are unchanged.

Otherwise: none.
