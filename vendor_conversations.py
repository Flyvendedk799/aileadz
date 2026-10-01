"""Durable vendor-assistant conversations (N-5.4).

The vendor assistant kept its memory in a per-process dict, so a deploy, a second
worker or a new tab lost the conversation. Transcripts now live in the shared
``conversation_history`` table with ``mode = 'vendor'``; the active conversation per
vendor is in ``user_active_sessions``. The "username" is always ``vendor:<id>`` from
the SESSION - a session id alone never reaches another vendor's transcript - and
every call degrades to an empty result instead of raising into the chat turn.
"""

import json
import uuid

from flask import current_app

MODE = "vendor"
_MAX_STORED = 40


def owner(vendor_id):
    return "vendor:%s" % vendor_id


def _conn():
    return current_app.mysql.connection


def _rollback():
    try:
        _conn().rollback()
    except Exception:
        pass


def _clean(messages):
    out = []
    for m in messages or []:
        text = (m.get("content") or "").strip() if isinstance(m.get("content"), str) else ""
        if m.get("role") in ("user", "assistant") and text:
            out.append({"role": m["role"], "content": text})
    return out[-_MAX_STORED:]


def new_sid(vendor_id):
    return "vendor_%s_%s" % (vendor_id, uuid.uuid4())


def active_sid(who):
    try:
        cur = _conn().cursor()
        cur.execute("SELECT session_id FROM user_active_sessions WHERE username = %s AND mode = %s", (who, MODE))
        row = cur.fetchone()
        cur.close()
    except Exception:
        _rollback()
        return None
    if not row:
        return None
    return (row.get("session_id") if isinstance(row, dict) else row[0]) or None


def set_active(who, sid):
    try:
        cur = _conn().cursor()
        cur.execute("INSERT INTO user_active_sessions (username, mode, session_id) VALUES (%s, %s, %s) "
                    "ON DUPLICATE KEY UPDATE session_id = VALUES(session_id)", (who, MODE, sid))
        _conn().commit()
        cur.close()
    except Exception:
        _rollback()


def load(who, sid):
    """Stored messages for exactly this vendor's conversation, [] when none."""
    if not who or not sid:
        return []
    try:
        cur = _conn().cursor()
        cur.execute("SELECT messages FROM conversation_history WHERE username = %s AND session_id = %s AND mode = %s LIMIT 1",
                    (who, sid, MODE))
        row = cur.fetchone()
        cur.close()
    except Exception:
        _rollback()
        return []
    if not row:
        return []
    raw = row.get("messages") if isinstance(row, dict) else row[0]
    try:
        data = json.loads(raw) if raw else []
    except (TypeError, ValueError):
        return []
    return _clean(data if isinstance(data, list) else [])


def save(who, sid, messages):
    msgs = _clean(messages)
    if not who or not sid or not msgs:
        return False
    title = next((m["content"][:60] for m in msgs if m["role"] == "user"), "Ny samtale")
    payload = json.dumps(msgs, ensure_ascii=False)
    try:
        cur = _conn().cursor()
        cur.execute("SELECT id FROM conversation_history WHERE username = %s AND session_id = %s AND mode = %s",
                    (who, sid, MODE))
        row = cur.fetchone()
        if row:
            rid = row.get("id") if isinstance(row, dict) else row[0]
            cur.execute("UPDATE conversation_history SET messages = %s, title = %s WHERE id = %s", (payload, title, rid))
        else:
            cur.execute("INSERT INTO conversation_history (username, session_id, title, mode, messages) "
                        "VALUES (%s, %s, %s, %s, %s)", (who, sid, title, MODE, payload))
        _conn().commit()
        cur.close()
    except Exception:
        _rollback()
        return False
    set_active(who, sid)
    return True


def resolve_sid(flask_session, vendor_id):
    """The conversation this browser continues: the session pointer (only if it is
    this vendor's own id), else the vendor's last active one, else a new one."""
    who = owner(vendor_id)
    sid = flask_session.get("vendor_chat_session_id")
    if sid and sid.startswith("vendor_%s_" % vendor_id):
        return sid
    sid = active_sid(who)
    if sid and sid.startswith("vendor_%s_" % vendor_id):
        flask_session["vendor_chat_session_id"] = sid
        return sid
    sid = new_sid(vendor_id)
    flask_session["vendor_chat_session_id"] = sid
    return sid
