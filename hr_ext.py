"""
hr_ext.py — NEW HR dashboard feature pages that surface dormant AI/data
capabilities (Pillar B) without touching the 4346-line hr_dashboard factory.

Registered with url_prefix='/hr' alongside hr_dashboard_bp. Every page:
  * gates on the same company-role rule as require_hr_access (company_admin /
    hr_manager / department_head, plus platform-admin bypass),
  * is strictly company_id-scoped via session['company_id'] (the HR tool
    executors read it themselves),
  * reuses existing logic (hr_tools._execute_*, calendar_service.build_ics,
    order_service) — never re-derives,
  * degrades to an empty state and never crashes boot (guarded imports).

All user-facing strings are Danish.
"""

import logging

from flask import (
    Blueprint, render_template, request, redirect, url_for, flash, session,
    current_app, jsonify, Response,
)

logger = logging.getLogger(__name__)

hr_ext_bp = Blueprint('hr_ext', __name__)

_HR_ROLES = ('company_admin', 'hr_manager', 'department_head')


# ---------------------------------------------------------------------------
# Gating + company resolution (mirrors hr_dashboard.require_hr_access, which is
# nested in a factory and not importable).
# ---------------------------------------------------------------------------
def _hr_ok():
    if 'user' not in session or not session.get('company_id'):
        return False
    return (session.get('company_role') in _HR_ROLES
            or session.get('role') == 'admin')


def _company():
    """Minimal company context for templates (id + display name)."""
    return {
        'id': session.get('company_id'),
        'company_name': session.get('company_name', ''),
    }


def _guard_html():
    if not _hr_ok():
        flash("Du har ikke adgang til HR-funktioner.", "danger")
        return redirect(url_for('dashboard.dashboard'))
    return None


def _tool(executor_name, args=None):
    """Call an hr_tools executor by name via _json_tool. Returns {} on failure."""
    try:
        import hr_tools
        fn = getattr(hr_tools, executor_name, None)
        if fn is None:
            return {}
        result = hr_tools._json_tool(fn, args or {})
        return result if isinstance(result, dict) else {'items': result}
    except Exception as e:  # pragma: no cover - defensive
        logger.warning("hr_ext: tool %s failed: %s", executor_name, e)
        return {}


def _departments():
    """Distinct active departments for the current company (for filters)."""
    try:
        import MySQLdb.cursors
        cur = current_app.mysql.connection.cursor(MySQLdb.cursors.DictCursor)
        cur.execute(
            """SELECT DISTINCT department FROM company_users
               WHERE company_id = %s AND status = 'active'
                 AND department IS NOT NULL AND department <> ''
               ORDER BY department""",
            (session.get('company_id'),),
        )
        rows = cur.fetchall() or []
        cur.close()
        return [r['department'] for r in rows]
    except Exception as e:
        logger.warning("hr_ext: department lookup failed: %s", e)
        return []


# ---------------------------------------------------------------------------
# B1 — Training Plan Builder (hr_recommend_training_plan)
# ---------------------------------------------------------------------------
@hr_ext_bp.route('/training-plan')
def training_plan():
    g = _guard_html()
    if g:
        return g
    department = (request.args.get('department') or '').strip()
    focus = (request.args.get('focus') or '').strip()
    args = {}
    if department:
        args['department'] = department
    if focus:
        args['focus'] = focus
    plan = _tool('_execute_hr_recommend_training_plan', args)
    return render_template(
        'fm/training_plan.html',
        company=_company(), plan=plan, departments=_departments(),
        selected_department=department, focus=focus,
    )


# ---------------------------------------------------------------------------
# B2 — Procurement / Supplier Brief (hr_get_supplier_coverage + expiring)
# ---------------------------------------------------------------------------
@hr_ext_bp.route('/procurement')
def procurement():
    g = _guard_html()
    if g:
        return g
    coverage = _tool('_execute_hr_get_supplier_coverage', {})
    expiring = _tool('_execute_hr_expiring_agreements', {})
    return render_template(
        'fm/procurement.html',
        company=_company(), coverage=coverage, expiring=expiring,
    )


# ---------------------------------------------------------------------------
# B3 — AI Quality / Search-quality console (hr_get_ai_usage_risks + insights)
# ---------------------------------------------------------------------------
@hr_ext_bp.route('/ai-quality')
def ai_quality():
    g = _guard_html()
    if g:
        return g
    risks = _tool('_execute_hr_get_ai_usage_risks', {})
    insights = _recent_insights()
    return render_template(
        'fm/ai_quality.html',
        company=_company(), risks=risks, insights=insights,
    )


def _recent_insights():
    try:
        import MySQLdb.cursors
        cur = current_app.mysql.connection.cursor(MySQLdb.cursors.DictCursor)
        cur.execute(
            """SELECT id, insight_type, title, body, severity, generated_at, is_read
               FROM company_insights
               WHERE company_id = %s AND (expires_at IS NULL OR expires_at > NOW())
               ORDER BY generated_at DESC LIMIT 12""",
            (session.get('company_id'),),
        )
        rows = cur.fetchall() or []
        cur.close()
        return rows
    except Exception as e:
        logger.warning("hr_ext: insight read failed: %s", e)
        return []


# ---------------------------------------------------------------------------
# B7 — Engagement / nudge workflows (non-starters + inactive employees)
# ---------------------------------------------------------------------------
@hr_ext_bp.route('/engagement')
def engagement():
    g = _guard_html()
    if g:
        return g
    non_starters = _tool('_execute_get_team_non_starters', {})
    inactive = _tool('_execute_hr_inactive_employees', {})
    return render_template(
        'fm/engagement.html',
        company=_company(), non_starters=non_starters, inactive=inactive,
    )


@hr_ext_bp.route('/engagement/nudge', methods=['POST'])
def engagement_nudge():
    if not _hr_ok():
        return jsonify({'success': False, 'message': 'Ikke autoriseret'}), 401
    data = request.get_json(silent=True) or request.form
    raw_ids = data.get('user_ids') or data.getlist('user_ids') if hasattr(data, 'getlist') else data.get('user_ids')
    if isinstance(raw_ids, str):
        raw_ids = [x for x in raw_ids.split(',') if x.strip()]
    try:
        user_ids = [int(x) for x in (raw_ids or [])]
    except (TypeError, ValueError):
        user_ids = []
    message = (data.get('message') or
               "Vi vil opfordre dig til at komme i gang med din læring. Log ind og se dine kurser.")
    if not user_ids:
        return jsonify({'success': False, 'message': 'Ingen medarbejdere valgt'}), 400

    company_id = session.get('company_id')
    sender_id = session.get('user_id')
    nudged = 0
    try:
        cur = current_app.mysql.connection.cursor()
        # Company-isolation: only nudge active members of this company.
        placeholders = ','.join(['%s'] * len(user_ids))
        cur.execute(
            f"""SELECT user_id FROM company_users
                WHERE company_id = %s AND status = 'active'
                  AND user_id IN ({placeholders})""",
            tuple([company_id] + user_ids),
        )
        valid = [r[0] for r in (cur.fetchall() or [])]
        for uid in valid:
            try:
                cur.execute(
                    """INSERT INTO company_notifications
                           (company_id, recipient_user_id, sender_user_id, target_roles,
                            title, message, is_urgent, is_read)
                       VALUES (%s, %s, %s, NULL, %s, %s, 0, 0)""",
                    (company_id, uid, sender_id, "Et venligt skub om din læring"[:255],
                     str(message)[:1000]),
                )
                nudged += 1
            except Exception as ie:
                logger.warning("hr_ext: nudge skipped for %s: %s", uid, ie)
        # Audit (best-effort, reuses order_service shape).
        try:
            from order_service import _write_audit
            _write_audit(cur, company_id=company_id, user_id=sender_id,
                         action="engagement.nudge", resource_id=str(nudged),
                         description=f"nudged {nudged} employees")
        except Exception:
            pass
        current_app.mysql.connection.commit()
        cur.close()
        return jsonify({'success': True, 'nudged': nudged})
    except Exception as e:
        logger.error("hr_ext: engagement_nudge failed: %s", e)
        try:
            current_app.mysql.connection.rollback()
        except Exception:
            pass
        return jsonify({'success': False, 'message': 'Kunne ikke sende påmindelser'}), 500


# ---------------------------------------------------------------------------
# C4 — Add-to-calendar (.ics) for orders and learning-path deadlines
# ---------------------------------------------------------------------------
def _ics_response(ics_text, filename):
    if not ics_text:
        return ("Kalenderhændelse kunne ikke genereres.", 404)
    resp = Response(ics_text, mimetype='text/calendar')
    resp.headers['Content-Disposition'] = f'attachment; filename="{filename}"'
    return resp


@hr_ext_bp.route('/calendar/order/<order_id>.ics')
def order_ics(order_id):
    if not _hr_ok():
        return redirect(url_for('dashboard.dashboard'))
    try:
        from order_service import OrderContext, get_order
        from calendar_service import build_ics
        row = get_order(OrderContext.from_session(), order_id)
        if not row or str(row.get('company_id')) != str(session.get('company_id')):
            return ("Ordren blev ikke fundet.", 404)
        title = f"Kursus: {row.get('product_title') or 'Uddannelse'}"
        start = row.get('variant_date') or row.get('created_at')
        ics = build_ics(
            title=title, start=start,
            location=row.get('variant_location') or '',
            description=f"Bestilt kursus (ordre {order_id}).",
        )
        return _ics_response(ics, f"kursus-{order_id}.ics")
    except Exception as e:
        logger.warning("hr_ext: order_ics failed: %s", e)
        return ("Kalenderhændelse kunne ikke genereres.", 500)


@hr_ext_bp.route('/calendar/deadline/<int:progress_id>.ics')
def deadline_ics(progress_id):
    if not _hr_ok():
        return redirect(url_for('dashboard.dashboard'))
    try:
        import MySQLdb.cursors
        from calendar_service import build_ics
        cur = current_app.mysql.connection.cursor(MySQLdb.cursors.DictCursor)
        cur.execute(
            """SELECT elp.id, elp.due_date, elp.company_id,
                      COALESCE(lp.path_name, lp.name, 'Læringsforløb') AS path_name
               FROM employee_learning_progress elp
               LEFT JOIN learning_paths lp ON lp.id = elp.learning_path_id
               WHERE elp.id = %s AND elp.company_id = %s""",
            (progress_id, session.get('company_id')),
        )
        row = cur.fetchone()
        cur.close()
        if not row or not row.get('due_date'):
            return ("Frist blev ikke fundet.", 404)
        ics = build_ics(
            title=f"Frist: {row.get('path_name')}", start=row.get('due_date'),
            description="Deadline for læringsforløb.",
        )
        return _ics_response(ics, f"frist-{progress_id}.ics")
    except Exception as e:
        logger.warning("hr_ext: deadline_ics failed: %s", e)
        return ("Kalenderhændelse kunne ikke genereres.", 500)


@hr_ext_bp.route('/calendar/feed.ics')
def calendar_feed():
    """Session-authed, company-scoped subscribable ICS feed of upcoming courses
    and deadlines ('Abonnér på virksomhedens kurser').

    Reuses the enterprise feed builder (enterprise_api.build_company_calendar_events
    + calendar_service.build_ics_feed) so the in-app feed exposes IDENTICAL data
    to the API-key feed — but behind the SESSION gate (the HR-role / company-scope
    rule the other hr_ext pages use), NOT the enterprise API key. No new
    people-level data surface: it is the same company-scoped order/deadline data
    the HR order tables already render. Always returns a valid (possibly empty)
    calendar so a subscribed client never sees a 500.
    """
    if not _hr_ok():
        return redirect(url_for('dashboard.dashboard'))
    company_id = session.get('company_id')
    try:
        import MySQLdb.cursors
        from calendar_service import build_ics_feed
        from enterprise_api import build_company_calendar_events
    except Exception as e:
        logger.warning("hr_ext: calendar feed deps unavailable: %s", e)
        return Response(
            "BEGIN:VCALENDAR\r\nVERSION:2.0\r\n"
            "PRODID:-//aileadz//calendar_service//DA\r\nEND:VCALENDAR\r\n",
            mimetype='text/calendar',
        )
    events = []
    try:
        cur = current_app.mysql.connection.cursor(MySQLdb.cursors.DictCursor)
        try:
            events = build_company_calendar_events(cur, company_id)
        finally:
            try:
                cur.close()
            except Exception:
                pass
    except Exception as e:
        logger.warning("hr_ext: calendar feed failed: %s", e)
        events = []
    ics = build_ics_feed(events, cal_name='Kurser & frister')
    resp = Response(ics, mimetype='text/calendar')
    resp.headers['Content-Disposition'] = 'inline; filename="aileadz-kalender.ics"'
    return resp
