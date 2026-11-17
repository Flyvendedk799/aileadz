"""Append-only HR/vendor turns; concurrent workers never replace a transcript."""
import ai_actions


def turns(sid, scope):
    username, company = ai_actions.identity()
    conn = ai_actions._db()
    if conn is None:
        with ai_actions._LOCK:
            rows = [dict(r) for r in ai_actions._MEMORY.values()
                    if (r['username'], r['company_key'], r['session_id'], r['scope'], r['kind']) ==
                    (username, company, sid, scope, 'transcript')]
    else:
        cur = conn.cursor()
        try:
            cur.execute('SELECT ' + ','.join(ai_actions._FIELDS) + ' FROM ai_actions WHERE username=%s AND company_key=%s AND session_id=%s AND scope=%s AND kind=%s ORDER BY created_at,id',
                        (username, company, sid, scope, 'transcript'))
            rows = [ai_actions._decode(r) for r in cur.fetchall()]
        finally:
            cur.close()
    return sorted(rows, key=lambda r: (r['created_at'], r['id']))


def append(sid, scope, user_text, assistant_text):
    operation = ai_actions.ACTIVE_OPERATION.get()
    key = ai_actions.create(sid, scope, 'transcript', user_text[:60],
                            {'messages': [{'role':'user', 'content':user_text},
                                          {'role':'assistant', 'content':assistant_text}]},
                            ttl=180 * 86400, action_id=operation + ':transcript' if operation else None)
    ai_actions.transition(key, ('pending',), 'complete')


def load(sid, scope):
    return [message for row in turns(sid, scope) for message in row['payload'].get('messages', [])]


def binding(sid, scope, *, create=False):
    if not sid:
        return False
    key = 'conversation:' + sid
    if create:
        ai_actions.create(sid, scope, 'conversation', scope, ttl=180 * 86400, action_id=key)
    row = ai_actions.get(key)
    if not row and scope == 'hr' and not create:
        # Old transcripts have no tenant column. Adopt only when the interaction
        # ledger proves exactly one owning company; never guess from the cookie.
        username, company = ai_actions.identity()
        conn = ai_actions._db()
        if username and company and conn is not None:
            cur = conn.cursor()
            try:
                cur.execute('SELECT DISTINCT company_id FROM hr_chatbot_interactions WHERE username=%s AND session_id=%s AND company_id IS NOT NULL', (username,sid))
                owners = {str(r['company_id'] if isinstance(r,dict) else r[0]) for r in cur.fetchall()}
                if owners == {company}:
                    ai_actions.create(sid, scope, 'conversation', scope, ttl=180 * 86400, action_id=key)
                    row = ai_actions.get(key)
            except Exception:
                conn.rollback()
            finally:
                cur.close()
    return bool(row and row['scope'] == scope and row['status'] != 'deleted')


def sessions(scope):
    username, company = ai_actions.identity()
    conn = ai_actions._db()
    if conn is None:
        rows = [r for r in ai_actions._MEMORY.values() if (r['username'],r['company_key'],r['scope'],r['kind']) == (username,company,scope,'transcript')]
    else:
        cur = conn.cursor()
        try:
            cur.execute('SELECT ' + ','.join(ai_actions._FIELDS) + ' FROM ai_actions WHERE username=%s AND company_key=%s AND scope=%s AND kind=%s ORDER BY created_at DESC LIMIT 500', (username,company,scope,'transcript'))
            rows = [ai_actions._decode(r) for r in cur.fetchall()]
        finally:
            cur.close()
    out = {}
    for r in sorted(rows,key=lambda r:r['created_at']):
        out[r['session_id']] = {'session_id':r['session_id'], 'title':r['name'], 'updated_at':str(r['updated_at'])}
    return list(reversed(list(out.values())))[:30]
