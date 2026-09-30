"""Billing management read side (N-6.3). Payment happens OFF-platform: this module
only lists, filters, summarises and exports what HR/admin track per order
(invoice number, dates, payment reference). Writes go through
``order_service.set_billing_status`` (transition rules + audit trail).
"""

from __future__ import annotations

import csv
import io
import logging

logger = logging.getLogger(__name__)

BILLABLE_STATUSES = ("approved", "booked", "completed", "pending", "confirmed", "processing")
FILTERS = ("not_invoiced", "invoiced", "paid", "credited", "overdue", "unpaid")

CSV_HEADER = [
    "Ordre", "Kursus", "Medarbejder", "Afdeling", "Virksomhed", "Pris (kr)", "Ordrestatus",
    "Fakturering", "Fakturanr.", "Fakturadato", "Forfaldsdato", "Betalt den",
    "Betalingsreference", "Note",
]


def _where(company_id, billing_filter, department, date_from, date_to, solo_only=False):
    ph = ",".join(["%s"] * len(BILLABLE_STATUSES))
    clauses = ["co.status IN (%s)" % ph]
    params = list(BILLABLE_STATUSES)
    if solo_only:
        clauses.append("co.company_id IS NULL")
    elif company_id is not None:
        clauses.append("co.company_id = %s")
        params.append(company_id)
    if billing_filter == "overdue":
        clauses.append("co.billing_status = 'invoiced' AND co.invoice_due_date IS NOT NULL "
                       "AND co.invoice_due_date < CURDATE()")
    elif billing_filter == "unpaid":
        clauses.append("co.billing_status IN ('not_invoiced', 'invoiced')")
    elif billing_filter in ("not_invoiced", "invoiced", "paid", "credited"):
        clauses.append("COALESCE(co.billing_status, 'not_invoiced') = %s")
        params.append(billing_filter)
    if department:
        clauses.append("co.department = %s")
        params.append(department)
    if date_from:
        clauses.append("DATE(co.created_at) >= %s")
        params.append(date_from)
    if date_to:
        clauses.append("DATE(co.created_at) <= %s")
        params.append(date_to)
    return " AND ".join(clauses), params


def fetch_orders(cur, *, company_id=None, billing_filter="", department="", date_from="",
                 date_to="", solo_only=False, limit=500):
    where, params = _where(company_id, billing_filter, department, date_from, date_to, solo_only)
    cur.execute(
        """
        SELECT co.order_id, co.product_title, co.username, co.user_name, co.department, co.price,
               co.status, co.created_at, co.company_id,
               COALESCE(co.billing_status, 'not_invoiced') AS billing_status,
               co.invoice_number, co.invoice_date, co.invoice_due_date, co.payment_date,
               co.payment_method, co.payment_reference, co.billing_note,
               (co.billing_status = 'invoiced' AND co.invoice_due_date IS NOT NULL
                AND co.invoice_due_date < CURDATE()) AS is_overdue
        FROM course_orders co
        WHERE """ + where + """
        ORDER BY co.created_at DESC LIMIT %s""",
        tuple(params) + (int(limit),),
    )
    return list(cur.fetchall() or [])


def summary(cur, *, company_id=None, department="", date_from="", date_to="", solo_only=False):
    """Counts and values per billing status (+ overdue). Excludes cancelled/rejected."""
    where, params = _where(company_id, "", department, date_from, date_to, solo_only)
    cur.execute(
        """
        SELECT COUNT(*) AS total_orders, COALESCE(SUM(co.price), 0) AS total_value,
          SUM(CASE WHEN COALESCE(co.billing_status,'not_invoiced') = 'not_invoiced' THEN 1 ELSE 0 END) AS not_invoiced,
          COALESCE(SUM(CASE WHEN COALESCE(co.billing_status,'not_invoiced') = 'not_invoiced' THEN co.price END), 0) AS not_invoiced_value,
          SUM(CASE WHEN co.billing_status = 'invoiced' THEN 1 ELSE 0 END) AS invoiced,
          COALESCE(SUM(CASE WHEN co.billing_status = 'invoiced' THEN co.price END), 0) AS invoiced_value,
          SUM(CASE WHEN co.billing_status = 'paid' THEN 1 ELSE 0 END) AS paid,
          COALESCE(SUM(CASE WHEN co.billing_status = 'paid' THEN co.price END), 0) AS paid_value,
          SUM(CASE WHEN co.billing_status = 'credited' THEN 1 ELSE 0 END) AS credited,
          SUM(CASE WHEN co.billing_status = 'invoiced' AND co.invoice_due_date IS NOT NULL
                   AND co.invoice_due_date < CURDATE() THEN 1 ELSE 0 END) AS overdue,
          COALESCE(SUM(CASE WHEN co.billing_status = 'invoiced' AND co.invoice_due_date IS NOT NULL
                   AND co.invoice_due_date < CURDATE() THEN co.price END), 0) AS overdue_value
        FROM course_orders co WHERE """ + where,
        tuple(params),
    )
    row = cur.fetchone() or {}
    return {k: (row.get(k) or 0) for k in (
        "total_orders", "total_value", "not_invoiced", "not_invoiced_value", "invoiced",
        "invoiced_value", "paid", "paid_value", "credited", "overdue", "overdue_value")}


def to_csv(orders, company_names=None):
    """Excel-friendly CSV (UTF-8 BOM, ';' separator) for external reconciliation."""
    import order_lifecycle as lc
    buf = io.StringIO()
    buf.write("﻿")
    w = csv.writer(buf, delimiter=";")
    w.writerow(CSV_HEADER)
    for o in orders:
        w.writerow([
            o.get("order_id"), o.get("product_title"), o.get("user_name") or o.get("username"),
            o.get("department") or "", (company_names or {}).get(o.get("company_id"), "") if company_names else "",
            ("%.2f" % float(o.get("price") or 0)).replace(".", ","),
            lc.status_label(o.get("status"), short=True), lc.billing_label(o.get("billing_status")),
            o.get("invoice_number") or "", o.get("invoice_date") or "", o.get("invoice_due_date") or "",
            o.get("payment_date") or "", o.get("payment_reference") or "", o.get("billing_note") or "",
        ])
    return buf.getvalue()


def notify_overdue(cur, *, company_id=None):
    """Raise one HR notification per overdue invoice (deduped per order). Returns count."""
    from notification_service import notify_roles, HR_ROLES
    cur.execute(
        """SELECT order_id, company_id, product_title, invoice_number, invoice_due_date, price
           FROM course_orders
           WHERE billing_status = 'invoiced' AND invoice_due_date IS NOT NULL
             AND invoice_due_date < CURDATE() AND company_id IS NOT NULL"""
        + (" AND company_id = %s" if company_id is not None else ""),
        ((company_id,) if company_id is not None else ()),
    )
    n = 0
    for r in list(cur.fetchall() or []):
        n += notify_roles(
            cur, r["company_id"], HR_ROLES,
            title="Faktura forfalden",
            message="Faktura %s for “%s” (%.0f kr.) var forfalden %s." % (
                r.get("invoice_number") or "uden nummer", r.get("product_title"),
                float(r.get("price") or 0), r.get("invoice_due_date")),
            kind="billing", is_urgent=True, action_url="/hr/billing?billing_status=overdue",
            dedupe_key="billing-overdue:%s" % r["order_id"], dedupe_hours=24 * 7,
        )
    return n
