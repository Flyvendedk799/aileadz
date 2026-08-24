"""Encrypted-at-rest storage for AI provider API keys.

Companion to ``ai_provider``: that module holds non-secret settings, this one
holds the credentials, deliberately in a separate table with a separate accessor
so a secret can never fall out of the settings snapshot that the admin page
renders.

Resolution order for every key is **database -> environment**. A deploy that
configures keys the old way (host environment only) keeps working untouched;
setting a key in the admin UI overrides it from the next cache refresh.

Security properties
-------------------
* **Encrypted at rest with Fernet**, reusing the key-resolution shape already
  used for SSO client secrets (``enterprise_sso``): ``AI_SECRET_KEY`` env or app
  config first, else a key derived from ``SECRET_KEY`` (weaker separation, warned
  about). Unlike the SSO path there is **no plaintext fallback**: with no key
  material at all, :func:`set_secret` refuses the write instead of storing a
  readable API key in the database.
* **Write-only from the UI.** Values are never returned to a template or an HTTP
  response. :func:`secret_status` exposes only presence, the last four
  characters, and who changed it when.
* **Never logged.** The audit trail and every log line here record the key NAME
  and the action, never the value.

Threat model, stated plainly: encryption protects database dumps, replicas and
backups. It does NOT protect against an attacker who has both the database and
the Fernet key (i.e. the application host). And it is a real privilege change —
setting a provider key used to require server access, and now any account with
the admin role can do it. That is the trade the feature asks for.

Legacy consumers
----------------
Several modules call the OpenAI SDK at module level and read ``OPENAI_API_KEY``
straight from the environment (``app1``, ``catalog_service``, ``insights_engine``,
``ai_eval``). Rather than rewrite each one, :func:`_refresh` exports DB-backed
secrets into ``os.environ`` on every cache refresh, so those callers see a
UI-configured key within one TTL. That mutation is idempotent and happens in
exactly one place.
"""

from __future__ import annotations

import base64
import hashlib
import logging
import os
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

try:
    from cryptography.fernet import Fernet, InvalidToken
    _FERNET_AVAILABLE = True
except Exception as _fernet_err:  # pragma: no cover - depends on deploy env
    Fernet = None
    InvalidToken = Exception
    _FERNET_AVAILABLE = False
    logger.warning(
        "ai_secrets: cryptography/Fernet unavailable (%s); API keys cannot be "
        "stored from the admin UI until 'cryptography' is installed.", _fernet_err
    )

# Marker prefix, so ciphertext is distinguishable from anything else. Mirrors
# enterprise_sso's convention.
_ENC_PREFIX = "fernet$"

# The only names that may be stored. Anything else is rejected outright.
SECRET_KEYS: Dict[str, str] = {
    "OPENAI_API_KEY": "OpenAI API-nøgle",
    "ANTHROPIC_API_KEY": "Anthropic API-nøgle",
}

_TTL = 60.0
_CACHE: Dict[str, str] = {}
_CACHE_AT = 0.0
_LOCK = threading.Lock()
_TABLE_READY = set()
# What os.environ held before we exported a DB-backed secret over it, so the
# export can be UNDONE when the row is removed. None means "was not set".
_ENV_ORIGINAL: Dict[str, Optional[str]] = {}

_DDL = """
CREATE TABLE IF NOT EXISTS ai_secrets (
    secret_name VARCHAR(100) NOT NULL PRIMARY KEY,
    secret_value TEXT,
    hint VARCHAR(16),
    updated_by VARCHAR(255),
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
"""


# --- encryption ---------------------------------------------------------------

def _get_fernet():
    """Return a Fernet instance, or None when no key material is available.

    Resolution: AI_SECRET_KEY (env, then app config) -> key derived from
    SECRET_KEY. Never raises.
    """
    if not _FERNET_AVAILABLE:
        return None
    try:
        raw_key = os.environ.get("AI_SECRET_KEY")
        if not raw_key:
            try:
                from flask import current_app, has_app_context

                if has_app_context():
                    raw_key = current_app.config.get("AI_SECRET_KEY")
            except Exception:
                raw_key = None
        if raw_key:
            if isinstance(raw_key, str):
                raw_key = raw_key.encode("utf-8")
            return Fernet(raw_key)

        secret_key = None
        try:
            from flask import current_app, has_app_context

            if has_app_context():
                secret_key = current_app.config.get("SECRET_KEY")
        except Exception:
            secret_key = None
        if not secret_key:
            secret_key = os.environ.get("SECRET_KEY")
        if not secret_key:
            return None
        if isinstance(secret_key, str):
            secret_key = secret_key.encode("utf-8")
        derived = base64.urlsafe_b64encode(hashlib.sha256(secret_key).digest())
        logger.warning(
            "ai_secrets: AI_SECRET_KEY is not set; deriving the API-key encryption "
            "key from SECRET_KEY (weaker separation, and rotating SECRET_KEY will "
            "orphan stored keys). Set AI_SECRET_KEY in the environment."
        )
        return Fernet(derived)
    except Exception as exc:
        logger.warning("ai_secrets: could not build a Fernet key (%s).", exc)
        return None


def encryption_available() -> bool:
    """True when a secret can actually be stored encrypted."""
    return _get_fernet() is not None


def encryption_detail() -> str:
    """Human-readable reason the admin UI can show when storage is unavailable."""
    if not _FERNET_AVAILABLE:
        return "cryptography-pakken er ikke installeret"
    if os.environ.get("AI_SECRET_KEY"):
        return "AI_SECRET_KEY"
    if _get_fernet() is not None:
        return "afledt af SECRET_KEY (sæt AI_SECRET_KEY for stærkere adskillelse)"
    return "hverken AI_SECRET_KEY eller SECRET_KEY er sat"


def _encrypt(plaintext: str) -> Optional[str]:
    """Encrypt a secret. Returns None when encryption is impossible.

    There is deliberately NO plaintext fallback: refusing the write is the safe
    failure mode for an API key.
    """
    fernet = _get_fernet()
    if fernet is None:
        return None
    try:
        return _ENC_PREFIX + fernet.encrypt(plaintext.encode("utf-8")).decode("utf-8")
    except Exception as exc:
        logger.warning("ai_secrets: encryption failed (%s); refusing to store.", exc)
        return None


def _decrypt(stored: Optional[str]) -> Optional[str]:
    """Decrypt a stored secret; None when unreadable. Never returns ciphertext."""
    if not stored or not isinstance(stored, str):
        return None
    if not stored.startswith(_ENC_PREFIX):
        # Not ours. Do not return it — an unmarked value is either corruption or
        # something written outside this module, and leaking it is worse than
        # falling back to the environment.
        logger.warning("ai_secrets: stored value is not Fernet-marked; ignoring it.")
        return None
    fernet = _get_fernet()
    if fernet is None:
        logger.warning("ai_secrets: encrypted key present but Fernet is unavailable.")
        return None
    try:
        return fernet.decrypt(stored[len(_ENC_PREFIX):].encode("utf-8")).decode("utf-8")
    except InvalidToken:
        logger.warning("ai_secrets: decryption failed (encryption key rotated?).")
        return None
    except Exception as exc:
        logger.warning("ai_secrets: decryption error (%s).", exc)
        return None


# --- storage ------------------------------------------------------------------

def ensure_ai_secrets_table(mysql) -> None:
    """Create ``ai_secrets`` if missing. Never raises."""
    if not mysql:
        return
    cache_key = id(mysql)
    if cache_key in _TABLE_READY:
        return
    try:
        from db_compat import refresh_flask_mysql_connection

        refresh_flask_mysql_connection(mysql)
        cur = mysql.connection.cursor()
        try:
            cur.execute(_DDL)
            mysql.connection.commit()
            _TABLE_READY.add(cache_key)
        finally:
            cur.close()
    except Exception as exc:
        logger.warning("ai_secrets: table unavailable, falling back to env: %s", exc)
        try:
            mysql.connection.rollback()
        except Exception:
            pass


def _read_rows() -> Optional[List[Dict[str, Any]]]:
    """Read every stored secret row, or None when the DB is unreachable here."""
    try:
        from flask import current_app, has_app_context

        if not has_app_context():
            return None
        mysql = getattr(current_app, "mysql", None)
        if mysql is None:
            return None
        ensure_ai_secrets_table(mysql)
        from db_compat import refresh_flask_mysql_connection

        refresh_flask_mysql_connection(mysql)
        cur = mysql.connection.cursor()
        try:
            cur.execute(
                "SELECT secret_name, secret_value, hint, updated_by, updated_at "
                "FROM ai_secrets"
            )
            rows = cur.fetchall() or []
        finally:
            cur.close()
        out = []
        for row in rows:
            if isinstance(row, dict):
                out.append(row)
            else:
                out.append({
                    "secret_name": row[0], "secret_value": row[1], "hint": row[2],
                    "updated_by": row[3], "updated_at": row[4],
                })
        return out
    except Exception as exc:
        logger.warning("ai_secrets: read failed, falling back to env: %s", exc)
        return None


def _refresh() -> Dict[str, str]:
    """Refresh the decrypted-secret cache, at most once per TTL.

    Also exports DB-backed secrets into ``os.environ`` so the module-level
    OpenAI callers elsewhere in the codebase (app1, catalog_service,
    insights_engine, ai_eval) pick up a UI-configured key without each of them
    having to learn about this module. See the module docstring.
    """
    global _CACHE, _CACHE_AT
    now = time.time()
    if _CACHE_AT and (now - _CACHE_AT) < _TTL:
        return _CACHE
    rows = _read_rows()
    if rows is None:
        return _CACHE
    fresh: Dict[str, str] = {}
    for row in rows:
        name = str(row.get("secret_name") or "")
        if name not in SECRET_KEYS:
            continue
        value = _decrypt(row.get("secret_value"))
        if value:
            fresh[name] = value
    with _LOCK:
        _CACHE = fresh
        _CACHE_AT = now
    _sync_environ(fresh)
    return _CACHE


def _sync_environ(fresh: Dict[str, str]) -> None:
    """Mirror DB-backed secrets into os.environ, reversibly.

    Exporting is what lets the legacy module-level OpenAI callers see a
    UI-configured key. Un-exporting is what makes REMOVAL actually take effect:
    without it, clearing a key would leave the exported value in os.environ and
    the removed key would keep working until the process restarted.

    Because every worker runs this on its own refresh, a removal propagates to
    all of them within one TTL, not just the worker that handled the request.
    """
    for name in SECRET_KEYS:
        value = fresh.get(name)
        if value:
            if name not in _ENV_ORIGINAL:
                _ENV_ORIGINAL[name] = os.environ.get(name)
            if os.environ.get(name) != value:
                os.environ[name] = value
        elif name in _ENV_ORIGINAL:
            original = _ENV_ORIGINAL.pop(name)
            if original is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = original


def invalidate_cache() -> None:
    """Force the next read to hit the DB (called after a write)."""
    global _CACHE_AT
    _CACHE_AT = 0.0


def get_secret(name: str) -> str:
    """Decrypted DB value, else the environment value, else "". Never raises."""
    try:
        value = _refresh().get(name)
        if value:
            return value
    except Exception:  # noqa: BLE001 - a credential read must never raise
        pass
    return (os.environ.get(name) or "").strip()


def has_secret(name: str) -> bool:
    return bool(get_secret(name))


def set_secret(mysql, name: str, value: str, updated_by: str = "") -> Tuple[bool, str]:
    """Store one API key encrypted. Returns ``(ok, error_message)``.

    Refuses unknown names, blank values, and — importantly — refuses to store
    anything at all when encryption is unavailable.
    """
    if name not in SECRET_KEYS:
        return False, "ukendt nøgle"
    value = (value or "").strip()
    if not value:
        return False, "tom værdi"
    encrypted = _encrypt(value)
    if encrypted is None:
        return False, (
            "kryptering utilgængelig ({}), nøglen blev IKKE gemt"
            .format(encryption_detail())
        )
    try:
        ensure_ai_secrets_table(mysql)
        from db_compat import refresh_flask_mysql_connection

        refresh_flask_mysql_connection(mysql)
        cur = mysql.connection.cursor()
        try:
            cur.execute(
                "INSERT INTO ai_secrets (secret_name, secret_value, hint, updated_by) "
                "VALUES (%s, %s, %s, %s) "
                "ON DUPLICATE KEY UPDATE secret_value = VALUES(secret_value), "
                "hint = VALUES(hint), updated_by = VALUES(updated_by)",
                (name, encrypted, value[-4:], (updated_by or "")[:255]),
            )
            mysql.connection.commit()
        finally:
            cur.close()
        # Refresh through the normal path so the environ mirror is recorded
        # reversibly rather than poked directly.
        invalidate_cache()
        _refresh()
        # Log the NAME only — never the value.
        logger.info("ai_secrets: %s updated by %s", name, updated_by or "?")
        return True, ""
    except Exception as exc:
        logger.error("ai_secrets: write failed for %s: %s", name, exc)
        try:
            mysql.connection.rollback()
        except Exception:
            pass
        return False, "databasefejl"


def clear_secret(mysql, name: str, updated_by: str = "") -> Tuple[bool, str]:
    """Delete a stored key so resolution falls back to the environment."""
    if name not in SECRET_KEYS:
        return False, "ukendt nøgle"
    try:
        ensure_ai_secrets_table(mysql)
        from db_compat import refresh_flask_mysql_connection

        refresh_flask_mysql_connection(mysql)
        cur = mysql.connection.cursor()
        try:
            cur.execute("DELETE FROM ai_secrets WHERE secret_name = %s", (name,))
            mysql.connection.commit()
        finally:
            cur.close()
        # _refresh() -> _sync_environ() restores whatever os.environ held before
        # we exported over it (or unsets it), so the removed key stops working
        # immediately in this worker and within one TTL in the others.
        invalidate_cache()
        _refresh()
        logger.info("ai_secrets: %s cleared by %s", name, updated_by or "?")
        return True, ""
    except Exception as exc:
        logger.error("ai_secrets: delete failed for %s: %s", name, exc)
        try:
            mysql.connection.rollback()
        except Exception:
            pass
        return False, "databasefejl"


def secret_status() -> List[Dict[str, Any]]:
    """Presence-only view for the admin page. NEVER includes a secret value."""
    _refresh()
    stored = {}
    rows = _read_rows()
    for row in rows or []:
        name = str(row.get("secret_name") or "")
        if name in SECRET_KEYS:
            stored[name] = row
    out = []
    for name, label in SECRET_KEYS.items():
        row = stored.get(name)
        env_set = bool((os.environ.get(name) or "").strip())
        db_set = bool(row and _CACHE.get(name))
        if db_set:
            source = "database"
        elif env_set:
            source = "miljøvariabel"
        else:
            source = "ikke sat"
        out.append({
            "name": name,
            "label": label,
            "configured": db_set or env_set,
            "source": source,
            "in_database": bool(row),
            "hint": (row or {}).get("hint") or "",
            "updated_by": (row or {}).get("updated_by") or "",
            "updated_at": (row or {}).get("updated_at"),
            # Surfaces a rotated/mismatched encryption key: the row exists but
            # cannot be decrypted, so the value is NOT in use.
            "unreadable": bool(row and not _CACHE.get(name)),
        })
    return out


# --- connectivity check -------------------------------------------------------

def test_provider_key(provider_name: str) -> Tuple[bool, str]:
    """Validate the CURRENTLY RESOLVED key with one cheap authenticated call.

    Uses each SDK's models listing: it authenticates without spending tokens.
    Returns ``(ok, message)``; never raises, and never echoes the key.
    """
    try:
        if provider_name == "anthropic":
            key = get_secret("ANTHROPIC_API_KEY")
            if not key:
                return False, "ANTHROPIC_API_KEY er ikke sat"
            import anthropic

            models = anthropic.Anthropic(api_key=key, timeout=20.0, max_retries=0).models.list()
            count = len(getattr(models, "data", None) or [])
            return True, f"Forbindelse OK ({count} modeller tilgængelige)"
        key = get_secret("OPENAI_API_KEY")
        if not key:
            return False, "OPENAI_API_KEY er ikke sat"
        import openai

        models = openai.OpenAI(api_key=key, timeout=20.0, max_retries=0).models.list()
        count = len(getattr(models, "data", None) or [])
        return True, f"Forbindelse OK ({count} modeller tilgængelige)"
    except Exception as exc:
        # Surface the provider's own message (auth errors are informative) but
        # cap it so a huge body cannot be flashed into the page.
        return False, f"{type(exc).__name__}: {str(exc)[:200]}"
