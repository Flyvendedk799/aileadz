"""HR learning paths that create real work (N-4.4).

* A path has ordered steps. A *catalog* step names a course (``course_handle``);
  assigning the path to an employee orders that course through
  ``order_service.create_order`` as an assigned course (approved when a manager assigns,
  the budget still charged and checked). *Info* steps are plain guidance.
* ``ordering_mode`` (frozen per assignment): ``all_at_once`` orders every course at
  assignment; ``sequential`` orders the first and ``refresh_assignment`` orders each next
  course when the steps before it are completed or skipped. A failed step waits for HR
  (``hr_retry_step`` / ``hr_skip_step``).
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
        "WHERE company_id = %s AND path_id = %s ORDER BY position, id",
        (company_id, path_id),
    )
    return list(cur.fetchall() or [])


def _money(value):
    try:
        from dashboard import dkmoney

        return dkmoney(value) if value not in (None, "") else ""
    except Exception:
        return ""


def _next_session(variants):
    """"3. december 2026 · Aarhus" for the earliest upcoming session, else ''."""
    import datetime

    from calendar_service import parse_danish_date
    import order_timing

    today = datetime.date.today()
    upcoming = []
    for v in variants or []:
        day = parse_danish_date(v.get("date")) if isinstance(v, dict) else None
        if day and day >= today:
            upcoming.append((day, v))
    if not upcoming:
        return ""
    day, v = min(upcoming, key=lambda item: item[0])
    where = v.get("city") or v.get("location") or ""
    return order_timing.format_date(day, style="long") + (" · %s" % where if where else "")


def describe_course(handle, company_id):
    """What HR needs to recognise a course in the step editor, or None when it no longer
    exists: ``{handle, title, vendor, price, price_label, next_session, format, internal}``."""
    import enrollment_service

    try:
        product = enrollment_service.get_course(handle, company_id)
    except Exception:
        product = None
    if not product:
        return None
    internal = str(handle).startswith("internal:")
    price = product.get("price_min")
    return {
        "handle": handle,
        "title": product.get("title") or handle,
        "vendor": "Internt kursus" if internal else (product.get("vendor") or ""),
        "price": price,
        "price_label": _money(price),
        "next_session": _next_session(product.get("variants")),
        "format": product.get("format") or "",
        "internal": internal,
    }


def search_catalog(cur, company_id, query, limit=10):
    """Courses HR can put in a path: the shared catalogue plus the company's own internal
    courses (labelled). Company-scoped; a query shorter than two characters lists nothing."""
    query = (query or "").strip()
    if len(query) < 2 or not company_id:
        return []
    import catalog_service

    limit = max(1, min(int(limit or 10), 25))
    results = []
    try:
        like = "%" + query.lower() + "%"
        cur.execute(
            "SELECT id FROM company_courses WHERE company_id = %s AND is_active = 1 AND "
            "(LOWER(title) LIKE %s OR LOWER(COALESCE(skill_tags, '')) LIKE %s) ORDER BY title LIMIT %s",
            (company_id, like, like, limit),
        )
        for row in list(cur.fetchall() or []):
            info = describe_course("internal:%s" % row["id"], company_id)
            if info:
                results.append(info)
    except Exception as e:
        logger.debug("learning_path_service: internal course search skipped: %s", e)
    try:
        found = catalog_service.search_products({"q": query}, per_page=limit, company_id=company_id)
    except Exception as e:
        logger.warning("learning_path_service: catalogue search failed: %s", e)
        found = {"products": []}
    for product in found.get("products") or []:
        results.append({
            "handle": product["handle"],
            "title": product["title"],
            "vendor": product.get("vendor") or "",
            "price": product.get("price_min"),
            "price_label": _money(product.get("price_min")),
            "next_session": _next_session(product.get("variants")),
            "format": product.get("format") or "",
            "internal": False,
        })
    return results[:limit]


def steps_from_form(getlist):
    """Structured editor fields (``step_type[]``, ``course_handle[]``, ``title[]``) as step dicts.

    ``getlist`` is ``request.form.getlist``. Rows stay aligned by index; a row is whatever the
    editor posted, validation happens in ``save_steps``."""
    types = getlist("step_type[]")
    handles = getlist("course_handle[]")
    titles = getlist("title[]")
    rows = []
    for i, step_type in enumerate(types):
        rows.append({
            "step_type": "catalog" if step_type == "catalog" else "info",
            "course_handle": (handles[i] if i < len(handles) else "").strip(),
            "title": (titles[i] if i < len(titles) else "").strip(),
        })
    return rows


def steps_from_text(text):
    """The no-JS fallback: one step per line, ``handle | label`` or ``# guidance``."""
    steps = []
    for line in (text or "").splitlines():
        line = line.strip()
        if not line:
            continue
        if line.startswith("#"):
            steps.append({"step_type": "info", "title": line.lstrip("# ").strip()})
        else:
            handle, _, title = line.partition("|")
            steps.append({"step_type": "catalog", "course_handle": handle.strip(), "title": title.strip()})
    return steps


def validate_steps(company_id, steps):
    """Clean step dicts plus ``errors`` = {row index: Danish message}. Every bad row is reported."""
    clean, errors = [], {}
    for i, s in enumerate(steps or []):
        handle = (s.get("course_handle") or "").strip()
        title = (s.get("title") or "").strip()
        kind = s.get("step_type") or ("catalog" if handle else "info")
        if kind == "catalog" or handle:
            if not handle:
                errors[i] = "Vælg et kursus fra kataloget, eller fjern trinnet."
                continue
            info = describe_course(handle, company_id)
            if not info:
                errors[i] = "Kurset ‘%s’ findes ikke i kataloget." % handle
                continue
            clean.append({"index": i, "step_type": "catalog", "course_handle": handle,
                          "title": (title or info["title"])[:255]})
        elif title:
            clean.append({"index": i, "step_type": "info", "course_handle": None, "title": title[:255]})
    return clean, errors


def save_steps(cur, company_id, path_id, steps, *, actor_user_id=None, note=None, ordering_mode=None):
    """Replace a path's steps and record the PREVIOUS state as a version.

    ``steps`` = [{"course_handle"?, "title"?, "step_type"?}]. A catalog step whose handle is
    missing or unknown is rejected, naming the row: ``{success: False, message, errors}`` with
    ``errors`` = {row index: message} for EVERY bad row, so the editor can show them all and
    keep what was entered. ``ordering_mode`` (``all_at_once`` / ``sequential``) is saved with the steps; running
    assignments keep the mode they were assigned with. Returns {success, version, steps} otherwise."""
    cur.execute("SELECT id, version FROM learning_paths WHERE id = %s AND company_id = %s FOR UPDATE", (path_id, company_id))
    path = cur.fetchone()
    if not path:
        return {"success": False, "message": "Læringsforløbet blev ikke fundet."}
    clean, errors = validate_steps(company_id, steps)
    if errors:
        return {"success": False, "errors": errors, "message": errors[min(errors)]}
    for position, step in enumerate(clean, start=1):
        step["position"] = position
    before = get_steps(cur, company_id, path_id)
    version = int(path.get("version") or 1)
    if before:
        cur.execute(
            "INSERT INTO learning_path_versions (path_id, company_id, version, steps_json, saved_by, note) VALUES (%s, %s, %s, %s, %s, %s)",
            (path_id, company_id, version, json.dumps(before, default=str, ensure_ascii=False), actor_user_id, (note or None)),
        )
    cur.execute("DELETE FROM learning_path_steps WHERE company_id = %s AND path_id = %s", (company_id, path_id))
    for s in clean:
        cur.execute(
            "INSERT INTO learning_path_steps (path_id, company_id, position, step_type, course_handle, title) "
            "VALUES (%s, %s, %s, %s, %s, %s)",
            (path_id, company_id, s["position"], s["step_type"], s["course_handle"], s["title"]),
        )
    cur.execute("UPDATE learning_paths SET version = %s WHERE id = %s AND company_id = %s", (version + 1, path_id, company_id))
    if ordering_mode is not None:
        cur.execute("UPDATE learning_paths SET ordering_mode = %s WHERE id = %s AND company_id = %s",
                    (normalize_mode(ordering_mode), path_id, company_id))
    return {"success": True, "version": version + 1, "steps": len(clean)}


ORDERING_MODES = ("all_at_once", "sequential")
DEFAULT_ORDERING_MODE = "all_at_once"
_DONE_STATES = ("completed", "skipped")


def normalize_mode(value):
    return value if value in ORDERING_MODES else DEFAULT_ORDERING_MODE


def _load_employee(cur, company_id, user_id, *, lock=False):
    """The active employee row ``create_order`` expects as ``assign_to`` (None when not a member)."""
    cur.execute(
        "SELECT cu.user_id, u.username, COALESCE(cu.full_name,u.username) AS name, COALESCE(cu.email,u.email) AS email, "
        "cu.department FROM company_users cu JOIN users u ON u.id = cu.user_id "
        "WHERE cu.company_id = %s AND cu.user_id = %s AND cu.status = 'active'" + (" FOR UPDATE" if lock else ""),
        (company_id, user_id),
    )
    return cur.fetchone()


def _assigner_ctx(cur, company_id, user_id):
    """The OrderContext of the person who assigned a path, so later steps are ordered as that
    assignment (pre-approved when they are still a manager). Unknown assigner = system actor."""
    import order_service

    if user_id:
        cur.execute(
            "SELECT u.username, cu.role, cu.department FROM company_users cu JOIN users u ON u.id = cu.user_id "
            "WHERE cu.company_id = %s AND cu.user_id = %s AND cu.status = 'active'",
            (company_id, user_id),
        )
        row = cur.fetchone()
        if row:
            return order_service.OrderContext(company_id=company_id, user_id=user_id, username=row["username"],
                                              company_role=row["role"], department=row["department"], source="learning_path")
    return order_service.OrderContext.system(company_id, source="learning_path", label="Læringsforløb")


def _order_extra(progress, step, emp, path_id):
    return {
        "assign_to": emp,
        "assignment_step_id": step["id"],
        "completion_deadline": progress.get("due_date"),
        "recommended_by_tool": "path:%s" % path_id,
    }


def _notify_step_problem(cur, company_id, progress, emp, step, message):
    """A path step could not be ordered: HR hears about it and the learner can choose a session."""
    try:
        from notification_service import notify_roles, notify_user, HR_ROLES

        title = step.get("title") or "kursus"
        notify_roles(
            cur, company_id, HR_ROLES,
            title="Kursus i forløb kunne ikke bestilles",
            message="‘%s’ til %s kunne ikke bestilles: %s" % (title, emp.get("name") or emp.get("username"), message),
            kind="assignment", is_urgent=True, action_url="/hr/learning-paths",
            dedupe_key="path-step-failed:%s" % step["id"], dedupe_hours=None,
        )
        notify_user(
            cur, user_id=emp["user_id"], username=emp.get("username"), company_id=company_id,
            title="Vælg hold til ‘%s’" % title, kind="assignment",
            message="Næste kursus i dit forløb kunne ikke bestilles automatisk. %s" % message,
            action_url="/min-laering/forloeb/%s" % progress["id"],
            dedupe_key="path-step-failed-learner:%s" % step["id"], dedupe_hours=None,
        )
    except Exception as e:
        from transaction_errors import propagate_transaction_abort

        propagate_transaction_abort(e)
        logger.debug("learning_path_service: step problem notification skipped: %s", e)


def _order_frozen_step(cur, company_id, progress, step, emp, ctx, *, path_id, in_transaction, create=None):
    """Order one frozen path step as an assigned course; failures stay on the step (``last_error``)."""
    import enrollment_service
    import order_service

    extra = _order_extra(progress, step, emp, path_id)
    kwargs = {}
    events = []
    if in_transaction:
        extra["savepoint"] = True
        kwargs["deferred_events"] = events
    result = (create or enrollment_service.create_order)(
        ctx, product_handle=step["course_handle"], product_title=step.get("title") or "", price=0, extra=extra, **kwargs
    )
    if result.get("success"):
        for event in events:
            order_service._emit_event_safe(*event)
    else:
        message = result.get("message") or "Bestillingen kunne ikke oprettes."
        cur.execute("UPDATE learning_assignment_steps SET status = 'failed', last_error = %s WHERE id = %s", (message, step["id"]))
        step["status"] = "failed"
    return result


def assign_path(cur, ctx, company_id, path_id, user_ids, *, due_date=None, sender_id=None, create=None, expected_version=None):
    """Enrol employees in a path and order its paid steps for each of them.

    The path's ``ordering_mode`` is frozen on every assignment: ``all_at_once`` orders every
    catalogue step now; ``sequential`` orders only the first one and ``refresh_assignment`` orders
    each next course when the steps before it are done. Orders are assigned by the person who
    assigns (pre-approved when they are a manager; the budget is charged per ordered step).

    Returns {assigned, skipped, orders, order_failures, path_name}. Employees must
    belong to the company; already enrolled ones are skipped. ``create`` is injectable."""
    import order_service

    conn = order_service._get_connection()
    cur.execute("SELECT * FROM learning_paths WHERE id = %s AND company_id = %s FOR UPDATE", (path_id, company_id))
    path = cur.fetchone()
    if not path:
        return {
            "assigned": 0,
            "skipped": 0,
            "orders": 0,
            "order_failures": 0,
            "path_name": None,
            "message": "Læringsforløbet blev ikke fundet.",
        }
    if expected_version is not None and int(expected_version) != int(path.get("version") or 1):
        return {"assigned": 0, "orders": 0, "order_failures": 1, "message": "Forløbet er ændret. Gennemse trinene og bekræft igen."}
    mode = normalize_mode(path.get("ordering_mode"))
    assigned_by = sender_id or getattr(ctx, "user_id", None)
    steps = get_steps(cur, company_id, path_id)
    out = {"assigned": 0, "skipped": 0, "orders": 0, "order_failures": 0, "path_name": path["path_name"],
           "ordering_mode": mode, "results": []}
    if not steps:
        out["message"] = "Tilføj trin til forløbet før tildeling."
        return out
    for uid in dict.fromkeys(user_ids):
        emp = _load_employee(cur, company_id, uid, lock=True)
        if not emp:
            out["skipped"] += 1
            continue
        cur.execute(
            "SELECT id FROM employee_learning_progress WHERE user_id = %s AND company_id = %s AND learning_path_id = %s",
            (uid, company_id, path_id),
        )
        if cur.fetchone():
            out["skipped"] += 1
            continue
        cur.execute(
            "INSERT INTO employee_learning_progress (user_id,company_id,learning_path_id,content_type,content_name,status,"
            "progress_percentage,due_date,started_at,ordering_mode,assigned_by_user_id) "
            "VALUES (%s,%s,%s,'learning_path',%s,'not_started',0,%s,CURRENT_TIMESTAMP,%s,%s)",
            (uid, company_id, path_id, path["path_name"], due_date, mode, assigned_by),
        )
        progress_id = cur.lastrowid
        progress = {"id": progress_id, "due_date": due_date, "ordering_mode": mode}
        step_ids = []
        for step in steps:
            cur.execute(
                "INSERT INTO learning_assignment_steps (progress_id,company_id,user_id,path_version,position,step_type,course_handle,title) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
                (
                    progress_id,
                    company_id,
                    uid,
                    path.get("version") or 1,
                    step["position"],
                    step["step_type"],
                    step.get("course_handle"),
                    step.get("title") or "Trin",
                ),
            )
            step_ids.append((cur.lastrowid, step))
        # Persist a recoverable frozen assignment before individual orders commit.
        conn.commit()
        out["assigned"] += 1
        catalog_steps = [(step_id, step) for step_id, step in step_ids if step.get("course_handle")]
        for step_id, step in (catalog_steps[:1] if mode == "sequential" else catalog_steps):
            frozen = dict(step, id=step_id)
            result = _order_frozen_step(cur, company_id, progress, frozen, emp, ctx, path_id=path_id,
                                        in_transaction=False, create=create)
            if result.get("success"):
                out["orders"] += not result.get("duplicate")
            else:
                out["order_failures"] += 1
            out["results"].append({"user_id": uid, "step_id": step_id, **result})
        from notification_service import insert_company_notification

        insert_company_notification(
            cur,
            company_id,
            recipient_user_id=uid,
            sender_user_id=sender_id,
            title="Nyt læringsforløb tildelt",
            message="Du er blevet tildelt ‘%s’. %s" % (
                path["path_name"],
                "Kurserne bestilles ét ad gangen; det næste bestilles, når du har gennemført det forrige."
                if mode == "sequential" else "Åbn forløbet og vælg eventuelle manglende hold."),
            action_url="/min-laering/forloeb/%s" % progress_id,
            kind="assignment",
            dedupe_key=None,
            actor_user_id=sender_id,
        )
        refresh_assignment(cur, progress_id, company_id)
        conn.commit()
    return out


def _money_value(value):
    try:
        return round(float(value or 0), 2)
    except (TypeError, ValueError):
        return 0.0


def _budget_map(cur, company_id):
    """{department: {annual, spent, remaining, limited}} for this fiscal year (same table the
    approvals page and ``order_service`` read). ``limited`` mirrors create_order: only a budget
    above zero can be overspent."""
    import datetime

    out = {}
    try:
        cur.execute(
            "SELECT department, annual_budget, spent FROM department_budgets WHERE company_id = %s AND fiscal_year = %s",
            (company_id, datetime.datetime.now().year),
        )
        for row in list(cur.fetchall() or []):
            annual = _money_value(row.get("annual_budget"))
            spent = _money_value(row.get("spent"))
            out[row["department"]] = {"annual": annual, "spent": spent, "remaining": round(annual - spent, 2), "limited": annual > 0}
    except Exception as e:
        logger.debug("learning_path_service: budgets unavailable for the review: %s", e)
    return out


def _priced_course(handle, company_id, title=""):
    """What one person's order of ``handle`` would cost, without writing anything.

    Uses the same quote as the real order. A course with several sessions cannot be quoted for
    "one" session: the list price is shown and the participant picks the session afterwards."""
    import enrollment_service

    product = None
    try:
        product = enrollment_service.get_course(handle, company_id)
    except Exception:
        product = None
    if not product:
        return {"handle": handle, "title": title or handle, "price": 0.0, "session": "", "location": "",
                "needs_session": False, "error": "Kurset findes ikke længere i kataloget."}
    line = {"handle": handle, "title": product.get("title") or title or handle, "session": "", "location": "",
            "needs_session": False, "error": ""}
    try:
        quote = enrollment_service.quote_course(handle, company_id)
        line.update(price=float(quote["price"]), session=quote.get("variant_date") or "", location=quote.get("variant_location") or "")
    except ValueError as exc:
        line.update(price=_money_value(product.get("price_min")), needs_session=True, note=str(exc))
    return line


def _finish_review(cur, company_id, people):
    """Totals, per-department budget before/after and the list of reasons that need HR's attention."""
    budgets = _budget_map(cur, company_id)
    departments = {}
    for person in people:
        dept = departments.setdefault(person["department"] or "", {"department": person["department"] or "", "now": 0.0, "later": 0.0})
        dept["now"] = round(dept["now"] + sum(o["price"] for o in person["now"]), 2)
        dept["later"] = round(dept["later"] + sum(o["price"] for o in person["later"]), 2)
    lines = []
    for dept in sorted(departments.values(), key=lambda d: d["department"]):
        budget = budgets.get(dept["department"])
        dept["has_budget"] = bool(budget and budget["limited"])
        if dept["has_budget"]:
            dept["before"] = budget["remaining"]
            dept["after"] = round(budget["remaining"] - dept["now"], 2)
            dept["after_later"] = round(dept["after"] - dept["later"], 2)
            dept["over"] = dept["after"] < 0
            dept["later_over"] = (not dept["over"]) and dept["after_later"] < 0
        else:
            dept.update(before=None, after=None, after_later=None, over=False, later_over=False)
        lines.append(dept)
    problems = [o["error"] for p in people for o in p["now"] + p["later"] if o.get("error")]
    return {
        "people": people,
        "departments": lines,
        "total_now": round(sum(d["now"] for d in lines), 2),
        "total_later": round(sum(d["later"] for d in lines), 2),
        "over_budget": any(d["over"] for d in lines),
        "problems": problems,
        "blocked": bool(problems) or not people,
    }


def preview_path_assignment(cur, company_id, path_id, user_ids):
    """What assigning a path would do, WITHOUT writing: per person the orders created now and
    (sequential paths) the ones ordered later, totals, and each department's budget before and after.

    Returns None for an unknown path. People who are not active employees are left out and people
    already enrolled are listed in ``already`` (they get nothing)."""
    cur.execute("SELECT * FROM learning_paths WHERE id = %s AND company_id = %s", (path_id, company_id))
    path = cur.fetchone()
    if not path:
        return None
    mode = normalize_mode(path.get("ordering_mode"))
    steps = get_steps(cur, company_id, path_id)
    catalog = [s for s in steps if s.get("course_handle")]
    priced = [_priced_course(s["course_handle"], company_id, s.get("title") or "") for s in catalog]
    now_lines, later_lines = (priced[:1], priced[1:]) if mode == "sequential" else (priced, [])
    people, already = [], []
    for uid in dict.fromkeys(int(u) for u in user_ids):
        emp = _load_employee(cur, company_id, uid)
        if not emp:
            continue
        cur.execute(
            "SELECT id FROM employee_learning_progress WHERE user_id = %s AND company_id = %s AND learning_path_id = %s",
            (uid, company_id, path_id),
        )
        if cur.fetchone():
            already.append(emp["name"])
            continue
        people.append({"user_id": uid, "name": emp["name"], "department": emp.get("department") or "",
                       "now": [dict(line) for line in now_lines], "later": [dict(line) for line in later_lines]})
    review = _finish_review(cur, company_id, people)
    if not catalog:
        review["problems"].append("Forløbet har ingen kurser at bestille. Tilføj trin, eller tildel det uden bestillinger.")
    review.update(path={"id": path_id, "name": path["path_name"], "version": int(path.get("version") or 1), "mode": mode},
                  already=already, blocked=review["blocked"] or not catalog,
                  guidance=len(steps) - len(catalog))
    return review


def preview_course_assignment(cur, company_id, quote, user_ids):
    """The same review for assigning one course to several people (one order each)."""
    people = []
    for uid in dict.fromkeys(int(u) for u in user_ids):
        emp = _load_employee(cur, company_id, uid)
        if not emp:
            continue
        line = {"handle": quote["product_handle"], "title": quote["product_title"], "price": float(quote["price"]),
                "session": quote.get("variant_date") or "", "location": quote.get("variant_location") or "",
                "needs_session": False, "error": ""}
        people.append({"user_id": uid, "name": emp["name"], "department": emp.get("department") or "",
                       "now": [line], "later": []})
    review = _finish_review(cur, company_id, people)
    review["path"] = None
    review["already"] = []
    return review


def assignments_for_learner(cur, user_id, company_id):
    """ "Tildelt af HR": the learner's assigned paths with due date and progress."""
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
            (user_id, company_id),
        )
        rows = list(cur.fetchall() or [])
    except Exception as e:
        logger.warning("learning_path_service: learner assignments failed: %s", e)
        return []
    for r in rows:
        cur.execute(
            "SELECT * FROM learning_assignment_steps WHERE progress_id = %s AND company_id = %s ORDER BY position", (r["id"], company_id)
        )
        r["steps"] = list(cur.fetchall() or [])
    return rows


def assignment_detail(cur, company_id, user_id, progress_id):
    cur.execute(
        "SELECT * FROM employee_learning_progress WHERE id = %s AND company_id = %s AND user_id = %s", (progress_id, company_id, user_id)
    )
    assignment = cur.fetchone()
    if not assignment:
        return None
    cur.execute(
        "SELECT s.*, o.status AS order_status FROM learning_assignment_steps s LEFT JOIN course_orders o ON o.order_id = s.order_id AND o.company_id = s.company_id WHERE s.progress_id = %s AND s.company_id = %s AND s.user_id = %s ORDER BY s.position",
        (progress_id, company_id, user_id),
    )
    assignment["steps"] = list(cur.fetchall() or [])
    return assignment


def _step_state(cur, company_id, step):
    """The truth about one frozen step: guidance and skipped steps keep their own state, an ordered
    step follows its order (completed / failed when cancelled or rejected / ordered)."""
    if step.get("status") == "skipped":
        return "skipped"          # HR skipped it, whatever its old order says
    if step.get("order_id"):
        cur.execute("SELECT status FROM course_orders WHERE order_id = %s AND company_id = %s", (step["order_id"], company_id))
        order = cur.fetchone()
        return (
            "completed"
            if order and order["status"] == "completed"
            else ("failed" if not order or order["status"] in ("cancelled", "rejected") else "ordered")
        )
    return step["status"]


def _progress_row(cur, progress_id, company_id):
    try:
        cur.execute(
            "SELECT id, user_id, learning_path_id, due_date, ordering_mode, assigned_by_user_id FROM employee_learning_progress "
            "WHERE id = %s AND company_id = %s",
            (progress_id, company_id),
        )
        return cur.fetchone()
    except Exception as e:
        from transaction_errors import propagate_transaction_abort

        propagate_transaction_abort(e)
        logger.debug("learning_path_service: progress row without ordering columns: %s", e)
        return None


def _advance_sequential(cur, company_id, progress, steps):
    """Order the next course of a sequential assignment when everything before it is done.

    ``steps`` = [(step row, state)] by position. The first course is ordered at assignment, so a
    guidance step before it never blocks; after that every step before the next course must be
    completed or skipped. A step that failed (order cancelled/rejected or could not be created)
    stops the chain until HR retries or skips it. Runs inside the caller's transaction."""
    first_course_seen = False
    for index, (step, state) in enumerate(steps):
        if not step.get("course_handle"):
            continue
        if step.get("order_id") or state in _DONE_STATES:
            if state in _DONE_STATES:
                first_course_seen = True
                continue
            return False                      # ordered and still running, or failed: wait
        if state == "failed":
            return False                      # could not be ordered: HR must act
        if first_course_seen and not all(st in _DONE_STATES for _, st in steps[:index]):
            return False
        emp = _load_employee(cur, company_id, progress["user_id"])
        if not emp:
            return False
        ctx = _assigner_ctx(cur, company_id, progress.get("assigned_by_user_id"))
        full = dict(step)
        result = _order_frozen_step(cur, company_id, progress, full, emp, ctx, path_id=progress.get("learning_path_id"),
                                    in_transaction=True)
        if result.get("success"):
            try:
                from notification_service import notify_user

                notify_user(cur, user_id=emp["user_id"], username=emp.get("username"), company_id=company_id,
                            title="Næste kursus i dit forløb er bestilt: %s" % full["title"], kind="assignment",
                            message="Du har gennemført det forrige trin. Kurset er godkendt, og HR eller udbyderen bekræfter din plads.",
                            action_url="/min-laering/forloeb/%s" % progress["id"],
                            dedupe_key="path-step-ordered:%s" % step["id"], dedupe_hours=None)
            except Exception as e:
                from transaction_errors import propagate_transaction_abort

                propagate_transaction_abort(e)
        else:
            _notify_step_problem(cur, company_id, progress, emp, full, result.get("message") or "Bestillingen kunne ikke oprettes.")
        return True
    return False


def refresh_assignment(cur, progress_id, company_id):
    """Roll up frozen steps. An acknowledged guidance step counts; a failed order does not.

    In a ``sequential`` assignment this is also where the next course is ordered: when the
    steps before it are completed or skipped, it is created through the same path as the
    assignment (pre-approved as the original assigner) inside the caller's transaction."""
    cur.execute(
        "SELECT id, status, order_id, step_type, course_handle, title, position FROM learning_assignment_steps WHERE progress_id = %s AND company_id = %s ORDER BY position",
        (progress_id, company_id),
    )
    steps = list(cur.fetchall() or [])
    states = []
    for step in steps:
        state = _step_state(cur, company_id, step)
        if step.get("order_id"):
            cur.execute("UPDATE learning_assignment_steps SET status = %s WHERE id = %s", (state, step["id"]))
        states.append((step, state))
    progress = _progress_row(cur, progress_id, company_id)
    if progress and normalize_mode(progress.get("ordering_mode")) == "sequential":
        if _advance_sequential(cur, company_id, progress, states):
            cur.execute(
                "SELECT id, status, order_id, step_type, course_handle, title, position FROM learning_assignment_steps WHERE progress_id = %s AND company_id = %s ORDER BY position",
                (progress_id, company_id),
            )
            states = [(s, _step_state(cur, company_id, s)) for s in list(cur.fetchall() or [])]
    done = sum(state in _DONE_STATES for _, state in states)
    percent = (100 if done == len(steps) else min(99, round(done * 100 / len(steps)))) if steps else 0
    state = "completed" if steps and done == len(steps) else ("in_progress" if done else "not_started")
    cur.execute(
        "UPDATE employee_learning_progress SET progress_percentage = %s, status = %s, completed_at = CASE WHEN %s = 'completed' THEN CURRENT_TIMESTAMP ELSE NULL END WHERE id = %s AND company_id = %s",
        (percent, state, state, progress_id, company_id),
    )
    return {"progress": percent, "status": state}


def refresh_for_order(cur, order_id, company_id):
    cur.execute(
        "SELECT DISTINCT progress_id FROM learning_assignment_steps WHERE order_id = %s AND company_id = %s", (order_id, company_id)
    )
    for row in list(cur.fetchall() or []):
        refresh_assignment(cur, row["progress_id"], company_id)


def acknowledge_step(cur, company_id, user_id, step_id, *, skipped=False, note=""):
    cur.execute(
        "SELECT * FROM learning_assignment_steps WHERE id = %s AND company_id = %s AND user_id = %s FOR UPDATE",
        (step_id, company_id, user_id),
    )
    step = cur.fetchone()
    if not step or step["step_type"] != "info":
        return {"success": False, "message": "Kun vejledningstrin kan afkrydses manuelt."}
    if skipped and not note.strip():
        return {"success": False, "message": "Skriv hvorfor trinnet springes over."}
    cur.execute(
        "UPDATE learning_assignment_steps SET status = %s, last_error = %s, completed_at = CURRENT_TIMESTAMP WHERE id = %s",
        ("skipped" if skipped else "completed", note[:1000] or None, step_id),
    )
    return {"success": True, **refresh_assignment(cur, step["progress_id"], company_id)}


def order_assignment_step(ctx, step_id, *, session_id=None):
    """The learner chooses the session of a step that could not be ordered, or re-requests it.

    A step that was never ordered (typically a course with several sessions) is ordered as the
    assignment it is: pre-approved as the person who assigned the path, only the session is the
    learner's choice. A step whose earlier order was cancelled or rejected goes through approval
    again. In a sequential path a course that is not due yet is ordered automatically later."""
    import order_service
    import enrollment_service

    conn = order_service._get_connection()
    cur = order_service._dict_cursor(conn)
    try:
        cur.execute(
            "SELECT s.*, u.email FROM learning_assignment_steps s JOIN users u ON u.id = s.user_id WHERE s.id = %s AND s.company_id = %s AND s.user_id = %s",
            (step_id, ctx.company_id, ctx.user_id),
        )
        step = cur.fetchone()
        if not step or not step.get("course_handle"):
            return {"success": False, "message": "Trinnet blev ikke fundet."}
        progress = _progress_row(cur, step["progress_id"], ctx.company_id) or {}
        if (normalize_mode(progress.get("ordering_mode")) == "sequential" and not step.get("order_id")
                and step.get("status") != "failed"):
            return {"success": False, "message": "Dette kursus bestilles automatisk, når de forrige trin er gennemført."}
        extra = {"assignment_step_id": step_id, "session_id": session_id}
        acting = ctx
        if not step.get("order_id") and progress.get("assigned_by_user_id"):
            emp = _load_employee(cur, ctx.company_id, ctx.user_id)
            if emp:
                acting = _assigner_ctx(cur, ctx.company_id, progress["assigned_by_user_id"])
                extra.update(_order_extra(progress, step, emp, progress.get("learning_path_id")), session_id=session_id)
        result = enrollment_service.create_order(
            acting,
            product_handle=step["course_handle"],
            user_email=step.get("email") or "",
            extra=extra,
        )
        if not result.get("success"):
            cur.execute(
                "UPDATE learning_assignment_steps SET last_error = %s WHERE id = %s AND order_id IS NULL", (result.get("message"), step_id)
            )
            conn.commit()
        return result
    finally:
        cur.close()


def hr_retry_step(ctx, step_id, *, session_id=None):
    """HR orders a failed step again (a cancelled or rejected order, or one that could not be
    created). Assigned by HR, so it is approved; the budget rule still applies."""
    import order_service
    import enrollment_service

    conn = order_service._get_connection()
    cur = order_service._dict_cursor(conn)
    try:
        cur.execute("SELECT * FROM learning_assignment_steps WHERE id = %s AND company_id = %s", (step_id, ctx.company_id))
        step = cur.fetchone()
        if not step or not step.get("course_handle"):
            return {"success": False, "message": "Trinnet blev ikke fundet."}
        if step.get("status") != "failed":
            return {"success": False, "message": "Kun trin, der er mislykkedes, kan bestilles igen."}
        progress = _progress_row(cur, step["progress_id"], ctx.company_id) or {}
        emp = _load_employee(cur, ctx.company_id, step["user_id"])
        if not emp:
            return {"success": False, "message": "Medarbejderen er ikke længere aktiv i virksomheden."}
        extra = _order_extra(progress, step, emp, progress.get("learning_path_id"))
        extra["session_id"] = session_id
        result = enrollment_service.create_order(ctx, product_handle=step["course_handle"], product_title=step.get("title") or "",
                                                 price=0, extra=extra)
        if result.get("success"):
            cur.execute("UPDATE learning_assignment_steps SET last_error = NULL WHERE id = %s", (step_id,))
            refresh_assignment(cur, step["progress_id"], ctx.company_id)
        else:
            cur.execute("UPDATE learning_assignment_steps SET last_error = %s WHERE id = %s AND order_id IS NULL",
                        (result.get("message"), step_id))
        conn.commit()
        return result
    finally:
        cur.close()


def hr_skip_step(ctx, step_id, note=""):
    """HR skips a failed step so the path can move on (in a sequential path the next course is ordered)."""
    import order_service

    conn = order_service._get_connection()
    cur = order_service._dict_cursor(conn)
    try:
        cur.execute("SELECT * FROM learning_assignment_steps WHERE id = %s AND company_id = %s FOR UPDATE", (step_id, ctx.company_id))
        step = cur.fetchone()
        if not step:
            return {"success": False, "message": "Trinnet blev ikke fundet."}
        if step.get("status") != "failed":
            return {"success": False, "message": "Kun trin, der er mislykkedes, kan springes over."}
        text = ("Sprunget over af HR. %s" % note).strip()[:1000]
        cur.execute(
            "UPDATE learning_assignment_steps SET status = 'skipped', last_error = %s, completed_at = CURRENT_TIMESTAMP WHERE id = %s",
            (text, step_id),
        )
        result = refresh_assignment(cur, step["progress_id"], ctx.company_id)
        conn.commit()
        return {"success": True, "message": "Trinnet er sprunget over.", **result}
    except Exception:
        conn.rollback()
        raise
    finally:
        cur.close()


def assign_course_to_people(cur, ctx, company_id, handle, user_ids, *, due_date=None, session_id=None, group_id=None, expected_price=None):
    """Shared course assignment used by the HR screen, AI, and API."""
    import enrollment_service
    import uuid

    out = {"assigned": 0, "skipped": 0, "orders": 0, "order_failures": 0, "results": []}
    ids = list(dict.fromkeys(int(i) for i in user_ids))
    group_id = group_id or uuid.uuid4().hex
    people = []
    for uid in ids:
        cur.execute(
            "SELECT cu.user_id, u.username, COALESCE(cu.full_name, u.username) AS name, COALESCE(cu.email,u.email) AS email, cu.department FROM company_users cu JOIN users u ON u.id = cu.user_id WHERE cu.company_id = %s AND cu.user_id = %s AND cu.status = 'active'",
            (company_id, uid),
        )
        emp = cur.fetchone()
        if not emp:
            out["skipped"] += 1
            continue
        people.append(emp)
    import order_service

    from person_names import display_name

    assigner_name = display_name(cur, company_id, user_id=ctx.user_id, username=ctx.username,
                                 default=ctx.actor_label or "HR")
    conn = order_service._get_connection()
    events = []
    try:
        for emp in sorted(people, key=lambda p: p["user_id"]):
            uid = emp["user_id"]
            result = enrollment_service.create_order(
                ctx,
                product_handle=handle,
                extra={
                    "assign_to": emp,
                    "completion_deadline": due_date,
                    "session_id": session_id,
                    "participant_count": len(people),
                    "group_order_id": group_id,
                    "expected_price": expected_price,
                    "notes": "Bestilt af %s til teamet" % assigner_name,
                },
                deferred_events=events,
            )
            out["results"].append({"user_id": uid, **result})
            if not result.get("success"):
                raise ValueError(result.get("message") or "En deltager kunne ikke tilmeldes.")
        duplicates = sum(bool(r.get("duplicate")) for r in out["results"])
        if 0 < duplicates < len(people):
            raise ValueError("Nogle deltagere har allerede en bestilling. Fjern dem og bekræft holdets pris igen.")
        conn.commit()
        out["assigned"] = len(people)
        out["orders"] = len(people) - duplicates
        for event in events:
            order_service._emit_event_safe(*event)
    except Exception as exc:
        conn.rollback()
        out["order_failures"] = len(people)
        out["results"] = [{"user_id": emp["user_id"], "success": False, "message": str(exc)} for emp in people]
        out["message"] = str(exc)

    return out


def snapshot_legacy_assignments(conn):
    """Freeze current definitions for old assignments once, preserving their existing progress.

    Old releases did not retain assignment versions. This migration explicitly
    adopts the current definition, never fabricates historical version evidence.
    """
    import MySQLdb.cursors

    cur = conn.cursor(MySQLdb.cursors.DictCursor)
    count = 0
    try:
        cur.execute(
            "SELECT e.id,e.company_id,e.user_id,e.learning_path_id,e.status,p.version FROM employee_learning_progress e JOIN learning_paths p ON p.id = e.learning_path_id AND p.company_id = e.company_id WHERE NOT EXISTS (SELECT 1 FROM learning_assignment_steps s WHERE s.progress_id = e.id)"
        )
        rows = list(cur.fetchall() or [])
        for item in rows:
            steps = get_steps(cur, item["company_id"], item["learning_path_id"])
            for step in steps:
                order_id = None
                state = "completed" if item["status"] == "completed" else "not_started"
                if step.get("course_handle"):
                    cur.execute(
                        "SELECT order_id,status FROM course_orders WHERE company_id = %s AND user_id = %s AND product_handle = %s AND status NOT IN ('cancelled','rejected') ORDER BY created_at DESC LIMIT 1",
                        (item["company_id"], item["user_id"], step["course_handle"]),
                    )
                    order = cur.fetchone()
                    if order:
                        order_id = order["order_id"]
                        state = "completed" if order["status"] == "completed" else "ordered"
                cur.execute(
                    "INSERT IGNORE INTO learning_assignment_steps (progress_id,company_id,user_id,path_version,position,step_type,course_handle,title,order_id,status,last_error) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                    (
                        item["id"],
                        item["company_id"],
                        item["user_id"],
                        item.get("version") or 1,
                        step["position"],
                        step["step_type"],
                        step.get("course_handle"),
                        step.get("title") or "Trin",
                        order_id,
                        state,
                        "Eksisterende tildeling overført til den aktuelle forløbsversion.",
                    ),
                )
            if steps:
                refresh_assignment(cur, item["id"], item["company_id"])
                count += 1
        return count
    finally:
        cur.close()


def sync_personal_path_completion(cur, row):
    """Attach verified order evidence to matching personal-plan recommendations.

    Personal plans may offer alternative courses in a step: completing one of
    those recommendations satisfies that step. Guidance remains user-managed.
    """
    if not row.get("username") or not row.get("product_handle"):
        return
    cur.execute("SELECT id,steps,status FROM user_learning_paths WHERE username=%s AND status<>'arkiveret' FOR UPDATE", (row["username"],))
    for path in list(cur.fetchall() or []):
        try:
            steps = json.loads(path.get("steps") or "[]")
        except (TypeError, ValueError):
            continue
        if not isinstance(steps, list):
            continue
        changed = False
        for step in steps:
            if not isinstance(step, dict):
                continue
            courses = step.get("courses") or []
            if not isinstance(courses, list):
                continue
            handles = {course.get("handle") for course in courses if isinstance(course, dict)}
            if row["product_handle"] in handles:
                step.update(done=True, completion_source="verified_order", completed_order_id=row["order_id"])
                changed = True
        if changed:
            status = "fuldfoert" if steps and all(isinstance(s, dict) and s.get("done") for s in steps) else path["status"]
            cur.execute(
                "UPDATE user_learning_paths SET steps=%s,status=%s WHERE id=%s AND username=%s",
                (json.dumps(steps, ensure_ascii=False), status, path["id"], row["username"]),
            )
