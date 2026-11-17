"""One goal model: every development goal a person has, with an owner and a visibility.

Contract
--------
A goal is written either by the person (``user_learning_goals``, keyed by
username) or by HR for that person (``employee_goals``). This module is the one
read model over both and the one place that decides who may see a goal:

  * A person's own goal is **private** until they share it with HR. Sharing is
    per goal and per company (``shared_company_id``): HR of another company, or
    of a company the person has left, never sees it.
  * An HR-written goal is **HR-only** until HR shares it with the person
    (``goal_sharing`` / ``goal_sharing_ui`` own that flag and its audit).

Readers return the same record shape for both kinds::

    {key, id, owner ('self'|'hr'), title, description, target_date,
     status ('active'|'done'|'paused'), status_label, shared (bool), shared_at}

``for_learner`` never returns an unshared HR goal; ``for_hr`` never returns an
unshared own goal. New HR-facing code (pages, HR assistant tools, reports,
exports) must read goals through ``for_hr``, never from the tables.

Callers pass a DictCursor, authorize the actor, own the transaction and commit.
The talent workspace's development links (``talent_goal_links.shared``) mirror
the goal's flag: ``share_own_goal`` keeps them in step.
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

STATUS_LABELS = {"active": "Aktiv", "done": "Fuldført", "paused": "På pause"}
_OWN_STATUS = {"aktiv": "active", "fuldfoert": "done", "paa_pause": "paused"}
_HR_STATUS = {"active": "active", "in_progress": "active", "completed": "done", "done": "done",
              "paused": "paused", "on_hold": "paused"}
# What HR may set on a goal it wrote (canonical -> stored value).
HR_STATUS_VALUES = {"active": "active", "done": "completed", "paused": "paused"}


def _ready():
    """The sharing columns on ``user_learning_goals`` are added by the profile
    schema step; make sure it has run in this process (cached after the first call)."""
    try:
        from app1.user_profile_db import ensure_tables
        ensure_tables()
    except Exception:
        logger.debug("profile schema step unavailable", exc_info=True)


def _record(owner, row, status, shared, shared_at=None):
    return {
        "key": "%s:%s" % ("own" if owner == "self" else "hr", row["id"]),
        "id": row["id"],
        "owner": owner,
        "title": row.get("title") or row.get("goal_title") or "",
        "description": row.get("description") or row.get("goal_description") or "",
        "target_date": row.get("target_date"),
        "status": status,
        "status_label": STATUS_LABELS[status],
        "shared": bool(shared),
        "shared_at": shared_at,
    }


def _own(row, company_id):
    shared = bool(row.get("shared_with_hr")) and company_id is not None and row.get("shared_company_id") == company_id
    return _record("self", row, _OWN_STATUS.get(row.get("status") or "aktiv", "active"),
                   shared, row.get("shared_at") if shared else None)


def _hr(row):
    return _record("hr", row, _HR_STATUS.get((row.get("status") or "active").lower(), "active"),
                   row.get("shared_with_employee"), row.get("shared_at"))


def _username(cur, company_id, user_id):
    """The username of an ACTIVE member of this company, else None."""
    cur.execute("SELECT u.username FROM users u JOIN company_users cu ON cu.user_id=u.id "
                "WHERE cu.company_id=%s AND u.id=%s AND cu.status='active'", (company_id, user_id))
    row = cur.fetchone()
    return row["username"] if row else None


def for_learner(cur, username, company_id=None, user_id=None):
    """Everything the person may see: all their own goals, and the HR goals shared with them."""
    _ready()
    cur.execute("SELECT id,title,description,target_date,status,shared_with_hr,shared_company_id,shared_at "
                "FROM user_learning_goals WHERE username=%s ORDER BY (status='aktiv') DESC, updated_at DESC", (username,))
    own = [_own(r, company_id) for r in cur.fetchall()]
    from_manager = []
    if company_id and user_id:
        cur.execute("SELECT id,goal_title,goal_description,target_date,status,shared_with_employee,shared_at "
                    "FROM employee_goals WHERE company_id=%s AND employee_id=%s AND shared_with_employee=1 "
                    "ORDER BY (target_date IS NULL), target_date, id", (company_id, user_id))
        from_manager = [_hr(r) for r in cur.fetchall()]
    return {"own": own, "from_manager": from_manager}


def for_hr(cur, company_id, user_id):
    """Everything this company's HR may see about one employee's goals.

    ``shared_by_employee``: the person's own goals they chose to share with this
    company, nothing else of theirs. ``shared_with_employee`` / ``hr_only``: the
    goals HR wrote."""
    _ready()
    out = {"shared_by_employee": [], "shared_with_employee": [], "hr_only": []}
    username = _username(cur, company_id, user_id)
    if not username:
        return out
    cur.execute("SELECT id,title,description,target_date,status,shared_with_hr,shared_company_id,shared_at "
                "FROM user_learning_goals WHERE username=%s AND shared_with_hr=1 AND shared_company_id=%s "
                "ORDER BY (status='aktiv') DESC, shared_at DESC", (username, company_id))
    out["shared_by_employee"] = [_own(r, company_id) for r in cur.fetchall()]
    cur.execute("SELECT id,goal_title,goal_description,target_date,status,shared_with_employee,shared_at "
                "FROM employee_goals WHERE company_id=%s AND employee_id=%s "
                "ORDER BY (target_date IS NULL), target_date, id", (company_id, user_id))
    for row in cur.fetchall():
        goal = _hr(row)
        out["shared_with_employee" if goal["shared"] else "hr_only"].append(goal)
    return out


def share_own_goal(cur, company_id, user_id, goal_id, shared):
    """The owner shares one of their goals with their company's HR, or takes it back.

    Returns the goal title, or None when the goal is not theirs. HR is told the
    first time a goal becomes shared; unsharing is silent and immediate, also for
    the goal's development links in the talent workspace."""
    _ready()
    username = _username(cur, company_id, user_id)
    if not username:
        return None
    cur.execute("SELECT id,title,shared_with_hr,shared_company_id FROM user_learning_goals WHERE id=%s AND username=%s",
                (goal_id, username))
    goal = cur.fetchone()
    if not goal:
        return None
    was = bool(goal["shared_with_hr"]) and goal["shared_company_id"] == company_id
    if shared:
        cur.execute("UPDATE user_learning_goals SET shared_with_hr=1, shared_company_id=%s, shared_at=CURRENT_TIMESTAMP "
                    "WHERE id=%s AND username=%s", (company_id, goal_id, username))
    else:
        cur.execute("UPDATE user_learning_goals SET shared_with_hr=0, shared_company_id=NULL, shared_at=NULL "
                    "WHERE id=%s AND username=%s", (goal_id, username))
    _mirror_links(cur, company_id, user_id, goal_id, shared)
    if shared and not was:
        from notification_service import HR_ROLES, notify_roles
        from person_names import display_name
        notify_roles(cur, company_id, HR_ROLES, title="Udviklingsmål delt med HR",
                     message='%s har delt udviklingsmålet "%s".' % (
                         display_name(cur, company_id, user_id=user_id, default="En kollega"), goal["title"]),
                     action_url="/hr/employee/%s/goals" % int(user_id), actor_user_id=user_id,
                     dedupe_key="goal-shared:%s" % goal_id)
    if bool(shared) != was:
        try:
            from security_audit import audit
            audit("goal.share" if shared else "goal.unshare", "learning_goal", goal_id,
                  "Eget mål %s %s HR" % (goal_id, "delt med" if shared else "trukket tilbage fra"),
                  company_id=company_id, user_id=user_id)
        except Exception:
            logger.debug("goal share audit skipped", exc_info=True)
    return goal["title"]


def _mirror_links(cur, company_id, user_id, goal_id, shared):
    try:
        cur.execute("UPDATE talent_goal_links SET shared=%s WHERE company_id=%s AND user_id=%s AND goal_id=%s",
                    (1 if shared else 0, company_id, user_id, goal_id))
    except Exception as e:  # talent tables are created lazily; no links, nothing to mirror
        from transaction_errors import propagate_transaction_abort
        propagate_transaction_abort(e)
        logger.debug("goal link mirror skipped: %s", e)


def own_goal_is_shared(cur, company_id, username, goal_id):
    _ready()
    cur.execute("SELECT 1 AS x FROM user_learning_goals WHERE id=%s AND username=%s AND shared_with_hr=1 AND shared_company_id=%s",
                (goal_id, username, company_id))
    return bool(cur.fetchone())


def hr_goal_employee(cur, company_id, goal_id):
    """Whose goal an HR-written goal is (user id), or None when it is not this company's. HR-side only."""
    cur.execute("SELECT employee_id FROM employee_goals WHERE id=%s AND company_id=%s", (goal_id, company_id))
    row = cur.fetchone()
    return row["employee_id"] if row else None


def set_hr_goal_status(cur, company_id, goal_id, status):
    """HR moves a goal it wrote between active, paused and done. Returns the employee's user id or None."""
    if status not in HR_STATUS_VALUES:
        raise ValueError("Ukendt status.")
    employee_id = hr_goal_employee(cur, company_id, goal_id)
    if employee_id is None:
        return None
    cur.execute("UPDATE employee_goals SET status=%s WHERE id=%s AND company_id=%s",
                (HR_STATUS_VALUES[status], goal_id, company_id))
    if status == "done":
        cur.execute("UPDATE employee_goals SET progress=100 WHERE id=%s AND company_id=%s", (goal_id, company_id))
    return employee_id
