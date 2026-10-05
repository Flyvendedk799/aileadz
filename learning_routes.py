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


@learning_bp.route("/min-laering/forloeb/<int:progress_id>")
@login_required
def assignment(progress_id):
    ctx = OrderContext.from_session(source="assignment")
    cur = cursor()
    try:
        item = paths.assignment_detail(cur, ctx.company_id, ctx.user_id, progress_id)
        if not item:
            abort(404)
        for step in item["steps"]:
            product = enrollment.get_course(step["course_handle"], ctx.company_id) if step.get("course_handle") else None
            step["variants"] = (product or {}).get("variants") or []
            for variant in step["variants"]:
                variant["session_id"] = enrollment.session_key(variant)
        return render_template("fm/learning_assignment.html", assignment=item)
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
        if request.form.get("action") == "acknowledge":
            result = paths.acknowledge_step(cur, ctx.company_id, ctx.user_id, step_id)
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
