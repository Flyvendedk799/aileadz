# Performance runbook — static assets, DB indexes, caching

This documents the platform-performance work and the **one manual step** it needs
on PythonAnywhere. Everything else applies automatically on a normal `git pull` +
**Web → Reload**.

## TL;DR deploy

1. `git pull` on PythonAnywhere.
2. **Web tab → Reload** the web app.
3. **One-time only:** add the `/static` mapping (below). After that, normal pulls
   need no extra steps.

On the first request after each reload, the app idempotently ensures the hot-path
DB indexes (see "Database indexes"). The first reload after this change may take a
few extra seconds while the indexes build; subsequent reloads are instant.

---

## 1. Static files → nginx (the one manual step) ⭐ biggest frontend win

By default Flask serves `/static/*` through the Python workers. On PythonAnywhere
you can have **nginx** serve them directly — faster, and it frees the workers for
real requests.

**Web tab → "Static files" → Add:**

| URL        | Directory                                   |
|------------|---------------------------------------------|
| `/static/` | `/home/TobiasMastek/dashboard/static/`      |

Then **Reload**. Verify with:

```bash
curl -sI https://TobiasMastek.pythonanywhere.com/static/futurematch/assets/fm.css | grep -i 'server\|cache-control'
```

You should see nginx serving it (not the worker) and a long `Cache-Control`.
PythonAnywhere applies a far-future expiry to mapped static files automatically.

**Cache-busting:** every asset URL is versioned with `?v=N` (e.g. `fm.css?v=13`).
Long-lived caching is safe **only because of this** — so whenever you edit a file
under `static/`, **bump its `?v=N`** in the template that references it, or repeat
visitors keep the old cached copy.

### Code-side caching (already applied, covers the pre-mapping / dev path)
- `run.py` sets `SEND_FILE_MAX_AGE_DEFAULT` (default 1 year, override with
  `STATIC_MAX_AGE_SECONDS`).
- `security_headers.py` adds `Cache-Control: public, max-age=31536000, immutable`
  to any `/static/*` response that still goes through the worker.

### Dynamic-response gzip (already applied)
`response_compression.py` registers an `after_request` hook that gzips text
responses (HTML/JSON/CSS/JS/SVG/XML) when the client sends `Accept-Encoding: gzip`.
PythonAnywhere's nginx gzips *mapped static* files but not worker-proxied dynamic
HTML, so this covers the big dashboard/report pages (~75% smaller on the wire). It
deliberately skips the SSE chat stream (`text/event-stream`), `send_file`/streamed
responses, already-encoded responses, and bodies under 600 bytes. Verify:

```bash
curl -s -H 'Accept-Encoding: gzip' -o /dev/null -D - https://TobiasMastek.pythonanywhere.com/dashboard | grep -i content-encoding
```

---

## 2. Database indexes (auto-applied)

`performance_indexes.ensure_performance_indexes(app)` adds indexes on the hot
columns the dashboards filter/group on (`created_at`, `company_id`, `status`,
`username`, `query_text`, …). It is wired into a `before_request` hook in `run.py`
that runs **once per worker process** (not gated by the enterprise-sync TTL), so a
`git pull` + reload applies it. It is idempotent (ignores MySQL error 1061) and
skips any table/column that doesn't exist, so it is safe to run repeatedly.

The same set is recorded as Alembic revision
`migrations/versions/0002_performance_indexes.py` for DBs managed with
`alembic upgrade head`. The two lists must stay in sync — edit both.

**To verify the indexes landed** (PythonAnywhere → Databases → MySQL console):

```sql
SHOW INDEX FROM chatbot_interactions;   -- expect idx_ci_created, idx_ci_query, ...
SHOW INDEX FROM company_users;          -- expect idx_cu_company_status, ...
```

**To confirm a query now uses them**, prefix it with `EXPLAIN` and check `key` is
not NULL / `type` is not `ALL` on the big tables.

---

## 3. Application caching (per worker, in-process)

PythonAnywhere has no Redis, so `perf_cache.py` is an in-process TTL cache: **each
worker keeps its own copy**, bounded by a short TTL. Applied to:

- Admin dashboard KPI block — `admin_dashboard._admin_home_data` (TTL 60s).
- HR dashboard per-company metrics — `hr_dashboard._hr_dashboard_metrics`, keyed by
  `company_id` (TTL 120s). The per-user `company` object is deliberately **not**
  cached, so users of the same company never share role/department state.
- Catalog `get_categories` / `get_vendors` / `get_filter_options` /
  `get_related_products` — keyed by the catalog file signature
  (`catalog_service._CACHE`), so they recompute once per catalog change instead of
  once per request. `search_products` also no longer builds the per-product search
  string on a no-query browse (the common case).

Implication: after a data change, a stale value can linger up to its TTL **per
worker**. That is intended. To force-clear the catalog cache after an import/override
edit, the existing `catalog_service.clear_catalog_cache()` path still works (it also
resets the new signature throttle).

---

## 4. Tunable env vars (all optional, sensible defaults)

| Var                            | Default | Effect |
|--------------------------------|---------|--------|
| `STATIC_MAX_AGE_SECONDS`       | 31536000 | `SEND_FILE_MAX_AGE_DEFAULT` for worker-served static |
| `REPORTS_WINDOW_DAYS`          | 90      | Date window bounding the heavy chatbot-report scans |
| `USAGE_WINDOW_DAYS`            | 365     | Date window bounding per-user credit_usage scans (reports.py / pages.py) |
| `CATALOG_SIGNATURE_TTL_SECONDS`| 5       | How long the catalog file-signature stat is reused |

---

## 5. Query rewrites (no action needed — listed for reference)

- Admin "companies" list: 3 correlated subqueries → pre-aggregated derived-table
  joins (one grouped scan each, no employee×order fan-out).
- Admin "users" list: now server-side paginated (50/page) with a SQL-side search,
  instead of fetching the whole `users` table.
- Chatbot BI "frequent questions": correlated subquery → `MIN(id)` join.
- Chatbot BI tool-usage / products-shown scans: bounded by `REPORTS_WINDOW_DAYS`.
- Platform admin "companies" list (`companies.admin_companies_list`): 4 correlated
  subqueries → pre-aggregated derived-table joins.
- Multitenant report company-stats: the `company_users × chatbot_interactions`
  cross-product join split into two independent aggregations (same values, no
  row blow-up).
- Per-user reports (`reports.py`, `pages.py`): `credit_usage` scans bounded by
  `USAGE_WINDOW_DAYS` + backed by new indexes.

---

## 6. Notes / intentionally NOT changed

- **Script `defer` / Chart.js lazy-load:** skipped. `shell.js`/`chat.js` already sit
  at end-of-body (not render-blocking content) and `shell.js` exports `window.fm*`
  globals inline pages may call; Chart.js is loaded on only one page (`roi.html`)
  and called inline. The risk outweighed the gain — caching + the nginx mapping are
  the asset wins instead.
- **Font trimming:** the requested weights/families are genuinely used across the
  range (`--ff-mono` alone appears ~25×; weights span 500–820), so trimming risked
  visible regressions. Self-hosting/subsetting the fonts is a possible future step.
