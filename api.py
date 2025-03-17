from flask import Blueprint, jsonify, session, current_app

api_bp = Blueprint('api', __name__)

@api_bp.route('/api/credits')
def get_credits():
    # Use the username from the session since that's how users are identified in your table.
    username = session.get('user')
    if not username:
        return jsonify({'credits': 0})
    cur = current_app.mysql.connection.cursor()
    cur.execute("SELECT credits FROM users WHERE username = %s", (username,))
    result = cur.fetchone()
    credits = result['credits'] if result else 0
    return jsonify({'credits': credits})
