"""
Login identities for people created by SSO or SCIM (S-3.1, S-3.4).

The app authenticates against ``users`` (id, username, password, email, role,
credits) and derives tenant access from ``company_users.user_id -> users.id``.
SSO and SCIM used to create only ``company_users`` rows with NO ``users`` row
and stored ``company_users.id`` in ``session['user_id']``, so an SSO user could
collide with (and act as) an unrelated ``users.id``. Everything now goes through
these helpers so the session always carries ``users.id``.

An SSO/SCIM-created identity gets a random, unusable password: the person signs
in through their identity provider, or chooses a password via the "Glemt
adgangskode" link (which proves control of the mailbox).
"""

import re
import secrets

from werkzeug.security import generate_password_hash

PLATFORM_ADMIN_ROLE = "admin"
# Roles an automated provisioner may hand out. Anything else falls back to employee.
PROVISIONABLE_ROLES = frozenset({"employee", "team_lead"})


class IdentityError(Exception):
    """The identity cannot be used for automated sign-in / provisioning."""


def normalize_email(email):
    return (email or "").strip().lower()


def find_user_by_email(cur, email):
    """The single ``users`` row for ``email`` or None. Two rows with the same
    address is ambiguous and refused rather than guessed."""
    email = normalize_email(email)
    if not email:
        return None
    cur.execute("SELECT * FROM users WHERE LOWER(email) = %s LIMIT 2", (email,))
    rows = cur.fetchall() or []
    if len(rows) > 1:
        raise IdentityError("more than one account uses this e-mail address")
    return rows[0] if rows else None


def _unique_username(cur, email):
    base = re.sub(r"[^a-z0-9._-]+", "", normalize_email(email).split("@")[0]) or "bruger"
    base = base[:40]
    candidate = base
    for _ in range(50):
        cur.execute("SELECT 1 FROM users WHERE username = %s LIMIT 1", (candidate,))
        if not cur.fetchone():
            return candidate
        candidate = "%s%d" % (base, secrets.randbelow(9000) + 1000)
    return "%s-%s" % (base, secrets.token_hex(4))


def ensure_login_identity(cur, email, *, credits=100):
    """Return ``(users_row, created)``. Never returns a platform admin."""
    email = normalize_email(email)
    if not email or "@" not in email:
        raise IdentityError("missing e-mail address")
    user = find_user_by_email(cur, email)
    if user:
        if (user.get("role") or "") == PLATFORM_ADMIN_ROLE:
            raise IdentityError("platform administrators cannot be provisioned or signed in automatically")
        return user, False
    username = _unique_username(cur, email)
    cur.execute(
        "INSERT INTO users (username, email, password, credits, role) VALUES (%s, %s, %s, %s, 'user')",
        (username, email, generate_password_hash(secrets.token_urlsafe(48)), credits),
    )
    user_id = cur.lastrowid
    cur.execute("SELECT * FROM users WHERE id = %s", (user_id,))
    return cur.fetchone(), True


def safe_role(role):
    role = (role or "employee").strip()
    return role if role in PROVISIONABLE_ROLES else "employee"


def get_membership(cur, company_id, user_id):
    cur.execute(
        "SELECT * FROM company_users WHERE company_id = %s AND user_id = %s "
        "ORDER BY (status = 'active') DESC LIMIT 1",
        (company_id, user_id),
    )
    return cur.fetchone()


def add_membership(cur, company_id, user, *, full_name="", role="employee", department="",
                   job_title="", employee_id=None, added_by=None, status="active"):
    """Insert the company_users row linking ``user`` (a users row) to the tenant."""
    cur.execute(
        """INSERT INTO company_users (
               company_id, user_id, username, full_name, email, role, department,
               job_title, employee_id, status, added_by
           ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
        (company_id, user["id"], user.get("username"), full_name or user.get("username"),
         user.get("email"), safe_role(role), department or None, job_title or None,
         employee_id, status, added_by),
    )
    return cur.lastrowid
