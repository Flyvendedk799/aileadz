"""HR report exports (N-4.1) and the content of scheduled reports (N-4.2).

ONE registry of company-scoped reports. Each builder returns
``(headers, rows)`` with Danish column names. ``to_csv`` produces Excel-friendly
CSV: UTF-8 with BOM (so æøå survive) and ';' as separator.
"""

from __future__ import annotations

import csv
import datetime
import io
import logging

logger = logging.getLogger(__name__)

# key -> (Danish title, description, icon)
REPORTS = {
    "employee_progress": ("Medarbejderfremdrift", "Status, afdeling, kurser, fremdrift og chatbot-engagement pr. medarbejder.", "fa-users"),
    "course_completions": ("Kursusgennemførelse", "Alle kursusordrer med status, dato, pris, lokation og gennemførelse.", "fa-graduation-cap"),
    "department_summary": ("Afdelingsresumé", "Afdelingernes medarbejdere, tilmeldinger, gennemførelsesrate og investering.", "fa-sitemap"),
    "budget": ("Budget", "Årsbudget, forbrug og resterende budget pr. afdeling.", "fa-wallet"),
    "approvals": ("Godkendelser", "Alle godkendelsesanmodninger med beslutning og tidspunkter.", "fa-circle-check"),
    "compliance": ("Compliance", "Lovpligtige krav og hvor mange medarbejdere, der mangler eller har udløbet.", "fa-clipboard-check"),
    "roi": ("Uddannelsesforbrug", "Forbrug og gennemførelse pr. afdeling (forbrug, ikke beregnet afkast).", "fa-chart-pie"),
    "skill_gaps": ("Kompetencegab", "Medarbejdere med kompetencer under målniveau.", "fa-brain"),
}
# Names the scheduling tool uses
ALIASES = {"training_status": "employee_progress"}


def resolve(report_type):
    rt = ALIASES.get(report_type, report_type)
    return rt if rt in REPORTS else None


def _dept_clause(column, department, params):
    if department:
        params.append(department)
        return " AND " + column + " = %s"
    return ""


def _date_clause(column, date_from, date_to, params):
    out = ""
    if date_from:
        out += " AND DATE(" + column + ") >= %s"
        params.append(date_from)
    if date_to:
        out += " AND DATE(" + column + ") <= %s"
        params.append(date_to)
    return out


def _run(cur, sql, params):
    cur.execute(sql, tuple(params))
    return list(cur.fetchall() or [])


def build(cur, company_id, report_type, *, department=None, date_from=None, date_to=None):
    """Return ``(headers, rows)`` for a report, or ``([], [])`` when empty."""
    rt = resolve(report_type)
    if not rt:
        raise ValueError("Ukendt rapporttype")
    p = [company_id]

    if rt == "employee_progress":
        where = _dept_clause("cu.department", department, p)
        rows = _run(cur, """
            SELECT u.username AS `Medarbejder`, u.email AS `E-mail`, cu.department AS `Afdeling`,
                   cu.job_title AS `Stilling`, cu.hire_date AS `Ansat fra`,
                   COUNT(DISTINCT co.id) AS `Kurser tilmeldt`,
                   COUNT(DISTINCT CASE WHEN co.completion_status = 'completed' THEN co.id END) AS `Kurser gennemført`,
                   COALESCE(AVG(elp.progress_percentage), 0) AS `Gns. fremdrift %`,
                   cu.total_chatbot_queries AS `AI-samtaler`, cu.last_login AS `Seneste login`
            FROM company_users cu JOIN users u ON cu.user_id = u.id
            LEFT JOIN course_orders co ON cu.user_id = co.user_id AND cu.company_id = co.company_id
            LEFT JOIN employee_learning_progress elp ON cu.user_id = elp.user_id AND cu.company_id = elp.company_id
            WHERE cu.company_id = %s""" + where + """
            GROUP BY cu.user_id, u.username, u.email, cu.department, cu.job_title, cu.hire_date,
                     cu.total_chatbot_queries, cu.last_login
            ORDER BY u.username""", p)
    elif rt == "course_completions":
        where = _dept_clause("cu.department", department, p) + _date_clause("co.created_at", date_from, date_to, p)
        rows = _run(cur, """
            SELECT u.username AS `Medarbejder`, cu.department AS `Afdeling`, co.product_title AS `Kursus`,
                   co.created_at AS `Bestilt`, co.status AS `Ordrestatus`, co.completion_status AS `Gennemførelse`,
                   co.completion_date AS `Gennemført den`, co.price AS `Pris (kr)`,
                   co.variant_location AS `Sted`, co.variant_date AS `Kursusdato`
            FROM course_orders co JOIN users u ON co.user_id = u.id
            JOIN company_users cu ON co.user_id = cu.user_id AND co.company_id = cu.company_id
            WHERE co.company_id = %s""" + where + " ORDER BY co.created_at DESC", p)
        for r in rows:
            import order_lifecycle as lc
            r["Ordrestatus"] = lc.status_label(r.get("Ordrestatus"), short=True)
    elif rt == "department_summary":
        where = _dept_clause("cu.department", department, p)
        rows = _run(cur, """
            SELECT cu.department AS `Afdeling`, COUNT(DISTINCT cu.user_id) AS `Medarbejdere`,
                   COUNT(DISTINCT co.id) AS `Tilmeldinger`,
                   COUNT(DISTINCT CASE WHEN co.completion_status = 'completed' THEN co.id END) AS `Gennemførte kurser`,
                   ROUND(COUNT(DISTINCT CASE WHEN co.completion_status = 'completed' THEN co.id END) * 100.0
                         / NULLIF(COUNT(DISTINCT co.id), 0), 1) AS `Gennemførelsesrate %`,
                   COALESCE(SUM(CASE WHEN co.completion_status = 'completed' THEN co.price END), 0) AS `Investering (kr)`
            FROM company_users cu
            LEFT JOIN course_orders co ON cu.user_id = co.user_id AND cu.company_id = co.company_id
            WHERE cu.company_id = %s""" + where + " GROUP BY cu.department ORDER BY cu.department", p)
    elif rt == "budget":
        where = _dept_clause("department", department, p)
        rows = _run(cur, """
            SELECT department AS `Afdeling`, fiscal_year AS `År`, annual_budget AS `Årsbudget (kr)`,
                   spent AS `Forbrugt (kr)`, (annual_budget - spent) AS `Tilbage (kr)`
            FROM department_budgets WHERE company_id = %s""" + where + " ORDER BY fiscal_year DESC, department", p)
    elif rt == "approvals":
        where = _dept_clause("co.department", department, p) + _date_clause("oa.requested_at", date_from, date_to, p)
        rows = _run(cur, """
            SELECT co.order_id AS `Ordre`, co.product_title AS `Kursus`, co.price AS `Pris (kr)`,
                   co.department AS `Afdeling`, oa.status AS `Beslutning`, oa.notes AS `Begrundelse`,
                   oa.requested_at AS `Anmodet`, oa.decided_at AS `Afgjort`
            FROM order_approvals oa JOIN course_orders co ON oa.order_id = co.order_id
            WHERE oa.company_id = %s""" + where + " ORDER BY oa.requested_at DESC", p)
        names = {"pending": "Afventer", "approved": "Godkendt", "rejected": "Afvist", "cancelled": "Annulleret"}
        for r in rows:
            r["Beslutning"] = names.get(r.get("Beslutning"), r.get("Beslutning"))
    elif rt == "compliance":
        where = _dept_clause("applies_to_department", department, p)
        rows = _run(cur, """
            SELECT title AS `Krav`, category AS `Kategori`, applies_to_department AS `Afdeling`,
                   recurrence_months AS `Gentagelse (mdr.)`, is_statutory AS `Lovpligtigt`
            FROM compliance_requirements WHERE company_id = %s""" + where + " ORDER BY title", p)
        for r in rows:
            r["Lovpligtigt"] = "Ja" if r.get("Lovpligtigt") else "Nej"
    elif rt == "roi":
        where = _dept_clause("cu.department", department, p) + _date_clause("co.created_at", date_from, date_to, p)
        rows = _run(cur, """
            SELECT cu.department AS `Afdeling`,
                   COUNT(DISTINCT co.id) AS `Ordrer`,
                   COUNT(DISTINCT CASE WHEN co.completion_status = 'completed' THEN co.id END) AS `Gennemført`,
                   COALESCE(SUM(CASE WHEN co.status NOT IN ('cancelled', 'rejected') THEN co.price END), 0) AS `Forbrug (kr)`
            FROM company_users cu
            LEFT JOIN course_orders co ON co.user_id = cu.user_id AND co.company_id = cu.company_id
            WHERE cu.company_id = %s""" + where + " GROUP BY cu.department ORDER BY cu.department", p)
    else:  # skill_gaps
        where = _dept_clause("cu.department", department, p)
        rows = _run(cur, """
            SELECT u.username AS `Medarbejder`, cu.department AS `Afdeling`, esm.skill_name AS `Kompetence`,
                   COALESCE(esm.current_level, 0) AS `Nuværende niveau`, esm.target_level AS `Målniveau`
            FROM employee_skills_matrix esm
            JOIN company_users cu ON cu.user_id = esm.employee_id AND cu.company_id = esm.company_id
            JOIN users u ON u.id = esm.employee_id
            WHERE esm.company_id = %s AND esm.target_level > 0
              AND COALESCE(esm.current_level, 0) < esm.target_level""" + where + " ORDER BY cu.department, u.username", p)

    if not rows:
        return [], []
    headers = list(rows[0].keys())
    return headers, [[r.get(h) for h in headers] for r in rows]


def _fmt(v):
    if v is None:
        return ""
    if isinstance(v, float):
        return ("%.2f" % v).replace(".", ",")
    if isinstance(v, (datetime.datetime, datetime.date)):
        return v.strftime("%Y-%m-%d")
    return str(v)


def to_csv(headers, rows):
    buf = io.StringIO()
    buf.write("﻿")
    w = csv.writer(buf, delimiter=";")
    w.writerow(headers)
    for r in rows:
        w.writerow([_fmt(v) for v in r])
    return buf.getvalue()
