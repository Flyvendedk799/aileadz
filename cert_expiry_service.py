"""cert_expiry_service.py — proactive certification-expiry reminders.

A per-learner nudge loop over the ``user_certifications`` table (added with the
first-class certifications feature). Finds certs whose ``expiry_date`` falls
within a near-term window (or has just lapsed) and raises ONE learner
notification per cert via the legacy ``notifications`` surface.

Platform conventions this relies on (see gdpr_service.py):
  * ``notifications.user_id`` HOLDS the username (not an int id). Certs are
    username-scoped, so the two line up directly.
  * The FM bell reads ``notifications WHERE user_id = <session user>`` so a row
    here surfaces to the right learner.

Design rules (mirrors deadline_service):
  * Every public function is GUARDED and must NEVER raise into the scheduler.
    Worst case is "no reminders this run".
  * Idempotent: each cert is reminded at most once per distinct ``expiry_date``
    via the ``expiry_reminded_for`` marker, so the daily job never re-spams. (A
    renewed cert gets a new expiry_date and therefore a fresh future reminder.)
"""
import datetime
import logging

from flask import current_app

logger = logging.getLogger(__name__)

DEFAULT_WITHIN_DAYS = 30      # remind when a cert expires within this many days
DEFAULT_GRACE_DAYS = 14       # also remind for certs that lapsed in the last N days
_MAX_ROWS = 1000              # defensive cap per pass


def _parse_expiry(value):
    """Parse 'YYYY' / 'YYYY-MM' / 'YYYY-MM-DD' -> a date at the END of that period.

    Partial dates resolve to the last day of the period so a cert marked
    "udløber 2026" isn't treated as already-expired on Jan 1. Returns a
    ``datetime.date`` or None when the value can't be understood.
    """
    s = (value or "").strip()
    if not s:
        return None
    parts = s.split("-")
    try:
        y = int(parts[0])
    except Exception:
        return None
    if y < 1900 or y > 3000:
        return None
    try:
        if len(parts) >= 3:
            return datetime.date(y, int(parts[1]), int(parts[2]))
        if len(parts) == 2:
            m = int(parts[1])
            if m == 12:
                return datetime.date(y, 12, 31)
            return datetime.date(y, m + 1, 1) - datetime.timedelta(days=1)
        return datetime.date(y, 12, 31)
    except Exception:
        return None


def remind_expiring_certifications(within_days=DEFAULT_WITHIN_DAYS,
                                   grace_days=DEFAULT_GRACE_DAYS, limit=_MAX_ROWS):
    """Find soon-to-expire / just-lapsed certs and raise a learner notification.

    Returns a summary dict ``{'scanned', 'reminded', 'errors'}``. Never raises.
    """
    summary = {"scanned": 0, "reminded": 0, "errors": 0}

    try:
        from app1.user_profile_db import ensure_tables
        ensure_tables()
    except Exception as e:
        logger.warning("cert_expiry: ensure_tables failed: %s", e)

    try:
        import MySQLdb.cursors
        conn = current_app.mysql.connection
        cur = conn.cursor(MySQLdb.cursors.DictCursor)
        cur.execute(
            "SELECT id, username, name, issuer, expiry_date "
            "FROM user_certifications "
            "WHERE expiry_date IS NOT NULL AND expiry_date <> '' "
            "  AND (expiry_reminded_for IS NULL OR expiry_reminded_for <> expiry_date) "
            "LIMIT %s",
            (int(limit),),
        )
        rows = cur.fetchall() or []
        cur.close()
    except Exception as e:
        logger.warning("cert_expiry: query failed: %s", e)
        try:
            current_app.mysql.connection.rollback()
        except Exception:
            pass
        return summary

    today = datetime.date.today()
    for r in rows:
        summary["scanned"] += 1
        try:
            exp = _parse_expiry(r.get("expiry_date"))
            if exp is None:
                continue
            days = (exp - today).days
            # In-window: expiring within `within_days`, OR lapsed within `grace_days`.
            if days > within_days or days < -grace_days:
                continue

            name = (r.get("name") or "din certificering").strip()
            issuer = (r.get("issuer") or "").strip()
            who = name + (f" fra {issuer}" if issuer else "")
            exp_str = r.get("expiry_date")
            if days < 0:
                title = "Certificering er udløbet"
                msg = (f"{who} udløb {exp_str}. Overvej at forny den, "
                       f"så din profil er opdateret.")
            elif days == 0:
                title = "Certificering udløber i dag"
                msg = f"{who} udløber i dag ({exp_str})."
            else:
                title = "Certificering udløber snart"
                msg = (f"{who} udløber om {days} dag" + ("e" if days != 1 else "")
                       + f" ({exp_str}). Husk at forny den.")

            _notify(r["username"], title, msg)
            _mark_reminded(r["id"], exp_str)
            summary["reminded"] += 1
        except Exception as e:
            summary["errors"] += 1
            logger.warning("cert_expiry: row %s failed: %s", r.get("id"), e)
            try:
                current_app.mysql.connection.rollback()
            except Exception:
                pass

    return summary


def _notify(username, title, message):
    """Insert one learner notification. ``user_id`` HOLDS the username."""
    conn = current_app.mysql.connection
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO notifications (user_id, title, message, image_url) VALUES (%s, %s, %s, %s)",
        (username, (title or "")[:255], (message or "")[:1000], ""),
    )
    conn.commit()
    cur.close()


def _mark_reminded(cert_id, expiry_value):
    """Stamp the cert so the same expiry_date is never reminded twice."""
    conn = current_app.mysql.connection
    cur = conn.cursor()
    cur.execute(
        "UPDATE user_certifications SET expiry_reminded_for = %s WHERE id = %s",
        (expiry_value, cert_id),
    )
    conn.commit()
    cur.close()
