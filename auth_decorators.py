"""
Centralised authentication / authorisation decorators for the whole platform.

Today ~37 routes hand-roll ``if 'user' not in session: ...`` checks, and any
route that forgets one is silently public. This module gives every blueprint a
single, consistent, well-tested set of guards to adopt instead.

Design rules (production-safe):

* Module import must NEVER require a Flask app context and must NEVER crash
  ``create_app()``. ``flask`` is imported lazily inside the wrappers so that
  importing this module is side-effect free.
* Requests that look like API / XHR calls (path starts with ``/api`` or the
  ``Accept`` header prefers ``application/json``) get a JSON 401/403 instead of
  an HTML redirect, so front-end fetch() callers receive a parseable error.
* ``requires_feature`` FAILS OPEN for now: if the tenant's feature flags cannot
  be resolved for any reason we *allow* the request and log a warning. The
  paywall wave will flip this to fail-closed once flag data is reliable. We do
  not want to break production before that wave.
* All user-facing strings are Danish.

Decorators exported:

    login_required(view)
    require_role(*roles)              -> decorator
    require_company(view)
    require_company_role(*roles)      -> decorator
    requires_feature(flag)            -> decorator
"""

from functools import wraps
import logging

logger = logging.getLogger(__name__)

# HR / company-scoped roles, exported for callers that want to reference them.
COMPANY_ROLES = ("company_admin", "hr_manager", "department_head")

# Platform super-admin role: always allowed by require_role.
SUPER_ADMIN_ROLE = "admin"


# ---------------------------------------------------------------------------
# Internal helpers (all import flask lazily so module import is context-free)
# ---------------------------------------------------------------------------

def _wants_json():
    """Return True when the current request should get a JSON error response
    rather than an HTML redirect.

    True when the request path starts with ``/api`` OR the client prefers
    ``application/json`` (typical for fetch/XHR). Defensive: if there is no
    request context for any reason, fall back to non-JSON (redirect)."""
    try:
        from flask import request

        path = request.path or ""
        if path.startswith("/api"):
            return True

        # Explicit JSON request bodies are clearly API calls.
        if getattr(request, "is_json", False):
            return True

        accept = request.accept_mimetypes
        # Prefer JSON only when it is genuinely preferred over text/html.
        if accept and accept.accept_json and not accept.accept_html:
            return True
        # X-Requested-With is the classic XHR marker.
        if request.headers.get("X-Requested-With") == "XMLHttpRequest":
            return True
    except Exception:
        return False
    return False


def _flash(message, category="warning"):
    """Flash a message, swallowing any error (e.g. no session/app context)."""
    try:
        from flask import flash

        flash(message, category)
    except Exception:
        # Flashing is best-effort; never let it break the guard.
        pass


def _redirect_to(endpoint, **values):
    """Redirect to a named endpoint, degrading gracefully if url_for fails
    (e.g. the blueprint is not registered in some stripped-down context)."""
    from flask import redirect, url_for

    try:
        return redirect(url_for(endpoint, **values))
    except Exception:
        # Last-resort fallback so we still send the user *somewhere* safe
        # instead of 500-ing inside an auth guard.
        try:
            return redirect("/")
        except Exception:
            return ("Unauthorized", 401)


def _json_error(message, status):
    from flask import jsonify

    resp = jsonify({"error": message, "status": status})
    resp.status_code = status
    return resp


def _deny(message, status, endpoint, **redirect_values):
    """Produce the right denial response for the current request.

    API/XHR -> JSON error with ``status``.
    Otherwise -> flash + redirect to ``endpoint``.
    """
    if _wants_json():
        return _json_error(message, status)
    _flash(message, "warning")
    return _redirect_to(endpoint, **redirect_values)


def _session():
    """Return the Flask session, or an empty dict if unavailable."""
    try:
        from flask import session

        return session
    except Exception:
        return {}


# ---------------------------------------------------------------------------
# Session liveness (S-1.10): deactivated / SCIM-removed users must not stay in.
#
# The decorators used to trust the signed session cookie alone, so a user whose
# ``company_users.status`` was flipped to inactive (HR toggle, SCIM DELETE)
# kept full access until the cookie expired. We now re-check membership status
# on every guarded request, cached per worker for ``SESSION_RECHECK_TTL``
# seconds to keep the DB load negligible. The check FAILS OPEN on database
# errors (an outage must not lock everybody out) but fails closed on a
# definitively non-active membership.
# ---------------------------------------------------------------------------

import threading
import time

SESSION_RECHECK_TTL = 60  # seconds
_STATUS_CACHE = {}        # {(user_id, company_id): (checked_at, is_active)}
_STATUS_LOCK = threading.Lock()


def _lookup_membership_status(user_id, company_id):
    """Look the membership up. Returns a dict ``{status, role, department,
    permissions}``, ``None`` if there is no such row, or ``False`` if the lookup
    could not be made (no app/DB). A bare status string is also accepted from
    stubs. Separate function so tests can stub it."""
    try:
        from flask import current_app

        mysql = getattr(current_app, "mysql", None)
        if mysql is None:
            return False
        try:
            from db_compat import refresh_flask_mysql_connection

            refresh_flask_mysql_connection(mysql)
        except Exception:
            pass
        cur = mysql.connection.cursor()
        try:
            cur.execute(
                "SELECT status, role, department, permissions FROM company_users "
                "WHERE user_id = %s AND company_id = %s "
                "ORDER BY (status = 'active') DESC LIMIT 1",
                (user_id, company_id),
            )
            row = cur.fetchone()
        finally:
            try:
                cur.close()
            except Exception:
                pass
        if not row:
            return None
        if not isinstance(row, dict):
            row = dict(zip(("status", "role", "department", "permissions"), row))
        return row
    except Exception as exc:  # pragma: no cover - defensive, fail open
        logger.warning("session recheck lookup failed: %s", exc)
        return False


def invalidate_session_cache(user_id=None):
    """Forget cached liveness results (call right after deactivating a user so
    the change bites immediately on this worker; other workers within the TTL)."""
    with _STATUS_LOCK:
        if user_id is None:
            _STATUS_CACHE.clear()
        else:
            for key in [k for k in _STATUS_CACHE if str(k[0]) == str(user_id)]:
                _STATUS_CACHE.pop(key, None)


def session_is_live(sess=None):
    """True if the session's user is still allowed in. Side-effect free."""
    sess = _session() if sess is None else sess
    user_id = sess.get("user_id")
    company_id = sess.get("company_id")
    if not user_id or not company_id:
        return True  # solo user / not bound to a tenant: nothing to re-check
    if sess.get("role") == SUPER_ADMIN_ROLE:
        return True  # platform admins act on tenants without being members
    if sess.get("admin_acting_company_id"):
        return True

    member = _member_cached(user_id, company_id)
    if member is False:
        return True  # could not check -> fail open
    live = member is not None and (member.get("status") or "").strip().lower() == "active"
    if live:
        _sync_session_role(sess, member)
    return live


def _normalize_member(result):
    if result is False or result is None:
        return result
    if isinstance(result, str):
        return {"status": result, "role": None, "department": None, "permissions": None}
    return result


def _member_cached(user_id, company_id):
    """Cached membership dict / None (no row) / False (lookup impossible)."""
    key = (user_id, company_id)
    now = time.time()
    with _STATUS_LOCK:
        hit = _STATUS_CACHE.get(key)
    if hit and now - hit[0] < SESSION_RECHECK_TTL:
        return hit[1]
    member = _normalize_member(_lookup_membership_status(user_id, company_id))
    if member is not False:
        with _STATUS_LOCK:
            if len(_STATUS_CACHE) > 20000:
                _STATUS_CACHE.clear()
            _STATUS_CACHE[key] = (now, member)
    return member


def _sync_session_role(sess, member):
    """A demotion / role change by HR takes effect within the recheck TTL
    instead of at the user's next login."""
    role = (member or {}).get("role")
    if role and sess.get("company_role") != role:
        try:
            sess["company_role"] = role
        except Exception:
            pass


def revoke_session():
    """Drop the signed-in identity from the session."""
    try:
        from flask import session

        session.clear()
    except Exception:
        pass


def _ensure_live_or_deny():
    """None when the session may proceed, otherwise a denial response."""
    if session_is_live():
        return None
    revoke_session()
    return _deny(
        "Din adgang er blevet deaktiveret. Kontakt din administrator.",
        401,
        "auth.login",
    )


def register_session_liveness(app):
    """Install an app-wide ``before_request`` that applies the liveness check to
    EVERY route, including the many that hand-roll ``'user' in session`` checks
    instead of using the decorators. Cheap: cached per worker for 60 s."""

    @app.before_request
    def _session_liveness_gate():  # pragma: no cover - thin wrapper, tested via client
        try:
            from flask import request

            if request.endpoint in (None, "static"):
                return None
            sess = _session()
            if not sess.get("user"):
                return None
            try:  # S-2.5: a forgotten "act as company" session ends by itself
                import impersonation
                if impersonation.expire_if_due(sess) is not None:
                    _flash("Din virksomhedsvisning er udløbet. Du ser igen dit eget workspace.", "info")
            except Exception:
                pass
            return _ensure_live_or_deny()
        except Exception as exc:
            logger.warning("session liveness gate skipped: %s", exc)
            return None


# ---------------------------------------------------------------------------
# Role -> capability matrix (S-2.3). ONE place decides who may do what; the
# route guards, the HR dashboard and (N-2.3) the sidebar all read it.
#
#   company_admin  >  hr_manager  >  department_head (scoped to own dept)
#                  >  team_lead   >  employee
#
# Each capability has a minimum role. Capabilities in DEPARTMENT_SCOPED, when
# held through the department_head role, apply to that person's own department
# only (see ``department_scope``). A member's stored ``permissions`` JSON is
# folded in as a per-person override -- but only where it DIFFERS from what the
# platform seeded for the role, so untouched defaults change nothing.
# ---------------------------------------------------------------------------

ROLE_RANK = {
    "employee": 0, "user": 0,
    "team_lead": 1,
    "department_head": 2,
    "hr_manager": 3,
    "company_admin": 4,
}

CAPABILITY_MIN_ROLE = {
    # entering the HR workspace and its department-scoped views
    "hr.view": "department_head",
    "hr.approvals": "department_head",       # scoped: own department only
    "hr.budget.view": "department_head",     # scoped: own department, read-only
    "hr.employees.view": "department_head",  # scoped: own department only
    # company-wide data and administration
    "hr.analytics": "hr_manager",
    "hr.reports.export": "hr_manager",
    "hr.budget.edit": "hr_manager",
    "hr.employees.manage": "hr_manager",
    "hr.manage": "hr_manager",               # suppliers, policies, departments, chatbot, widget, orders
    "hr.billing": "hr_manager",
    # tenant configuration
    "company.settings": "hr_manager",
    "company.branding": "hr_manager",
    "company.integrations": "company_admin",  # SSO, API keys, webhooks
}

DEPARTMENT_SCOPED = frozenset({"hr.approvals", "hr.budget.view", "hr.employees.view"})

# stored permissions key -> capability it overrides
PERMISSION_OVERRIDES = {
    "manage_users": "hr.employees.manage",
    "view_analytics": "hr.analytics",
    "export_data": "hr.reports.export",
    "manage_billing": "hr.billing",
    "manage_integrations": "company.integrations",
    "manage_branding": "company.branding",
}
# Overrides may also GRANT these beyond the role; everything else can only be
# restricted (so a JSON edit can never mint company-admin powers).
OVERRIDE_GRANTABLE = frozenset({"hr.analytics", "hr.reports.export", "hr.employees.manage"})

# What the platform seeds per role (companies register/add_employee). An
# override only counts when the stored value differs from this.
SEEDED_PERMISSIONS = {
    "company_admin": {"manage_users": True, "view_analytics": True, "export_data": True,
                      "manage_billing": True, "manage_integrations": True, "manage_branding": True},
    "hr_manager": {"manage_users": True, "view_analytics": True, "export_data": True,
                   "manage_billing": False, "manage_integrations": False, "manage_branding": True},
    "department_head": {"manage_users": False, "view_analytics": True, "export_data": True,
                        "manage_billing": False, "manage_integrations": False, "manage_branding": False},
    "team_lead": {"manage_users": False, "view_analytics": True, "export_data": False,
                  "manage_billing": False, "manage_integrations": False, "manage_branding": False},
    "employee": {"manage_users": False, "view_analytics": False, "export_data": False,
                 "manage_billing": False, "manage_integrations": False, "manage_branding": False},
}


def _parse_permissions(raw):
    import json
    if not raw:
        return {}
    if isinstance(raw, dict):
        return raw
    try:
        if isinstance(raw, (bytes, bytearray)):
            raw = raw.decode("utf-8", "replace")
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, dict) else {}
    except Exception:
        return {}


def _effective_company_role(sess, member):
    role = (member or {}).get("role") or sess.get("company_role") or "employee"
    return role if role in ROLE_RANK else "employee"


def _override_for(capability, role, member):
    """True / False when the member's stored permissions deviate from the role
    seed for the key that maps to ``capability``; None when there is no override."""
    perms = _parse_permissions((member or {}).get("permissions"))
    seed = SEEDED_PERMISSIONS.get(role, {})
    for key, cap in PERMISSION_OVERRIDES.items():
        if cap != capability or key not in perms:
            continue
        value = bool(perms[key])
        if key in seed and value == seed[key]:
            return None
        return value
    return None


def _member_for_session(sess):
    user_id, company_id = sess.get("user_id"), sess.get("company_id")
    if not user_id or not company_id:
        return None
    member = _member_cached(user_id, company_id)
    return None if member is False else member


def can(capability, sess=None):
    """Does the current session hold ``capability``? Unknown capabilities are
    denied. Platform admins hold everything."""
    sess = _session() if sess is None else sess
    if not sess.get("user"):
        return False
    if sess.get("role") == SUPER_ADMIN_ROLE:
        return True
    minimum = CAPABILITY_MIN_ROLE.get(capability)
    if minimum is None or not sess.get("company_id"):
        return False
    member = _member_for_session(sess)
    role = _effective_company_role(sess, member)
    allowed = ROLE_RANK.get(role, 0) >= ROLE_RANK[minimum]
    override = _override_for(capability, role, member)
    if override is False:
        return False
    if override is True and capability in OVERRIDE_GRANTABLE:
        # A grant lifts a department head / team lead to the capability, but the
        # person still needs to be inside the HR workspace.
        return ROLE_RANK.get(role, 0) >= ROLE_RANK["department_head"] or allowed
    return allowed


def department_scope(sess=None):
    """None = the caller sees every department. A string = restrict to that
    department (a department head with no department gets '' and so matches
    nothing -- fail closed)."""
    sess = _session() if sess is None else sess
    if sess.get("role") == SUPER_ADMIN_ROLE:
        return None
    member = _member_for_session(sess)
    role = _effective_company_role(sess, member)
    if role != "department_head":
        return None
    return ((member or {}).get("department") or "").strip()


def capabilities_for(sess=None):
    """All capabilities the session holds (what the sidebar reads, N-2.3)."""
    sess = _session() if sess is None else sess
    return {cap for cap in CAPABILITY_MIN_ROLE if can(cap, sess)}


def require_capability(capability, as_json=None):
    """Route guard: 401 when anonymous, 403 when the capability is missing."""

    def decorator(view):
        @wraps(view)
        def wrapped(*args, **kwargs):
            sess = _session()
            if not sess.get("user"):
                return _deny("Log ind for at fortsætte.", 401, "auth.login")
            denied = _ensure_live_or_deny()
            if denied is not None:
                return denied
            if not can(capability, sess):
                return _deny("Du har ikke adgang til denne side.", 403, "dashboard.dashboard")
            return view(*args, **kwargs)

        return wrapped

    return decorator


def register_capability_context(app):
    """Expose ``can`` / ``capabilities`` to every template (sidebar, buttons)."""

    @app.context_processor
    def _inject_capabilities():
        return {"can": can, "department_scope": department_scope}


# ---------------------------------------------------------------------------
# login_required
# ---------------------------------------------------------------------------

def login_required(view):
    """Require an authenticated user (``session['user']``).

    Anonymous requests get a JSON 401 (API/XHR) or a redirect to ``auth.login``
    with a Danish flash message.
    """

    @wraps(view)
    def wrapped(*args, **kwargs):
        if not _session().get("user"):
            return _deny(
                "Log ind for at fortsætte.",
                401,
                "auth.login",
            )
        denied = _ensure_live_or_deny()
        if denied is not None:
            return denied
        return view(*args, **kwargs)

    return wrapped


# ---------------------------------------------------------------------------
# require_role
# ---------------------------------------------------------------------------

def require_role(*roles):
    """Require an authenticated user whose ``session['role']`` is in ``roles``.

    Platform super-admin (``role == 'admin'``) is treated as always allowed
    *unless* it is explicitly excluded by not appearing... — per spec the
    simplest rule is used: allow iff ``session['role'] in roles``. To keep the
    "admin is super-admin" behaviour, ``'admin'`` is additionally allowed unless
    the caller passed an explicit role list that omits it *and* the route is one
    that should bar admins. Since callers express that simply by listing roles,
    we allow when role is in ``roles`` OR role == 'admin'.

    Not logged in        -> 401 JSON / redirect to auth.login.
    Logged in, wrong role -> 403 JSON / redirect to dashboard.dashboard.
    """

    allowed = tuple(roles)

    def decorator(view):
        @wraps(view)
        def wrapped(*args, **kwargs):
            sess = _session()
            if not sess.get("user"):
                return _deny("Log ind for at fortsætte.", 401, "auth.login")

            denied = _ensure_live_or_deny()
            if denied is not None:
                return denied

            role = sess.get("role")
            if role in allowed or role == SUPER_ADMIN_ROLE:
                return view(*args, **kwargs)

            return _deny(
                "Du har ikke adgang til denne side.",
                403,
                "dashboard.dashboard",
            )

        return wrapped

    return decorator


# ---------------------------------------------------------------------------
# require_company
# ---------------------------------------------------------------------------

def require_company(view):
    """Require an authenticated user bound to a company (``session['company_id']``).

    Missing user        -> 401 JSON / redirect to auth.login.
    Missing company_id  -> 403 JSON / redirect to dashboard.dashboard.
    """

    @wraps(view)
    def wrapped(*args, **kwargs):
        sess = _session()
        if not sess.get("user"):
            return _deny("Log ind for at fortsætte.", 401, "auth.login")

        if not sess.get("company_id"):
            return _deny(
                "Du skal være tilknyttet en virksomhed for at se denne side.",
                403,
                "dashboard.dashboard",
            )
        denied = _ensure_live_or_deny()
        if denied is not None:
            return denied
        return view(*args, **kwargs)

    return wrapped


# ---------------------------------------------------------------------------
# require_company_role
# ---------------------------------------------------------------------------

def require_company_role(*roles):
    """Require an authenticated user whose ``session['company_role']`` is in
    ``roles`` (HR roles: company_admin, hr_manager, department_head).

    Platform super-admin (``role == 'admin'``) is allowed through regardless,
    so support staff are never locked out of tenant-scoped admin pages.

    Not logged in              -> 401 JSON / redirect to auth.login.
    Wrong company_role         -> 403 JSON / redirect to dashboard.dashboard.
    """

    allowed = tuple(roles)

    def decorator(view):
        @wraps(view)
        def wrapped(*args, **kwargs):
            sess = _session()
            if not sess.get("user"):
                return _deny("Log ind for at fortsætte.", 401, "auth.login")

            # Platform super-admin bypass.
            if sess.get("role") == SUPER_ADMIN_ROLE:
                return view(*args, **kwargs)

            denied = _ensure_live_or_deny()
            if denied is not None:
                return denied

            if sess.get("company_role") in allowed:
                return view(*args, **kwargs)

            return _deny(
                "Du har ikke de nødvendige rettigheder i virksomheden.",
                403,
                "dashboard.dashboard",
            )

        return wrapped

    return decorator


# ---------------------------------------------------------------------------
# requires_feature  (plan-tier gating; FAILS OPEN for now)
# ---------------------------------------------------------------------------

def _resolve_company_features(company_id):
    """Best-effort resolution of a tenant's feature flags as a dict.

    Reads ``companies.features`` (a JSON column) for ``company_id``. Returns a
    dict, or ``None`` if features could not be resolved (caller fails open on
    ``None``). Never raises.
    """
    if not company_id:
        return None

    try:
        import json

        from flask import current_app

        mysql = getattr(current_app, "mysql", None)
        if mysql is None:
            return None

        # Heal a possibly-stale connection the same way the rest of the app does.
        try:
            from db_compat import refresh_flask_mysql_connection

            refresh_flask_mysql_connection(mysql)
        except Exception:
            # If the compat helper is unavailable, carry on with the raw conn.
            pass

        conn = mysql.connection
        cur = conn.cursor()
        try:
            cur.execute(
                "SELECT features FROM companies WHERE id = %s LIMIT 1",
                (company_id,),
            )
            row = cur.fetchone()
        finally:
            try:
                cur.close()
            except Exception:
                pass

        if not row:
            return None

        # DictCursor is the default -> read by column name; tolerate tuple too.
        if isinstance(row, dict):
            raw = row.get("features")
        else:
            raw = row[0]

        if raw is None:
            return {}
        if isinstance(raw, dict):
            return raw
        if isinstance(raw, (bytes, bytearray)):
            raw = raw.decode("utf-8", "replace")
        if isinstance(raw, str):
            raw = raw.strip()
            if not raw:
                return {}
            parsed = json.loads(raw)
            return parsed if isinstance(parsed, dict) else None
        return None
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("Kunne ikke hente feature-flags: %s", exc)
        return None


def requires_feature(flag):
    """Gate a route behind a plan-tier feature flag.

    Allows the request when the tenant's ``features`` JSON has ``flag`` set to a
    truthy value. **Fails open** for now: if the user is anonymous, has no
    company, or the flags cannot be resolved for any reason, the request is
    ALLOWED and a warning is logged. The paywall wave will tighten this.

    The only case that is actively *blocked* is: flags resolved successfully and
    the flag is explicitly present-and-falsy / absent in a resolved flag set.
    """

    def decorator(view):
        @wraps(view)
        def wrapped(*args, **kwargs):
            try:
                sess = _session()
                company_id = sess.get("company_id")

                features = _resolve_company_features(company_id)
                if features is None:
                    # Could not resolve -> fail open (allow) and log.
                    logger.warning(
                        "requires_feature(%s): flags uopløselige for company_id=%r "
                        "- tillader (fail-open)",
                        flag,
                        company_id,
                    )
                    return view(*args, **kwargs)

                if features.get(flag):
                    return view(*args, **kwargs)

                # Flags resolved and feature is off -> block.
                return _deny(
                    "Denne funktion er ikke tilgængelig på jeres nuværende abonnement.",
                    403,
                    "dashboard.dashboard",
                )
            except Exception as exc:  # pragma: no cover - defensive
                # Any unexpected error must not break prod: fail open.
                logger.warning(
                    "requires_feature(%s) fejlede uventet (%s) - tillader (fail-open)",
                    flag,
                    exc,
                )
                return view(*args, **kwargs)

        return wrapped

    return decorator


__all__ = [
    "login_required",
    "require_role",
    "require_company",
    "require_company_role",
    "requires_feature",
    "COMPANY_ROLES",
    "SUPER_ADMIN_ROLE",
    "session_is_live",
    "register_session_liveness",
    "can",
    "department_scope",
    "capabilities_for",
    "require_capability",
    "register_capability_context",
    "CAPABILITY_MIN_ROLE",
    "ROLE_RANK",
    "invalidate_session_cache",
    "revoke_session",
]
