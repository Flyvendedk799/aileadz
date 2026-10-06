from flask import Blueprint, render_template, session, current_app, redirect, url_for, flash

dashboard_bp = Blueprint('dashboard', __name__, template_folder='templates')


_ROLE_LABELS_DA = {
    'admin': 'Administrator',
    'company_admin': 'Virksomhedsadministrator',
    'hr_manager': 'HR-leder',
    'department_head': 'Afdelingsleder',
    'employee': 'Medarbejder',
}


@dashboard_bp.app_template_filter('dknum')
def dknum(value, decimals=None):
    """Danish number format: '.' as thousands separator, ',' as decimal mark.

    ``decimals=None`` keeps whole numbers whole and shows one decimal for
    fractions (12.5 -> '12,5'); an explicit ``decimals`` fixes the precision.
    Non-numeric input is returned unchanged so a template never breaks on it.
    """
    try:
        num = float(value)
    except (TypeError, ValueError):
        return value
    if decimals is None:
        decimals = 0 if num.is_integer() else 1
    text = "{:,.{d}f}".format(num, d=int(decimals))
    return text.replace(",", "\x00").replace(".", ",").replace("\x00", ".")


@dashboard_bp.app_template_filter('dkmoney')
def dkmoney(value):
    """The one price format: '12.500 kr.' (whole amounts) or '12.500,50 kr.'.

    Accepts numbers and numeric strings ('12500.00'). Empty input gives '';
    non-numeric input is returned unchanged so a template never breaks on it.
    """
    if value is None or value == '':
        return ''
    try:
        num = float(value)
    except (TypeError, ValueError):
        return value
    rounded = round(num, 2)
    return '%s kr.' % dknum(rounded, 0 if float(rounded).is_integer() else 2)


@dashboard_bp.app_template_global('course_date')
def course_date(order, booking=None, with_time=True, style='long'):
    """The course date of an order for display ("3. december 2026 kl. 09.00"):
    the booking's start when booked, else the ordered session label. See
    ``order_timing.course_label``."""
    import order_timing
    return order_timing.course_label(order, booking, with_time=with_time, style=style)


@dashboard_bp.app_template_filter('dkdate')
def dkdate(value, with_time=False, style=None):
    """Danish date format 'dd.mm.yyyy' (optionally ' hh:mm') for datetimes and
    ISO strings ('2026-10-01', '2026-10-01T08:30:00'). Empty input gives '';
    unparseable input is returned unchanged.

    ``style`` opts into the order-page formats of ``order_timing.format_date``:
    ``'short'`` = '03.12.2026', ``'long'`` = '3. december 2026'; with
    ``with_time`` they append ' kl. 09.00'. Without ``style`` the output is the
    legacy one above."""
    import datetime as _dt
    if style:
        import order_timing
        return order_timing.format_date(value, style=style, with_time=with_time)
    if value is None or value == '':
        return ''
    dt_value = value
    if isinstance(value, str):
        try:
            dt_value = _dt.datetime.fromisoformat(value.strip().replace('Z', '')[:19])
        except ValueError:
            return value
    if isinstance(dt_value, _dt.datetime):
        return dt_value.strftime('%d.%m.%Y %H:%M' if with_time else '%d.%m.%Y')
    if isinstance(dt_value, _dt.date):
        return dt_value.strftime('%d.%m.%Y')
    return value


def _fetch_dashboard_kpis():
    kpis = {
        'unread_notifications': 0,
        'pending_approvals': None,
        'catalog_tools': None,
        'role_label': 'Bruger',
    }
    try:
        from capabilities import effective_role
        kpis['role_label'] = _ROLE_LABELS_DA.get(effective_role(), 'Bruger')
    except Exception:
        kpis['role_label'] = 'Bruger'

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
