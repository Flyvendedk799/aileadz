"""Booking, change, attendance and outcome handling for one order, for every role.

One action handler (``perform_action``) and one view-model (``order_sections``)
serve every host page: the learner's ``/min-ordre/<id>``, HR's
``/hr/order/<id>/details`` and the vendor's ``/vendor/orders/<id>/booking``.
The section partials under ``templates/fm/order_sections/`` only render what
``order_sections`` hands them; which section is valid for which state and actor
is decided here and nowhere else.
"""

import json
from flask import Blueprint, abort, flash, redirect, render_template, request, url_for
from auth_decorators import login_required
import order_service as orders
import order_timing
import order_fulfillment as service

fulfillment_bp = Blueprint("fulfillment", __name__)

# Where the page scrolls to after each action.
ACTION_ANCHORS = {
    "book": "booking",
    "reference": "booking",
    "change": "aendring",
    "resolve": "aendring",
    "report": "deltagelse",
    "verify": "deltagelse",
    "review": "udbytte",
}

_MAX_OUTCOME_ROWS = 5


def _truthy(value):
    return str(value or "").strip().lower() in ("1", "true", "on", "yes")


def perform_action(ctx, order_id, form, *, vendor=False):
    """Run one console action posted by ``ctx`` and return ``(result, anchor)``.

    The single action handler behind every order page. Unknown or not permitted
    actions abort (404/403/400) exactly as the old console did."""
    row = orders.get_order(ctx, order_id)
    if not row or not orders.actors_for(ctx, row):
        abort(404)
    actors = orders.actors_for(ctx, row)
    from capabilities import can

    may_review = not vendor and can("company.employees") and bool({"manager", "admin"} & actors)
    action = form.get("action")
    if action == "book":
        result = orders.book_order(ctx, order_id, booking=form.to_dict() if hasattr(form, "to_dict") else dict(form), note=None)
    elif action == "change":
        result = service.request_change(ctx, order_id, form.get("kind"), form.to_dict() if hasattr(form, "to_dict") else dict(form))
    elif action == "resolve":
        try:
            change_id = int(form.get("change_id"))
        except (ValueError, TypeError):
            abort(400)
        result = service.resolve_change(
            ctx,
            order_id,
            change_id,
            form.get("decision") == "accept",
            note=form.get("note", ""),
            fee=form.get("fee") or 0,
            new_reference=form.get("new_reference", ""),
            new_start_at=form.get("new_start_at", ""),
            new_instructions=form.get("new_instructions", ""),
        )
    elif action == "reference":
        result = service.add_reference(ctx, order_id, form.get("reference"))
    elif action == "verify" and {"manager", "admin", "vendor"} & actors:
        result = orders.complete_order(ctx, order_id, note=form.get("note"))
    elif action == "report" and "owner" in actors:
        result = service.report_completion(
            ctx, order_id, note=form.get("evidence_note", ""), evidence_url=form.get("evidence_url", "")
        )
    elif action == "review" and may_review:
        names, levels = form.getlist("skill_name"), form.getlist("level")
        if len(names) != len(levels) or len(names) > 20:
            abort(400)
        ratings = [{"name": name, "level": level} for name, level in zip(names, levels) if name.strip()]
        result = service.outcome_review(ctx, order_id, ratings, note=form.get("note", ""))
    else:
        abort(403)
    return result, ACTION_ANCHORS.get(action, "")


def flash_result(result):
    flash(
        result.get("message") or ("Gemt." if result.get("success") else "Handlingen kunne ikke gennemføres."),
        "success" if result.get("success") else "warning",
    )


def _local_value(start):
    """``datetime-local`` value (``YYYY-MM-DDTHH:MM``) for a Copenhagen datetime."""
    return start.strftime("%Y-%m-%dT%H:%M") if start else ""


def _outcome_rows(row):
    """Skill rows for the outcome review: the course's own topics (never format or
    location tags) when the catalogue knows them, else blank rows."""
    names = []
    try:
        import completion_service

        names = [p["name"] for p in completion_service.skills_for_product(completion_service._product(row.get("product_handle")))]
    except Exception:
        names = []
    names = names[:_MAX_OUTCOME_ROWS] or ["", "", ""]
    return [{"name": name, "required": index == 0} for index, name in enumerate(names)]


def order_sections(ctx, row, *, vendor=False, post_url=None):
    """What the order page shows for ``row`` and ``ctx``: one dict, templates stay dumb.

    ``visible`` lists the section partials to render, in order (``pending``,
    ``booking_card``, ``book_form``, ``change_request``, ``changes_list``,
    ``attendance``, ``outcome_review``); the other keys are the data they need."""
    from capabilities import can
    from enrollment_service import get_course, session_key

    order_id = row["order_id"]
    actors = orders.actors_for(ctx, row)
    status = orders.lc.normalize_status(row.get("status"))
    manager = bool({"manager", "admin"} & actors)
    may_review = not vendor and bool(can("company.employees")) and manager
    cur = orders._dict_cursor(orders._get_connection())
    try:
        fulfillment = service.details(cur, order_id)
        cur.execute("SELECT * FROM course_order_changes WHERE order_id=%s ORDER BY created_at DESC, id DESC", (order_id,))
        changes = list(cur.fetchall() or [])
        for change in changes:
            try:
                change["payload"] = json.loads(change["payload_json"] or "{}")
            except (TypeError, ValueError):
                change["payload"] = {}
            counterpart = {"owner", "manager", "admin"} if change.get("requested_kind") == "vendor" else {"vendor", "manager", "admin"}
            change["can_resolve"] = change.get("status") == "pending" and bool(counterpart & actors)
        changes.sort(key=lambda c: 0 if c.get("status") == "pending" else 1)
        pending_change = next((c for c in changes if c.get("status") == "pending"), None)
        review = None
        people = []
        if status == orders.lc.COMPLETED:
            cur.execute("SELECT * FROM learning_outcome_reviews WHERE order_id=%s", (order_id,))
            review = cur.fetchone()
            if review:
                try:
                    review["baseline"] = json.loads(review.get("baseline_json") or "{}")
                except (TypeError, ValueError):
                    review["baseline"] = {}
        if status == orders.lc.BOOKED and manager and row.get("company_id"):
            cur.execute(
                "SELECT cu.user_id,COALESCE(cu.full_name,u.username) AS name FROM company_users cu JOIN users u ON u.id=cu.user_id WHERE cu.company_id=%s AND cu.status='active' ORDER BY name",
                (row["company_id"],),
            )
            people = list(cur.fetchall() or [])
    finally:
        cur.close()

    booking = fulfillment.get("booking_json") or {}
    variants = []
    if status == orders.lc.BOOKED:
        try:
            product = get_course(row["product_handle"], row.get("company_id")) or {}
            variants = list(product.get("variants") or [])
            for variant in variants:
                variant["session_id"] = session_key(variant)
        except Exception:
            variants = []

    held_message = order_timing.not_yet_held_message(row, booking) if status == orders.lc.BOOKED else None
    completion_state = fulfillment.get("completion_state") or "none"
    can_book = status == orders.lc.APPROVED and bool({"vendor", "manager", "admin"} & actors)
    start = order_timing.course_start(row)
    ordered_label = str(row.get("variant_date") or "")
    book_defaults = {
        "start_at": _local_value(start) if can_book else "",
        "location": row.get("variant_location") or "",
        "ordered_label": ordered_label if start else "",
        "ordered_day": start.strftime("%Y-%m-%d") if start else "",
        "cancellation_terms": (fulfillment.get("quote_json") or {}).get("cancellation_terms") or "",
    }
    can_verify = status == orders.lc.BOOKED and not held_message and bool({"manager", "admin", "vendor"} & actors)
    can_report = (
        status == orders.lc.BOOKED and not held_message and "owner" in actors and completion_state != "reported"
    )

    show = {
        "pending": status == orders.lc.PENDING_APPROVAL,
        "booking_card": status in (orders.lc.BOOKED, orders.lc.COMPLETED) and bool(booking),
        "book_form": can_book,
        "change_request": status == orders.lc.BOOKED and pending_change is None and bool(actors),
        "changes_list": bool(changes),
        "attendance": status in (orders.lc.BOOKED, orders.lc.COMPLETED),
        "outcome_review": status == orders.lc.COMPLETED and may_review and bool(review),
        "outcome_summary": status == orders.lc.COMPLETED and not may_review and (review or {}).get("status") == "completed",
    }
    approval_url = None
    if show["pending"] and manager and not vendor:
        approval_url = url_for("hr_dashboard.pending_approvals")
    change_kinds = [("cancel", "Afbestilling")]
    if variants:
        change_kinds.append(("reschedule", "Skift hold"))
    if people:
        change_kinds.append(("substitute", "Skift deltager"))
    return {
        "order_id": order_id,
        "status": status,
        "actors": actors,
        "visible": [name for name in ("pending", "booking_card", "book_form", "changes_list", "change_request", "attendance", "outcome_review", "outcome_summary") if show[name]],
        "show": show,
        "fulfillment": fulfillment,
        "booking": booking,
        "changes": changes,
        "pending_change": pending_change,
        "review": review,
        "people": people,
        "variants": variants,
        "change_kinds": change_kinds,
        "held_message": held_message,
        "completion_state": completion_state,
        "can_report": can_report,
        "can_verify": can_verify,
        "can_add_reference": status == orders.lc.BOOKED and bool({"vendor", "manager", "admin"} & actors),
        "may_review": may_review,
        "book_defaults": book_defaults,
        "approval_url": approval_url,
        "outcome_rows": _outcome_rows(row) if show["outcome_review"] and not (review or {}).get("status") == "completed" else [],
        "post_url": post_url,
        "vendor_mode": vendor,
    }


def order_page_url(order_id, actors, anchor=""):
    """The one order page of the strongest role among ``actors``."""
    if "vendor" in actors:
        url = url_for("vendor.vendor_booking", order_id=order_id)
    elif "manager" in actors:
        url = url_for("hr_dashboard.company_order_details", order_id=order_id)
    elif "admin" in actors:
        url = url_for("admin_dashboard.admin_order_detail", order_id=order_id)
    else:
        url = url_for("futurematch.my_order", order_id=order_id)
    return url + ("#" + anchor if anchor else "")


def workflow(ctx, order_id, *, vendor=False):
    row = orders.get_order(ctx, order_id)
    if not row or not orders.actors_for(ctx, row):
        abort(404)
    target = url_for("vendor.vendor_booking", order_id=order_id) if vendor else url_for("fulfillment.order_workflow", order_id=order_id)
    if request.method == "POST":
        result, anchor = perform_action(ctx, order_id, request.form, vendor=vendor)
        flash_result(result)
        return redirect(target + ("#" + anchor if anchor else ""))
    sections = order_sections(ctx, row, vendor=vendor, post_url=target)
    return render_template(
        "fm/vendor_booking.html" if vendor else "fm/booking_workflow.html",
        order=row,
        status_label=orders.lc.status_label(row["status"]),
        actors=sections["actors"],
        sections=sections,
        post_url=target,
        vendor_mode=vendor,
    )


@fulfillment_bp.route("/ordre/<order_id>/handling", methods=["POST"])
@login_required
def order_action(order_id):
    """Every action posted from the HR and admin order pages."""
    ctx = orders.OrderContext.from_session(source="order_page")
    row = orders.get_order(ctx, order_id)
    actors = orders.actors_for(ctx, row) if row else set()
    if not actors:
        abort(404)
    result, anchor = perform_action(ctx, order_id, request.form)
    flash_result(result)
    return redirect(order_page_url(order_id, actors, anchor))


@fulfillment_bp.route("/ordre/<order_id>/booking", methods=["GET", "POST"])
@login_required
def order_workflow(order_id):
    return workflow(orders.OrderContext.from_session(source="booking_console"), order_id)


@fulfillment_bp.route("/hr/ordre/<order_id>/udbytte", methods=["GET", "POST"])
@login_required
def outcome(order_id):
    return workflow(orders.OrderContext.from_session(source="outcome_review"), order_id)
