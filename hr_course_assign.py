"""HR bulk-assign a COURSE to colleagues (N-5.2, ``hr_bulk_assign`` policy).

The chat hands a team request to HR as a notification linking here with the
course and participants pre-filled. HR confirms and the page creates one order per
person through ``order_service.create_order`` (HR is a manager, so the orders are
approved at once and charged to each person's department budget), all sharing one
``group_order_id``.
"""

from __future__ import annotations

import logging

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
    import enrollment_service as catalog
    company_id = session["company_id"]

    if request.method == "POST":
        handle = request.form.get("handle", "")
        product = catalog.get_course(handle, company_id)
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

        from order_service import OrderContext
        from enrollment_service import session_key
        from learning_path_service import assign_course_to_people
        cur = _cur()
        try:
            selected_session = request.form.get('session_id') or (session_key(variant) if variant else None)
            if request.form.get('confirm') != 'yes':
                placeholders = ','.join(['%s']*len(ids))
                cur.execute('SELECT cu.user_id,COALESCE(cu.full_name,u.username) AS name FROM company_users cu JOIN users u ON u.id=cu.user_id WHERE cu.company_id=%s AND cu.status=%s AND cu.user_id IN ('+placeholders+')',tuple([company_id,'active']+ids))
                people = list(cur.fetchall() or [])
                if not people:
                    flash('Vælg aktive medarbejdere i virksomheden.','warning')
                    return redirect(url_for('course_assign.assign_course',course=handle))
                try:
                    quote = catalog.quote_course(handle,company_id,session_id=selected_session,participants=len(people))
                except ValueError as exc:
                    flash(str(exc),'warning')
                    return redirect(url_for('course_assign.assign_course',course=handle))
                return render_template('fm/confirm_course_assignment.html',quote=quote,people=people)
            result = assign_course_to_people(cur, OrderContext.from_session(source='hr_assign_course'), company_id,
                                             handle, ids, session_id=selected_session, expected_price=request.form.get('expected_price'))
            current_app.mysql.connection.commit()
        finally:
            cur.close()
        created, failed = result['orders'], result['order_failures']
        flash(f"{created} ordre(r) oprettet til '{product['title']}'."
              + (f" {failed} kunne ikke oprettes. " + (result.get("message") or "") if failed else ""), "success" if created else "danger")
        return redirect(url_for("hr_dashboard.pending_approvals"))

    handle = request.args.get("course", "")
    product = catalog.get_course(handle, company_id) if handle else None
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
