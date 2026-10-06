"""Human booking/changes/evidence screens. Vendors use the same handler with their own context."""

import json
from flask import Blueprint, abort, flash, redirect, render_template, request, url_for
from auth_decorators import login_required
import order_service as orders
import order_timing
import order_fulfillment as service

fulfillment_bp = Blueprint("fulfillment", __name__)


def workflow(ctx, order_id, *, vendor=False):
    row = orders.get_order(ctx, order_id)
    if not row or not orders.actors_for(ctx, row):
        abort(404)
    actors = orders.actors_for(ctx, row)
    from capabilities import can

    may_review = not vendor and can("company.employees") and bool({"manager", "admin"} & actors)
    target = url_for("vendor.vendor_booking", order_id=order_id) if vendor else url_for("fulfillment.order_workflow", order_id=order_id)
    if request.method == "POST":
        action = request.form.get("action")
        if action == "book":
            result = orders.book_order(ctx, order_id, booking=request.form.to_dict(), note=request.form.get("reference"))
        elif action == "change":
            result = service.request_change(ctx, order_id, request.form.get("kind"), request.form.to_dict())
        elif action == "resolve":
            try:
                change_id = int(request.form.get("change_id"))
            except (ValueError, TypeError):
                abort(400)
            result = service.resolve_change(
                ctx,
                order_id,
                change_id,
                request.form.get("decision") == "accept",
                note=request.form.get("note", ""),
                fee=request.form.get("fee") or 0,
                new_reference=request.form.get("new_reference", ""),
                new_start_at=request.form.get("new_start_at", ""),
                new_instructions=request.form.get("new_instructions", ""),
            )
        elif action == "verify" and {"manager", "admin", "vendor"} & actors:
            result = orders.complete_order(ctx, order_id, note=request.form.get("note"))
        elif action == "report" and "owner" in actors:
            result = service.report_completion(
                ctx, order_id, note=request.form.get("evidence_note", ""), evidence_url=request.form.get("evidence_url", "")
            )
        elif action == "review" and may_review:
            names, levels = request.form.getlist("skill_name"), request.form.getlist("level")
            if len(names) != len(levels) or len(names) > 20:
                abort(400)
            ratings = [{"name": name, "level": level} for name, level in zip(names, levels) if name.strip()]
            result = service.outcome_review(
                ctx,
                order_id,
                ratings,
                note=request.form.get("note", ""),
            )
        else:
            abort(403)
        flash(
            result.get("message") or ("Gemt." if result.get("success") else "Handlingen kunne ikke gennemføres."),
            "success" if result.get("success") else "warning",
        )
        return redirect(target)
    cur = orders._dict_cursor(orders._get_connection())
    try:
        fulfillment = service.details(cur, order_id)
        cur.execute("SELECT * FROM course_order_changes WHERE order_id=%s ORDER BY created_at DESC", (order_id,))
        changes = list(cur.fetchall() or [])
        for change in changes:
            change["payload"] = json.loads(change["payload_json"])
        cur.execute("SELECT * FROM learning_outcome_reviews WHERE order_id=%s", (order_id,))
        review = cur.fetchone()
        if review:
            review["baseline"] = json.loads(review.get("baseline_json") or "{}")
        people = []
        if {"manager", "admin"} & actors and row.get("company_id"):
            cur.execute(
                "SELECT cu.user_id,COALESCE(cu.full_name,u.username) AS name FROM company_users cu JOIN users u ON u.id=cu.user_id WHERE cu.company_id=%s AND cu.status='active' ORDER BY name",
                (row["company_id"],),
            )
            people = list(cur.fetchall() or [])
        from enrollment_service import get_course, session_key

        product = get_course(row["product_handle"], row.get("company_id")) or {}
        variants = product.get("variants") or []
        for variant in variants:
            variant["session_id"] = session_key(variant)
        return render_template(
            "fm/vendor_booking.html" if vendor else "fm/booking_workflow.html",
            order=row,
            status_label=orders.lc.status_label(row["status"]),
            actors=actors,
            fulfillment=fulfillment,
            changes=changes,
            review=review,
            people=people,
            variants=variants,
            may_review=may_review,
            held_message=order_timing.not_yet_held_message(row, fulfillment.get("booking_json"))
            if row.get("status") == orders.lc.BOOKED else None,
            post_url=target,
            vendor_mode=vendor,
        )
    finally:
        cur.close()


@fulfillment_bp.route("/ordre/<order_id>/booking", methods=["GET", "POST"])
@login_required
def order_workflow(order_id):
    return workflow(orders.OrderContext.from_session(source="booking_console"), order_id)


@fulfillment_bp.route("/hr/ordre/<order_id>/udbytte", methods=["GET", "POST"])
@login_required
def outcome(order_id):
    return workflow(orders.OrderContext.from_session(source="outcome_review"), order_id)
