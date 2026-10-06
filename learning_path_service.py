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


def save_steps(cur, company_id, path_id, steps, *, actor_user_id=None, note=None):
    """Replace a path's steps and record the PREVIOUS state as a version.

    ``steps`` = [{"course_handle"?, "title"?, "step_type"?}]. A catalog step whose handle is
    missing or unknown is rejected, naming the row: ``{success: False, message, errors}`` with
    ``errors`` = {row index: message} for EVERY bad row, so the editor can show them all and
    keep what was entered. Returns {success, version, steps} otherwise."""
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
    return {"success": True, "version": version + 1, "steps": len(clean)}


def assign_path(cur, ctx, company_id, path_id, user_ids, *, due_date=None, sender_id=None, create=None, expected_version=None):
    """Enrol employees in a path and order its paid steps for each of them.

    Returns {assigned, skipped, orders, order_failures, path_name}. Employees must
    belong to the company; already enrolled ones are skipped. ``create`` is injectable."""
    import order_service
    import enrollment_service

    conn = order_service._get_connection()
    cur.execute("SELECT id, path_name, version FROM learning_paths WHERE id = %s AND company_id = %s FOR UPDATE", (path_id, company_id))
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
    steps = get_steps(cur, company_id, path_id)
    out = {"assigned": 0, "skipped": 0, "orders": 0, "order_failures": 0, "path_name": path["path_name"], "results": []}
    if not steps:
        out["message"] = "Tilføj trin til forløbet før tildeling."
        return out
    for uid in dict.fromkeys(user_ids):
        cur.execute(
            "SELECT cu.user_id, u.username, COALESCE(cu.full_name,u.username) AS name, COALESCE(cu.email,u.email) AS email, cu.department FROM company_users cu JOIN users u ON u.id = cu.user_id WHERE cu.company_id = %s AND cu.user_id = %s AND cu.status = 'active' FOR UPDATE",
            (company_id, uid),
        )
        emp = cur.fetchone()
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
            "INSERT INTO employee_learning_progress (user_id,company_id,learning_path_id,content_type,content_name,status,progress_percentage,due_date,started_at) VALUES (%s,%s,%s,'learning_path',%s,'not_started',0,%s,CURRENT_TIMESTAMP)",
            (uid, company_id, path_id, path["path_name"], due_date),
        )
        progress_id = cur.lastrowid
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
        for step_id, step in step_ids:
            if not step.get("course_handle"):
                continue
            result = (create or enrollment_service.create_order)(
                ctx,
                product_handle=step["course_handle"],
                product_title=step.get("title") or "",
                price=0,
                extra={
                    "assign_to": emp,
                    "assignment_step_id": step_id,
                    "completion_deadline": due_date,
                    "recommended_by_tool": "path:%s" % path_id,
                },
            )
            if result.get("success"):
                out["orders"] += not result.get("duplicate")
            else:
                out["order_failures"] += 1
                cur.execute(
                    "UPDATE learning_assignment_steps SET status = 'failed', last_error = %s WHERE id = %s",
                    (result.get("message") or "Bestillingen kunne ikke oprettes.", step_id),
                )
            out["results"].append({"user_id": uid, "step_id": step_id, **result})
        from notification_service import insert_company_notification

        insert_company_notification(
            cur,
            company_id,
            recipient_user_id=uid,
            sender_user_id=sender_id,
            title="Nyt læringsforløb tildelt",
            message="Du er blevet tildelt ‘%s’. Åbn forløbet og vælg eventuelle manglende hold." % path["path_name"],
            action_url="/min-laering/forloeb/%s" % progress_id,
            kind="assignment",
            dedupe_key=None,
            actor_user_id=sender_id,
        )
        refresh_assignment(cur, progress_id, company_id)
        conn.commit()
    return out


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


def refresh_assignment(cur, progress_id, company_id):
    """Roll up frozen steps. An acknowledged guidance step counts; a failed order does not."""
    cur.execute(
        "SELECT id, status, order_id FROM learning_assignment_steps WHERE progress_id = %s AND company_id = %s", (progress_id, company_id)
    )
    steps = list(cur.fetchall() or [])
    done = 0
    for step in steps:
        if step.get("order_id"):
            cur.execute("SELECT status FROM course_orders WHERE order_id = %s AND company_id = %s", (step["order_id"], company_id))
            order = cur.fetchone()
            state = (
                "completed"
                if order and order["status"] == "completed"
                else ("failed" if not order or order["status"] in ("cancelled", "rejected") else "ordered")
            )
            cur.execute("UPDATE learning_assignment_steps SET status = %s WHERE id = %s", (state, step["id"]))
        else:
            state = step["status"]
        done += state in ("completed", "skipped")
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
    """Retry a failed step or choose its session without duplicating another enrolment."""
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
        result = enrollment_service.create_order(
            ctx,
            product_handle=step["course_handle"],
            user_email=step.get("email") or "",
            extra={"assignment_step_id": step_id, "session_id": session_id},
        )
        if not result.get("success"):
            cur.execute(
                "UPDATE learning_assignment_steps SET last_error = %s WHERE id = %s AND order_id IS NULL", (result.get("message"), step_id)
            )
            conn.commit()
        return result
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
