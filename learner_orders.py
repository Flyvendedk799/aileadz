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
import order_timing

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
    return lc.normalize_status(status) == lc.BOOKED


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
        from order_fulfillment import details
        conn = current_app.mysql.connection
        import MySQLdb.cursors
        detail_cur = conn.cursor(MySQLdb.cursors.DictCursor)
        try:
            fulfillment = details(detail_cur,order_id)
            detail_cur.execute("SELECT * FROM course_order_changes WHERE order_id=%s ORDER BY created_at DESC",(order_id,))
            changes = list(detail_cur.fetchall() or [])
        finally:
            detail_cur.close()
        pending_change = next((c for c in changes if (c.get("status") or "") == "pending"), None)
        return render_template(
            "fm/my_order.html", fulfillment=fulfillment, changes=changes,
            order=row,
            status=status,
            status_label=lc.status_label(status),
            status_hint=lc.STATUS_HINTS[status],
            status_tone=lc.STATUS_TONES[status],
            history=history,
            vendor_name=vendor_name,
            can_cancel=_can_cancel(status) and not pending_change,
            pending_change=pending_change,
            can_complete=_can_complete(status),
            has_date=bool(row.get("variant_date")),
            moment=moment,
            just_completed=request.args.get("completed") == "1" and status == lc.COMPLETED,
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
            # A request for a booked order is news, not a completed cancellation.
            flash(res.get("message") or "Din bestilling er annulleret.", "info" if res.get("requested") else "success")
        else:
            flash(res.get("message") or "Bestillingen kunne ikke annulleres.", "danger")
        return redirect(url_for("futurematch.my_order", order_id=order_id))

    @bp.route("/min-ordre/<order_id>/gennemfoert", methods=["POST"])
    def my_order_complete(order_id):
        if not session.get("user"):
            return jsonify({"success": False, "message": "Log ind først."}), 401
        from order_fulfillment import report_completion
        res = report_completion(_ctx(), order_id, note=request.form.get('evidence_note',''), evidence_url=request.form.get('evidence_url',''))
        if _wants_json():
            code = 200 if res.get("success") else (404 if res.get("error") == "not_found" else 400)
            return jsonify(res), code
        if res.get("success"):
            flash(res.get("message") or "Deltagelsen er registreret.", "success")
            return redirect(url_for("futurematch.my_order", order_id=order_id))
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

    @bp.route("/min-kalender.ics")
    def my_calendar():
        """Download the learner's own upcoming courses and deadlines (.ics).
        Strictly the session user's orders - never a colleague's."""
        if not session.get("user"):
            return redirect(url_for("auth.login"))
        events = []
        try:
            import MySQLdb.cursors
            from calendar_service import build_ics_feed
            cur = current_app.mysql.connection.cursor(MySQLdb.cursors.DictCursor)
            cur.execute(
                """SELECT co.order_id, co.product_title, co.variant_date, co.variant_location,
                          co.completion_deadline, co.status, d.booking_json
                   FROM course_orders co
                   LEFT JOIN course_order_details d ON d.order_id = co.order_id
                   WHERE (co.username = %s OR (co.user_id IS NOT NULL AND co.user_id = %s))
                     AND co.status IN ('approved', 'booked')
                   ORDER BY co.created_at DESC LIMIT 100""",
                (session.get("user"), session.get("user_id")))
            for r in cur.fetchall() or []:
                link = request.url_root.rstrip("/") + url_for("futurematch.my_order", order_id=r["order_id"])
                # The booked start (with time) wins over the human session label.
                booked_start = order_timing.booking_start_text(r)
                if booked_start or r.get("variant_date"):
                    events.append({"title": "Kursus: %s" % r["product_title"], "start": booked_start or r["variant_date"],
                                   "location": r.get("variant_location") or "", "url": link,
                                   "uid": "kursus-%s@futurematch" % r["order_id"]})
                elif r.get("completion_deadline"):
                    events.append({"title": "Frist: %s" % r["product_title"], "start": r["completion_deadline"],
                                   "url": link, "uid": "frist-%s@futurematch" % r["order_id"]})
            cur.close()
            ics = build_ics_feed(events, cal_name="Min læring")
        except Exception as e:
            logger.warning("learner_orders: calendar failed: %s", e)
            abort(404)
        resp = Response(ics, mimetype="text/calendar")
        resp.headers["Content-Disposition"] = 'attachment; filename="min-laering.ics"'
        return resp

    @bp.route("/min-ordre/<order_id>/kalender.ics")
    def my_order_ics(order_id):
        if not session.get("user"):
            return redirect(url_for("auth.login"))
        row, actors, _ = _load(order_id)
        if not row or "owner" not in actors or not row.get("variant_date"):
            abort(404)
        try:
            from calendar_service import build_ics
            import order_fulfillment
            import MySQLdb.cursors
            cur = current_app.mysql.connection.cursor(MySQLdb.cursors.DictCursor)
            try:
                booking = order_fulfillment.details(cur,order_id).get('booking_json') or {}
            finally:
                cur.close()
            ics = build_ics(
                title="Kursus: %s" % (row.get("product_title") or "Uddannelse"),
                start=booking.get('start_at') or row.get("variant_date"),
                end=booking.get('end_at') or None,
                location=booking.get('location') or booking.get('join_url') or row.get("variant_location") or "",
                description="\n".join(filter(None,["Bestilt kursus (ordre %s)." % str(order_id)[:8],booking.get('reference'),booking.get('instructions'),booking.get('join_url')])) ,
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
