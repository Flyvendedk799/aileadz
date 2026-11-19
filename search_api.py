"""
search_api.py — GET /api/search?q= for the global ⌘K command palette.

Returns live entity hits as JSON, scoped by role:
  * everyone signed in -> courses in the catalogue they may see, their OWN
    orders and their OWN development goals,
  * HR roles           -> employees in THEIR company only (department heads:
    their department),
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


def _own_results(q, like, company_id):
    """Courses, and the signed-in person's own orders and goals. Each part is
    independent: one failing source never hides the others."""
    groups = []
    try:
        import catalog_service
        found = catalog_service.search_products({'q': q}, page=1, per_page=5, company_id=company_id) or {}
        items = [{
            'title': p.get('title') or '', 'sub': p.get('vendor') or 'Kursus',
            'href': '/products/%s' % p.get('handle'), 'icon': 'fa-graduation-cap',
        } for p in (found.get('products') or [])[:5] if p.get('handle')]
        if items:
            groups.append({'label': 'Kurser', 'items': items})
    except Exception as e:
        logger.warning("search_api: course search failed: %s", e)

    user_id, username = session.get('user_id'), session.get('user')
    if user_id:
        try:
            import order_lifecycle
            cur = _cursor()
            scope, params = "user_id = %s", [user_id]
            if company_id:
                scope += " AND company_id = %s"
                params.append(company_id)
            cur.execute("SELECT order_id, product_title, status FROM course_orders WHERE " + scope +
                        " AND product_title LIKE %s ORDER BY created_at DESC LIMIT 5", tuple(params + [like]))
            items = [{
                'title': r['product_title'] or 'Bestilling', 'sub': order_lifecycle.status_label(r['status']),
                'href': '/min-ordre/%s' % r['order_id'], 'icon': 'fa-receipt',
            } for r in (cur.fetchall() or [])]
            cur.close()
            if items:
                groups.append({'label': 'Mine bestillinger', 'items': items})
        except Exception as e:
            logger.warning("search_api: order search failed: %s", e)
    if username:
        try:
            import goal_service
            cur = _cursor()
            try:
                own = goal_service.own_goals(cur, username)
            finally:
                cur.close()
            needle = like[1:-1].lower()  # the query without its LIKE wildcards
            hits = sorted((g for g in own if needle in g['title'].lower()),
                          key=lambda g: (g['status'] != 'active', -g['id']))[:5]
            labels = {'active': 'Aktivt mål', 'done': 'Fuldført mål', 'paused': 'Mål på pause'}
            items = [{
                'title': g['title'], 'sub': labels[g['status']],
                'href': '/mine-maal#goal-%s' % g['id'], 'icon': 'fa-bullseye',
            } for g in hits]
            if items:
                groups.append({'label': 'Mine mål', 'items': items})
        except Exception as e:
            logger.debug("search_api: goal search skipped: %s", e)
    return groups


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
    from auth_decorators import can, department_scope
    is_admin = can('platform.admin')

    groups.extend(_own_results(q, like, company_id))

    # --- Employees in the current company -------------------------------
    # S-1.5: only HR roles may look colleagues up. Department heads are limited
    # to their own department; plain employees get no employee results at all
    # (otherwise any employee could harvest colleagues' emails).
    can_search_people = bool(company_id) and can('hr.employees.view')
    if can_search_people:
        try:
            cur = _cursor()
            dept_clause = ""
            params = [company_id]
            if department_scope() is not None:
                dept_clause = (
                    " AND cu.department = (SELECT d.department FROM company_users d"
                    " WHERE d.user_id = %s AND d.company_id = %s LIMIT 1)"
                )
                params += [session.get('user_id'), company_id]
            params += [like, like, like]
            cur.execute(
                """SELECT cu.user_id, COALESCE(cu.full_name, u.username) AS name,
                          u.username, u.email, cu.department
                   FROM company_users cu JOIN users u ON cu.user_id = u.id
                   WHERE cu.company_id = %s AND cu.status = 'active'""" + dept_clause + """
                     AND (COALESCE(cu.full_name,'') LIKE %s OR u.username LIKE %s
                          OR COALESCE(u.email,'') LIKE %s)
                   ORDER BY name LIMIT 6""",
                tuple(params),
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
