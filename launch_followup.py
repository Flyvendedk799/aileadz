"""Bounded daily follow-up queues; reminders never infer a supplier outcome."""

import datetime
from flask import current_app
from notification_service import notify_roles, HR_ROLES


def run(now=None):
    import MySQLdb.cursors
    from customer_success import notify_account_team

    now = now or datetime.datetime.now()
    conn = current_app.mysql.connection
    cur = conn.cursor(MySQLdb.cursors.DictCursor)
    counts = {"bookings": 0, "changes": 0, "accounts": 0}
    try:
        cur.execute(
            "SELECT order_id,company_id,product_title FROM course_orders WHERE company_id IS NOT NULL AND status='approved' AND updated_at<%s ORDER BY updated_at LIMIT 500",
            (now - datetime.timedelta(days=2),),
        )
        for row in list(cur.fetchall() or []):
            notify_roles(
                cur,
                row["company_id"],
                HR_ROLES,
                title="Udbyderens bookingbekræftelse mangler",
                message="‘%s’ er godkendt, men pladsen er endnu ikke bekræftet. Kontakt udbyderen og registrer svaret."
                % row["product_title"],
                kind="booking_followup",
                action_url="/hr/order/%s/details#booking" % row["order_id"],
                dedupe_key="booking-followup:" + row["order_id"],
                dedupe_hours=72,
            )
            counts["bookings"] += 1
        cur.execute(
            "SELECT id,order_id,company_id FROM course_order_changes WHERE status='pending' AND created_at<%s ORDER BY created_at LIMIT 500",
            (now - datetime.timedelta(days=2),),
        )
        for row in list(cur.fetchall() or []):
            if row["company_id"]:
                notify_roles(
                    cur,
                    row["company_id"],
                    HR_ROLES,
                    title="Bookingændring mangler svar",
                    message="Indhent bekræftelse på ændringsønsket. Den oprindelige booking gælder stadig.",
                    kind="booking_followup",
                    action_url="/hr/order/%s/details#aendring" % row["order_id"],
                    dedupe_key="change-followup:%s" % row["id"],
                    dedupe_hours=72,
                )
                counts["changes"] += 1
        cur.execute(
            "SELECT a.*,c.company_name FROM customer_accounts a JOIN companies c ON c.id=a.company_id WHERE a.stage<>'paused' AND (a.next_review<=%s OR a.pilot_end<=%s OR a.renewal_date<=%s) ORDER BY a.company_id LIMIT 500",
            (now.date(), now.date() + datetime.timedelta(days=7), now.date() + datetime.timedelta(days=30)),
        )
        for row in list(cur.fetchall() or []):
            key = "account-review:%s:%s" % (row["company_id"], now.strftime("%Y-%W"))
            notify_account_team(
                cur,
                company_id=row["company_id"],
                title="Kundeforløb kræver opfølgning",
                message="%s har et statusmøde, pilotslut eller en fornyelse, der nærmer sig. Aftal næste skridt og opdater datoerne."
                % row["company_name"],
                key=key,
            )
            counts["accounts"] += 1
        conn.commit()
        return counts
    except Exception:
        conn.rollback()
        raise
    finally:
        cur.close()
