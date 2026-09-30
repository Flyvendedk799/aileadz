"""Capability helper: the seam between Part B (navigation, settings hub, order
actions) and the Part A role matrix (S-2.3).

Part B asks ``can('company.approvals')`` instead of comparing role names, so the
Part A work can replace ``_ROLE_CAPABILITIES`` / ``effective_role`` with the
real matrix (including per-company ``permissions`` overrides and department
scoping) WITHOUT touching any caller.  Until then this module encodes the
documented hierarchy::

    company_admin ⊃ hr_manager ⊃ department_head ⊃ employee

and treats the platform ``admin`` as allowed everywhere.

Swap point: ``effective_role(session)`` and ``can(cap, session)``.
"""

from __future__ import annotations

EMPLOYEE = "employee"
DEPARTMENT_HEAD = "department_head"
HR_MANAGER = "hr_manager"
COMPANY_ADMIN = "company_admin"
PLATFORM_ADMIN = "admin"

ROLE_RANK = {EMPLOYEE: 0, DEPARTMENT_HEAD: 1, HR_MANAGER: 2, COMPANY_ADMIN: 3, PLATFORM_ADMIN: 4}

# capability -> minimum role rank
_MIN_RANK = {
    # learner
    "learner.home": 0,
    "learner.orders": 0,
    "learner.goals": 0,
    "learner.data_rights": 0,
    # company workspace (HR)
    "company.workspace": 1,          # /hr overview
    "company.team": 1,               # own team / department
    "company.approvals": 1,          # scope to own dept is an S-2.3 concern
    "company.employees": 2,
    "company.budgets": 2,
    "company.analytics": 2,
    "company.reports": 2,
    "company.compliance": 2,
    "company.learning_paths": 2,
    "company.billing": 2,
    "company.notifications_send": 2,
    "company.assistant": 1,
    "company.settings": 2,           # settings hub
    "company.branding": 2,
    "company.chatbot_widget": 2,
    "company.webhooks": 2,
    "company.policies": 2,           # team-order policy, approval policies
    "company.sso": 3,
    "company.api_keys": 3,
    "company.credits": 2,
    # platform
    "platform.admin": 4,
}


def effective_role(session=None) -> str:
    """The single role a request acts as."""
    if session is None:
        try:
            from flask import session as _s
            session = _s
        except Exception:
            return EMPLOYEE
    try:
        if session.get("role") == PLATFORM_ADMIN:
            return PLATFORM_ADMIN
        role = (session.get("company_role") or "").strip().lower()
    except Exception:
        return EMPLOYEE
    return role if role in ROLE_RANK else EMPLOYEE


def role_rank(role) -> int:
    return ROLE_RANK.get(role or EMPLOYEE, 0)


def can(capability: str, session=None) -> bool:
    """True when the acting role holds ``capability``. Unknown capabilities are
    denied (fail closed)."""
    need = _MIN_RANK.get(capability)
    if need is None:
        return False
    return role_rank(effective_role(session)) >= need


def capabilities_for(role) -> set:
    r = role_rank(role)
    return {c for c, need in _MIN_RANK.items() if r >= need}


def is_manager(session=None) -> bool:
    return role_rank(effective_role(session)) >= 1


def is_hr(session=None) -> bool:
    return role_rank(effective_role(session)) >= 2


def register_jinja(app) -> None:
    """Template helpers: ``can('company.approvals')`` and ``has_endpoint('x.y')``.

    The sidebar uses these so a link is only rendered for a role that may use it,
    and only when the target route exists in this deployment.
    """
    def has_endpoint(name):
        return name in app.view_functions

    app.jinja_env.globals.update(can=can, has_endpoint=has_endpoint)
