"""Provider selection for the Futurematch AI runtime (OpenAI <-> Claude).

Why this module exists
----------------------
Everything AI-related in this codebase was env-var driven and hard-wired to the
OpenAI SDK. This module introduces ONE place that answers two questions:

    * which provider serves the conversational agent right now? -> ``provider()``
    * which model strings does that provider use?               -> ``main_model()`` / ``fast_model()``

Both resolve from an admin-editable DB setting that FALLS BACK to the existing
environment variables. With no ``ai_settings`` row present and no new env vars
set, every function here returns exactly the legacy value, so importing this
module changes nothing until an admin flips the switch.

Scope of the toggle (important)
-------------------------------
The toggle governs the CONVERSATIONAL AGENT path only: the tool loop, the SSE
final-answer stream, tool-less completions and the intent router in
``ai_runtime``. It deliberately does NOT govern:

    * embeddings / RAG retrieval (``app1/rag.py``) — Anthropic has no embeddings
      API, and the shipped catalog index is built with text-embedding-3-small at
      1024 dimensions. ``OPENAI_API_KEY`` therefore stays required in EVERY
      configuration, including provider=anthropic.
    * the RAG cross-encoder rerank, CV extraction, catalog auto-categorisation,
      HR insight summaries and the eval judge — batch/offline subsystems pinned
      to OpenAI via :func:`openai_fast_model` so flipping the toggle can never
      break them.

Settings storage
----------------
Values live in the ``ai_settings`` key/value table, are cached in-process for
``_SETTINGS_TTL`` seconds, and fall back to ``os.getenv``. Only keys in
:data:`MANAGED_KEYS` may be written from the admin UI — API keys are NEVER
stored in the database; they stay in the host environment.

Thread-safety: ``main_model()`` and friends are called from ThreadPool workers
(parallel tools, live tool events) where Flask's app context is not visible. The
DB read is therefore attempted only when an app context exists; worker threads
read the module-global snapshot and fall back to env when it is cold. A cold
worker read returns the legacy env value — safe by construction.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# --- Provider identifiers ------------------------------------------------------
PROVIDER_OPENAI = "openai"
PROVIDER_ANTHROPIC = "anthropic"
# Serve OpenAI, but ALSO run a sampled tool-less Claude completion in the
# background purely for latency/cost/quality comparison. See ai_runtime for the
# safety rules (shadow runs never execute tools and never touch the response).
PROVIDER_ANTHROPIC_SHADOW = "anthropic_shadow"

VALID_PROVIDERS: Tuple[str, ...] = (
    PROVIDER_OPENAI,
    PROVIDER_ANTHROPIC,
    PROVIDER_ANTHROPIC_SHADOW,
)

PROVIDER_LABELS: Dict[str, str] = {
    PROVIDER_OPENAI: "OpenAI (GPT)",
    PROVIDER_ANTHROPIC: "Anthropic (Claude)",
    PROVIDER_ANTHROPIC_SHADOW: "OpenAI + Claude skyggekørsel",
}

# Defaults per provider. OpenAI defaults mirror the historical env defaults in
# ai_runtime exactly, so nothing moves when the toggle is untouched.
DEFAULT_OPENAI_MAIN = "gpt-4o"
DEFAULT_OPENAI_FAST = "gpt-4o-mini"
# Claude defaults: Opus 5 for the main tier, Haiku 4.5 for the cheap tier.
# Haiku is used for the 4-token intent router and short tool-deciding turns; it
# does not support output_config.effort, which the adapter accounts for.
DEFAULT_ANTHROPIC_MAIN = "claude-opus-5"
DEFAULT_ANTHROPIC_FAST = "claude-haiku-4-5"

# --- Admin-manageable settings -------------------------------------------------
# key -> (label, default, choices or None). ONLY these keys can be written from
# the admin UI. Secrets are intentionally absent: keys live in the environment.
MANAGED_KEYS: Dict[str, Tuple[str, str, Optional[Tuple[str, ...]]]] = {
    "AI_PROVIDER": ("AI-udbyder", PROVIDER_OPENAI, VALID_PROVIDERS),
    "AI_MAIN_MODEL": ("OpenAI hovedmodel", DEFAULT_OPENAI_MAIN, None),
    "AI_FAST_MODEL": ("OpenAI hurtig model", DEFAULT_OPENAI_FAST, None),
    "ANTHROPIC_MAIN_MODEL": ("Claude hovedmodel", DEFAULT_ANTHROPIC_MAIN, None),
    "ANTHROPIC_FAST_MODEL": ("Claude hurtig model", DEFAULT_ANTHROPIC_FAST, None),
    "AI_MODEL_ROUTING": ("Modelrouting", "balanced", ("quality", "balanced", "cost")),
    "AI_SHADOW_SAMPLE_RATE": ("Skyggekørsel sample-rate", "0.1", None),
}

_SETTINGS_TTL = 60.0
_SNAPSHOT: Dict[str, str] = {}
_SNAPSHOT_AT = 0.0
_SNAPSHOT_LOCK = threading.Lock()
_TABLE_READY = set()

_AI_SETTINGS_DDL = """
CREATE TABLE IF NOT EXISTS ai_settings (
    setting_key VARCHAR(100) NOT NULL PRIMARY KEY,
    setting_value TEXT,
    updated_by VARCHAR(255),
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
"""


def ensure_ai_settings_table(mysql) -> None:
    """Create ``ai_settings`` if missing. Never raises; mirrors ensure_ai_log_tables."""
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
            cur.execute(_AI_SETTINGS_DDL)
            mysql.connection.commit()
            _TABLE_READY.add(cache_key)
        finally:
            cur.close()
    except Exception as exc:  # noqa: BLE001 - settings must never break a request
        logger.warning("ai_settings table unavailable, falling back to env: %s", exc)
        try:
            mysql.connection.rollback()
        except Exception:
            pass


def _read_db_snapshot() -> Optional[Dict[str, str]]:
    """Read the whole (tiny) settings table. Returns None when unavailable.

    Only attempted inside a Flask app context — ThreadPool workers have none and
    must not blow up here.
    """
    try:
        from flask import current_app, has_app_context

        if not has_app_context():
            return None
        mysql = getattr(current_app, "mysql", None)
        if mysql is None:
            return None
        ensure_ai_settings_table(mysql)
        from db_compat import refresh_flask_mysql_connection

        refresh_flask_mysql_connection(mysql)
        cur = mysql.connection.cursor()
        try:
            cur.execute("SELECT setting_key, setting_value FROM ai_settings")
            rows = cur.fetchall() or []
        finally:
            cur.close()
        snapshot: Dict[str, str] = {}
        for row in rows:
            if isinstance(row, dict):
                key = row.get("setting_key")
                value = row.get("setting_value")
            else:
                key, value = row[0], row[1]
            if key:
                snapshot[str(key)] = "" if value is None else str(value)
        return snapshot
    except Exception as exc:  # noqa: BLE001 - never raise out of a settings read
        logger.warning("ai_settings read failed, falling back to env: %s", exc)
        return None


def _snapshot() -> Dict[str, str]:
    """Return the cached settings snapshot, refreshing it at most every TTL."""
    global _SNAPSHOT, _SNAPSHOT_AT
    now = time.time()
    if _SNAPSHOT_AT and (now - _SNAPSHOT_AT) < _SETTINGS_TTL:
        return _SNAPSHOT
    fresh = _read_db_snapshot()
    if fresh is None:
        # No DB access from here (worker thread / no app context / DB down).
        # Keep serving the last good snapshot; callers fall back to env for
        # anything it does not contain.
        return _SNAPSHOT
    with _SNAPSHOT_LOCK:
        _SNAPSHOT = fresh
        _SNAPSHOT_AT = now
    return _SNAPSHOT


def invalidate_settings_cache() -> None:
    """Force the next read to hit the DB (called after an admin write)."""
    global _SNAPSHOT_AT
    _SNAPSHOT_AT = 0.0


def get_setting(key: str, default: str = "") -> str:
    """DB value -> env value -> ``default``. Never raises."""
    try:
        value = _snapshot().get(key)
        if value is not None and str(value).strip() != "":
            return str(value).strip()
    except Exception:  # noqa: BLE001
        pass
    return (os.getenv(key) or default or "").strip()


def set_setting(mysql, key: str, value: str, updated_by: str = "") -> bool:
    """Upsert one managed setting. Returns True on success. Never raises."""
    if key not in MANAGED_KEYS:
        return False
    try:
        ensure_ai_settings_table(mysql)
        from db_compat import refresh_flask_mysql_connection

        refresh_flask_mysql_connection(mysql)
        cur = mysql.connection.cursor()
        try:
            cur.execute(
                "INSERT INTO ai_settings (setting_key, setting_value, updated_by) "
                "VALUES (%s, %s, %s) "
                "ON DUPLICATE KEY UPDATE setting_value = VALUES(setting_value), "
                "updated_by = VALUES(updated_by)",
                (key, str(value), (updated_by or "")[:255]),
            )
            mysql.connection.commit()
        finally:
            cur.close()
        invalidate_settings_cache()
        return True
    except Exception as exc:  # noqa: BLE001
        logger.error("ai_settings write failed for %s: %s", key, exc)
        try:
            mysql.connection.rollback()
        except Exception:
            pass
        return False


def effective_settings() -> List[Dict[str, Any]]:
    """Every managed key with its live value and where that value came from.

    Powers the admin page so an operator can see at a glance whether a value is
    a DB override, an env var, or the built-in default.
    """
    snapshot = _snapshot()
    rows: List[Dict[str, Any]] = []
    for key, (label, default, choices) in MANAGED_KEYS.items():
        db_value = (snapshot.get(key) or "").strip()
        env_value = (os.getenv(key) or "").strip()
        if db_value:
            source, value = "database", db_value
        elif env_value:
            source, value = "miljøvariabel", env_value
        else:
            source, value = "standard", default
        rows.append({
            "key": key,
            "label": label,
            "value": value,
            "default": default,
            "choices": list(choices) if choices else None,
            "source": source,
            "overridden": bool(db_value),
        })
    return rows


# --- Resolved provider / model accessors --------------------------------------

def provider() -> str:
    """Active provider for the conversational agent path."""
    raw = get_setting("AI_PROVIDER", PROVIDER_OPENAI).lower()
    return raw if raw in VALID_PROVIDERS else PROVIDER_OPENAI


def provider_label(name: str = "") -> str:
    return PROVIDER_LABELS.get(name or provider(), PROVIDER_OPENAI)


def uses_anthropic() -> bool:
    """True when Claude serves the request path (shadow mode does NOT count)."""
    return provider() == PROVIDER_ANTHROPIC


def shadow_enabled() -> bool:
    return provider() == PROVIDER_ANTHROPIC_SHADOW


def shadow_sample_rate() -> float:
    try:
        return max(0.0, min(1.0, float(get_setting("AI_SHADOW_SAMPLE_RATE", "0.1"))))
    except (TypeError, ValueError):
        return 0.1


def openai_main_model() -> str:
    """OpenAI main model, regardless of the toggle (pinned subsystems)."""
    return get_setting("AI_MAIN_MODEL", DEFAULT_OPENAI_MAIN)


def openai_fast_model() -> str:
    """OpenAI fast model, regardless of the toggle (pinned subsystems).

    Used by embeddings-adjacent and batch/offline callers (RAG rerank, CV
    extraction) that talk to the OpenAI SDK directly and must keep working when
    the conversational agent is switched to Claude.
    """
    return get_setting("AI_FAST_MODEL", DEFAULT_OPENAI_FAST)


def anthropic_main_model() -> str:
    return get_setting("ANTHROPIC_MAIN_MODEL", DEFAULT_ANTHROPIC_MAIN)


def anthropic_fast_model() -> str:
    return get_setting("ANTHROPIC_FAST_MODEL", DEFAULT_ANTHROPIC_FAST)


def main_model() -> str:
    """Main-tier model for the ACTIVE provider."""
    if uses_anthropic():
        return anthropic_main_model()
    return openai_main_model()


def fast_model() -> str:
    """Cheap-tier model for the ACTIVE provider."""
    if uses_anthropic():
        return anthropic_fast_model()
    return openai_fast_model()


def anthropic_sdk_available() -> bool:
    try:
        import anthropic  # noqa: F401

        return True
    except Exception:
        return False


def _secret_present(name: str) -> bool:
    """Key presence via ai_secrets (database -> environment). Never raises."""
    try:
        import ai_secrets

        return ai_secrets.has_secret(name)
    except Exception:
        return bool(os.getenv(name))


def anthropic_configured() -> bool:
    """Both an API key and the SDK are present."""
    return _secret_present("ANTHROPIC_API_KEY") and anthropic_sdk_available()


def openai_configured() -> bool:
    return _secret_present("OPENAI_API_KEY")


def provider_readiness() -> Dict[str, Any]:
    """Readiness of the ACTIVE provider plus the always-required OpenAI key.

    OpenAI is required in every configuration because embeddings/RAG have no
    Anthropic equivalent — see the module docstring.
    """
    active = provider()
    needs_anthropic = active in (PROVIDER_ANTHROPIC, PROVIDER_ANTHROPIC_SHADOW)
    problems: List[str] = []
    if not openai_configured():
        problems.append("OPENAI_API_KEY mangler (kræves altid — embeddings/RAG)")
    if needs_anthropic and not _secret_present("ANTHROPIC_API_KEY"):
        problems.append("ANTHROPIC_API_KEY mangler")
    if needs_anthropic and not anthropic_sdk_available():
        problems.append("anthropic-pakken er ikke installeret")
    return {
        "provider": active,
        "provider_label": provider_label(active),
        "main_model": main_model(),
        "fast_model": fast_model(),
        "openai_configured": openai_configured(),
        "anthropic_configured": anthropic_configured(),
        "ready": not problems,
        "problems": problems,
    }
