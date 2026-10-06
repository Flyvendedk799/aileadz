"""One place that turns a user id / username into the name people read.

``company_users.full_name`` is what HR typed or SCIM supplied; ``users.username``
is a login handle ("lise") and must only be the fallback. Notifications, order
pages and approval cards go through here so nobody is shown a username when a
real name exists. Never raises; callers pass their own cursor (dict or tuple).
"""

from __future__ import annotations

import logging

from transaction_errors import propagate_transaction_abort

logger = logging.getLogger(__name__)


def _get(row, key, idx):
    if row is None:
        return None
    if isinstance(row, dict):
        return row.get(key)
    try:
        return row[idx]
    except Exception:
        return None


def display_name(cur, company_id, *, user_id=None, username=None, default=""):
    """Full name for one person: ``company_users.full_name``, else ``users.username``.

    ``company_id`` scopes the ``company_users`` lookup (tenancy); with no company the
    username is all there is. ``default`` is returned when nothing is known."""
    try:
        if user_id is not None:
            if company_id:
                cur.execute(
                    "SELECT NULLIF(TRIM(cu.full_name), '') AS full_name, u.username AS username "
                    "FROM users u LEFT JOIN company_users cu ON cu.user_id = u.id AND cu.company_id = %s "
                    "WHERE u.id = %s", (company_id, int(user_id)))
            else:
                cur.execute("SELECT NULL AS full_name, u.username AS username FROM users u WHERE u.id = %s",
                            (int(user_id),))
            row = cur.fetchone()
            name = _get(row, "full_name", 0) or _get(row, "username", 1)
            if name:
                return str(name)
        elif username and company_id:
            cur.execute(
                "SELECT NULLIF(TRIM(cu.full_name), '') AS full_name FROM company_users cu "
                "JOIN users u ON u.id = cu.user_id WHERE cu.company_id = %s AND u.username = %s",
                (company_id, username))
            name = _get(cur.fetchone(), "full_name", 0)
            if name:
                return str(name)
    except Exception as e:
        propagate_transaction_abort(e)
        logger.debug("display_name lookup failed: %s", e)
    return str(username or default or "")
