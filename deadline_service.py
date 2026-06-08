"""deadline_service.py — proactive learning-deadline reminders (plan #17).

Problem this solves (a missing proactive loop on data that already exists)
-------------------------------------------------------------------------
``employee_learning_progress.due_date`` exists (enterprise_tables.py) and the
``deadline_ics`` route (hr_ext.py) already reads it, but NOTHING ever warns when
a learning-path deadline is approaching: deadlines silently pass — a direct
completion- and compliance-failure. The scheduler is the right home (it already
runs ``compliance_recheck`` / ``daily_agreement_alerts`` / the weekly digest on
the same idle infra). This module fills that gap: on a daily cadence it finds
not-yet-completed learning-progress rows whose ``due_date`` is within N days (or
already overdue) and raises:

  1. ONE aggregate, k-anon-safe (counts-only) ``company_notifications`` card to
     the HR/manager roles per company — the proactive loop closure (always on,
     same safety basis as the compliance donut / the compliance recert card).
  2. OPTIONALLY a direct per-learner reminder card — but ONLY when the company
     has explicitly opted in (``company_settings.learning_deadline_reminders_enabled``),
     because nudging individuals on a cadence is the spam-prone half the plan
     requires HR opt-in for (risk register #321, multiSelect-not-exclusion).

Design constraints (matching compliance_service / digest_service / catalog_freshness)
-------------------------------------------------------------------------------------
  * SELF-CONTAINED, company-scoped SQL: one bounded query over
    ``employee_learning_progress`` (scoped by ``company_id``), no cross-company
    leakage, no FKs assumed.
  * AGGREGATE / k-anon-safe MANAGER card: it quotes COUNTS only (X overdue, Y due
    soon) — never a named individual — so it carries no k-anon exposure.
  * The per-learner reminder is addressed to the learner THEMSELVES
    (``recipient_user_id`` = that user) about their OWN deadline, so it is not a
    people-level disclosure to a third party; it is still gated behind the HR
    opt-in so a tenant is never auto-spamming its workforce.
  * Recent-duplicate dedupe (mirrors catalog_freshness / compliance_service): a
    per-company marker (manager card) and a per-progress-row marker (learner
    reminder) embedded in the message means the same deadline is not re-nudged
    within ``_NOTIFY_DEDUPE_DAYS`` days.
  * Cap per run: at most ``_MAX_LEARNER_REMINDERS`` learner reminders per company
    per pass so one large tenant can't flood the notification table in one go.
  * Fully GUARDED: every public function never raises into the scheduler — the
    worst case is "no nudge this pass, retried next pass". ``autocommit=False`` so
    the notification writer commits explicitly.
"""

import json
import logging

logger = logging.getLogger(__name__)

# HR/manager roles that see the aggregate company-wide card (matches the role set
# catalog_freshness / compliance_service / require_hr_access() use).
_HR_ROLES = ["company_admin", "hr_manager", "department_head"]

# Deadlines within this many days (or already overdue) are in scope.
_WITHIN_DAYS = 14

# Don't re-notify about the same scope (company aggregate / per-progress row)
# within this many days, so neither HR nor a learner is spammed.
_NOTIFY_DEDUPE_DAYS = 7

# Cap the per-learner direct reminders raised for one company in a single pass.
_MAX_LEARNER_REMINDERS = 100


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


def _learner_reminders_enabled(cur, company_id):
    """True only when the company has explicitly OPTED IN to per-learner nudges.

    HR controls this via ``company_settings.learning_deadline_reminders_enabled``
    (default 0 = OFF). Missing settings row / missing column / any error -> False
    (fail CLOSED: never auto-spam a workforce that did not opt in).
    """
    try:
        cur.execute(
            "SELECT learning_deadline_reminders_enabled AS en "
            "FROM company_settings WHERE company_id = %s",
            (company_id,),
        )
        return int(_scalar(cur.fetchone(), "en", 0) or 0) == 1
    except Exception as e:
        logger.debug("deadline_service: opt-in lookup failed for %s: %s", company_id, e)
        return False


def _upcoming_deadlines(cur, company_id, within_days=_WITHIN_DAYS):
    """Not-yet-completed learning-progress rows due within ``within_days`` days.

    Strictly company-scoped. "Not completed" = ``status`` is not 'completed' AND
    ``completed_at`` is NULL AND progress is below 100. A deadline is in scope
    when ``due_date`` is on or before (today + within_days) — this includes
    already-overdue rows (negative days_left).

    Returns a list of dicts sorted soonest/most-overdue first::

        {progress_id, user_id, content_name, due_date, days_left, is_overdue}

    Never raises; returns [] on any error.
    """
    try:
        within = int(within_days)
    except (TypeError, ValueError):
        within = _WITHIN_DAYS
    if within < 0:
        within = 0
    if not company_id:
        return []
    try:
        cur.execute(
            """
            SELECT elp.id                                       AS progress_id,
                   elp.user_id                                  AS user_id,
                   COALESCE(NULLIF(elp.content_name, ''),
                            lp.path_name, 'Læringsforløb')      AS content_name,
                   elp.due_date                                 AS due_date,
                   DATEDIFF(elp.due_date, CURDATE())            AS days_left
            FROM employee_learning_progress elp
            LEFT JOIN learning_paths lp ON lp.id = elp.learning_path_id
            WHERE elp.company_id = %s
              AND elp.due_date IS NOT NULL
              AND elp.due_date <= DATE_ADD(CURDATE(), INTERVAL %s DAY)
              AND COALESCE(elp.status, '') <> 'completed'
              AND elp.completed_at IS NULL
              AND COALESCE(elp.progress_percentage, 0) < 100
            ORDER BY elp.due_date ASC
            """,
            (company_id, within),
        )
        rows = cur.fetchall() or []
    except Exception as e:
        logger.debug("deadline_service: deadline query failed for %s: %s", company_id, e)
        return []

    out = []
    for r in rows:
        try:
            if isinstance(r, dict):
                pid = r.get("progress_id")
                uid = r.get("user_id")
                name = r.get("content_name") or "Læringsforløb"
                due = r.get("due_date")
                days_left = r.get("days_left")
            else:
                pid, uid, name, due, days_left = r[0], r[1], r[2], r[3], r[4]
            days_left = int(days_left) if days_left is not None else None
            out.append({
                "progress_id": pid,
                "user_id": uid,
                "content_name": (name or "Læringsforløb"),
                "due_date": due,
                "days_left": days_left,
                "is_overdue": (days_left is not None and days_left < 0),
            })
        except Exception:
            continue
    return out


def _company_marker(company_id):
    """Stable per-company marker for the aggregate card recent-duplicate guard."""
    return "learning-deadline:company-%s" % company_id


def _progress_marker(progress_id):
    """Stable per-progress-row marker for the learner reminder dedupe guard."""
    return "learning-deadline:progress-%s" % progress_id


def _build_manager_message(overdue, soon, marker):
    """Danish aggregate body for the HR/manager card (COUNTS only, no names)."""
    parts = []
    if overdue:
        parts.append("%d læringsforløb er forfaldne" % overdue)
    if soon:
        parts.append("%d har frist inden for %d dage" % (soon, _WITHIN_DAYS))
    detail = " og ".join(parts) if parts else "har frister, der nærmer sig"
    # The marker is embedded in the message so the dedupe LIKE can find it
    # without an extra column (same trick catalog_freshness / compliance use).
    return (
        "%s. Følg op, så fristerne ikke glider — log ind for at se hvem og "
        "planlæg de manglende gennemførelser. [%s]" % (detail.capitalize(), marker)
    )


def _build_learner_message(content_name, days_left, is_overdue, marker):
    """Danish reminder body for the learner's OWN deadline."""
    title = (content_name or "dit læringsforløb").strip()
    if is_overdue and days_left is not None:
        when = "var forfalden for %d dag%s siden" % (
            abs(days_left), "e" if abs(days_left) != 1 else "")
    elif days_left == 0:
        when = "har frist i dag"
    elif days_left is not None:
        when = "har frist om %d dag%s" % (days_left, "e" if days_left != 1 else "")
    else:
        when = "nærmer sig sin frist"
    return (
        "Påmindelse: “%s” %s. Log ind og fuldfør forløbet i tide. [%s]"
        % (title, when, marker)
    )


def _recent_marker_exists(cur, company_id, marker):
    """True if a notification carrying ``marker`` was raised in the dedupe window."""
    try:
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
        return int(_scalar(cur.fetchone(), "cnt", 0) or 0) > 0
    except Exception as e:
        logger.debug("deadline_service: dedupe check failed: %s", e)
        # Fail safe: treat an errored check as "recently nudged" so we never
        # double-send because the guard itself broke.
        return True


def remind_company(company_id):
    """Run the full proactive deadline check for one company. Never raises.

    Returns ``{'company_id', 'manager_cards', 'learner_reminders'}``. A single
    company's failure must not abort the scheduler batch.
    """
    summary = {"company_id": company_id, "manager_cards": 0, "learner_reminders": 0}
    if not company_id:
        return summary

    conn = _get_conn()
    if conn is None:
        return summary
    cur = _dict_cursor(conn)
    if cur is None:
        return summary

    try:
        rows = _upcoming_deadlines(cur, company_id)
        if not rows:
            return summary

        overdue = sum(1 for r in rows if r.get("is_overdue"))
        soon = len(rows) - overdue

        # 1) Aggregate HR/manager card — always (k-anon-safe, counts only).
        try:
            summary["manager_cards"] = _insert_manager_card(
                cur, conn, company_id, overdue, soon)
        except Exception as e:
            logger.warning("deadline_service: manager card failed for %s: %s",
                           company_id, e)

        # 2) Per-learner direct reminders — ONLY if HR opted in.
        try:
            if _learner_reminders_enabled(cur, company_id):
                summary["learner_reminders"] = _remind_learners(
                    cur, conn, company_id, rows)
        except Exception as e:
            logger.debug("deadline_service: learner reminders failed for %s: %s",
                         company_id, e)
    finally:
        try:
            cur.close()
        except Exception:
            pass
    return summary


def _insert_manager_card(cur, conn, company_id, overdue, soon):
    """Insert ONE deduped aggregate HR/manager card. Returns 1 if inserted else 0.

    Counts-only, k-anon-safe, role-broadcast (recipient NULL + target_roles).
    Never raises (caller guards too).
    """
    if not company_id or (not overdue and not soon):
        return 0
    marker = _company_marker(company_id)
    if _recent_marker_exists(cur, company_id, marker):
        return 0  # Already nudged HR recently — don't spam.

    is_urgent = 1 if overdue > 0 else 0
    title = ("Læringsfrister forfaldne — handling påkrævet"
             if overdue > 0 else "Læringsfrister nærmer sig")
    message = _build_manager_message(overdue, soon, marker)
    try:
        cur.execute(
            """
            INSERT INTO company_notifications
                (company_id, recipient_user_id, sender_user_id,
                 target_roles, title, message, is_urgent, is_read)
            VALUES (%s, NULL, NULL, %s, %s, %s, %s, 0)
            """,
            (company_id, json.dumps(_HR_ROLES), title[:255], message, is_urgent),
        )
        conn.commit()
        return 1
    except Exception as e:
        logger.warning("deadline_service: manager card insert failed for %s: %s",
                       company_id, e)
        try:
            conn.rollback()
        except Exception:
            pass
        return 0


def _remind_learners(cur, conn, company_id, rows):
    """Insert deduped per-learner reminders (opt-in only). Returns count inserted.

    Each reminder is addressed to the learner THEMSELVES about their OWN deadline
    (``recipient_user_id`` = that user), so it is not a third-party disclosure.
    Capped at ``_MAX_LEARNER_REMINDERS`` per company per pass. Never raises.
    """
    if not company_id or not rows:
        return 0
    inserted = 0
    try:
        for r in rows:
            if inserted >= _MAX_LEARNER_REMINDERS:
                break
            uid = r.get("user_id")
            pid = r.get("progress_id")
            if not uid or pid is None:
                continue
            try:
                marker = _progress_marker(pid)
                if _recent_marker_exists(cur, company_id, marker):
                    continue  # Already reminded this learner about this row.

                is_overdue = bool(r.get("is_overdue"))
                title = ("Forfalden læringsfrist" if is_overdue
                         else "Påmindelse om læringsfrist")
                message = _build_learner_message(
                    r.get("content_name"), r.get("days_left"), is_overdue, marker)
                cur.execute(
                    """
                    INSERT INTO company_notifications
                        (company_id, recipient_user_id, sender_user_id,
                         target_roles, title, message, is_urgent, is_read)
                    VALUES (%s, %s, NULL, NULL, %s, %s, %s, 0)
                    """,
                    (company_id, uid, title[:255], message,
                     1 if is_overdue else 0),
                )
                inserted += 1
            except Exception as exc:
                logger.debug("deadline_service: learner reminder skipped: %s", exc)
                continue

        if inserted:
            try:
                conn.commit()
            except Exception as exc:
                logger.warning("deadline_service: learner commit failed: %s", exc)
                try:
                    conn.rollback()
                except Exception:
                    pass
                inserted = 0
    except Exception as exc:
        logger.warning("deadline_service: learner reminders failed for %s: %s",
                       company_id, exc)
        inserted = 0
    return inserted
