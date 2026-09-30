"""Scheduled reports (N-4.2): the worker job that actually sends what
``company_report_schedules`` promises. Reports come from ``report_exports`` and are
e-mailed as CSV (UTF-8 BOM) to the company's HR managers. Pausing a schedule
(``enabled = 0``) or deleting it stops the mail.
"""

from __future__ import annotations

import datetime
import logging

logger = logging.getLogger(__name__)

CADENCE_DAYS = {"daily": 1, "weekly": 7, "monthly": 28}


def is_due(cadence, last_sent_at, now=None):
    """Due when never sent, or the cadence window (minus a 1 h tolerance) has passed."""
    now = now or datetime.datetime.now()
    days = CADENCE_DAYS.get(cadence)
    if days is None:
        return False
    if not last_sent_at:
        return True
    if isinstance(last_sent_at, str):
        try:
            last_sent_at = datetime.datetime.fromisoformat(last_sent_at)
        except ValueError:
            return True
    return (now - last_sent_at) >= datetime.timedelta(days=days) - datetime.timedelta(hours=1)


def run_due_schedules(cur, *, send=None, now=None):
    """Send every due, enabled schedule. Returns a summary dict. Never raises."""
    import report_exports
    from notification_service import role_recipients
    summary = {"checked": 0, "sent": 0, "empty": 0, "skipped": 0, "errors": 0}
    try:
        cur.execute("SELECT id, company_id, report_type, cadence, department, last_sent_at "
                    "FROM company_report_schedules WHERE enabled = 1")
        schedules = list(cur.fetchall() or [])
    except Exception as e:
        logger.warning("scheduled_reports: list failed: %s", e)
        return summary
    for s in schedules:
        summary["checked"] += 1
        if not is_due(s["cadence"], s.get("last_sent_at"), now):
            summary["skipped"] += 1
            continue
        try:
            headers, rows = report_exports.build(cur, s["company_id"], s["report_type"],
                                                 department=s.get("department"))
            status = "sent"
            if not rows:
                status = "empty"
                summary["empty"] += 1
            else:
                recipients = _hr_emails(cur, s["company_id"])
                if not recipients:
                    status = "no_recipients"
                    summary["skipped"] += 1
                else:
                    body = report_exports.to_csv(headers, rows)
                    title = report_exports.REPORTS[report_exports.resolve(s["report_type"])][0]
                    for to in recipients:
                        (send or _send)(to, title, s, body, len(rows))
                    summary["sent"] += 1
            cur.execute("UPDATE company_report_schedules SET last_sent_at = CURRENT_TIMESTAMP, "
                        "last_status = %s WHERE id = %s", (status, s["id"]))
        except Exception as e:
            summary["errors"] += 1
            logger.warning("scheduled_reports: schedule %s failed: %s", s.get("id"), e)
    return summary


def _hr_emails(cur, company_id):
    cur.execute(
        "SELECT COALESCE(cu.email, u.email) AS email FROM company_users cu "
        "LEFT JOIN users u ON u.id = cu.user_id "
        "WHERE cu.company_id = %s AND cu.status = 'active' AND cu.role IN ('company_admin', 'hr_manager')",
        (company_id,),
    )
    seen, out = set(), []
    for r in cur.fetchall() or []:
        e = (r.get("email") or "").strip()
        if e and e.lower() not in seen:
            seen.add(e.lower())
            out.append(e)
    return out


def _send(to_email, title, schedule, csv_body, row_count):
    from email_service import send_branded_email, _resolve_branding
    scope = schedule.get("department") or "hele virksomheden"
    send_branded_email(
        to_email, "Planlagt rapport: %s" % title, "scheduled_report",
        _resolve_branding(schedule["company_id"]), company_id=schedule["company_id"],
        attachments=[("%s.csv" % schedule["report_type"], csv_body.encode("utf-8"), "text/csv")],
        report_title=title, row_count=row_count, scope=scope,
        cadence={"daily": "dagligt", "weekly": "ugentligt", "monthly": "månedligt"}.get(schedule["cadence"], ""),
    )
