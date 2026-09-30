"""Company API keys for the settings hub (N-7.2).

Until now a key could only be created *with* an existing admin key through the
API. This module is what the UI calls: create (raw key returned ONCE), list (with
last-used), revoke - all scoped to one company. It is the single place that knows
how a key is stored, so Part A's hash-only hardening (S-3.2) drops in here:

* the SHA-256 of the key is stored in ``key_hash`` (``enterprise_api`` already
  authenticates against it);
* when that column exists the legacy plaintext ``api_key`` column stays NULL (the
  raw key is never stored) and ``key_prefix`` (first characters) is kept so the admin
  can recognise a key in the list.
"""

from __future__ import annotations

import hashlib
import json
import secrets

PERMISSION_PRESETS = {
    "read": ("Læseadgang", ["read:company", "read:employees", "read:learning", "read:orders", "read:reports",
                            "read:analytics", "read:branding", "read:webhooks"]),
    "readwrite": ("Læse og skrive", ["read:company", "read:employees", "read:learning", "read:orders",
                                     "read:reports", "read:analytics", "read:branding", "read:webhooks",
                                     "write:employees", "write:orders", "write:webhooks"]),
    "full": ("Fuld adgang (også SCIM)", ["admin:all"]),
}


def hash_key(raw: str) -> str:
    return hashlib.sha256((raw or "").encode("utf-8")).hexdigest()


def _has_col(cur, table, column) -> bool:
    try:
        cur.execute(
            "SELECT COUNT(*) AS n FROM INFORMATION_SCHEMA.COLUMNS WHERE TABLE_SCHEMA = DATABASE() "
            "AND TABLE_NAME = %s AND COLUMN_NAME = %s", (table, column))
        r = cur.fetchone()
        return bool(int((r["n"] if isinstance(r, dict) else r[0]) or 0))
    except Exception:
        return False


def create_key(conn, company_id, name, preset="read", created_by=None, expires_at=None):
    """Create a key; returns ``(id, raw_key)``. The raw key is never stored when
    hashing is available and must be shown to the admin exactly once."""
    if preset not in PERMISSION_PRESETS:
        raise ValueError("unknown permission preset")
    raw = "ak_" + secrets.token_hex(32)
    digest = hash_key(raw)
    perms = json.dumps(PERMISSION_PRESETS[preset][1])
    cur = conn.cursor()
    try:
        hashed = _has_col(cur, "company_api_keys", "key_hash")
        cols = "company_id, key_name, api_key, permissions, is_active, created_by, expires_at"
        vals = [company_id, (name or "API-nøgle")[:100], None if hashed else raw, perms, 1, created_by,
                expires_at or None]
        if hashed:
            cols += ", key_hash"
            vals.append(digest)
            if _has_col(cur, "company_api_keys", "key_prefix"):
                cols += ", key_prefix"
                vals.append(raw[:11])
        cur.execute("INSERT INTO company_api_keys (%s) VALUES (%s)" % (cols, ",".join(["%s"] * len(vals))), tuple(vals))
        new_id = cur.lastrowid
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        cur.close()
    return new_id, raw


def list_keys(conn, company_id):
    """The company's keys - never the key itself, only a recognisable hint."""
    cur = conn.cursor()
    try:
        hashed = _has_col(cur, "company_api_keys", "key_hash")
        prefixed = _has_col(cur, "company_api_keys", "key_prefix")
        cur.execute(
            "SELECT id, key_name, permissions, is_active, last_used_at, created_at, expires_at"
            + (", key_prefix" if prefixed else "") + (", key_hash" if hashed else "") + ", api_key" +
            " FROM company_api_keys WHERE company_id = %s ORDER BY created_at DESC, id DESC", (company_id,))
        rows = []
        for r in cur.fetchall() or []:
            r = r if isinstance(r, dict) else {}
            ident = (r.get("key_hash") or r.get("api_key") or "")
            r["hint"] = (r.get("key_prefix") + "…") if r.get("key_prefix") else (("…" + ident[-4:]) if ident else "")
            r.pop("key_hash", None)
            r.pop("api_key", None)
            try:
                perms = json.loads(r.get("permissions") or "[]")
            except Exception:
                perms = []
            r["full_access"] = "admin:all" in perms
            r["permission_count"] = len(perms)
            rows.append(r)
        return rows
    finally:
        cur.close()


def revoke_key(conn, company_id, key_id):
    """Deactivate one key of THIS company. True when a row was changed."""
    cur = conn.cursor()
    try:
        cur.execute("UPDATE company_api_keys SET is_active = 0 WHERE id = %s AND company_id = %s AND is_active = 1",
                    (int(key_id), company_id))
        changed = cur.rowcount > 0
        conn.commit()
        return changed
    finally:
        cur.close()
