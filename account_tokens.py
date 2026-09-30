"""Single-use, expiring account tokens (password reset, invites) - a thin seam.

The canonical token store is Part A's ``password_tokens`` (hash-only, atomic
consume). When that module is importable every call below delegates to it; until
then a small self-contained implementation (table ``account_tokens``, SHA-256 of
the token only) keeps the platform screens working. The API is stable::

    create_token(kind, subject_id, ttl_minutes=60) -> raw token
    consume_token(kind, raw)                       -> subject_id | None
    peek_token(kind, raw)                          -> subject_id | None

``kind`` is ``"<account_type>_<purpose>"``: ``user_reset``, ``user_invite``,
``vendor_reset``, ``vendor_invite``.
"""

from __future__ import annotations

import hashlib
import logging
import secrets

logger = logging.getLogger(__name__)


def _split(kind):
    account_type, _, purpose = (kind or "").partition("_")
    return account_type, (purpose or "reset")


def _conn():
    from flask import current_app
    return current_app.mysql.connection


def _delegate():
    try:
        import password_tokens  # Part A (S-2.4)
        return password_tokens
    except Exception:
        return None


def _hash(raw: str) -> str:
    return hashlib.sha256((raw or "").encode("utf-8")).hexdigest()


def create_token(kind, subject_id, ttl_minutes=60):
    pt = _delegate()
    if pt is not None:
        account_type, purpose = _split(kind)
        return pt.issue_token(_conn(), account_type, int(subject_id), purpose=purpose, ttl_minutes=int(ttl_minutes))
    raw = secrets.token_urlsafe(32)
    conn = _conn()
    cur = conn.cursor()
    try:
        cur.execute("UPDATE account_tokens SET used_at = NOW() WHERE kind = %s AND subject_id = %s AND used_at IS NULL",
                    (kind, int(subject_id)))
        cur.execute("INSERT INTO account_tokens (kind, subject_id, token_hash, expires_at) "
                    "VALUES (%s, %s, %s, DATE_ADD(NOW(), INTERVAL %s MINUTE))",
                    (kind, int(subject_id), _hash(raw), int(ttl_minutes)))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        cur.close()
    return raw


def peek_token(kind, raw):
    pt = _delegate()
    if pt is not None:
        account_type, purpose = _split(kind)
        info = pt.lookup(_conn(), raw, account_type=account_type)
        if info and info.get("purpose") == purpose:
            return int(info["account_id"])
        return None
    if not raw:
        return None
    cur = _conn().cursor()
    try:
        cur.execute("SELECT subject_id FROM account_tokens WHERE kind = %s AND token_hash = %s "
                    "AND used_at IS NULL AND expires_at > NOW() LIMIT 1", (kind, _hash(raw)))
        row = cur.fetchone()
        if not row:
            return None
        return int(row["subject_id"] if isinstance(row, dict) else row[0])
    finally:
        cur.close()


def consume_token(kind, raw):
    pt = _delegate()
    if pt is not None:
        subject = peek_token(kind, raw)
        if subject is None:
            return None
        return subject if pt.consume(_conn(), raw) else None
    if not raw:
        return None
    conn = _conn()
    cur = conn.cursor()
    try:
        cur.execute("SELECT id, subject_id FROM account_tokens WHERE kind = %s AND token_hash = %s "
                    "AND used_at IS NULL AND expires_at > NOW() LIMIT 1", (kind, _hash(raw)))
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


def build_url(endpoint, raw, fallback_path):
    """Absolute link for an e-mail; falls back to a literal path when the
    endpoint is not registered in this deployment."""
    from flask import request, url_for
    try:
        return url_for(endpoint, token=raw, _external=True)
    except Exception:
        return request.url_root.rstrip("/") + fallback_path.format(token=raw)
