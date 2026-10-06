# Runbook: Static assets, DB indexes, caching

Everything here applies automatically on deploy; no manual step. The app serves
`static/` itself (gunicorn worker behind the Cloudflare tunnel; there is no separate
static-file mapping), so the code-side caching below is what matters.

## 1. Static assets and cache-busting

- `run.py` sets `SEND_FILE_MAX_AGE_DEFAULT` from `STATIC_MAX_AGE_SECONDS` (default 1 year);
  `security_headers.py` adds `Cache-Control: public, max-age=31536000, immutable` to any
  `/static/*` response that lacks one.
- Because assets are cached for a year, **a changed file must get a changed URL**:
  - **Preferred:** `?v={{ asset_version('futurematch/assets/chat.js') }}`. `asset_version.py`
    (Jinja global registered in `run.py`) returns a 10-char SHA-1 of the file content, so an
    edit busts the cache by itself and an unchanged file keeps its cache across deploys. The
    chat assets (`chat.js`, `chat.css`, `ai-sidebar.js`, `ai-stream.js`, `profile-strength.js`) use it; a
    hand-bumped `?v=` for `chat.js` was once forgotten and shipped invisibly.
  - **Legacy `?v=N`** (still used by other templates): when you edit such a file, bump `N` in
    every template that references it, or switch that include to `asset_version`.

## 2. Gzip of dynamic responses

`response_compression.py` (an `after_request` hook registered in `run.py`) gzips text responses
(`text/*`, JSON, JS, XML, SVG) when the client sends `Accept-Encoding: gzip`. It skips the SSE chat
stream (`text/event-stream`), streamed / `send_file` responses, already-encoded responses and bodies
under 600 bytes, and returns the original response on any error. Verify:

```bash
curl -s -H 'Accept-Encoding: gzip' -o /dev/null -D - https://<host>/<big-page> | grep -i content-encoding
```

## 3. Database indexes (auto-applied)

`performance_indexes.ensure_performance_indexes(app)` adds the hot-path indexes (`created_at`,
`company_id`, `status`, `username`, `query_text`, ...; list in `PERFORMANCE_INDEXES`). It runs once
per worker process from a `before_request` hook in `run.py` (not gated by the enterprise-sync
stamp), ignores MySQL error 1061 (index exists) and skips tables/columns that do not exist, so it is
safe to repeat. The first request after a deploy that adds indexes can take a few seconds.

The same set is Alembic revision `migrations/versions/0002_performance_indexes.py`; that file is
standalone (no app imports), so **edit both lists together**.

Verify in a MySQL console: `SHOW INDEX FROM chatbot_interactions;` (expect `idx_ci_created`,
`idx_ci_query`, ...) and `SHOW INDEX FROM company_users;` (`idx_cu_company_status`, ...). To check a
query uses them, prefix it with `EXPLAIN` and look for a non-NULL `key` / `type` other than `ALL`.

## 4. Application caching (`perf_cache.py`)

A small thread-safe TTL cache: **per worker process** by default, shared across workers when
`REDIS_URL` is set and reachable (every Redis call is guarded and falls back to in-process). Use
`@ttl_cache(seconds, key=...)` only for data that tolerates a few seconds of staleness and is never
per-user correctness-critical. Current users:

- `admin_dashboard._admin_home_data` (60 s) and `hr_dashboard._hr_dashboard_metrics` (120 s, keyed by
  company id; the per-user `company` object is deliberately not cached).
- `app1/agent.py`, `app1/user_knowledge.py`, `learner_context.py` (`cache_get/cache_set/cache_clear`).
- `catalog_service._CACHE`: categories, vendors, filter options and related products are keyed by the
  catalog file signature, so they recompute once per catalog change; `catalog_service.clear_catalog_cache()`
  forces it after an import or admin edit.

A stale value can live up to its TTL per worker. That is intended.

## 5. Tunable env vars (all optional)

| Var | Default | Effect |
|---|---|---|
| `STATIC_MAX_AGE_SECONDS` | 31536000 | `SEND_FILE_MAX_AGE_DEFAULT` for worker-served static |
| `REPORTS_WINDOW_DAYS` | 90 | date window bounding the heavy chatbot-report scans (`admin_reports.py`) |
| `USAGE_WINDOW_DAYS` | 365 | date window bounding per-user `credit_usage` scans (`reports.py`, `pages.py`) |
| `CATALOG_SIGNATURE_TTL_SECONDS` | 5 | how long the catalog file-signature stat is reused |
| `REDIS_URL` | unset | shared cache backend for `perf_cache` |
| `ENTERPRISE_TABLE_SYNC_TTL_SECONDS` | 21600 | reuse window of the "tables ensured" boot stamp |

## 5. Icons and per-request memo (page weight and duplicate queries)

**Font Awesome is self-hosted** under `static/futurematch/vendor/fontawesome/` (Font Awesome Free 6.5.1:
icons CC BY 4.0, fonts SIL OFL 1.1, code MIT; the licence header stays in each CSS file). No page loads the
icon set from cdnjs any more.

- `css/fa-core.min.css` is the base + solid + regular sets in one file (the ttf fallbacks are dropped, woff2
  only). It is linked from `fm_base.html` and every standalone page (login, register, vendor pages, SSO login,
  widget) with `?v={{ asset_version(...) }}`, so it is cached for a year and busted by content hash.
- `css/brands.min.css` (+ `webfonts/fa-brands-400.woff2`) is linked only where a brand icon is drawn:
  `login.html`, `my_profile.html`, `admin_catalog.html` and `settings_sso.html` (the SSO presets in
  `settings_hub.py`). Add the same link to any new page that uses `fa-brands`.
- To upgrade: replace the files from the same cdnjs path with the new version, rebuild `fa-core.min.css` as
  fontawesome + solid + regular, and re-check `rg "fa-brands" templates static *.py`.
- `chat.js` is loaded only by `templates/fm/chat.html` (guarded by `tests/test_request_memo.py`); `fm_base.html`
  never includes it.

**One company row per request.** `request_memo.py` memoises on `flask.g` (GET/HEAD only; writes always reload):
`company_row(company_id)` is the single `SELECT * FROM companies`, used by branding (`_fetch_branding_row`,
itself memoised because `get_branding`, `has_custom_branding_feature` and `is_whitelabel_active` all ask for
it), the feature-flag lookup in `auth_decorators`, and `hr_dashboard.get_company_context`, which also hands
over the row it already joined (`prime_company_row`). Branding no longer joins `companies` with
`company_settings`; it reads the settings and the two primary brand assets as separate small queries.

| Measure | Before | After |
|---|---|---|
| `FROM companies` statements on `GET /hr/approvals` (`tests/test_request_memo.py`, branding not patched) | 3 | 1 |
| Icon CSS on every page | `all.min.css` from cdnjs, 102,641 B | `fa-core.min.css` first-party, 81,841 B, immutable-cached |
| Brand icon CSS and font | in `all.min.css`, glyph font 117 KB on demand | `brands.min.css` (19,307 B) only on 4 pages |

Not measured here (no browser in the build environment): transferred bytes of the home page and total SQL
statements per page in production. Record them from the browser network panel and the
`ai_agent_runs`/request log after the first deploy and add them to this table.
