"""Credit screens (N-6.4): HR sees company usage, admin sees all companies and tops up.

* ``GET  /hr/credits``                     company balance + usage per user and assistant
* ``POST /hr/credits/settings``            limit mode (soft/hard) + low-balance threshold
* ``GET  /admin/credits/companies``        every company: balance, burn rate, runway
* ``POST /admin/credits/grant``            top up a company through the ledger
* ``GET  /api/credits/usage``              the caller's own (or company) usage, JSON

Payment for credits happens off-platform; "top-ups" are admin grants.
"""

from __future__ import annotations

import logging

from flask import Blueprint, current_app, flash, jsonify, redirect, render_template, request, session, url_for

import credit_service
from auth_decorators import login_required, require_role

logger = logging.getLogger(__name__)

credit_bp = Blueprint("credits", __name__)


def _cur():
    import MySQLdb.cursors
    return current_app.mysql.connection.cursor(MySQLdb.cursors.DictCursor)


def _hr_company_id():
    """Company for an HR-level caller (hr_manager/company_admin) or an admin acting as one."""
    import capabilities
    if not capabilities.can("company.credits"):
        return None
    return session.get("company_id")


@credit_bp.route("/hr/credits")
@login_required
def company_credits():
    cid = _hr_company_id()
    if not cid:
        flash("Kun HR-ledere kan se virksomhedens kreditter.", "warning")
        return redirect(url_for("futurematch.employee_home"))
    days = request.args.get("days", 30, type=int)
    cur = _cur()
    try:
        summary = credit_service.usage_summary(cur, company_id=cid, days=days)
    except Exception as e:
        logger.warning("company credits failed: %s", e)
        summary = None
    finally:
        cur.close()
    return render_template("fm/credits_company.html", summary=summary, days=days)


@credit_bp.route("/hr/credits/settings", methods=["POST"])
@login_required
def company_credit_settings():
    cid = _hr_company_id()
    if not cid:
        return jsonify({"success": False, "message": "Ingen adgang."}), 403
    mode = request.form.get("limit_mode")
    thr = request.form.get("low_threshold")
    if mode not in (credit_service.LIMIT_SOFT, credit_service.LIMIT_HARD):
        flash("Vælg en gyldig grænsetype.", "danger")
        return redirect(url_for("credits.company_credits"))
    try:
        credit_service.update_settings(current_app.mysql.connection, cid, limit_mode=mode,
                                       low_threshold=int(thr) if thr not in (None, "") else None)
        flash("Kreditindstillinger gemt.", "success")
    except Exception as e:
        logger.warning("credit settings failed: %s", e)
        flash("Indstillingerne kunne ikke gemmes.", "danger")
    return redirect(url_for("credits.company_credits"))


@credit_bp.route("/admin/credits/companies")
@require_role("admin")
def admin_company_credits():
    cur = _cur()
    try:
        overview = credit_service.admin_overview(cur, days=request.args.get("days", 30, type=int))
    except Exception as e:
        logger.warning("admin credits overview failed: %s", e)
        overview = {"companies": [], "recent_grants": [], "days": 30}
    finally:
        cur.close()
    return render_template("fm/admin_credits_companies.html", overview=overview)


@credit_bp.route("/admin/credits/grant", methods=["POST"])
@require_role("admin")
def admin_grant_company_credits():
    try:
        company_id = int(request.form.get("company_id") or 0)
        amount = int(request.form.get("amount") or 0)
    except ValueError:
        flash("Angiv gyldige tal.", "danger")
        return redirect(url_for("credits.admin_company_credits"))
    if not company_id or amount == 0:
        flash("Vælg en virksomhed og et antal kreditter.", "danger")
        return redirect(url_for("credits.admin_company_credits"))
    res = credit_service.grant(current_app.mysql.connection, amount=amount,
                               reason=request.form.get("reason") or "Optankning",
                               actor=session.get("user") or "admin", company_id=company_id)
    flash("Kreditter tilføjet." if res.get("success") else (res.get("message") or "Fejl"),
          "success" if res.get("success") else "danger")
    return redirect(url_for("credits.admin_company_credits"))


@credit_bp.route("/api/credits/usage")
@login_required
def my_credit_usage():
    cur = _cur()
    try:
        company_id = session.get("company_id")
        if company_id:
            # A company member sees their own usage plus the company balance; the
            # per-user breakdown of colleagues is HR-only.
            summary = credit_service.usage_summary(cur, username=session.get("user"), days=30)
            acct = credit_service.get_account(cur, company_id)
            summary["company_balance"] = acct["balance"]
            if _hr_company_id():
                summary["company"] = credit_service.usage_summary(cur, company_id=company_id, days=30)
        else:
            summary = credit_service.usage_summary(cur, username=session.get("user"), days=30)
            summary["balance"] = credit_service.get_personal_balance(cur, session.get("user"))
        return jsonify({"success": True, "usage": summary})
    except Exception as e:
        logger.warning("credit usage failed: %s", e)
        return jsonify({"success": False, "usage": None}), 500
    finally:
        cur.close()
