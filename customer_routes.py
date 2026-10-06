"""Sales-led onboarding, support/seat requests and the account team's work queue."""

import datetime
import os
import re
import uuid
from flask import Blueprint, abort, current_app, flash, redirect, render_template, request, url_for
from auth_decorators import login_required, require_role
from capabilities import can
from order_service import OrderContext
import customer_success

customer_bp = Blueprint("customer_success", __name__)


def _cur():
    import MySQLdb.cursors

    return current_app.mysql.connection.cursor(MySQLdb.cursors.DictCursor)


def _company(cur, company_id):
    cur.execute("SELECT * FROM companies WHERE id=%s", (company_id,))
    row = cur.fetchone()
    if not row:
        abort(404)
    return row


@customer_bp.route("/for-virksomheder", methods=["GET", "POST"])
def sales():
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        email = request.form.get("email", "").strip()
        company = request.form.get("company_name", "").strip()
        if (
            request.form.get("website")
            or not name
            or not company
            or not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", email)
            or request.form.get("contact_consent") != "yes"
        ):
            flash("Udfyld navn, virksomhed og en gyldig e-mail, og bekræft at vi må kontakte dig.", "warning")
        else:
            cur = _cur()
            try:
                enquiry_id = str(uuid.uuid4())
                cur.execute(
                    "INSERT INTO sales_enquiries (id,name,email,company_name,message) VALUES (%s,%s,%s,%s,%s)",
                    (enquiry_id, name[:255], email[:255], company[:255], request.form.get("message", "")[:4000]),
                )
                customer_success.notify_account_team(
                    cur,
                    title="Ny demoforespørgsel",
                    message="%s fra %s har bedt om en demo. Åbn forespørgslen og aftal næste skridt." % (name[:255], company[:255]),
                    key="demo:" + enquiry_id,
                )
                current_app.mysql.connection.commit()
                flash("Din demoforespørgsel er gemt. Vi vender tilbage for at aftale et tidspunkt.", "success")
                return redirect(url_for("customer_success.sales", sent=1))
            finally:
                cur.close()
    return render_template("fm/sales.html", sent=request.args.get("sent") == "1")


@customer_bp.route("/virksomhed/kundeforloeb", methods=["GET", "POST"])
@login_required
def account():
    ctx = OrderContext.from_session(source="customer_account")
    if not ctx.company_id or not can("company.employees"):
        abort(403)
    cur = _cur()
    try:
        company = _company(cur, ctx.company_id)
        if request.method == "POST":
            kind = request.form.get("kind")
            if kind not in ("seats", "credits", "support", "renewal"):
                abort(400)
            note = request.form.get("note", "").strip()
            if not note:
                abort(400)
            quantity = request.form.get("quantity")
            try:
                quantity = int(quantity) if quantity else None
            except ValueError:
                abort(400)
            if quantity is not None and not 1 <= quantity <= 1000000:
                abort(400)
            cur.execute(
                "INSERT INTO customer_requests (company_id,user_id,kind,note,quantity) VALUES (%s,%s,%s,%s,%s)",
                (ctx.company_id, ctx.user_id, kind, note[:4000], quantity),
            )
            customer_success.notify_account_team(
                cur,
                title="Ny kundehenvendelse",
                message="%s har sendt en forespørgsel om %s. Åbn kundeforløbet for at svare." % (company["company_name"], kind),
                key="customer-request:%s" % cur.lastrowid,
                company_id=ctx.company_id,
            )
            current_app.mysql.connection.commit()
            flash("Din henvendelse er gemt hos den kundeansvarlige. Du kan følge svaret her.", "success")
            return redirect(url_for("customer_success.account"))
        cur.execute("SELECT * FROM customer_accounts WHERE company_id=%s", (ctx.company_id,))
        account = cur.fetchone() or {}
        cur.execute("SELECT * FROM customer_requests WHERE company_id=%s ORDER BY created_at DESC LIMIT 100", (ctx.company_id,))
        requests = list(cur.fetchall() or [])
        return render_template(
            "fm/customer_account.html",
            company=company,
            account=account,
            requests=requests,
            contact_email=account.get("account_email") or os.getenv("SUPPORT_EMAIL", "support@futurematch.dk"),
        )
    finally:
        cur.close()


@customer_bp.route("/hr/kom-i-gang", methods=["GET", "POST"])
@login_required
def readiness():
    ctx = OrderContext.from_session(source="customer_readiness")
    if not ctx.company_id or not can("company.employees"):
        abort(403)
    cur = _cur()
    try:
        company = _company(cur, ctx.company_id)
        if request.method == "POST":
            key = request.form.get("check_key")
            note = request.form.get("note", "").strip()
            if key not in customer_success.MANUAL_CHECKS or not note:
                abort(400)
            if request.form.get("action") == "reset":
                cur.execute("DELETE FROM company_launch_checks WHERE company_id=%s AND check_key=%s", (ctx.company_id, key))
            else:
                cur.execute(
                    "INSERT INTO company_launch_checks (company_id,check_key,note,confirmed_by) VALUES (%s,%s,%s,%s) ON DUPLICATE KEY UPDATE note=%s,confirmed_by=%s,confirmed_at=CURRENT_TIMESTAMP",
                    (ctx.company_id, key, note[:2000], ctx.user_id, note[:2000], ctx.user_id),
                )
            from tool_confirm import audit_chat_mutation

            audit_chat_mutation(
                cur,
                company_id=ctx.company_id,
                user_id=ctx.user_id,
                action="launch_review_" + request.form.get("action", "confirm"),
                resource_id=key,
                description=note[:2000],
                resource_type="launch_check",
            )
            current_app.mysql.connection.commit()
            return redirect(url_for("customer_success.readiness"))
        return render_template(
            "fm/customer_readiness.html",
            company=company,
            readiness=customer_success.readiness(cur, ctx.company_id),
            manual_checks=customer_success.MANUAL_CHECKS,
        )
    finally:
        cur.close()


@customer_bp.route("/admin/kundeforloeb")
@require_role("admin")
def accounts():
    cur = _cur()
    try:
        cur.execute(
            "SELECT c.id,c.company_name,c.subscription_plan,a.account_owner,a.stage,a.pilot_end,a.renewal_date,a.next_review,(SELECT COUNT(*) FROM customer_requests r WHERE r.company_id=c.id AND r.status<>%s) AS open_requests FROM companies c LEFT JOIN customer_accounts a ON a.company_id=c.id ORDER BY c.company_name",
            ("resolved",),
        )
        accounts = list(cur.fetchall() or [])
        cur.execute("SELECT * FROM sales_enquiries ORDER BY created_at DESC LIMIT 100")
        leads = list(cur.fetchall() or [])
        return render_template("fm/customer_accounts.html", accounts=accounts, leads=leads)
    finally:
        cur.close()


@customer_bp.route("/admin/kundeforloeb/<int:company_id>", methods=["GET", "POST"])
@require_role("admin")
def manage_account(company_id):
    cur = _cur()
    try:
        company = _company(cur, company_id)
        if request.method == "POST":
            if request.form.get("action") == "resolve":
                state = request.form.get("status")
                if state not in ("open", "in_progress", "resolved"):
                    abort(400)
                note = request.form.get("resolution", "").strip()
                if state == "resolved" and not note:
                    abort(400)
                cur.execute(
                    "UPDATE customer_requests SET status=%s,resolution=%s,resolved_at=CASE WHEN %s='resolved' THEN CURRENT_TIMESTAMP ELSE NULL END WHERE id=%s AND company_id=%s",
                    (state, note[:4000], state, request.form.get("request_id"), company_id),
                )
                customer_success.notify_request_response(cur, company_id, request.form.get("request_id"), state, note[:4000])
            else:
                fields = (
                    "account_owner",
                    "account_email",
                    "offer_name",
                    "included_services",
                    "success_criteria",
                    "pilot_end",
                    "renewal_date",
                    "next_review",
                    "stage",
                    "notes",
                )
                data = {k: request.form.get(k, "").strip() for k in fields}
                if data["stage"] not in ("onboarding", "pilot", "active", "renewal", "paused"):
                    abort(400)
                if data["account_email"] and not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", data["account_email"]):
                    abort(400)
                for field in ("pilot_end", "renewal_date", "next_review"):
                    if data[field]:
                        try:
                            datetime.date.fromisoformat(data[field])
                        except ValueError:
                            abort(400)
                    else:
                        data[field] = None
                for key in ("account_owner", "account_email", "offer_name"):
                    data[key] = data[key][:255]
                for key in ("included_services", "success_criteria", "notes"):
                    data[key] = data[key][:4000]
                cur.execute("INSERT IGNORE INTO customer_accounts (company_id) VALUES (%s)", (company_id,))
                cur.execute(
                    "UPDATE customer_accounts SET "
                    + ",".join(k + "=%s" for k in fields)
                    + ",updated_at=CURRENT_TIMESTAMP WHERE company_id=%s",
                    tuple(data[k] for k in fields) + (company_id,),
                )
            current_app.mysql.connection.commit()
            flash("Kundeforløbet er opdateret.", "success")
            return redirect(url_for("customer_success.manage_account", company_id=company_id))
        cur.execute("SELECT * FROM customer_accounts WHERE company_id=%s", (company_id,))
        account = cur.fetchone() or {}
        cur.execute("SELECT * FROM customer_requests WHERE company_id=%s ORDER BY created_at DESC", (company_id,))
        requests = list(cur.fetchall() or [])
        return render_template(
            "fm/customer_manage.html",
            company=company,
            account=account,
            requests=requests,
            readiness=customer_success.readiness(cur, company_id),
        )
    finally:
        cur.close()


@customer_bp.route("/admin/demoforespoergsler/<enquiry_id>", methods=["POST"])
@require_role("admin")
def update_enquiry(enquiry_id):
    status = request.form.get("status")
    if status not in ("new", "contacted", "demo_booked", "converted", "closed"):
        abort(400)
    cur = _cur()
    try:
        cid = request.form.get("company_id") or None
        if cid:
            try:
                cid = int(cid)
            except ValueError:
                abort(400)
            _company(cur, cid)
        if status == "converted" and not cid:
            abort(400)
        cur.execute(
            "UPDATE sales_enquiries SET status=%s,owner_note=%s,company_id=%s,updated_at=CURRENT_TIMESTAMP WHERE id=%s",
            (status, request.form.get("note", "")[:4000], cid, enquiry_id),
        )
        current_app.mysql.connection.commit()
        return redirect(url_for("customer_success.accounts"))
    finally:
        cur.close()
