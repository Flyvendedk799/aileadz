"""CV parse-job store — durable across gunicorn workers.

The CV portal's parse flow is two HTTP requests: POST /api/cv/parse spawns the
(slow) extract+LLM parse in a background thread, then GET /api/cv/parse-stream
opens an SSE connection that polls for the result. Under gunicorn those two
requests can land on DIFFERENT worker processes. The old implementation kept the
result in a process-local module dict, so the thread on worker B wrote a result
the SSE poller on worker A could never see — the stream timed out at 60s on every
real multi-worker deploy, and orphaned entries leaked forever.

This mirrors ``app1/confirm_store``: results live in MySQL (``ai_cv_parse_jobs``)
so any worker can read them, with an in-process dict as a same-worker fast path /
no-DB fallback (tests, anonymous boots). Rows carry an ``expires_at`` and are
swept on write, so nothing leaks. Fully guarded — a DB failure degrades to the
in-process dict, never raises into the request.
"""
import json
import time
import threading

_STORE: dict = {}
_LOCK = threading.Lock()
_TTL_S = 300  # 5 minutes — a parse + review fits comfortably inside this

_TABLE_SQL = """CREATE TABLE IF NOT EXISTS ai_cv_parse_jobs (
    session_id VARCHAR(128) PRIMARY KEY,
    status VARCHAR(16) NOT NULL,
    result_json LONGTEXT,
    expires_at DOUBLE NOT NULL,
    INDEX idx_expires (expires_at)
) DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci"""

_table_ready = False


def _mysql():
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
        print(f"[cv_parse_store] table ensure failed (falling back to in-process): {e}")
        try:
            mysql.connection.rollback()
        except Exception:
            pass
        return False


def _cleanup_expired_locked():
    now = time.time()
    for k in [k for k, v in _STORE.items() if v["expires_at"] < now]:
        _STORE.pop(k, None)


def _db_sweep(mysql):
    try:
        cur = mysql.connection.cursor()
        cur.execute("DELETE FROM ai_cv_parse_jobs WHERE expires_at < %s", (time.time(),))
        mysql.connection.commit()
        cur.close()
    except Exception:
        try:
            mysql.connection.rollback()
        except Exception:
            pass


def start(session_id):
    """Mark a parse job as running (clears any previous result for the id)."""
    if not session_id:
        return
    expires_at = time.time() + _TTL_S
    with _LOCK:
        _cleanup_expired_locked()
        _STORE[session_id] = {"status": "running", "result": None, "expires_at": expires_at}
    mysql = _mysql()
    if mysql is not None and _ensure_table(mysql):
        try:
            cur = mysql.connection.cursor()
            cur.execute(
                "INSERT INTO ai_cv_parse_jobs (session_id, status, result_json, expires_at) "
                "VALUES (%s, 'running', NULL, %s) "
                "ON DUPLICATE KEY UPDATE status='running', result_json=NULL, expires_at=VALUES(expires_at)",
                (str(session_id), expires_at),
            )
            mysql.connection.commit()
            cur.close()
            _db_sweep(mysql)
        except Exception as e:
            print(f"[cv_parse_store] start DB write failed (in-process still valid): {e}")
            try:
                mysql.connection.rollback()
            except Exception:
                pass


def finish(session_id, payload):
    """Store the terminal result ({'proposal','hint'} or {'error',...})."""
    if not session_id:
        return
    expires_at = time.time() + _TTL_S
    entry = {"status": "done", "result": dict(payload or {}), "expires_at": expires_at}
    with _LOCK:
        _STORE[session_id] = entry
    mysql = _mysql()
    if mysql is not None and _ensure_table(mysql):
        try:
            cur = mysql.connection.cursor()
            cur.execute(
                "INSERT INTO ai_cv_parse_jobs (session_id, status, result_json, expires_at) "
                "VALUES (%s, 'done', %s, %s) "
                "ON DUPLICATE KEY UPDATE status='done', result_json=VALUES(result_json), expires_at=VALUES(expires_at)",
                (str(session_id), json.dumps(payload or {}, ensure_ascii=False, default=str), expires_at),
            )
            mysql.connection.commit()
            cur.close()
        except Exception as e:
            print(f"[cv_parse_store] finish DB write failed (in-process still valid): {e}")
            try:
                mysql.connection.rollback()
            except Exception:
                pass


def read(session_id):
    """Return the terminal result dict if the job is done, else None.

    Non-consuming (the SSE poller may read several times before the result
    lands); ``discard`` removes the row once the stream has delivered it.
    """
    if not session_id:
        return None
    mysql = _mysql()
    if mysql is not None and _ensure_table(mysql):
        try:
            cur = mysql.connection.cursor()
            cur.execute(
                "SELECT status, result_json, expires_at FROM ai_cv_parse_jobs WHERE session_id = %s",
                (str(session_id),),
            )
            row = cur.fetchone()
            cur.close()
            if row:
                if isinstance(row, dict):
                    status, result_json, expires_at = row["status"], row["result_json"], row["expires_at"]
                else:
                    status, result_json, expires_at = row
                if float(expires_at) >= time.time() and status == "done":
                    try:
                        return json.loads(result_json or "{}")
                    except Exception:
                        return {}
                return None
        except Exception as e:
            print(f"[cv_parse_store] read DB failed (falling back to in-process): {e}")
            try:
                mysql.connection.rollback()
            except Exception:
                pass
    # In-process fallback.
    with _LOCK:
        entry = _STORE.get(session_id)
        if not entry or entry["expires_at"] < time.time():
            return None
        if entry["status"] != "done":
            return None
        return entry["result"]


def discard(session_id):
    """Remove a finished job once the stream has delivered it."""
    if not session_id:
        return
    with _LOCK:
        _STORE.pop(session_id, None)
    mysql = _mysql()
    if mysql is not None and _ensure_table(mysql):
        try:
            cur = mysql.connection.cursor()
            cur.execute("DELETE FROM ai_cv_parse_jobs WHERE session_id = %s", (str(session_id),))
            mysql.connection.commit()
            cur.close()
        except Exception:
            try:
                mysql.connection.rollback()
            except Exception:
                pass


def clear_all():
    """Test helper — never call in production."""
    global _table_ready
    with _LOCK:
        _STORE.clear()
