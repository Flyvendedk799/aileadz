"""DB-authoritative conversation state for the employee AI (chat + profiler).

Why this exists
---------------
The old model kept ONE active transcript row per user (user_conversations,
PK=username) shared by both surfaces, a per-process CHAT_MEMORY dict as the
real source of truth, and a "latest conversation for this mode" fallback on
every cold start. Together that meant:

* "Ny samtale" deleted the active row — and the rolling summary on it — and the
  next /ask fell back to the PREVIOUS transcript, so a blank thread was secretly
  continued with old context;
* the chat and the profiler overwrote each other's active conversation, and one
  session id was shared by both tabs;
* a gunicorn worker holding stale CHAT_MEMORY overwrote newer turns saved by
  another worker.

Here every conversation is a conversation_history row keyed by
(username, session_id), with a revision counter for optimistic concurrency.
Which conversation is open is a per-surface pointer (user_active_sessions), the
session id lives per surface in the Flask session, and cross-session memory is
a per-surface digest (user_conversation_summaries) that no "new chat" touches.
"""
import json
import uuid

import MySQLdb.cursors
from flask import current_app

from app1.user_profile_db import _extract_title

SURFACES = ("chat", "profiler")
_MAX_DIGEST_CHARS = 2500


def surface_for_mode(mode):
    """Every mode maps to the one surface: the AI Profiler was merged into the
    assistant, so old ``profiler`` rows and pointers resume in the chat."""
    return "chat"


def _conn():
    return current_app.mysql.connection


def _rollback():
    try:
        _conn().rollback()
    except Exception:
        pass


# ── Session ids (one per surface) ────────────────────────────────────────────

def _mirror(flask_session, surface, sid):
    # Legacy readers (confirm tokens, feedback, admin log) still read
    # session["session_id"]; point it at the surface that was used last.
    flask_session["session_id"] = sid
    flask_session["session_surface"] = surface


def resolve_sid(flask_session, mode, *, username=None):
    """The session id for this surface, creating one when there is none.

    A legacy single ``session_id`` is adopted only when it isn't already
    claimed by the other surface and (for logged-in users) its stored
    conversation belongs to this surface — otherwise the profiler could inherit
    a chat thread, which is exactly the bug this module removes.
    """
    surface = surface_for_mode(mode)
    ids = dict(flask_session.get("session_ids") or {})
    sid = ids.get(surface)
    if not sid:
        legacy = flask_session.get("session_id")
        claimed = set(ids.values())
        if legacy and legacy not in claimed:
            legacy_surface = flask_session.get("session_surface")
            adopt = legacy_surface in (None, surface)
            if adopt and username and legacy_surface is None:
                row = load(username, legacy)
                adopt = row is None or row.get("mode") == surface
            if adopt:
                sid = legacy
        if not sid:
            sid = str(uuid.uuid4())
        ids[surface] = sid
        flask_session["session_ids"] = ids
    _mirror(flask_session, surface, sid)
    return sid


def adopt_sid(flask_session, mode, sid):
    """Make ``sid`` the open conversation for this surface (resume / restore)."""
    surface = surface_for_mode(mode)
    ids = dict(flask_session.get("session_ids") or {})
    ids[surface] = sid
    flask_session["session_ids"] = ids
    _mirror(flask_session, surface, sid)
    return sid


def start_new_session(flask_session, mode):
    return adopt_sid(flask_session, mode, str(uuid.uuid4()))


def current_sid(flask_session, mode):
    return (flask_session.get("session_ids") or {}).get(surface_for_mode(mode))


def all_session_ids(flask_session):
    """Every session id this browser session holds (confirm tokens are session-bound)."""
    out = []
    for sid in list((flask_session.get("session_ids") or {}).values()) + [flask_session.get("session_id")]:
        if sid and sid not in out:
            out.append(sid)
    return out


# ── Active-session pointers ──────────────────────────────────────────────────

def set_active(username, mode, sid):
    if not username or not sid:
        return False
    try:
        cur = _conn().cursor()
        cur.execute(
            "INSERT INTO user_active_sessions (username, mode, session_id) VALUES (%s, %s, %s) "
            "ON DUPLICATE KEY UPDATE session_id = VALUES(session_id)",
            (username, surface_for_mode(mode), sid),
        )
        _conn().commit()
        cur.close()
        return True
    except Exception as exc:
        print(f"[ConversationState] set_active: {exc}")
        _rollback()
        return False


def get_active(username, mode):
    if not username:
        return None
    try:
        cur = _conn().cursor(MySQLdb.cursors.DictCursor)
        cur.execute(
            "SELECT session_id FROM user_active_sessions WHERE username = %s AND mode = %s",
            (username, surface_for_mode(mode)),
        )
        row = cur.fetchone()
        cur.close()
        return (row or {}).get("session_id") or None
    except Exception as exc:
        print(f"[ConversationState] get_active: {exc}")
        _rollback()
        return None


# ── Transcripts ──────────────────────────────────────────────────────────────

def _decode_messages(raw):
    if not raw:
        return []
    try:
        msgs = json.loads(raw) if isinstance(raw, (str, bytes)) else raw
        return msgs if isinstance(msgs, list) else []
    except (ValueError, TypeError):
        return []


def _decode_state(raw):
    if not raw:
        return {}
    try:
        state = json.loads(raw) if isinstance(raw, (str, bytes)) else raw
        return state if isinstance(state, dict) else {}
    except (ValueError, TypeError):
        return {}


def load(username, sid):
    """The stored conversation for exactly this (username, session_id), or None.

    Never falls back to another conversation: a fresh session has no row, and
    must start empty."""
    if not username or not sid:
        return None
    try:
        cur = _conn().cursor(MySQLdb.cursors.DictCursor)
        cur.execute(
            "SELECT id, session_id, title, mode, messages, rev, summary, summary_msg_count, "
            "state_json, updated_at FROM conversation_history "
            "WHERE username = %s AND session_id = %s ORDER BY updated_at DESC LIMIT 1",
            (username, sid),
        )
        row = cur.fetchone()
        cur.close()
    except Exception as exc:
        print(f"[ConversationState] load: {exc}")
        _rollback()
        return None
    if not row:
        return None
    return {
        "id": row.get("id"),
        "session_id": row.get("session_id"),
        "title": row.get("title"),
        "mode": surface_for_mode(row.get("mode")),
        "messages": _decode_messages(row.get("messages")),
        "rev": int(row.get("rev") or 0),
        "summary": row.get("summary") or "",
        "summary_msg_count": int(row.get("summary_msg_count") or 0),
        "state": _decode_state(row.get("state_json")),
        "updated_at": row.get("updated_at"),
    }


def current_rev(username, sid):
    """Stored revision for the conversation, or None when it has no row yet."""
    if not username or not sid:
        return None
    try:
        cur = _conn().cursor(MySQLdb.cursors.DictCursor)
        cur.execute(
            "SELECT rev FROM conversation_history WHERE username = %s AND session_id = %s "
            "ORDER BY updated_at DESC LIMIT 1",
            (username, sid),
        )
        row = cur.fetchone()
        cur.close()
    except Exception as exc:
        print(f"[ConversationState] current_rev: {exc}")
        _rollback()
        return None
    return int(row.get("rev") or 0) if row else None


def _persistable(messages):
    return [m for m in (messages or []) if m.get("role") in ("user", "assistant") and m.get("content")]


def _same_turn(a, b):
    return a.get("role") == b.get("role") and (a.get("content") or "").strip() == (b.get("content") or "").strip()


def merge_transcripts(stored, ours):
    """Append the turns ``ours`` added on top of ``stored``.

    Used when another worker saved in between: the stored transcript wins and
    this turn's new user/assistant messages are appended if they aren't there.
    """
    stored = _persistable(stored)
    ours = _persistable(ours)
    # The common prefix is what both sides already agree on.
    prefix = 0
    while prefix < min(len(stored), len(ours)) and _same_turn(stored[prefix], ours[prefix]):
        prefix += 1
    tail = ours[prefix:]
    merged = list(stored)
    for msg in tail[-2:]:
        if not merged or not _same_turn(merged[-1], msg):
            merged.append(msg)
    return merged


def save_turn(username, sid, mode, messages, *, expected_rev=None, state=None):
    """Persist the transcript for (username, sid) with optimistic concurrency.

    Returns {"rev", "conflict", "messages"}; ``messages`` is what is now stored
    (the merged transcript after a conflict) so the caller can refresh its
    in-process cache. Raises nothing — returns rev None on failure.
    """
    surface = surface_for_mode(mode)
    payload_msgs = _persistable(messages)
    result = {"rev": None, "conflict": False, "messages": payload_msgs}
    if not username or not sid or not payload_msgs:
        return result
    state_json = json.dumps(state, ensure_ascii=False) if state is not None else None
    try:
        cur = _conn().cursor(MySQLdb.cursors.DictCursor)
        for _attempt in range(2):
            cur.execute(
                "SELECT id, rev, messages FROM conversation_history "
                "WHERE username = %s AND session_id = %s ORDER BY updated_at DESC LIMIT 1",
                (username, sid),
            )
            row = cur.fetchone()
            title = _extract_title(payload_msgs, mode=surface)
            if not row:
                cur.execute(
                    "INSERT INTO conversation_history (username, session_id, title, mode, messages, rev, state_json) "
                    "VALUES (%s, %s, %s, %s, %s, 1, %s)",
                    (username, sid, title, surface, json.dumps(payload_msgs, ensure_ascii=False), state_json),
                )
                result["rev"] = 1
                break
            stored_rev = int(row.get("rev") or 0)
            if expected_rev is not None and stored_rev != int(expected_rev):
                payload_msgs = merge_transcripts(_decode_messages(row.get("messages")), payload_msgs)
                result["conflict"] = True
                title = _extract_title(payload_msgs, mode=surface)
            sets = "messages = %s, title = %s, mode = %s, rev = rev + 1"
            params = [json.dumps(payload_msgs, ensure_ascii=False), title, surface]
            if state_json is not None:
                sets += ", state_json = %s"
                params.append(state_json)
            cur.execute(
                f"UPDATE conversation_history SET {sets} WHERE id = %s AND rev = %s",
                tuple(params + [row["id"], stored_rev]),
            )
            if cur.rowcount:
                result["rev"] = stored_rev + 1
                break
            # Lost a race between SELECT and UPDATE: re-read and merge once.
            expected_rev = -1
        _conn().commit()
        cur.close()
    except Exception as exc:
        print(f"[ConversationState] save_turn: {exc}")
        _rollback()
        return result
    result["messages"] = payload_msgs
    if result["rev"] is not None:
        set_active(username, surface, sid)
    return result


def update_state(username, sid, patch):
    """Merge ``patch`` into the conversation's state_json (no revision bump)."""
    if not username or not sid or not patch:
        return False
    try:
        cur = _conn().cursor(MySQLdb.cursors.DictCursor)
        cur.execute(
            "SELECT id, state_json FROM conversation_history WHERE username = %s AND session_id = %s "
            "ORDER BY updated_at DESC LIMIT 1",
            (username, sid),
        )
        row = cur.fetchone()
        if not row:
            cur.close()
            return False
        state = _decode_state(row.get("state_json"))
        state.update(patch)
        cur.execute("UPDATE conversation_history SET state_json = %s WHERE id = %s",
                    (json.dumps(state, ensure_ascii=False), row["id"]))
        _conn().commit()
        cur.close()
        return True
    except Exception as exc:
        print(f"[ConversationState] update_state: {exc}")
        _rollback()
        return False


def save_session_summary(username, sid, summary, msg_count):
    if not username or not sid or not (summary or "").strip():
        return False
    try:
        cur = _conn().cursor()
        cur.execute(
            "UPDATE conversation_history SET summary = %s, summary_msg_count = %s "
            "WHERE username = %s AND session_id = %s",
            (summary.strip()[:4000], int(msg_count or 0), username, sid),
        )
        _conn().commit()
        cur.close()
        return True
    except Exception as exc:
        print(f"[ConversationState] save_session_summary: {exc}")
        _rollback()
        return False


def latest_undigested(username, mode, *, exclude_sid=None):
    """Most recent conversation of this surface whose digest is behind its transcript."""
    try:
        cur = _conn().cursor(MySQLdb.cursors.DictCursor)
        cur.execute(
            "SELECT session_id, messages, summary_msg_count FROM conversation_history "
            "WHERE username = %s AND mode = %s AND session_id <> %s "
            "ORDER BY updated_at DESC LIMIT 1",
            (username, surface_for_mode(mode), exclude_sid or ""),
        )
        row = cur.fetchone()
        cur.close()
    except Exception as exc:
        print(f"[ConversationState] latest_undigested: {exc}")
        _rollback()
        return None
    if not row:
        return None
    msgs = _persistable(_decode_messages(row.get("messages")))
    if len(msgs) < 2 or int(row.get("summary_msg_count") or 0) >= len(msgs):
        return None
    return {"session_id": row["session_id"], "messages": msgs}


# ── Per-surface digests (cross-session memory) ───────────────────────────────

def load_mode_summary(username, mode):
    if not username:
        return ""
    try:
        cur = _conn().cursor(MySQLdb.cursors.DictCursor)
        cur.execute(
            "SELECT summary FROM user_conversation_summaries WHERE username = %s AND mode = %s",
            (username, surface_for_mode(mode)),
        )
        row = cur.fetchone()
        cur.close()
        return ((row or {}).get("summary") or "").strip()
    except Exception as exc:
        print(f"[ConversationState] load_mode_summary: {exc}")
        _rollback()
        return ""


def save_mode_summary(username, mode, summary, source_session_id=None):
    text = (summary or "").strip()
    if not username or not text:
        return False
    if len(text) > _MAX_DIGEST_CHARS:
        text = text[-_MAX_DIGEST_CHARS:]
    try:
        cur = _conn().cursor()
        cur.execute(
            "INSERT INTO user_conversation_summaries (username, mode, summary, source_session_id) "
            "VALUES (%s, %s, %s, %s) ON DUPLICATE KEY UPDATE summary = VALUES(summary), "
            "source_session_id = VALUES(source_session_id)",
            (username, surface_for_mode(mode), text, source_session_id),
        )
        _conn().commit()
        cur.close()
        return True
    except Exception as exc:
        print(f"[ConversationState] save_mode_summary: {exc}")
        _rollback()
        return False


def digest_session(username, sid, mode, messages):
    """Fold one session into the surface digest and the semantic index.

    Synchronous; callers that must not block run it via digest_session_async.
    """
    msgs = _persistable(messages)
    if not username or not sid or len(msgs) < 2:
        return None
    try:
        from ai_context import summarize_session
        previous = load_mode_summary(username, mode)
        session_summary = summarize_session(msgs, surface_for_mode(mode))
        if not session_summary:
            return None
        save_session_summary(username, sid, session_summary, len(msgs))
        merged = summarize_session(msgs, surface_for_mode(mode), previous_digest=previous,
                                   session_summary=session_summary)
        save_mode_summary(username, mode, merged or session_summary, source_session_id=sid)
        try:
            from app1 import user_knowledge
            user_knowledge.index_conversation_summary(username, sid, surface_for_mode(mode), session_summary)
        except Exception:
            pass
        return session_summary
    except Exception as exc:
        print(f"[ConversationState] digest_session: {exc}")
        _rollback()
        return None


def digest_session_async(username, sid, mode, messages):
    """Run digest_session in a daemon thread with its own app context.

    Best-effort: a worker recycle can kill it, and the next new session of the
    same surface catches up via latest_undigested()."""
    try:
        import threading

        app = current_app._get_current_object()
        msgs = list(_persistable(messages))

        def _run():
            with app.app_context():
                try:
                    digest_session(username, sid, mode, msgs)
                finally:
                    try:
                        from db_compat import close_flask_mysql_connection
                        close_flask_mysql_connection()
                    except Exception:
                        pass

        threading.Thread(target=_run, name="conv-digest", daemon=True).start()
        return True
    except Exception as exc:
        print(f"[ConversationState] digest_session_async: {exc}")
        return False
