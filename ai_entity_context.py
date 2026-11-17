"""Resolve navigation references against the same actor/tenant boundary as pages."""
import json
import re
from flask import current_app, session


def order_focus(order_id, *, manager=False):
    import order_service
    import order_fulfillment
    ctx = order_service.OrderContext.from_session(source='ai_context')
    row = order_service.get_order(ctx, order_id)
    actors = order_service.actors_for(ctx, row) if row else set()
    if not row or not (actors & {'manager','admin'} if manager else 'owner' in actors):
        return None
    data = {k:row.get(k) for k in ('order_id','product_title','status','completion_status','completion_deadline')}
    data['booking'] = order_fulfillment.booking_for(order_id)
    conn = order_service._get_connection()
    cur = order_service._dict_cursor(conn)
    try:
        cur.execute("SELECT id,kind,status FROM course_order_changes WHERE order_id=%s AND status='pending'", (order_id,))
        data['pending_changes'] = cur.fetchall() or []
    finally:
        cur.close()
    return {'kind':'order', 'lines':[json.dumps(data, ensure_ascii=False, default=str)]}


def hr_context(raw):
    from auth_decorators import can, department_scope
    if not isinstance(raw, dict) or not can('hr.view'):
        return {}
    scope = department_scope()
    out = {}
    filters = raw.get('filters') if isinstance(raw.get('filters'),dict) else {}
    department = scope if scope is not None else str(filters.get('department') or '')[:100]
    if department:
        out['department'] = department
    try:
        days = int(filters.get('period_days',90))
        if 1 <= days <= 366:
            out['period_days'] = days
    except (TypeError, ValueError):
        pass
    path = str(raw.get('path') or '')[:200]
    match = re.search(r'/(employee|order|learning-paths)/([A-Za-z0-9_-]{1,50})(?:/|$)',path)
    if not match:
        return out
    kind, key = match.groups()
    if kind == 'order':
        item = order_focus(key, manager=True)
        if item:
            out['entity'] = item
        return out
    if not key.isdigit() or (kind == 'employee' and not can('hr.employees.view')):
        return out
    if kind == 'learning-paths' and not can('company.learning_paths'):
        return out
    import MySQLdb.cursors
    cur = current_app.mysql.connection.cursor(MySQLdb.cursors.DictCursor)
    try:
        if kind == 'employee':
            cur.execute("SELECT user_id,full_name,department FROM company_users WHERE company_id=%s AND user_id=%s AND status='active'", (session.get('company_id'),int(key)))
            row = cur.fetchone()
            if row and scope is not None and (not scope or row.get('department') != scope):
                row = None
        else:
            cur.execute('SELECT id,path_name,version FROM learning_paths WHERE company_id=%s AND id=%s', (session.get('company_id'),int(key)))
            row = cur.fetchone()
        if row:
            out['entity'] = {'kind':kind, **row}
    finally:
        cur.close()
    return out
