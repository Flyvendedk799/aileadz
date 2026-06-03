"""
GDPR data-subject routes (Theme A, roadmap value-4).

Endpoints
---------
User self-service (any logged-in user, acts only on themselves):
    GET  /mine-data                 -> download own PII export as JSON

Platform-admin data-subject requests (DSR):
    GET  /admin/gdpr                -> lookup + export/erase console (gdpr.html)
    POST /admin/gdpr/export         -> download a subject's PII export as JSON
    POST /admin/gdpr/erase          -> DESTRUCTIVE erasure, gated by:
                                         * dry-run preview first (default), and
                                         * an explicit typed-username confirmation
                                           that must EXACTLY match the target.

SAFETY
------
  * Erase is platform-admin-only by default (`require_role('admin')`). Company HR
    admins are NOT given erase here — keeping destructive power to platform admins
    avoids any cross-tenant mistake. Export likewise stays admin-only for the DSR
    console; ordinary users can still export *themselves* via /mine-data.
  * Erase requires `confirm_username` to EXACTLY equal `username`; otherwise the
    route refuses and only shows the dry-run plan.
  * The first erase POST (no confirmation, or `mode=preview`) always renders the
    dry-run plan and never mutates.
  * Every export/erase is written to audit_log (best-effort, never blocks the op).
  * This module imports cleanly without an app context and never crashes
    create_app(); the service layer is imported lazily inside handlers so a
    failure there can only affect the request, not boot.
  * All user-facing strings are Danish.
"""

import json
import logging

from flask import (
    Blueprint,
    Response,
    flash,
    redirect,
    render_template,
    request,
    session,
    url_for,
)

try:
    from auth_decorators import login_required, require_role
except Exception:  # pragma: no cover - keep import side-effect free / boot-safe
    # Fallback no-op-ish guards so importing this module can never crash boot.
    # These should never be used in practice (auth_decorators is always present);
    # they merely guarantee the blueprint module imports.
    def login_required(view):
        return view

    def require_role(*roles):
        def deco(view):
            return view
        return deco

logger = logging.getLogger(__name__)

gdpr_bp = Blueprint("gdpr", __name__, template_folder="templates")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _safe_filename_part(value):
    """Make a username safe for a download filename."""
    keep = []
    for ch in (value or "")[:60]:
        keep.append(ch if (ch.isalnum() or ch in "-_") else "_")
    return "".join(keep) or "bruger"


def _json_download(payload_str, username):
    """Build an attachment Response carrying the JSON export."""
    fname = f"gdpr-eksport-{_safe_filename_part(username)}.json"
    resp = Response(payload_str, mimetype="application/json; charset=utf-8")
    resp.headers["Content-Disposition"] = f'attachment; filename="{fname}"'
    resp.headers["Cache-Control"] = "no-store"
    return resp


def _audit(action, target_username, details):
    """Best-effort audit_log row. Never raises, never blocks the request."""
    try:
        from flask import current_app

        mysql = getattr(current_app, "mysql", None)
        if mysql is None:
            return
        try:
            from db_compat import refresh_flask_mysql_connection

            refresh_flask_mysql_connection(mysql)
        except Exception:
            pass
        conn = mysql.connection
        cur = conn.cursor()
        try:
            cur.execute(
                """
                INSERT INTO audit_log
                    (company_id, user_id, action, action_type, resource_type,
                     resource_id, description, ip_address)
                VALUES (%s, %s, %s, %s, 'gdpr', %s, %s, %s)
                """,
                (
                    session.get("company_id"),
                    session.get("user_id"),
                    action,
                    action,
                    str(target_username or "")[:100],
                    json.dumps(details, ensure_ascii=False)[:4000],
                    (request.remote_addr or "")[:50],
                ),
            )
            conn.commit()
        finally:
            try:
                cur.close()
            except Exception:
                pass
    except Exception as exc:  # pragma: no cover - audit must never break the op
        logger.debug("gdpr audit_log skipped (%s): %s", action, exc)


# ---------------------------------------------------------------------------
# User self-service
# ---------------------------------------------------------------------------

@gdpr_bp.route("/mine-data", methods=["GET"])
@login_required
def my_data():
    """Download the *current* user's own PII export (GDPR access right)."""
    username = session.get("user")
    if not username:
        flash("Log ind for at hente dine data.", "warning")
        return redirect(url_for("auth.login"))

    try:
        from gdpr_service import export_user_data

        payload = export_user_data(username)
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("gdpr.my_data fejlede for %s: %s", username, exc)
        flash("Kunne ikke generere din dataeksport lige nu. Prøv igen senere.", "danger")
        return redirect(url_for("dashboard.dashboard"))

    _audit("gdpr_self_export", username, {"by": "self"})
    return _json_download(payload, username)


# ---------------------------------------------------------------------------
# Platform-admin DSR console
# ---------------------------------------------------------------------------

@gdpr_bp.route("/admin/gdpr", methods=["GET"])
@require_role("admin")
def admin_console():
    """Render the GDPR data-subject console (lookup + export/erase)."""
    username = (request.args.get("username") or "").strip()
    summary = None
    if username:
        try:
            from gdpr_service import collect_user_data

            data = collect_user_data(username)
            summary = {
                "username": username,
                "row_counts": data.get("row_counts", {}),
                "total_rows": sum((data.get("row_counts") or {}).values()),
                "errors": data.get("errors", []),
            }
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning("gdpr.admin_console lookup fejlede: %s", exc)
            summary = {"username": username, "row_counts": {}, "total_rows": 0,
                       "errors": [f"Opslag fejlede: {exc}"]}

    return render_template("fm/gdpr.html", lookup_username=username, summary=summary, plan=None)


@gdpr_bp.route("/admin/gdpr/export", methods=["POST"])
@require_role("admin")
def admin_export():
    """Download a data subject's full PII export as JSON (platform admin)."""
    username = (request.form.get("username") or "").strip()
    if not username:
        flash("Angiv et brugernavn for at eksportere.", "warning")
        return redirect(url_for("gdpr.admin_console"))

    try:
        from gdpr_service import export_user_data

        payload = export_user_data(username)
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("gdpr.admin_export fejlede for %s: %s", username, exc)
        flash("Kunne ikke generere eksporten. Prøv igen senere.", "danger")
        return redirect(url_for("gdpr.admin_console", username=username))

    _audit("gdpr_admin_export", username, {"by": session.get("user")})
    return _json_download(payload, username)


@gdpr_bp.route("/admin/gdpr/erase", methods=["POST"])
@require_role("admin")
def admin_erase():
    """DESTRUCTIVE erasure, gated by dry-run preview + typed confirmation.

    Flow:
      1. mode=preview (or no confirmation) -> compute + show the dry-run plan,
         mutate NOTHING.
      2. mode=execute + confirm_username == username (exact match) -> run the
         erasure inside one transaction.
    Any mismatch falls back to the dry-run preview with a Danish warning.
    """
    username = (request.form.get("username") or "").strip()
    confirm = (request.form.get("confirm_username") or "").strip()
    mode = (request.form.get("mode") or "preview").strip()
    # Optional ai_memory linkage (anonymous SQLite store).
    browser_token = (request.form.get("browser_token") or "").strip() or None
    session_id = (request.form.get("session_id") or "").strip() or None

    actor = session.get("user")

    if not username:
        flash("Angiv et brugernavn for at fortsætte.", "warning")
        return redirect(url_for("gdpr.admin_console"))

    # Hard refusal: never let an admin erase their own logged-in account by
    # accident from this console (avoids self-lockout / surprise).
    if username == actor:
        flash(
            "Du kan ikke slette din egen administratorkonto fra denne konsol.",
            "danger",
        )
        return redirect(url_for("gdpr.admin_console", username=username))

    try:
        from gdpr_service import erase_user_data
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("gdpr.admin_erase: service utilgængelig: %s", exc)
        flash("GDPR-tjenesten er utilgængelig lige nu.", "danger")
        return redirect(url_for("gdpr.admin_console", username=username))

    # ---- Decide: real execution requires explicit mode + exact confirmation ----
    do_execute = (mode == "execute") and (confirm == username)

    if mode == "execute" and confirm != username:
        flash(
            "Bekræftelsen matchede ikke brugernavnet. Skriv det præcise brugernavn "
            "for at bekræfte sletning. Viser kun forhåndsvisning.",
            "warning",
        )

    plan = erase_user_data(
        username,
        actor=actor,
        dry_run=not do_execute,
        browser_token=browser_token,
        session_id=session_id,
    )

    if do_execute:
        _audit(
            "gdpr_admin_erase",
            username,
            {
                "by": actor,
                "ok": plan.get("ok"),
                "deleted": {k: v.get("rows") for k, v in plan.get("deleted", {}).items()},
                "anonymised": {k: v.get("rows") for k, v in plan.get("anonymised", {}).items()},
                "ai_memory": plan.get("ai_memory"),
                "errors": plan.get("errors"),
            },
        )
        if plan.get("ok"):
            flash(
                f"Sletning gennemført for '{username}'. Profilrækker slettet, "
                "regnskabs-/revisionsrækker anonymiseret.",
                "success",
            )
        else:
            flash(
                "Sletningen blev rullet tilbage — ingen ændringer blev gemt. "
                + " ".join(plan.get("errors", [])),
                "danger",
            )
    else:
        _audit("gdpr_admin_erase_preview", username, {"by": actor})

    # Build a lookup summary so the page still shows context.
    summary = None
    try:
        from gdpr_service import collect_user_data

        data = collect_user_data(username)
        summary = {
            "username": username,
            "row_counts": data.get("row_counts", {}),
            "total_rows": sum((data.get("row_counts") or {}).values()),
            "errors": data.get("errors", []),
        }
    except Exception:
        summary = {"username": username, "row_counts": {}, "total_rows": 0, "errors": []}

    return render_template(
        "fm/gdpr.html",
        lookup_username=username,
        summary=summary,
        plan=plan,
        executed=do_execute,
    )


__all__ = ["gdpr_bp"]
