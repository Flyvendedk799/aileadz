"""Department rules (N-3.5): one budget, one rename.

* ``learning_budget_per_employee`` on ``company_departments`` used to be saved and
  never used.  It is now the *input* that feeds ``department_budgets`` (the one
  table orders are charged against): annual_budget = per-employee x active
  employees, for the current fiscal year, without touching money already spent.
* Renaming a department cascades to everything that stores the name as text:
  employees, budgets, approval policies, skill targets, compliance requirements
  and open orders.  Callers commit.
"""

from __future__ import annotations

import datetime
import logging

logger = logging.getLogger(__name__)

# (table, column) pairs that carry the department NAME.
RENAME_TARGETS = (
    ("company_users", "department"),
    ("department_budgets", "department"),
    ("company_approval_policies", "department"),
    ("company_skill_targets", "department"),
    ("compliance_requirements", "applies_to_department"),
    ("course_orders", "department"),
)


def rename_department(cur, company_id, old_name, new_name):
    """Cascade a rename. Returns {table: rows_changed}. Table errors are skipped
    (older schemas) but logged."""
    changed = {}
    if not old_name or not new_name or old_name == new_name:
        return changed
    for table, column in RENAME_TARGETS:
        try:
            cur.execute(
                "UPDATE `%s` SET `%s` = %%s WHERE company_id = %%s AND `%s` = %%s" % (table, column, column),
                (new_name, company_id, old_name),
            )
            changed[table] = getattr(cur, "rowcount", 0) or 0
        except Exception as e:
            logger.warning("department rename: %s skipped (%s)", table, e)
    return changed


def sync_budget_from_per_employee(cur, company_id, department, per_employee, year=None):
    """Turn the per-employee learning budget into the department's annual budget.

    Only acts when a per-employee amount > 0 is set. ``spent`` is preserved."""
    try:
        per = float(per_employee or 0)
    except (TypeError, ValueError):
        return None
    if per <= 0 or not department:
        return None
    year = year or datetime.date.today().year
    cur.execute(
        "SELECT COUNT(*) AS n FROM company_users WHERE company_id = %s AND department = %s AND status = 'active'",
        (company_id, department),
    )
    row = cur.fetchone() or {}
    heads = int((row.get("n") if isinstance(row, dict) else row[0]) or 0) or 1
    annual = round(per * heads, 2)
    cur.execute(
        "SELECT id FROM department_budgets WHERE company_id = %s AND department = %s AND fiscal_year = %s",
        (company_id, department, year),
    )
    existing = cur.fetchone()
    if existing:
        cur.execute("UPDATE department_budgets SET annual_budget = %s WHERE id = %s",
                    (annual, existing["id"] if isinstance(existing, dict) else existing[0]))
    else:
        cur.execute(
            "INSERT INTO department_budgets (company_id, department, annual_budget, spent, fiscal_year) "
            "VALUES (%s, %s, %s, 0, %s)", (company_id, department, annual, year))
    return annual
