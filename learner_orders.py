"""Learner order detail (N-1.2) + the completion moment (N-1.3/N-1.4).

Routes (all on the ``futurematch`` blueprint, login required, owner only):

* ``GET  /min-ordre/<order_id>``              status, history, vendor, date, actions
* ``POST /min-ordre/<order_id>/annuller``     cancel (budget refunded exactly once)
* ``POST /min-ordre/<order_id>/gennemfoert``  "Markér som gennemført"
* ``POST /min-ordre/<order_id>/kompetencer``  save the skills the learner accepted
* ``GET  /min-ordre/<order_id>/kalender.ics`` add to calendar

A manager who opens a colleague's order link is sent to the HR order page, and
anybody else gets a 404 (never a hint that the order exists).
"""

from __future__ import annotations

import logging

from flask import (abort, current_app, flash, jsonify, redirect, render_template,
                   request, Response, session, url_for)

import order_lifecycle as lc

logger = logging.getLogger(__name__)


def _ctx():
    from order_service import OrderContext
    return OrderContext.from_session(source="web")


def _load(order_id):
    """(row, actors) for the session user, or (None, set())."""
    import order_service
    ctx = _ctx()
    row = order_service.get_order(ctx, order_id)
    if not row:
        return None, set(), ctx
    return row, order_service.actors_for(ctx, row), ctx


def _wants_json():
    return request.is_json or request.accept_mimetypes.best == "application/json" \
        or request.headers.get("X-Requested-With") == "XMLHttpRequest"


def _can_cancel(status):
    return lc.normalize_status(status) in lc.OPEN_STATUSES


def _can_complete(status):
    return lc.normalize_status(status) in (lc.APPROVED, lc.BOOKED)


def register_learner_order_routes(bp):
    @bp.route("/min-ordre/<order_id>")
    def my_order(order_id):
        if not session.get("user"):
            flash("Log ind for at se din bestilling.", "danger")
            return redirect(url_for("auth.login"))
        row, actors, _ = _load(order_id)
        if not row:
            abort(404)
        if "owner" not in actors:
            if {"manager", "admin"} & actors:
                return redirect(url_for("hr_dashboard.company_order_details", order_id=order_id))
            abort(404)

        import order_service
        history = order_service.get_history(_ctx(), order_id)
        status = lc.normalize_status(row.get("status"))
        moment = None
        if status == lc.COMPLETED:
            import completion_service
            moment = completion_service.completion_moment(row)
        vendor_name = ""
        try:
            import catalog_service
            product = catalog_service.get_product(row.get("product_handle"))
            vendor_name = (product or {}).get("vendor") or ""
        except Exception:
            pass
        return render_template(
            "fm/my_order.html",
            order=row,
            status=status,
            status_label=lc.status_label(status),
            status_hint=lc.STATUS_HINTS[status],
            status_tone=lc.STATUS_TONES[status],
            history=history,
            vendor_name=vendor_name,
            can_cancel=_can_cancel(status),
            can_complete=_can_complete(status),
            has_date=bool(row.get("variant_date")),
            moment=moment,
            just_completed=request.args.get("completed") == "1",
            billing_label=lc.BILLING_LEARNER_LABELS[lc.normalize_billing(row.get("billing_status"))],
            status_labels=lc.STATUS_LABELS_SHORT,
        )

    @bp.route("/min-ordre/<order_id>/annuller", methods=["POST"])
    def my_order_cancel(order_id):
        if not session.get("user"):
            return jsonify({"success": False, "message": "Log ind først."}), 401
        import order_service
        reason = (request.form.get("reason") or (request.get_json(silent=True) or {}).get("reason") or "").strip()
        res = order_service.cancel_order(_ctx(), order_id, reason=reason or None)
        if _wants_json():
            code = 200 if res.get("success") else (404 if res.get("error") == "not_found" else 400)
            return jsonify(res), code
        if res.get("success"):
            flash("Din bestilling er annulleret.", "success")
        else:
            flash(res.get("message") or "Bestillingen kunne ikke annulleres.", "danger")
        return redirect(url_for("futurematch.my_order", order_id=order_id))

    @bp.route("/min-ordre/<order_id>/gennemfoert", methods=["POST"])
    def my_order_complete(order_id):
        if not session.get("user"):
            return jsonify({"success": False, "message": "Log ind først."}), 401
        import order_service
        res = order_service.complete_order(_ctx(), order_id)
        if _wants_json():
            code = 200 if res.get("success") else (404 if res.get("error") == "not_found" else 400)
            return jsonify(res), code
        if res.get("success"):
            return redirect(url_for("futurematch.my_order", order_id=order_id, completed=1))
        flash(res.get("message") or "Kurset kunne ikke markeres som gennemført.", "danger")
        return redirect(url_for("futurematch.my_order", order_id=order_id))

    @bp.route("/min-ordre/<order_id>/kompetencer", methods=["POST"])
    def my_order_skills(order_id):
        if not session.get("user"):
            return jsonify({"success": False, "message": "Log ind først."}), 401
        row, actors, ctx = _load(order_id)
        if not row or "owner" not in actors:
            return jsonify({"success": False, "message": "Ordren blev ikke fundet."}), 404
        if lc.normalize_status(row.get("status")) != lc.COMPLETED:
            return jsonify({"success": False, "message": "Kurset er ikke gennemført endnu."}), 400
        data = request.get_json(silent=True) or {}
        choices = data.get("skills") or []
        if not isinstance(choices, list) or not choices:
            return jsonify({"success": False, "message": "Vælg mindst én kompetence."}), 400
        import completion_service
        out = completion_service.apply_skill_choices(
            session.get("user"), choices[:10], company_id=ctx.company_id,
            user_id=ctx.user_id, order_id=row.get("id"))
        return jsonify({"success": out["saved"] > 0, **out,
                        "message": "%d kompetence(r) tilføjet til din profil." % out["saved"]})

    @bp.route("/min-ordre/<order_id>/kalender.ics")
    def my_order_ics(order_id):
        if not session.get("user"):
            return redirect(url_for("auth.login"))
        row, actors, _ = _load(order_id)
        if not row or "owner" not in actors or not row.get("variant_date"):
            abort(404)
        try:
            from calendar_service import build_ics
            ics = build_ics(
                title="Kursus: %s" % (row.get("product_title") or "Uddannelse"),
                start=row.get("variant_date"),
                location=row.get("variant_location") or "",
                description="Bestilt kursus (ordre %s)." % str(order_id)[:8],
                url=request.url_root.rstrip("/") + url_for("futurematch.my_order", order_id=order_id),
            )
        except Exception as e:
            logger.warning("learner_orders: ics failed: %s", e)
            abort(404)
        if not ics:
            abort(404)
        resp = Response(ics, mimetype="text/calendar")
        resp.headers["Content-Disposition"] = 'attachment; filename="kursus-%s.ics"' % str(order_id)[:8]
        return resp
