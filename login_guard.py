"""
Login brute-force protection: per-account lockout and per-IP throttling (S-2.2).

Policy (env-tunable):
  * ACCOUNT: LOGIN_MAX_FAILURES (5) failed attempts for the same account within
    LOGIN_WINDOW_SECONDS (900) locks that account for LOGIN_LOCKOUT_SECONDS (900).
  * IP: LOGIN_IP_MAX_FAILURES (30) failures from one address in the window
    throttles that address.
  * A successful login clears the account counter.

State lives in-process (fast, always available) and is mirrored best-effort
into a small MySQL table so a lockout holds across gunicorn workers and
restarts. A database problem never blocks a login: the in-process layer still
applies. Account keys are hashed, never stored as usernames.

Messages are deliberately identical for "unknown user" and "wrong password" so
the form cannot be used to enumerate accounts.
"""

import hashlib
import logging
import os
import threading
import time

logger = logging.getLogger(__name__)

LOGIN_MAX_FAILURES = int(os.environ.get("LOGIN_MAX_FAILURES", "5"))
LOGIN_WINDOW_SECONDS = int(os.environ.get("LOGIN_WINDOW_SECONDS", "900"))
LOGIN_LOCKOUT_SECONDS = int(os.environ.get("LOGIN_LOCKOUT_SECONDS", "900"))
LOGIN_IP_MAX_FAILURES = int(os.environ.get("LOGIN_IP_MAX_FAILURES", "30"))

LOCKED_MESSAGE = ("For mange mislykkede forsøg. Af sikkerhedshensyn er login midlertidigt låst. "
                  "Prøv igen om %d min.")

_lock = threading.Lock()
_state = {}   # key -> {"fails": [timestamps], "locked_until": float}
_TABLE_READY = False


def _key(scope, value):
    digest = hashlib.sha256(("%s|%s" % (scope, (value or "").strip().lower())).encode("utf-8")).hexdigest()
    return "%s:%s" % (scope, digest)


def _entry(key, now):
    e = _state.setdefault(key, {"fails": [], "locked_until": 0.0})
    e["fails"] = [t for t in e["fails"] if now - t <= LOGIN_WINDOW_SECONDS]
    return e


# --------------------------------------------------------------------------
# Optional MySQL mirror (best effort)
# --------------------------------------------------------------------------

def _db_conn():
    try:
        from flask import current_app
        mysql = getattr(current_app, "mysql", None)
        if mysql is None or current_app.config.get("TESTING"):
            return None
        return mysql.connection
    except Exception:
        return None


def _ensure_table(conn):
    global _TABLE_READY
    if _TABLE_READY:
        return True
    try:
        cur = conn.cursor()
        cur.execute(
            """CREATE TABLE IF NOT EXISTS auth_login_attempts (
                   key_hash VARCHAR(80) NOT NULL PRIMARY KEY,
                   failed_count INT NOT NULL DEFAULT 0,
                   first_failed_at BIGINT NOT NULL DEFAULT 0,
                   locked_until BIGINT NOT NULL DEFAULT 0
               ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4"""
        )
        conn.commit()
        cur.close()
        _TABLE_READY = True
    except Exception as exc:
        logger.debug("login_guard: table not available: %s", exc)
    return _TABLE_READY


def _db_locked_until(key):
    conn = _db_conn()
    if conn is None or not _ensure_table(conn):
        return 0
    try:
        cur = conn.cursor()
        cur.execute("SELECT locked_until FROM auth_login_attempts WHERE key_hash = %s", (key,))
        row = cur.fetchone()
        cur.close()
        if not row:
            return 0
        return int((row.get("locked_until") if isinstance(row, dict) else row[0]) or 0)
    except Exception:
        return 0


def _db_record(key, now, locked_until):
    conn = _db_conn()
    if conn is None or not _ensure_table(conn):
        return
    try:
        cur = conn.cursor()
        cur.execute(
            """INSERT INTO auth_login_attempts (key_hash, failed_count, first_failed_at, locked_until)
               VALUES (%s, 1, %s, %s)
               ON DUPLICATE KEY UPDATE
                 failed_count = IF(first_failed_at < %s, 1, failed_count + 1),
                 first_failed_at = IF(first_failed_at < %s, %s, first_failed_at),
                 locked_until = GREATEST(locked_until, %s)""",
            (key, int(now), int(locked_until), int(now - LOGIN_WINDOW_SECONDS),
             int(now - LOGIN_WINDOW_SECONDS), int(now), int(locked_until)),
        )
        conn.commit()
        cur.close()
    except Exception as exc:
        logger.debug("login_guard: db record skipped: %s", exc)


def _db_clear(key):
    conn = _db_conn()
    if conn is None or not _ensure_table(conn):
        return
    try:
        cur = conn.cursor()
        cur.execute("DELETE FROM auth_login_attempts WHERE key_hash = %s", (key,))
        conn.commit()
        cur.close()
    except Exception:
        pass


# --------------------------------------------------------------------------
# Public API
# --------------------------------------------------------------------------

def check(username, ip, now=None):
    """Return ``(allowed, retry_after_seconds)`` WITHOUT counting an attempt."""
    now = time.time() if now is None else now
    ak, ik = _key("acct", username), _key("ip", ip)
    with _lock:
        ae = _entry(ak, now)
        ie = _entry(ik, now)
        locked_until = ae["locked_until"]
        ip_blocked = len(ie["fails"]) >= LOGIN_IP_MAX_FAILURES
        ip_retry = int(LOGIN_WINDOW_SECONDS - (now - ie["fails"][0])) + 1 if ip_blocked else 0
    if locked_until <= now:
        locked_until = max(locked_until, _db_locked_until(ak))
    if locked_until > now:
        return False, max(1, int(locked_until - now) + 1)
    if ip_blocked:
        return False, max(1, ip_retry)
    return True, 0


def record_failure(username, ip, now=None):
    """Count a failed login. Returns True if this failure locked the account."""
    now = time.time() if now is None else now
    ak, ik = _key("acct", username), _key("ip", ip)
    locked = False
    with _lock:
        ae = _entry(ak, now)
        ie = _entry(ik, now)
        ae["fails"].append(now)
        ie["fails"].append(now)
        if len(ae["fails"]) >= LOGIN_MAX_FAILURES:
            ae["locked_until"] = now + LOGIN_LOCKOUT_SECONDS
            locked = True
        locked_until = ae["locked_until"]
    _db_record(ak, now, locked_until if locked else 0)
    return locked


def record_success(username, ip=None):
    ak = _key("acct", username)
    with _lock:
        _state.pop(ak, None)
    _db_clear(ak)


def reset():
    with _lock:
        _state.clear()


def locked_message(retry_after):
    minutes = max(1, (int(retry_after) + 59) // 60)
    return LOCKED_MESSAGE % minutes


def client_ip():
    """The client address. ``wsgi.py`` runs ProxyFix (one trusted hop), so
    ``remote_addr`` is already the real client behind the tunnel; a raw
    X-Forwarded-For header is attacker-controlled and deliberately NOT used."""
    try:
        from flask import request
        return request.remote_addr or ""
    except Exception:
        return ""
