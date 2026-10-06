import datetime
from zoneinfo import ZoneInfo

"""Booking details, supplier changes, attendance evidence and outcome follow-up.

Order state/money writes are delegated to order_service's transaction helpers.
A requested change is never presented as a completed supplier cancellation.
"""
import json
from urllib.parse import urlparse
import order_service as orders
import order_lifecycle as lc


def _error(message, code="bad_transition"):
    return {"success": False, "error": code, "message": message}


def details(cur, order_id):
    cur.execute("SELECT * FROM course_order_details WHERE order_id = %s", (order_id,))
    row = cur.fetchone() or {}
    for key in ("quote_json", "booking_json"):
        try:
            row[key] = json.loads(row.get(key) or "{}")
        except (TypeError, ValueError):
            row[key] = {}
    return row


def booking_for(order_id):
    """The booking details of an order for display only ({} when none; never raises)."""
    try:
        cur = orders._dict_cursor(orders._get_connection())
        try:
            return details(cur, order_id).get("booking_json") or {}
        finally:
            cur.close()
    except Exception:
        return {}


def booking_values(row, supplied):
    from calendar_service import parse_danish_date

    supplied = supplied or {}
    date = str(supplied.get("start_at") or supplied.get("date") or row.get("variant_date") or "").strip()
    if not parse_danish_date(date):
        raise ValueError("Angiv den bekræftede kursusdato før booking.")
    location = str(supplied.get("location") or row.get("variant_location") or "").strip()
    join_url = str(supplied.get("join_url") or "").strip()
    if join_url and urlparse(join_url).scheme not in ("https", "http"):
        raise ValueError("Mødelinket skal starte med https:// eller http://.")
    if not location and not join_url:
        raise ValueError("Angiv et bekræftet sted eller et mødelink.")
    end = str(supplied.get("end_at") or "").strip()
    if end and (not parse_danish_date(end) or parse_danish_date(end) < parse_danish_date(date)):
        raise ValueError("Slutdatoen skal ligge efter startdatoen.")

    def timestamp(text):
        if not text or "T" not in text:
            return text
        value = datetime.datetime.fromisoformat(text)
        if value.tzinfo is None:
            value = value.replace(tzinfo=ZoneInfo("Europe/Copenhagen"))
        return value.isoformat()

    date, end = timestamp(date), timestamp(end)
    if end and "T" in date and "T" in end and datetime.datetime.fromisoformat(end) <= datetime.datetime.fromisoformat(date):
        raise ValueError("Sluttidspunktet skal ligge efter starttidspunktet.")
    return {
        "start_at": date,
        "end_at": end,
        "location": location,
        "join_url": join_url,
        "reference": str(supplied.get("reference") or "")[:255],
        "instructions": str(supplied.get("instructions") or "")[:4000],
        "cancellation_terms": str(supplied.get("cancellation_terms") or "")[:4000],
    }


def _save_details(cur, row, booking=None):
    cur.execute(
        "INSERT IGNORE INTO course_order_details (order_id,company_id,user_id) VALUES (%s,%s,%s)",
        (row["order_id"], row.get("company_id"), row.get("user_id")),
    )
    if booking is not None:
        cur.execute(
            "UPDATE course_order_details SET booking_json = %s WHERE order_id = %s",
            (json.dumps(booking, ensure_ascii=False), row["order_id"]),
        )


def book(ctx, order_id, booking=None, note=None):
    conn = orders._get_connection()
    cur = orders._dict_cursor(conn)
    try:
        orders._lock_order(cur, ctx, order_id)
        row = cur.fetchone()
        if not row:
            return _error("Bestillingen blev ikke fundet.")
        actors = orders.actors_for(ctx, row)
        if not actors:
            return _error("Bestillingen blev ikke fundet.", "not_found")
        if row["status"] == lc.BOOKED:
            return {"success": True, "unchanged": True, "order_id": order_id, "status": lc.BOOKED}
        allowed, code, message = lc.check_transition(row["status"], lc.BOOKED, actors)
        if not allowed:
            return _error(message, code)
        values = booking_values(row, booking)
        _save_details(cur, row, values)
        capture_baseline(cur, row)
        orders._confirm_booking_details(cur, row, values)
        info = orders._apply_transition(cur, ctx, row, lc.BOOKED, actors, note=note or values["reference"])
        conn.commit()
        orders._after_transition(ctx, row, info["old"], lc.BOOKED, info, note=note)
        return {
            "success": True,
            "order_id": order_id,
            "status": lc.BOOKED,
            "status_label": lc.status_label(lc.BOOKED),
            "previous_status": info["old"],
            "charged": False,
            "refunded": False,
            "booking": values,
            "message": "Bookingoplysningerne er gemt, og deltageren får besked.",
        }
    except ValueError as exc:
        conn.rollback()
        return _error(str(exc))
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.rollback()
        cur.close()


def request_change(ctx, order_id, kind, payload=None):
    payload = dict(payload or {})
    if kind not in ("cancel", "reschedule", "substitute"):
        return _error("Ukendt ændring.")
    conn = orders._get_connection()
    cur = orders._dict_cursor(conn)
    try:
        orders._lock_order(cur, ctx, order_id)
        row = cur.fetchone()
        if not row or row["status"] != lc.BOOKED:
            return _error("Ændringer kræver en booket bestilling.")
        actors = orders.actors_for(ctx, row)
        if not actors:
            return _error("Bestillingen blev ikke fundet.")
        if kind == "substitute" and not ({"manager", "admin"} & actors):
            return _error("Bed HR om at skifte deltageren.")
        cur.execute("SELECT id,kind FROM course_order_changes WHERE order_id = %s AND status = 'pending'", (order_id,))
        pending = cur.fetchone()
        if pending:
            return {
                "success": True,
                "pending": True,
                "change_id": pending["id"],
                "message": (
                    "Afbestillingen er allerede sendt til udbyderen og afventer svar. Din plads og budgettet er uændret."
                    if kind == "cancel" and pending.get("kind") == "cancel"
                    else "Der ligger allerede en ændring til behandling."
                ),
            }
        if kind == "reschedule":
            import enrollment_service

            payload["quote"] = enrollment_service.quote_course(
                row["product_handle"], row.get("company_id"), session_id=payload.get("session_id")
            )
        if kind == "substitute":
            try:
                uid = int(payload.get("user_id"))
            except (ValueError, TypeError):
                return _error("Vælg en aktiv medarbejder.")
            cur.execute(
                "SELECT cu.user_id,u.username,COALESCE(cu.full_name,u.username) AS name,COALESCE(cu.email,u.email) AS email,cu.department FROM company_users cu JOIN users u ON u.id=cu.user_id WHERE cu.company_id=%s AND cu.user_id=%s AND cu.status='active'",
                (row["company_id"], uid),
            )
            emp = cur.fetchone()
            if not emp:
                return _error("Medarbejderen blev ikke fundet.")
            payload["participant"] = emp
        payload["note"] = str(payload.get("note") or "")[:4000]
        cur.execute(
            "INSERT INTO course_order_changes (order_id,company_id,user_id,kind,requested_by,requested_kind,payload_json) VALUES (%s,%s,%s,%s,%s,%s,%s)",
            (
                order_id,
                row.get("company_id"),
                row.get("user_id"),
                kind,
                ctx.user_id,
                "vendor" if "vendor" in actors else "customer",
                json.dumps(payload, ensure_ascii=False),
            ),
        )
        change_id = cur.lastrowid
        from notification_service import notify_roles, HR_ROLES

        if row.get("company_id"):
            notify_roles(
                cur,
                row["company_id"],
                HR_ROLES,
                title="Bookingændring afventer svar",
                message="En ændring til ‘%s’ kræver bekræftelse. Budget og booking er uændrede indtil da." % row["product_title"],
                kind="order_change",
                action_url="/hr/order/%s/details" % order_id,
                dedupe_key="change:%s" % change_id,
                dedupe_hours=None,
            )
        change_notice(cur, row, change_id, "requested", "Et ændringsønske afventer svar. Den oprindelige booking gælder indtil accept.")
        orders._record_history(cur, row, kind="change", from_value=kind, to_value="requested", ctx=ctx, note=payload["note"])
        conn.commit()
        return {
            "success": True,
            "pending": True,
            "change_id": change_id,
            "refunded": False,
            "status": lc.BOOKED,
            "message": (
                "Afbestillingen er sendt til udbyderen. Din plads og budgettet er uændret, indtil den er accepteret."
                if kind == "cancel"
                else "Ændringsønsket er sendt. Den nuværende booking og budgetbinding gælder indtil bekræftelse."
            ),
        }
    except ValueError as exc:
        conn.rollback()
        return _error(str(exc))
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.rollback()
        cur.close()


def _rescheduled_booking(row, current, existing, *, new_reference="", new_start_at="", new_instructions=""):
    """Booking after an accepted reschedule: the existing booking with only the new
    session overlaid (date, place). Reference, instructions, join link and
    cancellation terms survive; the vendor or HR may update reference, start
    time and instructions explicitly. The old end time belonged to the old date."""
    from calendar_service import parse_danish_date

    existing = dict(existing or {})
    start = str(new_start_at or "").strip()
    if not start:
        day = parse_danish_date(current.get("variant_date"))
        start = day.isoformat() if day else ""
    supplied = {
        **existing,
        "start_at": start,
        "end_at": "",
        "location": current.get("variant_location") or existing.get("location") or "",
        "reference": str(new_reference or "").strip() or existing.get("reference") or "",
        "instructions": str(new_instructions or "").strip() or existing.get("instructions") or "",
    }
    return booking_values({**row, **current}, supplied)


def resolve_change(ctx, order_id, change_id, accept, *, note="", fee=0, new_reference="", new_start_at="", new_instructions=""):
    """Accept or reject a pending change. ``note`` is the decision (stored only in
    ``course_order_changes.decision_note``); the optional ``new_*`` fields update
    the booking on an accepted reschedule."""
    from enrollment_service import money

    conn = orders._get_connection()
    cur = orders._dict_cursor(conn)
    try:
        orders._lock_order(cur, ctx, order_id)
        row = cur.fetchone()
        if not row:
            return _error("Bestillingen blev ikke fundet.")
        actors = orders.actors_for(ctx, row)
        cur.execute("SELECT * FROM course_order_changes WHERE id=%s AND order_id=%s FOR UPDATE", (change_id, order_id))
        change = cur.fetchone()
        if not change:
            return _error("Ændringsønsket blev ikke fundet.")
        allowed = (
            ({"owner", "manager", "admin"} & actors) if change["requested_kind"] == "vendor" else ({"vendor", "manager", "admin"} & actors)
        )
        if not allowed:
            return _error("Denne ændring skal bekræftes af modparten eller HR.")
        if change["status"] != "pending":
            return {"success": True, "unchanged": True, "message": "Ændringen er allerede behandlet."}
        if accept and row["status"] != lc.BOOKED:
            return _error("Bestillingen er ikke længere booket.")
        if accept and not note.strip():
            return _error("Notér leverandørens bekræftelse eller aftalen med deltageren.")
        payload = json.loads(change["payload_json"])
        info = None
        history_note = note
        if accept and change["kind"] == "cancel":
            retained = money(fee)
            if retained > money(row["price"]):
                return _error("Gebyret må ikke overstige kursusprisen.")
            row["_cancellation_fee"] = float(retained)
            info = orders._apply_transition(cur, ctx, row, lc.CANCELLED, actors, note=note, reason=payload.get("note"))
            _save_details(cur, row)
            data = details(cur, order_id)["booking_json"]
            data["cancellation_fee"] = float(retained)
            _save_details(cur, row, data)
            from learning_path_service import refresh_for_order

            refresh_for_order(cur, order_id, row.get("company_id"))
        elif accept and change["kind"] == "reschedule":
            quote = payload["quote"]
            # Revalidate session and price at acceptance; changed terms need a new agreement.
            import enrollment_service

            current = enrollment_service.quote_course(row["product_handle"], row.get("company_id"), session_id=quote["session_id"])
            if money(current["price"]) != money(quote["price"]):
                return _error("Prisen er ændret. Afvis ønsket, og indhent en ny bekræftelse.")
            orders._replace_order_terms(cur, ctx, row, current)
            values = _rescheduled_booking(
                row, current, details(cur, order_id).get("booking_json"),
                new_reference=new_reference, new_start_at=new_start_at, new_instructions=new_instructions,
            )
            _save_details(cur, row, values)
            from order_timing import session_label

            label = session_label(current["variant_date"], values["start_at"])
            if label != row.get("variant_date"):
                cur.execute("UPDATE course_orders SET variant_date=%s WHERE order_id=%s", (label, order_id))
                row["variant_date"] = label
            history_note = "Ny dato: %s. %s" % (label, note)
        elif accept and change["kind"] == "substitute":
            orders._replace_order_participant(cur, ctx, row, payload["participant"])
        cur.execute(
            "UPDATE course_order_changes SET status=%s,decision_note=%s,resolved_by=%s,resolved_at=CURRENT_TIMESTAMP WHERE id=%s",
            ("accepted" if accept else "rejected", note[:4000], ctx.actor_label or ctx.username, change_id),
        )
        orders._record_history(
            cur, row, kind="change", from_value=change["kind"], to_value="accepted" if accept else "rejected", ctx=ctx, note=history_note
        )
        change_notice(
            cur,
            row,
            change_id,
            "accepted" if accept else "rejected",
            ("Ændringen er bekræftet: " if accept else "Ændringen er afvist: ") + note,
        )
        conn.commit()
        if info:
            orders._after_transition(ctx, row, lc.BOOKED, lc.CANCELLED, info, note=note)
        else:
            orders._emit_event_safe(
                row.get("company_id"), "order.updated", {"order_id": order_id, "change": change["kind"], "accepted": accept}
            )
        return {"success": True, "message": "Ændringen er bekræftet." if accept else "Ændringen er afvist; bookingen er uændret."}
    except ValueError as exc:
        conn.rollback()
        return _error(str(exc))
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.rollback()
        cur.close()


def capture_baseline(cur, row, *, at_booking=True):
    if not row.get("company_id") or not row.get("user_id"):
        return
    cur.execute(
        "SELECT skill_name,current_level FROM employee_skills_matrix WHERE company_id=%s AND employee_id=%s",
        (row["company_id"], row["user_id"]),
    )
    levels = list(cur.fetchall() or [])
    baseline = {r["skill_name"]: r["current_level"] for r in levels} if at_booking else {}
    cur.execute("SELECT manager_user_id FROM company_users WHERE company_id=%s AND user_id=%s", (row["company_id"], row["user_id"]))
    member = cur.fetchone() or {}
    cur.execute(
        "INSERT IGNORE INTO learning_outcome_reviews (order_id,company_id,user_id,manager_user_id,status,baseline_json) VALUES (%s,%s,%s,%s,'awaiting_completion',%s)",
        (row["order_id"], row["company_id"], row["user_id"], member.get("manager_user_id"), json.dumps(baseline, ensure_ascii=False)),
    )


def report_completion(ctx, order_id, *, note="", evidence_url=""):
    conn = orders._get_connection()
    cur = orders._dict_cursor(conn)
    try:
        orders._lock_order(cur, ctx, order_id)
        row = cur.fetchone()
        if not row or "owner" not in orders.actors_for(ctx, row):
            return _error("Bestillingen blev ikke fundet.")
        if row["status"] == lc.COMPLETED:
            return {"success": True, "unchanged": True, "status": lc.COMPLETED, "message": "Gennemførelsen er allerede bekræftet."}
        if row["status"] != lc.BOOKED:
            return _error("Kurset skal være booket, før du kan registrere deltagelse.")
        if evidence_url and urlparse(evidence_url).scheme not in ("https", "http"):
            return _error("Dokumentationslinket skal starte med https:// eller http://.")
        _save_details(cur, row)
        cur.execute(
            "UPDATE course_order_details SET completion_state='reported',evidence_note=%s,evidence_url=%s,reported_at=COALESCE(reported_at,CURRENT_TIMESTAMP) WHERE order_id=%s",
            (note[:4000], evidence_url[:1000], order_id),
        )
        from notification_service import notify_roles, HR_ROLES

        if row.get("company_id"):
            notify_roles(
                cur,
                row["company_id"],
                HR_ROLES,
                title="Deltagelse afventer bekræftelse",
                message="%s har meldt ‘%s’ gennemført. Bekræft deltagelse og kompetenceudbytte."
                % (row.get("user_name") or row.get("username"), row["product_title"]),
                kind="attendance",
                action_url="/hr/ordre/%s/udbytte" % order_id,
                dedupe_key="attendance:%s" % order_id,
                dedupe_hours=None,
            )
        conn.commit()
        return {
            "success": True,
            "reported": True,
            "order_id": order_id,
            "status": lc.BOOKED,
            "message": "Din deltagelse er registreret og afventer bekræftelse fra HR eller udbyderen.",
        }
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.rollback()
        cur.close()


def outcome_review(ctx, order_id, ratings, *, note=""):
    import skill_history
    from competency import canonical_skill

    conn = orders._get_connection()
    cur = orders._dict_cursor(conn)
    try:
        orders._lock_order(cur, ctx, order_id)
        row = cur.fetchone()
        if not row or not ({"manager", "admin"} & orders.actors_for(ctx, row)):
            return _error("Kun HR eller din leder kan bekræfte udbyttet.")
        if row["status"] != lc.COMPLETED:
            return _error("Bekræft først, at kurset er gennemført.")
        cur.execute("SELECT * FROM learning_outcome_reviews WHERE order_id=%s FOR UPDATE", (order_id,))
        review = cur.fetchone()
        if not review:
            return _error("Opfølgningsopgaven blev ikke fundet.")
        if review["status"] == "completed":
            return {"success": True, "unchanged": True, "message": "Udbyttet er allerede vurderet."}
        clean = []
        for rating in ratings:
            name = canonical_skill(str(rating.get("name") or "").strip())
            try:
                level = int(rating.get("level"))
            except (ValueError, TypeError):
                return _error("Vælg et niveau fra 1 til 5.")
            if not name or not 1 <= level <= 5:
                return _error("Angiv kompetence og niveau fra 1 til 5.")
            clean.append((name, level))
        if not clean or not note.strip():
            return _error("Angiv mindst én kompetence og en kort vurdering af udbyttet.")
        for name, level in clean:
            previous = skill_history.current_level_for(cur, row["company_id"], row["user_id"], name)
            cur.execute(
                "INSERT INTO employee_skills_matrix (employee_id,company_id,skill_name,current_level) VALUES (%s,%s,%s,%s) ON DUPLICATE KEY UPDATE current_level=%s",
                (row["user_id"], row["company_id"], name, level, level),
            )
            skill_history.record_snapshot(
                cur, row["company_id"], row["user_id"], name, level, previous_level=previous, source="post_course", order_id=row["id"]
            )
        cur.execute(
            "UPDATE learning_outcome_reviews SET status='completed',review_note=%s,reviewed_by=%s,reviewed_at=CURRENT_TIMESTAMP WHERE order_id=%s",
            (note[:4000], ctx.user_id, order_id),
        )
        conn.commit()
        return {"success": True, "message": "Kompetenceudbyttet er gemt og knyttet til dette kursus."}
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.rollback()
        cur.close()


def change_notice(cur, row, change_id, phase, message):
    """A change is a distinct event, never deduped against the original booking."""
    from notification_service import notify_user

    title = "Nyt om bookingændring: " + row["product_title"]
    key = "booking-change:%s:%s" % (change_id, phase)
    url = "/ordre/%s/booking" % row["order_id"]
    notify_user(
        cur,
        user_id=row.get("user_id"),
        username=row.get("username"),
        company_id=row.get("company_id"),
        title=title,
        message=message,
        kind="order_change",
        action_url=url,
        dedupe_key=key,
        dedupe_hours=None,
    )
    contacts = {row.get("user_email"): url} if row.get("user_email") else {}
    if row.get("vendor_id"):
        cur.execute("SELECT contact_email FROM vendors WHERE id=%s", (row["vendor_id"],))
        vendor = cur.fetchone() or {}
        if vendor.get("contact_email"):
            contacts[vendor["contact_email"]] = "/vendor/orders/%s/booking" % row["order_id"]
    for email, link in contacts.items():
        orders._send_email_safe(
            email,
            title,
            "business_update",
            row.get("company_id"),
            cursor=cur,
            dedupe_key=key,
            heading=title,
            message=message + "\n" + orders._app_base_url() + link,
        )
