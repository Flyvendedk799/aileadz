from flask import Blueprint, jsonify, session, current_app, request
from auth_decorators import login_required
import json

api_bp = Blueprint('api', __name__)

@api_bp.route('/api/credits')
def get_credits():
    username = session.get('user')
    if not username:
        return jsonify({'credits': 0})
    cur = current_app.mysql.connection.cursor()
    cur.execute("SELECT credits FROM users WHERE username = %s", (username,))
    result = cur.fetchone()
    credits = result['credits'] if result else 0
    return jsonify({'credits': credits})

@api_bp.route('/api/mark-notifications-read', methods=['POST'])
@login_required
def mark_notifications_read():
    user_id = session.get('user')
    data = request.get_json()
    if data and data.get('mark_all'):
        try:
            mysql = current_app.mysql
            cur = mysql.connection.cursor()
            query = "UPDATE notifications SET `read` = 1 WHERE user_id = %s AND `read` = 0"
            cur.execute(query, (user_id,))
            mysql.connection.commit()
            cur.close()
            return jsonify({'success': True})
        except Exception as e:
            current_app.logger.error("Error updating notifications: %s", e)
            return jsonify({'success': False, 'error': str(e)}), 500
    return jsonify({'success': False, 'error': 'Invalid data'}), 400

@api_bp.route('/api/notifications')
def get_notifications():
    if 'user' not in session:
        return jsonify({'notifications': []})
    user_id = session.get('user')
    try:
        mysql = current_app.mysql
        cur = mysql.connection.cursor()
        cur.execute("SELECT * FROM notifications WHERE user_id = %s ORDER BY timestamp DESC", (user_id,))
        notifications = cur.fetchall()
        cur.close()
        return jsonify({'notifications': notifications})
    except Exception as e:
        current_app.logger.error("Error fetching notifications: %s", e)
        return jsonify({'notifications': []})

@api_bp.route('/api/notifications/unread_count')
def unread_notifications_count():
    if 'user' not in session:
        return jsonify({'unread_count': 0})
    user_id = session.get('user')
    try:
        import MySQLdb.cursors
        cur = current_app.mysql.connection.cursor(MySQLdb.cursors.DictCursor)
        cur.execute("SELECT COUNT(*) AS unread_count FROM notifications WHERE user_id = %s AND `read` = 0", (user_id,))
        result = cur.fetchone()
        cur.close()
        unread_count = result['unread_count'] if result else 0
        return jsonify({'unread_count': unread_count})
    except Exception as e:
        current_app.logger.error("Error fetching unread notifications count: %s", e)
        return jsonify({'unread_count': 0})


@api_bp.route('/api/notifications/<int:notification_id>/mark_read', methods=['POST'])
@login_required
def mark_notification_read(notification_id):
    user_id = session.get('user')
    try:
         mysql = current_app.mysql
         cur = mysql.connection.cursor()
         cur.execute("UPDATE notifications SET `read` = 1 WHERE id = %s AND user_id = %s", (notification_id, user_id))
         mysql.connection.commit()
         cur.close()
         return jsonify({'success': True})
    except Exception as e:
         current_app.logger.error("Error marking notification as read: %s", e)
         return jsonify({'success': False, 'error': str(e)}), 500

# ── User Profile / CV API ──

def _require_login():
    if 'user' not in session:
        return None, jsonify({'success': False, 'error': 'Not authenticated'}), 401
    return session['user'], None, None


@api_bp.route('/api/profile/full')
@login_required
def get_full_profile_api():
    username = session.get('user')
    try:
        from app1.user_profile_db import get_full_profile, ensure_tables
        ensure_tables()
        profile = get_full_profile(username)
        return jsonify({'success': True, 'profile': profile})
    except Exception as e:
        current_app.logger.error("Error fetching full profile: %s", e)
        return jsonify({'success': False, 'error': str(e)}), 500


@api_bp.route('/api/profile/skills', methods=['GET', 'POST', 'DELETE'])
@login_required
def manage_skills_api():
    username = session.get('user')
    try:
        from app1.user_profile_db import get_skills, add_skill, remove_skill, update_skill_level, ensure_tables
        ensure_tables()

        if request.method == 'GET':
            return jsonify({'success': True, 'skills': get_skills(username)})

        data = request.get_json() or {}
        if request.method == 'POST':
            name = data.get('skill_name', '').strip()
            level = data.get('skill_level', 'mellem')
            source = data.get('source', 'manual')
            if not name:
                return jsonify({'success': False, 'error': 'skill_name required'}), 400
            add_skill(username, name, level, source)
            return jsonify({'success': True, 'message': f'Skill "{name}" added'})

        if request.method == 'DELETE':
            name = data.get('skill_name', '').strip()
            if not name:
                return jsonify({'success': False, 'error': 'skill_name required'}), 400
            removed = remove_skill(username, name)
            return jsonify({'success': True, 'removed': removed})
    except Exception as e:
        current_app.logger.error("Skills API error: %s", e)
        return jsonify({'success': False, 'error': str(e)}), 500


@api_bp.route('/api/profile/experience', methods=['GET', 'POST', 'PUT', 'DELETE'])
@login_required
def manage_experience_api():
    username = session.get('user')
    try:
        from app1.user_profile_db import get_experience, add_experience, remove_experience, update_experience, ensure_tables
        ensure_tables()

        if request.method == 'GET':
            return jsonify({'success': True, 'experience': get_experience(username)})

        data = request.get_json() or {}
        if request.method == 'POST':
            title = data.get('title', '').strip()
            if not title:
                return jsonify({'success': False, 'error': 'title required'}), 400
            new_id = add_experience(
                username, title,
                company=data.get('company', ''),
                start_year=data.get('start_year'),
                end_year=data.get('end_year'),
                is_current=data.get('is_current', False),
                description=data.get('description', '')
            )
            return jsonify({'success': True, 'id': new_id})

        if request.method == 'PUT':
            exp_id = data.get('id')
            if not exp_id:
                return jsonify({'success': False, 'error': 'id required'}), 400
            fields = {k: v for k, v in data.items() if k != 'id'}
            updated = update_experience(username, exp_id, **fields)
            return jsonify({'success': True, 'updated': updated})

        if request.method == 'DELETE':
            exp_id = data.get('id')
            if not exp_id:
                return jsonify({'success': False, 'error': 'id required'}), 400
            removed = remove_experience(username, exp_id)
            return jsonify({'success': True, 'removed': removed})
    except Exception as e:
        current_app.logger.error("Experience API error: %s", e)
        return jsonify({'success': False, 'error': str(e)}), 500


@api_bp.route('/api/profile/education', methods=['GET', 'POST', 'PUT', 'DELETE'])
@login_required
def manage_education_api():
    username = session.get('user')
    try:
        from app1.user_profile_db import get_education, add_education, remove_education, update_education, ensure_tables
        ensure_tables()

        if request.method == 'GET':
            return jsonify({'success': True, 'education': get_education(username)})

        data = request.get_json() or {}
        if request.method == 'POST':
            degree = data.get('degree', '').strip()
            if not degree:
                return jsonify({'success': False, 'error': 'degree required'}), 400
            new_id = add_education(
                username, degree,
                institution=data.get('institution', ''),
                year_completed=data.get('year_completed'),
                description=data.get('description', '')
            )
            return jsonify({'success': True, 'id': new_id})

        if request.method == 'PUT':
            edu_id = data.get('id')
            if not edu_id:
                return jsonify({'success': False, 'error': 'id required'}), 400
            fields = {k: v for k, v in data.items() if k != 'id'}
            updated = update_education(username, edu_id, **fields)
            return jsonify({'success': True, 'updated': updated})

        if request.method == 'DELETE':
            edu_id = data.get('id')
            if not edu_id:
                return jsonify({'success': False, 'error': 'id required'}), 400
            removed = remove_education(username, edu_id)
            return jsonify({'success': True, 'removed': removed})
    except Exception as e:
        current_app.logger.error("Education API error: %s", e)
        return jsonify({'success': False, 'error': str(e)}), 500


@api_bp.route('/api/profile/courses', methods=['GET', 'POST', 'DELETE'])
@login_required
def manage_completed_courses_api():
    username = session.get('user')
    try:
        from app1.user_profile_db import get_completed_courses, add_completed_course, remove_completed_course, ensure_tables
        ensure_tables()

        if request.method == 'GET':
            return jsonify({'success': True, 'courses': get_completed_courses(username)})

        data = request.get_json() or {}
        if request.method == 'POST':
            title = data.get('course_title', '').strip()
            if not title:
                return jsonify({'success': False, 'error': 'course_title required'}), 400
            add_completed_course(
                username, title,
                course_handle=data.get('course_handle'),
                vendor=data.get('vendor', ''),
                completed_date=data.get('completed_date'),
                certificate_note=data.get('certificate_note')
            )
            return jsonify({'success': True, 'message': f'Course "{title}" added'})

        if request.method == 'DELETE':
            title = data.get('course_title', '').strip()
            if not title:
                return jsonify({'success': False, 'error': 'course_title required'}), 400
            removed = remove_completed_course(username, title)
            return jsonify({'success': True, 'removed': removed})
    except Exception as e:
        current_app.logger.error("Courses API error: %s", e)
        return jsonify({'success': False, 'error': str(e)}), 500


@api_bp.route('/api/profile/summary', methods=['GET', 'POST'])
@login_required
def manage_profile_summary_api():
    username = session.get('user')
    try:
        from app1.user_profile_db import get_profile_summary, update_profile_summary, ensure_tables
        ensure_tables()

        if request.method == 'GET':
            summary = get_profile_summary(username)
            return jsonify({'success': True, 'summary': summary or {}})

        data = request.get_json() or {}
        update_profile_summary(username, **data)
        return jsonify({'success': True, 'message': 'Profile summary updated'})
    except Exception as e:
        current_app.logger.error("Profile summary API error: %s", e)
        return jsonify({'success': False, 'error': str(e)}), 500


@api_bp.route('/api/profile/certifications', methods=['GET', 'POST', 'PUT', 'DELETE'])
@login_required
def manage_certifications_api():
    username = session.get('user')
    try:
        from app1.user_profile_db import (
            get_certifications, add_certification, remove_certification,
            update_certification, ensure_tables
        )
        ensure_tables()

        if request.method == 'GET':
            return jsonify({'success': True, 'certifications': get_certifications(username)})

        data = request.get_json() or {}
        if request.method == 'POST':
            name = (data.get('name') or '').strip()
            if not name:
                return jsonify({'success': False, 'error': 'name required'}), 400
            new_id = add_certification(
                username, name,
                issuer=data.get('issuer', ''),
                issue_date=data.get('issue_date'),
                expiry_date=data.get('expiry_date'),
                credential_id=data.get('credential_id'),
                credential_url=data.get('credential_url'),
                source=data.get('source', 'manual'),
            )
            return jsonify({'success': True, 'id': new_id})

        if request.method == 'PUT':
            cert_id = data.get('id')
            if not cert_id:
                return jsonify({'success': False, 'error': 'id required'}), 400
            fields = {k: v for k, v in data.items() if k != 'id'}
            updated = update_certification(username, cert_id, **fields)
            return jsonify({'success': True, 'updated': updated})

        if request.method == 'DELETE':
            cert_id = data.get('id')
            if not cert_id:
                return jsonify({'success': False, 'error': 'id required'}), 400
            removed = remove_certification(username, cert_id)
            return jsonify({'success': True, 'removed': removed})
    except Exception as e:
        current_app.logger.error("Certifications API error: %s", e)
        return jsonify({'success': False, 'error': str(e)}), 500


@api_bp.route('/api/profile/languages', methods=['GET', 'POST', 'PUT', 'DELETE'])
@login_required
def manage_languages_api():
    username = session.get('user')
    try:
        from app1.user_profile_db import (
            get_languages, add_language, remove_language, update_language_level, ensure_tables
        )
        ensure_tables()

        if request.method == 'GET':
            return jsonify({'success': True, 'languages': get_languages(username)})

        data = request.get_json() or {}
        if request.method == 'POST':
            language = (data.get('language') or '').strip()
            if not language:
                return jsonify({'success': False, 'error': 'language required'}), 400
            add_language(username, language,
                         proficiency=data.get('proficiency', 'mellem'),
                         source=data.get('source', 'manual'))
            return jsonify({'success': True, 'message': f'Language "{language}" added'})

        if request.method == 'PUT':
            language = (data.get('language') or '').strip()
            level = (data.get('proficiency') or '').strip()
            if not language or not level:
                return jsonify({'success': False, 'error': 'language and proficiency required'}), 400
            updated = update_language_level(username, language, level)
            return jsonify({'success': True, 'updated': updated})

        if request.method == 'DELETE':
            language = (data.get('language') or '').strip()
            if not language:
                return jsonify({'success': False, 'error': 'language required'}), 400
            removed = remove_language(username, language)
            return jsonify({'success': True, 'removed': removed})
    except Exception as e:
        current_app.logger.error("Languages API error: %s", e)
        return jsonify({'success': False, 'error': str(e)}), 500


@api_bp.route('/api/profile/links', methods=['GET', 'POST', 'DELETE'])
@login_required
def manage_portfolio_links_api():
    username = session.get('user')
    try:
        from app1.user_profile_db import (
            get_portfolio_links, add_portfolio_link, remove_portfolio_link, ensure_tables
        )
        ensure_tables()

        if request.method == 'GET':
            return jsonify({'success': True, 'links': get_portfolio_links(username)})

        data = request.get_json() or {}
        if request.method == 'POST':
            url = (data.get('url') or '').strip()
            if not url:
                return jsonify({'success': False, 'error': 'url required'}), 400
            new_id = add_portfolio_link(username, data.get('label', ''), url, kind=data.get('kind'))
            return jsonify({'success': True, 'id': new_id})

        if request.method == 'DELETE':
            link_id = data.get('id')
            if not link_id:
                return jsonify({'success': False, 'error': 'id required'}), 400
            removed = remove_portfolio_link(username, link_id)
            return jsonify({'success': True, 'removed': removed})
    except Exception as e:
        current_app.logger.error("Portfolio links API error: %s", e)
        return jsonify({'success': False, 'error': str(e)}), 500


@api_bp.route('/api/profile/orders')
@login_required
def get_profile_orders_api():
    """Compact "my orders" feed for the profile page.

    Reuses the exact same strictly-user-scoped query and status mappings as the
    /min-tidslinje timeline so the profile card and the full page never disagree.
    Always degrades to an empty list so the profile page never breaks on a DB hiccup.
    """
    import datetime
    username = session.get('user')
    user_id = session.get('user_id')
    limit = request.args.get('limit', default=5, type=int) or 5
    limit = max(1, min(limit, 25))
    items = []
    try:
        import MySQLdb.cursors
        from db_compat import refresh_flask_mysql_connection
        from futurematch_ui import _ORDER_STATE, _ORDER_STATUS_LABELS, _ensure_timeline_tables
        mysql = getattr(current_app, 'mysql', None)
        refresh_flask_mysql_connection(mysql)
        conn = mysql.connection if mysql else None
        if conn is not None:
            _ensure_timeline_tables(conn)
            cur = conn.cursor(MySQLdb.cursors.DictCursor)
            cur.execute(
                """
                SELECT co.order_id, co.product_title, co.price, co.status,
                       co.created_at, co.completion_deadline, co.completion_status,
                       oa.status AS approval_status
                FROM course_orders co
                LEFT JOIN order_approvals oa ON oa.order_id = co.order_id
                WHERE (co.username = %s OR (co.user_id IS NOT NULL AND co.user_id = %s))
                ORDER BY co.created_at DESC
                """,
                (username, user_id),
            )
            rows = cur.fetchall() or []
            cur.close()
            now = datetime.datetime.now()
            for r in rows:
                raw_status = (r.get('status') or 'pending')
                deadline = r.get('completion_deadline')
                state = _ORDER_STATE.get(raw_status, 'afventer')
                overdue = bool(
                    deadline and state not in ('gennemfoert', 'annulleret', 'afvist')
                    and deadline < now
                )
                price = r.get('price')
                items.append({
                    'order_id': r.get('order_id'),
                    'title': r.get('product_title') or 'Ukendt kursus',
                    'created_at': r.get('created_at').isoformat() if r.get('created_at') else None,
                    'status': raw_status,
                    'status_label': _ORDER_STATUS_LABELS.get(raw_status, raw_status),
                    'state': state,
                    'approval_label': _ORDER_STATUS_LABELS.get(
                        r.get('approval_status'), r.get('approval_status')
                    ) if r.get('approval_status') else None,
                    'deadline': r.get('completion_deadline').isoformat() if r.get('completion_deadline') else None,
                    'overdue': overdue,
                    'price': float(price) if price is not None else None,
                })
    except Exception as e:
        current_app.logger.warning("Profile orders API: %s", e)
        return jsonify({'success': True, 'orders': [], 'total': 0, 'summary': {}})

    summary = {
        'total': len(items),
        'afventer': sum(1 for i in items if i['state'] in ('afventer', 'afventer_godkendelse')),
        'aktive': sum(1 for i in items if i['state'] == 'godkendt'),
        'gennemfoert': sum(1 for i in items if i['state'] == 'gennemfoert'),
        'overdue': sum(1 for i in items if i['overdue']),
    }
    return jsonify({'success': True, 'orders': items[:limit], 'total': len(items), 'summary': summary})


# ── Company notification center (company_notifications, recipient-scoped) ──
#
# These power the fm notification center + bell badge. They read the
# company_notifications table (company_id + recipient scoped) — distinct from the
# legacy `notifications` table above, which other surfaces still use. All routes
# degrade to a safe empty/zero result so the shell never breaks.

def _company_notif_scope():
    """(company_id, user_id, company_role_json) or None if not in a company."""
    company_id = session.get('company_id')
    if not company_id:
        return None
    return (company_id, session.get('user_id'),
            json.dumps(session.get('company_role')))


@api_bp.route('/api/notifications/company')
@login_required
def company_notifications_list():
    """Recent company notifications + unread count for the session user."""
    scope = _company_notif_scope()
    if not scope:
        return jsonify({'notifications': [], 'unread_count': 0})
    company_id, user_id, role_json = scope
    try:
        import MySQLdb.cursors
        cur = current_app.mysql.connection.cursor(MySQLdb.cursors.DictCursor)
        cur.execute(
            """
            SELECT id, title, message, is_urgent, is_read, created_at
            FROM company_notifications
            WHERE company_id = %s
              AND (recipient_user_id = %s OR recipient_user_id IS NULL)
              AND (target_roles IS NULL OR JSON_CONTAINS(target_roles, %s))
            ORDER BY is_read ASC, is_urgent DESC, created_at DESC
            LIMIT 20
            """,
            (company_id, user_id, role_json),
        )
        rows = cur.fetchall() or []
        cur.close()
        notifs = []
        unread = 0
        for r in rows:
            is_read = int(r.get('is_read') or 0)
            if not is_read:
                unread += 1
            created = r.get('created_at')
            notifs.append({
                'id': r.get('id'),
                'title': r.get('title'),
                'message': r.get('message'),
                'is_urgent': int(r.get('is_urgent') or 0),
                'is_read': is_read,
                'created_at': created.strftime('%d.%m %H:%M') if hasattr(created, 'strftime') else str(created or ''),
            })
        return jsonify({'notifications': notifs, 'unread_count': unread})
    except Exception as e:
        current_app.logger.warning("company notifications list: %s", e)
        return jsonify({'notifications': [], 'unread_count': 0})


@api_bp.route('/api/notifications/company/unread_count')
@login_required
def company_notifications_unread_count():
    """Unread company-notification count — drives the bell badge poll."""
    scope = _company_notif_scope()
    if not scope:
        return jsonify({'unread_count': 0})
    company_id, user_id, role_json = scope
    try:
        import MySQLdb.cursors
        cur = current_app.mysql.connection.cursor(MySQLdb.cursors.DictCursor)
        cur.execute(
            """
            SELECT COUNT(*) AS unread_count
            FROM company_notifications
            WHERE company_id = %s AND is_read = 0
              AND (recipient_user_id = %s OR recipient_user_id IS NULL)
              AND (target_roles IS NULL OR JSON_CONTAINS(target_roles, %s))
            """,
            (company_id, user_id, role_json),
        )
        row = cur.fetchone()
        cur.close()
        return jsonify({'unread_count': int(row['unread_count']) if row else 0})
    except Exception as e:
        current_app.logger.warning("company notifications unread count: %s", e)
        return jsonify({'unread_count': 0})


@api_bp.route('/api/notifications/<int:notification_id>/read', methods=['POST'])
@login_required
def company_notification_mark_read(notification_id):
    """Mark a single company notification read — strictly company-scoped.

    Only rows belonging to the session user's company and addressed to them (or
    broadcast) can be marked. This is the only write here, so it commits.
    """
    company_id = session.get('company_id')
    user_id = session.get('user_id')
    if not company_id:
        return jsonify({'success': False, 'error': 'No company context'}), 403
    try:
        mysql = current_app.mysql
        cur = mysql.connection.cursor()
        cur.execute(
            """
            UPDATE company_notifications SET is_read = 1
            WHERE id = %s AND company_id = %s
              AND (recipient_user_id = %s OR recipient_user_id IS NULL)
            """,
            (notification_id, company_id, user_id),
        )
        mysql.connection.commit()
        cur.close()
        return jsonify({'success': True})
    except Exception as e:
        current_app.logger.error("company notification mark read: %s", e)
        try:
            current_app.mysql.connection.rollback()
        except Exception:
            pass
        return jsonify({'success': False, 'error': str(e)}), 500


@api_bp.route('/api/notifications/unread_list')
def unread_notifications_list():
    if 'user' not in session:
        return jsonify({'notifications': []})
    user_id = session.get('user')
    try:
        import MySQLdb.cursors
        cur = current_app.mysql.connection.cursor(MySQLdb.cursors.DictCursor)
        cur.execute("SELECT id, title, timestamp, image_url FROM notifications WHERE user_id = %s AND `read` = 0 ORDER BY timestamp DESC LIMIT 5", (user_id,))
        notifs = cur.fetchall()
        cur.close()
        return jsonify({'notifications': notifs})
    except Exception as e:
        current_app.logger.error("Error fetching unread notifications list: %s", e)
        return jsonify({'notifications': []})




