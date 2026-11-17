"""Support correlation using existing order, approval, notification and mail keys."""
import ai_actions


def references(value):
    refs = {'order_ids':[], 'approval_ids':[], 'run_ids':[]}
    def visit(item):
        if isinstance(item, dict):
            for k,v in item.items():
                if k in refs and isinstance(v,list):
                    for ref in v[:100]:
                        if isinstance(ref,(str,int)) and str(ref) not in refs[k]:
                            refs[k].append(str(ref))
                target = {'order_id':'order_ids','approval_id':'approval_ids','run_id':'run_ids'}.get(k)
                if target and isinstance(v,(str,int)) and str(v) not in refs[target]:
                    refs[target].append(str(v))
                elif isinstance(v,(dict,list)):
                    visit(v)
        elif isinstance(item,list):
            for child in item[:100]:
                visit(child)
    visit(value)
    return refs


def record(name, data, sid=None):
    parent = ai_actions.ACTIVE_OPERATION.get()
    if not parent:
        return
    row = ai_actions.get(parent)
    if not row:
        return
    key = ai_actions.create(sid or row['session_id'], row['scope'], 'trace', name,
                            {'parent':parent, **references(data)}, ttl=180 * 86400)
    ai_actions.transition(key, ('pending',), 'complete')


def load(sid):
    """Called only by the platform-admin diagnostics route. No mail bodies."""
    from flask import current_app
    import MySQLdb.cursors
    cur = current_app.mysql.connection.cursor(MySQLdb.cursors.DictCursor)
    out = {'operations':[], 'orders':[], 'approvals':[], 'mail':[], 'notifications':[], 'warnings':[]}
    try:
        cur.execute('SELECT ' + ','.join(ai_actions._FIELDS) + ' FROM ai_actions WHERE session_id=%s ORDER BY created_at LIMIT 300', (sid,))
        rows = [ai_actions._decode(r) for r in cur.fetchall()]
        out['operations'] = [{**ai_actions.public(r), 'references':references(r), 'parent':r['payload'].get('parent')} for r in rows if r['kind'] != 'transcript']
        order_ids = set()
        for row in rows:
            order_ids.update(references(row)['order_ids'])
            order_ids.update(row['payload'].get('order_ids', []))
        for order_id in sorted(order_ids)[:50]:
            queries = {
                'orders':('SELECT order_id,status,company_id FROM course_orders WHERE order_id=%s', (order_id,)),
                'approvals':('SELECT id,order_id,status FROM order_approvals WHERE order_id=%s', (order_id,)),
                'mail':('SELECT id,order_id,state,attempts,delivery_group FROM mail_outbox WHERE order_id=%s', (order_id,)),
                'notifications':("SELECT id,kind,action_url FROM notifications WHERE action_url IN (%s,%s,%s)", ('/min-ordre/' + order_id, '/hr/order/' + order_id + '/details', '/hr/order/' + order_id + '/details#udbytte')),
            }
            for name,(sql,args) in queries.items():
                try:
                    cur.execute(sql,args)
                    out[name].extend(cur.fetchall() or [])
                except Exception:
                    out['warnings'].append(name + ' unavailable')
        keys = [r['id'] for r in rows if r['kind'] == 'confirmation']
        for key in keys:
            cur.execute('SELECT id,state,attempts,delivery_group FROM mail_outbox WHERE delivery_group=%s', (key,))
            out['mail'].extend(cur.fetchall() or [])
    finally:
        cur.close()
    return out
