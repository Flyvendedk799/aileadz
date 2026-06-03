# scim_api.py
"""
SCIM 2.0 provisioning / deprovisioning for aileadz (RFC 7643 / RFC 7644).

This module exposes a small, standards-shaped SCIM 2.0 *User* surface so that
enterprise IdPs / HRIS systems (Okta, Entra ID, OneLogin, Workday, …) can
automatically provision and DEPROVISION (auto-offboard) seats into the tenant's
``company_users`` roster.

Authentication reuses the EXISTING enterprise API auth (``require_api_auth``
from ``enterprise_api``): SCIM clients authenticate with the company's API key
(``X-API-Key`` header or ``api_key`` query param, exactly like the rest of the
enterprise API). That decorator sets ``g.company_id``, so EVERY operation in
this module is hard-scoped to the authenticated tenant — there are no foreign
keys, isolation is application-level, and every query carries
``WHERE company_id = g.company_id``.

Design rules (see CLAUDE.md):
  * Import-safe: nothing at module scope can crash ``create_app()``. Optional
    deps (the auth decorator, seat governance, the event bus) are guarded; if
    the auth decorator is missing we fall back to a decorator that returns a
    proper SCIM 503 error envelope rather than crashing the blueprint.
  * Backward-compatible + additive: a brand-new blueprint, new URL space
    (``/scim/v2/...``); touches no existing routes.
  * DB conventions: ``current_app.mysql.connection`` with a DictCursor (rows
    read BY COLUMN NAME); ``autocommit=False`` so we ``commit()`` manually.
  * Seat governance: provisioning honours ``seat_governance.can_add_employee``
    (guarded, fail-open) and returns a SCIM 403 when seats/trial block the add.
  * stdlib / json only.
  * User-facing ``detail`` strings on errors are Danish where it makes sense,
    while still being valid SCIM error envelopes.
"""

import json
import logging

from flask import Blueprint, request, current_app, g, Response, url_for

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Guarded optional imports. None of these may crash create_app().
# ---------------------------------------------------------------------------

# DictCursor: prefer MySQLdb's, but the project may run on PyMySQL (see run.py).
try:  # pragma: no cover - import-safety guard
    import MySQLdb.cursors as _mysql_cursors
except Exception:  # pragma: no cover
    _mysql_cursors = None

# Reuse the enterprise API key auth decorator so SCIM shares the tenant's key,
# rate-limit, lockout + g.company_id wiring. If it can't be imported we install
# a safe fallback that emits a SCIM-shaped 503 instead of exploding the route.
try:  # pragma: no cover - import-safety guard
    from enterprise_api import require_api_auth as _require_api_auth
except Exception as _auth_err:  # pragma: no cover
    _require_api_auth = None
    logging.getLogger(__name__).warning(
        "scim_api: enterprise_api.require_api_auth unavailable (%s); SCIM "
        "endpoints will return 503 until it loads.", _auth_err
    )

# Seat / subscription governance (guarded; fails open internally).
try:  # pragma: no cover - import-safety guard
    import seat_governance as _seat_governance
except Exception:  # pragma: no cover
    _seat_governance = None

# Integration event bus / outbox (guarded; best-effort, never blocks the op).
try:  # pragma: no cover - import-safety guard
    import event_bus as _event_bus
except Exception:  # pragma: no cover
    _event_bus = None


scim_bp = Blueprint("scim", __name__)

# Canonical SCIM schema URNs (RFC 7643).
SCHEMA_USER = "urn:ietf:params:scim:schemas:core:2.0:User"
SCHEMA_LIST_RESPONSE = "urn:ietf:params:scim:api:messages:2.0:ListResponse"
SCHEMA_ERROR = "urn:ietf:params:scim:api:messages:2.0:Error"
SCHEMA_PATCH_OP = "urn:ietf:params:scim:api:messages:2.0:PatchOp"

# SCIM content type (RFC 7644 §3.1).
SCIM_CONTENT_TYPE = "application/scim+json"

# Defensive default + ceiling for list page size.
DEFAULT_COUNT = 100
MAX_COUNT = 200


# ---------------------------------------------------------------------------
# Auth wiring
# ---------------------------------------------------------------------------

def _scim_auth(permission):
    """Return the enterprise auth decorator, or a SCIM-503 fallback.

    Reusing ``require_api_auth`` means SCIM clients authenticate with the
    company's API key and inherit ``g.company_id`` + rate limiting. When the
    decorator failed to import we still want the blueprint to register cleanly,
    so we hand back a decorator that responds with a valid SCIM error envelope.
    """
    if _require_api_auth is not None:
        return _require_api_auth(permission)

    def _fallback_decorator(f):
        from functools import wraps

        @wraps(f)
        def _unavailable(*args, **kwargs):
            return _scim_error(
                503,
                "SCIM provisioning er midlertidigt utilgængelig "
                "(authentication unavailable).",
            )

        return _unavailable

    return _fallback_decorator


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------

def _cursor():
    """Return a DictCursor on the request connection (rows read by name).

    Mirrors the enterprise_api convention. Raises only if there is genuinely no
    DB; callers run inside try/except and convert to a SCIM 500.
    """
    conn = current_app.mysql.connection
    if _mysql_cursors is not None:
        return conn.cursor(_mysql_cursors.DictCursor)
    # PyMySQL fallback shim (run.py installs DictCursor as the default).
    try:
        return conn.cursor(dictionary=True)
    except TypeError:
        return conn.cursor()


def _recalc_employee_count(cur, company_id):
    """Resync companies.current_employee_count from live active rows.

    Used after both provisioning and deprovisioning so the cached count never
    drifts. Matches the recalc pattern used elsewhere in the enterprise API.
    Best-effort: never raises (the column should exist, but we guard anyway).
    """
    try:
        cur.execute(
            """
            UPDATE companies
               SET current_employee_count = (
                   SELECT COUNT(*) FROM company_users
                    WHERE company_id = %s AND status = 'active'
               )
             WHERE id = %s
            """,
            (company_id, company_id),
        )
    except Exception as e:  # pragma: no cover - defensive
        logger.warning("scim_api: could not recalc employee count: %s", e)


# ---------------------------------------------------------------------------
# SCIM response helpers
# ---------------------------------------------------------------------------

def _scim_response(payload, status=200, extra_headers=None):
    """Serialize a dict to an application/scim+json Response."""
    body = json.dumps(payload, default=str, ensure_ascii=False)
    resp = Response(body, status=status, mimetype=SCIM_CONTENT_TYPE)
    if extra_headers:
        for k, v in extra_headers.items():
            resp.headers[k] = v
    return resp


def _scim_error(status, detail, scim_type=None):
    """Build a SCIM Error envelope (RFC 7644 §3.12)."""
    payload = {
        "schemas": [SCHEMA_ERROR],
        "status": str(status),
        "detail": detail,
    }
    if scim_type:
        payload["scimType"] = scim_type
    return _scim_response(payload, status=status)


def _resource_location(user_id):
    """Best-effort absolute/relative URL for a User resource's meta.location."""
    try:
        return url_for("scim.get_user", user_id=str(user_id), _external=False)
    except Exception:  # pragma: no cover - url_for needs a request ctx
        return "/scim/v2/Users/%s" % user_id


def _split_name(full_name):
    """Split a stored full_name into (given, family) for SCIM name.* output."""
    full_name = (full_name or "").strip()
    if not full_name:
        return "", ""
    parts = full_name.split()
    if len(parts) == 1:
        return parts[0], ""
    return parts[0], " ".join(parts[1:])


def _row_to_scim_user(row):
    """Map a company_users DictCursor row -> a SCIM 2.0 User resource."""
    user_id = str(row.get("id"))
    email = row.get("email") or ""
    username = row.get("username") or email
    full_name = row.get("full_name") or ""
    given, family = _split_name(full_name)
    status = (row.get("status") or "active").strip().lower()
    active = status == "active"

    created = row.get("added_at")
    modified = row.get("updated_at") or created

    resource = {
        "schemas": [SCHEMA_USER],
        "id": user_id,
        "userName": username,
        "active": active,
        "meta": {
            "resourceType": "User",
            "location": _resource_location(user_id),
        },
    }
    if created is not None:
        resource["meta"]["created"] = created
    if modified is not None:
        resource["meta"]["lastModified"] = modified

    # Name sub-attributes (RFC 7643 §4.1.1).
    name_obj = {"formatted": full_name}
    if given:
        name_obj["givenName"] = given
    if family:
        name_obj["familyName"] = family
    resource["name"] = name_obj
    if full_name:
        resource["displayName"] = full_name

    # Emails (multi-valued, mark primary).
    if email:
        resource["emails"] = [{"value": email, "primary": True}]

    # Enterprise-ish extras surfaced as core/IdP-friendly fields.
    if row.get("job_title"):
        resource["title"] = row.get("job_title")
    ext_id = row.get("employee_id")
    if ext_id:
        resource["externalId"] = str(ext_id)

    return resource


# ---------------------------------------------------------------------------
# Request-body parsing helpers
# ---------------------------------------------------------------------------

def _get_json_body():
    """Parse the request body as JSON, tolerating the SCIM content type.

    Returns (data, error_response). ``data`` is a dict on success.
    """
    try:
        data = request.get_json(force=True, silent=True)
    except Exception:
        data = None
    if not isinstance(data, dict):
        return None, _scim_error(
            400, "Ugyldig eller manglende JSON-body.", scim_type="invalidSyntax"
        )
    return data, None


def _full_name_from_scim(data, fallback=None):
    """Derive a full_name from a SCIM User payload.

    Prefers ``name.givenName`` + ``name.familyName``; falls back to
    ``name.formatted``, then ``displayName``, then the supplied fallback.
    """
    name = data.get("name") if isinstance(data.get("name"), dict) else {}
    given = (name.get("givenName") or "").strip()
    family = (name.get("familyName") or "").strip()
    combined = " ".join(p for p in (given, family) if p).strip()
    if combined:
        return combined
    formatted = (name.get("formatted") or "").strip()
    if formatted:
        return formatted
    display = (data.get("displayName") or "").strip()
    if display:
        return display
    return (fallback or "").strip()


def _primary_email_from_scim(data):
    """Extract the primary (or first) email value from a SCIM User payload."""
    emails = data.get("emails")
    if isinstance(emails, list) and emails:
        primary = None
        first = None
        for e in emails:
            if not isinstance(e, dict):
                continue
            val = (e.get("value") or "").strip()
            if not val:
                continue
            if first is None:
                first = val
            if e.get("primary"):
                primary = val
                break
        return (primary or first or "").strip()
    return ""


def _coerce_bool(value, default=None):
    """SCIM ``active`` can arrive as a real bool or a string ("true"/"false")."""
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        v = value.strip().lower()
        if v in ("true", "1", "yes"):
            return True
        if v in ("false", "0", "no"):
            return False
    return default


# ---------------------------------------------------------------------------
# Filter parsing for GET /Users?filter=userName eq "x"
# ---------------------------------------------------------------------------

def _parse_username_filter(filter_str):
    """Parse the common SCIM filter ``userName eq "value"`` (also externalId).

    IdPs almost always use exactly this form to check existence before a POST.
    We support ``eq`` on ``userName``, ``emails.value`` and ``externalId``.
    Returns (attribute, value) or (None, None) if unsupported/unparseable.
    """
    if not filter_str:
        return None, None
    s = filter_str.strip()
    low = s.lower()
    # Find " eq " operator.
    idx = low.find(" eq ")
    if idx == -1:
        return None, None
    attr = s[:idx].strip()
    rhs = s[idx + 4:].strip()
    # Strip surrounding quotes (single or double).
    if len(rhs) >= 2 and rhs[0] in "\"'" and rhs[-1] == rhs[0]:
        rhs = rhs[1:-1]
    attr_low = attr.lower()
    if attr_low in ("username", "emails.value", "emails", "externalid"):
        return attr_low, rhs
    return None, None


# ===========================================================================
# SCIM endpoints
# ===========================================================================

@scim_bp.route("/scim/v2/Users", methods=["GET"])
@_scim_auth("read:employees")
def list_users():
    """List provisioned users (SCIM ListResponse), company-scoped.

    Supports ``?filter=userName eq "x"`` (and emails.value / externalId) plus
    ``startIndex`` (1-based) and ``count`` pagination.
    """
    try:
        # Pagination (SCIM startIndex is 1-based).
        try:
            start_index = int(request.args.get("startIndex", 1))
        except (TypeError, ValueError):
            start_index = 1
        if start_index < 1:
            start_index = 1
        try:
            count = int(request.args.get("count", DEFAULT_COUNT))
        except (TypeError, ValueError):
            count = DEFAULT_COUNT
        if count < 0:
            count = 0
        count = min(count, MAX_COUNT)

        attr, value = _parse_username_filter(request.args.get("filter"))

        where = ["company_id = %s"]
        params = [g.company_id]
        if attr in ("username",):
            where.append("(username = %s OR email = %s)")
            params.extend([value, value])
        elif attr in ("emails.value", "emails"):
            where.append("email = %s")
            params.append(value)
        elif attr == "externalid":
            where.append("employee_id = %s")
            params.append(value)
        where_clause = " AND ".join(where)

        cur = _cursor()

        # Total count for the ListResponse envelope. where_clause is built
        # ONLY from fixed literals above; all user values are %s-bound params.
        cur.execute(
            "SELECT COUNT(*) AS total FROM company_users WHERE " + where_clause,
            params,
        )
        total_row = cur.fetchone()
        total = int((total_row or {}).get("total") or 0)

        resources = []
        if count > 0:
            offset = start_index - 1
            cur.execute(
                "SELECT id, company_id, user_id, username, full_name, email, "
                "job_title, employee_id, status, added_at, updated_at "
                "FROM company_users WHERE " + where_clause +
                " ORDER BY id ASC LIMIT %s OFFSET %s",
                params + [count, offset],
            )
            for row in cur.fetchall():
                resources.append(_row_to_scim_user(row))
        cur.close()

        payload = {
            "schemas": [SCHEMA_LIST_RESPONSE],
            "totalResults": total,
            "startIndex": start_index,
            "itemsPerPage": len(resources),
            "Resources": resources,
        }
        return _scim_response(payload, status=200)
    except Exception as e:
        logger.exception("scim_api.list_users failed: %s", e)
        return _scim_error(500, "Kunne ikke hente brugere.")


@scim_bp.route("/scim/v2/Users/<user_id>", methods=["GET"])
@_scim_auth("read:employees")
def get_user(user_id):
    """Fetch a single provisioned user, company-scoped."""
    try:
        cur = _cursor()
        cur.execute(
            "SELECT id, company_id, user_id, username, full_name, email, "
            "job_title, employee_id, status, added_at, updated_at "
            "FROM company_users WHERE id = %s AND company_id = %s",
            (user_id, g.company_id),
        )
        row = cur.fetchone()
        cur.close()
        if not row:
            return _scim_error(404, "Bruger ikke fundet.")
        return _scim_response(_row_to_scim_user(row), status=200)
    except Exception as e:
        logger.exception("scim_api.get_user failed: %s", e)
        return _scim_error(500, "Kunne ikke hente brugeren.")


@scim_bp.route("/scim/v2/Users", methods=["POST"])
@_scim_auth("write:employees")
def create_user():
    """Provision a new user into company_users (company-scoped).

    Honours seat governance: returns SCIM 403 when the company is out of seats
    or its trial has expired. Returns 409 when the userName/email already
    exists for this tenant (SCIM ``uniqueness`` semantics).
    """
    data, err = _get_json_body()
    if err:
        return err
    try:
        username = (data.get("userName") or "").strip()
        email = _primary_email_from_scim(data) or username
        if not username and email:
            username = email
        if not username:
            return _scim_error(
                400,
                "userName er påkrævet.",
                scim_type="invalidValue",
            )
        full_name = _full_name_from_scim(data, fallback=username)
        # SCIM active defaults to true on create when omitted.
        active = _coerce_bool(data.get("active"), default=True)
        status = "active" if active else "inactive"
        external_id = (data.get("externalId") or "").strip() or None
        title = (data.get("title") or "").strip() or None

        cur = _cursor()

        # Uniqueness: an existing row for this tenant (matched by userName or
        # email) is a SCIM 409. If it exists but is inactive, re-activate it so
        # re-provisioning a previously-offboarded identity works idempotently.
        cur.execute(
            "SELECT id, status FROM company_users "
            "WHERE company_id = %s AND (username = %s OR email = %s) LIMIT 1",
            (g.company_id, username, email),
        )
        existing = cur.fetchone()
        if existing:
            if (existing.get("status") or "").lower() == "active":
                cur.close()
                return _scim_error(
                    409,
                    "Bruger med dette userName/email findes allerede.",
                    scim_type="uniqueness",
                )
            # Reactivate a previously deprovisioned identity (idempotent POST).
            if active:
                if not _seat_check_ok():
                    cur.close()
                    ok, reason = _seat_status()
                    return _scim_error(
                        403,
                        reason or "Kan ikke tilføje flere medarbejdere "
                        "(seats opbrugt).",
                    )
                cur.execute(
                    "UPDATE company_users SET status = 'active', full_name = %s, "
                    "username = %s, email = %s WHERE id = %s AND company_id = %s",
                    (full_name, username, email, existing["id"], g.company_id),
                )
                _recalc_employee_count(cur, g.company_id)
            current_app.mysql.connection.commit()
            cur.execute(
                "SELECT id, company_id, user_id, username, full_name, email, "
                "job_title, employee_id, status, added_at, updated_at "
                "FROM company_users WHERE id = %s AND company_id = %s",
                (existing["id"], g.company_id),
            )
            row = cur.fetchone()
            cur.close()
            resource = _row_to_scim_user(row)
            return _scim_response(
                resource,
                status=200,
                extra_headers={"Location": resource["meta"]["location"]},
            )

        # New active provision -> seat governance gate.
        if active and not _seat_check_ok():
            cur.close()
            ok, reason = _seat_status()
            return _scim_error(
                403,
                reason or "Kan ikke tilføje flere medarbejdere "
                "(seats opbrugt).",
            )

        cur.execute(
            "INSERT INTO company_users "
            "(company_id, username, full_name, email, role, job_title, "
            " employee_id, status, added_by) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (
                g.company_id,
                username,
                full_name,
                email,
                "employee",
                title,
                external_id,
                status,
                _added_by(),
            ),
        )
        new_id = cur.lastrowid
        if status == "active":
            _recalc_employee_count(cur, g.company_id)
        current_app.mysql.connection.commit()

        cur.execute(
            "SELECT id, company_id, user_id, username, full_name, email, "
            "job_title, employee_id, status, added_at, updated_at "
            "FROM company_users WHERE id = %s AND company_id = %s",
            (new_id, g.company_id),
        )
        row = cur.fetchone()
        cur.close()

        _emit("scim.user.provisioned",
              {"user_id": new_id, "email": email, "userName": username})

        resource = _row_to_scim_user(row)
        return _scim_response(
            resource,
            status=201,
            extra_headers={"Location": resource["meta"]["location"]},
        )
    except Exception as e:
        logger.exception("scim_api.create_user failed: %s", e)
        try:
            current_app.mysql.connection.rollback()
        except Exception:
            pass
        return _scim_error(500, "Kunne ikke oprette brugeren.")


@scim_bp.route("/scim/v2/Users/<user_id>", methods=["PUT"])
@_scim_auth("write:employees")
def put_user(user_id):
    """Full replace of a user (SCIM PUT). Drives provision/deprovision via active."""
    data, err = _get_json_body()
    if err:
        return err
    try:
        cur = _cursor()
        cur.execute(
            "SELECT id, status, username, email, full_name FROM company_users "
            "WHERE id = %s AND company_id = %s",
            (user_id, g.company_id),
        )
        row = cur.fetchone()
        if not row:
            cur.close()
            return _scim_error(404, "Bruger ikke fundet.")

        was_active = (row.get("status") or "").lower() == "active"
        username = (data.get("userName") or row.get("username") or "").strip()
        email = _primary_email_from_scim(data) or row.get("email") or ""
        full_name = _full_name_from_scim(
            data, fallback=row.get("full_name") or username
        )
        active = _coerce_bool(data.get("active"), default=was_active)
        new_status = "active" if active else "inactive"

        # Re-activating an inactive seat must pass seat governance.
        if active and not was_active and not _seat_check_ok():
            cur.close()
            ok, reason = _seat_status()
            return _scim_error(
                403,
                reason or "Kan ikke genaktivere medarbejder (seats opbrugt).",
            )

        cur.execute(
            "UPDATE company_users SET username = %s, email = %s, "
            "full_name = %s, status = %s WHERE id = %s AND company_id = %s",
            (username, email, full_name, new_status, user_id, g.company_id),
        )
        if was_active != active:
            _recalc_employee_count(cur, g.company_id)
        current_app.mysql.connection.commit()

        cur.execute(
            "SELECT id, company_id, user_id, username, full_name, email, "
            "job_title, employee_id, status, added_at, updated_at "
            "FROM company_users WHERE id = %s AND company_id = %s",
            (user_id, g.company_id),
        )
        updated = cur.fetchone()
        cur.close()

        if was_active and not active:
            _emit("scim.user.deprovisioned",
                  {"user_id": row.get("id"), "email": email})

        return _scim_response(_row_to_scim_user(updated), status=200)
    except Exception as e:
        logger.exception("scim_api.put_user failed: %s", e)
        try:
            current_app.mysql.connection.rollback()
        except Exception:
            pass
        return _scim_error(500, "Kunne ikke opdatere brugeren.")


@scim_bp.route("/scim/v2/Users/<user_id>", methods=["PATCH"])
@_scim_auth("write:employees")
def patch_user(user_id):
    """Partial update (SCIM PatchOp). The canonical deprovision path:

    Okta/Entra send ``{"op":"replace","path":"active","value":false}`` (or a
    no-path replace ``{"active": false}``) to offboard. We apply ``active``,
    ``userName``, ``displayName`` and ``name.*`` operations.
    """
    data, err = _get_json_body()
    if err:
        return err
    try:
        cur = _cursor()
        cur.execute(
            "SELECT id, status, username, email, full_name FROM company_users "
            "WHERE id = %s AND company_id = %s",
            (user_id, g.company_id),
        )
        row = cur.fetchone()
        if not row:
            cur.close()
            return _scim_error(404, "Bruger ikke fundet.")

        was_active = (row.get("status") or "").lower() == "active"
        new_active = was_active
        new_username = row.get("username")
        new_email = row.get("email")
        new_full_name = row.get("full_name")

        operations = data.get("Operations")
        if not isinstance(operations, list):
            cur.close()
            return _scim_error(
                400,
                "PatchOp kræver et 'Operations'-array.",
                scim_type="invalidSyntax",
            )

        for op in operations:
            if not isinstance(op, dict):
                continue
            verb = (op.get("op") or "").strip().lower()
            path = (op.get("path") or "").strip()
            value = op.get("value")

            if verb == "remove":
                # The only common removal IdPs perform is clearing 'active',
                # which we treat as deprovision.
                if path.lower() == "active":
                    new_active = False
                continue

            # add / replace
            if path:
                p = path.lower()
                if p == "active":
                    new_active = _coerce_bool(value, default=new_active)
                elif p in ("username",):
                    new_username = (value or "").strip() or new_username
                elif p in ("displayname", "name.formatted"):
                    nv = (value or "").strip()
                    if nv:
                        new_full_name = nv
                elif p == "name.givenname":
                    _g, _f = _split_name(new_full_name)
                    new_full_name = " ".join(
                        x for x in ((value or "").strip(), _f) if x
                    ).strip() or new_full_name
                elif p == "name.familyname":
                    _g, _f = _split_name(new_full_name)
                    new_full_name = " ".join(
                        x for x in (_g, (value or "").strip()) if x
                    ).strip() or new_full_name
                elif p in ("emails", "emails.value", "emails[primary eq true].value"):
                    if isinstance(value, list):
                        ev = _primary_email_from_scim({"emails": value})
                        if ev:
                            new_email = ev
                    elif isinstance(value, str) and value.strip():
                        new_email = value.strip()
            elif isinstance(value, dict):
                # No-path replace: value is a partial User resource.
                if "active" in value:
                    new_active = _coerce_bool(value.get("active"), default=new_active)
                fn = _full_name_from_scim(value, fallback=None)
                if fn:
                    new_full_name = fn
                un = (value.get("userName") or "").strip()
                if un:
                    new_username = un
                em = _primary_email_from_scim(value)
                if em:
                    new_email = em

        new_status = "active" if new_active else "inactive"

        # Re-activation must clear seat governance.
        if new_active and not was_active and not _seat_check_ok():
            cur.close()
            ok, reason = _seat_status()
            return _scim_error(
                403,
                reason or "Kan ikke genaktivere medarbejder (seats opbrugt).",
            )

        cur.execute(
            "UPDATE company_users SET username = %s, email = %s, "
            "full_name = %s, status = %s WHERE id = %s AND company_id = %s",
            (new_username, new_email, new_full_name, new_status,
             user_id, g.company_id),
        )
        if was_active != new_active:
            _recalc_employee_count(cur, g.company_id)
        current_app.mysql.connection.commit()

        cur.execute(
            "SELECT id, company_id, user_id, username, full_name, email, "
            "job_title, employee_id, status, added_at, updated_at "
            "FROM company_users WHERE id = %s AND company_id = %s",
            (user_id, g.company_id),
        )
        updated = cur.fetchone()
        cur.close()

        if was_active and not new_active:
            _emit("scim.user.deprovisioned",
                  {"user_id": row.get("id"), "email": new_email})

        return _scim_response(_row_to_scim_user(updated), status=200)
    except Exception as e:
        logger.exception("scim_api.patch_user failed: %s", e)
        try:
            current_app.mysql.connection.rollback()
        except Exception:
            pass
        return _scim_error(500, "Kunne ikke opdatere brugeren.")


@scim_bp.route("/scim/v2/Users/<user_id>", methods=["DELETE"])
@_scim_auth("write:employees")
def delete_user(user_id):
    """Deprovision (soft-delete) a user: status -> 'inactive', company-scoped.

    SCIM DELETE returns 204 No Content. We soft-delete (set status='inactive')
    rather than hard-delete so learning history / analytics remain intact —
    this is the auto-offboarding path enterprise customers rely on. Idempotent:
    deleting an already-inactive or absent user still succeeds.
    """
    try:
        cur = _cursor()
        cur.execute(
            "SELECT id, status, email FROM company_users "
            "WHERE id = %s AND company_id = %s",
            (user_id, g.company_id),
        )
        row = cur.fetchone()
        if not row:
            cur.close()
            # Idempotent / company-scoped: unknown id is a clean 404 per SCIM.
            return _scim_error(404, "Bruger ikke fundet.")

        was_active = (row.get("status") or "").lower() == "active"
        if was_active:
            cur.execute(
                "UPDATE company_users SET status = 'inactive' "
                "WHERE id = %s AND company_id = %s",
                (user_id, g.company_id),
            )
            _recalc_employee_count(cur, g.company_id)
            current_app.mysql.connection.commit()
            _emit("scim.user.deprovisioned",
                  {"user_id": row.get("id"), "email": row.get("email")})
        cur.close()

        # 204 No Content (RFC 7644 §3.6).
        return Response(status=204)
    except Exception as e:
        logger.exception("scim_api.delete_user failed: %s", e)
        try:
            current_app.mysql.connection.rollback()
        except Exception:
            pass
        return _scim_error(500, "Kunne ikke deaktivere brugeren.")


@scim_bp.route("/scim/v2/ServiceProviderConfig", methods=["GET"])
@_scim_auth("read:employees")
def service_provider_config():
    """Advertise SCIM capabilities so IdPs can discover what we support."""
    cfg = {
        "schemas": [
            "urn:ietf:params:scim:schemas:core:2.0:ServiceProviderConfig"
        ],
        "documentationUri": "https://aileadz.dk/docs/scim",
        "patch": {"supported": True},
        "bulk": {"supported": False, "maxOperations": 0, "maxPayloadSize": 0},
        "filter": {"supported": True, "maxResults": MAX_COUNT},
        "changePassword": {"supported": False},
        "sort": {"supported": False},
        "etag": {"supported": False},
        "authenticationSchemes": [
            {
                "type": "oauthbearertoken",
                "name": "API Key",
                "description": "Authenticate with the company's enterprise API "
                               "key via the X-API-Key header.",
                "primary": True,
            }
        ],
        "meta": {"resourceType": "ServiceProviderConfig"},
    }
    return _scim_response(cfg, status=200)


# ---------------------------------------------------------------------------
# Seat governance + event-bus shims (all guarded / fail-open)
# ---------------------------------------------------------------------------

def _seat_check_ok():
    """True if the company may add one more active seat. Fails OPEN."""
    if _seat_governance is None:
        return True
    try:
        ok, _reason = _seat_governance.can_add_employee(g.company_id)
        return bool(ok)
    except Exception:
        return True


def _seat_status():
    """Return (ok, reason) for a blocked add, for the SCIM error detail."""
    if _seat_governance is None:
        return True, None
    try:
        return _seat_governance.can_add_employee(g.company_id)
    except Exception:
        return True, None


def _added_by():
    """Best-effort 'added_by' from the authenticating API key (may be absent)."""
    try:
        key_data = getattr(g, "api_key_data", None) or {}
        return key_data.get("created_by")
    except Exception:
        return None


def _emit(event_type, payload):
    """Best-effort durable event emission; never blocks the SCIM op."""
    if _event_bus is None:
        return
    try:
        _event_bus.emit_event(g.company_id, event_type, payload)
    except Exception as e:  # pragma: no cover - best-effort
        logger.debug("scim_api: emit_event(%s) skipped: %s", event_type, e)
