"""
Per-goal sharing of HR-written goals (S-4.4, decided 2026-09-30).

Each goal HR writes for an employee (``employee_goals``) carries a
**"Del med medarbejder"** flag, OFF by default:

  * a *shared* goal is visible to the learner ("Mål fra din leder" on /mine-maal)
    and is part of the learner AI's context;
  * an *unshared* goal is HR-only: it never reaches the learner UI, the learner
    AI, the learner's own data export, or any other learner-facing query.

The company-wide switch ``company_settings.ai_learner_hr_goals`` (default ON)
sits above the per-goal flag: when a company turns it off, even shared goals stay
out of the learner AI's context. Sharing/unsharing is audit-logged.

Every learner-facing read MUST go through ``shared_goal_filter`` /
``list_shared_goals_for_learner`` so the filter lives in exactly one place.
"""

import logging

logger = logging.getLogger(__name__)

# The single predicate every learner-facing query appends.
SHARED_PREDICATE = "shared_with_employee = 1"

_SCHEMA_READY = False


def ensure_schema(conn):
    """Idempotent migration: sharing columns + the company-level AI switch."""
    global _SCHEMA_READY
    if _SCHEMA_READY:
        return True
    cur = conn.cursor()
    try:
        def has(table, column):
            cur.execute("SHOW COLUMNS FROM `%s` LIKE %%s" % table, (column,))
            return cur.fetchone() is not None

        if not has("employee_goals", "shared_with_employee"):
            cur.execute("ALTER TABLE employee_goals ADD COLUMN shared_with_employee TINYINT(1) NOT NULL DEFAULT 0")
        if not has("employee_goals", "shared_at"):
            cur.execute("ALTER TABLE employee_goals ADD COLUMN shared_at DATETIME NULL")
        if not has("employee_goals", "shared_by"):
            cur.execute("ALTER TABLE employee_goals ADD COLUMN shared_by INT NULL")
        if not has("employee_goals", "share_note"):
            cur.execute("ALTER TABLE employee_goals ADD COLUMN share_note VARCHAR(500) NULL")
        if not has("company_settings", "ai_learner_hr_goals"):
            cur.execute("ALTER TABLE company_settings ADD COLUMN ai_learner_hr_goals TINYINT(1) NOT NULL DEFAULT 1")
        conn.commit()
        _SCHEMA_READY = True
        return True
    except Exception as exc:
        logger.warning("goal_sharing.ensure_schema failed: %s", exc)
        try:
            conn.rollback()
        except Exception:
            pass
        return False
    finally:
        cur.close()


def _rows(cur):
    return cur.fetchall() or []


def company_shares_with_ai(conn, company_id):
    """Company-level switch for feeding SHARED HR goals to the learner AI.
    Default ON; a missing row / column / any error also means ON."""
    if company_id is None:
        return True
    cur = conn.cursor()
    try:
        cur.execute("SELECT ai_learner_hr_goals FROM company_settings WHERE company_id = %s LIMIT 1", (company_id,))
        row = cur.fetchone()
        if not row:
            return True
        val = row.get("ai_learner_hr_goals") if isinstance(row, dict) else row[0]
        return True if val is None else bool(int(val))
    except Exception:
        return True
    finally:
        cur.close()


def set_company_ai_sharing(conn, company_id, enabled):
    ensure_schema(conn)
    cur = conn.cursor()
    try:
        cur.execute("UPDATE company_settings SET ai_learner_hr_goals = %s WHERE company_id = %s",
                    (1 if enabled else 0, company_id))
        if cur.rowcount == 0:
            cur.execute("INSERT INTO company_settings (company_id, ai_learner_hr_goals) VALUES (%s, %s)",
                        (company_id, 1 if enabled else 0))
        conn.commit()
    finally:
        cur.close()


def list_goals_for_hr(conn, company_id, employee_user_id):
    """HR's two sections for one employee: ``{'shared': [...], 'hr_only': [...]}``."""
    ensure_schema(conn)
    cur = conn.cursor()
    try:
        cur.execute(
            "SELECT id, goal_title, goal_description, target_date, status, progress, created_at, "
            "shared_with_employee, shared_at, shared_by, share_note FROM employee_goals "
            "WHERE employee_id = %s AND company_id = %s ORDER BY (target_date IS NULL), target_date ASC, id ASC",
            (employee_user_id, company_id))
        rows = _rows(cur)
    finally:
        cur.close()
    out = {"shared": [], "hr_only": []}
    for r in rows:
        shared = bool((r.get("shared_with_employee") if isinstance(r, dict) else r[7]) or 0)
        out["shared" if shared else "hr_only"].append(r)
    return out


def create_goal(conn, *, company_id, employee_user_id, title, description="", target_date=None,
                shared=False, actor_user_id=None, note=None):
    """New HR goal. Unshared unless the caller explicitly says otherwise."""
    title = (title or "").strip()
    if not title:
        raise ValueError("title required")
    ensure_schema(conn)
    cur = conn.cursor()
    try:
        cur.execute(
            "INSERT INTO employee_goals (employee_id, company_id, goal_title, goal_description, target_date, status, "
            "shared_with_employee, shared_at, shared_by, share_note) "
            "VALUES (%s, %s, %s, %s, %s, 'active', %s, " + ("NOW()" if shared else "NULL") + ", %s, %s)",
            (employee_user_id, company_id, title[:255], (description or "")[:2000], target_date or None,
             1 if shared else 0, actor_user_id if shared else None, (note or "")[:500] or None if shared else None))
        goal_id = cur.lastrowid
        conn.commit()
        return goal_id
    finally:
        cur.close()


def set_shared(conn, *, goal_id, company_id, shared, actor_user_id, note=None):
    """Move one goal between "Delt med medarbejderen" and "Kun synligt for HR".
    Company-scoped. Returns the goal row (with employee_id) or None if not found."""
    ensure_schema(conn)
    cur = conn.cursor()
    try:
        cur.execute("SELECT id, employee_id, goal_title, shared_with_employee FROM employee_goals "
                    "WHERE id = %s AND company_id = %s", (goal_id, company_id))
        row = cur.fetchone()
        if not row:
            return None
        row = row if isinstance(row, dict) else dict(zip(("id", "employee_id", "goal_title", "shared_with_employee"), row))
        if bool(row.get("shared_with_employee")) == bool(shared):
            return row  # already there; nothing to audit
        if shared:
            cur.execute("UPDATE employee_goals SET shared_with_employee = 1, shared_at = NOW(), shared_by = %s, "
                        "share_note = %s WHERE id = %s AND company_id = %s",
                        (actor_user_id, (note or "")[:500] or None, goal_id, company_id))
        else:
            cur.execute("UPDATE employee_goals SET shared_with_employee = 0, shared_at = NULL, shared_by = NULL, "
                        "share_note = NULL WHERE id = %s AND company_id = %s", (goal_id, company_id))
        conn.commit()
    finally:
        cur.close()
    try:
        from security_audit import audit
        audit("hr.goal.share" if shared else "hr.goal.unshare", "employee_goal", goal_id,
              "Mål %s %s medarbejder %s" % (goal_id, "delt med" if shared else "gjort privat for", row.get("employee_id")),
              company_id=company_id, user_id=actor_user_id)
    except Exception:
        pass
    row["shared_with_employee"] = 1 if shared else 0
    return row


def list_shared_goals_for_learner(conn, user_id, company_id, limit=20):
    """What the learner may see: SHARED goals only ("Mål fra din leder")."""
    ensure_schema(conn)
    cur = conn.cursor()
    try:
        cur.execute(
            "SELECT id, goal_title, goal_description, target_date, status, progress, shared_at, share_note "
            "FROM employee_goals WHERE employee_id = %s AND company_id = %s AND " + SHARED_PREDICATE +
            " ORDER BY (target_date IS NULL), target_date ASC, id ASC LIMIT %s",
            (user_id, company_id, int(limit)))
        return _rows(cur)
    finally:
        cur.close()
