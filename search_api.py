"""
search_api.py — GET /api/search?q= for the global ⌘K command palette.

Returns live entity hits as JSON, scoped by role:
  * any company member -> employees in THEIR company only,
  * platform admin     -> companies + users across the platform.

Strict company isolation (WHERE company_id = session company). Read-only,
guarded, never raises. Path is under /api so a 401 returns JSON, not a redirect.
"""

import logging

from flask import Blueprint, request, jsonify, session, current_app

logger = logging.getLogger(__name__)

search_api_bp = Blueprint('search_api', __name__)


def _cursor():
    import MySQLdb.cursors
    return current_app.mysql.connection.cursor(MySQLdb.cursors.DictCursor)


@search_api_bp.route('/api/search')
def search():
    if 'user' not in session:
        return jsonify({'error': 'unauthorized'}), 401

    q = (request.args.get('q') or '').strip()
    if len(q) < 2:
        return jsonify({'query': q, 'groups': []})
    like = f"%{q}%"
    groups = []

    company_id = session.get('company_id')
    is_admin = session.get('role') == 'admin'

    # --- Employees in the current company -------------------------------
    if company_id:
        try:
            cur = _cursor()
            cur.execute(
                """SELECT cu.user_id, COALESCE(cu.full_name, u.username) AS name,
                          u.username, u.email, cu.department
                   FROM company_users cu JOIN users u ON cu.user_id = u.id
                   WHERE cu.company_id = %s AND cu.status = 'active'
                     AND (COALESCE(cu.full_name,'') LIKE %s OR u.username LIKE %s
                          OR COALESCE(u.email,'') LIKE %s)
                   ORDER BY name LIMIT 6""",
                (company_id, like, like, like),
            )
            items = [{
                'title': r['name'], 'sub': (r.get('department') or r.get('email') or ''),
                'href': f"/hr/employee/{r['user_id']}/details", 'icon': 'fa-user',
            } for r in (cur.fetchall() or [])]
            cur.close()
            if items:
                groups.append({'label': 'Medarbejdere', 'items': items})
        except Exception as e:
            logger.warning("search_api: employee search failed: %s", e)

    # --- Platform-admin: companies + users ------------------------------
    if is_admin:
        try:
            cur = _cursor()
            cur.execute(
                """SELECT id, company_name, company_slug FROM companies
                   WHERE company_name LIKE %s OR company_slug LIKE %s
                   ORDER BY company_name LIMIT 6""",
                (like, like),
            )
            items = [{
                'title': r['company_name'], 'sub': r.get('company_slug') or '',
                'href': f"/companies/admin/{r['id']}", 'icon': 'fa-building',
            } for r in (cur.fetchall() or [])]
            cur.close()
            if items:
                groups.append({'label': 'Virksomheder', 'items': items})
        except Exception as e:
            logger.warning("search_api: company search failed: %s", e)

        try:
            cur = _cursor()
            cur.execute(
                """SELECT id, username, email FROM users
                   WHERE username LIKE %s OR COALESCE(email,'') LIKE %s
                   ORDER BY username LIMIT 6""",
                (like, like),
            )
            items = [{
                'title': r['username'], 'sub': r.get('email') or '',
                'href': "/admin/users", 'icon': 'fa-user-gear',
            } for r in (cur.fetchall() or [])]
            cur.close()
            if items:
                groups.append({'label': 'Brugere', 'items': items})
        except Exception as e:
            logger.warning("search_api: user search failed: %s", e)

    return jsonify({'query': q, 'groups': groups})
