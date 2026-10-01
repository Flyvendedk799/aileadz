"""
Admin "act as company" (impersonation) state, in one place (S-2.5).

* ``begin`` stashes the admin's OWN company context once, marks the session as
  acting (with an expiry), and is only ever reached through a POST.
* ``end`` restores exactly what ``begin`` stashed and clears the acting flag.
* The before_request gate in ``auth_decorators.register_session_liveness`` calls
  ``expire_if_due`` so a forgotten impersonation ends by itself.
"""

import time

MAX_ACT_AS_SECONDS = 2 * 60 * 60   # a support session lasts at most 2 hours

_STASH_KEYS = (
    ("_imp_prev_company_id", "company_id"),
    ("_imp_prev_company_role", "company_role"),
    ("_imp_prev_company_name", "company_name"),
)


def is_acting(sess):
    return bool(sess.get("admin_acting_company_id"))


def begin(sess, company):
    """Switch ``sess`` to act as ``company`` ({'id','company_name'})."""
    if not is_acting(sess):
        for stash_key, live_key in _STASH_KEYS:
            sess[stash_key] = sess.get(live_key)
    sess["admin_acting_company_id"] = company["id"]
    sess["admin_acting_until"] = int(time.time()) + MAX_ACT_AS_SECONDS
    sess["company_id"] = company["id"]
    sess["company_role"] = "company_admin"
    sess["company_name"] = company.get("company_name")


def end(sess):
    """Restore the admin's own context. Returns the company id that was acted on."""
    acting = sess.pop("admin_acting_company_id", None)
    sess.pop("admin_acting_until", None)
    for stash_key, live_key in _STASH_KEYS:
        prev = sess.pop(stash_key, None)
        if prev:
            sess[live_key] = prev
        else:
            sess.pop(live_key, None)
    return acting


def expire_if_due(sess, now=None):
    """End a lapsed impersonation. Returns the company id ended, else None."""
    if not is_acting(sess):
        return None
    until = sess.get("admin_acting_until")
    now = time.time() if now is None else now
    # Sessions started before expiry existed have no deadline: give them none
    # rather than kicking an admin out mid-task; they get one on next begin().
    if until and now > until:
        return end(sess)
    return None
