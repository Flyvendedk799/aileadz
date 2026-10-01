"""HR learning paths that create real work (N-4.4).

* A path has ordered steps. A *catalog* step names a course (``course_handle``);
  assigning the path to an employee creates a normal ``pending_approval`` order for
  that course through ``order_service.create_order`` (so budget and approval rules
  apply - before, paths bypassed both). *Info* steps are plain guidance.
* The employee sees the assignment on ``/min-laering`` ("Tildelt af HR", due date).
* Every save of the steps writes a snapshot to ``learning_path_versions`` and
  bumps ``learning_paths.version``; nothing is overwritten silently.
Callers commit.
"""

from __future__ import annotations

import json
import logging

logger = logging.getLogger(__name__)


def get_steps(cur, company_id, path_id):
    cur.execute(
        "SELECT id, position, step_type, course_handle, title FROM learning_path_steps "
        "WHERE company_id = %s AND path_id = %s ORDER BY position, id", (company_id, path_id))
    return list(cur.fetchall() or [])


def save_steps(cur, company_id, path_id, steps, *, actor_user_id=None, note=None):
    """Replace a path's steps and record the PREVIOUS state as a version.

    ``steps`` = [{"course_handle"?, "title"?, "step_type"?}]. Catalog steps whose
    handle is unknown are rejected with a clear message. Returns {success, version}."""
    cur.execute("SELECT id, version FROM learning_paths WHERE id = %s AND company_id = %s", (path_id, company_id))
    path = cur.fetchone()
    if not path:
        return {"success": False, "message": "Læringsforløbet blev ikke fundet."}
    clean = []
    for i, s in enumerate(steps or []):
        handle = (s.get("course_handle") or "").strip()
        title = (s.get("title") or "").strip()
        if handle:
            try:
                import catalog_service
                p = catalog_service.get_product(handle)
            except Exception:
                p = None
            if not p:
                return {"success": False, "message": "Kurset ‘%s’ findes ikke i kataloget." % handle}
            clean.append({"position": i + 1, "step_type": "catalog", "course_handle": handle,
                          "title": title or p.get("title") or handle})
        elif title:
            clean.append({"position": i + 1, "step_type": "info", "course_handle": None, "title": title[:255]})
    before = get_steps(cur, company_id, path_id)
    version = int(path.get("version") or 1)
    if before:
        cur.execute(
            "INSERT INTO learning_path_versions (path_id, company_id, version, steps_json, saved_by, note) "
            "VALUES (%s, %s, %s, %s, %s, %s)",
            (path_id, company_id, version, json.dumps(before, default=str, ensure_ascii=False),
             actor_user_id, (note or None)))
    cur.execute("DELETE FROM learning_path_steps WHERE company_id = %s AND path_id = %s", (company_id, path_id))
    for s in clean:
        cur.execute(
            "INSERT INTO learning_path_steps (path_id, company_id, position, step_type, course_handle, title) "
            "VALUES (%s, %s, %s, %s, %s, %s)",
            (path_id, company_id, s["position"], s["step_type"], s["course_handle"], s["title"]))
    cur.execute("UPDATE learning_paths SET version = %s WHERE id = %s AND company_id = %s",
                (version + 1, path_id, company_id))
    return {"success": True, "version": version + 1, "steps": len(clean)}


def assign_path(cur, ctx, company_id, path_id, user_ids, *, due_date=None, sender_id=None, create=None):
    """Enrol employees in a path and order its paid steps for each of them.

    Returns {assigned, skipped, orders, order_failures, path_name}. Employees must
    belong to the company; already enrolled ones are skipped. ``create`` is injectable."""
    import order_service
    from notification_service import insert_company_notification
    from compliance_assign import has_open_or_done
    cur.execute("SELECT id, path_name FROM learning_paths WHERE id = %s AND company_id = %s", (path_id, company_id))
    path = cur.fetchone()
    if not path:
        return {"assigned": 0, "skipped": 0, "orders": 0, "order_failures": 0, "path_name": None,
                "message": "Læringsforløbet blev ikke fundet."}
    steps = [s for s in get_steps(cur, company_id, path_id) if s.get("step_type") == "catalog" and s.get("course_handle")]
    make = create or order_service.create_order
    out = {"assigned": 0, "skipped": 0, "orders": 0, "order_failures": 0, "path_name": path["path_name"]}
    for uid in user_ids:
        cur.execute("SELECT user_id, COALESCE(u.username, cu.username) AS username, COALESCE(cu.full_name, u.username) AS name, "
                    "COALESCE(cu.email, u.email) AS email, cu.department FROM company_users cu "
                    "LEFT JOIN users u ON u.id = cu.user_id "
                    "WHERE cu.company_id = %s AND cu.user_id = %s AND cu.status = 'active'", (company_id, uid))
        emp = cur.fetchone()
        if not emp:
            out["skipped"] += 1
            continue
        cur.execute("SELECT id FROM employee_learning_progress WHERE user_id = %s AND company_id = %s "
                    "AND learning_path_id = %s", (uid, company_id, path_id))
        if cur.fetchone():
            out["skipped"] += 1
            continue
        cur.execute(
            "INSERT INTO employee_learning_progress (user_id, company_id, learning_path_id, content_type, content_name, "
            "status, progress_percentage, due_date, started_at) VALUES (%s, %s, %s, 'learning_path', %s, 'not_started', 0, %s, CURRENT_TIMESTAMP)",
            (uid, company_id, path_id, path["path_name"], due_date))
        out["assigned"] += 1
        for s in steps:
            if has_open_or_done(cur, uid, company_id, s["course_handle"]):
                continue
            try:
                import catalog_service
                product = catalog_service.get_product(s["course_handle"]) or {}
            except Exception:
                product = {}
            res = make(ctx, product_handle=s["course_handle"], product_title=s.get("title") or s["course_handle"],
                       price=product.get("price_min") or 0,
                       extra={"assign_to": emp, "department": emp.get("department"),
                              "completion_deadline": due_date, "recommended_by_tool": "path:%s" % path_id})
            if res.get("success") and not res.get("duplicate"):
                out["orders"] += 1
            elif not res.get("success"):
                out["order_failures"] += 1
        insert_company_notification(
            cur, company_id, recipient_user_id=uid, sender_user_id=sender_id,
            title="Nyt læringsforløb tildelt",
            message="Du er blevet tildelt læringsforløbet ‘%s’.%s" % (
                path["path_name"], (" Frist: %s." % due_date) if due_date else ""),
            action_url="/min-laering", kind="assignment", dedupe_key=None)
    return out


def assignments_for_learner(cur, user_id, company_id):
    """"Tildelt af HR": the learner's assigned paths with due date and progress."""
    if not user_id or not company_id:
        return []
    try:
        cur.execute(
            """SELECT elp.id, elp.learning_path_id, lp.path_name, elp.due_date, elp.status,
                      elp.progress_percentage
               FROM employee_learning_progress elp
               JOIN learning_paths lp ON lp.id = elp.learning_path_id
               WHERE elp.user_id = %s AND elp.company_id = %s AND elp.learning_path_id IS NOT NULL
               ORDER BY (elp.status = 'completed') ASC, (elp.due_date IS NULL) ASC, elp.due_date ASC LIMIT 10""",
            (user_id, company_id))
        rows = list(cur.fetchall() or [])
    except Exception as e:
        logger.warning("learning_path_service: learner assignments failed: %s", e)
        return []
    for r in rows:
        r["steps"] = [s for s in get_steps(cur, company_id, r["learning_path_id"])]
    return rows
