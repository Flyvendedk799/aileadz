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


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def conversion_funnel(company_id=None, days=30):
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

    Returns:
        dict with keys ``sessions, searches, shown, ordered`` (ints),
        ``rate_search, rate_shown, rate_ordered, overall_rate`` (floats, %),
        plus ``days`` and ``company_id`` echoed back. Always safe; never raises.
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
