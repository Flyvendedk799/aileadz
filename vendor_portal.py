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
import uuid

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

        if len(password) < 8:
            flash("Adgangskoden skal vaere mindst 8 tegn.", "danger")
            return render_template("fm/vendor_set_password.html", token=token,
                                   invalid=False, vendor_name=vendor_row.get("vendor_name") or "")
        if password != confirm:
            flash("De to adgangskoder er ikke ens.", "danger")
            return render_template("fm/vendor_set_password.html", token=token,
                                   invalid=False, vendor_name=vendor_row.get("vendor_name") or "")

        auth = _vendor_auth()
        if auth is None or not hasattr(auth, "hash_vendor_password"):
            flash("Leverandorlogin er midlertidigt utilgaengeligt. Prov igen senere.", "danger")
            return render_template("fm/vendor_set_password.html", token=token,
                                   invalid=False, vendor_name=vendor_row.get("vendor_name") or "")

        try:
            password_hash = auth.hash_vendor_password(password)
        except Exception as e:
            logger.warning("vendor_set_password: hashing failed: %s", e)
            flash("Adgangskoden kunne ikke gemmes. Prov igen senere.", "danger")
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
            flash("Adgangskoden kunne ikke gemmes. Prov igen senere.", "danger")
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

ABSOLUTTE REGLER:
- Svar KUN ud fra leverandørens egne aggregerede tal og anonymiserede markedsdata.
- Du må ALDRIG oplyse hvilken virksomhed eller hvilken medarbejder der har købt et kursus. Du har ikke adgang til den slags data — antyd den aldrig.
- Du ser kun denne leverandørs egne kurser. Nævn aldrig andre leverandørers salgstal (kun offentlige katalogfakta som pris/varighed må sammenlignes).
- Brug altid værktøjer før du nævner konkrete tal. Find aldrig på tal.
- Hvis et tal er skjult af anonymitetshensyn (k-anonymitet), så forklar kort hvorfor i stedet for at gætte.

STIL:
- Kort, præcist og på dansk. Brug bullet points til tal. Fremhæv den vigtigste indsigt først.
"""


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
    sid = session.get("vendor_chat_session_id")
    if not sid:
        sid = f"vendor_{vendor_id}_{uuid.uuid4()}"
        session["vendor_chat_session_id"] = sid
    if sid not in VENDOR_CHAT_MEMORY:
        VENDOR_CHAT_MEMORY[sid] = [{"role": "system", "content": VENDOR_SYSTEM_PROMPT}]

    messages = VENDOR_CHAT_MEMORY[sid]
    # Inject/refresh a small vendor-context system line (which vendor we are).
    context_line = {"role": "system", "content": f"LEVERANDØR: {vendor_name}"}
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
                iter_completion_stream,
                log_agent_run,
                log_tool_run,
                main_model,
                make_run_id,
                run_agent_with_fallback,
                user_facing_error_message,
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

            runtime_result = run_agent_with_fallback(
                messages=clean_messages,
                tools=VENDOR_TOOLS,
                tool_executor=_vendor_executor,
                username=f"vendor:{vendor_id}",
                session_id=sid,
                max_iterations=4,
                prompt_cache_key="futurematch-vendor",
                agent_scope="vendor",
            )

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
            full_text = runtime_result.text or ""
            if runtime_result.needs_final_stream or not full_text.strip():
                full_text = ""
                for token in iter_completion_stream(final_messages):
                    full_text += token
                    yield _vendor_sse({"type": "text", "content": token})
            else:
                yield _vendor_sse({"type": "text", "content": full_text})

            messages.append({"role": "assistant", "content": full_text, "_ts": time.time()})
            # Bound memory growth.
            if len(messages) > 30:
                VENDOR_CHAT_MEMORY[sid] = [messages[0]] + messages[-16:]

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
