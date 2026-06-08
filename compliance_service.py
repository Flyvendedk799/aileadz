"""compliance_service.py — proactive recertification reminders (plan #16).

Problem this solves (the #1 must-buy DK compliance wedge)
--------------------------------------------------------
The compliance matrix (``hr_tools.derive_company_compliance``) is a live, purely
read-on-render view: statutory/mandatory recerts (arbejdsmiljø, GDPR, ISO …)
only surface when a human happens to open the page. The scheduler had a wired-up
but explicit no-op ``compliance_recheck`` slot. This module fills that slot: it
re-derives compliance per company on a daily cadence and raises an in-app
``company_notifications`` card (and, for critical statutory overdue, a branded
manager email) whenever a requirement has expiring/overdue/missing coverage — so
recertification windows stop lapsing silently.

Design constraints (matching catalog_freshness / digest_service)
----------------------------------------------------------------
  * Reuses ``hr_tools.derive_company_compliance`` — the EXACT same derivation the
    HR matrix and chatbot tool use — rather than reimplementing it. This module
    orchestrates the cadence + the notification channel only.
  * AGGREGATE ONLY / k-anon-safe: the cards quote per-requirement status COUNTS
    (X overdue, Y missing, Z expiring) — company-wide aggregates, never a named
    individual or a people-level row — so they carry no k-anon exposure (same
    safety basis as the compliance donut shipped in plan #6).
  * Company-scoped: every read and every notification is keyed on ``company_id``.
  * Recent-duplicate dedupe (mirrors ``catalog_freshness.notify_expiring_agreements``):
    a per-requirement marker embedded in the message means the same requirement
    is not re-nudged within ``_NOTIFY_DEDUPE_DAYS`` days, so HR is never spammed.
  * Email escalation is strictly ops-gated (``email_service.send_branded_email``
    no-ops cleanly when MAIL is not configured) AND email-dedupe-gated
    (``email_recently_sent``), so it stays silent without SMTP and never re-sends
    the same alert within the window.
  * Fully GUARDED: every public function never raises into the scheduler — the
    worst case is "no nudge this pass, retried next pass". ``autocommit=False`` so
    the notification writer commits explicitly.
"""

import json
import logging

logger = logging.getLogger(__name__)

# HR-ish roles that should see compliance recert nudges (matches the role set
# catalog_freshness and require_hr_access() use for HR notifications).
_HR_ROLES = ["company_admin", "hr_manager", "department_head"]

# Don't re-notify about the same requirement within this many days.
_NOTIFY_DEDUPE_DAYS = 14

# A requirement is worth a nudge when it has any of these gap statuses.
# "expiring" is included so HR gets a heads-up BEFORE a recert actually lapses.
_GAP_KEYS = ("overdue", "missing", "expiring")

# Email escalation is reserved for the most urgent case: a STATUTORY requirement
# with at least one OVERDUE (already-lapsed) employee. Re-send no more than once
# per this many hours per requirement.
_EMAIL_DEDUPE_HOURS = 24 * 7  # one escalation email per requirement per week


def _get_conn():
    """Return the request/app-scoped MySQL connection, or None. Never raises."""
    try:
        from flask import current_app
        return current_app.mysql.connection
    except Exception:
        return None


def _dict_cursor(conn):
    """Return a DictCursor (rows read by column name). Never raises -> None."""
    try:
        import MySQLdb.cursors
        return conn.cursor(MySQLdb.cursors.DictCursor)
    except Exception:
        try:
            return conn.cursor()
        except Exception:
            return None


def _gap_requirements(company_id):
    """Re-derive compliance and return the requirements that have a live gap.

    Delegates to ``hr_tools.derive_company_compliance`` (the canonical derivation)
    and keeps only requirements with at least one overdue/missing/expiring
    employee. Returns a list of the per-requirement dicts (aggregate counts only).
    Never raises; returns [] on any error.
    """
    conn = _get_conn()
    if conn is None or not company_id:
        return []
    try:
        from hr_tools import derive_company_compliance
    except Exception as e:
        logger.debug("compliance_service: hr_tools import failed: %s", e)
        return []
    try:
        # only_gaps=True already filters to has_gap requirements server-side; we
        # re-check the explicit gap counts below so the marker/urgency are exact.
        result = derive_company_compliance(conn, company_id, only_gaps=True) or {}
    except Exception as e:
        logger.debug("compliance_service: derive failed for %s: %s", company_id, e)
        return []
    reqs = result.get("requirements") or []
    gaps = []
    for r in reqs:
        try:
            if any(int(r.get(k) or 0) > 0 for k in _GAP_KEYS):
                gaps.append(r)
        except Exception:
            continue
    return gaps


def _marker(req):
    """Stable per-requirement marker for the recent-duplicate guard.

    Prefers the requirement id (immutable) and falls back to the title so the
    LIKE-based dedupe finds prior cards even on a tenant with un-id'd rows.
    """
    rid = req.get("id")
    if rid is not None:
        return "compliance-recert:req-%s" % rid
    return "compliance-recert:%s" % ((req.get("title") or "krav").strip().lower())


def _build_message(req, marker):
    """Danish notification body for one at-risk requirement (counts only)."""
    title = (req.get("title") or "Compliance-krav").strip()
    overdue = int(req.get("overdue") or 0)
    missing = int(req.get("missing") or 0)
    expiring = int(req.get("expiring") or 0)

    parts = []
    if overdue:
        parts.append("%d udløbet/forfaldne" % overdue)
    if missing:
        parts.append("%d mangler kurset" % missing)
    if expiring:
        parts.append("%d udløber snart" % expiring)
    detail = ", ".join(parts) if parts else "har huller i overholdelsen"

    statutory = " (lovpligtigt)" if req.get("is_statutory") else ""
    scope = req.get("applies_to_department") or "Alle afdelinger"

    # The marker is embedded in the message so the dedupe LIKE can find it
    # without needing an extra column (same trick catalog_freshness uses).
    return (
        "Recertificering for “%s”%s er ved at glide: %s "
        "(%s). Planlæg recertificering, så kravet ikke falder ud af "
        "overholdelse. [%s]" % (title, statutory, detail, scope, marker)
    )


def _manager_recipients(cur, company_id):
    """Manager-level active recipients (email + name) for this company.

    Returns a list of {'email', 'name'} dicts; skips rows without an email.
    Mirrors digest_service._recipients (same role set / query shape).
    """
    try:
        placeholders = ", ".join(["%s"] * len(_HR_ROLES))
        cur.execute(
            "SELECT email, full_name FROM company_users "
            "WHERE company_id = %s AND status = 'active' "
            "  AND role IN (" + placeholders + ") "
            "  AND email IS NOT NULL AND email <> ''",
            (company_id, *_HR_ROLES),
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
        logger.debug("compliance_service: recipients query failed: %s", e)
        return []


def _escalate_critical(company_id, gaps):
    """Email managers for STATUTORY requirements with overdue (lapsed) coverage.

    Strictly the most urgent slice — a statutory recert that has actually lapsed.
    Ops-gated (``send_branded_email`` no-ops without MAIL) and dedupe-gated
    (``email_recently_sent`` per requirement) so a configured tenant gets at most
    one escalation email per requirement per ``_EMAIL_DEDUPE_HOURS``. Returns the
    number of emails actually sent. Never raises.
    """
    critical = [r for r in gaps
                if r.get("is_statutory") and int(r.get("overdue") or 0) > 0]
    if not critical:
        return 0

    try:
        from email_service import (
            send_branded_email, email_recently_sent, _resolve_branding,
        )
    except Exception as e:
        logger.debug("compliance_service: email_service unavailable: %s", e)
        return 0

    conn = _get_conn()
    if conn is None:
        return 0
    cur = _dict_cursor(conn)
    if cur is None:
        return 0
    try:
        recipients = _manager_recipients(cur, company_id)
    finally:
        try:
            cur.close()
        except Exception:
            pass
    if not recipients:
        return 0

    try:
        branding = _resolve_branding(company_id) or {}
    except Exception:
        branding = {}
    company_name = (branding or {}).get("company_name") or "Futurematch"

    sent = 0
    for req in critical:
        marker = _marker(req)
        # One escalation email per requirement per window (covers ALL recipients
        # collectively, so the same lapse doesn't re-blast every night).
        dedupe_key = "compliance_recert:%s:%s" % (company_id, marker)
        try:
            if email_recently_sent(dedupe_key, within_hours=_EMAIL_DEDUPE_HOURS,
                                   company_id=company_id):
                continue
        except Exception:
            pass

        req_title = (req.get("title") or "Compliance-krav").strip()
        overdue = int(req.get("overdue") or 0)
        scope = req.get("applies_to_department") or "Alle afdelinger"
        subject = "Lovpligtig recertificering forfalden: %s" % req_title

        for r in recipients:
            try:
                ok = send_branded_email(
                    r["email"],
                    subject,
                    "compliance_recert_alert",
                    branding,
                    company_id=company_id,
                    dedupe_key=dedupe_key,
                    company_name=company_name,
                    recipient_name=r.get("name") or "",
                    requirement_title=req_title,
                    overdue_count=overdue,
                    scope=scope,
                )
                if ok:
                    sent += 1
            except Exception as e:
                logger.debug("compliance_service: email to %s skipped: %s",
                             r.get("email"), e)
    return sent


def recheck_company(company_id):
    """Run the full proactive recheck for one company: in-app cards + email.

    Returns a small summary dict ``{'company_id', 'notifications', 'emails'}``.
    Never raises — a single company's failure must not abort the scheduler batch.
    """
    summary = {"company_id": company_id, "notifications": 0, "emails": 0}
    if not company_id:
        return summary

    gaps = _gap_requirements(company_id)
    if not gaps:
        return summary

    try:
        summary["notifications"] = _insert_cards(company_id, gaps)
    except Exception as e:
        logger.warning("compliance_service: cards failed for %s: %s", company_id, e)
    try:
        summary["emails"] = _escalate_critical(company_id, gaps)
    except Exception as e:
        logger.debug("compliance_service: escalation failed for %s: %s", company_id, e)
    return summary


def _insert_cards(company_id, gaps):
    """Insert in-app HR cards for the given at-risk requirements (dedupe'd).

    Split out from the public path so ``recheck_company`` can reuse the already
    re-derived ``gaps`` (one derivation per company per pass). Returns the number
    of NEW cards inserted. Never raises.
    """
    if not company_id or not gaps:
        return 0
    conn = _get_conn()
    if conn is None:
        return 0
    cur = _dict_cursor(conn)
    if cur is None:
        return 0

    inserted = 0
    try:
        roles_json = json.dumps(_HR_ROLES)
        for req in gaps:
            try:
                marker = _marker(req)
                cur.execute(
                    """
                    SELECT COUNT(*) AS cnt
                    FROM company_notifications
                    WHERE company_id = %s
                      AND message LIKE %s
                      AND created_at >= DATE_SUB(NOW(), INTERVAL %s DAY)
                    """,
                    (company_id, "%" + marker + "%", _NOTIFY_DEDUPE_DAYS),
                )
                row = cur.fetchone()
                cnt = (row.get("cnt") if isinstance(row, dict)
                       else (row[0] if row else 0)) or 0
                if int(cnt) > 0:
                    continue  # Already nudged recently — don't spam.

                overdue = int(req.get("overdue") or 0)
                is_statutory = bool(req.get("is_statutory"))
                is_urgent = 1 if (is_statutory and overdue > 0) else 0
                title = ("Lovpligtig recertificering forfalden: %s"
                         if is_urgent
                         else "Recertificering kræver handling: %s") % (
                    (req.get("title") or "Compliance-krav").strip()
                )
                message = _build_message(req, marker)

                cur.execute(
                    """
                    INSERT INTO company_notifications
                        (company_id, recipient_user_id, sender_user_id,
                         target_roles, title, message, is_urgent, is_read)
                    VALUES (%s, NULL, NULL, %s, %s, %s, %s, 0)
                    """,
                    (company_id, roles_json, title[:255], message, is_urgent),
                )
                inserted += 1
            except Exception as exc:
                logger.debug("compliance_service: card insert skipped: %s", exc)
                continue

        if inserted:
            try:
                conn.commit()
            except Exception as exc:
                logger.warning("compliance_service: commit failed: %s", exc)
                try:
                    conn.rollback()
                except Exception:
                    pass
                inserted = 0
    except Exception as exc:
        logger.warning("compliance_service: insert cards failed for %s: %s",
                       company_id, exc)
        inserted = 0
    finally:
        try:
            cur.close()
        except Exception:
            pass
    return inserted
