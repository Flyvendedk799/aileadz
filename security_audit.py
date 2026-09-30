"""
Best-effort security audit helper shared by the Part A (security) code paths.

Writes one ``audit_log`` row (same shape as order_service / gdpr_routes). It
never raises and never blocks the request: an audit failure is logged, not
surfaced. The actor is taken from the Flask session when available.
"""

import json
import logging

logger = logging.getLogger(__name__)


def audit(action, resource_type="security", resource_id="", description="",
          company_id=None, user_id=None, details=None):
    """Insert an audit_log row. Returns True if a row was written."""
    try:
        from flask import current_app, request, session

        mysql = getattr(current_app, "mysql", None)
        if mysql is None:
            return False
        try:
            from db_compat import refresh_flask_mysql_connection

            refresh_flask_mysql_connection(mysql)
        except Exception:
            pass
        actor = session.get("user")
        if isinstance(details, (dict, list)):
            details = json.dumps(details, ensure_ascii=False, default=str)
        text = description or ""
        if actor:
            text = ("[%s] %s" % (actor, text)).strip()
        conn = mysql.connection
        cur = conn.cursor()
        try:
            cur.execute(
                """
                INSERT INTO audit_log
                    (company_id, user_id, action, action_type, resource_type,
                     resource_id, description, details, ip_address)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    company_id if company_id is not None else session.get("company_id"),
                    user_id if user_id is not None else session.get("user_id"),
                    action,
                    action,
                    resource_type,
                    str(resource_id or "")[:100],
                    text[:4000],
                    (details or "")[:4000] if details else None,
                    (request.remote_addr or "")[:50],
                ),
            )
            conn.commit()
            return True
        finally:
            try:
                cur.close()
            except Exception:
                pass
    except Exception as exc:  # pragma: no cover - audit must never break the op
        logger.debug("security audit skipped (%s): %s", action, exc)
        return False
