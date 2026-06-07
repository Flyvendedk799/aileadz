"""digest_service.py — per-manager weekly digest email (the cadence keystone).

Problem this solves (integration-spine completion, A3.6)
-------------------------------------------------------
The platform accumulates manager-relevant signals all week (orders awaiting
approval, budget burn, employees going quiet, widening skill gaps) but nothing
ever *pushes* a summary out. Managers only see these when they happen to open a
dashboard. This module builds a concise, company-scoped weekly digest and sends
it (best-effort) to each manager-level recipient via the existing branded-email
layer. It is driven by the scheduler's ``weekly_manager_digest`` job.

Design constraints (matching the rest of the codebase)
------------------------------------------------------
  * ``current_app.mysql.connection`` for DB access; ``DictCursor`` reads.
  * ``autocommit=False`` -> we ``commit()`` only if we write (we don't here;
    this module is read-only against the data, it just sends email).
  * Every query is scoped by ``company_id`` (no FKs in this codebase). A
    company's digest can NEVER include another company's rows.
  * SELF-CONTAINED SQL: this module does NOT import hr_tools.py — it owns small,
    purpose-built queries so the weekly job stays cheap and independent.
  * Everything is GUARDED: ``build_company_digest`` and ``send_company_digest``
    NEVER raise into the scheduler. The worst case is "no email this week".
  * Rendered via ``email_service.send_branded_email`` (ops-gated: no-ops cleanly
    when no MAIL backend is configured), reusing the same branded template
    machinery as order_approval_needed.
"""

import logging

logger = logging.getLogger(__name__)

# Manager-level roles that receive the digest. company_users.role is aliased to
# the session's company_role elsewhere (see admin_dashboard), so these are the
# same role strings used across the app.
MANAGER_ROLES = ("hr_manager", "department_head", "company_admin")

# An employee is "inactive" if they have not been active for this many days.
INACTIVE_DAYS = 7


def _get_conn():
    """Return the request/app-scoped MySQL connection, or None. Never raises."""
    try:
        from flask import current_app
        return current_app.mysql.connection
    except Exception:
        return None


def _dict_cursor(conn):
    """Return a DictCursor (rows read by column name)."""
    try:
        import MySQLdb.cursors
        return conn.cursor(MySQLdb.cursors.DictCursor)
    except Exception:
        # PyMySQL / default cursor (this app installs DictCursor as default).
        return conn.cursor()


def _scalar(row, key, default=0):
    """Read one value from a possibly-dict / possibly-tuple row."""
    if row is None:
        return default
    if isinstance(row, dict):
        v = row.get(key)
    else:
        try:
            v = row[0]
        except Exception:
            v = default
    return default if v is None else v


# ---------------------------------------------------------------------------
# Metric queries — small, self-contained, STRICTLY company_id-scoped.
# ---------------------------------------------------------------------------

def _pending_approvals_count(cur, company_id):
    """Number of orders awaiting approval for this company."""
    try:
        cur.execute(
            "SELECT COUNT(*) AS n FROM order_approvals "
            "WHERE company_id = %s AND status = 'pending'",
            (company_id,),
        )
        return int(_scalar(cur.fetchone(), "n", 0) or 0)
    except Exception as e:
        logger.debug("digest_service: pending approvals query failed: %s", e)
        return 0


def _budget_utilization(cur, company_id):
    """Company-wide training-budget utilization % (spent / annual_budget).

    Aggregates across this company's department_budgets for the current fiscal
    year. Returns an int percentage (0..N) or None when there is no budget.
    """
    try:
        import datetime
        fiscal_year = datetime.datetime.now().year
        cur.execute(
            "SELECT COALESCE(SUM(annual_budget), 0) AS annual, "
            "       COALESCE(SUM(spent), 0) AS spent "
            "FROM department_budgets "
            "WHERE company_id = %s AND fiscal_year = %s",
            (company_id, fiscal_year),
        )
        row = cur.fetchone()
        annual = float(_scalar(row, "annual", 0) or 0)
        spent = float(_scalar(row, "spent", 0) or 0)
        if annual <= 0:
            return None
        return int(round((spent / annual) * 100))
    except Exception as e:
        logger.debug("digest_service: budget utilization query failed: %s", e)
        return None


def _inactive_employees_count(cur, company_id):
    """Active employees with no activity in the last INACTIVE_DAYS days.

    "Activity" is the most recent of last_active_at / last_login /
    last_chatbot_interaction. Rows where ALL three are NULL are treated as
    inactive (never seen). Only counts employees with status='active'.
    """
    try:
        # COALESCE(...) is NULL only when ALL three activity columns are NULL
        # (never seen) -> that row counts as inactive too. The whole activity
        # predicate is parenthesised so the company_id/status scope always holds.
        cur.execute(
            "SELECT COUNT(*) AS n FROM company_users "
            "WHERE company_id = %s AND status = 'active' "
            "  AND ("
            "      COALESCE(last_active_at, last_login, last_chatbot_interaction) IS NULL "
            "      OR COALESCE(last_active_at, last_login, last_chatbot_interaction) "
            "         < DATE_SUB(NOW(), INTERVAL %s DAY)"
            "  )",
            (company_id, INACTIVE_DAYS),
        )
        return int(_scalar(cur.fetchone(), "n", 0) or 0)
    except Exception as e:
        logger.debug("digest_service: inactive employees query failed: %s", e)
        return 0


def _skill_gaps_count(cur, company_id):
    """Open skill gaps for this company (current_level below target_level).

    Reads employee_skills_matrix (company-scoped). A gap is a row whose
    current_level is below its target_level. Returns the count of such rows.
    """
    try:
        cur.execute(
            "SELECT COUNT(*) AS n FROM employee_skills_matrix "
            "WHERE company_id = %s "
            "  AND target_level IS NOT NULL AND target_level > 0 "
            "  AND COALESCE(current_level, 0) < target_level",
            (company_id,),
        )
        return int(_scalar(cur.fetchone(), "n", 0) or 0)
    except Exception as e:
        logger.debug("digest_service: skill gaps query failed: %s", e)
        return 0


def _critical_skill_gaps_count(cur, company_id):
    """Critical/high-priority open skill gaps (joined to company_skill_targets).

    Best-effort: matches matrix gaps against high/critical-priority skill
    targets for the same company + skill. Returns 0 on any error (the headline
    metric is the total gap count; this just surfaces the urgent slice).
    """
    try:
        cur.execute(
            "SELECT COUNT(*) AS n "
            "FROM employee_skills_matrix m "
            "JOIN company_skill_targets t "
            "  ON t.company_id = m.company_id AND t.skill_name = m.skill_name "
            "WHERE m.company_id = %s "
            "  AND t.priority IN ('high', 'critical') "
            "  AND m.target_level IS NOT NULL AND m.target_level > 0 "
            "  AND COALESCE(m.current_level, 0) < m.target_level",
            (company_id,),
        )
        return int(_scalar(cur.fetchone(), "n", 0) or 0)
    except Exception as e:
        logger.debug("digest_service: critical skill gaps query failed: %s", e)
        return 0


def _recipients(cur, company_id):
    """Manager-level active recipients (email + name) for this company.

    Returns a list of {'email', 'name'} dicts. Skips rows without an email.
    """
    try:
        # IN-clause is built from the fixed MANAGER_ROLES tuple (no user input).
        placeholders = ", ".join(["%s"] * len(MANAGER_ROLES))
        cur.execute(
            "SELECT email, full_name FROM company_users "
            "WHERE company_id = %s AND status = 'active' "
            "  AND role IN (" + placeholders + ") "
            "  AND email IS NOT NULL AND email <> ''",
            (company_id, *MANAGER_ROLES),
        )
        out = []
        for row in (cur.fetchall() or []):
            if isinstance(row, dict):
                email = (row.get("email") or "").strip()
                name = row.get("full_name") or ""
            else:
                email = (row[0] or "").strip()
                name = row[1] if len(row) > 1 else ""
            if email:
                out.append({"email": email, "name": name})
        return out
    except Exception as e:
        logger.debug("digest_service: recipients query failed: %s", e)
        return []


def _company_name(cur, company_id):
    try:
        cur.execute("SELECT company_name FROM companies WHERE id = %s", (company_id,))
        return _scalar(cur.fetchone(), "company_name", "") or ""
    except Exception:
        return ""


# ---------------------------------------------------------------------------
# Public: build + send one company's digest.
# ---------------------------------------------------------------------------

def build_company_digest(company_id):
    """Build the digest metrics dict for one company. Never raises.

    Returns a dict::

        {
          'company_id': int,
          'company_name': str,
          'pending_approvals': int,
          'budget_utilization': int | None,   # percentage, None if no budget
          'inactive_employees': int,
          'skill_gaps': int,
          'critical_skill_gaps': int,
          'recipients': [{'email', 'name'}, ...],
          'has_signal': bool,                  # any non-zero metric
        }

    or ``None`` if there is no DB connection.
    """
    if not company_id:
        return None
    conn = _get_conn()
    if conn is None:
        return None

    cur = None
    try:
        cur = _dict_cursor(conn)
        pending = _pending_approvals_count(cur, company_id)
        budget = _budget_utilization(cur, company_id)
        inactive = _inactive_employees_count(cur, company_id)
        gaps = _skill_gaps_count(cur, company_id)
        critical = _critical_skill_gaps_count(cur, company_id)
        recipients = _recipients(cur, company_id)
        name = _company_name(cur, company_id)
    except Exception as e:
        logger.warning("digest_service: build_company_digest failed for %s: %s", company_id, e)
        return None
    finally:
        try:
            if cur is not None:
                cur.close()
        except Exception:
            pass

    has_signal = bool(
        pending or inactive or gaps or critical
        or (budget is not None and budget >= 80)
    )
    return {
        "company_id": int(company_id),
        "company_name": name,
        "pending_approvals": pending,
        "budget_utilization": budget,
        "inactive_employees": inactive,
        "skill_gaps": gaps,
        "critical_skill_gaps": critical,
        "recipients": recipients,
        "has_signal": has_signal,
    }


def send_company_digest(company_id, *, force=False):
    """Build + email this company's weekly digest. Never raises.

    Best-effort: sends one branded email per manager-level recipient. When
    ``force`` is False (the default) we skip sending if there is no signal at
    all (no pending approvals, no inactive employees, no skill gaps and budget
    under 80%) so managers aren't spammed with empty digests.

    Returns a small summary dict::

        {'company_id', 'recipients', 'sent', 'skipped': bool, 'has_signal'}
    """
    summary = {"company_id": company_id, "recipients": 0, "sent": 0,
               "skipped": False, "has_signal": False}
    try:
        digest = build_company_digest(company_id)
        if not digest:
            summary["skipped"] = True
            return summary

        summary["has_signal"] = digest["has_signal"]
        recipients = digest.get("recipients") or []
        summary["recipients"] = len(recipients)

        if not recipients:
            summary["skipped"] = True
            return summary
        if not force and not digest["has_signal"]:
            # Nothing worth a manager's attention this week — stay quiet.
            summary["skipped"] = True
            return summary

        try:
            from email_service import send_branded_email, _resolve_branding
        except Exception as e:
            logger.debug("digest_service: email_service unavailable: %s", e)
            summary["skipped"] = True
            return summary

        branding = {}
        try:
            branding = _resolve_branding(company_id) or {}
        except Exception:
            branding = {}

        company_name = (
            digest.get("company_name")
            or (branding or {}).get("company_name")
            or "Futurematch"
        )
        budget = digest["budget_utilization"]
        budget_text = ("%d%%" % budget) if budget is not None else "—"
        subject = "Ugentligt lederoverblik — %s" % company_name

        sent = 0
        for r in recipients:
            try:
                ok = send_branded_email(
                    r["email"],
                    subject,
                    "manager_weekly_digest",
                    branding,
                    company_id=company_id,
                    company_name=company_name,
                    recipient_name=r.get("name") or "",
                    pending_approvals=digest["pending_approvals"],
                    budget_utilization=budget_text,
                    inactive_employees=digest["inactive_employees"],
                    skill_gaps=digest["skill_gaps"],
                    critical_skill_gaps=digest["critical_skill_gaps"],
                )
                if ok:
                    sent += 1
            except Exception as e:
                logger.debug("digest_service: send to %s skipped: %s", r.get("email"), e)
        summary["sent"] = sent
        return summary
    except Exception as e:
        logger.warning("digest_service: send_company_digest failed for %s: %s", company_id, e)
        summary["skipped"] = True
        return summary
