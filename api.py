from flask import Blueprint, jsonify, session, current_app, request
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
def mark_notifications_read():
    if 'user' not in session:
        return jsonify({'success': False, 'error': 'Not authenticated'}), 401
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
def mark_notification_read(notification_id):
    if 'user' not in session:
        return jsonify({'success': False, 'error': 'Not authenticated'}), 401
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
def get_full_profile_api():
    username = session.get('user')
    if not username:
        return jsonify({'success': False, 'error': 'Not authenticated'}), 401
    try:
        from app1.user_profile_db import get_full_profile, ensure_tables
        ensure_tables()
        profile = get_full_profile(username)
        return jsonify({'success': True, 'profile': profile})
    except Exception as e:
        current_app.logger.error("Error fetching full profile: %s", e)
        return jsonify({'success': False, 'error': str(e)}), 500


@api_bp.route('/api/profile/skills', methods=['GET', 'POST', 'DELETE'])
def manage_skills_api():
    username = session.get('user')
    if not username:
        return jsonify({'success': False, 'error': 'Not authenticated'}), 401
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
def manage_experience_api():
    username = session.get('user')
    if not username:
        return jsonify({'success': False, 'error': 'Not authenticated'}), 401
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
def manage_education_api():
    username = session.get('user')
    if not username:
        return jsonify({'success': False, 'error': 'Not authenticated'}), 401
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
def manage_completed_courses_api():
    username = session.get('user')
    if not username:
        return jsonify({'success': False, 'error': 'Not authenticated'}), 401
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
def manage_profile_summary_api():
    username = session.get('user')
    if not username:
        return jsonify({'success': False, 'error': 'Not authenticated'}), 401
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




