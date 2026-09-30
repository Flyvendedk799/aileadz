"""HR bulk-assign a COURSE to colleagues (N-5.2, ``hr_bulk_assign`` policy).

The chat hands a team request to HR as a notification linking here with the
course and participants pre-filled. HR confirms and the page creates one order per
person through ``order_service.create_order`` (HR is a manager, so the orders are
approved at once and charged to each person's department budget), all sharing one
``group_order_id``.
"""

from __future__ import annotations

import logging
import uuid

from flask import Blueprint, current_app, flash, redirect, render_template, request, session, url_for

import capabilities
from auth_decorators import login_required

logger = logging.getLogger(__name__)

course_assign_bp = Blueprint("course_assign", __name__)


def _cur():
    import MySQLdb.cursors
    return current_app.mysql.connection.cursor(MySQLdb.cursors.DictCursor)


def _allowed():
    return bool(session.get("company_id")) and capabilities.can("company.employees")


@course_assign_bp.route("/hr/assign-course", methods=["GET", "POST"])
@login_required
def assign_course():
    if not _allowed():
        flash("Kun HR kan tildele kurser til hold.", "warning")
        return redirect(url_for("futurematch.employee_home"))
    import catalog_service as catalog
    company_id = session["company_id"]

    if request.method == "POST":
        handle = request.form.get("handle", "")
        product = catalog.get_product(handle)
        if not product:
            flash("Kurset blev ikke fundet i kataloget.", "danger")
            return redirect(url_for("hr_dashboard.dashboard"))
        ids = []
        for v in request.form.getlist("employee_ids"):
            try:
                ids.append(int(v))
            except ValueError:
                pass
        if not ids:
            flash("Vælg mindst én medarbejder.", "warning")
            return redirect(request.referrer or url_for("course_assign.assign_course", course=handle))
        try:
            vi = int(request.form.get("variant_index", -1))
        except ValueError:
            vi = -1
        variants = product.get("variants") or []
        variant = variants[vi] if 0 <= vi < len(variants) else (variants[0] if len(variants) == 1 else {})
        price = variant.get("price") if variant.get("price") is not None else (product.get("price_min") or 0)

        from order_service import OrderContext, create_order
        cur = _cur()
        try:
            cur.execute(
                "SELECT cu.user_id AS user_id, COALESCE(u.username, cu.username) AS username, cu.full_name AS full_name, "
                "COALESCE(cu.email, u.email) AS email, cu.department AS department FROM company_users cu "
                "LEFT JOIN users u ON u.id = cu.user_id WHERE cu.company_id = %s AND cu.status = 'active' "
                "AND cu.user_id IN (" + ",".join(["%s"] * len(ids)) + ")", tuple([company_id] + ids))
            people = list(cur.fetchall() or [])
        finally:
            cur.close()
        actor = OrderContext.from_session(source="hr_assign_course")
        group_id = uuid.uuid4().hex
        created = failed = 0
        for p in people:
            ctx = OrderContext(company_id=company_id, user_id=p["user_id"], username=p["username"],
                               company_role=actor.company_role or "hr_manager",
                               department=p.get("department") or "", source="hr_assign_course",
                               is_platform_admin=actor.is_platform_admin, actor_label=actor.username)
            res = create_order(
                ctx, product_handle=handle, product_title=product["title"], price=price,
                variant_date=variant.get("date", ""), variant_location=variant.get("location", ""),
                user_email=p.get("email") or "", user_name=p.get("full_name") or p["username"],
                extra={"group_order_id": group_id, "department": p.get("department") or "",
                       "notes": f"Tildelt af {actor.username} (HR)"})
            if res.get("success"):
                created += 1
            else:
                failed += 1
        flash(f"{created} ordre(r) oprettet til '{product['title']}'."
              + (f" {failed} kunne ikke oprettes." if failed else ""), "success" if created else "danger")
        return redirect(url_for("hr_dashboard.pending_approvals"))

    handle = request.args.get("course", "")
    product = catalog.get_product(handle) if handle else None
    pre = set()
    for v in (request.args.get("users") or "").split(","):
        if v.strip().isdigit():
            pre.add(int(v))
    cur = _cur()
    try:
        cur.execute(
            "SELECT cu.user_id AS user_id, COALESCE(cu.full_name, u.username) AS name, cu.department AS department "
            "FROM company_users cu LEFT JOIN users u ON u.id = cu.user_id WHERE cu.company_id = %s "
            "AND cu.status = 'active' ORDER BY cu.department, name", (company_id,))
        employees = list(cur.fetchall() or [])
    finally:
        cur.close()
    return render_template("fm/assign_course.html", product=product, employees=employees, preselected=pre)
