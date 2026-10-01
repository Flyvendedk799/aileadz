"""Team orders from chat follow a company policy set by HR (N-5.2, decided).

Modes (``company_team_order_policy.mode``):

* ``linked_orders``  the chat collects participants and creates N linked orders
  (one per person, sharing a ``group_order_id``); each goes through approval and
  budget like any other order.
* ``hr_bulk_assign`` the chat hands the request to HR (a task with the course and
  participants pre-filled for bulk-assign).
* ``not_allowed``    team ordering is off; each person requests the course
  themselves.

A company has one DEFAULT row (``vendor_id IS NULL``) and optional per-vendor
overrides. ``effective_mode`` resolves vendor override -> company default ->
``DEFAULT_MODE``.

Also here: participant resolution (colleagues of the same company only), the
settings partial state and the save route (``company.policies`` capability).
"""

from __future__ import annotations

import logging

from flask import Blueprint, current_app, flash, jsonify, redirect, request, session, url_for

logger = logging.getLogger(__name__)

LINKED = "linked_orders"
HR_BULK = "hr_bulk_assign"
NOT_ALLOWED = "not_allowed"
MODES = (LINKED, HR_BULK, NOT_ALLOWED)
DEFAULT_MODE = LINKED

MODE_LABELS = {
    LINKED: "Én ordre pr. person (følger godkendelse og budget)",
    HR_BULK: "Send til HR, som tildeler kurset til holdet",
    NOT_ALLOWED: "Ikke tilladt, hver person anmoder selv",
}

MAX_PARTICIPANTS = 25


def _val(row, key, idx=0):
    if row is None:
        return None
    return row.get(key) if isinstance(row, dict) else row[idx]


def get_policies(cur, company_id):
    """``{"default": mode, "vendors": {vendor_id: mode}}`` for a company."""
    cur.execute("SELECT vendor_id, mode FROM company_team_order_policy WHERE company_id = %s ORDER BY id",
                (company_id,))
    default, vendors = None, {}
    for r in cur.fetchall() or []:
        vid, mode = _val(r, "vendor_id", 0), _val(r, "mode", 1)
        if mode not in MODES:
            continue
        if vid is None:
            default = mode
        else:
            vendors[int(vid)] = mode
    return {"default": default or DEFAULT_MODE, "vendors": vendors, "explicit_default": default is not None}


def effective_mode(cur, company_id, vendor_id=None):
    """Vendor override -> company default -> DEFAULT_MODE."""
    if not company_id:
        return NOT_ALLOWED
    try:
        pol = get_policies(cur, company_id)
    except Exception as e:
        logger.debug("team policy lookup failed, using default: %s", e)
        return DEFAULT_MODE
    if vendor_id is not None and int(vendor_id) in pol["vendors"]:
        return pol["vendors"][int(vendor_id)]
    return pol["default"]


def set_policy(conn, company_id, mode, vendor_id=None, updated_by=None):
    """Upsert the company default (vendor_id None) or one vendor override."""
    if mode not in MODES:
        raise ValueError("ukendt tilstand")
    cur = conn.cursor()
    try:
        if vendor_id is None:
            cur.execute("SELECT id FROM company_team_order_policy WHERE company_id = %s AND vendor_id IS NULL",
                        (company_id,))
        else:
            cur.execute("SELECT id FROM company_team_order_policy WHERE company_id = %s AND vendor_id = %s",
                        (company_id, vendor_id))
        row = cur.fetchone()
        if row:
            cur.execute("UPDATE company_team_order_policy SET mode = %s, updated_by = %s WHERE id = %s",
                        (mode, updated_by, _val(row, "id", 0)))
        else:
            cur.execute("INSERT INTO company_team_order_policy (company_id, vendor_id, mode, updated_by) "
                        "VALUES (%s, %s, %s, %s)", (company_id, vendor_id, mode, updated_by))
        conn.commit()
    finally:
        cur.close()


def clear_vendor_override(conn, company_id, vendor_id):
    cur = conn.cursor()
    try:
        cur.execute("DELETE FROM company_team_order_policy WHERE company_id = %s AND vendor_id = %s",
                    (company_id, vendor_id))
        conn.commit()
    finally:
        cur.close()


# ── participants ────────────────────────────────────────────────────────────

def resolve_participants(cur, company_id, names, requester_username=None):
    """Match free-text names/usernames/emails to ACTIVE colleagues of this company.

    Returns ``{"matched": [...], "ambiguous": {name: [candidates]}, "unmatched": [...]}``.
    Only members of ``company_id`` can ever be matched (never another tenant).
    """
    cur.execute(
        "SELECT cu.user_id AS user_id, COALESCE(u.username, cu.username) AS username, "
        "cu.full_name AS full_name, COALESCE(cu.email, u.email) AS email, cu.department AS department, "
        "cu.role AS role FROM company_users cu LEFT JOIN users u ON u.id = cu.user_id "
        "WHERE cu.company_id = %s AND cu.status = 'active'", (company_id,))
    people = [{
        "user_id": _val(r, "user_id", 0), "username": _val(r, "username", 1), "full_name": _val(r, "full_name", 2),
        "email": _val(r, "email", 3), "department": _val(r, "department", 4), "role": _val(r, "role", 5),
    } for r in cur.fetchall() or []]
    matched, ambiguous, unmatched, seen = [], {}, [], set()
    for raw in (names or [])[:MAX_PARTICIPANTS]:
        q = str(raw or "").strip()
        if not q:
            continue
        ql = q.casefold()
        if ql in ("mig", "mig selv", "jeg", "me", "myself") and requester_username:
            hits = [p for p in people if (p["username"] or "").casefold() == requester_username.casefold()]
        else:
            hits = [p for p in people if ql in ((p["username"] or "").casefold(), (p["email"] or "").casefold(),
                                                (p["full_name"] or "").casefold())]
            if not hits:
                hits = [p for p in people if ql in (p["full_name"] or "").casefold()
                        or ql in (p["username"] or "").casefold()]
        if len(hits) == 1:
            if hits[0]["user_id"] not in seen:
                seen.add(hits[0]["user_id"])
                matched.append(hits[0])
        elif len(hits) > 1:
            ambiguous[q] = [{"name": h["full_name"] or h["username"], "department": h["department"]} for h in hits[:5]]
        else:
            unmatched.append(q)
    return {"matched": matched, "ambiguous": ambiguous, "unmatched": unmatched}


def policy_guidance_da(mode):
    """One line the assistant can paraphrase; it must not recite it like a rule."""
    return {
        LINKED: "Virksomheden lader dig bestille til flere: der oprettes én ordre pr. person, og hver ordre følger godkendelse og budget.",
        HR_BULK: "Virksomheden lader HR tildele kurser til hold: jeg sender dit ønske til HR med kursus og deltagere udfyldt.",
        NOT_ALLOWED: "Virksomheden har slået teambestilling fra for dette kursus: hver person anmoder selv om pladsen.",
    }.get(mode, "")


# ── settings UI (partial included by the settings hub) ─────────────────────

team_policy_bp = Blueprint("team_policy", __name__)


def policy_state():
    """Data for ``_team_order_policy.html`` (HR's own company only)."""
    import capabilities
    cid = session.get("company_id")
    state = {"allowed": False, "modes": [(m, MODE_LABELS[m]) for m in MODES], "default": DEFAULT_MODE,
             "vendors": [], "available_vendors": []}
    if not cid or not capabilities.can("company.policies"):
        return state
    state["allowed"] = True
    try:
        import MySQLdb.cursors
        cur = current_app.mysql.connection.cursor(MySQLdb.cursors.DictCursor)
        pol = get_policies(cur, cid)
        state["default"] = pol["default"]
        cur.execute("SELECT id, vendor_name FROM vendors ORDER BY vendor_name")
        names = {int(_val(r, "id", 0)): _val(r, "vendor_name", 1) for r in cur.fetchall() or []}
        cur.close()
        state["vendors"] = [{"vendor_id": vid, "vendor_name": names.get(vid, "Udbyder %s" % vid),
                             "mode": mode, "label": MODE_LABELS[mode]} for vid, mode in pol["vendors"].items()]
        state["available_vendors"] = [{"id": i, "name": n} for i, n in names.items() if i not in pol["vendors"]]
    except Exception as e:
        logger.debug("policy_state failed: %s", e)
    return state


def register_jinja(app):
    app.jinja_env.globals["team_order_policy_state"] = policy_state


@team_policy_bp.route("/hr/team-order-policy/save", methods=["POST"])
def save_policy():
    import capabilities
    if not session.get("user") or not capabilities.can("company.policies"):
        return jsonify({"success": False, "message": "Ingen adgang."}), 403
    cid = session.get("company_id")
    if not cid:
        return jsonify({"success": False, "message": "Ingen virksomhed."}), 403
    mode = request.form.get("mode", "")
    vendor_raw = request.form.get("vendor_id", "")
    remove = request.form.get("remove")
    try:
        vendor_id = int(vendor_raw) if vendor_raw not in ("", None) else None
    except ValueError:
        return jsonify({"success": False, "message": "Ugyldig udbyder."}), 400
    try:
        if remove and vendor_id is not None:
            clear_vendor_override(current_app.mysql.connection, cid, vendor_id)
            flash("Undtagelsen for udbyderen er fjernet.", "success")
        else:
            set_policy(current_app.mysql.connection, cid, mode, vendor_id, updated_by=session.get("user_id"))
            flash("Politikken for teambestilling er gemt.", "success")
    except ValueError:
        flash("Vælg en gyldig tilstand.", "danger")
    except Exception as e:
        logger.warning("save team policy failed: %s", e)
        flash("Politikken kunne ikke gemmes.", "danger")
    return redirect(request.referrer or url_for("hr_dashboard.approval_policies"))
