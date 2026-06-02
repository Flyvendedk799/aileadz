"""Catalog freshness monitor (roadmap quick win, Theme E/D).

Two read-only detectors plus a best-effort HR nudge:

  * ``expiring_agreements`` — the reliable half. Reads the enterprise
    ``company_supplier_agreements`` table and surfaces negotiated supplier
    agreements that are about to lapse (or already have), so HR can renew them
    before discounts silently disappear. Scoped by ``company_id`` when given.

  * ``stale_courses`` — the noisier half. Flags catalog courses whose latest
    session date is in the PAST. To avoid false positives on the thousands of
    ambiguous Danish dates in the catalog ("17. - 19. februar", with no year)
    AND on postal codes that look like years ("2000 Frederiksberg"), a session
    date is ONLY counted as stale when it parses with an EXPLICIT year via a
    Danish month-name pattern. This gate is deliberate (per the roadmap).

  * ``notify_expiring_agreements`` — optionally enqueues an HR notification row
    for expiring agreements into the existing ``company_notifications`` table
    (targeting HR roles), idempotently so the same agreement is not notified
    twice within the dedupe window.

Hard rules honoured here:
  - Every public function is fully guarded and NEVER raises; on any error it
    returns a safe empty/zero value so callers can render an empty state.
  - Tenant isolation is application-level: every query is scoped by
    ``company_id`` when one is supplied.
  - Catalog reads are read-only (compute-on-read); the catalog is never mutated.
  - autocommit is False, so the notification writer commits explicitly.
"""

import datetime
import re

try:  # Boot-safe: a problem importing Flask must never crash create_app.
    from flask import current_app, has_app_context
except Exception:  # pragma: no cover - defensive boot guard
    current_app = None

    def has_app_context():
        return False


# Danish month names -> month number. Used to disambiguate genuine session
# dates (a day + month name + 4-digit year) from bare 4-digit postal codes,
# which are extremely common in the catalog location strings.
_DANISH_MONTHS = {
    "januar": 1,
    "februar": 2,
    "marts": 3,
    "april": 4,
    "maj": 5,
    "juni": 6,
    "juli": 7,
    "august": 8,
    "september": 9,
    "oktober": 10,
    "november": 11,
    "december": 12,
}

# "3. marts 2025", "11. marts 2025", "2. februar 2026" — day, Danish month
# name, then an EXPLICIT 4-digit year. The month name is what stops a postal
# code ("2000 Frederiksberg") from ever being read as a year.
_DK_DATE_RE = re.compile(
    r"\b(\d{1,2})\.?\s+(" + "|".join(_DANISH_MONTHS.keys()) + r")\s+((?:19|20)\d{2})\b",
    re.IGNORECASE,
)

# Explicit ISO dates (e.g. CSV-imported "2025-03-14"). Also year-explicit.
_ISO_DATE_RE = re.compile(r"\b((?:19|20)\d{2})-(\d{1,2})-(\d{1,2})\b")


def _today():
    return datetime.date.today()


def _refresh_connection(mysql):
    """Best-effort refresh of the (possibly stale) PyMySQL/MySQLdb connection."""
    try:
        import db_compat

        db_compat.refresh_flask_mysql_connection(mysql)
    except Exception:
        pass


def _dict_cursor():
    """Return a DictCursor on the live connection, or None if unavailable."""
    if current_app is None or not has_app_context():
        return None
    try:
        import MySQLdb.cursors
    except Exception:
        return None
    try:
        mysql = current_app.mysql
        _refresh_connection(mysql)
        return mysql.connection.cursor(MySQLdb.cursors.DictCursor)
    except Exception:
        return None


def _log_warning(message, exc):
    try:
        if current_app is not None:
            current_app.logger.warning("%s: %s", message, exc)
    except Exception:
        pass


# ── Date parsing helpers (explicit-year gate) ──────────────────────────────

def _parse_explicit_dates(text):
    """Return every date in ``text`` that parses with an EXPLICIT year.

    Matches Danish "<day>. <month-name> <year>" and ISO "YYYY-MM-DD". Anything
    without a 4-digit year (the bulk of the catalog) yields no matches, which is
    exactly the gate the roadmap asks for. Returns a list of ``datetime.date``.
    """
    if not text:
        return []
    found = []
    try:
        for m in _DK_DATE_RE.finditer(text):
            day = int(m.group(1))
            month = _DANISH_MONTHS.get(m.group(2).lower())
            year = int(m.group(3))
            if not month:
                continue
            try:
                found.append(datetime.date(year, month, day))
            except ValueError:
                continue
        for m in _ISO_DATE_RE.finditer(text):
            year, month, day = int(m.group(1)), int(m.group(2)), int(m.group(3))
            try:
                found.append(datetime.date(year, month, day))
            except ValueError:
                continue
    except Exception:
        return []
    return found


def _latest_session_date(product):
    """Latest explicit-year session date for a normalized catalog product.

    Looks at variant date/title/location strings and the product-level ``dates``
    list. A course is only "stale" when its LATEST session is in the past, so
    date ranges whose end date is still in the future are correctly NOT flagged.
    Returns a ``datetime.date`` or None when no explicit-year date exists.
    """
    candidates = []
    try:
        for variant in product.get("variants") or []:
            if not isinstance(variant, dict):
                continue
            for key in ("date", "title", "location"):
                candidates.extend(_parse_explicit_dates(variant.get(key) or ""))
        for value in product.get("dates") or []:
            candidates.extend(_parse_explicit_dates(str(value)))
    except Exception:
        return None
    return max(candidates) if candidates else None


# ── Reliable half: expiring / expired supplier agreements ──────────────────

def expiring_agreements(company_id=None, within_days=30):
    """Supplier agreements expiring within ``within_days`` days (or already expired).

    Reads ``company_supplier_agreements`` (enterprise schema). Scoped by
    ``company_id`` when given; otherwise platform-wide (admin view). Only active
    agreements with a concrete ``valid_until`` are considered — open-ended
    agreements (NULL ``valid_until``) never lapse and are skipped.

    Returns a list of dicts sorted by urgency (soonest/most-overdue first):
        company_id, vendor_name, agreement_name, agreement_reference,
        discount_type, discount_value, valid_from, valid_until,
        days_left (negative = already expired), is_expired (bool).
    Never raises; returns [] on any error.
    """
    try:
        within = int(within_days)
    except (TypeError, ValueError):
        within = 30
    if within < 0:
        within = 0

    cur = _dict_cursor()
    if cur is None:
        return []

    results = []
    try:
        params = []
        where = ["is_active = 1", "valid_until IS NOT NULL"]
        if company_id:
            where.append("company_id = %s")
            params.append(company_id)
        # Already expired OR expiring within the window.
        where.append("valid_until <= DATE_ADD(CURDATE(), INTERVAL %s DAY)")
        params.append(within)

        cur.execute(
            """
            SELECT company_id, vendor_name, agreement_name, agreement_reference,
                   discount_type, discount_value, valid_from, valid_until, id
            FROM company_supplier_agreements
            WHERE """
            + " AND ".join(where)
            + """
            ORDER BY valid_until ASC, vendor_name ASC
            """,
            tuple(params),
        )
        today = _today()
        for row in cur.fetchall():
            valid_until = row.get("valid_until")
            if isinstance(valid_until, datetime.datetime):
                valid_until = valid_until.date()
            days_left = None
            is_expired = False
            if isinstance(valid_until, datetime.date):
                days_left = (valid_until - today).days
                is_expired = days_left < 0
            try:
                discount_value = (
                    float(row.get("discount_value"))
                    if row.get("discount_value") is not None
                    else 0.0
                )
            except (TypeError, ValueError):
                discount_value = 0.0
            results.append(
                {
                    "id": row.get("id"),
                    "company_id": row.get("company_id"),
                    "vendor_name": (row.get("vendor_name") or "").strip() or "Ukendt leverandør",
                    "agreement_name": row.get("agreement_name") or "",
                    "agreement_reference": row.get("agreement_reference") or "",
                    "discount_type": row.get("discount_type") or "percentage",
                    "discount_value": discount_value,
                    "valid_from": row.get("valid_from"),
                    "valid_until": valid_until,
                    "days_left": days_left,
                    "is_expired": is_expired,
                }
            )
    except Exception as exc:
        _log_warning("expiring_agreements lookup failed", exc)
        results = []
    finally:
        try:
            cur.close()
        except Exception:
            pass
    return results


# ── Noisier half: stale-dated courses (explicit-year gate) ─────────────────

def stale_courses(limit=100):
    """Catalog courses whose latest explicit-year session date is in the PAST.

    Read-only via ``catalog_service``. A course is only flagged when at least
    one of its sessions parses with an explicit year (Danish month + year, or
    ISO) AND its LATEST such date is before today. Ambiguous, year-less dates
    (the vast majority of the catalog) are intentionally ignored to avoid false
    positives — exactly as the roadmap requires.

    Returns a list of dicts sorted by how long ago the last session was
    (most stale first):
        handle, title, vendor, last_session_date, days_stale, url.
    Never raises; returns [] on any error.
    """
    try:
        cap = int(limit)
    except (TypeError, ValueError):
        cap = 100
    if cap <= 0:
        cap = 100

    try:
        import catalog_service
    except Exception as exc:
        _log_warning("stale_courses: catalog_service import failed", exc)
        return []

    today = _today()
    stale = []
    try:
        products = catalog_service.get_products()
    except Exception as exc:
        _log_warning("stale_courses: get_products failed", exc)
        return []

    for product in products or []:
        try:
            last_date = _latest_session_date(product)
            if last_date is None:
                continue  # No explicit-year date -> never flagged (the gate).
            if last_date >= today:
                continue  # Latest session is today or future -> fresh.
            days_stale = (today - last_date).days
            handle = product.get("handle") or ""
            stale.append(
                {
                    "handle": handle,
                    "title": product.get("title") or "Unavngivet kursus",
                    "vendor": product.get("vendor") or "Ukendt",
                    "last_session_date": last_date,
                    "days_stale": days_stale,
                    "url": ("/products/%s" % handle) if handle else "",
                }
            )
        except Exception:
            continue

    stale.sort(key=lambda c: (-(c.get("days_stale") or 0), c.get("title", "").lower()))
    return stale[:cap]


# ── Optional best-effort HR nudge ──────────────────────────────────────────

# HR-ish roles that should see supplier-agreement nudges (matches
# require_hr_access() in hr_dashboard).
_HR_ROLES = ["company_admin", "hr_manager", "department_head"]

# Don't re-notify about the same agreement within this many days.
_NOTIFY_DEDUPE_DAYS = 14


def notify_expiring_agreements(company_id, within_days=30):
    """Best-effort: enqueue an HR notification per expiring agreement.

    Inserts into the existing ``company_notifications`` table targeting HR roles
    (``target_roles`` JSON, ``recipient_user_id`` NULL = role-broadcast), exactly
    how HR notifications are already read in hr_dashboard. Idempotent: skips an
    agreement that already has a recent (last ``_NOTIFY_DEDUPE_DAYS`` days)
    unread notification with the same reference, so HR is never spammed.

    Requires a concrete ``company_id`` (these are tenant-scoped nudges). Returns
    the number of NEW notifications inserted. Never raises; returns 0 on error.
    autocommit is False, so we commit explicitly.
    """
    if not company_id:
        return 0

    agreements = expiring_agreements(company_id=company_id, within_days=within_days)
    if not agreements:
        return 0

    cur = _dict_cursor()
    if cur is None:
        return 0

    inserted = 0
    try:
        import json as _json

        roles_json = _json.dumps(_HR_ROLES)
        for ag in agreements:
            try:
                vendor = ag.get("vendor_name") or "Ukendt leverandør"
                # Stable per-agreement marker for the recent-duplicate guard.
                marker = "supplier-agreement:%s" % (
                    ag.get("agreement_reference") or ag.get("id") or vendor
                )
                cur.execute(
                    """
                    SELECT COUNT(*) AS cnt
                    FROM company_notifications
                    WHERE company_id = %s
                      AND message LIKE %s
                      AND created_at >= DATE_SUB(NOW(), INTERVAL %s DAY)
                    """,
                    (company_id, "%" + marker + "%", _NOTIFY_DEDUPE_DAYS),
                )
                row = cur.fetchone()
                if row and (row.get("cnt") or 0) > 0:
                    continue  # Already nudged recently — don't spam.

                days_left = ag.get("days_left")
                if isinstance(days_left, int) and days_left < 0:
                    title = "Leverandøraftale udløbet: %s" % vendor
                    when = "udløb for %d dage siden" % abs(days_left)
                else:
                    title = "Leverandøraftale udløber snart: %s" % vendor
                    when = (
                        "udløber om %d dage" % days_left
                        if isinstance(days_left, int)
                        else "udløber snart"
                    )
                valid_until = ag.get("valid_until")
                vu_text = (
                    valid_until.strftime("%d-%m-%Y")
                    if isinstance(valid_until, datetime.date)
                    else "ukendt dato"
                )
                # The marker is embedded in the message so the dedupe LIKE above
                # can find it without needing an extra column.
                message = (
                    "Rabataftalen med %s %s (gyldig til %s). "
                    "Forny aftalen, så rabatten ikke forsvinder fra kataloget. "
                    "[%s]" % (vendor, when, vu_text, marker)
                )

                cur.execute(
                    """
                    INSERT INTO company_notifications
                        (company_id, recipient_user_id, sender_user_id,
                         target_roles, title, message, is_urgent, is_read)
                    VALUES (%s, NULL, NULL, %s, %s, %s, %s, 0)
                    """,
                    (
                        company_id,
                        roles_json,
                        title[:255],
                        message,
                        1 if (isinstance(days_left, int) and days_left < 0) else 0,
                    ),
                )
                inserted += 1
            except Exception as exc:
                _log_warning("notify_expiring_agreements: insert skipped", exc)
                continue

        if inserted:
            try:
                current_app.mysql.connection.commit()
            except Exception as exc:
                _log_warning("notify_expiring_agreements: commit failed", exc)
                try:
                    current_app.mysql.connection.rollback()
                except Exception:
                    pass
                inserted = 0
    except Exception as exc:
        _log_warning("notify_expiring_agreements failed", exc)
        inserted = 0
    finally:
        try:
            cur.close()
        except Exception:
            pass
    return inserted
