"""Durable, actor- and tenant-bound AI operations, proposals and downloadable results.

The database is authoritative. A running operation is never replayed after an
uncertain outcome. In-memory storage exists only for the offline sandbox.
"""
from __future__ import annotations

import contextvars
import hashlib
import json
import os
import secrets
import threading
import time

from flask import current_app, has_request_context, has_app_context, session

ACTIVE_OPERATION = contextvars.ContextVar('ai_operation', default=None)
_MEMORY = {}
_LOCK = threading.RLock()
_FIELDS = ('id', 'username', 'company_key', 'session_id', 'scope', 'kind', 'name',
           'status', 'payload_json', 'receipt_json', 'expires_at', 'created_at', 'updated_at')
TERMINAL = {'consumed', 'failed', 'partial', 'rejected', 'cancelled', 'expired', 'uncertain', 'complete'}


def identity():
    if not has_request_context():
        return '', ''
    if session.get('user_type') == 'vendor' and session.get('vendor_id'):
        return 'vendor:%s' % session['vendor_id'], ''
    return str(session.get('user') or ''), str(session.get('company_id') or '')


def _db():
    mysql = getattr(current_app, 'mysql', None) if has_app_context() else None
    if mysql is None:
        if os.getenv('SANDBOX') == '1':
            return None
        raise RuntimeError('Handlingen kunne ikke gemmes. Prøv igen senere.')
    try:
        cur = mysql.connection.cursor()
        try:
            cur.execute('SELECT id FROM ai_actions LIMIT 1')
            cur.fetchall()
        except Exception:
            mysql.connection.rollback()
            from schema_registry import AI_ACTIONS_DDL
            cur.execute(AI_ACTIONS_DDL)
            mysql.connection.commit()
        finally:
            cur.close()
        return mysql.connection
    except Exception:
        if os.getenv('SANDBOX') == '1':
            return None
        raise


def _decode(row):
    if row is None:
        return None
    row = dict(row) if isinstance(row, dict) else dict(zip(_FIELDS, row))
    for field in ('payload_json', 'receipt_json'):
        raw = row.pop(field, None)
        row[field.replace('_json', '')] = json.loads(raw) if raw else {}
    return row


def create(sid, scope, kind, name, payload=None, *, ttl=600, action_id=None, owner=None):
    username, company = owner or identity()
    key = action_id or 'act_' + secrets.token_hex(16)
    now = time.time()
    values = (key, str(username), str(company), str(sid or '')[:128], str(scope)[:32], str(kind)[:32], str(name or '')[:128], 'pending',
              json.dumps(payload or {}, ensure_ascii=False, default=str), '{}', now + ttl, now, now)
    conn = _db()
    if conn is None:
        with _LOCK:
            _MEMORY.setdefault(key, _decode(values))
    else:
        cur = conn.cursor()
        try:
            cur.execute('INSERT IGNORE INTO ai_actions (' + ','.join(_FIELDS) + ') VALUES (' + ','.join(['%s'] * len(values)) + ')', values)
            conn.commit()
        finally:
            cur.close()
    return key


def get(key, *, owner=None, internal=False):
    conn = _db()
    if conn is None:
        with _LOCK:
            row = json.loads(json.dumps(_MEMORY.get(key))) if key in _MEMORY else None
    else:
        cur = conn.cursor()
        try:
            cur.execute('SELECT ' + ','.join(_FIELDS) + ' FROM ai_actions WHERE id=%s', (key,))
            row = _decode(cur.fetchone())
        finally:
            cur.close()
    if row and not internal and (row['username'], row['company_key']) != (owner or identity()):
        return None
    if row and not internal and row['scope'] == 'hr':
        from auth_decorators import can
        if not can('hr.view'):
            return None
    return row


def transition(key, expected, status, *, receipt=None, internal=False):
    """Compare-and-set; exactly one worker can claim an operation."""
    row = get(key, internal=internal)
    if not row or row['status'] not in expected:
        return False
    encoded = json.dumps(receipt if receipt is not None else row['receipt'], ensure_ascii=False, default=str)
    expires_at = time.time() + 240 if status == 'running' else row['expires_at']
    conn = _db()
    if conn is None:
        with _LOCK:
            if _MEMORY[key]['status'] not in expected:
                return False
            _MEMORY[key].update(status=status, receipt=json.loads(encoded), updated_at=time.time(), expires_at=expires_at)
            return True
    cur = conn.cursor()
    try:
        cur.execute('UPDATE ai_actions SET status=%s,receipt_json=%s,updated_at=%s,expires_at=%s WHERE id=%s AND status IN (' + ','.join(['%s'] * len(expected)) + ')',
                    (status, encoded, time.time(), expires_at, key, *expected))
        changed = cur.rowcount == 1
        conn.commit()
        return changed
    finally:
        cur.close()


def inspect(key):
    row = get(key)
    if row and row['expires_at'] < time.time() and row['status'] in ('pending', 'running'):
        transition(key, (row['status'],), 'expired' if row['status'] == 'pending' else 'uncertain')
        row = get(key)
    return row


def claim(key):
    row = inspect(key)
    if row and transition(key, ('pending',), 'running'):
        return row
    return None


def finish(key, receipt):
    from tool_confirm import action_status
    status = action_status(receipt)
    transition(key, ('running', 'uncertain'), status, receipt=receipt)
    return status


def list_actions(*, sid=None, limit=50):
    username, company = identity()
    if not username:
        return []
    conn = _db()
    if conn is None:
        with _LOCK:
            rows = [dict(r) for r in _MEMORY.values() if (r['username'], r['company_key']) == (username, company)]
        rows = sorted(rows, key=lambda r: r['created_at'], reverse=True)
    else:
        cur = conn.cursor()
        try:
            sql = 'SELECT ' + ','.join(_FIELDS) + " FROM ai_actions WHERE username=%s AND company_key=%s AND kind IN ('confirmation','artifact','turn')"
            args = [username, company]
            if sid:
                sql += ' AND session_id=%s'
                args.append(sid)
            sql += " ORDER BY CASE WHEN status='pending' THEN 0 WHEN status='running' THEN 1 ELSE 2 END, created_at DESC LIMIT %s"
            args.append(min(int(limit), 100))
            cur.execute(sql, tuple(args))
            rows = [_decode(r) for r in cur.fetchall()]
        finally:
            cur.close()
    visible = [public(inspect(r['id'])) for r in rows if (not sid or r['session_id'] == sid) and r['kind'] in ('confirmation', 'artifact', 'turn')]
    return [item for item in visible if item][:limit]


def public(row):
    if not row:
        return None
    payload = row['payload']
    return {'id': row['id'], 'session_id': row['session_id'], 'scope': row['scope'],
            'kind': row['kind'], 'name': row['name'], 'status': row['status'],
            'expires_at': row['expires_at'], 'created_at': row['created_at'],
            'card': payload.get('card', {}), 'receipt': {k: v for k, v in row['receipt'].items() if k != 'stream'},
            'status_url': '/app1/actions/' + row['id']}


def cancellation_checkpoint():
    key = ACTIVE_OPERATION.get()
    if key:
        row = inspect(key)
        if not row or row['status'] != 'running':
            raise RuntimeError('Handlingen er stoppet. Kontroller status før du prøver igen.')


def turn_key(request_id, scope):
    username, company = identity()
    return 'run_' + hashlib.sha256(json.dumps([username, company, scope, request_id]).encode()).hexdigest()[:40]


def artifact(sid, scope, data):
    key = create(sid, scope, 'artifact', data.get('filename') or 'download',
                 {'card': {'summary_da': data.get('message_da') or 'Kalenderfil'},
                  'content': data.get('content') or data.get('ics') or '',
                  'filename': data.get('filename') or 'kursus.ics',
                  'mime_type': data.get('mime_type') or 'text/calendar'}, ttl=180 * 86400)
    transition(key, ('pending',), 'complete')
    return {'type': 'ui_action', 'action': 'navigate', 'target': '/app1/actions/' + key + '/download',
            'label': 'Hent kalenderfil', 'new_tab': False}


def bind_session(sid):
    key = ACTIVE_OPERATION.get()
    row = get(key) if key else None
    if not row or row['session_id'] == sid:
        return
    conn = _db()
    if conn is None:
        with _LOCK:
            _MEMORY[key]['session_id'] = sid
    else:
        cur = conn.cursor()
        try:
            cur.execute('UPDATE ai_actions SET session_id=%s WHERE id=%s', (sid,key))
            conn.commit()
        finally:
            cur.close()
