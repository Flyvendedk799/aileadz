"""
Isolated authentication for VENDOR (supplier) accounts.

Vendors are a completely separate identity from platform users / company
employees. A logged-in vendor sets::

    session['user_type'] = 'vendor'
    session['vendor_id']  = <vendors.id>
    session['vendor_name'] = <vendors.vendor_name>

and MUST NEVER set ``session['user']``. That single rule guarantees a vendor
can never pass the regular ``login_required`` / ``require_role`` /
``require_company*`` guards in ``auth_decorators.py`` (all of which key off
``session['user']``) and therefore can never reach employee / HR / admin
surfaces.

Design rules (production-safe, mirrors auth_decorators.py):

* Importing this module must NEVER require a Flask app context and must NEVER
  crash ``create_app()``. ``flask`` and the password backend are imported
  lazily / guarded so importing this module is side-effect free.
* Password hashing prefers ``werkzeug.security`` (the rest of the app uses it
  and stores ``pbkdf2:``/``scrypt:`` hashes). If werkzeug is unavailable we
  fall back to a self-contained ``hashlib.pbkdf2_hmac`` implementation that is
  interoperable in *spirit* (salted PBKDF2) but uses a distinct ``pbkdf2$``
  prefix so the two formats never collide.
* All user-facing strings are Danish.

Exports:

    hash_vendor_password(password) -> str
    verify_vendor_password(password, stored_hash) -> bool
    authenticate_vendor(email, password) -> vendor_row dict | None
    current_vendor() -> vendor_row dict | None
    set_vendor_session(vendor_row) -> None
    vendor_login_required(view) -> view
"""

from functools import wraps
import hashlib
import hmac
import logging
import os

logger = logging.getLogger(__name__)

# Endpoint we redirect anonymous vendors to. The vendor blueprint (owned by a
# sibling task) registers ``vendor.login``; we degrade gracefully if it is not
# registered yet so this module is safe to import/use before that lands.
VENDOR_LOGIN_ENDPOINT = "vendor.login"

# Self-contained PBKDF2 fallback parameters (only used when werkzeug is absent).
_PBKDF2_PREFIX = "pbkdf2$"
_PBKDF2_ALGO = "sha256"
_PBKDF2_ITERATIONS = 240000
_PBKDF2_SALT_BYTES = 16


# ---------------------------------------------------------------------------
# Lazy flask helpers (module import stays context-free)
# ---------------------------------------------------------------------------

def _session():
    """Return the Flask session, or an empty dict if unavailable."""
    try:
        from flask import session

        return session
    except Exception:
        return {}


def _wants_json():
    """True when the current request should get a JSON error rather than a
    redirect (API / XHR callers). Defensive: no request context -> False."""
    try:
        from flask import request

        path = request.path or ""
        if path.startswith("/api"):
            return True
        if getattr(request, "is_json", False):
            return True
        accept = request.accept_mimetypes
        if accept and accept.accept_json and not accept.accept_html:
            return True
        if request.headers.get("X-Requested-With") == "XMLHttpRequest":
            return True
    except Exception:
        return False
    return False


def _flash(message, category="warning"):
    try:
        from flask import flash

        flash(message, category)
    except Exception:
        pass


def _redirect_to_login():
    """Redirect to the vendor login endpoint, degrading gracefully."""
    from flask import redirect, url_for

    try:
        return redirect(url_for(VENDOR_LOGIN_ENDPOINT))
    except Exception:
        # Blueprint not registered (yet) or no request context: send the
        # vendor somewhere safe rather than 500-ing inside an auth guard.
        try:
            return redirect("/vendor/login")
        except Exception:
            return ("Unauthorized", 401)


def _json_error(message, status):
    from flask import jsonify

    resp = jsonify({"error": message, "status": status})
    resp.status_code = status
    return resp


# ---------------------------------------------------------------------------
# Password hashing  (werkzeug preferred, guarded hashlib.pbkdf2 fallback)
# ---------------------------------------------------------------------------

def hash_vendor_password(password):
    """Return a salted password hash for ``password``.

    Prefers ``werkzeug.security.generate_password_hash`` (produces ``pbkdf2:``
    / ``scrypt:`` strings, matching the rest of the platform). Falls back to a
    self-contained PBKDF2-HMAC-SHA256 hash with a distinct ``pbkdf2$`` prefix
    if werkzeug is unavailable. Never raises for normal string input.
    """
    if password is None:
        password = ""
    try:
        from werkzeug.security import generate_password_hash

        return generate_password_hash(password)
    except Exception:
        # Self-contained fallback.
        salt = os.urandom(_PBKDF2_SALT_BYTES)
        dk = hashlib.pbkdf2_hmac(
            _PBKDF2_ALGO,
            password.encode("utf-8"),
            salt,
            _PBKDF2_ITERATIONS,
        )
        return "%s%s$%d$%s$%s" % (
            _PBKDF2_PREFIX,
            _PBKDF2_ALGO,
            _PBKDF2_ITERATIONS,
            salt.hex(),
            dk.hex(),
        )


def _verify_fallback(password, stored_hash):
    """Verify a hash produced by the ``pbkdf2$`` fallback format."""
    try:
        # Format: pbkdf2$<algo>$<iterations>$<salt_hex>$<dk_hex>
        body = stored_hash[len(_PBKDF2_PREFIX):]
        algo, iters_s, salt_hex, dk_hex = body.split("$", 3)
        iterations = int(iters_s)
        salt = bytes.fromhex(salt_hex)
        expected = bytes.fromhex(dk_hex)
        computed = hashlib.pbkdf2_hmac(
            algo, password.encode("utf-8"), salt, iterations
        )
        return hmac.compare_digest(computed, expected)
    except Exception:
        return False


def verify_vendor_password(password, stored_hash):
    """Return True iff ``password`` matches ``stored_hash``.

    Handles both werkzeug-format hashes (``pbkdf2:`` / ``scrypt:`` …) and the
    self-contained ``pbkdf2$`` fallback format. Never raises; returns False on
    any error or missing input.
    """
    if not stored_hash or password is None:
        return False
    try:
        if isinstance(stored_hash, (bytes, bytearray)):
            stored_hash = stored_hash.decode("utf-8", "replace")
    except Exception:
        return False

    # Our self-contained fallback format.
    if stored_hash.startswith(_PBKDF2_PREFIX):
        return _verify_fallback(password, stored_hash)

    # Otherwise defer to werkzeug (covers pbkdf2:, scrypt:, etc.).
    try:
        from werkzeug.security import check_password_hash

        return check_password_hash(stored_hash, password)
    except Exception:
        return False


# ---------------------------------------------------------------------------
# DB access (guarded; mirrors how the rest of the app reaches MySQL)
# ---------------------------------------------------------------------------

def _dict_cursor():
    """Return a DictCursor on the request connection, or None on any failure.

    Heals a possibly-stale connection via db_compat like the rest of the app.
    Never raises.
    """
    try:
        from flask import current_app

        mysql = getattr(current_app, "mysql", None)
        if mysql is None:
            return None

        try:
            from db_compat import refresh_flask_mysql_connection

            refresh_flask_mysql_connection(mysql)
        except Exception:
            pass

        conn = mysql.connection
        try:
            import MySQLdb

            return conn.cursor(MySQLdb.cursors.DictCursor)
        except Exception:
            # DictCursor is the app-wide default, so a bare cursor() is fine.
            return conn.cursor()
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("vendor_auth: kunne ikke åbne database-cursor: %s", exc)
        return None


def _fetch_vendor_by_email(email):
    """Return the vendor row dict for ``contact_email`` (any status), or None."""
    if not email:
        return None
    cur = _dict_cursor()
    if cur is None:
        return None
    try:
        cur.execute(
            "SELECT * FROM vendors WHERE contact_email = %s LIMIT 1",
            (email,),
        )
        row = cur.fetchone()
        return row if isinstance(row, dict) else None
    except Exception as exc:
        logger.warning("vendor_auth: opslag på e-mail fejlede: %s", exc)
        return None
    finally:
        try:
            cur.close()
        except Exception:
            pass


def _fetch_vendor_by_id(vendor_id):
    """Return the vendor row dict for ``vendors.id``, or None."""
    if not vendor_id:
        return None
    cur = _dict_cursor()
    if cur is None:
        return None
    try:
        cur.execute(
            "SELECT * FROM vendors WHERE id = %s LIMIT 1",
            (vendor_id,),
        )
        row = cur.fetchone()
        return row if isinstance(row, dict) else None
    except Exception as exc:
        logger.warning("vendor_auth: opslag på id fejlede: %s", exc)
        return None
    finally:
        try:
            cur.close()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Authentication
# ---------------------------------------------------------------------------

def authenticate_vendor(email, password):
    """Authenticate a vendor by ``contact_email`` + ``password``.

    Returns the vendor row dict iff a vendor with that email exists, has
    ``status == 'active'``, and the password verifies against
    ``password_hash``. Returns None otherwise. Never raises.
    """
    try:
        if not email or password is None:
            return None

        vendor = _fetch_vendor_by_email(email)
        if not vendor:
            return None

        # Must be an ACTIVE account (pending/suspended cannot log in).
        if (vendor.get("status") or "").strip().lower() != "active":
            return None

        if not verify_vendor_password(password, vendor.get("password_hash")):
            return None

        return vendor
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("vendor_auth: authenticate_vendor fejlede: %s", exc)
        return None


# ---------------------------------------------------------------------------
# Session helpers
# ---------------------------------------------------------------------------

def set_vendor_session(vendor_row):
    """Establish an isolated vendor session from ``vendor_row``.

    Sets ``user_type='vendor'``, ``vendor_id`` and ``vendor_name`` and clears
    any stray ``user`` key so the vendor can never pass the regular
    ``login_required`` guards. No-op-safe if there is no session context.
    """
    if not vendor_row:
        return
    sess = _session()
    try:
        # Hard isolation: a vendor must never look like a platform user.
        sess.pop("user", None)
        sess.pop("user_id", None)
        sess.pop("role", None)
        sess.pop("company_id", None)
        sess.pop("company_role", None)
        sess.pop("company_name", None)

        sess["user_type"] = "vendor"
        sess["vendor_id"] = vendor_row.get("id")
        sess["vendor_name"] = vendor_row.get("vendor_name")
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("vendor_auth: set_vendor_session fejlede: %s", exc)


def current_vendor():
    """Return the logged-in vendor's row dict, or None.

    Resolved from ``session['vendor_id']`` (only when the session is genuinely
    a vendor session, i.e. ``user_type == 'vendor'``). Never raises.
    """
    sess = _session()
    try:
        if sess.get("user_type") != "vendor":
            return None
        vendor_id = sess.get("vendor_id")
        if not vendor_id:
            return None
        return _fetch_vendor_by_id(vendor_id)
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("vendor_auth: current_vendor fejlede: %s", exc)
        return None


# ---------------------------------------------------------------------------
# Decorator
# ---------------------------------------------------------------------------

def vendor_login_required(view):
    """Require an authenticated VENDOR session.

    Allows the request only when ``session['vendor_id']`` is set AND
    ``session['user_type'] == 'vendor'``. Otherwise:

      * API / XHR  -> JSON 401.
      * otherwise  -> Danish flash + redirect to ``vendor.login``.
    """

    @wraps(view)
    def wrapped(*args, **kwargs):
        sess = _session()
        if sess.get("vendor_id") and sess.get("user_type") == "vendor":
            return view(*args, **kwargs)

        if _wants_json():
            return _json_error("Log ind som leverandør for at fortsætte.", 401)
        _flash("Log ind som leverandør for at fortsætte.", "warning")
        return _redirect_to_login()

    return wrapped


__all__ = [
    "hash_vendor_password",
    "verify_vendor_password",
    "authenticate_vendor",
    "current_vendor",
    "set_vendor_session",
    "vendor_login_required",
    "VENDOR_LOGIN_ENDPOINT",
]
