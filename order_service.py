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

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Cross-integration side effects (events / email / notifications).
#
# All best-effort and fully guarded: a failure here NEVER affects the order
# transaction (they run AFTER commit, on their own connections) and NEVER raises
# into the caller. This is the two-track integration spine — outbox events for
# webhook fan-out + best-effort branded email + in-app notification rows.
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
                     dedupe_key=None, **context):
    """Send a branded transactional email. No-op without SMTP; never raises."""
    if not to_email:
        return
    try:
        from email_service import send_branded_email, _resolve_branding
        branding = _resolve_branding(company_id)
        send_branded_email(to_email, subject, template_name, branding,
                           company_id=company_id, dedupe_key=dedupe_key, **context)
    except Exception as e:  # pragma: no cover - defensive
        logger.debug("order_service: email(%s) skipped: %s", template_name, e)


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
                                      price, department, requester):
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
        if email_recently_sent(dedupe_key, within_hours=_EMAIL_DEDUPE_HOURS,
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
                "order_approval_needed", cid,
                dedupe_key=dedupe_key,
                product_title=product_title or "",
                amount=amount,
                requester=requester or "",
                department=(department or "") or "",
                approvals_url=approvals_url,
            )
    except Exception as e:  # pragma: no cover - defensive
        logger.debug("order_service: approval-needed email skipped: %s", e)


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


def _notify_company_admins_safe(cur, company_id, title, message, is_urgent=0):
    """Insert an in-app notification card for the company's HR/admins. Never raises.

    Uses the same company_notifications shape as the HR dashboard nudge path.
    Runs on the caller's cursor so it shares the order transaction's commit.
    """
    if not company_id:
        return
    try:
        cur.execute(
            """INSERT INTO company_notifications
                   (company_id, recipient_user_id, sender_user_id, target_roles,
                    title, message, is_urgent, is_read)
               VALUES (%s, NULL, NULL, %s, %s, %s, %s, 0)""",
            (company_id, '["company_admin","hr_manager"]', str(title)[:255],
             str(message), is_urgent),
        )
    except Exception as e:  # pragma: no cover - defensive
        logger.debug("order_service: company notification skipped: %s", e)


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
_NON_CHARGING_STATUSES = frozenset({"pending_approval"})
_CANCELLED_LIKE_STATUSES = frozenset({"cancelled", "rejected"})


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
        "department", "source",
    )

    def __init__(self, company_id=None, user_id=None, username=None,
                 company_role=None, department=None, source="web"):
        self.company_id = _int_or_none(company_id)
        self.user_id = _int_or_none(user_id)
        self.username = (username or "").strip() or None
        self.company_role = (company_role or "").strip().lower() or None
        self.department = (department or "").strip() or None
        self.source = source or "web"

    # -- builders -----------------------------------------------------------
    @classmethod
    def from_session(cls, source="web"):
        """Build from the Flask session (chat / web / tool paths)."""
        try:
            from flask import session
        except Exception:
            session = {}
        try:
            return cls(
                company_id=session.get("company_id"),
                user_id=session.get("user_id"),
                username=session.get("user") or session.get("username") or "guest",
                company_role=session.get("company_role", "employee"),
                department=session.get("company_department", ""),
                source=source,
            )
        except Exception:
            # Outside request context / broken session — degrade to anonymous.
            return cls(source=source)

    @classmethod
    def from_api_g(cls, source="api", company_role="company_admin"):
        """Build from the enterprise-API ``flask.g``.

        The API key authenticates a *company*, not an individual employee, so we
        treat the API actor as manager-level by default (``company_admin``) — the
        same way a department head ordering on someone's behalf would be. This
        means API-created company orders skip the per-employee approval step but
        STILL go through the shared budget check (budget-aware approval applies).
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


def build_context_from_session(source="web"):
    """Convenience helper (mirrors OrderContext.from_session)."""
    return OrderContext.from_session(source=source)


def build_context_from_api(source="api", company_role="company_admin"):
    """Convenience helper (mirrors OrderContext.from_api_g)."""
    return OrderContext.from_api_g(source=source, company_role=company_role)


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
    """Best-effort audit row. Guarded — never breaks the surrounding tx."""
    try:
        cur.execute(
            """
            INSERT INTO audit_log
                (company_id, user_id, action, action_type, resource_type,
                 resource_id, description)
            VALUES (%s, %s, %s, %s, 'order', %s, %s)
            """,
            (company_id, user_id, action, action, str(resource_id), description),
        )
    except Exception as e:  # pragma: no cover - audit must never fail the op
        logger.debug("order_service: audit_log skipped (%s): %s", action, e)


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


def _ownership_ok(ctx, order_row):
    """Ownership gate shared by get_order / cancel_order / set_status.

    Returns True when ctx may see/act on this order:
      * ctx is the order's OWNER (username or user_id matches), OR
      * ctx is a company manager/admin of the SAME company as the order.
    Cross-tenant / other-user => False (callers should 404, not 403).
    """
    if not order_row:
        return False

    row_user_id = _int_or_none(order_row.get("user_id"))
    row_username = (order_row.get("username") or "").strip() or None
    row_company_id = _int_or_none(order_row.get("company_id"))

    # Owner match.
    if ctx.user_id is not None and row_user_id is not None and ctx.user_id == row_user_id:
        return True
    if ctx.username and row_username and ctx.username == row_username:
        return True

    # Company manager/admin of the SAME tenant.
    if (ctx.company_id is not None and row_company_id is not None
            and ctx.company_id == row_company_id and ctx.is_manager):
        return True

    return False


# ---------------------------------------------------------------------------
# create_order
# ---------------------------------------------------------------------------
def create_order(ctx, *, product_handle, product_title, price,
                 variant_date="", variant_location="",
                 user_email="", user_name="", user_phone="",
                 status=None, extra=None):
    """Create an order through the single authorized path.

    One transaction (single connection, commit once, rollback on error):
      1. Resolve needs_approval (employees / unknown role -> approval).
      2. Budget-aware approval: if company+department+price>0 and the charge
         would exceed annual_budget, force needs_approval=True (never hard-fail).
      3. INSERT course_orders with status pending_approval | pending | <status>.
      4. If needs_approval: INSERT order_approvals(status='pending').
      5. Charge budget EXACTLY ONCE (spent += price, budget_charged=1) only when
         the order is in a budget-affecting state (NOT pending_approval).
      6. Audit log action='order.created'.

    Returns dict: {success, order_id, status, needs_approval, budget_warning, ...}
    """
    extra = extra or {}
    price_f = _to_float(price)
    order_id = str(uuid.uuid4())

    conn = _get_connection()
    if conn is None:
        return {
            "success": False,
            "error": "no_db",
            "message": "Databasen er ikke tilgængelig lige nu. Prøv igen senere.",
        }

    cur = None
    try:
        cur = _dict_cursor(conn)

        # --- 1. needs_approval (preserve current behaviour) ---------------
        # Employees (or unknown role) with a company need approval; managers/
        # admins and non-company (anonymous) orders do not.
        needs_approval = bool(ctx.company_id) and ctx.is_employee

        dept = ctx.department or extra.get("department") or ""

        # --- 1b. AUTO-APPROVAL policy layer (runs BEFORE budget check) -----
        # A company can configure a per-company (optionally per-department)
        # auto-approval threshold. Orders at/under auto_approve_under are
        # auto-approved (needs_approval cleared); orders at/over
        # require_approval_over are always routed to approval. The budget
        # overspend safety rule below can still RE-force approval even when a
        # policy auto-approved — safety first, it only ever tightens.
        auto_approved_by_policy = False
        if ctx.company_id and price_f > 0:
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
                    WHERE company_id = %s AND department = %s AND fiscal_year = %s
                    """,
                    (ctx.company_id, dept, fiscal_year),
                )
                budget_row = cur.fetchone()
            except Exception as be:
                logger.warning("order_service: budget lookup failed: %s", be)
                budget_row = None

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
                    budget_warning = (
                        f"Bestillingen på {price_f:.0f} kr. overskrider afdelingens "
                        f"resterende budget på {remaining:.0f} kr. "
                        f"(brugt {spent:.0f} af {annual:.0f} kr.). "
                        f"Ordren er sendt til godkendelse i stedet for at blive afvist."
                    )

        # --- 3. resolve status & INSERT course_orders --------------------
        if needs_approval:
            initial_status = "pending_approval"
        elif auto_approved_by_policy:
            # Policy auto-approved (and budget did NOT force approval): land in
            # an approved status and run the existing charge path below.
            initial_status = "approved"
        else:
            initial_status = status or "pending"

        # Charge now only if the order is NOT in a non-charging (approval) state
        # AND there is a department budget row to charge against and a price.
        charge_now = (
            initial_status not in _NON_CHARGING_STATUSES
            and bool(ctx.company_id) and bool(dept) and price_f > 0
            and budget_row is not None
        )
        budget_charged = 1 if charge_now else 0

        cur.execute(
            """
            INSERT INTO course_orders
                (order_id, company_id, user_id, username, product_handle,
                 product_title, price, variant_date, variant_location, status,
                 department, user_email, user_name, user_phone,
                 chatbot_session_id, chatbot_queries_before_order,
                 recommended_by_tool, budget_charged, created_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                    %s, %s, %s, %s, NOW())
            """,
            (
                order_id, ctx.company_id, ctx.user_id, ctx.username,
                product_handle, product_title, price_f,
                variant_date or "", variant_location or "", initial_status,
                dept, user_email or "", user_name or "", user_phone or "",
                extra.get("chatbot_session_id", ""),
                _int_or_none(extra.get("chatbot_queries_before_order")) or 0,
                extra.get("recommended_by_tool", ""),
                budget_charged,
            ),
        )

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

        # --- 5. charge budget EXACTLY ONCE -------------------------------
        if charge_now and budget_row is not None:
            try:
                new_spent = _to_float(budget_row.get("spent")) + price_f
                cur.execute(
                    "UPDATE department_budgets SET spent = %s WHERE id = %s",
                    (new_spent, budget_row["id"]),
                )
            except Exception as ce:
                logger.warning("order_service: budget charge failed: %s", ce)

        # --- 6. audit ----------------------------------------------------
        _audit_desc = f"{product_title} ({ctx.source})"
        if auto_approved_by_policy:
            _audit_desc += " [auto-godkendt via politik]"
        _write_audit(
            cur,
            company_id=ctx.company_id,
            user_id=ctx.user_id,
            action="order.auto_approved" if auto_approved_by_policy else "order.created",
            resource_id=order_id,
            description=_audit_desc,
        )

        # In-app notification to HR/admins when an order needs their approval
        # (shares this transaction so it commits atomically with the order).
        if needs_approval and ctx.company_id:
            _notify_company_admins_safe(
                cur, ctx.company_id,
                "Ny bestilling afventer godkendelse",
                f"{product_title} er bestilt af {ctx.username or 'en medarbejder'} "
                f"og afventer godkendelse.",
                is_urgent=1,
            )

        conn.commit()

        # --- 7. cross-integration side effects (post-commit, best-effort) ---
        if needs_approval:
            _event_type = "order.needs_approval"
        elif auto_approved_by_policy:
            _event_type = "order.auto_approved"
        else:
            _event_type = "order.created"
        _emit_event_safe(
            ctx.company_id,
            _event_type,
            {"order_id": order_id, "product_title": product_title,
             "price": price_f, "status": initial_status,
             "department": dept, "source": ctx.source,
             "auto_approved": auto_approved_by_policy},
        )
        if user_email and not needs_approval:
            _send_email_safe(
                user_email, f"Ordrebekræftelse — {product_title}",
                "order_confirmation", ctx.company_id,
                product_title=product_title, order_id=order_id,
                recipient_name=user_name or ctx.username or "",
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
                requester=ctx.username or user_name or "",
            )

        return {
            "success": True,
            "order_id": order_id,
            "status": initial_status,
            "needs_approval": needs_approval,
            "auto_approved": auto_approved_by_policy,
            "budget_warning": budget_warning,
            "budget_charged": bool(budget_charged),
            "price": price_f,
        }
    except Exception as e:
        logger.error("order_service.create_order failed: %s", e)
        try:
            conn.rollback()
        except Exception:
            pass
        return {
            "success": False,
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

    Authorized == ownership_ok(ctx, row). For LEGACY anonymous orders where the
    row has NULL company_id (pre-enterprise chatbot orders), fall back to the
    PRIOR behaviour and return the row so the existing anonymous-consumer status
    flow keeps working — enterprise PII (company_id NOT NULL) is never leaked.
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


# ---------------------------------------------------------------------------
# cancel_order — ownership-gated, exactly-once refund
# ---------------------------------------------------------------------------
def cancel_order(ctx, order_id):
    """Cancel an order (status -> 'cancelled'), refunding budget exactly once.

    Ownership-gated identically to get_order (owner OR same-company manager).
    Refund: ONLY when budget_charged == 1 -> spent -= price, budget_charged = 0,
    using the order's OWN fiscal_year (year(created_at)) — NOT the current year.
    Idempotent: cancelling an already-cancelled / uncharged order never refunds
    twice.
    """
    conn = _get_connection()
    if conn is None:
        return {"success": False, "error": "no_db",
                "message": "Databasen er ikke tilgængelig lige nu."}

    cur = None
    try:
        cur = _dict_cursor(conn)
        cur.execute("SELECT * FROM course_orders WHERE order_id = %s", (order_id,))
        row = cur.fetchone()
        if not row:
            return {"success": False, "error": "not_found",
                    "message": "Ordren blev ikke fundet."}

        if not _ownership_ok(ctx, row):
            # Anti-enumeration: behave like not-found.
            return {"success": False, "error": "not_found",
                    "message": "Ordren blev ikke fundet."}

        current_status = (row.get("status") or "").strip()
        already_cancelled = current_status in _CANCELLED_LIKE_STATUSES

        # Set status -> cancelled (idempotent if already cancelled).
        cur.execute(
            "UPDATE course_orders SET status = 'cancelled', updated_at = NOW() "
            "WHERE order_id = %s",
            (order_id,),
        )

        refunded = _maybe_refund(cur, row)

        _write_audit(
            cur,
            company_id=_int_or_none(row.get("company_id")),
            user_id=ctx.user_id,
            action="order.cancelled",
            resource_id=order_id,
            description=f"refunded={refunded}",
        )

        conn.commit()
        return {
            "success": True,
            "order_id": order_id,
            "status": "cancelled",
            "refunded": refunded,
            "already_cancelled": already_cancelled,
            "message": "Ordren er annulleret.",
        }
    except Exception as e:
        logger.error("order_service.cancel_order failed: %s", e)
        try:
            conn.rollback()
        except Exception:
            pass
        return {"success": False, "error": str(e),
                "message": "Der opstod en fejl ved annullering af ordren."}
    finally:
        try:
            if cur is not None:
                cur.close()
        except Exception:
            pass


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
            (price_f, company_id, department, fiscal_year),
        )
        cur.execute(
            "UPDATE course_orders SET budget_charged = 0 WHERE order_id = %s",
            (row.get("order_id"),),
        )
        return True
    except Exception as e:
        logger.warning("order_service: refund failed: %s", e)
        return False


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
            WHERE company_id = %s AND department = %s AND fiscal_year = %s
            """,
            (company_id, department, fiscal_year),
        )
        brow = cur.fetchone()
        if not brow:
            return False
        new_spent = _to_float(brow.get("spent")) + price_f
        cur.execute(
            "UPDATE department_budgets SET spent = %s WHERE id = %s",
            (new_spent, brow["id"]),
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
            )
            _emit_event_safe(company_id, "budget.overrun", {
                "department": department, "spent": new_spent,
                "annual_budget": annual, "order_id": row.get("order_id"),
            })
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
        return False


# ---------------------------------------------------------------------------
# set_status — authorized transitions with exactly-once budget
# ---------------------------------------------------------------------------
def set_status(ctx, order_id, new_status):
    """Authorized status transition with exactly-once budget side effects.

    Ownership-gated (owner OR same-company manager). Side effects:
      * pending_approval -> approved/pending/confirmed/...  => charge once.
      * any -> rejected / cancelled                         => refund once.
    Returns dict {success, order_id, status, charged, refunded, ...}.
    """
    new_status = (new_status or "").strip()
    if not new_status:
        return {"success": False, "error": "bad_status",
                "message": "Ugyldig status."}

    conn = _get_connection()
    if conn is None:
        return {"success": False, "error": "no_db",
                "message": "Databasen er ikke tilgængelig lige nu."}

    cur = None
    try:
        cur = _dict_cursor(conn)
        cur.execute("SELECT * FROM course_orders WHERE order_id = %s", (order_id,))
        row = cur.fetchone()
        if not row:
            return {"success": False, "error": "not_found",
                    "message": "Ordren blev ikke fundet."}

        if not _ownership_ok(ctx, row):
            return {"success": False, "error": "not_found",
                    "message": "Ordren blev ikke fundet."}

        old_status = (row.get("status") or "").strip()

        cur.execute(
            "UPDATE course_orders SET status = %s, updated_at = NOW() "
            "WHERE order_id = %s",
            (new_status, order_id),
        )
        # If an approver is acting, record approved_by.
        if new_status in ("approved", "confirmed") and ctx.user_id:
            try:
                cur.execute(
                    "UPDATE course_orders SET approved_by = %s WHERE order_id = %s",
                    (ctx.user_id, order_id),
                )
            except Exception:
                pass

        charged = False
        refunded = False

        if new_status in _CANCELLED_LIKE_STATUSES:
            refunded = _maybe_refund(cur, row)
        elif old_status in _NON_CHARGING_STATUSES and new_status not in _NON_CHARGING_STATUSES:
            # Order is leaving pending_approval into a live state -> charge once.
            charged = _maybe_charge(cur, row)

        _write_audit(
            cur,
            company_id=_int_or_none(row.get("company_id")),
            user_id=ctx.user_id,
            action="order.status_changed",
            resource_id=order_id,
            description=f"{old_status}->{new_status} charged={charged} refunded={refunded}",
        )

        conn.commit()

        # --- cross-integration side effects (post-commit, best-effort) ------
        company_id = _int_or_none(row.get("company_id"))
        # Budget overrun email: the charge above flagged a crossing on the row.
        # Send the branded alert to managers now (post-commit, own connection).
        _overrun = row.get("_budget_overrun")
        if _overrun:
            _send_budget_overrun_emails_safe(
                _overrun.get("company_id"),
                department=_overrun.get("department"),
                spent=_overrun.get("spent"),
                annual_budget=_overrun.get("annual_budget"),
                order_id=_overrun.get("order_id"),
            )
        if new_status in ("approved", "confirmed"):
            _event = "order.approved"
        elif new_status in _CANCELLED_LIKE_STATUSES:
            _event = "order.rejected"
        else:
            _event = "order.status_changed"
        # Shared payload shape for every order status side-effect event.
        _order_payload = {
            "order_id": order_id, "status": new_status,
            "previous_status": old_status, "charged": charged, "refunded": refunded,
            "product_title": row.get("product_title"),
            "department": (row.get("department") or "") or None,
            "user_email": row.get("user_email"),
        }
        _emit_event_safe(company_id, _event, _order_payload)
        # ALWAYS emit a generic order.updated on a real status transition so the
        # subscription UI's advertised 'order.updated' event actually fires (in
        # addition to the approved/rejected specials above). No-op when the
        # status did not actually change.
        if old_status != new_status:
            _emit_event_safe(company_id, "order.updated", _order_payload)
        # course.completed — when the order/completion transitions to completed.
        if new_status == "completed" and old_status != "completed":
            _emit_event_safe(company_id, "course.completed", {
                "order_id": order_id,
                "product_title": row.get("product_title"),
                "product_handle": row.get("product_handle"),
                "department": (row.get("department") or "") or None,
                "user_id": _int_or_none(row.get("user_id")),
                "user_email": row.get("user_email"),
                "completed_at": datetime.datetime.now().isoformat(),
            })
        # Tell the requester their order was decided.
        _requester_email = row.get("user_email")
        if _requester_email and new_status in ("approved", "confirmed", "rejected"):
            _decision = "afvist" if new_status == "rejected" else "godkendt"
            _send_email_safe(
                _requester_email,
                f"Din kursusbestilling er {_decision}",
                "order_approved", company_id,
                product_title=row.get("product_title", ""), order_id=order_id,
                decision=_decision,
                message=("Din bestilling blev desværre afvist."
                         if new_status == "rejected"
                         else "Din bestilling er godkendt — log ind for at komme i gang."),
            )

        return {
            "success": True,
            "order_id": order_id,
            "status": new_status,
            "previous_status": old_status,
            "charged": charged,
            "refunded": refunded,
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
