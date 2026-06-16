"""Shared confirm / authorization / audit helpers for AI chat mutations.

AI Tooler 2 factors the proven confirm-gate pattern out of
``hr_tools._execute_approve_order_from_chat`` so every new platform-control tool
(HR or employee) can reuse the same three primitives instead of re-implementing
~120 lines each:

* ``needs_confirmation_payload`` — the canonical "preview only" dict a tool returns
  when ``confirm`` is not set. The agent + SSE layer turn this into a confirm card.
* ``manager_guard`` — resolve the actor via ``order_service.OrderContext`` and refuse
  the mutation for non-managers.
* ``audit_chat_mutation`` — best-effort ``audit_log`` row for a confirmed mutation;
  never raises, so it can't break the operation it records.

Kept at the repo top level (not under ``app1/``) so both top-level ``hr_tools`` and
the ``app1`` package can import it without a circular-import cycle.
"""
from __future__ import annotations

from typing import Any, Dict, Optional, Tuple


def needs_confirmation_payload(
    *,
    action: str,
    summary_da: str,
    details: Optional[Dict[str, Any]] = None,
    **extra: Any,
) -> Dict[str, Any]:
    """Canonical preview dict for a side-effect tool awaiting confirmation.

    ``action`` identifies the operation (used by the confirm route to re-dispatch),
    ``summary_da`` is the Danish one-liner shown on the confirm card, and ``details``
    is an optional structured map of what will change. Extra keyword fields are merged
    in for tool-specific context (e.g. ``recipient_count``).
    """
    payload: Dict[str, Any] = {
        "needs_confirmation": True,
        "action": action,
        "message_da": summary_da,
    }
    if details:
        payload["details"] = details
    payload.update(extra)
    return payload


def manager_guard(
    *,
    source: str = "chat",
    message: Optional[str] = None,
) -> Tuple[Optional[Any], Optional[Dict[str, Any]]]:
    """Resolve the actor and require company-manager rights.

    Returns ``(ctx, error)``. ``error`` is ``None`` when the actor is a manager;
    otherwise it's a dict the caller should surface (and refuse the mutation). The
    context (when available) is returned even on the not-authorized path so callers
    can still log/telemeter the attempt, mirroring the original ``_hr_manager_ctx``.
    """
    try:
        from order_service import OrderContext
        ctx = OrderContext.from_session(source=source)
    except Exception as exc:  # pragma: no cover - defensive import/build guard
        print(f"[TOOL_CONFIRM][manager_guard] OrderContext unavailable: {exc}")
        return None, {"error": "Brugerkontekst er ikke tilgængelig lige nu."}
    if not getattr(ctx, "is_manager", False):
        return ctx, {
            "error": "not_authorized",
            "message": message or "Kun ledere/HR-managere kan udføre denne handling.",
        }
    return ctx, None


def audit_chat_mutation(
    cur,
    *,
    company_id: Any,
    user_id: Any,
    action: str,
    resource_id: Any,
    description: str = "",
    resource_type: str = "hr_chat",
) -> None:
    """Best-effort audit row for a confirmed chat mutation. Never raises."""
    try:
        cur.execute(
            """
            INSERT INTO audit_log
                (company_id, user_id, action, action_type, resource_type,
                 resource_id, description)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            """,
            (company_id, user_id, action, action, resource_type, str(resource_id), description),
        )
    except Exception as exc:  # pragma: no cover - audit must never fail the op
        print(f"[TOOL_CONFIRM][audit] audit_log skipped ({action}): {exc}")
