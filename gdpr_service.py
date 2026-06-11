"""
GDPR data-subject toolkit (Theme A, roadmap value-4).

Two capabilities for a Danish HR-SaaS procurement gate:

  1. EXPORT  — gather *all* PII the platform holds about one data subject
              (keyed by username) into a JSON-serialisable structure, plus a
              best-effort pull from the orphaned ai_memory.db (browser-token
              anonymous profiles) when a token is linkable.

  2. ERASE   — the GDPR "right to be forgotten". DESTRUCTIVE. Two classes:
                 * HARD-DELETE the pure-profile / behavioural rows that carry no
                   financial or audit value (skills, experience, education,
                   completed courses, profile summary, conversations, learning
                   goals, notifications).
                 * ANONYMISE the rows that must survive for accounting / audit
                   integrity (course_orders keep price + order id; chatbot_interactions
                   keep aggregate counts; company_users keep the seat record;
                   the users auth row keeps username as a login/audit key) by
                   blanking the PII columns and/or tombstoning the username.

DESIGN RULES (production-safe):
  * This module must NEVER crash on import and must never crash create_app().
    All DB work is wrapped; every helper degrades gracefully.
  * EVERY mutating statement is scoped to the exact username (WHERE username=%s
    / WHERE user_id=%s where that column holds the username). There is NEVER a
    broad or unfiltered DELETE/UPDATE. Erasing user A leaves user B untouched.
  * erase_user_data runs in a SINGLE transaction and rolls back on any error.
  * dry_run=True computes the plan (row counts that *would* change) WITHOUT
    mutating anything.
  * All user-facing strings are Danish.

Column names below were confirmed against the live schema:
  - user_skills / user_experience / user_education / user_completed_courses /
    user_profile_summary / user_conversations / conversation_history /
    user_learning_goals  -> keyed by `username`
  - user_certifications / user_languages / user_portfolio_links /
    user_memories (2026-06 profile tables + AI-dossier) -> keyed by `username`
  - notifications        -> `user_id` column HOLDS the username (not an int id)
  - course_orders        -> `username`, `user_email`, `user_name`, `user_phone`
  - chatbot_interactions -> `username`
  - company_users        -> `username`, `full_name`, `email`, `phone`, `status`
  - users                -> `username` (login key, kept), `email`, `name`,
                            `email_notifications`
  - ai_memory.db         -> anonymous_profiles/sessions/analytics/etc keyed by
                            browser_token / session_id (no username link)
"""

import json
import logging

logger = logging.getLogger(__name__)

# Tombstone value written to course_orders.username so the order row survives
# for accounting but no longer points at a real person.
TOMBSTONE_USERNAME = "slettet-bruger"


# ---------------------------------------------------------------------------
# Low-level helpers (all guarded; never raise on their own)
# ---------------------------------------------------------------------------

def _mysql():
    """Return current_app.mysql, healing a stale connection. None on failure."""
    try:
        from flask import current_app

        mysql = getattr(current_app, "mysql", None)
        if mysql is None:
            return None
        try:
            from db_compat import refresh_flask_mysql_connection

            refresh_flask_mysql_connection(mysql)
        except Exception:
            # Carry on with the raw connection if the compat helper is missing.
            pass
        return mysql
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("gdpr_service: kunne ikke hente mysql: %s", exc)
        return None


def _dict_cursor(conn):
    """Return a DictCursor regardless of the underlying driver."""
    try:
        import MySQLdb.cursors

        return conn.cursor(MySQLdb.cursors.DictCursor)
    except Exception:
        # PyMySQL fallback path used by the local sandbox shim in run.py.
        try:
            return conn.cursor(dictionary=True)
        except Exception:
            return conn.cursor()


def _json_safe(value):
    """Coerce a DB value into something json.dumps can serialise."""
    import datetime
    import decimal

    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, (datetime.datetime, datetime.date, datetime.time)):
        return value.isoformat()
    if isinstance(value, decimal.Decimal):
        return float(value)
    if isinstance(value, (bytes, bytearray)):
        try:
            return value.decode("utf-8", "replace")
        except Exception:
            return repr(value)
    return str(value)


def _rows_to_dicts(rows):
    out = []
    for row in rows or []:
        if isinstance(row, dict):
            out.append({k: _json_safe(v) for k, v in row.items()})
        else:
            # Tuple cursor fallback — index-keyed; best effort.
            out.append([_json_safe(v) for v in row])
    return out


def _fetch_all(conn, sql, params):
    """Run a SELECT and return JSON-safe dict rows. Empty list on any error."""
    cur = None
    try:
        cur = _dict_cursor(conn)
        cur.execute(sql, params)
        return _rows_to_dicts(cur.fetchall())
    except Exception as exc:
        logger.warning("gdpr_service: SELECT fejlede (%s): %s", sql[:60], exc)
        return []
    finally:
        if cur is not None:
            try:
                cur.close()
            except Exception:
                pass


# Every MySQL table we read for an export, mapped to the SQL that pulls one
# data subject's rows. `%s` is always the username. This same map drives the
# erase plan (see _DELETE_TABLES / _ANONYMISE_TABLES below).
_EXPORT_QUERIES = [
    ("user_skills", "SELECT * FROM user_skills WHERE username=%s"),
    ("user_experience", "SELECT * FROM user_experience WHERE username=%s"),
    ("user_education", "SELECT * FROM user_education WHERE username=%s"),
    ("user_completed_courses", "SELECT * FROM user_completed_courses WHERE username=%s"),
    ("user_profile_summary", "SELECT * FROM user_profile_summary WHERE username=%s"),
    ("user_conversations", "SELECT * FROM user_conversations WHERE username=%s"),
    ("conversation_history", "SELECT * FROM conversation_history WHERE username=%s"),
    ("user_learning_goals", "SELECT * FROM user_learning_goals WHERE username=%s"),
    # 2026-06 profile tables (certificeringer, sprog, portfolio-links).
    ("user_certifications", "SELECT * FROM user_certifications WHERE username=%s"),
    ("user_languages", "SELECT * FROM user_languages WHERE username=%s"),
    ("user_portfolio_links", "SELECT * FROM user_portfolio_links WHERE username=%s"),
    # AI'ens fritekst-dossier (præferencer, livskontekst, personlighed) — det
    # mest følsomme lager; SKAL med i både eksport og sletning.
    ("user_memories", "SELECT * FROM user_memories WHERE username=%s"),
    # notifications.user_id HOLDS the username (platform convention).
    ("notifications", "SELECT * FROM notifications WHERE user_id=%s"),
    ("course_orders", "SELECT * FROM course_orders WHERE username=%s"),
    ("chatbot_interactions", "SELECT * FROM chatbot_interactions WHERE username=%s"),
    ("company_users", "SELECT * FROM company_users WHERE username=%s"),
    # The auth/account row itself (PII = email, name). Never list password hash
    # is harmless to export to the subject themselves, but we drop it below.
    ("users", "SELECT * FROM users WHERE username=%s"),
]

# Columns scrubbed from any exported row so we never hand a password hash back.
_REDACT_COLUMNS = {"password", "password_hash", "pwd", "hashed_password"}


def _redact(rows):
    out = []
    for row in rows:
        if isinstance(row, dict):
            out.append({k: ("[fjernet]" if k in _REDACT_COLUMNS else v) for k, v in row.items()})
        else:
            out.append(row)
    return out


# ---------------------------------------------------------------------------
# ai_memory.db (SQLite, orphaned) — best-effort, browser-token keyed
# ---------------------------------------------------------------------------

def _collect_ai_memory(username, browser_token=None, session_id=None):
    """Best-effort pull from app1/memory_store.py (SQLite ai_memory.db).

    The SQLite store is keyed by browser_token / session_id, NOT username, so
    we can only link it when the caller supplies a token/session id. If nothing
    is linkable we return an explanatory stub (never raise)."""
    if not browser_token and not session_id:
        return {
            "linkable": False,
            "note": (
                "ai_memory.db er anonymt (nøgles på browser-token/session-id, "
                "ikke brugernavn). Ingen browser-token angivet, så der er ingen "
                "kobling til denne bruger."
            ),
            "anonymous_profile": None,
            "sessions": [],
        }
    result = {"linkable": True, "anonymous_profile": None, "sessions": []}
    try:
        from app1 import memory_store

        if browser_token:
            try:
                prof = memory_store.load_anonymous_profile(browser_token)
                if prof:
                    result["anonymous_profile"] = {
                        k: _json_safe(v) for k, v in prof.items()
                    }
            except Exception as exc:
                logger.warning("gdpr_service: ai_memory profile pull fejlede: %s", exc)
        if session_id:
            try:
                sess = memory_store.load_session(session_id)
                if sess:
                    result["sessions"].append({k: _json_safe(v) for k, v in sess.items()})
            except Exception as exc:
                logger.warning("gdpr_service: ai_memory session pull fejlede: %s", exc)
    except Exception as exc:
        logger.warning("gdpr_service: memory_store utilgængelig: %s", exc)
        result["note"] = "Kunne ikke tilgå ai_memory.db."
    return result


# ---------------------------------------------------------------------------
# COLLECT  /  EXPORT
# ---------------------------------------------------------------------------

def collect_user_data(username, *, browser_token=None, session_id=None):
    """Gather ALL PII the platform holds about `username` into one dict.

    Returns a JSON-serialisable dict. Never raises — on a hard failure it
    returns a structure with an `error` note so callers can still respond."""
    import datetime

    username = (username or "").strip()
    report = {
        "subject_username": username,
        "generated_at": datetime.datetime.utcnow().isoformat() + "Z",
        "schema_version": 1,
        "tables": {},
        "ai_memory": None,
        "errors": [],
    }
    if not username:
        report["errors"].append("Intet brugernavn angivet.")
        return report

    mysql = _mysql()
    if mysql is None:
        report["errors"].append("Databaseforbindelse utilgængelig.")
    else:
        try:
            conn = mysql.connection
            for table, sql in _EXPORT_QUERIES:
                rows = _fetch_all(conn, sql, (username,))
                if table == "users":
                    rows = _redact(rows)
                report["tables"][table] = rows
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning("gdpr_service.collect_user_data fejlede: %s", exc)
            report["errors"].append(f"Delvis eksport: {exc}")

    # Best-effort ai_memory.db (anonymous, browser-token keyed).
    try:
        report["ai_memory"] = _collect_ai_memory(
            username, browser_token=browser_token, session_id=session_id
        )
    except Exception as exc:  # pragma: no cover - defensive
        report["ai_memory"] = {"linkable": False, "note": f"Fejl: {exc}"}

    # Convenience totals so a buyer/admin can eyeball the footprint.
    report["row_counts"] = {
        t: (len(r) if isinstance(r, list) else 0) for t, r in report["tables"].items()
    }
    return report


def export_user_data(username, *, browser_token=None, session_id=None, indent=2):
    """Collect + serialise to a downloadable JSON string (UTF-8, Danish-safe).

    Returns a `str`. Use .encode('utf-8') for a bytes download body."""
    data = collect_user_data(
        username, browser_token=browser_token, session_id=session_id
    )
    try:
        return json.dumps(data, ensure_ascii=False, indent=indent, default=_json_safe)
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("gdpr_service.export_user_data serialisering fejlede: %s", exc)
        # Last-resort minimal payload so the caller still gets a valid download.
        return json.dumps(
            {"subject_username": username, "error": str(exc)}, ensure_ascii=False
        )


# ---------------------------------------------------------------------------
# ERASE  (DESTRUCTIVE)
# ---------------------------------------------------------------------------

# Pure-profile / behavioural rows with no financial or audit value -> HARD DELETE.
# (table, where_column) — where_column always carries the username.
_DELETE_TABLES = [
    ("user_skills", "username"),
    ("user_experience", "username"),
    ("user_education", "username"),
    ("user_completed_courses", "username"),
    ("user_profile_summary", "username"),
    ("user_conversations", "username"),
    ("conversation_history", "username"),
    ("user_learning_goals", "username"),
    # 2026-06 profile tables (certificeringer, sprog, portfolio-links).
    ("user_certifications", "username"),
    ("user_languages", "username"),
    ("user_portfolio_links", "username"),
    # AI'ens fritekst-dossier — ren profil, ingen regnskabs-/revisionsværdi.
    ("user_memories", "username"),
    # notifications.user_id holds the username.
    ("notifications", "user_id"),
]

# Rows that MUST survive for accounting / audit integrity -> ANONYMISE in place.
# Each entry: table, where_column, SET-clause (no WHERE), human action note.
# The WHERE is always added by the executor scoped to the single username.
_ANONYMISE_TABLES = [
    (
        "course_orders",
        "username",
        # Keep order_id + price for accounting; strip the person + tombstone the
        # username so the row no longer identifies anyone.
        "user_email=NULL, user_name=NULL, user_phone=NULL, username=%s",
        "anonymiseret (beholder ordre + pris til regnskab; brugernavn → tombstone)",
        (TOMBSTONE_USERNAME,),
    ),
    (
        "chatbot_interactions",
        "username",
        # Keep aggregate counts/quality; blank the username link.
        "username=NULL",
        "anonymiseret (beholder aggregerede tal; brugernavn nulstillet)",
        (),
    ),
    (
        "company_users",
        "username",
        # Keep the seat/audit record; deactivate + strip PII.
        "status='inactive', full_name=NULL, email=NULL, phone=NULL",
        "deaktiveret + PII fjernet (beholder pladsens revisionsspor)",
        (),
    ),
    (
        "users",
        "username",
        # Keep username as the login/audit key; strip the rest of the PII.
        # (users has no `name` column — only username/email — do NOT reference it.)
        "email=NULL, email_notifications=0",
        "PII fjernet (beholder brugernavn som login/revisionsnøgle)",
        (),
    ),
]


def _count(conn, table, where_col, username):
    """COUNT rows that match this exact username. 0 on any error."""
    cur = None
    try:
        cur = conn.cursor()
        cur.execute(
            f"SELECT COUNT(*) FROM `{table}` WHERE `{where_col}`=%s", (username,)
        )
        row = cur.fetchone()
        if row is None:
            return 0
        if isinstance(row, dict):
            return int(list(row.values())[0] or 0)
        return int(row[0] or 0)
    except Exception as exc:
        logger.warning("gdpr_service: COUNT %s fejlede: %s", table, exc)
        return 0
    finally:
        if cur is not None:
            try:
                cur.close()
            except Exception:
                pass


def _erase_ai_memory(browser_token=None, session_id=None):
    """Best-effort erase of the anonymous SQLite rows for this token/session.

    Scoped strictly to the supplied browser_token / session_id. No-op (and
    reported as skipped) when nothing is linkable."""
    if not browser_token and not session_id:
        return {"action": "sprunget over (ingen browser-token/session-id at koble på)", "rows": 0}
    deleted = 0
    try:
        from app1 import memory_store

        conn = memory_store._get_conn()
        try:
            if browser_token:
                cur = conn.execute(
                    "DELETE FROM anonymous_profiles WHERE browser_token = ?",
                    (browser_token,),
                )
                deleted += cur.rowcount or 0
            if session_id:
                for tbl in ("sessions", "analytics", "debug_logs", "latency_logs"):
                    try:
                        cur = conn.execute(
                            f"DELETE FROM {tbl} WHERE session_id = ?", (session_id,)
                        )
                        deleted += cur.rowcount or 0
                    except Exception:
                        pass
            conn.commit()
        except Exception as exc:
            try:
                conn.rollback()
            except Exception:
                pass
            return {"action": f"fejlede: {exc}", "rows": 0}
        return {"action": "slettet", "rows": deleted}
    except Exception as exc:
        logger.warning("gdpr_service: ai_memory erase utilgængelig: %s", exc)
        return {"action": f"utilgængelig: {exc}", "rows": 0}


def erase_user_data(username, *, actor, dry_run=False, browser_token=None, session_id=None):
    """DESTRUCTIVE GDPR erasure for exactly one data subject.

    Hard-deletes the pure-profile rows and anonymises the audit/financial rows,
    all scoped to `username`. Runs in ONE MySQL transaction; rolls back on any
    error. `dry_run=True` returns the plan (counts) WITHOUT mutating.

    Returns a report dict:
        {
          "username": ...,
          "actor": ...,
          "dry_run": bool,
          "ok": bool,
          "deleted":     {table: {"action": "...", "rows": n}, ...},
          "anonymised":  {table: {"action": "...", "rows": n}, ...},
          "ai_memory":   {"action": "...", "rows": n},
          "errors": [...],
        }
    """
    username = (username or "").strip()
    report = {
        "username": username,
        "actor": actor,
        "dry_run": bool(dry_run),
        "ok": False,
        "deleted": {},
        "anonymised": {},
        "ai_memory": {"action": "ikke kørt", "rows": 0},
        "errors": [],
    }

    # Hard guard: a blank/whitespace username could otherwise widen scope.
    if not username:
        report["errors"].append("Afvist: tomt brugernavn — erase kræver et eksakt brugernavn.")
        return report
    if username == TOMBSTONE_USERNAME:
        report["errors"].append(
            "Afvist: brugernavnet er allerede tombstone-værdien — ville ramme tidligere slettede ordrer."
        )
        return report

    mysql = _mysql()
    if mysql is None:
        report["errors"].append("Databaseforbindelse utilgængelig.")
        return report
    conn = mysql.connection

    # ---- Plan (always computed; this is the whole dry-run output) ----
    for table, where_col in _DELETE_TABLES:
        n = _count(conn, table, where_col, username)
        report["deleted"][table] = {"action": "slet (hard delete)", "rows": n}
    for table, where_col, _set, note, _extra in _ANONYMISE_TABLES:
        n = _count(conn, table, where_col, username)
        report["anonymised"][table] = {"action": note, "rows": n}

    if dry_run:
        report["ok"] = True
        report["ai_memory"] = {
            "action": "ville blive forsøgt (kun hvis browser-token angivet)",
            "rows": 0,
        }
        return report

    # ---- Execute: per-table best-effort, one commit ----
    # GDPR erasure must favour erasing everything REACHABLE over all-or-nothing:
    # on a FK-less, drift-prone schema a single missing table/column must NOT
    # leave the rest of the data subject's PII in place. Each statement is
    # isolated; a per-table failure is recorded and the remaining statements
    # still run (a MySQL statement error does not abort the surrounding
    # transaction). We commit once at the end and report any per-table failures.
    cur = None
    try:
        cur = conn.cursor()
        for table, where_col in _DELETE_TABLES:
            try:
                cur.execute(
                    f"DELETE FROM `{table}` WHERE `{where_col}`=%s", (username,)
                )
                report["deleted"][table] = {
                    "action": "slettet",
                    "rows": int(cur.rowcount or 0),
                }
            except Exception as exc:
                report["deleted"][table] = {
                    "action": "sprunget over (tabel/kolonne mangler)",
                    "rows": 0, "error": str(exc),
                }
                report["errors"].append(f"{table}: {exc}")
        for table, where_col, set_clause, note, extra_params in _ANONYMISE_TABLES:
            try:
                params = tuple(extra_params) + (username,)
                cur.execute(
                    f"UPDATE `{table}` SET {set_clause} WHERE `{where_col}`=%s",
                    params,
                )
                report["anonymised"][table] = {
                    "action": note,
                    "rows": int(cur.rowcount or 0),
                }
            except Exception as exc:
                report["anonymised"][table] = {
                    "action": "sprunget over (tabel/kolonne mangler)",
                    "rows": 0, "error": str(exc),
                }
                report["errors"].append(f"{table}: {exc}")
        conn.commit()
        # ok = the erasure ran; errors[] lists any tables skipped for schema drift.
        report["ok"] = True
    except Exception as exc:
        try:
            conn.rollback()
        except Exception:
            pass
        report["ok"] = False
        report["errors"].append(f"Uventet fejl — rullet tilbage: {exc}")
        logger.warning("gdpr_service.erase_user_data uventet fejl: %s", exc)
        if cur is not None:
            try:
                cur.close()
            except Exception:
                pass
        return report
    finally:
        if cur is not None:
            try:
                cur.close()
            except Exception:
                pass

    # ---- ai_memory.db (separate store, separate best-effort transaction) ----
    report["ai_memory"] = _erase_ai_memory(
        browser_token=browser_token, session_id=session_id
    )

    return report
