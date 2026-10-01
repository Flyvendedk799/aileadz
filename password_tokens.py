"""
Single-use, expiring tokens for password reset and invite/set-password (S-2.4).

* The raw token only ever travels by e-mail; the database stores its SHA-256.
* One row per issue. Issuing a new token for the same account voids earlier
  unused ones. ``consume`` is atomic (``UPDATE ... WHERE used_at IS NULL``), so a
  link works exactly once, even with two simultaneous submits.
* Works for both identity types: ``user`` (users.id) and ``vendor`` (vendors.id).
* Purposes: ``reset`` (default 60 min) and ``invite`` (7 days).

Every function takes a DB-API connection so it is trivially testable; the
``*_current`` helpers use Flask's ``current_app.mysql``.
"""

import hashlib
import logging
import secrets

logger = logging.getLogger(__name__)

RESET_TTL_MINUTES = 60
INVITE_TTL_MINUTES = 7 * 24 * 60
ACCOUNT_TYPES = ("user", "vendor")
PURPOSES = ("reset", "invite")

_TABLE_READY = False

_DDL = """CREATE TABLE IF NOT EXISTS password_reset_tokens (
    id INT AUTO_INCREMENT PRIMARY KEY,
    token_hash CHAR(64) NOT NULL,
    account_type VARCHAR(10) NOT NULL,
    account_id INT NOT NULL,
    purpose VARCHAR(10) NOT NULL DEFAULT 'reset',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    expires_at DATETIME NOT NULL,
    used_at DATETIME NULL,
    UNIQUE KEY uk_token_hash (token_hash),
    INDEX idx_account (account_type, account_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4"""


def hash_token(raw):
    return hashlib.sha256((raw or "").encode("utf-8")).hexdigest()


def ensure_table(conn):
    global _TABLE_READY
    if _TABLE_READY:
        return
    cur = conn.cursor()
    try:
        cur.execute(_DDL)
        conn.commit()
        _TABLE_READY = True
    finally:
        cur.close()


def issue_token(conn, account_type, account_id, purpose="reset", ttl_minutes=None):
    """Create a token and return the RAW value (email it; it is never stored)."""
    if account_type not in ACCOUNT_TYPES or purpose not in PURPOSES:
        raise ValueError("bad token type/purpose")
    ttl = ttl_minutes if ttl_minutes is not None else (
        INVITE_TTL_MINUTES if purpose == "invite" else RESET_TTL_MINUTES)
    ensure_table(conn)
    raw = secrets.token_urlsafe(32)
    cur = conn.cursor()
    try:
        # Void older unused tokens for this account: only the newest link works.
        cur.execute(
            "UPDATE password_reset_tokens SET used_at = NOW() "
            "WHERE account_type = %s AND account_id = %s AND used_at IS NULL",
            (account_type, account_id),
        )
        cur.execute(
            "INSERT INTO password_reset_tokens "
            "(token_hash, account_type, account_id, purpose, expires_at) "
            "VALUES (%s, %s, %s, %s, DATE_ADD(NOW(), INTERVAL %s MINUTE))",
            (hash_token(raw), account_type, account_id, purpose, int(ttl)),
        )
        conn.commit()
    finally:
        cur.close()
    return raw


def lookup(conn, raw, account_type=None):
    """Return ``{'account_type','account_id','purpose'}`` for a valid, unused,
    unexpired token, else None. Does NOT consume it."""
    if not raw or len(raw) > 200:
        return None
    ensure_table(conn)
    cur = conn.cursor()
    try:
        cur.execute(
            "SELECT account_type, account_id, purpose FROM password_reset_tokens "
            "WHERE token_hash = %s AND used_at IS NULL AND expires_at >= NOW() LIMIT 1",
            (hash_token(raw),),
        )
        row = cur.fetchone()
    finally:
        cur.close()
    if not row:
        return None
    if not isinstance(row, dict):
        row = {"account_type": row[0], "account_id": row[1], "purpose": row[2]}
    if account_type and row["account_type"] != account_type:
        return None
    return row


def consume(conn, raw):
    """Atomically burn the token. True only for the first caller."""
    if not raw or len(raw) > 200:
        return False
    ensure_table(conn)
    cur = conn.cursor()
    try:
        cur.execute(
            "UPDATE password_reset_tokens SET used_at = NOW() "
            "WHERE token_hash = %s AND used_at IS NULL AND expires_at >= NOW()",
            (hash_token(raw),),
        )
        ok = cur.rowcount == 1
        conn.commit()
    finally:
        cur.close()
    return ok


def build_url(endpoint, raw, **values):
    """Absolute link for an e-mail."""
    from flask import url_for
    try:
        return url_for(endpoint, token=raw, _external=True, **values)
    except Exception:
        return url_for(endpoint, token=raw, **values)
