"""Vendor self-service portal blueprint.

This is the public-facing surface a course vendor (leverandor) uses to:
  - log in with their own credentials (ISOLATED from the main employee/HR/admin app)
  - view their own catalog + their recent submission status (dashboard)
  - edit their own vendor profile row
  - upload a CSV catalog that lands in the EXISTING admin import-drafts queue

Hard isolation rules (see SHARED CONTRACT):
  - A vendor session sets session['user_type']='vendor', session['vendor_id'],
    session['vendor_name'] and MUST NOT set session['user']. This guarantees a
    vendor can never satisfy the regular login_required / role gates and so can
    never reach the employee/HR/admin surfaces.
  - Every query is scoped to session['vendor_id'] / session['vendor_name'] — a
    vendor never sees another vendor's data.

Boot-safety: vendor_auth (owned by another module) is imported lazily/guarded so
a missing or broken vendor_auth can never crash create_app(). If vendor_auth is
unavailable the routes fail closed (Danish error / redirect to login), never 500
in create_app().
"""

import logging

from flask import (
    Blueprint,
    current_app,
    flash,
    redirect,
    render_template,
    request,
    session,
    url_for,
)

try:
    import catalog_service as catalog
except Exception as e:  # pragma: no cover - boot safety
    catalog = None
    logging.getLogger(__name__).warning("vendor_portal: catalog_service unavailable: %s", e)

logger = logging.getLogger(__name__)

vendor_bp = Blueprint("vendor", __name__, url_prefix="/vendor")

# Session keys that belong to a vendor login. logout clears ONLY these so a
# vendor logout can never disturb an unrelated main-app session.
_VENDOR_SESSION_KEYS = ("vendor_id", "vendor_name", "user_type")


# ---------------------------------------------------------------------------
# vendor_auth bridge (guarded). vendor_auth.py is owned by another module; we
# never hard-import it at module load so this blueprint stays boot-safe.
# ---------------------------------------------------------------------------
def _vendor_auth():
    """Return the vendor_auth module, or None if it is unavailable."""
    try:
        import vendor_auth
        return vendor_auth
    except Exception as e:  # pragma: no cover - boot safety
        logger.warning("vendor_portal: vendor_auth unavailable: %s", e)
        return None


def _vendor_login_required(view):
    """Wrap a view with vendor_auth.@vendor_login_required when available.

    Falls back to a local inline guard (same semantics: vendor_id present AND
    user_type == 'vendor') if vendor_auth cannot be imported, so the portal is
    never left unguarded.
    """
    auth = _vendor_auth()
    decorator = getattr(auth, "vendor_login_required", None) if auth else None
    if callable(decorator):
        return decorator(view)

    from functools import wraps

    @wraps(view)
    def _fallback(*args, **kwargs):
        if session.get("vendor_id") and session.get("user_type") == "vendor":
            return view(*args, **kwargs)
        return redirect(url_for("vendor.vendor_login"))

    return _fallback


def set_vendor_session(vendor_row):
    """Set the isolated vendor session.

    Sets ONLY the vendor keys and explicitly removes any 'user' key so a vendor
    login can never be mistaken for a regular employee/HR/admin login.
    """
    session["user_type"] = "vendor"
    session["vendor_id"] = vendor_row.get("id")
    session["vendor_name"] = vendor_row.get("vendor_name") or vendor_row.get("name") or ""
    # Defensive: a vendor session must NEVER carry a main-app user identity.
    session.pop("user", None)
    session.pop("role", None)
    session.pop("company_id", None)


def _clear_vendor_session():
    for key in _VENDOR_SESSION_KEYS:
        session.pop(key, None)


# ---------------------------------------------------------------------------
# Auth routes
# ---------------------------------------------------------------------------
@vendor_bp.route("/login", methods=["GET", "POST"])
def vendor_login():
    # Already logged in as a vendor -> straight to dashboard.
    if session.get("vendor_id") and session.get("user_type") == "vendor":
        return redirect(url_for("vendor.vendor_dashboard"))

    if request.method == "POST":
        email = (request.form.get("email") or "").strip().lower()
        password = request.form.get("password") or ""
        if not email or not password:
            flash("Udfyld bade e-mail og adgangskode.", "danger")
            return render_template("fm/vendor_login.html", email=email)

        auth = _vendor_auth()
        if auth is None or not hasattr(auth, "authenticate_vendor"):
            flash("Leverandorlogin er midlertidigt utilgaengeligt. Prov igen senere.", "danger")
            return render_template("fm/vendor_login.html", email=email)

        try:
            vendor_row = auth.authenticate_vendor(email, password)
        except Exception as e:  # never 500 on a login attempt
            logger.warning("vendor_portal: authenticate_vendor failed: %s", e)
            vendor_row = None

        if not vendor_row:
            flash("Forkert e-mail eller adgangskode, eller kontoen er ikke aktiv.", "danger")
            return render_template("fm/vendor_login.html", email=email)

        set_vendor_session(vendor_row)
        flash("Velkommen tilbage.", "success")
        return redirect(url_for("vendor.vendor_dashboard"))

    return render_template("fm/vendor_login.html", email="")


@vendor_bp.route("/logout")
def vendor_logout():
    _clear_vendor_session()
    flash("Du er nu logget ud.", "success")
    return redirect(url_for("vendor.vendor_login"))


# ---------------------------------------------------------------------------
# Helpers — DB access scoped to the logged-in vendor only.
# ---------------------------------------------------------------------------
def _db():
    return current_app.mysql.connection


def _fetch_vendor_row(vendor_id):
    """Load the vendor's OWN row, scoped strictly to vendor_id."""
    try:
        conn = _db()
        cur = conn.cursor()
        cur.execute(
            "SELECT id, vendor_name, slug, contact_email, status, description, "
            "website, logo_url, created_at, updated_at "
            "FROM vendors WHERE id = %s",
            (vendor_id,),
        )
        row = cur.fetchone()
        cur.close()
        return row
    except Exception as e:
        logger.warning("vendor_portal: _fetch_vendor_row failed: %s", e)
        return None


def _fetch_submissions(vendor_id, limit=15):
    """Recent submissions for THIS vendor only (scoped to vendor_id)."""
    try:
        conn = _db()
        cur = conn.cursor()
        cur.execute(
            "SELECT id, job_id, filename, row_count, status, reviewed_at, created_at "
            "FROM vendor_submissions WHERE vendor_id = %s "
            "ORDER BY created_at DESC LIMIT %s",
            (vendor_id, int(limit)),
        )
        rows = cur.fetchall() or []
        cur.close()
        return list(rows)
    except Exception as e:
        logger.warning("vendor_portal: _fetch_submissions failed: %s", e)
        return []


def _vendor_products(vendor_name):
    """Products in the live catalog that belong to this vendor (by name).

    Scoped by the product 'vendor' string == session['vendor_name']. Matching is
    case-insensitive so casing drift between the vendors row and product strings
    does not hide a vendor's own catalog.
    """
    if catalog is None or not vendor_name:
        return []
    try:
        wanted = vendor_name.strip().lower()
        return [
            p for p in catalog.get_products()
            if (p.get("vendor") or "").strip().lower() == wanted
        ]
    except Exception as e:
        logger.warning("vendor_portal: _vendor_products failed: %s", e)
        return []


# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------
@vendor_bp.route("/", methods=["GET"])
@_vendor_login_required
def vendor_dashboard():
    vendor_id = session.get("vendor_id")
    vendor_name = session.get("vendor_name") or ""

    vendor_row = _fetch_vendor_row(vendor_id) or {}
    products = _vendor_products(vendor_name)
    submissions = _fetch_submissions(vendor_id, limit=10)

    last_submission = submissions[0] if submissions else None
    kpis = {
        "course_count": len(products),
        "submission_count": len(submissions),
        "pending_count": sum(1 for s in submissions if (s.get("status") or "") == "pending"),
        "last_submission": last_submission,
    }

    return render_template(
        "fm/vendor_dashboard.html",
        vendor=vendor_row,
        vendor_name=vendor_name,
        products=products,
        submissions=submissions,
        kpis=kpis,
    )


# ---------------------------------------------------------------------------
# Profile (edit own vendors row only)
# ---------------------------------------------------------------------------
@vendor_bp.route("/profile", methods=["GET", "POST"])
@_vendor_login_required
def vendor_profile():
    vendor_id = session.get("vendor_id")

    if request.method == "POST":
        description = (request.form.get("description") or "").strip()
        website = (request.form.get("website") or "").strip()
        logo_url = (request.form.get("logo_url") or "").strip()
        contact_email = (request.form.get("contact_email") or "").strip().lower()

        if not contact_email:
            flash("Kontakt-e-mail er pakraevet.", "danger")
            vendor_row = _fetch_vendor_row(vendor_id) or {}
            return render_template("fm/vendor_profile.html", vendor=vendor_row)

        try:
            conn = _db()
            cur = conn.cursor()
            # Scoped to vendor_id ONLY — a vendor can never edit another row.
            cur.execute(
                "UPDATE vendors SET description = %s, website = %s, logo_url = %s, "
                "contact_email = %s WHERE id = %s",
                (description, website, logo_url, contact_email, vendor_id),
            )
            conn.commit()
            cur.close()
            flash("Din profil er opdateret.", "success")
        except Exception as e:
            try:
                _db().rollback()
            except Exception:
                pass
            logger.warning("vendor_portal: profile update failed: %s", e)
            # Likely a duplicate contact_email (UNIQUE) — keep it friendly + Danish.
            flash("Profilen kunne ikke gemmes. E-mailen er muligvis allerede i brug.", "danger")

        return redirect(url_for("vendor.vendor_profile"))

    vendor_row = _fetch_vendor_row(vendor_id) or {}
    return render_template("fm/vendor_profile.html", vendor=vendor_row)


# ---------------------------------------------------------------------------
# Submit catalog (CSV -> existing admin import-drafts queue)
# ---------------------------------------------------------------------------
@vendor_bp.route("/submit-catalog", methods=["GET", "POST"])
@_vendor_login_required
def vendor_submit():
    vendor_id = session.get("vendor_id")

    if request.method == "POST":
        upload = request.files.get("catalog_csv")
        if not upload or not upload.filename:
            flash("Vaelg en CSV-fil.", "danger")
            return redirect(url_for("vendor.vendor_submit"))

        if catalog is None:
            flash("Katalogimport er midlertidigt utilgaengelig. Prov igen senere.", "danger")
            return redirect(url_for("vendor.vendor_submit"))

        filename = upload.filename
        # Guard the parse: a bad file becomes a Danish error, never a 500.
        try:
            parsed = catalog.parse_catalog_csv(upload)
        except Exception as e:
            logger.warning("vendor_portal: CSV parse failed: %s", e)
            flash("CSV-filen kunne ikke laeses. Tjek formatet og prov igen.", "danger")
            return redirect(url_for("vendor.vendor_submit"))

        try:
            draft = catalog.save_import_draft(
                parsed,
                filename=filename,
                uploaded_by="vendor:" + str(vendor_id),
            )
        except Exception as e:
            logger.warning("vendor_portal: save_import_draft failed: %s", e)
            flash("Importkladden kunne ikke gemmes. Prov igen senere.", "danger")
            return redirect(url_for("vendor.vendor_submit"))

        job_id = (draft or {}).get("job_id", "")
        row_count = len((parsed or {}).get("products") or [])

        # Record the submission so the vendor can track its review status. A DB
        # failure here must not lose the draft (it is already saved + queued for
        # admin review), so we only warn.
        try:
            conn = _db()
            cur = conn.cursor()
            cur.execute(
                "INSERT INTO vendor_submissions "
                "(vendor_id, job_id, filename, row_count, status) "
                "VALUES (%s, %s, %s, %s, 'pending')",
                (vendor_id, job_id, filename, row_count),
            )
            conn.commit()
            cur.close()
        except Exception as e:
            try:
                _db().rollback()
            except Exception:
                pass
            logger.warning("vendor_portal: vendor_submissions insert failed: %s", e)

        flash(
            "Dit katalog er indsendt og afventer godkendelse. "
            f"Vi behandlede {row_count} kurser.",
            "success",
        )
        return redirect(url_for("vendor.vendor_dashboard"))

    return render_template("fm/vendor_submit.html")
