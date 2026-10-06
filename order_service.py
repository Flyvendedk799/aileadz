"""
order_service.py — ONE authorized order/money service that ALL order paths call.

Consolidates the previously-divergent order paths (chatbot handler, web routes,
AI tool executors, enterprise API) behind a single, authorized, transactional
service with:

  * a shared authorization/validation surface (OrderContext),
  * budget-aware approval (NEVER silently overspend annual_budget),
  * exactly-once budget charge/refund via course_orders.budget_charged,
  * an ownership gate (anti-IDOR) for reading / cancelling orders.

DB conventions (see CLAUDE.md / db_compat.py):
  * connection lives on flask.g via current_app.mysql.connection,
  * DictCursor is the default (rows read BY COLUMN NAME),
  * autocommit=False  -> commit() manually, rollback() on error,
  * db_compat.refresh_flask_mysql_connection(mysql) heals stale connections.

This module is import-safe: it never imports anything at module scope that could
crash create_app(), and every public entry point degrades to a soft failure dict
rather than raising. All user-facing strings are Danish.
"""

import uuid
import logging
import datetime

import order_lifecycle as lc
from transaction_errors import is_retryable_lock_error, propagate_transaction_abort

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Cross-integration side effects (events / email / notifications).
#
# Business mail and in-app notices are staged in the caller's transaction.
# Transaction-aborting lock errors must propagate to its owner. Webhook fan-out
# remains post-commit and best effort, never part of a retried order attempt.
# ---------------------------------------------------------------------------
def _emit_event_safe(company_id, event_type, payload):
    """Record an integration event in the outbox. Never raises."""
    if not company_id:
        return
    try:
        from event_bus import emit_event
        emit_event(company_id, event_type, payload or {})
    except Exception as e:  # pragma: no cover - defensive
        logger.debug("order_service: emit_event(%s) skipped: %s", event_type, e)


def _send_email_safe(to_email, subject, template_name, company_id,
                     dedupe_key=None, cursor=None, **context):
    """Stage durable business mail. Transactional staging errors abort the write."""
    if not to_email:
        return
    try:
        from email_service import _resolve_branding
        from mail_delivery import enqueue
        branding = _resolve_branding(company_id)
        key = dedupe_key or ('order:%s:%s:%s' % (context.get('order_id'),template_name,context.get('decision','')) if context.get('order_id') else None)
        if key and key.startswith('budget_overrun_alert:'):
            key += ':' + datetime.date.today().isoformat()
        enqueue(to_email,subject,template_name,branding,company_id=company_id,dedupe_key=key,cursor=cursor,**context)
    except Exception as e:  # pragma: no cover - defensive
        logger.debug("order_service: email(%s) skipped: %s", template_name, e)
        if cursor is not None:
            raise


def _app_base_url():
    """Best-effort external base URL for email CTA links. '' when unknown."""
    try:
        import os
        base = (os.getenv("APP_BASE_URL") or "").strip()
        if base:
            return base.rstrip("/")
    except Exception:
        pass
    try:
        from flask import current_app
        base = (current_app.config.get("APP_BASE_URL") or "").strip()
        return base.rstrip("/") if base else ""
    except Exception:
        return ""


def _manager_recipient_emails(company_id):
    """Manager-level recipient emails for a company. Reuses the SAME helper
    digest_service uses (hr_manager / department_head / company_admin, active,
    non-empty email). Returns a de-duplicated list of address strings. Never
    raises — returns [] on any error / no DB.
    """
    cid = _int_or_none(company_id)
    if cid is None:
        return []
    conn = _get_connection()
    if conn is None:
        return []
    cur = None
    try:
        from digest_service import _recipients as _digest_recipients
        cur = _dict_cursor(conn)
        rows = _digest_recipients(cur, cid) or []
        seen = set()
        out = []
        for r in rows:
            email = (r.get("email") or "").strip()
            if email and email.lower() not in seen:
                seen.add(email.lower())
                out.append(email)
        return out
    except Exception as e:  # pragma: no cover - defensive
        logger.debug("order_service: manager recipients lookup skipped: %s", e)
        return []
    finally:
        try:
            if cur is not None:
                cur.close()
        except Exception:
            pass


# Don't re-send the same approval-needed / budget-overrun alert within this
# window even if the underlying condition persists across multiple orders.
_EMAIL_DEDUPE_HOURS = 24


def _send_approval_needed_emails_safe(company_id, *, order_id, product_title,
                                      price, department, requester, cursor=None):
    """Email the order_approval_needed template to manager-level recipients.

    Fired AFTER commit, best-effort: no-ops cleanly when SMTP is unconfigured
    (the send layer is ops-gated) and when there are no manager recipients.
    Recent-duplicate guarded per (order) so a flapping caller can't spam.
    """
    cid = _int_or_none(company_id)
    if cid is None:
        return
    try:
        from email_service import email_recently_sent
        dedupe_key = "order_approval_needed:%s" % order_id
        if cursor is None and email_recently_sent(dedupe_key, within_hours=_EMAIL_DEDUPE_HOURS,
                               company_id=cid):
            return
        recipients = _manager_recipient_emails(cid)
        if not recipients:
            return
        base = _app_base_url()
        approvals_url = (base + "/hr/approvals") if base else ""
        price_f = _to_float(price)
        amount = ("%.0f" % price_f) if price_f > 0 else ""
        for to_email in recipients:
            _send_email_safe(
                to_email,
                "Kursusbestilling afventer godkendelse",
                "order_approval_needed", cid, cursor=cursor,
                dedupe_key=dedupe_key,
                product_title=product_title or "",
                amount=amount,
                requester=requester or "",
                department=(department or "") or "",
                approvals_url=approvals_url,
            )
    except Exception as e:  # pragma: no cover - defensive
        logger.debug("order_service: approval-needed email skipped: %s", e)
        if cursor is not None:
            raise


def _send_budget_overrun_emails_safe(company_id, *, department, spent,
                                     annual_budget, order_id):
    """Email the budget_overrun_alert template to manager-level recipients.

    Fired from the charge path when a department crosses its annual budget.
    Best-effort + ops-gated + recent-duplicate guarded per (department) so a
    department that stays over budget across many orders alerts at most once per
    window.
    """
    cid = _int_or_none(company_id)
    if cid is None:
        return
    try:
        from email_service import email_recently_sent
        dept = (department or "").strip()
        dedupe_key = "budget_overrun_alert:%s:%s" % (cid, dept)
        if email_recently_sent(dedupe_key, within_hours=_EMAIL_DEDUPE_HOURS,
                               company_id=cid):
            return
        recipients = _manager_recipient_emails(cid)
        if not recipients:
            return
        spent_f = _to_float(spent)
        annual_f = _to_float(annual_budget)
        for to_email in recipients:
            _send_email_safe(
                to_email,
                "Budgetadvarsel: %s" % (dept or "afdeling"),
                "budget_overrun_alert", cid,
                dedupe_key=dedupe_key,
                department=dept,
                spent="%.0f" % spent_f,
                annual_budget="%.0f" % annual_f,
                order_id=order_id or "",
            )
    except Exception as e:  # pragma: no cover - defensive
        logger.debug("order_service: budget-overrun email skipped: %s", e)


def _person_name(cur, row):
    """The name to show for the learner of an order row: full name, else the order's own
    ``user_name``, else the username. Never raises."""
    from person_names import display_name
    username = (row.get("username") or "").strip()
    name = display_name(cur, _int_or_none(row.get("company_id")), user_id=_int_or_none(row.get("user_id")),
                        username=username or None)
    if (not name or name == username) and (row.get("user_name") or "").strip():
        return row["user_name"].strip()
    return name or username


def _approval_recipients(cur, company_id, requester_user_id, department):
    """Who is told in-app that an order awaits approval: HR/company admins, the requester's
    own manager and the head of the requester's department. One entry per person."""
    from notification_service import role_recipients, HR_ROLES
    people = {}
    for r in role_recipients(cur, company_id, HR_ROLES):
        people[_int_or_none(r.get("user_id"))] = r
    try:
        cur.execute(
            "SELECT cu.user_id AS user_id, COALESCE(u.username, cu.username) AS username "
            "FROM company_users cu LEFT JOIN users u ON u.id = cu.user_id "
            "WHERE cu.company_id = %s AND cu.status = 'active' AND ("
            " cu.user_id = (SELECT m.manager_user_id FROM company_users m WHERE m.company_id = %s AND m.user_id = %s LIMIT 1)"
            " OR (cu.role = 'department_head' AND cu.department = %s AND cu.department <> ''))",
            (company_id, company_id, requester_user_id, department or ""),
        )
        for r in cur.fetchall() or []:
            uid = _int_or_none(r.get("user_id") if isinstance(r, dict) else r[0])
            if uid is not None and uid not in people:
                people[uid] = {"user_id": uid, "username": r.get("username") if isinstance(r, dict) else r[1]}
    except Exception as e:
        propagate_transaction_abort(e)
        logger.debug("order_service: manager recipients skipped: %s", e)
    return [r for r in people.values() if r.get("username")]


def _notify_approvers_safe(cur, company_id, requester_user_id, department, title, message, *,
                           action_url=None, dedupe_key=None):
    """Urgent in-app card for everyone who can decide this approval (see ``_approval_recipients``);
    nobody gets it twice and the requester never gets it about their own order. Never raises."""
    if not company_id:
        return
    try:
        from notification_service import notify_user
        for r in _approval_recipients(cur, company_id, requester_user_id, department):
            notify_user(cur, title=str(title)[:255], message=str(message), username=r["username"],
                        user_id=r.get("user_id"), company_id=company_id, kind="order", is_urgent=True,
                        action_url=action_url, dedupe_key=dedupe_key, dedupe_hours=24 if dedupe_key else None,
                        actor_user_id=requester_user_id)
    except Exception as e:  # pragma: no cover - defensive
        propagate_transaction_abort(e)
        logger.debug("order_service: approver notification skipped: %s", e)


def _notify_company_admins_safe(cur, company_id, title, message, is_urgent=0,
                                action_url=None, dedupe_key=None):
    """In-app card for the company's HR/admins. Never raises.

    Goes through the unified notification service (one row per recipient with
    its own read state). Runs on the caller's cursor so it commits atomically
    with the order transaction.
    """
    if not company_id:
        return
    try:
        from notification_service import notify_roles
        notify_roles(
            cur, company_id, ("company_admin", "hr_manager"),
            title=str(title)[:255], message=str(message),
            kind="order", is_urgent=bool(is_urgent), action_url=action_url,
            dedupe_key=dedupe_key, dedupe_hours=24 if dedupe_key else None,
        )
    except Exception as e:  # pragma: no cover - defensive
        propagate_transaction_abort(e)
        logger.debug("order_service: company notification skipped: %s", e)


def _notify_assignee_safe(cur, ctx, actor_ctx, order_id, product_title, assigner_name, *, approved):
    """The one in-app card the learner gets for an assigned course. Never raises."""
    try:
        from notification_service import notify_user
        notify_user(
            cur, user_id=ctx.user_id, username=ctx.username, company_id=ctx.company_id,
            kind="assignment", sender_user_id=actor_ctx.user_id,
            title=("Du er tildelt %s" % product_title)[:255],
            message=("%s har tildelt dig kurset. Det er godkendt, og HR eller udbyderen bekræfter din plads."
                     % (assigner_name or "HR")) if approved else
                    ("%s har tildelt dig kurset. Bestillingen afventer godkendelse, fordi afdelingens budget ikke rækker."
                     % (assigner_name or "HR")),
            action_url=order_url(order_id, absolute=False),
            dedupe_key="assigned:%s" % order_id, dedupe_hours=None,
        )
    except Exception as e:  # pragma: no cover - defensive
        propagate_transaction_abort(e)
        logger.debug("order_service: assignee notification skipped: %s", e)


# ---------------------------------------------------------------------------
# Roles considered "manager-level" — these can view/manage company orders and
# do NOT need approval for their own orders. Everyone else (employee / unknown)
# is treated as an employee and routed through approval.
# ---------------------------------------------------------------------------
_MANAGER_ROLES = frozenset({
    "department_head", "hr_manager", "company_admin", "admin", "manager",
})

# Statuses that mean "this order consumes budget right now".
# An order in pending_approval has NOT yet consumed budget (it is charged on
# approval). Everything else that is live consumes budget on creation.
_NON_CHARGING_STATUSES = frozenset({lc.PENDING_APPROVAL})
_CANCELLED_LIKE_STATUSES = frozenset({lc.CANCELLED, lc.REJECTED})

# Don't create a second open order for the same person + course + date within
# this many minutes (closes the chat "ja" + stale Bekræft card double-confirm).
_DUPLICATE_WINDOW_MINUTES = 10


# ---------------------------------------------------------------------------
# OrderContext — captures the actor identity for every order operation.
# ---------------------------------------------------------------------------
class OrderContext:
    """Actor identity + provenance for an order operation.

    Use the classmethods to build it from the Flask session (web/chat/tool
    paths) or from the enterprise-API ``flask.g`` (api path). Never trust caller
    -supplied identity fields; always rebuild from the trusted context.
    """

    __slots__ = (
        "company_id", "user_id", "username", "company_role",
        "department", "source", "actor_kind", "vendor_id", "is_platform_admin",
        "actor_label",
    )

    def __init__(self, company_id=None, user_id=None, username=None,
                 company_role=None, department=None, source="web",
                 actor_kind="user", vendor_id=None, is_platform_admin=False,
                 actor_label=None):
        self.company_id = _int_or_none(company_id)
        self.user_id = _int_or_none(user_id)
        self.username = (username or "").strip() or None
        self.company_role = (company_role or "").strip().lower() or None
        self.department = (department or "").strip() or None
        self.source = source or "web"
        # actor_kind: 'user' (learner/HR), 'vendor' (portal), 'system' (policy/jobs).
        self.actor_kind = actor_kind or "user"
        self.vendor_id = _int_or_none(vendor_id)
        self.is_platform_admin = bool(is_platform_admin)
        self.actor_label = (actor_label or self.username or "").strip() or None

    # -- builders -----------------------------------------------------------
    @classmethod
    def from_session(cls, source="web"):
        """Build from the Flask session (chat / web / tool paths)."""
        try:
            from flask import session
        except Exception:
            session = {}
        try:
            is_admin = session.get("role") == "admin"
            return cls(
                company_id=session.get("company_id"),
                user_id=session.get("user_id"),
                username=session.get("user") or session.get("username") or "guest",
                company_role=session.get("company_role", "employee"),
                department=session.get("company_department", ""),
                source=source,
                is_platform_admin=is_admin,
            )
        except Exception:
            # Outside request context / broken session — degrade to anonymous.
            return cls(source=source)

    @classmethod
    def for_vendor(cls, vendor_id, label=None, source="vendor"):
        """A vendor acting from the portal on ITS OWN orders only."""
        return cls(vendor_id=vendor_id, actor_kind="vendor", source=source,
                   actor_label=label or ("vendor:%s" % vendor_id))

    @classmethod
    def system(cls, company_id=None, source="system", label="system"):
        """Automated actor (auto-approval policy, scheduled jobs)."""
        return cls(company_id=company_id, actor_kind="system", source=source,
                   company_role="company_admin", actor_label=label)

    @classmethod
    def from_api_g(cls, source="api", company_role="company_admin"):
        """Build from the enterprise-API ``flask.g``.

        The API key authenticates a *company*, not an individual employee, so we
        treat the API actor as manager-level by default (``company_admin``) — the
        same way a department head ordering on someone's behalf would be. This
        identifies who may request the operation. The canonical enrolment wrapper
        resolves a real employee and applies that employee's approval policy;
        the API actor does not bypass the employee's budget or approval rules.
        """
        try:
            from flask import g
            company_id = getattr(g, "company_id", None)
        except Exception:
            company_id = None
        return cls(
            company_id=company_id,
            user_id=None,
            username=None,
            company_role=company_role,
            department=None,
            source=source,
        )

    # -- helpers ------------------------------------------------------------
    @property
    def is_manager(self):
        return (self.company_role or "") in _MANAGER_ROLES

    @property
    def is_employee(self):
        """Employee == explicit 'employee' role OR unknown/missing role."""
        return not self.is_manager

    def to_dict(self):
        return {
            "company_id": self.company_id,
            "user_id": self.user_id,
            "username": self.username,
            "company_role": self.company_role,
            "department": self.department,
            "source": self.source,
        }


# ---------------------------------------------------------------------------
# Internal utilities
# ---------------------------------------------------------------------------
def _int_or_none(v):
    if v is None or v == "":
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _to_float(v):
    if v is None:
        return 0.0
    try:
        return float(v)
    except (TypeError, ValueError):
        # Tolerate "1.234,50 kr." style strings.
        import re
        cleaned = re.sub(r"[^\d,.\-]", "", str(v)).replace(".", "").replace(",", ".") \
            if ("," in str(v) and "." in str(v)) else re.sub(r"[^\d.\-]", "", str(v).replace(",", "."))
        try:
            return float(cleaned)
        except (TypeError, ValueError):
            return 0.0


def _get_connection():
    """Return a healed MySQL connection or None (never raises)."""
    try:
        from flask import current_app
        mysql = getattr(current_app, "mysql", None)
        if not mysql:
            return None
        try:
            from db_compat import refresh_flask_mysql_connection
            refresh_flask_mysql_connection(mysql)
        except Exception:
            pass
        return mysql.connection
    except Exception as e:
        logger.warning("order_service: no db connection: %s", e)
        return None


def _dict_cursor(conn):
    """Return a DictCursor (rows read by column name)."""
    try:
        import MySQLdb.cursors
        return conn.cursor(MySQLdb.cursors.DictCursor)
    except Exception:
        # Fall back to the default cursor (which is DictCursor in this app).
        return conn.cursor()


def _fiscal_year_of(created_at):
    """Fiscal year = year of the order's created_at (NOT current year)."""
    if isinstance(created_at, (datetime.datetime, datetime.date)):
        return created_at.year
    if created_at:
        try:
            return datetime.datetime.fromisoformat(str(created_at)).year
        except Exception:
            pass
    return datetime.datetime.now().year


def _write_audit(cur, *, company_id, user_id, action, resource_id, description=""):
    """Best-effort audit row. Guarded — never breaks the surrounding tx.

    Canonical columns are ``action_type`` + ``details``; the legacy duplicates
    ``action`` + ``description`` are still filled so older readers keep working
    (N-3.4: one meaning per column pair).
    """
    try:
        cur.execute(
            """
            INSERT INTO audit_log
                (company_id, user_id, action, action_type, resource_type,
                 resource_id, description, details)
            VALUES (%s, %s, %s, %s, 'order', %s, %s, %s)
            """,
            (company_id, user_id, action, action, str(resource_id), description,
             description),
        )
    except Exception as e:  # pragma: no cover - audit must never fail the op
        propagate_transaction_abort(e)
        logger.debug("order_service: audit_log skipped (%s): %s", action, e)


def _record_history(cur, row, *, kind, from_value, to_value, ctx, note=None):
    """Append to order_status_history (who changed what, when). Never raises."""
    try:
        label = ctx.actor_label or ctx.username or ""
        if ctx.actor_kind == "user" and ctx.user_id and (not ctx.actor_label or ctx.actor_label == ctx.username):
            # People read names, not login handles ("Hanne HR", not "hr").
            from person_names import display_name
            label = display_name(cur, _int_or_none(row.get("company_id")) or ctx.company_id,
                                 user_id=ctx.user_id, username=label or None, default=label)
        cur.execute(
            """
            INSERT INTO order_status_history
                (order_id, company_id, kind, from_value, to_value, actor_user_id,
                 actor_kind, actor_label, note)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (row.get("order_id"), _int_or_none(row.get("company_id")), kind,
             from_value, to_value, ctx.user_id, _actor_kind_label(ctx),
             label[:255],
             (note or None) and str(note)[:500]),
        )
    except Exception as e:  # pragma: no cover - history must never fail the op
        propagate_transaction_abort(e)
        logger.debug("order_service: history skipped: %s", e)


def _actor_kind_label(ctx):
    if ctx.actor_kind == "vendor":
        return "vendor"
    if ctx.actor_kind == "system":
        return "system"
    if ctx.is_platform_admin:
        return "admin"
    return "manager" if ctx.is_manager else "user"


def _resolve_approval_policy(cur, company_id, department):
    """Resolve the active auto-approval policy for one company (optionally one
    department). STRICTLY company_id-scoped — one company's policy can never
    affect another's.

    Precedence: a department-specific active policy wins over the company-wide
    (department IS NULL) default. Returns a dict with float thresholds, or None
    when no active policy exists / on any error (callers fall back to the prior
    employee-approval behaviour).

        {"auto_approve_under": float, "require_approval_over": float | None}
    """
    cid = _int_or_none(company_id)
    if cid is None:
        return None
    dept = (department or "").strip()
    try:
        # Department-specific policy first (most specific), then company-wide
        # (department IS NULL). LIMIT 1 — the most specific active row wins.
        cur.execute(
            """
            SELECT auto_approve_under, require_approval_over, department
            FROM company_approval_policies
            WHERE company_id = %s
              AND is_active = 1
              AND (department = %s OR department IS NULL)
            ORDER BY (department IS NULL) ASC
            LIMIT 1
            """,
            (cid, dept),
        )
        row = cur.fetchone()
    except Exception as e:
        propagate_transaction_abort(e)
        logger.warning("order_service: approval policy lookup failed: %s", e)
        return None
    if not row:
        return None

    auto_under = _to_float(row.get("auto_approve_under"))
    raw_over = row.get("require_approval_over")
    require_over = _to_float(raw_over) if raw_over is not None else None
    return {
        "auto_approve_under": auto_under,
        "require_approval_over": require_over,
    }


def actors_for(ctx, order_row):
    """Which roles ``ctx`` plays on this order: a subset of
    ``{'owner', 'manager', 'vendor', 'admin', 'system'}``.

    * ``admin``   – platform admin (any company, any order).
    * ``system``  – automation (auto-approval policy, scheduled jobs).
    * ``vendor``  – a vendor, but ONLY for orders carrying its own vendor_id.
    * ``owner``   – the learner who placed it.
    * ``manager`` – HR / company admin / department head of the SAME company
      (a department head only for orders in their own department).
    """
    actors = set()
    if not order_row:
        return actors
    if ctx.actor_kind == "system":
        actors.add("system")
    if ctx.is_platform_admin:
        actors.add("admin")

    if ctx.actor_kind == "vendor":
        row_vendor = _int_or_none(order_row.get("vendor_id"))
        if ctx.vendor_id is not None and row_vendor is not None and ctx.vendor_id == row_vendor:
            actors.add("vendor")
        return actors

    row_user_id = _int_or_none(order_row.get("user_id"))
    row_username = (order_row.get("username") or "").strip() or None
    row_company_id = _int_or_none(order_row.get("company_id"))
    if ctx.user_id is not None and row_user_id is not None and ctx.user_id == row_user_id:
        actors.add("owner")
    elif ctx.username and row_username and ctx.username == row_username:
        actors.add("owner")

    if (ctx.company_id is not None and row_company_id is not None
            and ctx.company_id == row_company_id and ctx.is_manager):
        if ctx.company_role == "department_head" and ctx.department:
            # Interim department scoping until the S-2.3 matrix lands.
            if (order_row.get("department") or "").strip().lower() == ctx.department.lower():
                actors.add("manager")
        else:
            actors.add("manager")
    return actors


def _lock_order(cur, ctx, order_id):
    """``SELECT ... FOR UPDATE`` one order. A company-bound actor (not a platform
    admin, vendor or system job) only ever locks rows of ITS OWN company (or
    company-less personal orders), so a guessed order id of another tenant is
    never even read (S-1.7 tenant isolation)."""
    if (ctx.company_id is not None and not ctx.is_platform_admin
            and ctx.actor_kind not in ("vendor", "system")):
        cur.execute("SELECT * FROM course_orders WHERE order_id = %s "
                    "AND (company_id = %s OR company_id IS NULL) FOR UPDATE", (order_id, ctx.company_id))
    else:
        cur.execute("SELECT * FROM course_orders WHERE order_id = %s FOR UPDATE", (order_id,))


def _ownership_ok(ctx, order_row):
    """Read/act gate shared by get_order and friends: true when ctx plays any
    role on the order. Cross-tenant / other-user => False (callers 404)."""
    return bool(actors_for(ctx, order_row))


# ---------------------------------------------------------------------------
# create_order
# ---------------------------------------------------------------------------
ORDER_DETAIL_URL = "/min-ordre/%s"

# ``order_approvals.notes`` of an order a manager assigned (approved at assignment).
ASSIGNMENT_APPROVAL_NOTE = lc.ASSIGNMENT_APPROVAL_NOTE


def order_url(order_id, absolute=True):
    path = ORDER_DETAIL_URL % order_id
    if absolute:
        base = _app_base_url()
        return (base + path) if base else path
    return path


def _parse_deadline(value):
    """Best-effort ISO date (YYYY-MM-DD) from a variant date / path due date."""
    if not value:
        return None
    if isinstance(value, (datetime.datetime, datetime.date)):
        return value.strftime("%Y-%m-%d")
    import re
    s = str(value).strip()
    m = re.search(r"(\d{4})-(\d{2})-(\d{2})", s)
    if m:
        return "%s-%s-%s" % m.groups()
    m = re.search(r"(\d{1,2})[./-](\d{1,2})[./-](\d{4})", s)
    if m:
        d, mo, y = m.groups()
        try:
            return datetime.date(int(y), int(mo), int(d)).strftime("%Y-%m-%d")
        except ValueError:
            return None
    return None


def _vendor_id_for_handle(cur, product_handle):
    """Resolve the vendors.id that supplies a catalog handle (via the catalog's
    vendor name/slug). None when the vendor has no portal account."""
    if not product_handle:
        return None
    try:
        import catalog_service
        product = catalog_service.get_product(product_handle)
        if not product:
            return None
        name = (product.get("vendor") or "").strip()
        slug = product.get("vendor_slug") or ""
        cur.execute(
            "SELECT id FROM vendors WHERE slug = %s OR LOWER(vendor_name) = LOWER(%s) LIMIT 1",
            (slug, name),
        )
        r = cur.fetchone()
        return _int_or_none(r.get("id") if isinstance(r, dict) else (r[0] if r else None))
    except Exception as e:
        logger.debug("order_service: vendor lookup skipped: %s", e)
        return None


def _find_recent_duplicate(cur, ctx, product_handle, variant_date, variant_location=""):
    """An equivalent still-open order for the same person within the window."""
    if not product_handle or not (ctx.username or ctx.user_id):
        return None
    try:
        cur.execute(
            """
            SELECT order_id, status, price FROM course_orders
            WHERE COALESCE(company_id,0) = %s AND COALESCE(variant_location,'') = %s AND product_handle = %s AND COALESCE(variant_date, '') = %s
              AND (username = %s OR (user_id IS NOT NULL AND user_id = %s))
              AND status IN ('pending_approval', 'approved', 'booked')
              AND created_at >= DATE_SUB(NOW(), INTERVAL %s MINUTE)
            ORDER BY created_at DESC LIMIT 1 FOR UPDATE
            """,
            (ctx.company_id or 0,variant_location or "",product_handle, variant_date or "", ctx.username, ctx.user_id,
             _DUPLICATE_WINDOW_MINUTES),
        )
        return cur.fetchone()
    except Exception as e:
        propagate_transaction_abort(e)
        logger.debug("order_service: duplicate check skipped: %s", e)
        return None


def _next_step_message(status, needs_approval):
    """Honest Danish copy about what happens next. No payment details, ever."""
    if needs_approval or status == lc.PENDING_APPROVAL:
        return ("Din bestilling er sendt til godkendelse. Du hører fra os, så snart den er "
                "behandlet, og kan følge status på din tidslinje.")
    return ("Bestillingen er godkendt. Udbyderen bekræfter din plads, og du får besked, når den er "
            "booket. Fakturering sker uden for appen.")


def _undo_create(conn, cur, use_savepoint):
    """Undo a failed create_order: only its own writes when it runs inside the caller's
    transaction (savepoint), else the whole transaction."""
    if use_savepoint and cur is not None:
        cur.execute("ROLLBACK TO SAVEPOINT order_service_create")
    else:
        conn.rollback()


def create_order(ctx, **kwargs):
    """Retry only a fully rolled-back standalone transaction, never half a batch.

    MySQL gap locks can deadlock concurrent requests even when the participant
    and budget locks are correct. All business mail is staged transactionally;
    external events run only after a successful commit, so replaying an aborted
    attempt cannot double-charge or send a second confirmation.
    """
    for attempt in range(3):
        result = _create_order_once(ctx, **kwargs)
        retryable = result.pop('_retryable_lock_error', False)
        if not retryable or kwargs.get('deferred_events') is not None:
            return result
        if attempt < 2:
            import time
            time.sleep(0.025 * (attempt + 1))
    return result


def _create_order_once(ctx, *, product_handle, product_title, price,
                 variant_date="", variant_location="",
                 user_email="", user_name="", user_phone="",
                 status=None, extra=None, deferred_events=None):
    """Create an order through the single authorized path.

    One transaction (single connection, commit once, rollback on error):
      1. Idempotency guard: an equivalent open order within the last few minutes
         is returned instead of creating a duplicate (chat double-confirm).
      2. Resolve needs_approval (employees / unknown role -> approval), apply the
         company auto-approval policy, then the budget safety rule (never
         silently overspend).
      3. INSERT course_orders with status pending_approval | approved (vendor_id,
         completion_deadline and group_order_id filled in).
      4. If needs_approval: INSERT order_approvals(status='pending').
      5. Charge budget EXACTLY ONCE when the order is live (not pending_approval).
      6. History row, audit row, in-app cards, ONE confirmation email.

    Returns dict: {success, order_id, status, needs_approval, budget_warning,
    status_label, next_step, order_url, duplicate?...}
    """
    extra = extra or {}
    acting_ctx = ctx
    price_f = _to_float(price)
    order_id = str(uuid.uuid4())

    # HR or a manager assigns a course to an employee (compliance, learning paths,
    # team orders): the order is placed as that employee. Assigned by a signed-in
    # manager = approved at once (nobody approves their own assignment); the budget
    # is still charged and the assigner is recorded as the approver. The budget
    # overspend rule below can still route an assignment to approval. API keys and
    # system jobs carry no user id, so they never pre-approve.
    assign = extra.get("assign_to")
    pre_approved_by = None
    assigner_ctx = None
    if assign:
        assigner_ctx = acting_ctx
        assigner = ctx.actor_label or ctx.username or "HR"
        if (acting_ctx.actor_kind == "user" and acting_ctx.is_manager and acting_ctx.user_id
                and acting_ctx.company_id):
            pre_approved_by = acting_ctx.user_id
        ctx = OrderContext(company_id=ctx.company_id, user_id=assign.get("user_id"),
                           username=assign.get("username"), company_role="employee",
                           department=assign.get("department"), source=ctx.source,
                           actor_label=assigner)
        user_email = user_email or assign.get("email") or ""
        user_name = user_name or assign.get("name") or assign.get("username") or ""
        extra = dict(extra)

    conn = _get_connection()
    if conn is None:
        return {
            "success": False,
            "error": "no_db",
            "message": "Databasen er ikke tilgængelig lige nu. Prøv igen senere.",
        }

    cur = None
    # ``extra["savepoint"]``: the caller already has a transaction open (a path step ordered while an
    # order is being completed). A failure here must undo only this order, never the caller's work.
    use_savepoint = bool(extra.get("savepoint"))
    try:
        cur = _dict_cursor(conn)
        if use_savepoint:
            cur.execute("SAVEPOINT order_service_create")

        assigner_name = None
        if assigner_ctx is not None:
            from person_names import display_name
            assigner_name = display_name(cur, assigner_ctx.company_id, user_id=assigner_ctx.user_id,
                                         username=assigner_ctx.username,
                                         default=assigner_ctx.actor_label or "HR")
            extra.setdefault("notes", "Tildelt af %s" % assigner_name)

        assignment_step = extra.get('assignment_step_id')
        if assignment_step:
            cur.execute("SELECT * FROM learning_assignment_steps WHERE id = %s AND company_id = %s AND user_id = %s FOR UPDATE", (assignment_step, ctx.company_id, ctx.user_id))
            step = cur.fetchone()
            if not step or step.get('course_handle') != product_handle:
                _undo_create(conn, cur, use_savepoint)
                return {'success': False, 'error': 'assignment_not_found', 'message': 'Tildelingen blev ikke fundet.'}
            if step.get('order_id'):
                cur.execute("SELECT status FROM course_orders WHERE order_id = %s AND company_id = %s", (step['order_id'],ctx.company_id))
                existing = cur.fetchone()
                if existing and existing['status'] not in ('cancelled','rejected'):
                    _undo_create(conn, cur, use_savepoint)
                    return {'success': True, 'duplicate': True, 'order_id': step['order_id'], 'status': existing['status'], 'order_url': order_url(step['order_id'])}

        # Serialize duplicate detection for the same participant, including two
        # simultaneous confirmations from different application workers.
        if ctx.user_id:
            cur.execute('SELECT id FROM users WHERE id=%s FOR UPDATE',(ctx.user_id,))
            cur.fetchone()
        # --- 0. idempotency ------------------------------------------------
        dup = _find_recent_duplicate(cur, ctx, product_handle, variant_date, variant_location)
        if dup:
            dup_id = dup.get("order_id") if isinstance(dup, dict) else dup[0]
            dup_status = lc.normalize_status(dup.get("status") if isinstance(dup, dict) else dup[1])
            if assignment_step:
                cur.execute("UPDATE learning_assignment_steps SET order_id = %s, status = 'ordered', last_error = NULL WHERE id = %s", (dup_id,assignment_step))
                if deferred_events is None:
                    conn.commit()
            return {
                "success": True,
                "duplicate": True,
                "order_id": dup_id,
                "status": dup_status,
                "status_label": lc.status_label(dup_status),
                "needs_approval": dup_status == lc.PENDING_APPROVAL,
                "auto_approved": False,
                "budget_warning": None,
                "budget_charged": False,
                "price": _to_float(dup.get("price")) if isinstance(dup,dict) else price_f,
                "next_step": _next_step_message(dup_status, dup_status == lc.PENDING_APPROVAL),
                "order_url": order_url(dup_id),
                "message": "Du har allerede anmodet om dette kursus. Se status på din tidslinje.",
            }

        # --- 1. needs_approval (preserve current behaviour) ---------------
        # Employees (or unknown role) with a company need approval; managers/
        # admins and non-company (anonymous) orders do not.
        needs_approval = bool(ctx.company_id) and ctx.is_employee
        pre_approved = bool(pre_approved_by) and needs_approval
        if pre_approved:
            needs_approval = False

        dept = ctx.department or extra.get("department") or ""

        # --- 1b. AUTO-APPROVAL policy layer (runs BEFORE budget check) -----
        # A company can configure a per-company (optionally per-department)
        # auto-approval threshold. Orders at/under auto_approve_under are
        # auto-approved (needs_approval cleared); orders at/over
        # require_approval_over are always routed to approval. The budget
        # overspend safety rule below can still RE-force approval even when a
        # policy auto-approved — safety first, it only ever tightens.
        auto_approved_by_policy = False
        if ctx.company_id and price_f > 0 and not pre_approved:
            policy = _resolve_approval_policy(cur, ctx.company_id, dept)
            if policy:
                require_over = policy.get("require_approval_over")
                auto_under = policy.get("auto_approve_under") or 0.0
                if require_over is not None and require_over > 0 and price_f >= require_over:
                    # Over the hard ceiling -> always needs approval.
                    needs_approval = True
                elif auto_under > 0 and price_f <= auto_under:
                    # Under the auto-approve threshold -> auto-approve.
                    needs_approval = False
                    auto_approved_by_policy = True

        # --- 2. budget-aware approval ------------------------------------
        budget_warning = None
        fiscal_year = datetime.datetime.now().year
        budget_row = None
        if ctx.company_id and dept and price_f > 0:
            try:
                cur.execute(
                    """
                    SELECT id, annual_budget, spent FROM department_budgets
                    WHERE company_id = %s AND department = %s AND fiscal_year = %s FOR UPDATE
                    """,
                    (ctx.company_id, dept, fiscal_year),
                )
                budget_row = cur.fetchone()
            except Exception as be:
                logger.warning("order_service: budget lookup failed: %s", be)
                raise

            if budget_row:
                annual = _to_float(budget_row.get("annual_budget"))
                spent = _to_float(budget_row.get("spent"))
                remaining = annual - spent
                if annual > 0 and (spent + price_f) > annual:
                    # Do NOT silently overspend — route to approval. This is the
                    # safety rule: it overrides any auto-approval the policy
                    # layer granted above (safety first).
                    needs_approval = True
                    auto_approved_by_policy = False
                    pre_approved = False
                    budget_warning = (
                        f"Bestillingen på {price_f:.0f} kr. overskrider afdelingens "
                        f"resterende budget på {remaining:.0f} kr. "
                        f"(brugt {spent:.0f} af {annual:.0f} kr.). "
                        f"Ordren er sendt til godkendelse i stedet for at blive afvist."
                    )

        # --- 3. resolve status & INSERT course_orders --------------------
        if needs_approval:
            initial_status = lc.PENDING_APPROVAL
        else:
            # Auto-approved by policy, or a manager/solo order that needs no
            # approval: the order is APPROVED and waits for the vendor to book.
            initial_status = lc.normalize_status(status) if status else lc.APPROVED
            if initial_status == lc.PENDING_APPROVAL:
                initial_status = lc.APPROVED

        # Charge now only if the order is NOT in a non-charging (approval) state
        # AND there is a department budget row to charge against and a price.
        charge_now = (
            initial_status not in _NON_CHARGING_STATUSES
            and bool(ctx.company_id) and bool(dept) and price_f > 0
            and budget_row is not None
        )
        budget_charged = 1 if charge_now else 0

        vendor_id = _int_or_none(extra.get("vendor_id")) or _vendor_id_for_handle(cur, product_handle)
        deadline = _parse_deadline(extra.get("completion_deadline")) or _parse_deadline(variant_date)
        group_order_id = (extra.get("group_order_id") or None)

        cur.execute(
            """
            INSERT INTO course_orders
                (order_id, company_id, user_id, username, product_handle,
                 product_title, price, variant_date, variant_location, status,
                 department, user_email, user_name, user_phone,
                 chatbot_session_id, chatbot_queries_before_order,
                 recommended_by_tool, budget_charged, vendor_id,
                 completion_deadline, group_order_id, request_notes, billing_status, created_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                    %s, %s, %s, %s, %s, %s, %s, %s, 'not_invoiced', NOW())
            """,
            (
                order_id, ctx.company_id, ctx.user_id, ctx.username,
                product_handle, product_title, price_f,
                variant_date or "", variant_location or "", initial_status,
                dept, user_email or "", user_name or "", user_phone or "",
                extra.get("chatbot_session_id", ""),
                _int_or_none(extra.get("chatbot_queries_before_order")) or 0,
                extra.get("recommended_by_tool", ""),
                budget_charged, vendor_id, deadline, group_order_id,
                (extra.get("notes") or None) and str(extra.get("notes"))[:2000],
            ),
        )

        if pre_approved:
            # Assigned by a manager = approved at assignment. Record who approved so the
            # order page can say so; no pending approval row is ever created.
            cur.execute("UPDATE course_orders SET approved_by = %s WHERE order_id = %s", (pre_approved_by, order_id))
            cur.execute(
                "INSERT INTO order_approvals (order_id, company_id, requester_user_id, approver_user_id, "
                "status, notes, decided_at) VALUES (%s, %s, %s, %s, 'approved', %s, NOW())",
                (order_id, ctx.company_id, ctx.user_id, pre_approved_by, ASSIGNMENT_APPROVAL_NOTE),
            )

        if extra.get("quote"):
            import json
            quote = extra["quote"]
            cur.execute("INSERT INTO course_order_details (order_id, company_id, user_id, session_id, quote_json) VALUES (%s, %s, %s, %s, %s)",
                        (order_id, ctx.company_id, ctx.user_id, quote.get("session_id"), json.dumps(quote, ensure_ascii=False)))
            if quote.get("internal_course_id"):
                cur.execute("UPDATE course_orders SET internal_course_id = %s, course_source = 'internal' WHERE order_id = %s",
                            (quote["internal_course_id"], order_id))

        if assignment_step:
            cur.execute("UPDATE learning_assignment_steps SET order_id = %s, status = 'ordered', last_error = NULL WHERE id = %s", (order_id,assignment_step))

        # --- 4. approval row ---------------------------------------------
        if needs_approval and ctx.company_id:
            try:
                cur.execute(
                    """
                    INSERT INTO order_approvals
                        (order_id, company_id, requester_user_id, status)
                    VALUES (%s, %s, %s, 'pending')
                    """,
                    (order_id, ctx.company_id, ctx.user_id),
                )
            except Exception as ae:
                logger.warning("order_service: approval insert failed: %s", ae)
                raise

        # --- 5. charge budget EXACTLY ONCE -------------------------------
        if charge_now and budget_row is not None:
            try:
                cur.execute(
                    "UPDATE department_budgets SET spent = spent + %s WHERE id = %s",
                    (price_f, budget_row["id"]),
                )
            except Exception as ce:
                logger.warning("order_service: budget charge failed: %s", ce)
                raise

        # --- 6. history + audit -------------------------------------------
        _new_row = {"order_id": order_id, "company_id": ctx.company_id}
        _record_history(cur, _new_row, kind="status", from_value=None, to_value=initial_status,
                        ctx=acting_ctx,
                        note=("Tildelt af %s · godkendt ved tildeling" % assigner_name if pre_approved
                              else "Tildelt af %s" % assigner_name if assigner_name
                              else "Auto-godkendt via politik" if auto_approved_by_policy else None))
        _audit_desc = f"{product_title} ({ctx.source})"
        if auto_approved_by_policy:
            _audit_desc += " [auto-godkendt via politik]"
        _write_audit(
            cur,
            company_id=ctx.company_id,
            user_id=acting_ctx.user_id,
            action="order.auto_approved" if auto_approved_by_policy else "order.created",
            resource_id=order_id,
            description=_audit_desc,
        )

        # In-app notification to HR/admins when an order needs their approval
        # (shares this transaction so it commits atomically with the order).
        requester_name = None
        if needs_approval and ctx.company_id:
            from person_names import display_name
            requester_name = display_name(cur, ctx.company_id, user_id=ctx.user_id, username=ctx.username,
                                          default=user_name or "en medarbejder")
            _notify_approvers_safe(
                cur, ctx.company_id, ctx.user_id, dept,
                "Ny bestilling afventer godkendelse",
                f"{product_title} er bestilt af {requester_name} og afventer godkendelse.",
                action_url="/hr/approvals",
                dedupe_key="approval-needed:%s" % order_id,
            )

        # The assigned learner hears about it once, in the app ("Du er tildelt ..."). Path
        # steps are announced by the path's own notification instead.
        if assigner_ctx is not None and not assignment_step and ctx.user_id:
            _notify_assignee_safe(cur, ctx, acting_ctx, order_id, product_title, assigner_name,
                                  approved=not needs_approval)

        if user_email:
            # ONE confirmation email (order_handler no longer sends its own).
            _send_email_safe(
                user_email, f"Ordrebekræftelse — {product_title}",
                "order_confirmation", ctx.company_id, cursor=cur,
                product_title=product_title, order_id=order_id,
                recipient_name=user_name or ctx.username or "",
                status_line=lc.status_label(initial_status),
                next_step=_next_step_message(initial_status, needs_approval),
                order_url=order_url(order_id),
            )
        # Approval-needed: email the managers who can act on it, so the decision
        # doesn't sit behind a login. Mirrors the in-app card above; best-effort
        # + ops-gated + recent-duplicate guarded.
        if needs_approval and ctx.company_id:
            _send_approval_needed_emails_safe(
                ctx.company_id,
                order_id=order_id,
                product_title=product_title,
                price=price_f,
                department=dept,
                requester=requester_name or user_name or ctx.username or "", cursor=cur,
            )

        if not needs_approval:
            _notify_vendor_safe({'order_id':order_id,'vendor_id':vendor_id,'product_title':product_title,
                                 'variant_date':variant_date,'variant_location':variant_location,
                                 'user_name':user_name or ctx.username,'company_id':ctx.company_id},cursor=cur)

        if deferred_events is None:
            conn.commit()

        def emit(company_id, event_type, payload):
            if deferred_events is None:
                _emit_event_safe(company_id,event_type,payload)
            else:
                deferred_events.append((company_id,event_type,payload))

        # --- 7. cross-integration side effects (post-commit, best-effort) ---
        if needs_approval:
            _event_type = "order.needs_approval"
        elif auto_approved_by_policy:
            _event_type = "order.auto_approved"
        else:
            _event_type = "order.created"
        emit(
            ctx.company_id,
            _event_type,
            {"order_id": order_id, "product_title": product_title,
             "price": price_f, "status": initial_status,
             "department": dept, "source": ctx.source,
             "auto_approved": auto_approved_by_policy},
        )
        if not needs_approval:
            # An approved order also announces itself as order.approved so
            # webhook subscribers get the same signal as a manual approval.
            emit(ctx.company_id, "order.approved", {
                "order_id": order_id, "product_title": product_title,
                "status": initial_status, "previous_status": None,
                "department": dept, "user_email": user_email or None,
                "auto_approved": auto_approved_by_policy})


        return {
            "success": True,
            "order_id": order_id,
            "status": initial_status,
            "status_label": lc.status_label(initial_status),
            "needs_approval": needs_approval,
            "auto_approved": auto_approved_by_policy,
            "budget_warning": budget_warning,
            "budget_charged": bool(budget_charged),
            "pre_approved": bool(pre_approved),
            "price": price_f,
            "vendor_id": vendor_id,
            "next_step": _next_step_message(initial_status, needs_approval),
            "order_url": order_url(order_id),
        }
    except Exception as e:
        logger.error("order_service.create_order failed: %s", e)
        try:
            _undo_create(conn, cur, use_savepoint)
        except Exception:
            pass
        return {
            "success": False,
            "_retryable_lock_error": is_retryable_lock_error(e),
            "error": str(e),
            "message": "Der opstod en fejl ved oprettelse af ordren. Prøv venligst igen.",
        }
    finally:
        try:
            if cur is not None:
                cur.close()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# get_order — ownership-gated read
# ---------------------------------------------------------------------------
def get_order(ctx, order_id):
    """Return the order row (dict) ONLY if ctx is authorized; else None.

    Authorized == the ctx plays a role on the order (owner / same-company
    manager / the order's vendor / platform admin). For LEGACY anonymous orders
    where the row has NULL company_id (pre-enterprise chatbot orders), fall back
    to the PRIOR behaviour so the anonymous-consumer status flow keeps working
    — enterprise PII (company_id NOT NULL) is never leaked.
    Cross-tenant / other-user => None (callers should return 404).
    """
    conn = _get_connection()
    if conn is None:
        return None

    cur = None
    try:
        cur = _dict_cursor(conn)
        cur.execute(
            "SELECT * FROM course_orders WHERE order_id = %s",
            (order_id,),
        )
        row = cur.fetchone()
        if not row:
            return None

        if _ownership_ok(ctx, row):
            return row

        # Legacy anonymous fallback: ONLY when the row has no company (NULL
        # company_id). Never leak enterprise rows.
        if _int_or_none(row.get("company_id")) is None:
            return row

        return None
    except Exception as e:
        logger.error("order_service.get_order failed: %s", e)
        return None
    finally:
        try:
            if cur is not None:
                cur.close()
        except Exception:
            pass


def assignment_info(order_row):
    """Who assigned this order, or None when the learner ordered it themselves.

    ``{"assigned_by": name, "approved": bool}``: ``approved`` is true when a manager
    approved it by assigning it ("Godkendt ved tildeling"); an assignment that the budget
    rule sent to approval is still an assignment but not approved yet. Never raises."""
    if not order_row:
        return None
    note = (order_row.get("request_notes") or "").strip()
    conn = _get_connection()
    if conn is None:
        return None
    cur = None
    try:
        cur = _dict_cursor(conn)
        cur.execute(
            "SELECT approver_user_id FROM order_approvals WHERE order_id = %s AND company_id = %s "
            "AND status = 'approved' AND notes = %s ORDER BY id DESC LIMIT 1",
            (order_row.get("order_id"), order_row.get("company_id"), ASSIGNMENT_APPROVAL_NOTE),
        )
        approval = cur.fetchone()
        if approval and approval.get("approver_user_id"):
            from person_names import display_name
            return {"assigned_by": display_name(cur, order_row.get("company_id"),
                                                user_id=approval["approver_user_id"], default="HR"),
                    "approved": True}
        if note.startswith("Tildelt af "):
            return {"assigned_by": note[len("Tildelt af "):].strip() or "HR", "approved": False}
    except Exception as e:
        logger.debug("order_service.assignment_info failed: %s", e)
    finally:
        try:
            if cur is not None:
                cur.close()
        except Exception:
            pass
    return None


def get_history(ctx, order_id):
    """Status + billing history for an order the ctx may see ([] otherwise).
    Billing rows are only returned to managers/admins, never to the learner;
    status and change-request rows (``kind='change'``) are visible to everyone."""
    row = get_order(ctx, order_id)
    if not row:
        return []
    conn = _get_connection()
    if conn is None:
        return []
    actors = actors_for(ctx, row)
    cur = None
    try:
        cur = _dict_cursor(conn)
        cur.execute(
            "SELECT kind, from_value, to_value, actor_kind, actor_label, note, created_at "
            "FROM order_status_history WHERE order_id = %s ORDER BY created_at ASC, id ASC",
            (order_id,),
        )
        rows = list(cur.fetchall() or [])
    except Exception as e:
        logger.debug("order_service.get_history failed: %s", e)
        return []
    finally:
        try:
            if cur is not None:
                cur.close()
        except Exception:
            pass
    if not ({"manager", "admin"} & actors):
        rows = [r for r in rows if (r.get("kind") or "status") in ("status", "change")]
    return rows


# ---------------------------------------------------------------------------
# Budget helpers (exactly once)
# ---------------------------------------------------------------------------
def _maybe_refund(cur, row):
    """Refund budget exactly once. Returns True iff a refund happened.

    Uses the order's OWN fiscal_year (year of created_at). Only refunds when
    budget_charged == 1, then sets budget_charged = 0 so it can never refund
    twice.
    """
    try:
        charged = _int_or_none(row.get("budget_charged")) or 0
    except Exception:
        charged = 0
    if not charged:
        return False

    company_id = _int_or_none(row.get("company_id"))
    department = (row.get("department") or "").strip()
    price_f = _to_float(row.get("price"))
    if not company_id or not department or price_f <= 0:
        # Nothing to refund against; still clear the flag to stay consistent.
        try:
            cur.execute(
                "UPDATE course_orders SET budget_charged = 0 WHERE order_id = %s",
                (row.get("order_id"),),
            )
        except Exception:
            pass
        return False

    fiscal_year = _fiscal_year_of(row.get("created_at"))
    try:
        cur.execute(
            """
            UPDATE department_budgets
            SET spent = GREATEST(0, spent - %s)
            WHERE company_id = %s AND department = %s AND fiscal_year = %s
            """,
            (max(0, price_f - _to_float(row.get("_cancellation_fee"))), company_id, department, fiscal_year),
        )
        cur.execute(
            "UPDATE course_orders SET budget_charged = 0 WHERE order_id = %s",
            (row.get("order_id"),),
        )
        return True
    except Exception as e:
        logger.warning("order_service: refund failed: %s", e)
        raise


def _maybe_charge(cur, row):
    """Charge budget exactly once for an order transitioning into a live state.

    Returns True iff a charge happened. Only charges when budget_charged == 0,
    then sets budget_charged = 1. Uses the order's OWN fiscal_year.
    """
    try:
        charged = _int_or_none(row.get("budget_charged")) or 0
    except Exception:
        charged = 0
    if charged:
        return False

    company_id = _int_or_none(row.get("company_id"))
    department = (row.get("department") or "").strip()
    price_f = _to_float(row.get("price"))
    if not company_id or not department or price_f <= 0:
        return False

    fiscal_year = _fiscal_year_of(row.get("created_at"))
    try:
        cur.execute(
            """
            SELECT id, annual_budget, spent FROM department_budgets
            WHERE company_id = %s AND department = %s AND fiscal_year = %s FOR UPDATE
            """,
            (company_id, department, fiscal_year),
        )
        brow = cur.fetchone()
        if not brow:
            return False
        new_spent = _to_float(brow.get("spent")) + price_f
        cur.execute(
            "UPDATE department_budgets SET spent = spent + %s WHERE id = %s",
            (price_f, brow["id"]),
        )
        cur.execute(
            "UPDATE course_orders SET budget_charged = 1 WHERE order_id = %s",
            (row.get("order_id"),),
        )

        # C3: budget overrun — surface as an in-app card (in this transaction) +
        # an outbox event. Best-effort; never blocks the charge. The branded
        # email is deferred to the caller's POST-COMMIT phase (it needs its own
        # connection + ops-gated send), so we stash the overrun details on the
        # row for set_status to pick up rather than sending mid-transaction.
        annual = _to_float(brow.get("annual_budget"))
        if annual > 0 and new_spent > annual:
            _notify_company_admins_safe(
                cur, company_id,
                f"Budget overskredet: {department}",
                f"Afdelingen '{department}' har nu brugt {new_spent:.0f} kr. af "
                f"{annual:.0f} kr. efter en godkendt bestilling.",
                is_urgent=1,
                action_url="/hr/budgets",
                dedupe_key="budget-overrun:%s:%s" % (company_id, department),
            )
            row["_budget_overrun"] = {
                "company_id": company_id,
                "department": department,
                "spent": new_spent,
                "annual_budget": annual,
                "order_id": row.get("order_id"),
            }
        return True
    except Exception as e:
        logger.warning("order_service: charge-on-transition failed: %s", e)
        raise


# ---------------------------------------------------------------------------
# Transition engine — every status writer goes through here
# ---------------------------------------------------------------------------
_LEGACY_INPUTS = set(lc.ORDER_STATUSES) | set(lc.LEGACY_ALIASES)


def _learner_notice(new, row, reason=None, note=None):
    """(title, message) for the learner's in-app notification, or None."""
    title = row.get("product_title") or "dit kursus"
    if new == lc.APPROVED:
        return ("Din bestilling er godkendt",
                f"“{title}” er godkendt. Nu afventer vi udbyderens bekræftelse af din plads.")
    if new == lc.REJECTED:
        extra = f" Begrundelse: {note}" if note else ""
        return ("Din bestilling blev afvist", f"“{title}” blev desværre afvist.{extra}")
    if new == lc.BOOKED:
        return ("Din plads er booket", f"Din plads på “{title}” er bekræftet.")
    if new == lc.CANCELLED:
        extra = f" Årsag: {reason}" if reason else ""
        return ("Din bestilling er annulleret", f"“{title}” er annulleret.{extra}")
    if new == lc.COMPLETED:
        return ("Kurset er gennemført",
                f"Tillykke med “{title}”. Tilføj de færdigheder, du har fået, til din profil.")
    return None


def _apply_transition(cur, ctx, row, new, actors, *, note=None, reason=None):
    """Write one validated transition inside the caller's transaction.

    Returns ``{"charged", "refunded", "old"}``. Raises on SQL failure so the
    caller rolls the whole transition (status + approval + budget + history)
    back together.
    """
    old = lc.normalize_status(row.get("status"))
    order_id = row.get("order_id")
    actor_label = (ctx.actor_label or ctx.username or "")[:255]

    sets = ["status = %s", "updated_at = NOW()"]
    params = [new]
    if new == lc.APPROVED and ctx.user_id and ctx.actor_kind != "system":
        sets.append("approved_by = %s")
        params.append(ctx.user_id)
    if new == lc.BOOKED:
        sets.append("booked_at = NOW()")
        sets.append("booked_by = %s")
        params.append(actor_label)
    if new == lc.CANCELLED:
        sets.append('cancellation_fee = %s')
        params.append(_to_float(row.get('_cancellation_fee')))
    if new in (lc.CANCELLED, lc.REJECTED) and (reason or note):
        sets.append("cancel_reason = %s")
        params.append(str(reason or note)[:255])
    if new == lc.COMPLETED:
        sets.append("completion_status = 'completed'")
        sets.append("completion_date = COALESCE(completion_date, NOW())")
    cur.execute(
        "UPDATE course_orders SET " + ", ".join(sets) + " WHERE order_id = %s",
        tuple(params) + (order_id,),
    )

    # The approval row moves with the order — in the SAME transaction (before,
    # the row could commit while the status change failed).
    if old == lc.PENDING_APPROVAL and new in (lc.APPROVED, lc.REJECTED, lc.CANCELLED):
        cur.execute(
            """
            UPDATE order_approvals
            SET status = %s, notes = COALESCE(%s, notes), approver_user_id = %s,
                decided_at = NOW()
            WHERE order_id = %s AND status = 'pending'
            """,
            (new, (note or None), ctx.user_id, order_id),
        )

    charged = refunded = False
    if new in _CANCELLED_LIKE_STATUSES:
        refunded = _maybe_refund(cur, row)
    elif old in _NON_CHARGING_STATUSES and new not in _NON_CHARGING_STATUSES:
        charged = _maybe_charge(cur, row)

    history_note = note or reason
    if new == lc.BOOKED:
        booker = "udbyderen" if "vendor" in actors else ("admin" if ctx.is_platform_admin and "manager" not in actors else "HR")
        history_note = "Booket af %s" % booker + (f": {history_note}" if history_note else "")
    _record_history(cur, row, kind="status", from_value=old, to_value=new, ctx=ctx, note=history_note)

    _write_audit(
        cur,
        company_id=_int_or_none(row.get("company_id")),
        user_id=ctx.user_id,
        action="order.status_changed",
        resource_id=order_id,
        description=f"{old}->{new} charged={charged} refunded={refunded}",
    )

    # The approval question has been answered: its card stops being unread/urgent for everyone.
    if old == lc.PENDING_APPROVAL and new != lc.PENDING_APPROVAL:
        from notification_service import resolve_by_dedupe_key
        resolve_by_dedupe_key(cur, "approval-needed:%s" % order_id, _int_or_none(row.get("company_id")))

    # In-app notifications (same transaction).
    try:
        from notification_service import notify_user, notify_roles, HR_ROLES
        notice = _learner_notice(new, row, reason=reason, note=note)
        owner_acting = "owner" in actors and not ({"manager", "admin", "vendor"} & actors)
        if notice and row.get("username") and not owner_acting:
            notify_user(cur, title=notice[0], message=notice[1], username=row.get("username"),
                        user_id=_int_or_none(row.get("user_id")),
                        company_id=_int_or_none(row.get("company_id")), kind="order",
                        action_url=order_url(order_id, absolute=False),
                        dedupe_key="order:%s:%s" % (order_id, new), dedupe_hours=None,
                        actor_user_id=ctx.user_id)
        cid = _int_or_none(row.get("company_id"))
        if cid and new == lc.BOOKED:
            # HR hears about a booking too, except the HR person who made it.
            from notification_service import role_recipients
            booker = "Udbyderen" if "vendor" in actors else "HR"
            learner_name = _person_name(cur, row) or "medarbejderen"
            for rcpt in role_recipients(cur, cid, HR_ROLES):
                if ctx.actor_kind != "vendor" and ctx.user_id is not None and _int_or_none(rcpt.get("user_id")) == ctx.user_id:
                    continue
                notify_user(cur, title="Plads bekræftet",
                            message=f"{booker} har bekræftet pladsen på “{row.get('product_title')}” til {learner_name}.",
                            username=rcpt["username"], user_id=rcpt["user_id"], company_id=cid, kind="order",
                            action_url="/hr/order/%s/details" % order_id,
                            dedupe_key="order-booked-hr:%s" % order_id, dedupe_hours=None)
        if cid and new == lc.CANCELLED and ("vendor" in actors or "owner" in actors):
            who = "Udbyderen" if "vendor" in actors else (_person_name(cur, row) or "Medarbejderen")
            notify_roles(cur, cid, HR_ROLES,
                         title="Bestilling annulleret",
                         message=f"{who} har annulleret “{row.get('product_title')}”."
                                 + (f" Årsag: {reason}" if reason else ""),
                         kind="order", is_urgent=("vendor" in actors),
                         action_url="/hr/order/%s/details" % order_id,
                         dedupe_key="order-cancelled:%s" % order_id, dedupe_hours=None,
                         actor_user_id=ctx.user_id)
    except Exception as e:  # notifications must never break the transition
        logger.debug("order_service: transition notifications skipped: %s", e)

    _queue_transition_emails(ctx,row,old,new,note=note,reason=reason,cursor=cur)
    return {"charged": charged, "refunded": refunded, "old": old}


def _after_transition(ctx, row, old, new, info, *, note=None, reason=None):
    """Post-commit side effects: webhook events, emails, vendor notice."""
    company_id = _int_or_none(row.get("company_id"))
    order_id = row.get("order_id")

    _overrun = row.get("_budget_overrun")
    if _overrun:
        _emit_event_safe(company_id, "budget.overrun", _overrun)
        _send_budget_overrun_emails_safe(
            _overrun.get("company_id"),
            department=_overrun.get("department"),
            spent=_overrun.get("spent"),
            annual_budget=_overrun.get("annual_budget"),
            order_id=_overrun.get("order_id"),
        )

    payload = {
        "order_id": order_id, "status": new, "previous_status": old,
        "charged": info.get("charged"), "refunded": info.get("refunded"),
        "product_title": row.get("product_title"),
        "department": (row.get("department") or "") or None,
        "user_email": row.get("user_email"),
    }
    specific = {lc.APPROVED: "order.approved", lc.REJECTED: "order.rejected",
                lc.BOOKED: "order.booked", lc.COMPLETED: "order.completed",
                lc.CANCELLED: "order.cancelled"}.get(new)
    if specific:
        _emit_event_safe(company_id, specific, payload)
    # Generic signal for subscribers of the advertised 'order.updated'.
    _emit_event_safe(company_id, "order.updated", payload)
    if new == lc.COMPLETED:
        _emit_event_safe(company_id, "course.completed", {
            "order_id": order_id,
            "product_title": row.get("product_title"),
            "product_handle": row.get("product_handle"),
            "department": (row.get("department") or "") or None,
            "user_id": _int_or_none(row.get("user_id")),
            "user_email": row.get("user_email"),
            "completed_at": datetime.datetime.now().isoformat(),
        })



def set_status(ctx, order_id, new_status, *, note=None, reason=None):
    """The ONE authorized status transition (N-1.1).

    * validates the move against the lifecycle map AND the actor's role
      (learner may cancel/complete own order, HR approves/rejects/books,
      a vendor books/declines/completes only its own orders, admin anything);
    * writes status + approval row + budget charge/refund + history + audit in ONE
      transaction; side effects (webhooks, email, vendor notice) run post-commit;
    * is idempotent: asking for the status the order already has succeeds with
      ``unchanged=True`` and does nothing (no double charge, no double email).

    Legacy names (``pending``, ``confirmed``, ...) are mapped onto the canonical
    statuses. Returns {success, order_id, status, previous_status, charged,
    refunded, unchanged?, error?, message}.
    """
    raw = (new_status or "").strip().lower()
    if not raw or raw not in _LEGACY_INPUTS:
        return {"success": False, "error": "bad_status", "message": "Ugyldig status."}
    new = lc.normalize_status(raw)

    if new == lc.COMPLETED:
        return complete_order(ctx,order_id,note=note)
    if lc.normalize_status(new_status) == lc.BOOKED:
        return book_order(ctx,order_id,note=note)
    conn = _get_connection()
    if conn is None:
        return {"success": False, "error": "no_db",
                "message": "Databasen er ikke tilgængelig lige nu."}

    cur = None
    try:
        cur = _dict_cursor(conn)
        _lock_order(cur, ctx, order_id)
        row = cur.fetchone()
        if not row:
            return {"success": False, "error": "not_found",
                    "message": "Ordren blev ikke fundet."}

        actors = actors_for(ctx, row)
        if not actors:
            # Anti-enumeration: behave like not-found.
            return {"success": False, "error": "not_found",
                    "message": "Ordren blev ikke fundet."}

        old = lc.normalize_status(row.get("status"))
        ok, code, msg = lc.check_transition(old, new, actors)
        if code == "no_change":
            return {"success": True, "unchanged": True, "order_id": order_id, "status": new,
                    "previous_status": old, "charged": False, "refunded": False,
                    "already_cancelled": new in _CANCELLED_LIKE_STATUSES,
                    "message": "Ordren havde allerede denne status."}
        if not ok:
            conn.rollback()
            return {"success": False, "error": code, "message": msg,
                    "status": old, "order_id": order_id}

        if new == lc.CANCELLED and old == lc.BOOKED and 'vendor' not in actors:
            conn.rollback()
            from order_fulfillment import request_change
            return request_change(ctx,order_id,'cancel',{'note':reason or note or ''})
        info = _apply_transition(cur, ctx, row, new, actors, note=note, reason=reason)
        conn.commit()
        _after_transition(ctx, row, old, new, info, note=note, reason=reason)

        return {
            "success": True,
            "order_id": order_id,
            "status": new,
            "status_label": lc.status_label(new),
            "previous_status": old,
            "charged": info["charged"],
            "refunded": info["refunded"],
            "already_cancelled": False,
        }
    except Exception as e:
        logger.error("order_service.set_status failed: %s", e)
        try:
            conn.rollback()
        except Exception:
            pass
        return {"success": False, "error": str(e),
                "message": "Der opstod en fejl ved opdatering af ordrestatus."}
    finally:
        try:
            if cur is not None:
                cur.close()
        except Exception:
            pass


def cancel_order(ctx, order_id, reason=None):
    """Cancel an order (owner, same-company manager, vendor or admin), refunding
    budget exactly once. Idempotent: cancelling twice never refunds twice.

    A booked order is not cancelled by its owner or HR: the call files a change
    request with the vendor and returns ``requested: True`` (status stays
    ``booked``, budget untouched) with the request message, never "annulleret"."""
    res = set_status(ctx, order_id, lc.CANCELLED, reason=reason)
    if res.get("success"):
        if res.get("pending"):
            # A booked order is only cancelled once the vendor accepts: this is a
            # request. Keep order_fulfillment's message and say so explicitly.
            res["requested"] = True
            res["already_cancelled"] = False
            return res
        res.setdefault("message", "Ordren er annulleret.")
        if not res.get("unchanged"):
            res["message"] = "Ordren er annulleret."
        res["already_cancelled"] = bool(res.get("unchanged"))
        res["requested"] = False
    return res


def book_order(ctx, order_id, note=None, booking=None):
    from order_fulfillment import book
    return book(ctx,order_id,booking=booking,note=note)


def decide_approval(ctx, approval_id, decision, notes="", department_scope=None):
    """Approve or reject via the approval queue. Resolves the approval row,
    then runs the single transition (status + approval row + budget) in one
    transaction. Returns set_status' dict (plus ``order_id``).

    ``department_scope`` (S-1.6): a department head may only decide requests
    from their OWN department; any other department is refused as ``forbidden``."""
    if decision not in ("approved", "rejected"):
        return {"success": False, "error": "bad_decision", "message": "Ugyldig beslutning."}
    conn = _get_connection()
    if conn is None:
        return {"success": False, "error": "no_db", "message": "Databasen er ikke tilgængelig lige nu."}
    cur = None
    try:
        cur = _dict_cursor(conn)
        cur.execute(
            "SELECT oa.order_id, co.department FROM order_approvals oa "
            "LEFT JOIN course_orders co ON oa.order_id = co.order_id "
            "WHERE oa.id = %s AND oa.company_id = %s AND oa.status = 'pending'",
            (approval_id, ctx.company_id),
        )
        a = cur.fetchone()
    finally:
        try:
            if cur is not None:
                cur.close()
        except Exception:
            pass
    if not a:
        return {"success": False, "error": "not_found",
                "message": "Godkendelsen blev ikke fundet, eller er allerede behandlet."}
    if department_scope is not None and (a.get("department") or "") != department_scope:
        return {"success": False, "error": "forbidden",
                "message": "Du kan kun behandle anmodninger fra din egen afdeling."}
    target = lc.APPROVED if decision == "approved" else lc.REJECTED
    return set_status(ctx, a["order_id"], target, note=(notes or None))


def bulk_decide(ctx, approval_ids, decision, notes="", department_scope=None):
    """Decide many approvals; per-item results, one failure never blocks the rest."""
    results = []
    for aid in approval_ids or []:
        try:
            r = decide_approval(ctx, int(aid), decision, notes, department_scope=department_scope)
        except Exception as e:  # pragma: no cover - defensive
            r = {"success": False, "error": str(e)}
        r["approval_id"] = aid
        results.append(r)
    ok = sum(1 for r in results if r.get("success"))
    return {"success": ok > 0 or not results, "done": ok, "failed": len(results) - ok, "results": results}


# ---------------------------------------------------------------------------
# Completion — the ONE completion path (N-1.3)
# ---------------------------------------------------------------------------
def complete_order(ctx, order_id, *, note=None):
    """Mark a course completed. Used by the learner button, the AI tool, HR
    "mark complete" and the vendor portal.

    In one transaction: order -> completed (+ completion_status/date), the
    learner's completed-course list, learning progress, the employee counters.
    Then: ``course.completed`` event and a manager notification. Returns the
    completion moment payload (skill proposals, next steps) for the UI/AI.
    """
    conn = _get_connection()
    if conn is None:
        return {"success": False, "error": "no_db",
                "message": "Databasen er ikke tilgængelig lige nu."}
    cur = None
    try:
        cur = _dict_cursor(conn)
        _lock_order(cur, ctx, order_id)
        row = cur.fetchone()
        if not row:
            return {"success": False, "error": "not_found", "message": "Ordren blev ikke fundet."}
        actors = actors_for(ctx, row)
        if not actors:
            return {"success": False, "error": "not_found", "message": "Ordren blev ikke fundet."}

        if 'owner' in actors and not ({'manager','admin','vendor'} & actors):
            conn.rollback()
            from order_fulfillment import report_completion
            return report_completion(ctx,order_id,note=note or '')

        old = lc.normalize_status(row.get("status"))
        if old == lc.COMPLETED:
            moment = _completion_moment(row)
            return {"success": True, "unchanged": True, "order_id": order_id,
                    "status": lc.COMPLETED, "status_label": lc.status_label(lc.COMPLETED),
                    "already_completed": True, **moment}
        ok, code, msg = lc.check_transition(old, lc.COMPLETED, actors)
        if not ok:
            conn.rollback()
            return {"success": False, "error": code, "message": msg, "status": old}

        # Decision: a course is "completed" only after it has taken place, whoever
        # confirms it (HR, vendor, admin, the AI tool). No override.
        from order_fulfillment import details as _booking_details
        from order_timing import not_yet_held_message
        held_error = not_yet_held_message(row, _booking_details(cur, order_id).get("booking_json"))
        if held_error:
            conn.rollback()
            return {"success": False, "error": "not_yet_held", "message": held_error, "status": old}

        info = _apply_transition(cur, ctx, row, lc.COMPLETED, actors, note=note)
        from order_fulfillment import _save_details, capture_baseline
        _save_details(cur,row)
        capture_baseline(cur,row,at_booking=False)
        cur.execute("UPDATE course_order_details SET completion_state='verified',verified_at=CURRENT_TIMESTAMP,verified_by=%s WHERE order_id=%s", (ctx.actor_label or ctx.username or ctx.actor_kind,order_id))
        cur.execute("UPDATE learning_outcome_reviews SET status='open' WHERE order_id=%s AND status='awaiting_completion'",(order_id,))
        _record_completion_side_effects(cur, row)
        from learning_path_service import refresh_for_order
        refresh_for_order(cur, order_id, row.get("company_id"))
        from learning_path_service import sync_personal_path_completion
        sync_personal_path_completion(cur,row)
        conn.commit()
        _after_transition(ctx, row, old, lc.COMPLETED, info, note=note)
        _notify_manager_of_completion(row)
        moment = _completion_moment(row)
        return {"success": True, "order_id": order_id, "status": lc.COMPLETED,
                "status_label": lc.status_label(lc.COMPLETED),
                "previous_status": old, **moment}
    except Exception as e:
        logger.error("order_service.complete_order failed: %s", e)
        try:
            conn.rollback()
        except Exception:
            pass
        return {"success": False, "error": str(e),
                "message": "Der opstod en fejl, da kurset skulle markeres som gennemført."}
    finally:
        try:
            if cur is not None:
                cur.close()
        except Exception:
            pass


def _record_completion_side_effects(cur, row):
    """Profile course list + learning progress + counters (inside the tx)."""
    username = row.get("username")
    handle = row.get("product_handle") or ""
    title = row.get("product_title") or ""
    uid = _int_or_none(row.get("user_id"))
    cid = _int_or_none(row.get("company_id"))
    vendor_name = ""
    try:
        import catalog_service
        p = catalog_service.get_product(handle) if handle else None
        vendor_name = (p or {}).get("vendor") or ""
    except Exception:
        pass
    if username and title:
        cur.execute(
            """
            INSERT INTO user_completed_courses
                (username, course_title, course_handle, vendor, completed_date)
            VALUES (%s, %s, %s, %s, %s)
            ON DUPLICATE KEY UPDATE course_handle = VALUES(course_handle),
                vendor = VALUES(vendor), completed_date = VALUES(completed_date)
            """,
            (username, title[:255], handle or None, vendor_name,
             datetime.date.today().isoformat()),
        )
    if uid:
        cur.execute(
            "SELECT id FROM employee_learning_progress WHERE user_id = %s AND "
            "COALESCE(course_handle, '') = %s AND COALESCE(company_id, 0) = %s LIMIT 1",
            (uid, handle, cid or 0),
        )
        existing = cur.fetchone()
        if existing:
            cur.execute(
                "UPDATE employee_learning_progress SET status = 'completed', "
                "progress_percentage = 100, completed_at = NOW() WHERE id = %s",
                (existing["id"] if isinstance(existing, dict) else existing[0],),
            )
        else:
            cur.execute(
                """
                INSERT INTO employee_learning_progress
                    (user_id, company_id, course_handle, content_type, content_name,
                     status, progress_percentage, completed_at, created_at)
                VALUES (%s, %s, %s, 'course', %s, 'completed', 100, NOW(), NOW())
                """,
                (uid, cid, handle, title[:255]),
            )
        if cid:
            cur.execute(
                "UPDATE company_users SET total_courses_completed = COALESCE(total_courses_completed, 0) + 1, "
                "courses_completed = COALESCE(courses_completed, 0) + 1 "
                "WHERE company_id = %s AND user_id = %s",
                (cid, uid),
            )


def _notify_manager_of_completion(row):
    """Manager task: "bekræft kompetenceløft" for the learner's manager (else HR)."""
    cid = _int_or_none(row.get("company_id"))
    uid = _int_or_none(row.get("user_id"))
    if not cid or not uid:
        return
    conn = _get_connection()
    if conn is None:
        return
    cur = None
    try:
        from notification_service import notify_user, notify_roles, HR_ROLES
        cur = _dict_cursor(conn)
        cur.execute("SELECT manager_user_id FROM company_users WHERE company_id = %s AND user_id = %s LIMIT 1",
                    (cid, uid))
        r = cur.fetchone()
        mgr = _int_or_none(r.get("manager_user_id") if isinstance(r, dict) else (r[0] if r else None))
        msg = (f"{_person_name(cur, row) or 'En medarbejder'} har gennemført "
               f"“{row.get('product_title')}”. Bekræft kompetenceløftet, så det tæller i kompetenceoverblikket.")
        common = dict(title="Bekræft kompetenceløft", message=msg, kind="skill_uplift",
                      action_url="/hr/order/%s/details#udbytte" % row.get("order_id"),
                      dedupe_key="uplift:%s" % row.get("order_id"), dedupe_hours=None)
        if mgr:
            notify_user(cur, user_id=mgr, company_id=cid, **common)
        else:
            notify_roles(cur, cid, HR_ROLES, **common)
        conn.commit()
    except Exception as e:
        logger.debug("order_service: manager completion notice skipped: %s", e)
        try:
            conn.rollback()
        except Exception:
            pass
    finally:
        try:
            if cur is not None:
                cur.close()
        except Exception:
            pass


def _completion_moment(row):
    """Skills proposed from the course metadata, next steps, review prompt."""
    try:
        import completion_service
        return completion_service.completion_moment(row)
    except Exception as e:
        logger.debug("order_service: completion moment skipped: %s", e)
        return {"skill_proposals": [], "next_steps": [], "review_url": None}


# ---------------------------------------------------------------------------
# Billing — a separate dimension; payment happens OFF-platform (N-6.3)
# ---------------------------------------------------------------------------
_BILLING_ROLES = frozenset({"hr_manager", "company_admin"})


def _billing_allowed(ctx, row):
    if ctx.is_platform_admin:
        return True
    cid = _int_or_none(row.get("company_id"))
    return (cid is not None and ctx.company_id == cid
            and (ctx.company_role or "") in _BILLING_ROLES)


def set_billing_status(ctx, order_id, new_billing, *, invoice_number=None, invoice_date=None,
                       due_date=None, payment_date=None, payment_reference=None,
                       payment_method=None, note=None):
    """not_invoiced -> invoiced -> paid (+ credited). HR/admin only; every change
    is recorded in order_status_history(kind='billing'). Solo-user orders (no
    company) are managed by the platform admin only."""
    target = (new_billing or "").strip().lower()
    if target not in lc.BILLING_STATUSES:
        return {"success": False, "error": "bad_status", "message": "Ugyldig faktureringsstatus."}

    conn = _get_connection()
    if conn is None:
        return {"success": False, "error": "no_db", "message": "Databasen er ikke tilgængelig lige nu."}
    cur = None
    try:
        cur = _dict_cursor(conn)
        _lock_order(cur, ctx, order_id)
        row = cur.fetchone()
        if not row or not _billing_allowed(ctx, row):
            return {"success": False, "error": "not_found", "message": "Ordren blev ikke fundet."}

        old = lc.normalize_billing(row.get("billing_status"))
        if old == target:
            return {"success": True, "unchanged": True, "order_id": order_id, "billing_status": target}
        if not lc.can_bill_transition(old, target):
            conn.rollback()
            return {"success": False, "error": "bad_transition",
                    "message": "Fakturering kan ikke gå fra ‘%s’ til ‘%s’."
                               % (lc.billing_label(old), lc.billing_label(target))}
        if lc.normalize_status(row.get("status")) in (lc.REJECTED, lc.PENDING_APPROVAL) and target != lc.NOT_INVOICED:
            conn.rollback()
            return {"success": False, "error": "not_billable",
                    "message": "Kun godkendte bestillinger kan faktureres."}

        sets = ["billing_status = %s", "updated_at = NOW()"]
        params = [target]
        if target == lc.INVOICED:
            if not (invoice_number or row.get("invoice_number")):
                conn.rollback()
                return {"success": False, "error": "invoice_number_required",
                        "message": "Angiv fakturanummeret fra jeres eksterne system."}
            if invoice_number:
                sets.append("invoice_number = %s"); params.append(str(invoice_number)[:100])
            sets.append("invoice_date = COALESCE(%s, CURDATE())"); params.append(invoice_date or None)
            if due_date:
                sets.append("invoice_due_date = %s"); params.append(due_date)
        if target == lc.PAID:
            sets.append("payment_date = COALESCE(%s, NOW())"); params.append(payment_date or None)
            sets.append("payment_status = 'paid'")
            if payment_reference:
                sets.append("payment_reference = %s"); params.append(str(payment_reference)[:255])
            if payment_method:
                sets.append("payment_method = %s"); params.append(str(payment_method)[:50])
        if target == lc.CREDITED and not note:
            conn.rollback()
            return {"success": False, "error": "note_required",
                    "message": "Skriv en kort begrundelse for krediteringen."}
        if target == lc.NOT_INVOICED:
            sets.append("invoice_date = NULL"); sets.append("invoice_due_date = NULL")
        if note:
            sets.append("billing_note = %s"); params.append(str(note)[:2000])
        cur.execute("UPDATE course_orders SET " + ", ".join(sets) + " WHERE order_id = %s",
                    tuple(params) + (order_id,))
        _record_history(cur, row, kind="billing", from_value=old, to_value=target, ctx=ctx,
                        note=note or invoice_number or payment_reference)
        _write_audit(cur, company_id=_int_or_none(row.get("company_id")), user_id=ctx.user_id,
                     action="order.billing_changed", resource_id=order_id,
                     description=f"{old}->{target} inv={invoice_number or row.get('invoice_number') or ''}")
        conn.commit()
        _emit_event_safe(_int_or_none(row.get("company_id")), "order.billing_changed", {
            "order_id": order_id, "billing_status": target, "previous_billing_status": old,
            "invoice_number": invoice_number or row.get("invoice_number")})
        return {"success": True, "order_id": order_id, "billing_status": target,
                "billing_label": lc.billing_label(target), "previous": old}
    except Exception as e:
        logger.error("order_service.set_billing_status failed: %s", e)
        try:
            conn.rollback()
        except Exception:
            pass
        return {"success": False, "error": str(e), "message": "Kunne ikke opdatere faktureringen."}
    finally:
        try:
            if cur is not None:
                cur.close()
        except Exception:
            pass


def update_billing_details(ctx, order_id, *, invoice_number=None, invoice_date=None, due_date=None,
                           payment_date=None, payment_reference=None, payment_method=None, note=None):
    """Edit invoice/payment references without changing the billing status."""
    sets, params = [], []
    for col, val in (("invoice_number", invoice_number), ("invoice_date", invoice_date),
                     ("invoice_due_date", due_date), ("payment_date", payment_date),
                     ("payment_reference", payment_reference), ("payment_method", payment_method),
                     ("billing_note", note)):
        if val is not None:
            sets.append(col + " = %s")
            params.append(val)
    if not sets:
        return {"success": False, "error": "no_fields", "message": "Ingen felter at opdatere."}
    conn = _get_connection()
    if conn is None:
        return {"success": False, "error": "no_db", "message": "Databasen er ikke tilgængelig lige nu."}
    cur = None
    try:
        cur = _dict_cursor(conn)
        _lock_order(cur, ctx, order_id)
        row = cur.fetchone()
        if not row or not _billing_allowed(ctx, row):
            return {"success": False, "error": "not_found", "message": "Ordren blev ikke fundet."}
        cur.execute("UPDATE course_orders SET " + ", ".join(sets) + ", updated_at = NOW() WHERE order_id = %s",
                    tuple(params) + (order_id,))
        billing = lc.normalize_billing(row.get("billing_status"))
        _record_history(cur, row, kind="billing", from_value=billing, to_value=billing, ctx=ctx,
                        note="Fakturadetaljer rettet")
        conn.commit()
        return {"success": True, "order_id": order_id, "billing_status": billing}
    except Exception as e:
        logger.error("order_service.update_billing_details failed: %s", e)
        try:
            conn.rollback()
        except Exception:
            pass
        return {"success": False, "error": str(e), "message": "Kunne ikke opdatere faktureringen."}
    finally:
        try:
            if cur is not None:
                cur.close()
        except Exception:
            pass


def bulk_set_billing_status(ctx, order_ids, new_billing, **fields):
    """Apply one billing change to many orders; per-order results."""
    results = []
    for oid in order_ids or []:
        r = set_billing_status(ctx, oid, new_billing, **fields)
        r["order_id"] = oid
        results.append(r)
    done = sum(1 for r in results if r.get("success"))
    return {"success": done > 0, "done": done, "failed": len(results) - done, "results": results}


# ---------------------------------------------------------------------------
# Vendor notices (N-6.1)
# ---------------------------------------------------------------------------
def _notify_vendor_safe(row,cursor=None):
    """Email the order's vendor that a seat is waiting to be confirmed. The
    in-app notice is the vendor orders page badge (derived from approved,
    not-yet-booked orders). Never raises; no-op without a vendor contact."""
    vid = _int_or_none((row or {}).get("vendor_id"))
    if not vid:
        return
    conn = _get_connection()
    if conn is None:
        return
    cur = None
    try:
        cur = _dict_cursor(conn)
        cur.execute("SELECT vendor_name, contact_email FROM vendors WHERE id = %s", (vid,))
        v = cur.fetchone()
        if not v or not v.get("contact_email"):
            return
        base = _app_base_url()
        _send_email_safe(
            v["contact_email"], "Ny bestilling afventer din bekræftelse", "vendor_new_order", row.get("company_id"), cursor=cursor,
            dedupe_key="vendor_new_order:%s" % row.get("order_id"),
            vendor_name=v.get("vendor_name") or "", product_title=row.get("product_title") or "",
            variant_date=row.get("variant_date") or "", variant_location=row.get("variant_location") or "",
            participant=row.get("user_name") or row.get("username") or "",
            orders_url=(base + "/vendor/orders") if base else "",
        )
    except Exception as e:
        logger.debug("order_service: vendor notice skipped: %s", e)
        if cursor is not None:
            raise
    finally:
        try:
            if cur is not None:
                cur.close()
        except Exception:
            pass


def _send_hr_vendor_decline_email_safe(row, reason,cursor=None):
    """Email company managers that the vendor declined an order."""
    cid = _int_or_none((row or {}).get("company_id"))
    if not cid:
        return
    base = _app_base_url()
    for to_email in _manager_recipient_emails(cid):
        _send_email_safe(
            to_email, "Udbyderen har afvist en bestilling", "order_cancelled", cid, cursor=cursor,
            dedupe_key="vendor_declined:%s:%s" % (row.get("order_id"), to_email),
            product_title=row.get("product_title", ""), order_id=row.get("order_id"),
            reason=reason or "", order_url=(base + "/hr/order/%s/details" % row.get("order_id")) if base else "",
        )


def _replace_order_terms(cur, ctx, row, quote):
    """Apply an explicitly accepted reschedule and its budget delta in one transaction."""
    import json
    was_charged = bool(row.get('budget_charged'))
    if was_charged:
        _maybe_refund(cur,row)
    cur.execute('UPDATE course_orders SET price=%s,variant_date=%s,variant_location=%s,budget_charged=0,updated_at=NOW() WHERE order_id=%s', (quote['price'],quote['variant_date'],quote['variant_location'],row['order_id']))
    row.update(price=quote['price'],variant_date=quote['variant_date'],variant_location=quote['variant_location'],budget_charged=0)
    if was_charged:
        _maybe_charge(cur,row)
    from order_fulfillment import _save_details
    _save_details(cur,row)
    cur.execute('UPDATE course_order_details SET session_id=%s,quote_json=%s WHERE order_id=%s',(quote['session_id'],json.dumps(quote,ensure_ascii=False),row['order_id']))


def _replace_order_participant(cur, ctx, row, participant):
    """Recheck membership at acceptance; move the commitment to the actual participant."""
    cur.execute("SELECT cu.user_id,u.username,COALESCE(cu.full_name,u.username) AS name,COALESCE(cu.email,u.email) AS email,cu.department FROM company_users cu JOIN users u ON u.id=cu.user_id WHERE cu.company_id=%s AND cu.user_id=%s AND cu.status='active' FOR UPDATE", (row['company_id'],participant['user_id']))
    person=cur.fetchone()
    if not person:
        raise ValueError('Den nye deltager er ikke længere aktiv i virksomheden.')
    cur.execute("SELECT order_id FROM course_orders WHERE company_id=%s AND user_id=%s AND product_handle=%s AND status IN ('pending_approval','approved','booked') AND order_id<>%s",(row['company_id'],person['user_id'],row['product_handle'],row['order_id']))
    if cur.fetchone():
        raise ValueError('Den nye deltager har allerede en åben bestilling til kurset.')
    was_charged=bool(row.get('budget_charged'))
    if was_charged:
        _maybe_refund(cur,row)
    cur.execute('UPDATE course_orders SET user_id=%s,username=%s,user_name=%s,user_email=%s,department=%s,budget_charged=0,updated_at=NOW() WHERE order_id=%s',(person['user_id'],person['username'],person['name'],person['email'],person['department'],row['order_id']))
    cur.execute('SELECT DISTINCT progress_id FROM learning_assignment_steps WHERE order_id=%s', (row['order_id'],))
    original_paths = list(cur.fetchall() or [])
    cur.execute("UPDATE learning_assignment_steps SET order_id=NULL,status='failed',last_error=%s WHERE order_id=%s",('Deltageren er ændret. Vælg en ny tilmelding til dette trin.',row['order_id']))
    row.update(user_id=person['user_id'],username=person['username'],user_name=person['name'],user_email=person['email'],department=person['department'],budget_charged=0)
    cur.execute('UPDATE course_order_details SET user_id=%s WHERE order_id=%s',(person['user_id'],row['order_id']))
    cur.execute('DELETE FROM learning_outcome_reviews WHERE order_id=%s',(row['order_id'],))
    from order_fulfillment import capture_baseline
    capture_baseline(cur,row)
    from learning_path_service import refresh_assignment
    for path in original_paths:
        refresh_assignment(cur,path['progress_id'],row['company_id'])
    if was_charged:
        _maybe_charge(cur,row)


def _confirm_booking_details(cur, row, booking):
    """Record the confirmed place on the order. ``variant_date`` stays the human
    session label ("3. december 2026"): it only changes, to a long Danish date and
    never an ISO timestamp, when the booked day differs from the ordered session.
    The exact ``start_at`` lives in ``course_order_details.booking_json``."""
    from order_timing import session_label
    label = session_label(row.get('variant_date') or '', booking['start_at'])
    cur.execute('UPDATE course_orders SET variant_date=%s,variant_location=%s WHERE order_id=%s',
                (label,booking['location'] or 'Online',row['order_id']))
    row.update(variant_date=label,variant_location=booking['location'] or 'Online')


def _queue_transition_emails(ctx,row,old,new,*,note=None,reason=None,cursor=None):
    company_id = row.get("company_id")
    order_id = row["order_id"]
    to_email = row.get("user_email")
    if to_email and ctx.actor_kind != "system":
        link = order_url(order_id)
        if new in (lc.APPROVED, lc.REJECTED):
            decision = "afvist" if new == lc.REJECTED else "godkendt"
            msg = ("Din bestilling blev desværre afvist." if new == lc.REJECTED
                   else "Din bestilling er godkendt. Udbyderen bekræfter nu din plads, og du får besked, når den er booket.")
            if new == lc.REJECTED and note:
                msg += f" Begrundelse fra HR: {note}"
            _send_email_safe(
                to_email, f"Din kursusbestilling er {decision}", "order_approved", company_id, cursor=cursor,
                product_title=row.get("product_title", ""), order_id=order_id,
                decision=decision, message=msg, order_url=link,
            )
        elif new == lc.BOOKED:
            _send_email_safe(
                to_email, f"Din plads er booket — {row.get('product_title', '')}",
                "order_booked", company_id, cursor=cursor,
                product_title=row.get("product_title", ""), order_id=order_id,
                variant_date=row.get("variant_date") or "",
                variant_location=row.get("variant_location") or "", order_url=link,
            )
        elif new == lc.CANCELLED and "owner" not in actors_for(ctx, row):
            _send_email_safe(
                to_email, f"Din bestilling er annulleret — {row.get('product_title', '')}",
                "order_cancelled", company_id, cursor=cursor,
                product_title=row.get("product_title", ""), order_id=order_id,
                reason=reason or "", order_url=link,
            )

    if new == lc.APPROVED and old == lc.PENDING_APPROVAL:
        _notify_vendor_safe(row,cursor=cursor)
    if new == lc.CANCELLED and ctx.actor_kind == "vendor":
        _send_hr_vendor_decline_email_safe(row, reason,cursor=cursor)
