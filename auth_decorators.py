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
]
