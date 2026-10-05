"""Compliance -> action (N-4.3): "Tildel påkrævet kursus".

Orders the course a compliance requirement names for every employee it applies to
who has neither completed nor already requested it. Orders are created through
``order_service.create_order`` with ``assign_to`` so they follow the ONE lifecycle
(pending_approval, budget checks, notifications) and are marked "Tildelt af HR".
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

OPEN_OR_DONE = ("pending_approval", "approved", "booked", "completed", "pending", "confirmed", "processing")


def applicable_employees(cur, company_id, dept, role):
    sql = ("SELECT cu.user_id, COALESCE(u.username, cu.username) AS username, "
           "COALESCE(cu.full_name, u.username) AS name, COALESCE(cu.email, u.email) AS email, cu.department "
           "FROM company_users cu LEFT JOIN users u ON u.id = cu.user_id "
           "WHERE cu.company_id = %s AND cu.status = 'active'")
    params = [company_id]
    if dept:
        sql += " AND cu.department = %s"
        params.append(dept)
    if role:
        sql += " AND cu.role = %s"
        params.append(role)
    cur.execute(sql, tuple(params))
    return list(cur.fetchall() or [])


def has_open_or_done(cur, user_id, company_id, handle, recurrence_months=0):
    """An expired completion must not suppress a renewal; an open renewal does."""
    import datetime
    cur.execute("SELECT status, completion_date FROM course_orders WHERE user_id = %s AND company_id = %s AND product_handle = %s AND status IN ('pending_approval','approved','booked','completed','pending','confirmed','processing')", (user_id,company_id,handle))
    for row in cur.fetchall() or []:
        if row['status'] != 'completed' or not recurrence_months:
            return True
        completed = row.get('completion_date')
        if isinstance(completed,str):
            try:
                completed = datetime.datetime.fromisoformat(completed)
            except ValueError:
                completed = None
        if completed is None:
            # Legacy undated records retain their old interpretation; new verified
            # completions always have a timestamp.
            return True
        if isinstance(completed,datetime.datetime):
            completed = completed.date()
        if completed + datetime.timedelta(days=int(int(recurrence_months)*30.44)) > datetime.date.today() + datetime.timedelta(days=60):
            return True
    return False


def assign_required_course(cur, ctx, company_id, requirement_id, *, create=None):
    """Returns {created, skipped, failed, message}. ``create`` is injectable for tests."""
    import enrollment_service
    cur.execute("SELECT id, title, required_course_handle, applies_to_department, applies_to_role, recurrence_months "
                "FROM compliance_requirements WHERE id = %s AND company_id = %s", (requirement_id, company_id))
    req = cur.fetchone()
    if not req:
        return {"created": 0, "skipped": 0, "failed": 0, "message": "Kravet blev ikke fundet."}
    handle = (req.get("required_course_handle") or "").strip()
    if not handle:
        return {"created": 0, "skipped": 0, "failed": 0,
                "message": "Kravet har intet påkrævet kursus. Redigér kravet og angiv kursets handle først."}
    try:
        product = enrollment_service.get_course(handle, company_id)
    except Exception:
        product = None
    if not product:
        return {"created": 0, "skipped": 0, "failed": 0,
                "message": "Kurset ‘%s’ findes ikke i kataloget. Ret kursets handle på kravet." % handle}
    price = product.get("price_min") or 0
    created = skipped = failed = 0
    make = create or enrollment_service.create_order
    for emp in applicable_employees(cur, company_id, req.get("applies_to_department"), req.get("applies_to_role")):
        if has_open_or_done(cur, emp["user_id"], company_id, handle, req.get("recurrence_months") or 0):
            skipped += 1
            continue
        res = make(ctx, product_handle=handle, product_title=product.get("title") or handle, price=price,
                   user_email=emp.get("email") or "", user_name=emp.get("name") or "",
                   extra={"assign_to": emp, "department": emp.get("department"),
                          "compliance_requirement_id": requirement_id, "recommended_by_tool": "compliance:%s" % requirement_id})
        if res.get("success") and not res.get("duplicate"):
            created += 1
        elif res.get("success"):
            skipped += 1
        else:
            failed += 1
    msg = "%d bestillinger oprettet til godkendelse" % created
    if skipped:
        msg += ", %d havde allerede kurset" % skipped
    if failed:
        msg += ", %d kunne ikke oprettes" % failed
    return {"created": created, "skipped": skipped, "failed": failed, "message": msg + "."}
