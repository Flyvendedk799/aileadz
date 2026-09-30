"""Account lifecycle helpers (N-2.1): reset and invite links.

Nobody should ever handle a plaintext password. HR and admins send a link; new
employees get an invite whose link lets them choose their own password.

The screens, token storage and rate limits are Part A's (S-2.4): the routes
``/forgot-password``, ``/reset-password/<token>`` and ``/set-password/<token>``
live in the ``auth`` blueprint and tokens are hash-only rows in
``password_tokens``. This module is the thin caller-facing API the HR dashboard,
the admin user list, bulk invite and company registration share; every function
delegates to ``auth.send_user_password_link``.
"""

from __future__ import annotations

import logging

from flask import current_app

logger = logging.getLogger(__name__)


def _cursor():
    import MySQLdb.cursors
    return current_app.mysql.connection.cursor(MySQLdb.cursors.DictCursor)


def send_reset_link(user_id: int, *, actor: str = "self", user: dict | None = None) -> bool:
    """Issue a reset token for ``user_id`` and e-mail the link. Returns True when a
    mail was handed to the mail layer; never reveals more than that boolean.
    Pass ``user`` (id/username/email) when the caller already has the row."""
    from auth import send_user_password_link
    if user is None:
        cur = _cursor()
        try:
            cur.execute("SELECT id, username, email FROM users WHERE id = %s", (user_id,))
            user = cur.fetchone()
        finally:
            cur.close()
    if not user or not user.get("email"):
        return False
    logger.info("password reset link issued for user %s by %s", user["id"], actor)
    return bool(send_user_password_link(current_app.mysql.connection, user, "reset"))


def send_invite(user_id: int, *, email: str, name: str = "", username: str = "",
                company: dict | None = None) -> bool:
    """Invite a new user: a set-password link valid for 7 days."""
    if not email:
        return False
    from auth import send_user_password_link
    return bool(send_user_password_link(
        current_app.mysql.connection,
        {"id": int(user_id), "email": email, "username": username or name}, "invite"))
