"""skill_history.py — the skill-uplift capture pipeline (plan #19).

Problem this solves (the one genuine net-new capability)
--------------------------------------------------------
The whole value story is "training closes skill gaps", yet NOTHING measured
whether a completed course actually moved ``employee_skills_matrix.current_level``
toward ``target_level``. ``current_level`` is overwritten on change
(``updated_at ON UPDATE`` — enterprise_tables.py), so there is no before/after
trajectory to measure gap closure from; ROI was spend+completion only. No
pre/post assessment existed and ``performance_rating`` has zero write path
(verified in the plan's risk register), so the capture pipeline is built here
from scratch.

What this module is
-------------------
The single, minimal WRITE side of the loop: every time an employee's actual
skill level changes, append one immutable row to ``employee_skill_history``
(``employee, skill, level, previous_level, source, order_id, captured_at``).
That append-only trail is what gives gap closure a time dimension. Two capture
paths feed it today:

  * ``source='assign'``      — a manager (re)rates a skill via the HR UI /
    advisor (``hr_dashboard.assign_employee_skill``).
  * ``source='post_course'`` — a manager CONFIRMS a post-training level for an
    employee+skill (``hr_dashboard.confirm_skill_uplift``), the genuinely new
    capture path that ties a measured lift to the ``course_orders`` row that
    drove it (``order_id``).

Design constraints (mirrors compliance_service / deadline_service)
------------------------------------------------------------------
  * COMPANY-SCOPED, self-contained SQL — one bounded INSERT, no cross-company
    leakage, no FKs assumed.
  * FULLY GUARDED — ``record_snapshot`` NEVER raises into its caller. The skill
    write itself (the user's real action) must always succeed even if the
    history append fails; the worst case is one missing trajectory point.
  * ONLY records a row when the level actually CHANGED (or there is no prior
    snapshot), so re-saving the same level does not inflate the trail.
  * The aggregate, k-anon-safe READ side (company-level trend + uplift-per-kr)
    lives in ``insights_engine`` next to the other analytics; this module never
    emits people-level data.
"""

import logging

logger = logging.getLogger(__name__)

# Recognised capture sources. Unknown sources are coerced to 'assign' so a
# typo in a caller can never write an unbounded VARCHAR.
VALID_SOURCES = ('assign', 'post_course', 'rollup', 'import')
_DEFAULT_SOURCE = 'assign'


def _coerce_level(value):
    """Coerce a level to a 0..5 int, or None if it is genuinely absent."""
    if value is None:
        return None
    try:
        lvl = int(value)
    except (TypeError, ValueError):
        return None
    if lvl < 0:
        return 0
    if lvl > 5:
        return 5
    return lvl


def record_snapshot(cur, company_id, employee_id, skill_name, level,
                    previous_level=None, source=_DEFAULT_SOURCE, order_id=None):
    """Append one ``employee_skill_history`` row for a skill-level change.

    Uses the caller's OPEN cursor so the snapshot is written in the SAME
    transaction as the ``employee_skills_matrix`` write that produced it — the
    caller commits. This is deliberate: the trajectory point and the level it
    describes must be atomic. The caller is responsible for the surrounding
    transaction / commit; this helper only issues the INSERT.

    Idempotency / noise control: when ``previous_level`` is supplied and equals
    the new ``level``, NO row is written (re-saving the same level is not a
    trajectory point). When ``previous_level`` is None the caller is telling us
    it does not know (or this is the first observation) — the row is written so
    the very first level for an employee+skill is always captured.

    Fully guarded: returns True if a row was written, False otherwise (including
    on any DB error). NEVER raises — a failed history append must not break the
    real skill write.
    """
    if cur is None or company_id is None or employee_id is None:
        return False
    name = (skill_name or '').strip()
    if not name:
        return False
    name = name[:100]

    new_level = _coerce_level(level)
    if new_level is None:
        return False
    prev = _coerce_level(previous_level)

    # No-op guard: a re-save of the identical level is not a trajectory point.
    if prev is not None and prev == new_level:
        return False

    src = source if source in VALID_SOURCES else _DEFAULT_SOURCE

    oid = None
    if order_id is not None:
        try:
            oid = int(order_id)
        except (TypeError, ValueError):
            oid = None

    try:
        cur.execute(
            """
            INSERT INTO employee_skill_history
                (employee_id, company_id, skill_name, level, previous_level, source, order_id)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            """,
            (int(employee_id), int(company_id), name, new_level, prev, src, oid),
        )
        return True
    except Exception as e:
        logger.warning(
            "skill_history: record_snapshot failed (company=%s employee=%s skill=%s): %s",
            company_id, employee_id, name, e)
        return False


def current_level_for(cur, company_id, employee_id, skill_name):
    """Return the employee's current stored skill level (int) or None.

    Read helper so a write path can compute ``previous_level`` before it
    overwrites ``employee_skills_matrix.current_level``. Company-scoped and
    fully guarded (returns None on any error / no row).
    """
    if cur is None or company_id is None or employee_id is None:
        return None
    name = (skill_name or '').strip()
    if not name:
        return None
    try:
        cur.execute(
            """
            SELECT current_level FROM employee_skills_matrix
            WHERE company_id = %s AND employee_id = %s AND skill_name = %s
            LIMIT 1
            """,
            (int(company_id), int(employee_id), name[:100]),
        )
        row = cur.fetchone()
    except Exception as e:
        logger.warning("skill_history: current_level_for failed: %s", e)
        return None
    if not row:
        return None
    val = row.get('current_level') if isinstance(row, dict) else row[0]
    return _coerce_level(val)
