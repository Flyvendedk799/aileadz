"""Actionable learner assignments and company-only internal-course discovery."""

from urllib.parse import urlparse
from flask import Blueprint, abort, current_app, flash, redirect, render_template, request, session, url_for
from auth_decorators import login_required
from order_service import OrderContext
import learning_path_service as paths
import enrollment_service as enrollment

learning_bp = Blueprint("learning", __name__)


def cursor():
    import MySQLdb.cursors

    return current_app.mysql.connection.cursor(MySQLdb.cursors.DictCursor)


def _present_assignment(cur, ctx, item):
    """What the learner reads: real course titles, each order's true status, session dates, who
    assigned the path and, for a sequential path, when the next course will be ordered. Internal
    details (path version, raw step status) stay out of it."""
    import order_lifecycle as lc
    import order_timing
    from person_names import display_name

    done = ("completed", "skipped")
    sequential = paths.normalize_mode(item.get("ordering_mode")) == "sequential"
    steps = item["steps"]
    for step in steps:
        product = enrollment.get_course(step["course_handle"], ctx.company_id) if step.get("course_handle") else None
        step["variants"] = (product or {}).get("variants") or []
        for variant in step["variants"]:
            variant["session_id"] = enrollment.session_key(variant)
        label = (step.get("title") or "").strip()
        real = ((product or {}).get("title") or "").strip()
        step["display_title"] = real or label or "Trin"
        step["label"] = label if real and label and label != real else ""
        step["is_course"] = bool(step.get("course_handle"))
        step["status_text"], step["status_tone"] = _step_status(step, lc, sequential, done)
        step["session_text"] = ""
        if step.get("order_id") and step.get("order_status"):
            step["session_text"] = order_timing.course_label(
                {"variant_date": step.get("order_variant_date"), "booking_json": step.get("order_booking_json")}, with_time=False)
        # A sequential course that is not due yet is ordered automatically: no button to press.
        step["auto_ordered_later"] = bool(sequential and step["is_course"] and not step.get("order_id")
                                          and step.get("status") == "not_started")
    item["assigned_by"] = (display_name(cur, ctx.company_id, user_id=item["assigned_by_user_id"])
                           if item.get("assigned_by_user_id") else "")
    item["sequential"] = sequential
    item["next_note"] = ""
    if sequential:
        waiting = next((s for s in steps if s["auto_ordered_later"]), None)
        if waiting:
            blockers = [s for s in steps[:steps.index(waiting)] if s.get("status") not in done]
            if not blockers:
                item["next_note"] = "Næste kursus, ‘%s’, bestilles automatisk." % waiting["display_title"]
            else:
                item["next_note"] = "Næste kursus, ‘%s’, bestilles, når du har gennemført %s." % (
                    waiting["display_title"],
                    "‘%s’" % blockers[0]["display_title"] if len(blockers) == 1 else "de forrige trin")
    return item


def _step_status(step, lc, sequential, done):
    """(text, tone): the order's own status label for a course, never a generic "Bestilt"."""
    status = step.get("status")
    if status == "completed":
        return "Gennemført", "green"
    if status == "skipped":
        return "Sprunget over", ""
    if step.get("order_id") and step.get("order_status"):
        text = lc.status_label(step["order_status"], short=True)
        return text, {"completed": "green", "booked": "teal", "approved": "teal", "pending_approval": "amber"}.get(
            lc.normalize_status(step["order_status"]), "red")
    if status == "failed":
        return "Kræver opfølgning", "red"
    if step.get("course_handle"):
        return ("Bestilles senere", "") if sequential else ("Ikke bestilt endnu", "amber")
    return "Ikke startet", ""


@learning_bp.route("/min-laering/forloeb/<int:progress_id>")
@login_required
def assignment(progress_id):
    ctx = OrderContext.from_session(source="assignment")
    cur = cursor()
    try:
        item = paths.assignment_detail(cur, ctx.company_id, ctx.user_id, progress_id)
        if not item:
            abort(404)
        return render_template("fm/learning_assignment.html", assignment=_present_assignment(cur, ctx, item))
    finally:
        cur.close()


@learning_bp.route("/min-laering/forloeb/<int:progress_id>/trin/<int:step_id>", methods=["POST"])
@login_required
def act_on_step(progress_id, step_id):
    ctx = OrderContext.from_session(source="assignment")
    cur = cursor()
    try:
        item = paths.assignment_detail(cur, ctx.company_id, ctx.user_id, progress_id)
        if not item or step_id not in [s["id"] for s in item["steps"]]:
            abort(404)
        action = request.form.get("action")
        if action in ("acknowledge", "skip"):
            result = paths.acknowledge_step(cur, ctx.company_id, ctx.user_id, step_id, skipped=(action == "skip"),
                                            note=(request.form.get("note") or "").strip())
            current_app.mysql.connection.commit()
        else:
            result = paths.order_assignment_step(ctx, step_id, session_id=request.form.get("session_id"))
        flash(
            result.get("message") or ("Trinnet er opdateret." if result.get("success") else "Handlingen kunne ikke gennemføres."),
            "success" if result.get("success") else "warning",
        )
        return redirect(url_for("learning.assignment", progress_id=progress_id))
    finally:
        cur.close()


@learning_bp.route("/interne-kurser")
@login_required
def internal_courses():
    cur = cursor()
    try:
        cur.execute(
            "SELECT id,title,description,format,duration_hours,price,location FROM company_courses WHERE company_id = %s AND is_active = 1 ORDER BY title",
            (session.get("company_id"),),
        )
        return render_template("fm/internal_catalog.html", courses=list(cur.fetchall() or []))
    finally:
        cur.close()


@learning_bp.route("/interne-kurser/<int:course_id>", methods=["GET", "POST"])
@login_required
def internal_course(course_id):
    ctx = OrderContext.from_session(source="internal_course")
    product = enrollment.get_course("internal:%s" % course_id, ctx.company_id)
    if not product:
        abort(404)
    cur = cursor()
    try:
        cur.execute("SELECT email FROM users WHERE id=%s", (ctx.user_id,))
        contact = cur.fetchone() or {}
    finally:
        cur.close()
    if request.method == "POST":
        result = enrollment.create_order(
            ctx,
            product_handle=product["handle"],
            user_email=request.form.get("email") or contact.get("email") or "",
            user_name=ctx.username or "",
            extra={"notes": request.form.get("notes") or "", "expected_price": request.form.get("expected_price")},
        )
        if result.get("success"):
            return redirect(url_for("futurematch.my_order", order_id=result["order_id"]))
        flash(result.get("message") or "Bestillingen kunne ikke oprettes.", "danger")
    if urlparse(product.get("external_url") or "").scheme not in ("http", "https"):
        product["external_url"] = ""
    return render_template("fm/internal_course_detail.html", product=product, contact_email=contact.get("email") or "")
