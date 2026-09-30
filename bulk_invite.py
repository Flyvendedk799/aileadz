"""Bulk CSV invite for HR (N-2.1).

The REST endpoint ``/api/v1/bulk/import-employees`` needs an API key; HR needs a
screen. This is it: upload a CSV, see exactly what will happen (preview), confirm,
and every new person gets an invitation with a link to choose their own password.

CSV columns (header row, ``,`` or ``;``, UTF-8 with or without BOM):
``navn, email, afdeling, stilling, rolle`` - only ``navn`` and ``email`` are required.
"""

from __future__ import annotations

import csv
import io
import json
import logging
import re
import secrets

from flask import (Blueprint, Response, current_app, flash, redirect, render_template, request,
                   session, url_for)
from werkzeug.security import generate_password_hash

from capabilities import can

logger = logging.getLogger(__name__)

bulk_invite_bp = Blueprint("bulk_invite", __name__)

MAX_ROWS = 300
MAX_BYTES = 512 * 1024
ALLOWED_ROLES = {"employee", "department_head", "hr_manager"}   # company_admin is never granted in bulk
HEADER_ALIASES = {
    "navn": "name", "name": "name", "fulde navn": "name", "full_name": "name",
    "email": "email", "e-mail": "email", "mail": "email",
    "afdeling": "department", "department": "department",
    "stilling": "job_title", "titel": "job_title", "job_title": "job_title",
    "rolle": "role", "role": "role",
}
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]{2,}$")

SAMPLE_CSV = "navn;email;afdeling;stilling;rolle\nMette Hansen;mette@firma.dk;Salg;Key Account Manager;employee\n"


def parse_csv(text: str):
    """Return ``(rows, errors)``; each row is a dict of name/email/department/job_title/role."""
    text = (text or "").lstrip("﻿")
    if not text.strip():
        return [], ["Filen er tom."]
    try:
        dialect = csv.Sniffer().sniff(text[:2000], delimiters=",;\t")
    except csv.Error:
        dialect = csv.excel
    reader = csv.reader(io.StringIO(text), dialect)
    try:
        header = next(reader)
    except StopIteration:
        return [], ["Filen er tom."]
    cols = {}
    for i, h in enumerate(header):
        key = HEADER_ALIASES.get(h.strip().lower())
        if key and key not in cols:
            cols[key] = i
    errors = []
    if "name" not in cols or "email" not in cols:
        return [], ["Første række skal være overskrifter og mindst indeholde ‘navn’ og ‘email’."]
    rows, seen = [], set()
    for n, raw in enumerate(reader, start=2):
        if not any(c.strip() for c in raw):
            continue
        if len(rows) >= MAX_ROWS:
            errors.append("Der importeres højst %d personer ad gangen. Resten er udeladt." % MAX_ROWS)
            break
        get = lambda k: (raw[cols[k]].strip() if k in cols and cols[k] < len(raw) else "")  # noqa: E731
        row = {"line": n, "name": get("name"), "email": get("email").lower(),
               "department": get("department"), "job_title": get("job_title"),
               "role": (get("role") or "employee").lower(), "problem": None}
        if not row["name"]:
            row["problem"] = "Navn mangler"
        elif not EMAIL_RE.match(row["email"]):
            row["problem"] = "Ugyldig e-mail"
        elif row["email"] in seen:
            row["problem"] = "Samme e-mail står flere gange"
        elif row["role"] not in ALLOWED_ROLES:
            row["problem"] = "Ukendt rolle (brug employee, department_head eller hr_manager)"
        seen.add(row["email"])
        rows.append(row)
    return rows, errors


def _unique_username(cur, email: str) -> str:
    base = re.sub(r"[^a-z0-9._-]", "", email.split("@")[0].lower()) or "bruger"
    name, i = base, 1
    while True:
        cur.execute("SELECT 1 FROM users WHERE username = %s", (name,))
        if not cur.fetchone():
            return name
        i += 1
        name = "%s%d" % (base, i)


def import_rows(cur, company: dict, rows, *, actor_id=None):
    """Create accounts + memberships for valid rows. Returns counts and per-row outcome."""
    created = linked = skipped = 0
    outcome, invites = [], []
    seats_left = None
    try:
        import seat_governance
        status = seat_governance.trial_status(company["id"])
        if status.get("expired"):
            seats_left = 0
        elif isinstance(status.get("seats_left"), int):
            seats_left = max(status["seats_left"], 0)
    except Exception:
        seats_left = None
    for r in rows:
        if r["problem"]:
            skipped += 1
            outcome.append((r, "Springes over: " + r["problem"]))
            continue
        cur.execute("SELECT id FROM users WHERE LOWER(email) = %s", (r["email"],))
        user = cur.fetchone()
        if user:
            cur.execute("SELECT 1 FROM company_users WHERE company_id = %s AND user_id = %s",
                        (company["id"], user["id"]))
            if cur.fetchone():
                skipped += 1
                outcome.append((r, "Er allerede med i virksomheden"))
                continue
        if seats_left is not None and seats_left <= 0:
            skipped += 1
            outcome.append((r, "Springes over: ingen ledige pladser"))
            continue
        if user:
            uid, is_new = user["id"], False
        else:
            username = _unique_username(cur, r["email"])
            cur.execute("INSERT INTO users (username, email, password, credits, role) VALUES (%s, %s, %s, 0, 'user')",
                        (username, r["email"], generate_password_hash(secrets.token_urlsafe(24))))
            uid, is_new = cur.lastrowid, True
        cur.execute(
            """INSERT INTO company_users (company_id, user_id, username, full_name, email, role, department,
                                          job_title, status, added_by)
               VALUES (%s, %s, (SELECT username FROM users WHERE id = %s), %s, %s, %s, %s, %s, 'active', %s)""",
            (company["id"], uid, uid, r["name"], r["email"], r["role"], r["department"] or None,
             r["job_title"] or None, actor_id))
        if seats_left is not None:
            seats_left -= 1
        if is_new:
            created += 1
            invites.append((uid, r))
            outcome.append((r, "Oprettet - invitation sendes"))
        else:
            linked += 1
            outcome.append((r, "Eksisterende bruger tilknyttet"))
    return {"created": created, "linked": linked, "skipped": skipped}, outcome, invites


def _allowed():
    return can("company.employees")


@bulk_invite_bp.route("/hr/employees/bulk-invite", methods=["GET", "POST"])
def bulk_invite():
    if not session.get("user"):
        return redirect(url_for("auth.login"))
    if not session.get("company_id") or not _allowed():
        flash("Du har ikke adgang til at invitere medarbejdere.", "danger")
        return redirect(url_for("dashboard.dashboard"))
    company = {"id": session["company_id"], "company_name": session.get("company_name") or "Futurematch"}

    if request.method == "GET":
        return render_template("fm/bulk_invite.html", step="upload", sample=SAMPLE_CSV)

    action = request.form.get("action", "preview")
    text = request.form.get("csv_text") or ""
    upload = request.files.get("file")
    if upload and upload.filename:
        data = upload.read(MAX_BYTES + 1)
        if len(data) > MAX_BYTES:
            flash("Filen er for stor (højst 512 kB).", "danger")
            return render_template("fm/bulk_invite.html", step="upload", sample=SAMPLE_CSV), 400
        try:
            text = data.decode("utf-8-sig")
        except UnicodeDecodeError:
            text = data.decode("latin-1")
    rows, errors = parse_csv(text)
    if not rows:
        for e in errors or ["Der var ingen rækker at importere."]:
            flash(e, "danger")
        return render_template("fm/bulk_invite.html", step="upload", sample=SAMPLE_CSV), 400

    if action != "confirm":
        valid = sum(1 for r in rows if not r["problem"])
        return render_template("fm/bulk_invite.html", step="preview", rows=rows, errors=errors,
                               valid=valid, csv_text=text)

    import MySQLdb.cursors
    conn = current_app.mysql.connection
    cur = conn.cursor(MySQLdb.cursors.DictCursor)
    try:
        counts, outcome, invites = import_rows(cur, company, rows, actor_id=session.get("user_id"))
        cur.execute("""INSERT INTO audit_log (company_id, user_id, action_type, resource_type, resource_id, details)
                       VALUES (%s, %s, 'bulk_invite', 'company', %s, %s)""",
                    (company["id"], session.get("user_id"), str(company["id"]), json.dumps(counts)))
        conn.commit()
    except Exception as exc:
        conn.rollback()
        logger.error("bulk invite failed: %s", exc)
        flash("Importen mislykkedes, og der er ikke oprettet noget. Prøv igen.", "danger")
        return render_template("fm/bulk_invite.html", step="upload", sample=SAMPLE_CSV), 500
    finally:
        cur.close()

    mailed = 0
    from account_flows import send_invite
    for uid, r in invites:
        try:
            if send_invite(uid, email=r["email"], name=r["name"], company=company):
                mailed += 1
        except Exception as exc:
            logger.warning("invite mail failed for %s: %s", r["email"], exc)
    try:
        from event_bus import emit_event
        for uid, r in invites:
            emit_event(company["id"], "employee.added", {"user_id": uid, "email": r["email"], "name": r["name"],
                                                          "role": r["role"], "department": r["department"],
                                                          "source": "bulk_invite"})
    except Exception:
        pass
    return render_template("fm/bulk_invite.html", step="done", counts=counts, outcome=outcome, mailed=mailed)


@bulk_invite_bp.route("/hr/employees/bulk-invite/eksempel.csv")
def bulk_invite_sample():
    return Response("﻿" + SAMPLE_CSV, mimetype="text/csv",
                    headers={"Content-Disposition": 'attachment; filename="medarbejdere-eksempel.csv"'})
