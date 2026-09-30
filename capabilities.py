"""Capability helper for navigation, the settings hub and order actions (N-2.3).

Part B callers ask ``can('company.approvals')`` instead of comparing role names.
The answer comes from Part A's role matrix in ``auth_decorators`` (S-2.3), which
includes per-company ``permissions`` overrides, department scoping and the
session-liveness recheck. ``auth_decorators.CAPABILITY_ALIASES`` maps the
``company.*`` names used here to the matrix capabilities, so there is exactly one
place that decides who may do what.
"""

from __future__ import annotations

import auth_decorators

EMPLOYEE = "employee"
DEPARTMENT_HEAD = "department_head"
HR_MANAGER = "hr_manager"
COMPANY_ADMIN = "company_admin"
PLATFORM_ADMIN = "admin"


def effective_role(session=None) -> str:
    """The single role a request acts as (platform admin, company role or employee)."""
    if session is None:
        session = auth_decorators._session()
    try:
        if session.get("role") == PLATFORM_ADMIN:
            return PLATFORM_ADMIN
        role = (session.get("company_role") or "").strip().lower()
    except Exception:
        return EMPLOYEE
    return role if role in auth_decorators.ROLE_RANK else EMPLOYEE


def role_rank(role) -> int:
    if role == PLATFORM_ADMIN:
        return max(auth_decorators.ROLE_RANK.values()) + 1
    return auth_decorators.ROLE_RANK.get(role or EMPLOYEE, 0)


def can(capability: str, session=None) -> bool:
    """True when the acting user holds ``capability`` (fail closed if unknown)."""
    try:
        return bool(auth_decorators.can(capability, session))
    except Exception:
        return False


def capabilities_for(session=None) -> set:
    return auth_decorators.capabilities_for(session)


def is_manager(session=None) -> bool:
    return can("hr.view", session)


def is_hr(session=None) -> bool:
    return can("hr.manage", session)


def register_jinja(app) -> None:
    """Template helper ``has_endpoint('x.y')`` (``can`` comes from
    ``auth_decorators.register_capability_context``; it is also set as a global so
    templates rendered outside a request context keep working)."""
    def has_endpoint(name):
        return name in app.view_functions

    app.jinja_env.globals.update(can=can, has_endpoint=has_endpoint)
