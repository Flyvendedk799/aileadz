"""Pending-confirmation token store.

Holds tool call args server-side for a short TTL so the client only receives
an opaque token — raw arguments never travel to the browser.

Tokens are bound to the originating session; a token presented by a different
session is treated as unknown (returns None from pop_pending).
"""
import secrets
import time
import threading

_STORE: dict = {}
_LOCK = threading.Lock()
_TTL_S = 600  # 10 minutes


def _cleanup_expired():
    """Remove entries past their TTL. Must be called while holding _LOCK."""
    now = time.time()
    expired = [k for k, v in _STORE.items() if v["expires_at"] < now]
    for k in expired:
        _STORE.pop(k, None)


def store_pending(session_id: str, scope: str, tool_name: str, args: dict) -> str:
    """Store a pending confirm and return an opaque token.

    Args:
        session_id: The originating session (e.g. session["session_id"]).
        scope:      "hr" or "employee" — determines re-dispatch path.
        tool_name:  The tool function name (e.g. "manage_my_order").
        args:       The tool arguments as they arrived (without confirm=True).

    Returns:
        A 32-hex-char opaque token the client can present back to
        /app1/confirm_tool_action.
    """
    token = secrets.token_hex(16)
    with _LOCK:
        _cleanup_expired()
        _STORE[token] = {
            "session_id": session_id,
            "scope": scope,
            "tool_name": tool_name,
            "args": dict(args or {}),
            "expires_at": time.time() + _TTL_S,
        }
    return token


def pop_pending(session_id: str, token: str):
    """Consume and return a pending confirmation entry, or None.

    Returns None when the token is unknown, expired, or was issued for a
    different session_id (prevents token-guessing / session-fixation).
    The entry is removed on the first successful pop — double-confirm
    receives None and the confirm route treats it as already_confirmed.
    """
    with _LOCK:
        entry = _STORE.get(token)
        if entry is None:
            return None
        if entry["session_id"] != session_id:
            return None
        if entry["expires_at"] < time.time():
            _STORE.pop(token, None)
            return None
        _STORE.pop(token)
        return entry


def pending_count() -> int:
    """Return current pending count (test helper)."""
    with _LOCK:
        return len(_STORE)


def clear_all():
    """Clear all pending entries (test helper — never call in production)."""
    with _LOCK:
        _STORE.clear()
