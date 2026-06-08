"""
report_query.py — the single tenant-safe reporting/aggregation layer.

One place that knows how to turn the BI fact table (``chatbot_interactions``)
and the ``course_orders`` table into management-grade numbers. Every function
runs REAL SQL aggregation (``GROUP BY`` / ``COUNT`` / ``SUM``) in the database —
it never pulls 1000 rows and counts them in Python, and it never returns a
hardcoded placeholder.

Tenant isolation
----------------
There are no FKs or row-level security in this database — isolation is
enforced *here*, in the application layer. Every function takes an explicit
``company_id``:

* ``company_id`` is an int  -> all queries get ``WHERE company_id = %s`` (and the
  joined ``course_orders`` rows are scoped to the same company), so a tenant only
  ever sees its own data.
* ``company_id is None``     -> platform-wide aggregation (admin / super-admin use
  only). No tenant filter is applied.

Safety contract
---------------
These functions are called from request handlers and dashboards, so they must
never raise and never crash boot. Every DB access is guarded; on a missing
table, a stale connection, or any error we self-heal once and otherwise return
safe zero-filled structures. Callers can render the result unconditionally.
"""

import logging

logger = logging.getLogger(__name__)

# k-anonymity helpers (roadmap value-4, Theme A/E). Guarded: if the module is
# missing the reports still return — just without small-cohort suppression. A
# missing k-anon import must NEVER crash a report or boot.
try:
    import kanon as _kanon
except Exception:  # pragma: no cover - boot-safety guard
    _kanon = None

# Tools (logged in chatbot_interactions.tools_used) that represent a "search"
# step in the funnel — i.e. the user asked something that triggered a lookup.
_SEARCH_TOOLS = (
    'search_courses',
    'filter_courses',
    'recommend_for_profile',
    'suggest_learning_path',
    'search',
)

# Order statuses that count as a realised conversion (an actual order, not a
# half-finished/cancelled one). Kept broad on purpose: any row in course_orders
# attributed to a chatbot session is already a strong intent signal.
_ORDERED_STATUSES = ('pending', 'approved', 'completed', 'paid', 'confirmed', 'processing')


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _cursor():
    """Return a DictCursor on the live Flask-MySQL connection, self-healing a
    stale connection once. Returns ``None`` if a cursor cannot be obtained —
    callers treat that as "no data" and fall back to zeros."""
    try:
        from flask import current_app
        mysql = getattr(current_app, 'mysql', None)
        if mysql is None:
            return None
        try:
            return mysql.connection.cursor(_dict_cursor())
        except Exception:
            # Stale/dropped connection — try to heal once, then retry.
            try:
                import db_compat
                db_compat.refresh_flask_mysql_connection(mysql)
            except Exception:
                return None
            return mysql.connection.cursor(_dict_cursor())
    except Exception:
        return None


def _dict_cursor():
    """Resolve the DictCursor class, or ``None`` to fall back to the default
    cursor (which is already a DictCursor in this app)."""
    try:
        import MySQLdb.cursors
        return MySQLdb.cursors.DictCursor
    except Exception:
        return None


def _close(cur):
    if cur is not None:
        try:
            cur.close()
        except Exception:
            pass


def _scope(company_id, alias=None):
    """Build the tenant-isolation WHERE fragment + params for a single table.

    Returns ``(sql_fragment, params_tuple)`` where ``sql_fragment`` is either
    an empty string (platform-wide) or ``"col = %s"``. The fragment never
    includes the leading WHERE/AND so callers compose it however they like.
    """
    if company_id is None:
        return '', ()
    col = 'company_id' if not alias else '{}.company_id'.format(alias)
    return '{} = %s'.format(col), (int(company_id),)


def _int(value, default=0):
    try:
        if value is None:
            return default
        return int(value)
    except (TypeError, ValueError):
        return default


def _rate(numerator, denominator):
    """Percentage of numerator over denominator, rounded to 1 decimal. 0 when
    the denominator is empty (so an empty funnel is all-zeros, not a crash)."""
    try:
        if not denominator:
            return 0.0
        return round((numerator / denominator) * 100.0, 1)
    except (TypeError, ZeroDivisionError):
        return 0.0


def _days(days):
    """Clamp the look-back window to a sane positive integer (1..3650)."""
    d = _int(days, 30)
    if d < 1:
        d = 1
    if d > 3650:
        d = 3650
    return d


def _floor_k():
    """Resolve the active small-tenant suppression floor (k).

    Mirrors kanon's env-overridable K_DEFAULT. Guarded: if the k-anon module is
    missing or anything goes wrong we fall back to a conservative constant of 5
    so the floor can never be silently weakened to 0/1 by an import failure.
    """
    try:
        if _kanon is not None:
            k = int(getattr(_kanon, 'K_DEFAULT', 5))
            return k if k >= 1 else 5
    except Exception:
        pass
    return 5


def _small_tenant_note(k):
    """Danish UI note explaining why a scalar company figure is suppressed.

    Prefers kanon's canonical note (keeps wording consistent across the app),
    falls back to a self-contained Danish string if k-anon is unavailable."""
    try:
        if _kanon is not None:
            return _kanon.anon_note(k)
    except Exception:
        pass
    return 'Grupper under k={k} er skjult af hensyn til anonymitet'.format(k=k)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def conversion_funnel(company_id=None, days=30, suppress_floor=False):
    """Reconstruct the chatbot -> order conversion funnel with real SQL.

    The funnel stages are derived from the BI fact table
    (``chatbot_interactions``) and attributed to orders via
    ``course_orders.chatbot_session_id = chatbot_interactions.session_id``:

      sessions  -> distinct chatbot sessions in the window
      searches  -> sessions whose interactions triggered a search-type tool
                   (or returned tool results)
      shown     -> sessions where the chatbot actually showed product(s)
      ordered   -> sessions that produced an attributed course order

    Each stage is a COUNT of DISTINCT sessions computed in the database — never
    a Python loop over fetched rows. Conversion rates between consecutive
    stages, plus the overall session->order rate, are returned too.

    Args:
        company_id: int tenant id, or ``None`` for platform-wide (admin).
        days: look-back window in days (clamped to 1..3650).
        suppress_floor: when True, apply a SMALL-TENANT suppression floor. The
            funnel is a scalar company-level count whose only safety rests on
            aggregation — for a tenant with fewer than ``kanon.K_DEFAULT``
            distinct sessions a single learner's journey is re-identifiable. In
            that case every stage count/rate is zeroed and ``suppressed`` is set
            with a Danish ``anon_note``. Tenant-facing HR routes pass True;
            platform-wide admin (company_id=None) leaves it False (the
            platform-wide cohort is always far above k).

    Returns:
        dict with keys ``sessions, searches, shown, ordered`` (ints),
        ``rate_search, rate_shown, rate_ordered, overall_rate`` (floats, %),
        plus ``days``, ``company_id``, ``suppressed`` (bool) and ``anon_note``
        (str|None) echoed back. Always safe; never raises.
    """
    d = _days(days)
    result = {
        'sessions': 0,
        'searches': 0,
        'shown': 0,
        'ordered': 0,
        'rate_search': 0.0,    # sessions -> searches
        'rate_shown': 0.0,     # searches -> shown
        'rate_ordered': 0.0,   # shown -> ordered
        'overall_rate': 0.0,   # sessions -> ordered
        'days': d,
        'company_id': company_id,
        'suppressed': False,
        'anon_note': None,
    }

    cur = _cursor()
    if cur is None:
        return result

    try:
        scope_sql, scope_params = _scope(company_id)
        tenant_and = (' AND ' + scope_sql) if scope_sql else ''

        # Build the search-tool LIKE predicate (tools_used is a CSV string).
        like_clauses = ' OR '.join(['tools_used LIKE %s'] * len(_SEARCH_TOOLS))
        like_params = tuple('%{}%'.format(t) for t in _SEARCH_TOOLS)

        # ── sessions / searches / shown — one pass over the fact table ──
        # COUNT(DISTINCT session_id) per stage, filtered by window + tenant.
        # tool_results_count > 0 also qualifies as a "search" (results came back
        # even if the tool name wasn't captured).
        fact_sql = """
            SELECT
                COUNT(DISTINCT session_id) AS sessions,
                COUNT(DISTINCT CASE
                    WHEN ({like_clauses}) OR COALESCE(tool_results_count, 0) > 0
                    THEN session_id END) AS searches,
                COUNT(DISTINCT CASE
                    WHEN products_shown IS NOT NULL
                         AND products_shown <> ''
                         AND products_shown <> '[]'
                    THEN session_id END) AS shown
            FROM chatbot_interactions
            WHERE created_at >= DATE_SUB(NOW(), INTERVAL %s DAY)
              AND session_id IS NOT NULL AND session_id <> ''
              {tenant_and}
        """.format(like_clauses=like_clauses, tenant_and=tenant_and)

        params = like_params + (d,) + scope_params
        cur.execute(fact_sql, params)
        row = cur.fetchone() or {}
        result['sessions'] = _int(row.get('sessions'))
        result['searches'] = _int(row.get('searches'))
        result['shown'] = _int(row.get('shown'))

        # ── ordered — distinct chatbot sessions that produced an order ──
        # Attribution: course_orders.chatbot_session_id back to the session,
        # scoped to the same tenant and the same time window.
        order_scope_sql, order_scope_params = _scope(company_id)
        order_tenant_and = (' AND ' + order_scope_sql) if order_scope_sql else ''
        status_placeholders = ', '.join(['%s'] * len(_ORDERED_STATUSES))
        order_sql = """
            SELECT COUNT(DISTINCT chatbot_session_id) AS ordered
            FROM course_orders
            WHERE chatbot_session_id IS NOT NULL AND chatbot_session_id <> ''
              AND created_at >= DATE_SUB(NOW(), INTERVAL %s DAY)
              AND status IN ({statuses})
              {tenant_and}
        """.format(statuses=status_placeholders, tenant_and=order_tenant_and)
        cur.execute(order_sql, (d,) + _ORDERED_STATUSES + order_scope_params)
        orow = cur.fetchone() or {}
        result['ordered'] = _int(orow.get('ordered'))

        # ── small-tenant suppression floor (k-anon) ──
        # The funnel is a scalar count whose safety rests on aggregation alone.
        # For a tiny tenant a sub-k session cohort lets a single learner's
        # journey be re-identified. When requested (HR-scoped calls), zero out
        # every stage below the floor rather than expose it. Skipped entirely
        # when there are 0 sessions (an empty funnel is not a privacy leak —
        # it's just "no data"), so the empty-state copy still renders.
        if suppress_floor:
            k = _floor_k()
            if 0 < result['sessions'] < k:
                result['sessions'] = 0
                result['searches'] = 0
                result['shown'] = 0
                result['ordered'] = 0
                result['suppressed'] = True
                result['anon_note'] = _small_tenant_note(k)

        # ── derived rates ──
        result['rate_search'] = _rate(result['searches'], result['sessions'])
        result['rate_shown'] = _rate(result['shown'], result['searches'])
        result['rate_ordered'] = _rate(result['ordered'], result['shown'])
        result['overall_rate'] = _rate(result['ordered'], result['sessions'])
    except Exception as e:
        logger.warning("conversion_funnel failed (company_id=%s): %s", company_id, e)
    finally:
        _close(cur)

    return result


def daily_volume(company_id=None, days=30):
    """Daily interaction volume over the window, aggregated with GROUP BY DATE.

    Returns a dict with ordered parallel lists::

        {'labels': ['2026-05-01', ...], 'data': [42, ...]}

    suitable for direct injection into Chart.js. Tenant-scoped when a
    ``company_id`` is given. Always safe; never raises.
    """
    d = _days(days)
    out = {'labels': [], 'data': []}

    cur = _cursor()
    if cur is None:
        return out

    try:
        scope_sql, scope_params = _scope(company_id)
        tenant_and = (' AND ' + scope_sql) if scope_sql else ''
        sql = """
            SELECT DATE(created_at) AS day, COUNT(*) AS cnt
            FROM chatbot_interactions
            WHERE created_at >= DATE_SUB(CURDATE(), INTERVAL %s DAY)
              {tenant_and}
            GROUP BY DATE(created_at)
            ORDER BY day ASC
        """.format(tenant_and=tenant_and)
        cur.execute(sql, (d,) + scope_params)
        for r in cur.fetchall() or []:
            day = r.get('day')
            label = day.strftime('%Y-%m-%d') if hasattr(day, 'strftime') else str(day)
            out['labels'].append(label)
            out['data'].append(_int(r.get('cnt')))
    except Exception as e:
        logger.warning("daily_volume failed (company_id=%s): %s", company_id, e)
    finally:
        _close(cur)

    return out


def top_queries(company_id=None, days=30, limit=10):
    """Most frequent user queries over the window, via GROUP BY query_text.

    Returns a list of ``{'query': str, 'count': int}`` ordered by count desc.
    Tenant-scoped when a ``company_id`` is given. Always safe; never raises.

    k-anonymity (roadmap value-4): a verbatim ``query_text`` asked by fewer than
    k DISTINCT people is effectively one person's exact wording — readable PII on
    a "top queries" panel. We measure the distinct cohort behind each query with
    ``COUNT(DISTINCT COALESCE(username, session_id))`` in SQL and suppress any
    query whose cohort is below k. Overall volume metrics live in
    ``daily_volume`` / ``conversion_funnel`` and are unaffected. The list shape
    (``[{'query','count'}, ...]``) is unchanged — only sub-k rows are removed.
    Guarded: if k-anon is unavailable the unsuppressed list is returned.
    """
    d = _days(days)
    lim = _int(limit, 10)
    if lim < 1:
        lim = 1
    if lim > 200:
        lim = 200

    rows_out = []
    cur = _cursor()
    if cur is None:
        return rows_out

    try:
        scope_sql, scope_params = _scope(company_id)
        tenant_and = (' AND ' + scope_sql) if scope_sql else ''
        # distinct_people: the size of the cohort that asked this exact query.
        # COALESCE so a logged-out (NULL username) row still counts via session.
        sql = """
            SELECT query_text AS query,
                   COUNT(*) AS cnt,
                   COUNT(DISTINCT COALESCE(username, session_id)) AS distinct_people
            FROM chatbot_interactions
            WHERE created_at >= DATE_SUB(NOW(), INTERVAL %s DAY)
              AND query_text IS NOT NULL AND query_text <> ''
              {tenant_and}
            GROUP BY query_text
            ORDER BY cnt DESC
            LIMIT %s
        """.format(tenant_and=tenant_and)
        cur.execute(sql, (d,) + scope_params + (lim,))
        for r in cur.fetchall() or []:
            q = r.get('query') or ''
            rows_out.append({
                'query': str(q)[:200],
                'count': _int(r.get('cnt')),
                # internal cohort size for k-anon; stripped before returning.
                '_distinct_people': _int(r.get('distinct_people')),
            })
    except Exception as e:
        logger.warning("top_queries failed (company_id=%s): %s", company_id, e)
    finally:
        _close(cur)

    # k-anon suppression on the distinct-people cohort, then strip the internal
    # key so the returned shape stays exactly {'query','count'}.
    if _kanon is not None and rows_out:
        try:
            rows_out, _note = _kanon.suppress_small_groups(rows_out, '_distinct_people')
        except Exception:
            pass
    for row in rows_out:
        if isinstance(row, dict):
            row.pop('_distinct_people', None)

    return rows_out


def attributed_revenue(company_id=None, days=90, suppress_floor=False):
    """AI-ROI attribution: revenue (DKK) on orders the chatbot adviser drove.

    Joins the BI fact table to ``course_orders`` on the shared session id
    (``chatbot_interactions.session_id = course_orders.chatbot_session_id``) and
    sums the order price for orders that were recommended by an AI tool — i.e.
    ``course_orders.recommended_by_tool`` is set. This is the "DKK omsat
    tilskrevet AI-rådgiveren" headline figure.

    Everything is computed DB-side (SUM / COUNT) — no rows are pulled into
    Python. Tenant-scoped when a ``company_id`` is given; platform-wide when it
    is ``None``. Always safe; never raises.

    Args:
        company_id: int tenant id, or ``None`` for platform-wide (admin).
        days: look-back window in days.
        suppress_floor: when True, apply a SMALL-TENANT suppression floor. The
            attributed-revenue headline is a scalar SUM/COUNT — for a tenant
            with fewer than ``kanon.K_DEFAULT`` distinct attributed sessions a
            single order's value is re-identifiable. In that case revenue /
            orders / sessions / AOV are zeroed and ``suppressed`` is set with a
            Danish ``anon_note``. Tenant-facing HR routes pass True;
            platform-wide admin (company_id=None) leaves it False.

    Returns:
        dict::
            {'revenue': float,            # SUM(price) of AI-attributed orders
             'orders': int,               # COUNT of AI-attributed orders
             'sessions': int,             # distinct attributed chatbot sessions
             'avg_order_value': float,    # revenue / orders
             'days': int, 'company_id': company_id,
             'suppressed': bool, 'anon_note': str|None}
    """
    d = _days(days)
    result = {
        'revenue': 0.0,
        'orders': 0,
        'sessions': 0,
        'avg_order_value': 0.0,
        'days': d,
        'company_id': company_id,
        'suppressed': False,
        'anon_note': None,
    }

    cur = _cursor()
    if cur is None:
        return result

    try:
        # Scope the orders side to the tenant (alias 'co').
        scope_sql, scope_params = _scope(company_id, alias='co')
        tenant_and = (' AND ' + scope_sql) if scope_sql else ''
        status_placeholders = ', '.join(['%s'] * len(_ORDERED_STATUSES))
        sql = """
            SELECT
                COALESCE(SUM(co.price), 0) AS revenue,
                COUNT(*) AS orders,
                COUNT(DISTINCT co.chatbot_session_id) AS sessions
            FROM course_orders co
            JOIN chatbot_interactions ci
              ON ci.session_id = co.chatbot_session_id
            WHERE co.chatbot_session_id IS NOT NULL AND co.chatbot_session_id <> ''
              AND co.recommended_by_tool IS NOT NULL AND co.recommended_by_tool <> ''
              AND co.created_at >= DATE_SUB(NOW(), INTERVAL %s DAY)
              AND co.status IN ({statuses})
              {tenant_and}
        """.format(statuses=status_placeholders, tenant_and=tenant_and)
        params = (d,) + _ORDERED_STATUSES + scope_params
        cur.execute(sql, params)
        row = cur.fetchone() or {}
        try:
            result['revenue'] = round(float(row.get('revenue') or 0), 2)
        except (TypeError, ValueError):
            result['revenue'] = 0.0
        result['orders'] = _int(row.get('orders'))
        result['sessions'] = _int(row.get('sessions'))

        # ── small-tenant suppression floor (k-anon) ──
        # The DKK headline is a scalar SUM/COUNT; for a tiny tenant a single
        # attributed order's value is re-identifiable. When requested (HR-scoped
        # calls), suppress the whole figure below the floor. The cohort is the
        # number of distinct attributed sessions. Skipped when there are 0
        # sessions (no data is not a leak), so the empty state still renders.
        if suppress_floor:
            k = _floor_k()
            if 0 < result['sessions'] < k:
                result['revenue'] = 0.0
                result['orders'] = 0
                result['sessions'] = 0
                result['suppressed'] = True
                result['anon_note'] = _small_tenant_note(k)

        if result['orders']:
            result['avg_order_value'] = round(result['revenue'] / result['orders'], 2)
    except Exception as e:
        logger.warning("attributed_revenue failed (company_id=%s): %s", company_id, e)
    finally:
        _close(cur)

    return result


def cohort_retention(company_id=None, months=6):
    """Learner-cohort retention by first-order month (pure SQL).

    Groups every learner (``course_orders.user_id``) into the calendar month of
    their FIRST order, then measures — for each subsequent month offset 0..N —
    how many of that cohort completed at least one course in that month. This is
    the classic retention triangle: cohort size on the diagonal, falling
    retention to the right.

    Completion is ``completion_status = 'completed'`` (using completion_date when
    present, else the order's created_at). All aggregation is DB-side; we group
    by (cohort_month, month_offset) and COUNT(DISTINCT user_id) — no per-row
    Python work. Tenant-scoped when ``company_id`` is given.

    k-anonymity: a cohort whose first-month size is below k represents only a
    handful of (possibly one) learners, so its retention curve would profile an
    individual. Such cohorts are suppressed from the visible matrix. Guarded —
    without k-anon the full matrix is returned.

    Returns:
        dict::
            {'cohorts': [
                {'cohort': 'YYYY-MM', 'size': int,
                 'retention': [{'offset': 0, 'count': int, 'pct': float}, ...]},
                ...],
             'max_offset': int, 'months': int, 'company_id': company_id,
             'anon_note': str|None}
        Always safe; never raises.
    """
    m = _int(months, 6)
    if m < 1:
        m = 1
    if m > 24:
        m = 24

    out = {
        'cohorts': [],
        'max_offset': m,
        'months': m,
        'company_id': company_id,
        'anon_note': None,
    }

    cur = _cursor()
    if cur is None:
        return out

    try:
        scope_sql, scope_params = _scope(company_id, alias='o')
        tenant_and = (' AND ' + scope_sql) if scope_sql else ''

        # Per-cohort size = distinct learners whose FIRST order is in that month.
        # first_order CTE-style subquery via GROUP BY user_id MIN(created_at).
        # We compute it inline so it works on MySQL without explicit CTEs.
        # cohort_month = first-order month; activity rows tie completions back to
        # the learner's cohort and compute the month offset with PERIOD_DIFF.
        sql = """
            SELECT fo.cohort_month AS cohort_month,
                   PERIOD_DIFF(
                       EXTRACT(YEAR_MONTH FROM COALESCE(o.completion_date, o.created_at)),
                       fo.cohort_period
                   ) AS month_offset,
                   COUNT(DISTINCT o.user_id) AS learners
            FROM course_orders o
            JOIN (
                SELECT user_id,
                       DATE_FORMAT(MIN(created_at), '%%Y-%%m') AS cohort_month,
                       EXTRACT(YEAR_MONTH FROM MIN(created_at)) AS cohort_period
                FROM course_orders
                WHERE user_id IS NOT NULL
                  {first_tenant_and}
                GROUP BY user_id
            ) fo ON fo.user_id = o.user_id
            WHERE o.user_id IS NOT NULL
              AND o.completion_status = 'completed'
              {tenant_and}
            GROUP BY fo.cohort_month, fo.cohort_period, month_offset
            HAVING month_offset >= 0 AND month_offset <= %s
            ORDER BY fo.cohort_month ASC, month_offset ASC
        """.format(
            first_tenant_and=(' AND ' + _scope(company_id)[0]) if scope_sql else '',
            tenant_and=tenant_and,
        )
        # Params: inner first-order tenant scope, outer tenant scope, then offset.
        first_scope_params = _scope(company_id)[1]
        params = first_scope_params + scope_params + (m,)
        cur.execute(sql, params)

        # Build {cohort_month: {offset: learners}}
        grid = {}
        for r in cur.fetchall() or []:
            cm = r.get('cohort_month')
            if not cm:
                continue
            off = _int(r.get('month_offset'), -1)
            if off < 0:
                continue
            grid.setdefault(cm, {})[off] = _int(r.get('learners'))

        # Cohort size = retention at offset 0 (first-order month completions can
        # be fewer than the cohort; the true cohort size is distinct first-order
        # learners). Fetch first-order cohort sizes separately for an accurate base.
        size_sql = """
            SELECT cohort_month, COUNT(*) AS size
            FROM (
                SELECT user_id, DATE_FORMAT(MIN(created_at), '%%Y-%%m') AS cohort_month
                FROM course_orders o
                WHERE user_id IS NOT NULL
                  {tenant_and}
                GROUP BY user_id
            ) firsts
            GROUP BY cohort_month
            ORDER BY cohort_month ASC
        """.format(tenant_and=tenant_and)
        cur.execute(size_sql, scope_params)
        sizes = {}
        for r in cur.fetchall() or []:
            cm = r.get('cohort_month')
            if cm:
                sizes[cm] = _int(r.get('size'))

        # Assemble cohort rows (only cohorts that have a known size).
        cohort_rows = []
        for cm in sorted(sizes.keys()):
            size = sizes[cm]
            retention = []
            offsets = grid.get(cm, {})
            for off in range(0, m + 1):
                cnt = _int(offsets.get(off))
                retention.append({
                    'offset': off,
                    'count': cnt,
                    'pct': _rate(cnt, size),
                })
            cohort_rows.append({
                'cohort': cm,
                'size': size,
                '_cohort': size,  # internal cohort-size key for k-anon
                'retention': retention,
            })

        # k-anon: drop cohorts whose first-month size is sub-k.
        if _kanon is not None and cohort_rows:
            try:
                cohort_rows, note = _kanon.suppress_small_groups(cohort_rows, '_cohort')
                if note and note.get('suppressed'):
                    out['anon_note'] = note.get('note_da')
            except Exception:
                pass
        for row in cohort_rows:
            if isinstance(row, dict):
                row.pop('_cohort', None)

        out['cohorts'] = cohort_rows
    except Exception as e:
        logger.warning("cohort_retention failed (company_id=%s): %s", company_id, e)
    finally:
        _close(cur)

    return out
