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


def tool_call_parts(tool_call: Any) -> Tuple[str, Dict[str, Any]]:
    """Return ``(tool_name, args)`` from either tool-call shape.

    Providers hand over ``tool_call.function.name`` with ``arguments`` as a JSON
    string. Internal re-dispatch — the confirm route replaying a held mutation —
    naturally builds a flat object carrying an already-parsed dict. Both are
    legitimate callers, so both executors read through this instead of assuming
    one shape (assuming the provider shape is what made every "Bekræft" click
    fail with AttributeError before it reached the tool).

    Raises ``ValueError`` when the arguments are a string that isn't JSON, so
    the caller can keep reporting a parse error the way it always has.
    """
    import json as _json

    fn = getattr(tool_call, "function", None) or tool_call
    name = getattr(fn, "name", "") or ""
    raw = getattr(fn, "arguments", None)
    if raw is None:
        raw = getattr(tool_call, "arguments", None)

    if isinstance(raw, dict):
        return name, dict(raw)
    if raw in (None, ""):
        return name, {}
    if isinstance(raw, (bytes, bytearray)):
        raw = raw.decode("utf-8", "replace")
    if isinstance(raw, str):
        parsed = _json.loads(raw)  # ValueError propagates to the caller
        return name, parsed if isinstance(parsed, dict) else {}
    return name, {}


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
    capability: Optional[str] = None,
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
    from flask import has_request_context
    from auth_decorators import can
    capability_denied = capability and has_request_context() and not can(capability)
    if capability_denied or not getattr(ctx, "is_manager", False):
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


def confirmed_arguments(tool_name, arguments, result):
    """Carry server-computed quote facts into the single-use confirmation token.

    Never merge arbitrary tool output into executable arguments. Only these
    quote/version fields may augment the original call.
    """
    args = dict(arguments or {})
    if tool_name not in ('create_course_order','create_order_for_employee','assign_learning_path_to_team','manage_my_order','hr_request_order_change'):
        return args
    preview = result.get('confirmation_args') or {}
    for key in ('expected_price','session_id','expected_path_version','expected_total'):
        value=preview.get(key)
        if value is not None and isinstance(value,(str,int,float)):
            args[key]=value
    return args


def action_status(result):
    """One interpretation of action outcomes for tools, receipts and all UIs."""
    if not isinstance(result, dict):
        return "failed"
    if result.get("status") == "partial_success" or (result.get("partial_failure") and any(result.get(k) for k in ("sent", "queued", "orders", "assigned"))):
        return "partial"
    if result.get("success") is True or result.get("status") in {
        "success", "ok", "order_created", "team_orders_created", "handed_off_to_hr", "already_confirmed",
    }:
        return "consumed"
    return "failed"


def enforce_confirmation(name, args):
    """A provider cannot approve its own proposal by supplying confirm=true."""
    from flask import has_request_context
    if not has_request_context() or not args.get('confirm'):
        return args
    import ai_actions
    row = ai_actions.inspect(ai_actions.ACTIVE_OPERATION.get()) if ai_actions.ACTIVE_OPERATION.get() else None
    if not row or row['kind'] != 'confirmation' or row['name'] != name or row['status'] != 'running':
        args = dict(args, confirm=False)
    return args


CONFIRMATION_GUIDANCE = (
    'Handlinger med bekræftelseskort udføres kun via brugerens klik på Bekræft. '
    'Kald værktøjet for at vise en forhåndsvisning; sæt aldrig selv confirm=true, heller ikke efter et ja i chatten. '
    'Når kortet vises, forklar kort hvad det ændrer. Henvis til AI-handlinger ved afbrudt forbindelse '
    'eller usikkert resultat; opret ikke en ny tilsvarende handling for at gætte på udfaldet.'
)
