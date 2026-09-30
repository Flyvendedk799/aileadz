"""One notification system (N-3.2).

Before: two tables (``notifications`` keyed by username, ``company_notifications``
shared by role) that the page, the KPI and the bell badge each counted
differently, and reading a broadcast marked it read for the whole company.

Now: a single ``notifications`` table with **one row per recipient**, so read
state is per user.  Role-targeted cards and company broadcasts are fanned out to
the matching active users at write time.  Every notification can carry an
``action_url`` (where the click should go) and a ``dedupe_key`` so scheduled jobs
never stack identical cards (this replaces the old visible marker string that
``deadline_service`` embedded in message text).

All writers take the caller's cursor so the notification commits atomically with
the business change.  Every public function is guarded: a notification failure
never breaks the caller.
"""

from __future__ import annotations

import json
import logging

logger = logging.getLogger(__name__)

MANAGER_ROLES = ("company_admin", "hr_manager", "department_head")
HR_ROLES = ("company_admin", "hr_manager")


def _row_get(row, key, idx=0):
    if row is None:
        return None
    if isinstance(row, dict):
        return row.get(key)
    try:
        return row[idx]
    except Exception:
        return None


def _dedupe_hit(cur, username, dedupe_key, hours):
    """True when this recipient already has a row with this dedupe key."""
    if not dedupe_key:
        return False
    if hours:
        cur.execute(
            "SELECT 1 FROM notifications WHERE user_id = %s AND dedupe_key = %s "
            "AND `timestamp` >= DATE_SUB(NOW(), INTERVAL %s HOUR) LIMIT 1",
            (username, dedupe_key, int(hours)),
        )
    else:
        cur.execute(
            "SELECT 1 FROM notifications WHERE user_id = %s AND dedupe_key = %s LIMIT 1",
            (username, dedupe_key),
        )
    return cur.fetchone() is not None


def _resolve_identity(cur, username=None, user_id=None):
    """Return (username, users.id) from whichever half is known."""
    if username and user_id:
        return username, int(user_id)
    try:
        if user_id and not username:
            cur.execute("SELECT username FROM users WHERE id = %s", (int(user_id),))
            r = cur.fetchone()
            return _row_get(r, "username"), int(user_id)
        if username and not user_id:
            cur.execute("SELECT id FROM users WHERE username = %s", (username,))
            r = cur.fetchone()
            uid = _row_get(r, "id")
            return username, int(uid) if uid is not None else None
    except Exception as e:
        logger.debug("notification identity lookup failed: %s", e)
    return username, user_id


def notify_user(cur, *, title, message="", username=None, user_id=None,
                company_id=None, kind="info", action_url=None, is_urgent=False,
                sender_user_id=None, dedupe_key=None, dedupe_hours=24,
                image_url=None):
    """Create one notification for one user. Returns the new id or None."""
    try:
        username, user_id = _resolve_identity(cur, username, user_id)
        if not username:
            return None
        if _dedupe_hit(cur, username, dedupe_key, dedupe_hours):
            return None
        cur.execute(
            """INSERT INTO notifications
                   (user_id, company_id, recipient_user_id, sender_user_id, kind,
                    title, message, image_url, action_url, is_urgent, dedupe_key, `read`)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 0)""",
            (username, company_id, user_id, sender_user_id, (kind or "info")[:40],
             str(title or "")[:255], str(message or ""), image_url,
             (action_url or None) and str(action_url)[:500],
             1 if is_urgent else 0, (dedupe_key or None) and str(dedupe_key)[:191]),
        )
        return getattr(cur, "lastrowid", None) or True
    except Exception as e:
        logger.debug("notify_user skipped: %s", e)
        return None


def role_recipients(cur, company_id, roles=None):
    """Active company members (optionally restricted to roles) as dicts with
    ``user_id`` and ``username``. Never raises."""
    try:
        sql = ("SELECT cu.user_id AS user_id, COALESCE(u.username, cu.username) AS username "
               "FROM company_users cu LEFT JOIN users u ON u.id = cu.user_id "
               "WHERE cu.company_id = %s AND cu.status = 'active'")
        params = [company_id]
        if roles:
            sql += " AND cu.role IN (" + ",".join(["%s"] * len(roles)) + ")"
            params.extend(roles)
        cur.execute(sql, tuple(params))
        out, seen = [], set()
        for r in cur.fetchall() or []:
            uname = _row_get(r, "username", 1)
            if uname and uname not in seen:
                seen.add(uname)
                out.append({"user_id": _row_get(r, "user_id", 0), "username": uname})
        return out
    except Exception as e:
        logger.debug("role_recipients failed: %s", e)
        return []


def notify_roles(cur, company_id, roles, *, title, message="", **kw):
    """Fan a card out to every active member holding one of ``roles``.
    Returns the number of notifications created."""
    if not company_id:
        return 0
    n = 0
    for rcpt in role_recipients(cur, company_id, list(roles) if roles else None):
        if notify_user(cur, title=title, message=message, username=rcpt["username"],
                       user_id=rcpt["user_id"], company_id=company_id, **kw):
            n += 1
    return n


def notify_company(cur, company_id, *, title, message="", roles=None, **kw):
    """Broadcast to everyone in a company (optionally only some roles)."""
    return notify_roles(cur, company_id, roles, title=title, message=message, **kw)


def insert_company_notification(cur, company_id, *, title, message="",
                                recipient_user_id=None, target_roles=None,
                                sender_user_id=None, is_urgent=False,
                                action_url=None, kind="info", dedupe_key=None,
                                dedupe_hours=24 * 14):
    """Drop-in for the old ``INSERT INTO company_notifications`` call sites.

    ``recipient_user_id`` set  -> one notification for that user.
    ``target_roles`` (list)    -> fan-out to every active member with such a role.
    neither                    -> fan-out to all active members of the company.
    Returns how many notifications were created (0 when deduped or no audience).
    """
    if not company_id:
        return 0
    common = dict(title=title, message=message, kind=kind,
                  action_url=action_url, is_urgent=is_urgent,
                  sender_user_id=sender_user_id, dedupe_key=dedupe_key,
                  dedupe_hours=dedupe_hours)
    if recipient_user_id:
        return 1 if notify_user(cur, user_id=recipient_user_id, company_id=company_id,
                                **common) else 0
    return notify_roles(cur, company_id, list(target_roles) if target_roles else None,
                        **common)


# ── reading ────────────────────────────────────────────────────────────────

def list_for_user(cur, username, limit=60):
    cur.execute(
        """SELECT n.id, n.title, n.message, n.is_urgent, n.`read` AS is_read,
                  n.action_url, n.kind, n.image_url, n.`timestamp` AS created_at,
                  u.username AS sender_name
           FROM notifications n
           LEFT JOIN users u ON u.id = n.sender_user_id
           WHERE n.user_id = %s
           ORDER BY n.`read` ASC, n.is_urgent DESC, n.`timestamp` DESC
           LIMIT %s""",
        (username, int(limit)),
    )
    return list(cur.fetchall() or [])


def unread_count(cur, username):
    cur.execute("SELECT COUNT(*) AS c FROM notifications WHERE user_id = %s AND `read` = 0",
                (username,))
    r = cur.fetchone()
    try:
        return int(_row_get(r, "c", 0) or 0)
    except (TypeError, ValueError):
        return 0


def mark_read(cur, username, notification_id=None):
    """Mark one (or, with ``notification_id=None``, all) of THIS user's rows read."""
    if notification_id is None:
        cur.execute("UPDATE notifications SET `read` = 1, read_at = NOW() "
                    "WHERE user_id = %s AND `read` = 0", (username,))
    else:
        cur.execute("UPDATE notifications SET `read` = 1, read_at = NOW() "
                    "WHERE id = %s AND user_id = %s", (int(notification_id), username))
    return cur.rowcount


# ── migration of the legacy company_notifications table ────────────────────

def migrate_company_notifications(conn):
    """Fan legacy ``company_notifications`` rows out into ``notifications``.
    Idempotent (dedupe key ``legacy-cn:<id>``); leaves the old table in place."""
    cur = conn.cursor()
    try:
        cur.execute("SELECT id, company_id, recipient_user_id, sender_user_id, target_roles, "
                    "title, message, is_urgent, is_read, created_at FROM company_notifications")
        rows = list(cur.fetchall() or [])
    except Exception as e:
        logger.info("no legacy company_notifications to migrate (%s)", e)
        cur.close()
        return 0
    moved = 0
    for r in rows:
        d = r if isinstance(r, dict) else {}
        cid = d.get("company_id")
        raw_roles = d.get("target_roles")
        roles = None
        if raw_roles:
            try:
                roles = json.loads(raw_roles) if isinstance(raw_roles, (str, bytes)) else raw_roles
                if isinstance(roles, str):
                    roles = [roles]
            except Exception:
                roles = None
        if d.get("recipient_user_id"):
            targets = [{"user_id": d["recipient_user_id"], "username": None}]
        else:
            targets = role_recipients(cur, cid, roles)
        for t in targets:
            key = "legacy-cn:%s" % d.get("id")
            uname, uid = _resolve_identity(cur, t.get("username"), t.get("user_id"))
            if not uname:
                continue
            if _dedupe_hit(cur, uname, key, None):
                continue
            cur.execute(
                """INSERT INTO notifications
                       (user_id, company_id, recipient_user_id, sender_user_id, kind, title,
                        message, is_urgent, dedupe_key, `read`, `timestamp`)
                   VALUES (%s, %s, %s, %s, 'legacy', %s, %s, %s, %s, %s, %s)""",
                (uname, cid, uid, d.get("sender_user_id"), d.get("title"), d.get("message"),
                 d.get("is_urgent") or 0, key, d.get("is_read") or 0, d.get("created_at")),
            )
            moved += 1
    conn.commit()
    cur.close()
    return moved
