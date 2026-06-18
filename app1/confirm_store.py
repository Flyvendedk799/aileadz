"""Pending-confirmation token store.

Holds tool-call args server-side for a short TTL so the client only receives an
opaque token — raw arguments never travel to the browser. Tokens are bound to
the originating session; a token presented by a different session is treated as
unknown (returns None from pop_pending).

Multi-worker correctness (the reason this is DB-backed):
    Under gunicorn the app runs several worker processes. A pure in-process
    dict means a token minted by worker A is invisible to worker B, so the
    confirm round-trip lands on the wrong worker, pop_pending returns None, and
    the highest-blast-radius mutations (orders, company email) are silently
    dropped while the UI reports success. We persist tokens in MySQL
    (`ai_confirm_tokens`) so any worker can resolve them, and keep the
    in-process dict as a same-worker write-through cache + a graceful fallback
    for environments without a database (tests, anonymous/no-DB boots).
"""
import secrets
import time
import threading

_STORE: dict = {}
_LOCK = threading.Lock()
_TTL_S = 600  # 10 minutes

_TABLE_SQL = """CREATE TABLE IF NOT EXISTS ai_confirm_tokens (
    token VARCHAR(64) PRIMARY KEY,
    session_id VARCHAR(128) NOT NULL,
    scope VARCHAR(32) NOT NULL,
    tool_name VARCHAR(128) NOT NULL,
    args_json LONGTEXT,
    expires_at DOUBLE NOT NULL,
    INDEX idx_expires (expires_at)
) DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci"""

_table_ready = False


def _mysql():
    """Return the live MySQL handle, or None when no DB is available.

    Guarded so tests / anonymous / no-DB boots fall back to the in-process
    dict without raising — confirm tokens still work within a single worker.
    """
    try:
        from flask import current_app, has_app_context
        if not has_app_context():
            return None
        mysql = getattr(current_app, "mysql", None)
        if mysql is None:
            return None
        try:
            from db_compat import refresh_flask_mysql_connection
            refresh_flask_mysql_connection(mysql)
        except Exception:
            pass
        return mysql
    except Exception:
        return None


def _ensure_table(mysql):
    global _table_ready
    if _table_ready:
        return True
    try:
        cur = mysql.connection.cursor()
        cur.execute(_TABLE_SQL)
        mysql.connection.commit()
        cur.close()
        _table_ready = True
        return True
    except Exception as e:
        print(f"[confirm_store] table ensure failed (falling back to in-process): {e}")
        try:
            mysql.connection.rollback()
        except Exception:
            pass
        return False


def _cleanup_expired():
    """Remove expired in-process entries. Must be called while holding _LOCK."""
    now = time.time()
    for k in [k for k, v in _STORE.items() if v["expires_at"] < now]:
        _STORE.pop(k, None)


def store_pending(session_id: str, scope: str, tool_name: str, args: dict) -> str:
    """Store a pending confirm and return an opaque 32-hex-char token.

    Writes through to both the DB (cross-worker) and the in-process dict
    (same-worker fast path / fallback). A DB failure never blocks issuing the
    token — the in-process copy keeps single-worker deployments working.
    """
    token = secrets.token_hex(16)
    expires_at = time.time() + _TTL_S
    entry = {
        "session_id": session_id,
        "scope": scope,
        "tool_name": tool_name,
        "args": dict(args or {}),
        "expires_at": expires_at,
    }
    with _LOCK:
        _cleanup_expired()
        _STORE[token] = entry

    mysql = _mysql()
    if mysql is not None and _ensure_table(mysql):
        try:
            import json
            cur = mysql.connection.cursor()
            cur.execute(
                "INSERT INTO ai_confirm_tokens (token, session_id, scope, tool_name, args_json, expires_at) "
                "VALUES (%s, %s, %s, %s, %s, %s)",
                (token, str(session_id), str(scope), str(tool_name),
                 json.dumps(args or {}, ensure_ascii=False, default=str), expires_at),
            )
            mysql.connection.commit()
            cur.close()
        except Exception as e:
            print(f"[confirm_store] DB write failed (in-process token still valid): {e}")
            try:
                mysql.connection.rollback()
            except Exception:
                pass
    return token


def pop_pending(session_id: str, token: str):
    """Consume and return a pending confirmation entry, or None.

    Returns None when the token is unknown, expired, or was issued for a
    different session_id (prevents token-guessing / session-fixation). The
    entry is consumed on the first successful pop — a double-confirm receives
    None and the confirm route treats it as already_confirmed.

    Single authoritative consume point. When a DB is available, the DB row is
    the ONLY consume point (an atomic, session-checked, rowcount-gated DELETE),
    so two confirms of the same token — even on different gunicorn workers —
    can never both win: exactly one DELETE returns rowcount==1. The in-process
    dict is used ONLY when no DB is available (tests / no-DB boots), where there
    is a single worker and no cross-worker race. This closes the
    in-process-A-vs-DB-B double-execute window.
    """
    if not token:
        return None
    mysql = _mysql()
    if mysql is not None and _ensure_table(mysql):
        return _db_pop(mysql, session_id, token)
    # No DB: single-worker in-process fallback.
    with _LOCK:
        entry = _STORE.get(token)
        if entry is None:
            return None
        if entry["session_id"] != session_id:
            return None  # wrong session: do NOT consume the owner's entry
        if entry["expires_at"] < time.time():
            _STORE.pop(token, None)
            return None
        _STORE.pop(token, None)
        return entry


def _db_pop(mysql, session_id, token):
    """Atomic DB consume: SELECT → verify session+expiry (BEFORE delete, so a
    mismatched session never destroys the owner's row) → DELETE gated on
    rowcount (so only one concurrent caller wins). Returns the entry or None."""
    try:
        import json
        cur = mysql.connection.cursor()
        cur.execute(
            "SELECT session_id, scope, tool_name, args_json, expires_at "
            "FROM ai_confirm_tokens WHERE token = %s",
            (token,),
        )
        row = cur.fetchone()
        if not row:
            cur.close()
            return None
        if isinstance(row, dict):
            sess, scope, tool_name, args_json, expires_at = (
                row["session_id"], row["scope"], row["tool_name"],
                row["args_json"], row["expires_at"],
            )
        else:
            sess, scope, tool_name, args_json, expires_at = row
        # Session check FIRST — a token presented by a different session must not
        # delete (and thereby grief) the legitimate owner's pending row.
        if str(sess) != str(session_id):
            cur.close()
            return None
        # Atomic consume: only the caller whose DELETE affects a row wins.
        cur.execute("DELETE FROM ai_confirm_tokens WHERE token = %s", (token,))
        deleted = cur.rowcount
        mysql.connection.commit()
        cur.close()
        # Tidy the local twin (best-effort) now that it's consumed.
        with _LOCK:
            _STORE.pop(token, None)
        if not deleted:
            return None  # a concurrent caller already consumed it
        if float(expires_at) < time.time():
            return None
        try:
            args = json.loads(args_json or "{}")
        except Exception:
            args = {}
        return {
            "session_id": str(sess), "scope": scope, "tool_name": tool_name,
            "args": args, "expires_at": float(expires_at),
        }
    except Exception as e:
        print(f"[confirm_store] DB pop failed: {e}")
        try:
            mysql.connection.rollback()
        except Exception:
            pass
        return None


def pending_count() -> int:
    """Return current in-process pending count (test helper)."""
    with _LOCK:
        return len(_STORE)


def clear_all():
    """Clear all in-process pending entries (test helper — never call in production)."""
    with _LOCK:
        _STORE.clear()
