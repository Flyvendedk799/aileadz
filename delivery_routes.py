"""Tenant-scoped mail status and explicit recovery; no credentials or message bodies exposed."""

from flask import Blueprint, abort, current_app, flash, redirect, render_template, request, url_for
from auth_decorators import login_required
from order_service import OrderContext
from capabilities import can

mail_bp = Blueprint("mail_delivery", __name__)


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
        return redirect(url_for("mail_delivery.deliveries"))
    import MySQLdb.cursors

    cur = current_app.mysql.connection.cursor(MySQLdb.cursors.DictCursor)
    try:
        if ctx.is_platform_admin:
            cur.execute(
                "SELECT id,company_id,to_email,subject,state,attempts,last_error,created_at,sent_at FROM mail_outbox ORDER BY created_at DESC LIMIT 200"
            )
        else:
            cur.execute(
                "SELECT id,company_id,to_email,subject,state,attempts,last_error,created_at,sent_at FROM mail_outbox WHERE company_id=%s ORDER BY created_at DESC LIMIT 200",
                (ctx.company_id,),
            )
        return render_template("fm/mail_deliveries.html", deliveries=list(cur.fetchall() or []))
    finally:
        cur.close()
