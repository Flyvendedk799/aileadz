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
    form = {"name": "", "email": "", "company_name": "", "message": ""}
    errors = {}
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        email = request.form.get("email", "").strip()
        company = request.form.get("company_name", "").strip()
        form = {"name": name, "email": email, "company_name": company, "message": request.form.get("message", "")}
        if request.form.get("website"):
            # Honeypot: a bot gets the same confirmation as a person, and nothing is stored.
            return redirect(url_for("customer_success.sales", sent=1))
        if not name:
            errors["name"] = "Skriv dit navn."
        if not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", email):
            errors["email"] = "Skriv en gyldig e-mailadresse, fx navn@virksomhed.dk."
        if not company:
            errors["company_name"] = "Skriv virksomhedens navn."
        if request.form.get("contact_consent") != "yes":
            errors["contact_consent"] = "Bekræft, at vi må kontakte dig om forespørgslen."
        if not errors:
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
                return redirect(url_for("customer_success.sales", sent=1))
            finally:
                cur.close()
    return (
        render_template("fm/sales.html", sent=request.args.get("sent") == "1", form=form, errors=errors),
        400 if errors else 200,
    )


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


LEAD_STATUSES = [
    ("new", "Ny"),
    ("contacted", "Kontaktet"),
    ("demo_booked", "Demo aftalt"),
    ("converted", "Kunde oprettet"),
    ("closed", "Afsluttet"),
]
LEADS_PER_PAGE = 25


def _admin_users(cur):
    """Platform admins: the only people a lead or an account can be owned by."""
    cur.execute("SELECT id,username FROM users WHERE role=%s ORDER BY username", ("admin",))
    return list(cur.fetchall() or [])


def _age_label(created_at):
    """Days since a DATETIME (or its ISO text) as Danish text: i dag, 1 dag, N dage."""
    try:
        if isinstance(created_at, str):
            created_at = datetime.datetime.fromisoformat(created_at[:19])
        days = (datetime.datetime.now() - created_at).days
    except Exception:
        return "—"
    if days <= 0:
        return "i dag"
    return "1 dag" if days == 1 else "%d dage" % days


@customer_bp.route("/admin/kundeforloeb")
@require_role("admin")
def accounts():
    status = request.args.get("status", "")
    if status not in dict(LEAD_STATUSES):
        status = ""
    try:
        page = max(1, int(request.args.get("page", "1")))
    except ValueError:
        page = 1
    cur = _cur()
    try:
        cur.execute(
            "SELECT c.id,c.company_name,c.subscription_plan,a.account_owner,a.stage,a.pilot_end,a.renewal_date,a.next_review,(SELECT COUNT(*) FROM customer_requests r WHERE r.company_id=c.id AND r.status<>%s) AS open_requests FROM companies c LEFT JOIN customer_accounts a ON a.company_id=c.id ORDER BY c.company_name",
            ("resolved",),
        )
        accounts = list(cur.fetchall() or [])
        cur.execute("SELECT status,COUNT(*) AS n FROM sales_enquiries GROUP BY status")
        lead_counts = {r["status"]: int(r["n"]) for r in (cur.fetchall() or [])}
        lead_total = sum(lead_counts.values())
        shown = lead_counts.get(status, 0) if status else lead_total
        pages = max(1, -(-shown // LEADS_PER_PAGE))
        page = min(page, pages)
        where, params = ("WHERE e.status=%s", [status]) if status else ("", [])
        cur.execute(
            "SELECT e.*,u.username AS owner_name FROM sales_enquiries e LEFT JOIN users u ON u.id=e.owner_user_id "
            + where
            + " ORDER BY e.created_at DESC LIMIT %s OFFSET %s",
            tuple(params) + (LEADS_PER_PAGE, (page - 1) * LEADS_PER_PAGE),
        )
        leads = list(cur.fetchall() or [])
        for lead in leads:
            lead["age"] = _age_label(lead.get("created_at"))
        return render_template(
            "fm/customer_accounts.html",
            accounts=accounts,
            leads=leads,
            lead_statuses=LEAD_STATUSES,
            lead_counts=lead_counts,
            lead_total=lead_total,
            lead_status=status,
            lead_page=page,
            lead_pages=pages,
            admin_users=_admin_users(cur),
        )
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
                    "owner_user_id",
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
                owner_name = _owner_username(cur, data["owner_user_id"])
                if data["owner_user_id"]:
                    data["account_owner"] = owner_name
                elif "account_owner" not in request.form:
                    # No picker value and no legacy text posted: keep the old free-text owner.
                    cur.execute("SELECT account_owner FROM customer_accounts WHERE company_id=%s", (company_id,))
                    data["account_owner"] = ((cur.fetchone() or {}).get("account_owner")) or ""
                data["owner_user_id"] = int(data["owner_user_id"]) if data["owner_user_id"] else None
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
            admin_users=_admin_users(cur),
        )
    finally:
        cur.close()


def _owner_username(cur, owner_user_id):
    """Username of a platform admin picked as owner, empty for none; 400 for anyone else."""
    if not owner_user_id:
        return ""
    try:
        uid = int(owner_user_id)
    except (TypeError, ValueError):
        abort(400)
    cur.execute("SELECT username FROM users WHERE id=%s AND role=%s", (uid, "admin"))
    row = cur.fetchone()
    if not row:
        abort(400)
    return row["username"]


@customer_bp.route("/admin/demoforespoergsler/<enquiry_id>", methods=["POST"])
@require_role("admin")
def update_enquiry(enquiry_id):
    status = request.form.get("status")
    if status not in dict(LEAD_STATUSES):
        abort(400)
    cur = _cur()
    try:
        cur.execute("SELECT id FROM sales_enquiries WHERE id=%s", (enquiry_id,))
        if not cur.fetchone():
            abort(404)
        owner_user_id = request.form.get("owner_user_id", "").strip()
        _owner_username(cur, owner_user_id)
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
            "UPDATE sales_enquiries SET status=%s,owner_note=%s,owner_user_id=%s,company_id=%s,updated_at=CURRENT_TIMESTAMP WHERE id=%s",
            (status, request.form.get("note", "")[:4000], int(owner_user_id) if owner_user_id else None, cid, enquiry_id),
        )
        current_app.mysql.connection.commit()
        back = {}
        if request.form.get("return_status") in dict(LEAD_STATUSES):
            back["status"] = request.form["return_status"]
        if request.form.get("return_page", "").isdigit():
            back["page"] = int(request.form["return_page"])
        return redirect(url_for("customer_success.accounts", **back))
    finally:
        cur.close()
