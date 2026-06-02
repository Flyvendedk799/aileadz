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

        # --- 2. budget-aware approval ------------------------------------
        budget_warning = None
        fiscal_year = datetime.datetime.now().year
        dept = ctx.department or extra.get("department") or ""
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
                    # Do NOT silently overspend — route to approval.
                    needs_approval = True
                    budget_warning = (
                        f"Bestillingen på {price_f:.0f} kr. overskrider afdelingens "
                        f"resterende budget på {remaining:.0f} kr. "
                        f"(brugt {spent:.0f} af {annual:.0f} kr.). "
                        f"Ordren er sendt til godkendelse i stedet for at blive afvist."
                    )

        # --- 3. resolve status & INSERT course_orders --------------------
        if needs_approval:
            initial_status = "pending_approval"
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
        _write_audit(
            cur,
            company_id=ctx.company_id,
            user_id=ctx.user_id,
            action="order.created",
            resource_id=order_id,
            description=f"{product_title} ({ctx.source})",
        )

        conn.commit()

        return {
            "success": True,
            "order_id": order_id,
            "status": initial_status,
            "needs_approval": needs_approval,
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
