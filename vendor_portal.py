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

import json
import logging
import time

import order_timing
from flask import (
    Blueprint,
    Response,
    current_app,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    session,
    stream_with_context,
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
            flash("Udfyld både e-mail og adgangskode.", "danger")
            return render_template("fm/vendor_login.html", email=email)

        auth = _vendor_auth()
        if auth is None or not hasattr(auth, "authenticate_vendor"):
            flash("Leverandørlogin er midlertidigt utilgængeligt. Prøv igen senere.", "danger")
            return render_template("fm/vendor_login.html", email=email)

        # S-2.2: same brute-force guard as the main login (separate key space).
        import login_guard
        guard_key = "vendor:" + email
        ip = login_guard.client_ip()
        allowed, retry_after = login_guard.check(guard_key, ip)
        if not allowed:
            flash(login_guard.locked_message(retry_after), "danger")
            return render_template("fm/vendor_login.html", email=email)

        try:
            vendor_row = auth.authenticate_vendor(email, password)
        except Exception as e:  # never 500 on a login attempt
            logger.warning("vendor_portal: authenticate_vendor failed: %s", e)
            vendor_row = None

        if not vendor_row:
            login_guard.record_failure(guard_key, ip)
            flash("Forkert e-mail eller adgangskode, eller kontoen er ikke aktiv.", "danger")
            return render_template("fm/vendor_login.html", email=email)

        login_guard.record_success(guard_key, ip)
        set_vendor_session(vendor_row)
        flash("Velkommen tilbage.", "success")
        return redirect(url_for("vendor.vendor_dashboard"))

    return render_template("fm/vendor_login.html", email="")


@vendor_bp.route("/logout", methods=["GET", "POST"])
def vendor_logout():
    """POST-only state change (S-2.2); a stray GET only shows a confirm page."""
    if request.method == "GET":
        if not session.get("vendor_id"):
            return redirect(url_for("vendor.vendor_login"))
        from flask import Response
        return Response(
            "<!doctype html><html lang='da'><head><meta charset='utf-8'><title>Log ud</title></head>"
            "<body style='font-family:system-ui;max-width:26rem;margin:5rem auto;padding:0 1rem;text-align:center'>"
            "<h1 style='font-size:1.4rem'>Vil du logge ud?</h1>"
            "<form method='post' action='%s'><button type='submit' style='padding:.7rem 1.4rem;font-size:1rem;"
            "border-radius:.6rem;border:0;background:#0b6b63;color:#fff;cursor:pointer'>Log ud</button></form>"
            "</body></html>" % url_for("vendor.vendor_logout"), mimetype="text/html")
    _clear_vendor_session()
    flash("Du er nu logget ud.", "success")
    return redirect(url_for("vendor.vendor_login"))


# ---------------------------------------------------------------------------
# Onboarding: set-password via signed/opaque invite token
# ---------------------------------------------------------------------------
# An admin creates the vendor (admin_dashboard.admin_vendor_create), which mints
# an opaque invite_token (+ expiry) on the vendors row and emails it. The vendor
# follows the link here to set their OWN password. This is the ONLY recovery
# path; it is intentionally outside the @vendor_login_required gate (the vendor
# is not logged in yet) but is gated by the unexpired single-use token instead.
@vendor_bp.route("/set-password/<token>", methods=["GET", "POST"])
def vendor_set_password(token):
    token = (token or "").strip()

    def _load_vendor_for_token(tok):
        """Return the vendor row iff the token is valid AND unexpired, else None."""
        if not tok:
            return None
        try:
            conn = _db()
            cur = conn.cursor()
            cur.execute(
                "SELECT id, vendor_name, contact_email, invite_expires_at "
                "FROM vendors WHERE invite_token = %s "
                "AND invite_expires_at IS NOT NULL AND invite_expires_at >= NOW() "
                "LIMIT 1",
                (tok,),
            )
            row = cur.fetchone()
            cur.close()
            return row
        except Exception as e:
            logger.warning("vendor_set_password: token lookup failed: %s", e)
            return None

    vendor_row = _load_vendor_for_token(token)
    if not vendor_row:
        flash("Linket er ugyldigt eller udløbet. Bed din kontaktperson om et nyt invitationslink.", "danger")
        return render_template("fm/vendor_set_password.html", token=token, invalid=True,
                               vendor_name="")

    if request.method == "POST":
        password = request.form.get("password") or ""
        confirm = request.form.get("confirm") or ""

        from password_policy import validate_password
        _pw_errors = validate_password(password, vendor_row.get("vendor_name"), vendor_row.get("contact_email"))
        if _pw_errors:
            for _err in _pw_errors:
                flash(_err, "danger")
            return render_template("fm/vendor_set_password.html", token=token,
                                   invalid=False, vendor_name=vendor_row.get("vendor_name") or "")
        if password != confirm:
            flash("De to adgangskoder er ikke ens.", "danger")
            return render_template("fm/vendor_set_password.html", token=token,
                                   invalid=False, vendor_name=vendor_row.get("vendor_name") or "")

        auth = _vendor_auth()
        if auth is None or not hasattr(auth, "hash_vendor_password"):
            flash("Leverandørlogin er midlertidigt utilgængeligt. Prøv igen senere.", "danger")
            return render_template("fm/vendor_set_password.html", token=token,
                                   invalid=False, vendor_name=vendor_row.get("vendor_name") or "")

        try:
            password_hash = auth.hash_vendor_password(password)
        except Exception as e:
            logger.warning("vendor_set_password: hashing failed: %s", e)
            flash("Adgangskoden kunne ikke gemmes. Prøv igen senere.", "danger")
            return render_template("fm/vendor_set_password.html", token=token,
                                   invalid=False, vendor_name=vendor_row.get("vendor_name") or "")

        try:
            conn = _db()
            cur = conn.cursor()
            # Set the password, ACTIVATE the account and CLEAR the single-use
            # token (+ expiry) so the link can never be reused. Re-checked the
            # token in WHERE so a concurrent/expired use cannot slip through.
            cur.execute(
                "UPDATE vendors SET password_hash = %s, status = 'active', "
                "invite_token = NULL, invite_expires_at = NULL "
                "WHERE id = %s AND invite_token = %s",
                (password_hash, vendor_row.get("id"), token),
            )
            affected = cur.rowcount
            conn.commit()
            cur.close()
        except Exception as e:
            try:
                _db().rollback()
            except Exception:
                pass
            logger.warning("vendor_set_password: update failed: %s", e)
            flash("Adgangskoden kunne ikke gemmes. Prøv igen senere.", "danger")
            return render_template("fm/vendor_set_password.html", token=token,
                                   invalid=False, vendor_name=vendor_row.get("vendor_name") or "")

        if not affected:
            flash("Linket er ugyldigt eller allerede brugt.", "danger")
            return render_template("fm/vendor_set_password.html", token=token, invalid=True,
                                   vendor_name="")

        flash("Din adgangskode er sat. Du kan nu logge ind.", "success")
        return redirect(url_for("vendor.vendor_login"))

    return render_template("fm/vendor_set_password.html", token=token, invalid=False,
                           vendor_name=vendor_row.get("vendor_name") or "")


# ---------------------------------------------------------------------------
# Forgot / reset password for vendors (S-2.4)
# ---------------------------------------------------------------------------
_VENDOR_RESET_NOTICE = ("Hvis der findes en leverandørkonto med den e-mail, har vi sendt et link "
                        "til at vælge en ny adgangskode. Tjek din indbakke.")


@vendor_bp.route("/forgot-password", methods=["GET", "POST"])
def vendor_forgot_password():
    if request.method == "POST":
        import login_guard
        import password_tokens
        import rate_limit
        email = (request.form.get("email") or "").strip().lower()
        ip = login_guard.client_ip()
        ok = rate_limit.hit("vforgot:ip:" + ip, 10, 900) and rate_limit.hit("vforgot:id:" + email, 3, 900)
        if ok and email:
            try:
                conn = _db()
                cur = conn.cursor()
                cur.execute("SELECT id, vendor_name, status FROM vendors WHERE contact_email = %s LIMIT 1", (email,))
                row = cur.fetchone()
                cur.close()
                if row:
                    vid = row["id"] if isinstance(row, dict) else row[0]
                    status = (row["status"] if isinstance(row, dict) else row[2]) or ""
                    # Suspended vendors get no link; pending ones use their invite.
                    if status.strip().lower() == "active":
                        raw = password_tokens.issue_token(conn, "vendor", vid, purpose="reset")
                        from email_service import send_branded_email
                        send_branded_email(
                            email, "Nulstil din adgangskode", "password_reset", {},
                            reset_url=password_tokens.build_url("vendor.vendor_reset_password", raw),
                            expires_in="60 minutter")
            except Exception as e:
                logger.warning("vendor_forgot_password failed: %s", e)
        flash(_VENDOR_RESET_NOTICE, "success")
        return redirect(url_for("vendor.vendor_forgot_password"))
    return render_template("fm/auth_simple.html",
                           page_title="Glemt adgangskode", heading="Glemt adgangskode",
                           subtitle="Skriv den e-mail, din leverandørkonto er oprettet med, så sender vi et link.",
                           action=url_for("vendor.vendor_forgot_password"),
                           fields=[{"name": "email", "label": "E-mail", "type": "email",
                                    "autocomplete": "email", "required": True}],
                           submit_label="Send link", back_url=url_for("vendor.vendor_login"),
                           back_label="Tilbage til login")


@vendor_bp.route("/reset-password/<token>", methods=["GET", "POST"])
def vendor_reset_password(token):
    import password_tokens
    from password_policy import validate_password, POLICY_HINT
    conn = _db()
    title = "Vælg ny adgangskode"
    invalid_page = lambda: render_template(  # noqa: E731
        "fm/auth_simple.html", page_title=title, heading=title, invalid=True,
        back_url=url_for("vendor.vendor_forgot_password"), back_label="Send et nyt link")
    info = None
    try:
        info = password_tokens.lookup(conn, token, account_type="vendor")
    except Exception as e:
        logger.warning("vendor_reset_password lookup failed: %s", e)
    if not info:
        flash("Linket er ugyldigt eller udløbet. Bed om et nyt link.", "danger")
        return invalid_page()
    cur = conn.cursor()
    cur.execute("SELECT id, vendor_name, contact_email, status FROM vendors WHERE id = %s", (info["account_id"],))
    v = cur.fetchone()
    cur.close()
    if not v or ((v["status"] if isinstance(v, dict) else v[3]) or "").strip().lower() != "active":
        flash("Linket er ugyldigt eller udløbet. Bed om et nyt link.", "danger")
        return invalid_page()
    v = v if isinstance(v, dict) else {"id": v[0], "vendor_name": v[1], "contact_email": v[2]}

    fields = [
        {"name": "password", "label": "Ny adgangskode", "type": "password",
         "autocomplete": "new-password", "required": True, "hint": POLICY_HINT},
        {"name": "confirm", "label": "Gentag adgangskode", "type": "password",
         "autocomplete": "new-password", "required": True},
    ]
    page = lambda: render_template(  # noqa: E731
        "fm/auth_simple.html", page_title=title, heading=title,
        subtitle="Vælg en adgangskode til leverandørportalen.",
        action=url_for("vendor.vendor_reset_password", token=token), fields=fields,
        submit_label="Gem adgangskode", back_url=url_for("vendor.vendor_login"), back_label="Til login")

    if request.method == "POST":
        password = request.form.get("password") or ""
        confirm = request.form.get("confirm") or ""
        errors = validate_password(password, v.get("vendor_name"), v.get("contact_email"))
        if password != confirm:
            errors.append("De to adgangskoder er ikke ens.")
        if errors:
            for err in errors:
                flash(err, "danger")
            return page()
        auth = _vendor_auth()
        if auth is None or not password_tokens.consume(conn, token):
            flash("Linket er ugyldigt eller allerede brugt. Bed om et nyt link.", "danger")
            return invalid_page()
        cur = conn.cursor()
        cur.execute("UPDATE vendors SET password_hash = %s WHERE id = %s",
                    (auth.hash_vendor_password(password), v["id"]))
        conn.commit()
        cur.close()
        flash("Din adgangskode er gemt. Du kan nu logge ind.", "success")
        return redirect(url_for("vendor.vendor_login"))
    return page()


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


def _ratings_for_handles(handles):
    """Map product_handle -> {avg_rating, review_count} for the given handles.

    Scoped strictly to the handles passed in (the vendor's OWN catalog handles —
    the caller resolves those). Aggregate-only: never reads buyer identity.
    Returns {} on any failure / missing table.
    """
    handles = [h for h in (handles or []) if h]
    if not handles:
        return {}
    try:
        conn = _db()
        cur = conn.cursor()
        placeholders = ",".join(["%s"] * len(handles))
        cur.execute(
            f"""SELECT product_handle, AVG(rating) AS avg_rating, COUNT(*) AS review_count
                FROM course_reviews WHERE product_handle IN ({placeholders})
                GROUP BY product_handle""",
            tuple(handles),
        )
        rows = cur.fetchall() or []
        cur.close()
        out = {}
        for r in rows:
            h = r.get("product_handle")
            avg = r.get("avg_rating")
            try:
                avg = round(float(avg), 1) if avg is not None else None
            except (TypeError, ValueError):
                avg = None
            out[h] = {"avg_rating": avg, "review_count": int(r.get("review_count") or 0)}
        return out
    except Exception as e:
        logger.debug("vendor_portal: _ratings_for_handles skipped: %s", e)
        return {}


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

    # Per-course average rating (own catalog only; aggregate-only). Attach onto
    # each product so the dashboard table can show a star average + count.
    ratings = _ratings_for_handles([p.get("handle") for p in products])
    for p in products:
        r = ratings.get(p.get("handle")) or {}
        p["avg_rating"] = r.get("avg_rating")
        p["review_count"] = r.get("review_count", 0)

    last_submission = submissions[0] if submissions else None
    kpis = {
        "awaiting_booking": awaiting_booking_count(vendor_id),
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
# Analytics (own KPIs as charts — leverandørens egne tal)
# ---------------------------------------------------------------------------
@vendor_bp.route("/analytics", methods=["GET"])
@_vendor_login_required
def vendor_analytics():
    """This vendor's own KPIs as charts (orders/completion over time, top
    courses). Sourced from vendor_tools.vendor_analytics_series — aggregate-only
    and k-anonymous: buyer (company/employee) identity is never read or shown."""
    vendor_name = session.get("vendor_name") or ""

    series = {}
    try:
        from vendor_tools import vendor_analytics_series
        series = vendor_analytics_series(vendor_name, months=6) or {}
    except Exception as e:
        logger.warning("vendor_analytics: series unavailable: %s", e)
        series = {}

    # On any tool error fall back to an empty, fully-formed shape so the page
    # renders a clean empty state rather than 500-ing.
    if not isinstance(series, dict) or series.get("error") or "months" not in series:
        series = {
            "vendor": vendor_name,
            "course_count": 0,
            "months": [],
            "orders_series": [],
            "completed_series": [],
            "totals": {"orders": 0, "completed": 0, "orders_30d": 0,
                       "completion_rate_pct": 0.0},
            "top_courses": [],
        }

    return render_template(
        "fm/vendor_analytics.html",
        vendor_name=vendor_name,
        analytics=series,
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
            # N-3.1: the DB is the vendor-profile source; mirror what the vendor edits.
            try:
                vrow = _fetch_vendor_row(vendor_id) or {}
                import catalog_service
                catalog_service.save_vendor_profile(
                    conn, vrow.get("vendor_name") or "",
                    {"reputation": description, "website": website, "logo_url": logo_url},
                    actor=f"vendor:{vendor_id}")
            except Exception as pe:
                logger.debug("vendor_portal: profile mirror skipped: %s", pe)
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
@vendor_bp.route('/courses/<handle>/edit',methods=['GET','POST'])
@_vendor_login_required
def vendor_course_edit(handle):
    product=catalog.get_product_any(handle)
    vendor=session.get('vendor_name') or ''
    if not product or not catalog._same_vendor(product.get('vendor'),vendor):
        from flask import abort
        abort(404)
    if request.method=='POST':
        try:
            if not request.form.get('revision') or catalog.product_revision(product)!=request.form['revision']:
                raise ValueError('Kurset er ændret. Genindlæs siden og gennemgå de aktuelle oplysninger.')
            fields=catalog.session_fields_from_form(request.form)
            fields.update(title=request.form.get('title','').strip()[:255],summary=request.form.get('summary','').strip()[:4000])
            if not fields['title']:raise ValueError('Angiv en kursustitel.')
            candidate=dict(product.get('raw') or {},**fields,handle=handle,vendor=vendor)
            draft=catalog.save_import_draft({'products':[candidate],'errors':[],'warnings':[],
                    'direct_edit':True,'forced_vendor':vendor,'edit_fields':fields,'expected_revision':request.form['revision']},
                    filename='Kursusrettelse: '+product['title'],uploaded_by='vendor:'+str(session['vendor_id']))
            cur=_db().cursor()
            try:
                cur.execute("INSERT INTO vendor_submissions (vendor_id,job_id,filename,row_count,status) VALUES (%s,%s,%s,1,'pending')",(session['vendor_id'],draft['job_id'],'Kursusrettelse: '+product['title']))
                _db().commit()
            finally:cur.close()
            flash('Rettelsen er indsendt. Den publicerede version er uændret indtil godkendelse.','success')
            return redirect(url_for('vendor.vendor_dashboard'))
        except (ValueError,TimeoutError) as exc:
            flash(str(exc) if isinstance(exc,ValueError) else 'Kataloget er optaget. Prøv igen om lidt.','warning')
            return render_template('fm/vendor_course_edit.html',product={**product,'title':request.form.get('title'),'summary':request.form.get('summary')},**catalog.session_editor_context(product,request.form)),400
    return render_template('fm/vendor_course_edit.html',product=product,**catalog.session_editor_context(product))


@vendor_bp.route("/submit-catalog", methods=["GET", "POST"])
@_vendor_login_required
def vendor_submit():
    vendor_id = session.get("vendor_id")

    if request.method == "POST":
        upload = request.files.get("catalog_csv")
        if not upload or not upload.filename:
            flash("Vælg en CSV-fil.", "danger")
            return redirect(url_for("vendor.vendor_submit"))

        if catalog is None:
            flash("Katalogimport er midlertidigt utilgængelig. Prøv igen senere.", "danger")
            return redirect(url_for("vendor.vendor_submit"))

        filename = upload.filename
        # Guard the parse: a bad file becomes a Danish error, never a 500.
        try:
            # S-2.7: the vendor comes from the SESSION, never from the CSV, and
            # handles owned by another vendor are refused.
            parsed = catalog.parse_catalog_csv(upload, force_vendor=session.get("vendor_name") or "")
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
            flash("Importkladden kunne ikke gemmes. Prøv igen senere.", "danger")
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


# ---------------------------------------------------------------------------
# Reset link helper (N-6.1). The forgot/reset SCREENS are Part A's (S-2.4):
# vendor_forgot_password / vendor_reset_password; this is what the admin's
# "send nulstillingslink" button uses.
# ---------------------------------------------------------------------------
def send_vendor_reset_link(vendor_row):
    """Mint a vendor_reset token and email the link. Returns True when a mail
    was handed to the mail layer. Never raises."""
    try:
        import password_tokens
        raw = password_tokens.issue_token(current_app.mysql.connection, "vendor", vendor_row["id"], purpose="reset")
        link = password_tokens.build_url("vendor.vendor_reset_password", raw)
        from email_service import send_branded_email
        return bool(send_branded_email(
            vendor_row.get("contact_email"), "Nulstil din adgangskode - Futurematch leverandørportal",
            "password_reset", {}, reset_url=link,
            dedupe_key="vendor_reset:%s:%s" % (vendor_row["id"], raw[:6]),
        ))
    except Exception as e:
        logger.warning("vendor_portal: reset link failed: %s", e)
        return False


# ---------------------------------------------------------------------------
# Orders (N-6.1): the vendor confirms (booked), declines or completes ITS OWN orders
# ---------------------------------------------------------------------------
def _active_vendor_ctx():
    """OrderContext for the logged-in vendor, or None when the account is not
    active any more. The status is re-read on EVERY request so suspending a
    vendor takes effect immediately (the session alone is not trusted)."""
    vendor_id = session.get("vendor_id")
    if not vendor_id or session.get("user_type") != "vendor":
        return None
    row = _fetch_vendor_row(vendor_id)
    if not row or (row.get("status") or "") != "active":
        return None
    from order_service import OrderContext
    return OrderContext.for_vendor(vendor_id, label=row.get("vendor_name") or session.get("vendor_name"))


def awaiting_booking_count(vendor_id):
    """Approved orders this vendor has not confirmed yet (the derived in-app badge)."""
    if not vendor_id:
        return 0
    try:
        cur = _db().cursor()
        cur.execute("SELECT COUNT(*) AS n FROM course_orders WHERE vendor_id = %s AND status IN ('approved', 'pending')",
                    (vendor_id,))
        r = cur.fetchone()
        cur.close()
        return int((r.get("n") if isinstance(r, dict) else r[0]) or 0)
    except Exception:
        return 0


@vendor_bp.context_processor
def _vendor_nav_badge():
    if session.get("user_type") == "vendor" and session.get("vendor_id"):
        return {"vendor_awaiting_count": awaiting_booking_count(session.get("vendor_id"))}
    return {}


_ORDER_TABS = {
    "afventer": ("Afventer bekræftelse", "('approved', 'pending')"),
    "booket": ("Bekræftet", "('booked', 'confirmed', 'processing')"),
    "afsluttet": ("Afsluttet", "('completed')"),
    "annulleret": ("Annulleret", "('cancelled', 'rejected')"),
}


def _vendor_orders(vendor_id, tab):
    """This vendor's orders only (vendor_id is the session's, never a parameter)."""
    where = "co.vendor_id = %s AND co.status NOT IN ('pending_approval')"
    if tab in _ORDER_TABS:
        where += " AND co.status IN " + _ORDER_TABS[tab][1]
    cur = _db().cursor()
    cur.execute(
        "SELECT co.order_id, co.product_title, co.product_handle, co.variant_date, co.variant_location, "
        "co.status, co.user_name, co.user_email, co.user_phone, co.request_notes, co.cancel_reason, "
        "co.created_at, c.company_name, d.booking_json "
        "FROM course_orders co LEFT JOIN companies c ON c.id = co.company_id "
        "LEFT JOIN course_order_details d ON d.order_id = co.order_id "
        "WHERE " + where + " ORDER BY co.created_at DESC LIMIT 200",
        (vendor_id,),
    )
    rows = list(cur.fetchall() or [])
    cur.close()
    return rows


def _pending_change_ids(order_ids):
    """Order ids (of the given list) with an open change request: one grouped query."""
    if not order_ids:
        return set()
    try:
        cur = _db().cursor()
        cur.execute(
            "SELECT order_id FROM course_order_changes WHERE status = 'pending' AND order_id IN (%s) GROUP BY order_id"
            % ",".join(["%s"] * len(order_ids)),
            tuple(order_ids),
        )
        rows = list(cur.fetchall() or [])
        cur.close()
        return {r["order_id"] if isinstance(r, dict) else r[0] for r in rows}
    except Exception as e:
        logger.debug("vendor_orders: pending changes lookup failed: %s", e)
        return set()


@vendor_bp.route("/orders", methods=["GET"])
@_vendor_login_required
def vendor_orders():
    ctx = _active_vendor_ctx()
    if ctx is None:
        _clear_vendor_session()
        flash("Din konto er ikke aktiv. Kontakt Futurematch, hvis det er en fejl.", "danger")
        return redirect(url_for("vendor.vendor_login"))
    tab = request.args.get("tab") or "afventer"
    if tab not in _ORDER_TABS and tab != "alle":
        tab = "afventer"
    import order_lifecycle as lc
    try:
        orders = _vendor_orders(ctx.vendor_id, tab)
        load_error = False
    except Exception as e:
        logger.warning("vendor_orders: load failed: %s", e)
        orders, load_error = [], True
    pending_changes = _pending_change_ids([o["order_id"] for o in orders])
    for o in orders:
        st = lc.normalize_status(o.get("status"))
        o["state"] = st
        o["change_pending"] = o["order_id"] in pending_changes
        o["label"] = lc.status_label(st, short=True)
        o["tone"] = lc.STATUS_TONES[st]
        o["can_book"] = st == lc.APPROVED
        o["can_decline"] = st in (lc.APPROVED, lc.BOOKED)
        # Attendance is only confirmed once the course has taken place.
        o["can_complete"] = st == lc.BOOKED and order_timing.not_yet_held_message(o) is None
        o["held_message"] = order_timing.not_yet_held_message(o) if st == lc.BOOKED else None
    return render_template(
        "fm/vendor_orders.html", vendor_name=session.get("vendor_name") or "", orders=orders, tab=tab,
        tabs=_ORDER_TABS, load_error=load_error,
    )


def _order_action(order_id, fn_name):
    ctx = _active_vendor_ctx()
    if ctx is None:
        return None, None
    import order_service
    if fn_name == "book":
        return ctx, order_service.book_order(ctx, order_id, booking=request.form.to_dict())
    if fn_name == "complete":
        return ctx, order_service.complete_order(ctx, order_id)
    if fn_name == "decline":
        reason = (request.form.get("reason") or "").strip()
        if not reason:
            return ctx, {"success": False, "error": "reason_required",
                         "message": "Skriv en kort begrundelse, så HR og deltageren ved hvorfor."}
        return ctx, order_service.set_status(ctx, order_id, "cancelled", reason=reason[:240])
    return ctx, {"success": False, "error": "bad_action", "message": "Ukendt handling."}


def _order_action_response(order_id, fn_name, ok_msg):
    ctx, res = _order_action(order_id, fn_name)
    if ctx is None:
        _clear_vendor_session()
        flash("Din konto er ikke aktiv. Kontakt Futurematch, hvis det er en fejl.", "danger")
        return redirect(url_for("vendor.vendor_login"))
    if res.get("success"):
        flash(ok_msg, "success")
    else:
        flash(res.get("message") or "Handlingen kunne ikke gennemføres.", "danger")
    return redirect(url_for("vendor.vendor_orders", tab=request.form.get("tab") or "afventer"))


@vendor_bp.route('/orders/<order_id>/booking',methods=['GET','POST'])
@_vendor_login_required
def vendor_booking(order_id):
    from fulfillment_routes import workflow
    return workflow(_active_vendor_ctx(),order_id,vendor=True)


@vendor_bp.route("/orders/<order_id>/book", methods=["POST"])
@_vendor_login_required
def vendor_order_book(order_id):
    return _order_action_response(order_id, "book", "Pladsen er bekræftet. Deltageren og HR får besked.")


@vendor_bp.route("/orders/<order_id>/decline", methods=["POST"])
@_vendor_login_required
def vendor_order_decline(order_id):
    return _order_action_response(order_id, "decline", "Bestillingen er afvist. HR og deltageren får besked.")


@vendor_bp.route("/orders/<order_id>/complete", methods=["POST"])
@_vendor_login_required
def vendor_order_complete(order_id):
    return _order_action_response(order_id, "complete", "Deltagelsen er registreret som gennemført.")


# ===========================================================================
# Vendor AI assistant (leverandør-assistent)
# ---------------------------------------------------------------------------
# A small agent turn scoped strictly to the logged-in vendor. It offers the
# vendor-only tools (vendor_tools.VENDOR_TOOLS), executes them via
# execute_vendor_tool(name, args, session['vendor_name']) and streams the answer
# back as SSE. Hard rules:
#   * The agent ONLY sees this vendor's own aggregated numbers + anonymized,
#     platform-wide market demand. Buyer (company/employee) identity is never
#     exposed — enforced both in the tools and in the system prompt.
#   * Boot-safe: ai_runtime / vendor_tools are imported lazily inside the route
#     so a missing/broken AI stack can never crash create_app(); the route then
#     fails closed with a Danish error instead of 500-ing.
# ===========================================================================

# In-memory per-vendor-session conversation memory (mirrors hr_agent's store).
VENDOR_CHAT_MEMORY = {}
VENDOR_SESSION_TTL = 3600

VENDOR_SYSTEM_PROMPT = """Du er en leverandør-assistent for en kursusudbyder på Futurematch-platformen.

DIN ROLLE:
- Du hjælper leverandøren med at forstå deres egne salgstal og markedet.
- Du er konkret og handlingsorienteret og giver 1-2 anbefalinger ud fra dataen.

HVAD DU KAN (via værktøjer):
- Vise leverandørens egne aggregerede salgstal (ordrer, trend 30/90 dage, gennemførelsesrate, topkurser).
- Vise anonymiseret markedsefterspørgsel pr. kategori/emne på tværs af platformen.
- Sammenligne leverandørens egne kurser med lignende kurser i kataloget på pris, varighed, sværhedsgrad og format.
- Tjekke kvaliteten af leverandørens egne katalogopslag (vendor_catalog_health) og give en prioriteret retteliste: manglende pris, ingen kommende datoer, tynd beskrivelse, manglende kategori/niveau/sprog/varighed/billede. Brug det når leverandøren spørger hvorfor et kursus ikke bliver set, hvad de kan forbedre, eller hvad der mangler i kataloget — og bind rettelsen til den effekt den har (filtre, søgning, anbefalinger).

ABSOLUTTE REGLER:
- Svar KUN ud fra leverandørens egne aggregerede tal og anonymiserede markedsdata.
- Du må ALDRIG oplyse hvilken virksomhed eller hvilken medarbejder der har købt et kursus. Du har ikke adgang til den slags data — antyd den aldrig.
- Du ser kun denne leverandørs egne kurser. Nævn aldrig andre leverandørers salgstal (kun offentlige katalogfakta som pris/varighed må sammenlignes).
- Brug altid værktøjer før du nævner konkrete tal. Find aldrig på tal.
- Hvis et tal er skjult af anonymitetshensyn (k-anonymitet), så forklar kort hvorfor i stedet for at gætte.

STIL:
- Kort, præcist og på dansk. Brug bullet points til tal. Fremhæv den vigtigste indsigt først.
- Tal som en kollega, ikke som en formular: stil højst ét opklarende spørgsmål ad gangen.
- Afslut hvert svar med 2-3 korte forslag til næste skridt i formen
  <suggestions>["forslag 1", "forslag 2", "forslag 3"]</suggestions> (tagget vises ikke for brugeren).

EKSEMPLER PÅ TONEN:
Leverandør: Hvordan går det med mine kurser?
Dig: (henter tallene først) Dine ordrer er steget 12 % de seneste 30 dage, og det er især PRINCE2, der trækker. Gennemførelsesraten er solid, men to kurser har ingen kommende datoer, og dem ser kunderne ikke. Skal jeg pege på de to?

Leverandør: Hvem har købt mit dyreste kursus?
Dig: Det kan jeg ikke se, og jeg nævner aldrig købere. Jeg kan til gengæld vise, hvordan kurset klarer sig på pris og efterspørgsel i forhold til lignende kurser.
"""

# Suggestion chips shown when the model forgets its <suggestions> tag.
VENDOR_FALLBACK_SUGGESTIONS = ["Vis mine topkurser", "Hvad efterspørges lige nu?", "Tjek mine kursusopslag"]


def _cleanup_vendor_sessions():
    now = time.time()
    stale = [
        sid for sid, msgs in VENDOR_CHAT_MEMORY.items()
        if msgs and isinstance(msgs[-1], dict) and msgs[-1].get("_ts", 0) < now - VENDOR_SESSION_TTL
    ]
    for sid in stale:
        VENDOR_CHAT_MEMORY.pop(sid, None)


def _vendor_sse(event):
    """Serialize a single SSE 'data:' frame."""
    return f"data: {json.dumps(event)}\n\n"


@vendor_bp.route("/ask", methods=["POST"])
@_vendor_login_required
def vendor_ask():
    """Run one vendor-scoped agent turn and stream the answer as SSE.

    Strictly scoped to session['vendor_name']: the only tools offered are the
    vendor tools, and they are executed with the SESSION vendor name so a vendor
    can never reach another vendor's (or any buyer's) data.
    """
    vendor_name = session.get("vendor_name") or ""
    vendor_id = session.get("vendor_id")

    # Accept JSON or form body.
    payload = request.get_json(silent=True) or {}
    user_query = (payload.get("message") or payload.get("query")
                  or request.form.get("message") or request.form.get("query") or "").strip()

    if not user_query:
        return jsonify({"error": "Skriv et spørgsmål."}), 400

    # Lazy, guarded imports so a broken AI stack never crashes the portal.
    try:
        from vendor_tools import VENDOR_TOOLS, execute_vendor_tool
    except Exception as e:  # pragma: no cover - boot safety
        logger.warning("vendor_ask: vendor_tools unavailable: %s", e)
        return jsonify({"error": "Leverandør-assistenten er midlertidigt utilgængelig."}), 503

    # Per-vendor conversation memory, keyed by an isolated session id.
    _cleanup_vendor_sessions()
    # Durable memory (N-5.4): the transcript lives in MySQL, the in-process dict is
    # only a cache, so a deploy / second worker / new tab keeps the conversation.
    import vendor_conversations
    who = vendor_conversations.owner(vendor_id)
    sid = vendor_conversations.resolve_sid(session, vendor_id)
    if sid not in VENDOR_CHAT_MEMORY:
        VENDOR_CHAT_MEMORY[sid] = [{"role": "system", "content": VENDOR_SYSTEM_PROMPT}] + [
            dict(m, _ts=time.time()) for m in vendor_conversations.load(who, sid)]

    messages = VENDOR_CHAT_MEMORY[sid]
    # Inject/refresh a small vendor-context system line (which vendor we are). The
    # name is vendor-controlled free text, so it is fenced as DATA (prompt injection).
    try:
        import grounding as _g
        fenced_name = _g.delimit_untrusted("leverandørnavn", vendor_name) or vendor_name
    except Exception:
        fenced_name = vendor_name
    context_line = {"role": "system", "content": f"LEVERANDØR: {fenced_name}"}
    if len(messages) > 1 and messages[1].get("role") == "system" \
            and (messages[1].get("content") or "").startswith("LEVERANDØR:"):
        messages[1] = context_line
    else:
        messages.insert(1, context_line)
    messages.append({"role": "user", "content": user_query, "_ts": time.time()})

    def stream_generator():
        full_text = ""
        try:
            yield _vendor_sse({"type": "ping", "content": "ok"})

            from db_compat import close_flask_mysql_connection
            from ai_runtime import (
                PROMPT_VERSION as AI_PROMPT_VERSION,
                build_tool_call_event,
                iter_agent_with_live_tool_events,
                iter_completion_stream,
                live_tool_events_enabled,
                log_agent_run,
                log_tool_run,
                main_model,
                make_run_id,
                run_agent_with_fallback,
            )
            from ai_tool_registry import tool_name

            # Strip private "_ts" bookkeeping before sending to the model.
            clean_messages = [
                {k: v for k, v in m.items() if k != "_ts"} for m in messages
            ]

            # Vendor tool executor: ALWAYS bind the SESSION vendor name so the
            # model can never widen scope to another vendor / buyer.
            def _vendor_executor(tool_call, username=None, session_id=None):
                name = tool_call.function.name
                try:
                    args = json.loads(tool_call.function.arguments or "{}")
                except Exception:
                    args = {}
                result = execute_vendor_tool(name, args, vendor_name)
                return json.dumps(result, default=str, ensure_ascii=False)

            run_id = make_run_id()
            yield _vendor_sse({"type": "thinking", "content": "Analyserer…"})

            agent_kwargs = {
                "messages": clean_messages,
                "tools": VENDOR_TOOLS,
                "tool_executor": _vendor_executor,
                "username": f"vendor:{vendor_id}",
                "session_id": sid,
                "max_iterations": 4,
                "prompt_cache_key": "futurematch-vendor",
                "agent_scope": "vendor",
                "company_scope": f"vendor:{vendor_id}",
            }
            live_tool_call_ids = set()
            if live_tool_events_enabled():
                runtime_result = None
                for _kind, _payload in iter_agent_with_live_tool_events(
                    agent_kwargs, thread_name="vendor-agent-live"
                ):
                    if _kind == "tool_event":
                        if _payload.get("id"):
                            live_tool_call_ids.add(_payload["id"])
                        yield _vendor_sse(_payload)
                    elif _kind == "ping":
                        yield _vendor_sse({"type": "ping", "content": "working"})
                    elif _kind == "result":
                        runtime_result = _payload
                if runtime_result is None:
                    raise RuntimeError("live tool events: vendor agent-loopet leverede intet resultat")
            else:
                runtime_result = run_agent_with_fallback(**agent_kwargs)

            try:
                log_agent_run(
                    getattr(current_app, "mysql", None),
                    run_id=run_id,
                    session_id=sid,
                    company_id=None,
                    username=f"vendor:{vendor_id}",
                    agent_scope="vendor",
                    runtime=runtime_result.runtime,
                    model=main_model(),
                    prompt_version=AI_PROMPT_VERSION,
                    toolset_version="futurematch-vendor-tools-v1",
                    tool_names=[tool_name(t) for t in VENDOR_TOOLS],
                    response_id=runtime_result.response_id,
                    status="ok",
                    fallback_reason=runtime_result.fallback_reason,
                    latency_ms=runtime_result.latency_ms,
                    usage=runtime_result.usage,
                    compaction_level=runtime_result.compaction_level,
                    runtime_path=runtime_result.runtime_path or runtime_result.runtime,
                )
            except Exception:
                pass

            for tool_result in runtime_result.tool_results:
                if tool_result.call_id not in live_tool_call_ids:
                    yield _vendor_sse(build_tool_call_event(tool_result, agent_scope="vendor"))
                try:
                    log_tool_run(
                        getattr(current_app, "mysql", None),
                        run_id=run_id,
                        session_id=sid,
                        company_id=None,
                        username=f"vendor:{vendor_id}",
                        agent_scope="vendor",
                        result=tool_result,
                    )
                except Exception:
                    pass

            close_flask_mysql_connection()

            final_messages = list(
                runtime_result.stream_messages or runtime_result.messages or clean_messages
            )
            import ai_reply
            raw_text = runtime_result.text or ""
            flt = ai_reply.SuggestionFilter()
            if runtime_result.needs_final_stream or not raw_text.strip():
                raw_text = ""
                for token in iter_completion_stream(final_messages):
                    raw_text += token
                    shown = flt.feed(token)
                    if shown:
                        yield _vendor_sse({"type": "text", "content": shown})
                tail = flt.flush()
                if tail:
                    yield _vendor_sse({"type": "text", "content": tail})
            else:
                yield _vendor_sse({"type": "text", "content": ai_reply.strip_suggestions(raw_text)})
            full_text = ai_reply.strip_suggestions(raw_text)

            # Grounding check (same circuit-breaker the HR assistant uses): figures the
            # answer quotes must be backed by THIS turn's tool results.
            try:
                import grounding as _grounding
                evidence = [getattr(tr, "output", None) for tr in (runtime_result.tool_results or [])]
                evidence = [e for e in evidence if e]
                if evidence and full_text.strip():
                    verdict = _grounding.grounding_disclaimer(full_text, evidence)
                    if verdict.get("violation") and verdict.get("disclaimer"):
                        note = "\n\n" + verdict["disclaimer"]
                        full_text += note
                        yield _vendor_sse({"type": "text", "content": note})
            except Exception as _ge:
                logger.debug("vendor grounding check skipped: %s", _ge)

            suggestions = ai_reply.extract_suggestions(raw_text) or VENDOR_FALLBACK_SUGGESTIONS
            yield _vendor_sse({"type": "suggestions", "items": suggestions})

            messages.append({"role": "assistant", "content": full_text, "_ts": time.time()})
            # Bound memory growth.
            if len(messages) > 30:
                VENDOR_CHAT_MEMORY[sid] = [messages[0]] + messages[-16:]
            vendor_conversations.save(who, sid, [m for m in VENDOR_CHAT_MEMORY[sid] if m.get("role") in ("user", "assistant")])

            yield _vendor_sse({"type": "done"})
        except Exception as e:
            logger.warning("vendor_ask: stream failed: %s", e)
            try:
                from ai_runtime import user_facing_error_message as _ufem
                _err_msg = _ufem(e)
            except Exception:
                _err_msg = "Der opstod en fejl. Prøv venligst igen."
            yield _vendor_sse({"type": "error", "content": _err_msg})
            yield _vendor_sse({"type": "done"})
        finally:
            try:
                from db_compat import close_flask_mysql_connection
                close_flask_mysql_connection()
            except Exception:
                pass

    return Response(
        stream_with_context(stream_generator()),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
