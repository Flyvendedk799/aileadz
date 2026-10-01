from flask import Blueprint, render_template, session, current_app, redirect, url_for, flash

dashboard_bp = Blueprint('dashboard', __name__, template_folder='templates')


def _fetch_dashboard_kpis():
    kpis = {
        'unread_notifications': 0,
        'pending_approvals': None,
        'catalog_tools': None,
        'role_label': 'Bruger',
    }
    role = session.get('role', 'user')
    kpis['role_label'] = 'Administrator' if role == 'admin' else 'Bruger'

    user_id = session.get('user')
    company_id = session.get('company_id')

    try:
        mysql = current_app.mysql
        cur = mysql.connection.cursor()

        if user_id:
            cur.execute(
                "SELECT COUNT(*) AS cnt FROM notifications WHERE user_id = %s AND `read` = 0",
                (user_id,),
            )
            row = cur.fetchone()
            kpis['unread_notifications'] = (row['cnt'] if row else 0) or 0

        if company_id:
            cur.execute(
                """
                SELECT COUNT(*) AS cnt FROM course_orders
                WHERE company_id = %s AND status = 'pending_approval'
                """,
                (company_id,),
            )
            row = cur.fetchone()
            kpis['pending_approvals'] = (row['cnt'] if row else 0) or 0

            cur.execute(
                "SELECT COUNT(*) AS cnt FROM course_orders WHERE company_id = %s",
                (company_id,),
            )
            row = cur.fetchone()
            kpis['catalog_tools'] = (row['cnt'] if row else 0) or 0

        cur.close()
    except Exception:
        pass

    return kpis


@dashboard_bp.app_template_global()
def spark_poly(values):
    """SVG polyline points (74x26) for a small series; a flat line when empty."""
    vals = [float(v) for v in (values or [])]
    if len(vals) < 2:
        return "0,13 74,13"
    lo, hi = min(vals), max(vals)
    span = (hi - lo) or 1.0
    step = 74.0 / (len(vals) - 1)
    return " ".join("%.0f,%.0f" % (i * step, 24 - ((v - lo) / span) * 22 if hi != lo else 13)
                    for i, v in enumerate(vals))


def _daily(rows, days=7):
    """[(date_str, n)] -> list of ``days`` ints ending today (missing days = 0)."""
    import datetime
    by = {}
    for r in rows or []:
        d = r.get('d') if isinstance(r, dict) else r[0]
        n = r.get('n') if isinstance(r, dict) else r[1]
        by[str(d)[:10]] = int(n or 0)
    today = datetime.date.today()
    return [by.get((today - datetime.timedelta(days=i)).isoformat(), 0) for i in range(days - 1, -1, -1)]


def _fetch_dashboard_extras():
    """Real data for the sparklines, "Seneste aktivitet" and "Populære kurser"
    (N-4.8). Every query is guarded; empty results render proper empty states."""
    extras = {'sparks': {}, 'activity': [], 'popular': []}
    username = session.get('user')
    company_id = session.get('company_id')
    if not username:
        return extras
    try:
        import order_lifecycle as lc
        cur = current_app.mysql.connection.cursor()
        try:
            cur.execute("SELECT DATE(`timestamp`) AS d, COUNT(*) AS n FROM notifications "
                        "WHERE user_id = %s AND `timestamp` >= DATE_SUB(NOW(), INTERVAL 7 DAY) GROUP BY DATE(`timestamp`)",
                        (username,))
            extras['sparks']['notifications'] = _daily(cur.fetchall())
            cur.execute("SELECT DATE(`timestamp`) AS d, COUNT(*) AS n FROM credit_usage "
                        "WHERE username = %s AND `timestamp` >= DATE_SUB(NOW(), INTERVAL 7 DAY) GROUP BY DATE(`timestamp`)",
                        (username,))
            extras['sparks']['credits'] = _daily(cur.fetchall())
            if company_id:
                cur.execute("SELECT DATE(requested_at) AS d, COUNT(*) AS n FROM order_approvals "
                            "WHERE company_id = %s AND requested_at >= DATE_SUB(NOW(), INTERVAL 7 DAY) GROUP BY DATE(requested_at)",
                            (company_id,))
                extras['sparks']['approvals'] = _daily(cur.fetchall())
                cur.execute("SELECT DATE(created_at) AS d, COUNT(*) AS n FROM course_orders "
                            "WHERE company_id = %s AND created_at >= DATE_SUB(NOW(), INTERVAL 7 DAY) GROUP BY DATE(created_at)",
                            (company_id,))
                extras['sparks']['orders'] = _daily(cur.fetchall())
                scope_sql, scope_params = "h.company_id = %s", (company_id,)
                pop_sql, pop_params = "company_id = %s AND", (company_id,)
            else:
                scope_sql, scope_params = "co.username = %s", (username,)
                pop_sql, pop_params = "", ()
            cur.execute(
                "SELECT co.product_title AS title, h.to_value AS status, h.kind AS kind, h.created_at AS at "
                "FROM order_status_history h JOIN course_orders co ON co.order_id = h.order_id "
                "WHERE " + scope_sql + " AND h.kind = 'status' ORDER BY h.created_at DESC LIMIT 6", scope_params)
            for r in cur.fetchall() or []:
                extras['activity'].append({
                    'text': "%s: %s" % (r['title'], lc.status_label(r['status'], short=True)),
                    'at': r['at'].strftime('%d.%m %H:%M') if hasattr(r['at'], 'strftime') else str(r['at'] or '')[:16],
                })
            cur.execute(
                "SELECT product_title AS title, COUNT(*) AS n FROM course_orders WHERE " + pop_sql +
                " status NOT IN ('cancelled', 'rejected') AND created_at >= DATE_SUB(NOW(), INTERVAL 90 DAY) "
                "GROUP BY product_title ORDER BY n DESC LIMIT 5", pop_params)
            extras['popular'] = [{'title': r['title'], 'n': int(r['n'])} for r in (cur.fetchall() or [])]
        finally:
            cur.close()
    except Exception as exc:
        current_app.logger.debug("dashboard extras skipped: %s", exc)
    return extras


@dashboard_bp.route('/dashboard')
def dashboard():
    """Landing page after login.

    * Not logged in -> the login page (this used to render a half-empty dashboard).
    * Learners without a management role (employees and solo users) land on
      "Min læring"; every "no access" bounce in the app also ends up there.
    * Managers and admins get the workspace overview.
    """
    if not session.get('user'):
        flash('Log ind for at komme videre.', 'info')
        return redirect(url_for('auth.login'))
    from capabilities import can
    if not can('company.workspace') and session.get('role') != 'admin':
        return redirect(url_for('futurematch.employee_home'))
    return render_template('fm/index.html', kpis=_fetch_dashboard_kpis(), extras=_fetch_dashboard_extras())
