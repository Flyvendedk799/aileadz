"""Tenant-scoped mail status and explicit recovery; no credentials or message bodies exposed."""

import datetime

from flask import Blueprint, abort, current_app, flash, redirect, render_template, request, url_for
from auth_decorators import login_required
from order_service import OrderContext
from capabilities import can

mail_bp = Blueprint("mail_delivery", __name__)

PER_PAGE = 50
# A pending mail that has been due for longer than this means nothing is sending.
STALE_AFTER = datetime.timedelta(minutes=10)
STATES = ("pending", "sending", "sent", "failed", "uncertain", "skipped")
PERIOD_DAYS = {"7": 7, "30": 30}


def _filters():
    """(state, period, q, page) from the query string; bad values fall back safely."""
    state = request.args.get("state", "")
    state = state if state in STATES else ""
    period = request.args.get("period", "")
    period = period if period in PERIOD_DAYS else ""
    q = (request.args.get("q") or "").strip()[:100]
    try:
        page = max(1, int(request.args.get("page", 1)))
    except (TypeError, ValueError):
        page = 1
    return state, period, q, page


def _kept_args():
    """The filters worth keeping across a retry (never the page: the list shifts)."""
    state, period, q, _page = _filters()
    return {k: v for k, v in (("state", state), ("period", period), ("q", q)) if v}


@mail_bp.route("/hr/leveringer", methods=["GET", "POST"])
@login_required
def deliveries():
    ctx = OrderContext.from_session(source="mail_console")
    if not can("company.employees") and not ctx.is_platform_admin:
        abort(403)
    if request.method == "POST":
        from mail_delivery import retry

        ok = retry(ctx, request.form.get("delivery_id"), confirm_uncertain=request.form.get("confirm_uncertain") == "yes")
        flash(
            "Mailen er sat i kø igen." if ok else "Mailen kunne ikke genafsendes. Kontroller status og bekræft eventuel ukendt modtagelse.",
            "success" if ok else "warning",
        )
        return redirect(url_for("mail_delivery.deliveries", **_kept_args()))
    import MySQLdb.cursors

    state, period, q, page = _filters()
    now = datetime.datetime.now()
    where, params = [], []
    if not ctx.is_platform_admin:
        where.append("company_id=%s")
        params.append(ctx.company_id)
    if state:
        where.append("state=%s")
        params.append(state)
    if period:
        where.append("created_at>=%s")
        params.append(now - datetime.timedelta(days=PERIOD_DAYS[period]))
    if q:
        like = "%" + q + "%"
        where.append("(to_email LIKE %s OR subject LIKE %s)")
        params.extend([like, like])
    clause = (" WHERE " + " AND ".join(where)) if where else ""
    scope_clause = " WHERE company_id=%s" if not ctx.is_platform_admin else ""
    scope_params = [ctx.company_id] if not ctx.is_platform_admin else []

    cur = current_app.mysql.connection.cursor(MySQLdb.cursors.DictCursor)
    try:
        cur.execute("SELECT COUNT(*) AS n FROM mail_outbox" + clause, tuple(params))
        total = int((cur.fetchone() or {}).get("n") or 0)
        pages = max(1, (total + PER_PAGE - 1) // PER_PAGE)
        page = min(page, pages)
        cur.execute(
            "SELECT id,company_id,to_email,subject,state,attempts,last_error,created_at,sent_at,order_id "
            "FROM mail_outbox" + clause + " ORDER BY created_at DESC, id LIMIT %s OFFSET %s",
            tuple(params) + (PER_PAGE, (page - 1) * PER_PAGE),
        )
        rows = list(cur.fetchall() or [])
        cur.execute(
            "SELECT MIN(available_at) AS oldest FROM mail_outbox" + (scope_clause + " AND" if scope_clause else " WHERE")
            + " state='pending' AND available_at<=%s",
            tuple(scope_params) + (now - STALE_AFTER,),
        )
        stale_since = (cur.fetchone() or {}).get("oldest")
    finally:
        cur.close()
    order_endpoint = "admin_dashboard.admin_order_detail" if ctx.is_platform_admin else "hr_dashboard.company_order_details"
    for row in rows:
        row["order_url"] = url_for(order_endpoint, order_id=row["order_id"]) if row.get("order_id") else None
    return render_template(
        "fm/mail_deliveries.html",
        deliveries=rows,
        stale_since=stale_since,
        is_platform_admin=ctx.is_platform_admin,
        filters={"state": state, "period": period, "q": q},
        has_filters=bool(state or period or q),
        pg={"page": page, "pages": pages, "total": total, "has_prev": page > 1, "has_next": page < pages},
    )
