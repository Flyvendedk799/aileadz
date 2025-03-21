from flask import Blueprint, jsonify, session, current_app, request

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




