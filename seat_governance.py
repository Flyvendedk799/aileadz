"""
seat_governance.py — seat-count + subscription/trial governance for tenants.

ONE place that answers two questions, both strictly company-scoped:

  * can_add_employee(company_id) -> (ok: bool, reason: str | None)
        May this company add one more employee right now? Returns
        (False, <Danish reason>) when the seat limit is reached OR the trial has
        expired; otherwise (True, None).

  * trial_status(company_id) -> dict
        A snapshot of the company's plan / trial / seat situation for the UI.

DB conventions (see CLAUDE.md / db_compat.py):
  * connection lives on flask.g via current_app.mysql.connection,
  * DictCursor is the default (rows read BY COLUMN NAME),
  * autocommit=False — these functions are READ-ONLY, so no commit is needed,
  * tenant isolation is APPLICATION-LEVEL: every query is scoped by company_id.

Design rules:
  * Import-safe: nothing at module scope can crash create_app().
  * NEVER raises. On any error we FAIL OPEN -> (True, None) for can_add_employee
    so a transient DB glitch never blocks a legitimate employee-add; the error
    is logged. trial_status() returns a safe, permissive default on error.
  * All user-facing strings are Danish.
"""

import logging
import datetime

logger = logging.getLogger(__name__)


def _int_or_none(value):
    """Coerce to int, or None if not coercible."""
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _get_cursor():
    """Return a DictCursor on the request connection, healing stale ones.

    Returns None (never raises) if no connection / app context is available.
    """
    try:
        from flask import current_app
        mysql = getattr(current_app, "mysql", None)
        if mysql is None:
            return None
        # Heal a stale request connection where possible (best-effort).
        try:
            from db_compat import refresh_flask_mysql_connection
            refresh_flask_mysql_connection(mysql)
        except Exception:
            pass
        conn = mysql.connection
        if conn is None:
            return None
        try:
            import MySQLdb.cursors
            return conn.cursor(MySQLdb.cursors.DictCursor)
        except Exception:
            # DictCursor is the project default; a plain cursor would read by
            # index and break the by-name access below, so bail out safely.
            return None
    except Exception as e:
        logger.warning("seat_governance: could not obtain cursor: %s", e)
        return None


def _load_company_seat_row(cur, company_id):
    """Read the seat/subscription columns for one company. company-scoped.

    Returns a dict with plan / trial_ends_at / max_employees / seats_used, or
    None if the company is not found. Never raises.
    """
    cid = _int_or_none(company_id)
    if cid is None:
        return None

    # Pull the governance columns straight off the companies row.
    cur.execute(
        """
        SELECT subscription_plan, trial_ends_at, max_employees,
               current_employee_count
        FROM companies
        WHERE id = %s
        """,
        (cid,),
    )
    company = cur.fetchone()
    if not company:
        return None

    plan = (company.get("subscription_plan") or "").strip().lower() or None
    trial_ends_at = company.get("trial_ends_at")
    max_employees = _int_or_none(company.get("max_employees"))
    if max_employees is None or max_employees <= 0:
        max_employees = 50  # contract default

    # Authoritative live seat count: COUNT active company_users. We prefer the
    # live count over the (possibly stale) cached current_employee_count.
    seats_used = _int_or_none(company.get("current_employee_count")) or 0
    try:
        cur.execute(
            """
            SELECT COUNT(*) AS c
            FROM company_users
            WHERE company_id = %s AND status = 'active'
            """,
            (cid,),
        )
        cnt_row = cur.fetchone()
        live = _int_or_none(cnt_row.get("c")) if cnt_row else None
        if live is not None:
            seats_used = live
    except Exception as e:
        # Fall back to the cached count rather than failing the whole check.
        logger.warning("seat_governance: live seat count failed for %s: %s", cid, e)

    return {
        "plan": plan,
        "trial_ends_at": trial_ends_at,
        "max_employees": max_employees,
        "seats_used": seats_used,
    }


def _trial_expired(plan, trial_ends_at):
    """True only when this is a 'trial' plan whose trial_ends_at is in the past."""
    if plan != "trial":
        return False
    if not trial_ends_at:
        # A trial with no end date is treated as NOT expired (fail open).
        return False
    try:
        now = datetime.datetime.now()
        # trial_ends_at may already be a datetime; coerce strings just in case.
        if isinstance(trial_ends_at, datetime.datetime):
            ends = trial_ends_at
        elif isinstance(trial_ends_at, datetime.date):
            ends = datetime.datetime(trial_ends_at.year, trial_ends_at.month, trial_ends_at.day)
        else:
            ends = datetime.datetime.fromisoformat(str(trial_ends_at))
        return ends < now
    except Exception:
        # Unparseable -> treat as not expired so we never wrongly block.
        return False


def can_add_employee(company_id):
    """May this company add one more active employee right now?

    Returns (ok: bool, reason: str | None). reason is a Danish, user-facing
    message when ok is False. Company-scoped. NEVER raises — on any error it
    FAILS OPEN -> (True, None) so a glitch never blocks a legitimate add.
    """
    cur = None
    try:
        cur = _get_cursor()
        if cur is None:
            # No DB available -> do not block the operation.
            return True, None

        row = _load_company_seat_row(cur, company_id)
        if row is None:
            # Unknown company -> nothing to enforce here; do not block.
            return True, None

        # 1) Trial expiry gate.
        if _trial_expired(row["plan"], row["trial_ends_at"]):
            return (
                False,
                "Din prøveperiode er udløbet. Opgradér dit abonnement for at "
                "tilføje flere medarbejdere.",
            )

        # 2) Seat-limit gate.
        if row["seats_used"] >= row["max_employees"]:
            return (
                False,
                "Du har nået grænsen for antal medarbejdere "
                "(%d af %d pladser brugt). Opgradér dit abonnement for at "
                "tilføje flere." % (row["seats_used"], row["max_employees"]),
            )

        return True, None
    except Exception as e:
        # Fail OPEN: never block a legitimate operation on an internal glitch.
        logger.warning("seat_governance.can_add_employee failed (failing open): %s", e)
        return True, None
    finally:
        if cur is not None:
            try:
                cur.close()
            except Exception:
                pass


def trial_status(company_id):
    """Return a seat/subscription snapshot for the UI. Company-scoped.

    Shape:
        {
            'plan': str | None,
            'trial_ends_at': datetime | None,
            'days_left': int | None,   # days until trial end (>=0; None if N/A)
            'expired': bool,
            'seats_used': int,
            'seats_max': int,
            'seats_left': int,
        }

    NEVER raises. On any error returns a safe, permissive default so callers can
    render without special-casing.
    """
    default = {
        "plan": None,
        "trial_ends_at": None,
        "days_left": None,
        "expired": False,
        "seats_used": 0,
        "seats_max": 50,
        "seats_left": 50,
    }
    cur = None
    try:
        cur = _get_cursor()
        if cur is None:
            return dict(default)

        row = _load_company_seat_row(cur, company_id)
        if row is None:
            return dict(default)

        plan = row["plan"]
        trial_ends_at = row["trial_ends_at"]
        seats_used = row["seats_used"]
        seats_max = row["max_employees"]
        seats_left = seats_max - seats_used
        if seats_left < 0:
            seats_left = 0

        expired = _trial_expired(plan, trial_ends_at)

        days_left = None
        if plan == "trial" and trial_ends_at:
            try:
                now = datetime.datetime.now()
                if isinstance(trial_ends_at, datetime.datetime):
                    ends = trial_ends_at
                elif isinstance(trial_ends_at, datetime.date):
                    ends = datetime.datetime(
                        trial_ends_at.year, trial_ends_at.month, trial_ends_at.day
                    )
                else:
                    ends = datetime.datetime.fromisoformat(str(trial_ends_at))
                delta_days = (ends - now).days
                days_left = delta_days if delta_days > 0 else 0
            except Exception:
                days_left = None

        return {
            "plan": plan,
            "trial_ends_at": trial_ends_at,
            "days_left": days_left,
            "expired": expired,
            "seats_used": seats_used,
            "seats_max": seats_max,
            "seats_left": seats_left,
        }
    except Exception as e:
        logger.warning("seat_governance.trial_status failed (safe default): %s", e)
        return dict(default)
    finally:
        if cur is not None:
            try:
                cur.close()
            except Exception:
                pass
