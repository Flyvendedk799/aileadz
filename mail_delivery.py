"""Durable outbound business mail. SMTP acceptance is not proof of inbox delivery.

Known pre-delivery failures retry with backoff. An expired in-flight lease or an
ambiguous transport exception requires an explicit resend decision, avoiding a
blind replay after a worker crash. Auth/reset mail retains its existing path.
"""

import base64
import datetime
import hashlib
import json
import logging
import smtplib
import uuid
from flask import current_app

logger = logging.getLogger(__name__)
MAX_ATTEMPTS = 8


def _cur():
    import MySQLdb.cursors

    return current_app.mysql.connection.cursor(MySQLdb.cursors.DictCursor)


def enqueue(
    to_email,
    subject,
    template_name,
    branding=None,
    *,
    company_id=None,
    dedupe_key=None,
    attachments=None,
    cursor=None,
    report_schedule_id=None,
    delivery_group=None,
    **context,
):
    if not to_email:
        return False
    payload = {
        "template": template_name,
        "branding": branding or {},
        "context": context,
        "attachments": [(name, base64.b64encode(data).decode(), mime) for name, data, mime in attachments or []],
    }
    encoded = json.dumps(payload, ensure_ascii=False, default=str)
    if len(encoded.encode()) > 10 * 1024 * 1024:
        raise ValueError("Rapporten er for stor til e-mail. Brug eksport og et mindre datoudsnit.")
    key = hashlib.sha256((str(company_id) + ":" + to_email.lower() + ":" + (dedupe_key or str(uuid.uuid4()))).encode()).hexdigest()
    own = cursor is None
    cur = cursor or _cur()
    try:
        cur.execute(
            "INSERT IGNORE INTO mail_outbox (id,company_id,to_email,subject,payload_json,dedupe_key,report_schedule_id,delivery_group,available_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            (
                str(uuid.uuid4()),
                company_id,
                to_email,
                subject[:500],
                encoded,
                key,
                report_schedule_id,
                delivery_group,
                datetime.datetime.now(),
            ),
        )
        if own:
            current_app.mysql.connection.commit()
        return True
    except Exception:
        if own:
            current_app.mysql.connection.rollback()
        raise
    finally:
        if own:
            cur.close()


def _update_report(cur, row):
    if not row.get("report_schedule_id"):
        return
    cur.execute(
        "SELECT state,COUNT(*) AS n FROM mail_outbox WHERE report_schedule_id=%s AND delivery_group=%s GROUP BY state",
        (row["report_schedule_id"], row["delivery_group"]),
    )
    states = {r["state"]: r["n"] for r in cur.fetchall() or []}
    status = (
        "sent" if states and set(states) <= {"sent", "skipped"} else ("attention" if set(states) & {"failed", "uncertain"} else "queued")
    )
    if status == "sent":
        cur.execute(
            "UPDATE company_report_schedules SET last_status=%s,last_sent_at=CURRENT_TIMESTAMP WHERE id=%s AND company_id=%s",
            (status, row["report_schedule_id"], row["company_id"]),
        )
    else:
        cur.execute(
            "UPDATE company_report_schedules SET last_status=%s WHERE id=%s AND company_id=%s",
            (status, row["report_schedule_id"], row["company_id"]),
        )


def drain(limit=50, *, deliver=None, now=None):
    import email_service

    now = now or datetime.datetime.now()
    conn = current_app.mysql.connection
    cur = _cur()
    totals = {"sent": 0, "retry": 0, "failed": 0, "uncertain": 0, "skipped": 0}
    try:
        cur.execute(
            "UPDATE mail_outbox SET state='uncertain',last_error=%s WHERE state='sending' AND locked_until<%s",
            ("Afsendelsen blev afbrudt. Kontroller modtagelsen før manuel genafsendelse.", now),
        )
        cur.execute("SELECT * FROM mail_outbox WHERE state='uncertain' AND report_schedule_id IS NOT NULL")
        for expired in list(cur.fetchall() or []):
            _update_report(cur, expired)
        conn.commit()
        for _ in range(limit):
            cur.execute(
                "SELECT * FROM mail_outbox WHERE state='pending' AND available_at<=%s ORDER BY available_at,id LIMIT 1 FOR UPDATE", (now,)
            )
            row = cur.fetchone()
            if not row:
                conn.rollback()
                break
            attempts = int(row["attempts"]) + 1
            claim_id = str(uuid.uuid4())
            cur.execute(
                "UPDATE mail_outbox SET state='sending',attempts=%s,locked_until=%s,claim_id=%s WHERE id=%s AND state='pending'",
                (attempts, now + datetime.timedelta(minutes=5), claim_id, row["id"]),
            )
            if cur.rowcount != 1:
                conn.rollback()
                continue
            conn.commit()
            state = "sent"
            error = None
            delivery_started = False
            try:
                payload = json.loads(row["payload_json"])
                attachments = [(name, base64.b64decode(data, validate=True), mime) for name, data, mime in payload["attachments"]]
                if payload["template"] in email_service.NON_TRANSACTIONAL_TEMPLATES and email_service.recipient_opted_out(row["to_email"]):
                    state = "skipped"
                elif deliver is None and not email_service._mail_configured():
                    state = "pending"
                    error = "E-mail er ikke sat op. Ret mailopsætningen og prøv igen."
                else:
                    delivery_started = True
                    ok = (deliver or email_service.send_branded_email)(
                        row["to_email"],
                        row["subject"],
                        payload["template"],
                        payload["branding"],
                        company_id=row["company_id"],
                        attachments=attachments,
                        raise_delivery_errors=True,
                        message_id="<%s@futurematch>" % row["id"],
                        **payload["context"],
                    )
                    if not ok:
                        state = "pending"
                        error = "Mailen blev ikke afsendt. Kontroller opsætning og skabelon."
            except (smtplib.SMTPConnectError, ConnectionRefusedError, smtplib.SMTPRecipientsRefused, smtplib.SMTPAuthenticationError):
                state = "pending"
                error = "Mailserveren afviste forbindelsen eller modtageren. Kontroller opsætningen."
            except Exception:
                state = "uncertain" if delivery_started else "failed"
                error = (
                    "Mailserverens kvittering er ukendt. Kontroller modtagelsen før manuel genafsendelse."
                    if delivery_started
                    else "Meddelelsen kunne ikke klargøres. Kontakt support; ingen afsendelse blev forsøgt."
                )
            if state == "pending" and attempts >= MAX_ATTEMPTS:
                state = "failed"
            totals["retry" if state == "pending" else state] += 1
            cur.execute(
                "UPDATE mail_outbox SET state=%s,last_error=%s,available_at=%s,locked_until=NULL,sent_at=CASE WHEN %s=%s THEN CURRENT_TIMESTAMP ELSE sent_at END WHERE id=%s AND claim_id=%s AND state=%s",
                (
                    state,
                    error,
                    now + datetime.timedelta(seconds=min(21600, 60 * 2 ** min(attempts, 8))),
                    state,
                    "sent",
                    row["id"],
                    claim_id,
                    "sending",
                ),
            )
            _update_report(cur, row)
            conn.commit()
        return totals
    finally:
        conn.rollback()
        cur.close()


def retry(ctx, delivery_id, *, confirm_uncertain=False):
    cur = _cur()
    conn = current_app.mysql.connection
    try:
        cur.execute("SELECT * FROM mail_outbox WHERE id=%s FOR UPDATE", (delivery_id,))
        row = cur.fetchone()
        if not row or (not ctx.is_platform_admin and row.get("company_id") != ctx.company_id):
            return False
        if not ctx.is_platform_admin and not ctx.is_manager:
            return False
        if row["state"] == "uncertain" and not confirm_uncertain:
            return False
        if row["state"] not in ("failed", "uncertain", "pending"):
            return False
        cur.execute(
            "UPDATE mail_outbox SET state='pending',attempts=0,last_error=NULL,locked_until=NULL,claim_id=NULL,available_at=%s WHERE id=%s",
            (datetime.datetime.now(), delivery_id),
        )
        orders_note = "Manuel genafsendelse godkendt" + (" efter kontrol af ukendt leveringsstatus" if confirm_uncertain else "")
        from order_service import _write_audit

        _write_audit(
            cur,
            company_id=row.get("company_id"),
            user_id=ctx.user_id,
            action="mail.retry",
            resource_id=delivery_id,
            description=orders_note,
        )
        conn.commit()
        return True
    finally:
        conn.rollback()
        cur.close()
