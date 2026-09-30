"""Single-use, expiring account tokens (password reset, invites).

Only a SHA-256 hash of the token is stored; the raw token is returned once and
goes into the emailed link. API (kept stable so Part A's S-2.4 can swap the
internals): ``create_token(kind, subject_id, ttl_minutes=60) -> raw`` and
``consume_token(kind, raw) -> subject_id | None``.
"""

from __future__ import annotations

import hashlib
import logging
import secrets

logger = logging.getLogger(__name__)


def _hash(raw: str) -> str:
    return hashlib.sha256((raw or "").encode("utf-8")).hexdigest()


def _conn():
    from flask import current_app
    return current_app.mysql.connection


def create_token(kind, subject_id, ttl_minutes=60):
    """Create a token; older unused tokens of the same kind+subject are revoked."""
    raw = secrets.token_urlsafe(32)
    conn = _conn()
    cur = conn.cursor()
    try:
        cur.execute(
            "UPDATE account_tokens SET used_at = NOW() WHERE kind = %s AND subject_id = %s AND used_at IS NULL",
            (kind, int(subject_id)),
        )
        cur.execute(
            "INSERT INTO account_tokens (kind, subject_id, token_hash, expires_at) "
            "VALUES (%s, %s, %s, DATE_ADD(NOW(), INTERVAL %s MINUTE))",
            (kind, int(subject_id), _hash(raw), int(ttl_minutes)),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        cur.close()
    return raw


def consume_token(kind, raw):
    """Return subject_id and burn the token, or None if unknown/used/expired."""
    if not raw:
        return None
    conn = _conn()
    cur = conn.cursor()
    try:
        cur.execute(
            "SELECT id, subject_id FROM account_tokens WHERE kind = %s AND token_hash = %s "
            "AND used_at IS NULL AND expires_at > NOW() LIMIT 1",
            (kind, _hash(raw)),
        )
        row = cur.fetchone()
        if not row:
            return None
        tid = row["id"] if isinstance(row, dict) else row[0]
        sid = row["subject_id"] if isinstance(row, dict) else row[1]
        cur.execute("UPDATE account_tokens SET used_at = NOW() WHERE id = %s AND used_at IS NULL", (tid,))
        won = cur.rowcount > 0
        conn.commit()
        return int(sid) if won else None
    except Exception as e:
        logger.warning("consume_token failed: %s", e)
        try:
            conn.rollback()
        except Exception:
            pass
        return None
    finally:
        cur.close()


def peek_token(kind, raw):
    """Validate without burning (to render a set-password form)."""
    if not raw:
        return None
    cur = _conn().cursor()
    try:
        cur.execute(
            "SELECT subject_id FROM account_tokens WHERE kind = %s AND token_hash = %s "
            "AND used_at IS NULL AND expires_at > NOW() LIMIT 1",
            (kind, _hash(raw)),
        )
        row = cur.fetchone()
        if not row:
            return None
        return int(row["subject_id"] if isinstance(row, dict) else row[0])
    finally:
        cur.close()
