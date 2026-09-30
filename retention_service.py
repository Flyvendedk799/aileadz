"""
Data-retention job (S-4.3): personal data does not live forever by accident.

Covers chat transcripts, AI run/debug logs, API logs, e-mail logs and other
operational tables. Driven by the scheduler (daily job ``data_retention``); the
worker of N-8.1 simply keeps calling the scheduler.

Policy per table (``POLICIES``):
  * ``days``      default retention; override with ``RETENTION_<NAME>_DAYS`` in
                  the environment (``0`` / ``off`` switches that rule off);
  * ``action``    ``delete`` the rows, or ``scrub`` = keep the row (and so the
                  aggregate statistics) but null out the personal columns;
  * work is done in bounded batches (``BATCH``) so a large backlog never holds a
    long lock; one table failing never stops the others.

Not covered on purpose: ``audit_log`` (legal record; pseudonymised on erasure,
see gdpr_service) and the user's own profile data (kept until they delete it).
"""

import logging
import os
import re
import time

logger = logging.getLogger(__name__)

BATCH = 2000
MAX_BATCHES_PER_TABLE = 25   # 50 000 rows per table per pass; the rest next pass

# name -> policy. ``column`` is the timestamp the age is measured on; ``epoch``
# marks a numeric unix-time column instead of a DATETIME.
POLICIES = [
    dict(name="email_log", table="email_log", column="created_at", days=365, action="delete"),
    dict(name="api_request_logs", table="api_request_logs", column="created_at", days=180, action="delete"),
    dict(name="ai_agent_runs", table="ai_agent_runs", column="created_at", days=180, action="delete"),
    dict(name="ai_tool_runs", table="ai_tool_runs", column="created_at", days=180, action="delete"),
    # Chat transcripts: the row (counts, quality score, category) outlives the text.
    dict(name="chatbot_interactions", table="chatbot_interactions", column="created_at", days=730, action="scrub",
         set="username=NULL, query_text=NULL, response_text=NULL, user_location=NULL, products_shown=NULL",
         where="(username IS NOT NULL OR query_text IS NOT NULL OR response_text IS NOT NULL)"),
    dict(name="hr_chatbot_interactions", table="hr_chatbot_interactions", column="created_at", days=365,
         action="scrub", set="username=NULL, query_text=NULL, response_text=NULL",
         where="(username IS NOT NULL OR query_text IS NOT NULL OR response_text IS NOT NULL)"),
    dict(name="conversation_history", table="conversation_history", column="updated_at", days=730, action="delete"),
    dict(name="company_settings_history", table="company_settings_history", column="created_at", days=730,
         action="delete"),
    dict(name="company_notifications", table="company_notifications", column="created_at", days=730, action="delete"),
    dict(name="hr_notification_queue", table="hr_notification_queue", column="created_at", days=180,
         action="delete", where="is_dismissed = 1"),
    dict(name="event_outbox", table="event_outbox", column="created_at", days=30, action="delete",
         where="status = 'delivered'"),
    dict(name="password_reset_tokens", table="password_reset_tokens", column="expires_at", days=30, action="delete"),
    dict(name="dsr_requests", table="dsr_requests", column="handled_at", days=1095, action="delete",
         where="status IN ('completed','rejected')"),
    dict(name="ai_cv_parse_jobs", table="ai_cv_parse_jobs", column="expires_at", days=1, action="delete", epoch=True),
]

# AI store (app1/memory_store.py, N-3.3): MySQL ``ai_*`` tables in production, the
# same tables in a local SQLite file for development. Numeric epoch columns.
AI_STORE_POLICIES = [
    dict(name="ai_store_analytics", table="ai_analytics_events", column="timestamp", days=365,
         action="delete", epoch=True),
    dict(name="ai_store_debug_logs", table="ai_debug_logs", column="timestamp", days=14,
         action="delete", epoch=True),
    dict(name="ai_store_latency_logs", table="ai_latency_logs", column="timestamp", days=14,
         action="delete", epoch=True),
    dict(name="ai_store_sessions", table="ai_sessions", column="last_active", days=90,
         action="delete", epoch=True),
    dict(name="ai_store_anonymous_profiles", table="ai_anonymous_profiles", column="last_active", days=30,
         action="delete", epoch=True),
]
SQLITE_POLICIES = AI_STORE_POLICIES      # kept name: the dev-fallback runner and the admin overview use it

_NAME_OK = re.compile(r"^[a-z_]+$")


def effective_days(policy, environ=None):
    """Retention in days for ``policy`` after environment overrides; ``None``
    when the rule is switched off (``RETENTION_<NAME>_DAYS=0`` or ``off``)."""
    environ = os.environ if environ is None else environ
    raw = environ.get("RETENTION_%s_DAYS" % policy["name"].upper())
    if raw is None or str(raw).strip() == "":
        return policy["days"]
    raw = str(raw).strip().lower()
    if raw in ("0", "off", "false", "no", "never"):
        return None
    try:
        days = int(raw)
    except ValueError:
        logger.warning("retention: ignoring bad RETENTION_%s_DAYS=%r", policy["name"].upper(), raw)
        return policy["days"]
    return days if days > 0 else None


def build_statement(policy, days, now=None):
    """(sql, params) for one bounded batch of a MySQL policy. Identifiers come
    from the static POLICIES list (never from input), still validated."""
    table, column = policy["table"], policy["column"]
    if not (_NAME_OK.match(table) and _NAME_OK.match(column)):
        raise ValueError("bad identifier in retention policy")
    now = time.time() if now is None else now
    cutoff = now - days * 86400
    if policy.get("epoch"):
        age, params = "`%s` < %%s" % column, [cutoff]
    else:
        age, params = "`%s` < FROM_UNIXTIME(%%s)" % column, [cutoff]
    where = age + (" AND " + policy["where"] if policy.get("where") else "")
    if policy["action"] == "delete":
        return "DELETE FROM `%s` WHERE %s LIMIT %d" % (table, where, BATCH), params
    return "UPDATE `%s` SET %s WHERE %s LIMIT %d" % (table, policy["set"], where, BATCH), params


def _run_policy(conn, policy, days, now=None, dry_run=False):
    sql, params = build_statement(policy, days, now)
    if dry_run:
        count_sql = "SELECT COUNT(*) FROM `%s` WHERE %s" % (policy["table"], sql.split(" WHERE ", 1)[1].rsplit(" LIMIT ", 1)[0])
        cur = conn.cursor()
        try:
            cur.execute(count_sql, params)
            row = cur.fetchone()
            return int((row.get("COUNT(*)") if isinstance(row, dict) else row[0]) or 0) if row else 0
        finally:
            cur.close()
    total = 0
    for _ in range(MAX_BATCHES_PER_TABLE):
        cur = conn.cursor()
        try:
            cur.execute(sql, params)
            n = int(cur.rowcount or 0)
            conn.commit()
        finally:
            cur.close()
        total += n
        if n < BATCH:
            break
    return total


def _ai_store_is_sqlite():
    """True when the AI store runs on the local SQLite fallback (development)."""
    try:
        from app1 import memory_store
        return memory_store._backend() == "sqlite"
    except Exception:
        return False


def _run_sqlite(now=None, dry_run=False, environ=None):
    out = {}
    try:
        from app1 import memory_store
        conn = memory_store._get_conn()
    except Exception as exc:
        return {"error": str(exc)}
    now = time.time() if now is None else now
    for policy in SQLITE_POLICIES:
        days = effective_days(policy, environ)
        if days is None:
            out[policy["name"]] = "off"
            continue
        cutoff = now - days * 86400
        try:
            if dry_run:
                row = conn.execute("SELECT COUNT(*) FROM %s WHERE %s < ?" % (policy["table"], policy["column"]),
                                   (cutoff,)).fetchone()
                out[policy["name"]] = int(row[0] if row else 0)
            else:
                cur = conn.execute("DELETE FROM %s WHERE %s < ?" % (policy["table"], policy["column"]), (cutoff,))
                conn.commit()
                out[policy["name"]] = int(cur.rowcount or 0)
        except Exception as exc:
            out[policy["name"]] = "error: %s" % exc
    return out


def run_retention(conn=None, *, now=None, dry_run=False, environ=None):
    """One retention pass. Returns ``{policy_name: rows | 'off' | 'error: ...'}``.
    Never raises: a table that does not exist (yet) is reported and skipped."""
    summary = {}
    if conn is None:
        try:
            from flask import current_app
            conn = current_app.mysql.connection
        except Exception:
            conn = None
    use_sqlite = _ai_store_is_sqlite()
    for policy in (POLICIES if use_sqlite else POLICIES + AI_STORE_POLICIES):
        days = effective_days(policy, environ)
        if days is None:
            summary[policy["name"]] = "off"
            continue
        if conn is None:
            summary[policy["name"]] = "error: no database"
            continue
        try:
            summary[policy["name"]] = _run_policy(conn, policy, days, now=now, dry_run=dry_run)
        except Exception as exc:
            summary[policy["name"]] = "error: %s" % exc
            try:
                conn.rollback()
            except Exception:
                pass
            logger.info("retention: %s skipped: %s", policy["name"], exc)
    if use_sqlite:
        summary.update(_run_sqlite(now=now, dry_run=dry_run, environ=environ))
    return summary


def policy_table():
    """Human-readable overview (admin page / docs): name, action, effective days."""
    rows = []
    for p in POLICIES:
        rows.append({"name": p["name"], "table": p["table"], "action": p["action"], "days": effective_days(p)})
    for p in AI_STORE_POLICIES:
        rows.append({"name": p["name"], "table": p["table"] + " (AI store)", "action": "delete", "days": effective_days(p)})
    return rows
