"""
TOTP two-factor authentication (S-2.6), RFC 6238 over the standard library.

Policy:
  * ENFORCED for platform admins (``users.role == 'admin'``): after the password
    step they can do nothing but enrol until a code has been verified.
    ``TWOFA_ENFORCE_ADMIN=0`` switches enforcement off (break-glass only).
  * OPTIONAL for everyone else, including company admins, from /account/2fa.
    ``TWOFA_ENFORCE_COMPANY_ADMIN=1`` makes it mandatory for company admins.
  * Once enabled, a user is ALWAYS challenged at login, whatever the role.

Storage (table ``user_2fa``): the secret is Fernet-encrypted (key derived from
``SECRET_KEY`` or taken from ``TWOFA_FERNET_KEY``); backup codes are stored as
SHA-256 hashes and are single use. ``last_step`` blocks code replay.
"""

import base64
import hashlib
import hmac
import logging
import os
import secrets
import struct
import time
from urllib.parse import quote

logger = logging.getLogger(__name__)

STEP_SECONDS = 30
DIGITS = 6
ISSUER = "Futurematch"
BACKUP_CODE_COUNT = 8

# ---------------------------------------------------------------- TOTP core

def new_secret():
    """160-bit random secret, base32 (what authenticator apps expect)."""
    return base64.b32encode(secrets.token_bytes(20)).decode("ascii").rstrip("=")


def _b32decode(secret):
    s = (secret or "").strip().replace(" ", "").upper()
    s += "=" * (-len(s) % 8)
    return base64.b32decode(s)


def hotp(secret, counter):
    digest = hmac.new(_b32decode(secret), struct.pack(">Q", counter), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    value = struct.unpack(">I", digest[offset:offset + 4])[0] & 0x7FFFFFFF
    return str(value % (10 ** DIGITS)).zfill(DIGITS)


def totp_now(secret, now=None):
    return hotp(secret, int((time.time() if now is None else now) // STEP_SECONDS))


def verify_code(secret, code, now=None, window=1, last_step=None):
    """Return the matching time-step (int) if ``code`` is valid, else None.
    ``window`` allows +/- that many 30 s steps of clock drift; steps at or
    before ``last_step`` are rejected (replay)."""
    code = "".join(ch for ch in str(code or "") if ch.isdigit())
    if len(code) != DIGITS:
        return None
    step_now = int((time.time() if now is None else now) // STEP_SECONDS)
    for delta in range(-window, window + 1):
        step = step_now + delta
        if last_step is not None and step <= last_step:
            continue
        if hmac.compare_digest(hotp(secret, step), code):
            return step
    return None


def provisioning_uri(secret, account, issuer=ISSUER):
    label = quote("%s:%s" % (issuer, account))
    return "otpauth://totp/%s?secret=%s&issuer=%s&algorithm=SHA1&digits=%d&period=%d" % (
        label, secret, quote(issuer), DIGITS, STEP_SECONDS)


def pretty_secret(secret):
    return " ".join(secret[i:i + 4] for i in range(0, len(secret), 4))


# ---------------------------------------------------------- backup codes

def _hash_code(code):
    return hashlib.sha256(("2fa-backup|" + code.strip().lower().replace("-", "")).encode()).hexdigest()


def new_backup_codes(n=BACKUP_CODE_COUNT):
    plain = []
    for _ in range(n):
        raw = secrets.token_hex(5)            # 10 hex chars
        plain.append("%s-%s" % (raw[:5], raw[5:]))
    return plain, [_hash_code(c) for c in plain]


# ------------------------------------------------------------ encryption

def _fernet():
    from cryptography.fernet import Fernet
    key = os.environ.get("TWOFA_FERNET_KEY")
    if key:
        return Fernet(key.encode() if isinstance(key, str) else key)
    base = os.environ.get("SECRET_KEY") or "sandbox-2fa-key"
    try:
        from flask import current_app
        base = current_app.config.get("SECRET_KEY") or base
    except Exception:
        pass
    return Fernet(base64.urlsafe_b64encode(hashlib.sha256(("2fa|" + str(base)).encode()).digest()))


def encrypt_secret(secret):
    return _fernet().encrypt(secret.encode()).decode()


def decrypt_secret(blob):
    return _fernet().decrypt(blob.encode()).decode()


# ------------------------------------------------------------- database

_DDL = """CREATE TABLE IF NOT EXISTS user_2fa (
    user_id INT NOT NULL PRIMARY KEY,
    secret_enc TEXT NOT NULL,
    enabled TINYINT(1) NOT NULL DEFAULT 0,
    backup_codes TEXT NULL,
    last_step BIGINT NOT NULL DEFAULT 0,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    enabled_at DATETIME NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4"""
_TABLE_READY = False


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


def _row(conn, user_id):
    ensure_table(conn)
    cur = conn.cursor()
    try:
        cur.execute("SELECT user_id, secret_enc, enabled, backup_codes, last_step "
                    "FROM user_2fa WHERE user_id = %s", (user_id,))
        r = cur.fetchone()
    finally:
        cur.close()
    if not r:
        return None
    if not isinstance(r, dict):
        r = dict(zip(("user_id", "secret_enc", "enabled", "backup_codes", "last_step"), r))
    return r


def is_enabled(conn, user_id):
    r = _row(conn, user_id)
    return bool(r and r.get("enabled"))


def start_enrollment(conn, user_id):
    """(Re)start enrolment: a fresh secret, not yet enabled. Returns the secret."""
    ensure_table(conn)
    secret = new_secret()
    cur = conn.cursor()
    try:
        cur.execute(
            "INSERT INTO user_2fa (user_id, secret_enc, enabled, backup_codes, last_step) "
            "VALUES (%s, %s, 0, NULL, 0) "
            "ON DUPLICATE KEY UPDATE secret_enc = IF(enabled = 1, secret_enc, VALUES(secret_enc))",
            (user_id, encrypt_secret(secret)),
        )
        conn.commit()
    finally:
        cur.close()
    r = _row(conn, user_id)
    return decrypt_secret(r["secret_enc"])


def pending_secret(conn, user_id):
    r = _row(conn, user_id)
    if r and not r.get("enabled"):
        return decrypt_secret(r["secret_enc"])
    return None


def confirm_enrollment(conn, user_id, code):
    """Verify the first code; on success enable 2FA and return the plain
    backup codes (shown once). Returns None if the code is wrong."""
    r = _row(conn, user_id)
    if not r or r.get("enabled"):
        return None
    step = verify_code(decrypt_secret(r["secret_enc"]), code)
    if step is None:
        return None
    plain, hashed = new_backup_codes()
    cur = conn.cursor()
    try:
        cur.execute(
            "UPDATE user_2fa SET enabled = 1, enabled_at = NOW(), last_step = %s, backup_codes = %s "
            "WHERE user_id = %s AND enabled = 0",
            (step, ",".join(hashed), user_id),
        )
        ok = cur.rowcount == 1
        conn.commit()
    finally:
        cur.close()
    return plain if ok else None


def verify_login(conn, user_id, code):
    """True if ``code`` is a valid TOTP (not replayed) or an unused backup code."""
    r = _row(conn, user_id)
    if not r or not r.get("enabled"):
        return False
    step = verify_code(decrypt_secret(r["secret_enc"]), code, last_step=int(r.get("last_step") or 0))
    cur = conn.cursor()
    try:
        if step is not None:
            cur.execute("UPDATE user_2fa SET last_step = %s WHERE user_id = %s AND last_step < %s",
                        (step, user_id, step))
            ok = cur.rowcount == 1
            conn.commit()
            return ok
        # Backup code?
        hashes = [h for h in (r.get("backup_codes") or "").split(",") if h]
        candidate = _hash_code(str(code or ""))
        if candidate in hashes:
            hashes.remove(candidate)
            cur.execute("UPDATE user_2fa SET backup_codes = %s WHERE user_id = %s AND backup_codes = %s",
                        (",".join(hashes), user_id, r.get("backup_codes")))
            ok = cur.rowcount == 1
            conn.commit()
            return ok
    finally:
        cur.close()
    return False


def backup_codes_left(conn, user_id):
    r = _row(conn, user_id)
    return len([h for h in ((r or {}).get("backup_codes") or "").split(",") if h])


def regenerate_backup_codes(conn, user_id):
    plain, hashed = new_backup_codes()
    cur = conn.cursor()
    try:
        cur.execute("UPDATE user_2fa SET backup_codes = %s WHERE user_id = %s AND enabled = 1",
                    (",".join(hashed), user_id))
        ok = cur.rowcount == 1
        conn.commit()
    finally:
        cur.close()
    return plain if ok else None


def disable(conn, user_id):
    ensure_table(conn)
    cur = conn.cursor()
    try:
        cur.execute("DELETE FROM user_2fa WHERE user_id = %s", (user_id,))
        conn.commit()
    finally:
        cur.close()


# ---------------------------------------------------------------- policy

def _flag(name, default):
    return os.environ.get(name, default) not in ("0", "false", "False", "no", "")


def enrollment_required(role, company_role=None):
    """Must this user have 2FA enabled before using the app?"""
    if role == "admin" and _flag("TWOFA_ENFORCE_ADMIN", "1"):
        return True
    if company_role == "company_admin" and _flag("TWOFA_ENFORCE_COMPANY_ADMIN", "0"):
        return True
    return False
