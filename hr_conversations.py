"""Durable HR-assistant conversations (N-5.3).

The HR panel used to keep its memory in a per-process dict, so a deploy, a second
worker or a new tab lost the whole conversation. HR transcripts now live in the
same ``conversation_history`` table as the employee chat (``mode = 'hr'``), with
the active conversation per user in ``user_active_sessions``. They are tagged
``hr`` so the employee chat sidebar never lists them.

Everything here is scoped to ``username`` (the signed-in HR user) - a session id
alone never reaches another user's transcript - and every call degrades to an
empty result instead of raising into the chat turn.
"""
import json
import uuid

from flask import current_app

MODE = "hr"
_MAX_STORED = 60          # messages kept per conversation


def _conn():
    return current_app.mysql.connection


def _rollback():
    try:
        _conn().rollback()
    except Exception:
        pass


def new_sid(prefix="hr_"):
    return f"{prefix}{uuid.uuid4()}"


def _clean(messages):
    out = []
    for m in messages or []:
        role = m.get("role")
        text = (m.get("content") or "").strip() if isinstance(m.get("content"), str) else ""
        if role in ("user", "assistant") and text:
            out.append({"role": role, "content": text})
    return out[-_MAX_STORED:]


def _title(messages):
    for m in messages:
        if m["role"] == "user":
            t = m["content"].strip().replace("\n", " ")
            return (t[:60] + "…") if len(t) > 60 else t
    return "Ny samtale"


def active_sid(username, mode=MODE):
    if not username:
        return None
    try:
        cur = _conn().cursor()
        cur.execute("SELECT session_id FROM user_active_sessions WHERE username = %s AND mode = %s",
                    (username, mode))
        row = cur.fetchone()
        cur.close()
    except Exception:
        _rollback()
        return None
    if not row:
        return None
    return (row.get("session_id") if isinstance(row, dict) else row[0]) or None


def set_active(username, sid, mode=MODE):
    if not username or not sid:
        return False
    try:
        cur = _conn().cursor()
        cur.execute(
            "INSERT INTO user_active_sessions (username, mode, session_id) VALUES (%s, %s, %s) "
            "ON DUPLICATE KEY UPDATE session_id = VALUES(session_id)", (username, mode, sid))
        _conn().commit()
        cur.close()
        return True
    except Exception:
        _rollback()
        return False


def load(username, sid, mode=MODE):
    """Stored messages for exactly this user's conversation, [] when none."""
    if not username or not sid:
        return []
    try:
        cur = _conn().cursor()
        cur.execute("SELECT messages FROM conversation_history "
                    "WHERE username = %s AND session_id = %s AND mode = %s LIMIT 1", (username, sid, mode))
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


def save(username, sid, messages, mode=MODE):
    """Upsert the transcript. Returns True when stored."""
    msgs = _clean(messages)
    if not username or not sid or not msgs:
        return False
    payload = json.dumps(msgs, ensure_ascii=False)
    try:
        cur = _conn().cursor()
        cur.execute("SELECT id FROM conversation_history WHERE username = %s AND session_id = %s AND mode = %s",
                    (username, sid, mode))
        row = cur.fetchone()
        if row:
            rid = row.get("id") if isinstance(row, dict) else row[0]
            cur.execute("UPDATE conversation_history SET messages = %s, title = %s WHERE id = %s",
                        (payload, _title(msgs), rid))
        else:
            cur.execute("INSERT INTO conversation_history (username, session_id, title, mode, messages) "
                        "VALUES (%s, %s, %s, %s, %s)", (username, sid, _title(msgs), mode, payload))
        _conn().commit()
        cur.close()
    except Exception:
        _rollback()
        return False
    set_active(username, sid, mode)
    return True


def list_sessions(username, limit=30, mode=MODE):
    """Newest first: [{session_id, title, updated_at}]."""
    if not username:
        return []
    try:
        cur = _conn().cursor()
        cur.execute("SELECT session_id, title, updated_at FROM conversation_history "
                    "WHERE username = %s AND mode = %s ORDER BY updated_at DESC LIMIT %s",
                    (username, mode, int(limit)))
        rows = cur.fetchall() or []
        cur.close()
    except Exception:
        _rollback()
        return []
    out = []
    for r in rows:
        r = dict(r) if isinstance(r, dict) else {"session_id": r[0], "title": r[1], "updated_at": r[2]}
        out.append({"session_id": r["session_id"], "title": r.get("title") or "Ny samtale",
                    "updated_at": str(r.get("updated_at") or "")})
    return out


def open_session(username, sid, mode=MODE):
    """Make ``sid`` the active conversation if it is this user's. Returns its messages or None."""
    if not username or not sid:
        return None
    msgs = load(username, sid, mode)
    if not msgs:
        return None
    set_active(username, sid, mode)
    return msgs


def resolve_sid(flask_session, username, key="hr_chat_session_id", mode=MODE, prefix="hr_"):
    """The HR conversation this browser continues: the session pointer, else the
    user's last active one (new tab / new device / after a deploy), else a new one."""
    sid = flask_session.get(key)
    if sid and sid.startswith(prefix):
        # A pointer we minted ourselves is fine even before its first save.
        return sid
    sid = active_sid(username, mode)
    if sid:
        flask_session[key] = sid
        return sid
    sid = new_sid(prefix)
    flask_session[key] = sid
    return sid


def start_new(flask_session, username, key="hr_chat_session_id", mode=MODE, prefix="hr_"):
    sid = new_sid(prefix)
    flask_session[key] = sid
    set_active(username, sid, mode)
    return sid
