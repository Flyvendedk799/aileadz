"""HR-written goals with per-goal sharing (N-3.5 UX side of S-4.4).

Every HR goal (``employee_goals``) has a "Del med medarbejder" toggle, OFF by
default.  Only shared goals reach the learner: ``/mine-maal`` ("Mål fra din
leder"), the learner AI context and learner-facing exports.  Unshared goals stay
HR-only.  Sharing and unsharing are audit-logged and the learner is notified when
a goal is shared with them.

``employee_goals.employee_id`` holds ``users.id`` (see learner_context docstring).
All writers take the caller's cursor and commit nothing: the route commits.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

OPEN_STATUSES = ("active", "in_progress")


def _row(cur):
    return cur.fetchone()


def employee_in_company(cur, company_id, user_id):
    cur.execute(
        "SELECT user_id, username, full_name FROM company_users "
        "WHERE company_id = %s AND user_id = %s AND status = 'active' LIMIT 1",
        (company_id, user_id),
    )
    return _row(cur)


def list_goals_for_hr(cur, company_id, employee_id):
    """(shared, private) goal lists for the HR view of one employee."""
    cur.execute(
        """SELECT id, goal_title, goal_description, target_date, status, progress,
                  shared_with_employee, shared_at, share_note, created_at
           FROM employee_goals WHERE company_id = %s AND employee_id = %s
           ORDER BY created_at DESC, id DESC""",
        (company_id, employee_id),
    )
    rows = list(cur.fetchall() or [])
    shared = [r for r in rows if int(r.get("shared_with_employee") or 0) == 1]
    private = [r for r in rows if int(r.get("shared_with_employee") or 0) != 1]
    return shared, private


def shared_goals_for_learner(cur, user_id, company_id):
    """What the learner may see: SHARED goals only. Never raises."""
    if not user_id or not company_id:
        return []
    try:
        cur.execute(
            """SELECT id, goal_title, goal_description, target_date, status, progress, shared_at, share_note
               FROM employee_goals
               WHERE employee_id = %s AND company_id = %s AND shared_with_employee = 1
               ORDER BY (status IN ('completed')) ASC, shared_at DESC, id DESC""",
            (user_id, company_id),
        )
        return list(cur.fetchall() or [])
    except Exception as e:
        logger.warning("goal_sharing: learner goals failed: %s", e)
        return []


def add_goal(cur, *, company_id, employee_id, title, description="", target_date=None, share=False,
             actor_user_id=None, note=None):
    title = (title or "").strip()[:255]
    if not title:
        return {"success": False, "message": "Angiv en titel til målet."}
    cur.execute(
        """INSERT INTO employee_goals
               (employee_id, company_id, goal_title, goal_description, target_date, status, progress,
                shared_with_employee)
           VALUES (%s, %s, %s, %s, %s, 'active', 0, 0)""",
        (employee_id, company_id, title, (description or "").strip() or None, target_date or None),
    )
    gid = getattr(cur, "lastrowid", None)
    _audit(cur, company_id, actor_user_id, "goal.created", gid, "Mål oprettet (kun synligt for HR)")
    if share and gid:
        return set_shared(cur, company_id=company_id, goal_id=gid, shared=True,
                          actor_user_id=actor_user_id, note=note)
    return {"success": True, "goal_id": gid, "shared": False}


def set_shared(cur, *, company_id, goal_id, shared, actor_user_id=None, note=None):
    """Move a goal between "Delt med medarbejderen" and "Kun synligt for HR"."""
    cur.execute(
        "SELECT id, employee_id, goal_title, shared_with_employee FROM employee_goals "
        "WHERE id = %s AND company_id = %s",
        (goal_id, company_id),
    )
    goal = _row(cur)
    if not goal:
        return {"success": False, "error": "not_found", "message": "Målet blev ikke fundet."}
    target = 1 if shared else 0
    if int(goal.get("shared_with_employee") or 0) == target:
        return {"success": True, "unchanged": True, "goal_id": goal_id, "shared": bool(target)}
    if target:
        cur.execute(
            "UPDATE employee_goals SET shared_with_employee = 1, shared_at = CURRENT_TIMESTAMP, share_note = %s "
            "WHERE id = %s AND company_id = %s",
            ((note or None) and str(note)[:500], goal_id, company_id),
        )
    else:
        cur.execute(
            "UPDATE employee_goals SET shared_with_employee = 0, shared_at = NULL, share_note = NULL "
            "WHERE id = %s AND company_id = %s",
            (goal_id, company_id),
        )
    _audit(cur, company_id, actor_user_id,
           "goal.shared" if target else "goal.unshared", goal_id,
           "%s: %s" % ("Delt med medarbejder" if target else "Gjort privat", goal.get("goal_title")))
    if target:
        try:
            from notification_service import notify_user
            msg = "Din leder har delt et udviklingsmål med dig: “%s”." % goal.get("goal_title")
            if note:
                msg += " Besked: %s" % note
            notify_user(cur, user_id=goal["employee_id"], company_id=company_id, sender_user_id=actor_user_id,
                        title="Nyt mål fra din leder", message=msg, kind="goal", action_url="/mine-maal",
                        dedupe_key="goal-shared:%s" % goal_id, dedupe_hours=None)
        except Exception as e:
            logger.debug("goal_sharing: notification skipped: %s", e)
    return {"success": True, "goal_id": goal_id, "shared": bool(target)}


def _audit(cur, company_id, user_id, action, goal_id, description):
    try:
        cur.execute(
            """INSERT INTO audit_log (company_id, user_id, action, action_type, resource_type, resource_id,
                                      description, details)
               VALUES (%s, %s, %s, %s, 'goal', %s, %s, %s)""",
            (company_id, user_id, action, action, str(goal_id), description, description),
        )
    except Exception as e:
        logger.debug("goal_sharing: audit skipped: %s", e)
