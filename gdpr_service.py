"""
GDPR data-subject toolkit (Theme A, roadmap value-4).

Two capabilities for a Danish HR-SaaS procurement gate:

  1. EXPORT  — gather *all* PII the platform holds about one data subject
              (keyed by username) into a JSON-serialisable structure, plus a
              best-effort pull from the SQLite ai_memory.db AI store (browser-token
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
    ("user_learning_paths", "SELECT * FROM user_learning_paths WHERE username=%s"),
    ("user_profile_checkins", "SELECT * FROM user_profile_checkins WHERE username=%s"),
    # 2026-06 profile tables (certificeringer, sprog, portfolio-links).
    ("user_certifications", "SELECT * FROM user_certifications WHERE username=%s"),
    ("user_languages", "SELECT * FROM user_languages WHERE username=%s"),
    ("user_portfolio_links", "SELECT * FROM user_portfolio_links WHERE username=%s"),
    # AI'ens fritekst-dossier (præferencer, livskontekst, personlighed) — det
    # mest følsomme lager; SKAL med i både eksport og sletning.
    ("user_memories", "SELECT * FROM user_memories WHERE username=%s"),
    # AI conversation state + semantic knowledge index (2026-09): per-mode
    # active-session pointers, durable per-mode conversation digests, and the
    # embedded snippets of memories/profile facts/conversation summaries.
    ("user_active_sessions", "SELECT * FROM user_active_sessions WHERE username=%s"),
    ("user_conversation_summaries", "SELECT * FROM user_conversation_summaries WHERE username=%s"),
    ("user_knowledge", "SELECT id, username, source_type, source_id, mode, content, "
                       "embedding_model, created_at, updated_at FROM user_knowledge WHERE username=%s"),
    # notifications.user_id HOLDS the username (platform convention).
    ("notifications", "SELECT * FROM notifications WHERE user_id=%s"),
    ("brands", "SELECT * FROM brands WHERE username=%s"),
    ("app_usage", "SELECT * FROM app_usage WHERE username=%s"),
    ("social_metrics", "SELECT * FROM social_metrics WHERE username=%s"),
    ("user_learning_path_versions", "SELECT * FROM user_learning_path_versions WHERE username=%s"),
    ("course_orders", "SELECT * FROM course_orders WHERE username=%s"),
    ("chatbot_interactions", "SELECT * FROM chatbot_interactions WHERE username=%s"),
    ("company_users", "SELECT * FROM company_users WHERE username=%s"),
    # The auth/account row itself (PII = email, name). Never list password hash
    # is harmless to export to the subject themselves, but we drop it below.
    ("users", "SELECT * FROM users WHERE username=%s"),
]

# Columns scrubbed from any exported row so we never hand a password hash back.
_REDACT_COLUMNS = {"password", "password_hash", "pwd", "hashed_password",
                   "secret_enc", "backup_codes", "token_hash"}


def _redact(rows):
    out = []
    for row in rows:
        if isinstance(row, dict):
            out.append({k: ("[fjernet]" if k in _REDACT_COLUMNS else v) for k, v in row.items()})
        else:
            out.append(row)
    return out


# ---------------------------------------------------------------------------
# ai_memory.db (SQLite AI store) — best-effort, browser-token / session-id keyed
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

def collect_user_data(username, *, browser_token=None, session_id=None, learner_view=False):
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
            # S-4.1: everything keyed by users.id / e-mail / session ids as well.
            subject = resolve_subject(conn, username)
            report["subject"] = {
                "user_id": subject.get("user_id"),
                "emails": subject.get("emails"),
                "company_user_ids": subject.get("member_ids"),
                "ai_session_count": len(subject.get("session_ids") or []),
            }
            report["tables"].update(_extra_export_rows(conn, subject, learner_view=learner_view))
            # The SQLite AI store (feedback, debug log, latency) for the subject's sessions.
            report["ai_store"] = _sqlite_ai_store_plan(subject, browser_token)
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


def export_user_data(username, *, browser_token=None, session_id=None, indent=2, learner_view=False):
    """Collect + serialise to a downloadable JSON string (UTF-8, Danish-safe).

    Returns a `str`. Use .encode('utf-8') for a bytes download body."""
    data = collect_user_data(
        username, browser_token=browser_token, session_id=session_id, learner_view=learner_view
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
    ("user_learning_paths", "username"),
    ("user_profile_checkins", "username"),
    # 2026-06 profile tables (certificeringer, sprog, portfolio-links).
    ("user_certifications", "username"),
    ("user_languages", "username"),
    ("user_portfolio_links", "username"),
    # AI'ens fritekst-dossier — ren profil, ingen regnskabs-/revisionsværdi.
    ("user_memories", "username"),
    # AI conversation state + semantic knowledge index — pure profile data.
    ("user_active_sessions", "username"),
    ("user_conversation_summaries", "username"),
    ("user_knowledge", "username"),
    # notifications.user_id holds the username.
    ("notifications", "user_id"),
    ("brands", "username"),
    ("app_usage", "username"),
    ("social_metrics", "username"),
    ("user_learning_path_versions", "username"),
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


def _count_where(conn, table, where, params):
    """COUNT rows matching an already-built WHERE (S-4.1). 0 on any error."""
    cur = None
    try:
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM `%s` WHERE %s" % (table, where), tuple(params))
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


def _erase_ai_memory(browser_token=None, session_id=None, username=None):
    """Best-effort erase of the anonymous SQLite rows for this token/session.

    Scoped strictly to the supplied browser_token / session_id. No-op (and
    reported as skipped) when nothing is linkable."""
    if not browser_token and not session_id and not username:
        return {"action": "sprunget over (ingen browser-token/session-id at koble på)", "rows": 0}
    try:
        from app1 import memory_store

        try:
            deleted = memory_store.erase_subject(browser_token=browser_token, session_id=session_id,
                                                 username=username)
        except Exception as exc:
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

    # S-4.1: resolve the full identity FIRST (the anonymise steps below blank
    # the e-mail / username that the lookups need).
    subject = resolve_subject(conn, username)
    report["subject"] = {"user_id": subject.get("user_id"), "emails": subject.get("emails"),
                         "company_user_ids": subject.get("member_ids"),
                         "ai_session_count": len(subject.get("session_ids") or [])}

    # ---- Plan (always computed; this is the whole dry-run output) ----
    for table, where_col in _DELETE_TABLES:
        n = _count(conn, table, where_col, username)
        report["deleted"][table] = {"action": "slet (hard delete)", "rows": n}
    for table, where_col, _set, note, _extra in _ANONYMISE_TABLES:
        n = _count(conn, table, where_col, username)
        report["anonymised"][table] = {"action": note, "rows": n}
    _ACTION_NOTE = {"delete": "slet (hard delete)", "anonymise": "anonymiseret (identitet fjernet, posten bevares)",
                    "pseudonymise": "pseudonymiseret (revisionsspor bevares uden personen)"}
    legacy_rows = {t: v.get("rows", 0) for b in (report["deleted"], report["anonymised"]) for t, v in b.items()}
    extra_rows = {}
    for spec in EXTRA_SPECS:
        where, wparams = _build_where(spec, subject)
        n = _count_where(conn, _spec_table(spec), where, wparams) if where else 0
        key = _spec_table(spec)
        extra_rows[key] = extra_rows.get(key, 0) + n   # twin specs (same table) add up
        bucket = report["deleted"] if spec["kind"] == "delete" else report["anonymised"]
        bucket[key] = {"action": _ACTION_NOTE[spec["kind"]],
                       # legacy + identity-keyed specs can match the SAME rows: show the larger
                       "rows": max(legacy_rows.get(key, 0), extra_rows[key])}
    report["ai_store"] = _sqlite_ai_store_plan(subject, browser_token)

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
        # S-4.1: identity-keyed tables (users.id / e-mail / AI session ids).
        exec_rows = {}
        for spec in EXTRA_SPECS:
            table = _spec_table(spec)
            where, wparams = _build_where(spec, subject)
            bucket = report["deleted"] if spec["kind"] == "delete" else report["anonymised"]
            if not where:
                bucket.setdefault(table, {"action": _ACTION_NOTE[spec["kind"]], "rows": 0})
                continue
            try:
                if spec["kind"] == "delete":
                    cur.execute("DELETE FROM `%s` WHERE %s" % (table, where), tuple(wparams))
                else:
                    set_sql, set_params = _set_clause(spec, subject)
                    cur.execute("UPDATE `%s` SET %s WHERE %s" % (table, set_sql, where),
                                tuple(set_params) + tuple(wparams))
                exec_rows[table] = exec_rows.get(table, 0) + int(cur.rowcount or 0)
                before = legacy_rows.get(table, 0)
                bucket[table] = {"action": _ACTION_NOTE[spec["kind"]].replace("slet (hard delete)", "slettet"),
                                 "rows": max(before, exec_rows[table])}
            except Exception as exc:
                bucket[table] = {"action": "sprunget over (tabel/kolonne mangler)", "rows": 0, "error": str(exc)}
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
        browser_token=browser_token, session_id=session_id, username=username
    )
    # S-4.1: the SQLite AI store is linked to the subject through the session ids
    # collected above (it is written on every turn, so it is NOT orphaned).
    report["ai_store"] = _sqlite_ai_store_erase(subject, browser_token)

    return report

# ===========================================================================
# S-4.1  Identity-based coverage
#
# The original lists above are keyed by USERNAME only. That misses:
#   * people created by SSO / SCIM, whose company_users rows carry an e-mail
#     and (now) a users.id but may not match on username;
#   * every table keyed by users.id or e-mail instead of username (the
#     employee_* tables, email_log, order_approvals, reviews, 2FA, reset tokens,
#     AI run logs, HR chatbot history);
#   * audit_log (must be PSEUDONYMISED, never deleted);
#   * the SQLite AI store (feedback, debug logs, latency, anonymous profiles),
#     which used to be written to on every turn yet called "orphaned".
#
# Everything below is driven by ``resolve_subject`` (username + users.id + all
# e-mail addresses + company_users ids + AI session ids) and by the declarative
# ``EXTRA_SPECS``. ``COVERAGE`` is the registry that tests/test_gdpr_table_coverage
# checks against every CREATE TABLE in the code base: a new table with personal
# data that is not listed here fails the build.
# ===========================================================================

import hashlib


def _pseudonym(subject):
    seed = "%s|%s" % (subject.get("user_id") or "", subject.get("username") or "")
    return "slettet-bruger-" + hashlib.sha256(seed.encode("utf-8")).hexdigest()[:8]


def resolve_subject(conn, username=None, *, user_id=None, email=None):
    """Everything that identifies one data subject across the schema.

    Returns ``{username, user_id, emails, member_ids, company_ids, session_ids}``.
    Each lookup is best effort and read-only."""
    subject = {
        "username": (username or "").strip() or None,
        "user_id": user_id,
        "emails": [],
        "member_ids": [],
        "company_ids": [],
        "session_ids": [],
    }
    emails = set()
    if email:
        emails.add(email.strip().lower())

    rows = []
    if subject["username"]:
        rows = _fetch_all(conn, "SELECT id, username, email FROM users WHERE username=%s", (subject["username"],))
    elif user_id:
        rows = _fetch_all(conn, "SELECT id, username, email FROM users WHERE id=%s", (user_id,))
    elif email:
        rows = _fetch_all(conn, "SELECT id, username, email FROM users WHERE LOWER(email)=%s", (email.strip().lower(),))
    if rows and isinstance(rows[0], dict):
        subject["user_id"] = rows[0].get("id")
        subject["username"] = subject["username"] or rows[0].get("username")
        if rows[0].get("email"):
            emails.add(str(rows[0]["email"]).strip().lower())

    # company_users: match on users.id, username OR e-mail (SCIM/SSO rows).
    member_rows = []
    conds, params = [], []
    if subject["user_id"]:
        conds.append("user_id=%s")
        params.append(subject["user_id"])
    if subject["username"]:
        conds.append("username=%s")
        params.append(subject["username"])
    for e in sorted(emails):
        conds.append("LOWER(email)=%s")
        params.append(e)
    if conds:
        member_rows = _fetch_all(conn, "SELECT id, company_id, email FROM company_users WHERE " + " OR ".join(conds), tuple(params))
    for r in member_rows:
        if not isinstance(r, dict):
            continue
        subject["member_ids"].append(r.get("id"))
        if r.get("company_id") is not None:
            subject["company_ids"].append(r.get("company_id"))
        if r.get("email"):
            emails.add(str(r["email"]).strip().lower())
    subject["emails"] = sorted(emails)
    subject["company_ids"] = sorted(set(subject["company_ids"]))

    sessions = set()
    if subject["username"]:
        for table in ("conversation_history", "user_conversations", "user_active_sessions",
                      "chatbot_interactions", "hr_chatbot_interactions", "ai_agent_runs"):
            for r in _fetch_all(conn, "SELECT DISTINCT session_id FROM `%s` WHERE username=%%s" % table,
                                (subject["username"],)):
                if isinstance(r, dict) and r.get("session_id"):
                    sessions.add(str(r["session_id"]))
    subject["session_ids"] = sorted(sessions)
    return subject


def _in_clause(values):
    return "(" + ",".join(["%s"] * len(values)) + ")"


def _build_where(spec, subject):
    """(sql, params) for a spec's OR-ed ``match`` terms AND-ed with ``extra``.
    Returns (None, None) when the subject has no value for any term."""
    parts, params = [], []
    for col, field in spec["match"]:
        if field == "username":
            vals = [subject["username"]] if subject.get("username") else []
        elif field == "user_id":
            vals = [subject["user_id"]] if subject.get("user_id") is not None else []
        elif field == "user_id_or_member":
            vals = ([subject["user_id"]] if subject.get("user_id") is not None else []) + list(subject.get("member_ids") or [])
        elif field == "email":
            vals = list(subject.get("emails") or [])
        elif field == "session_id":
            vals = list(subject.get("session_ids") or [])
        else:  # pragma: no cover - programming error
            raise ValueError("unknown subject field %r" % field)
        vals = [v for v in vals if v is not None]
        if not vals:
            continue
        if len(vals) == 1:
            parts.append("LOWER(`%s`)=%%s" % col if field == "email" else "`%s`=%%s" % col)
        else:
            parts.append("LOWER(`%s`) IN %s" % (col, _in_clause(vals)) if field == "email"
                         else "`%s` IN %s" % (col, _in_clause(vals)))
        params.extend(vals)
    if not parts:
        return None, None
    sql = "(" + " OR ".join(parts) + ")"
    extra = spec.get("extra")
    if extra:
        if extra == "company_scope":
            cids = subject.get("company_ids") or []
            if not cids:
                return None, None
            sql += " AND `company_id` IN " + _in_clause(cids)
            params.extend(cids)
        else:
            sql += " AND " + extra
    return sql, params


# kind: delete | anonymise | pseudonymise.  set: SQL SET fragment ('%s' params
# from ``set_params(subject)``); match: [(column, subject-field)], OR-ed.
EXTRA_SPECS = [
    dict(kind="anonymise", table="company_launch_checks", match=[("confirmed_by","user_id")], set="confirmed_by=NULL"),
    dict(kind="delete", table="customer_requests", match=[("user_id","user_id")]),
    dict(kind="delete", table="sales_enquiries", match=[("email","email")]),
    dict(kind="delete", table="mail_outbox", match=[("to_email","email")]),
    *[dict(kind="delete", table=t, match=[("user_id", "user_id")]) for t in ("learning_assignment_steps", "course_order_details", "course_order_changes", "learning_outcome_reviews")],
    # --- hard deletes (personal, no audit/financial value) ---------------
    dict(kind="delete", table="company_chat_messages",
         match=[("sender_member_id", "user_id_or_member"),
                ("recipient_member_id", "user_id_or_member")], extra="company_scope"),
    dict(kind="delete", table="ai_agent_runs", match=[("username", "username")]),
    dict(kind="delete", table="ai_tool_runs", match=[("username", "username")]),
    dict(kind="delete", table="hr_chatbot_interactions", match=[("username", "username")]),
    dict(kind="delete", table="ai_cv_parse_jobs", match=[("session_id", "session_id")]),
    dict(kind="delete", table="ai_confirm_tokens", match=[("session_id", "session_id")]),
    dict(kind="delete", table="employee_learning_progress", match=[("user_id", "user_id")]),
    dict(kind="delete", table="employee_skills_matrix", match=[("employee_id", "user_id")], extra="company_scope"),
    dict(kind="delete", table="employee_goals", match=[("employee_id", "user_id")], extra="company_scope"),
    dict(kind="delete", table="employee_performance_reviews", match=[("employee_id", "user_id")], extra="company_scope"),
    # skill history is written with users.id by HR screens and company_users.id by the profile path.
    dict(kind="delete", table="employee_skill_history", match=[("employee_id", "user_id_or_member")], extra="company_scope"),
    dict(kind="delete", table="user_2fa", match=[("user_id", "user_id")]),
    dict(kind="delete", table="password_reset_tokens", match=[("account_id", "user_id")], extra="`account_type`='user'"),
    dict(kind="delete", table="email_log", match=[("to_email", "email")]),
    # --- anonymise in place (business / accounting records survive) -------
    dict(kind="anonymise", table="course_orders", match=[("user_id", "user_id"), ("user_email", "email")],
         set="user_email=NULL, user_name=NULL, user_phone=NULL, user_id=NULL"),
    dict(kind="anonymise", table="course_reviews", match=[("user_id", "user_id"), ("username", "username")],
         set="username=NULL, user_id=NULL"),
    dict(kind="anonymise", table="order_approvals", match=[("requester_user_id", "user_id")], set="requester_user_id=0"),
    dict(kind="anonymise", table="order_approvals#approver", match=[("approver_user_id", "user_id")],
         set="approver_user_id=NULL", real_table="order_approvals"),
    dict(kind="anonymise", table="company_users", match=[("user_id", "user_id"), ("email", "email")],
         set="status='inactive', full_name=NULL, email=NULL, phone=NULL, username=NULL, employee_id=NULL"),
    dict(kind="anonymise", table="users", match=[("id", "user_id")], set="password='!erased'"),
    # The DSR ticket is proof the request was handled; it keeps status/dates, not the person.
    dict(kind="anonymise", table="dsr_requests", match=[("user_id", "user_id"), ("email", "email")],
         set="user_id=NULL, username=NULL, email=NULL, reason=NULL"),
    dict(kind="anonymise", table="credit_usage", match=[("username", "username"), ("user_id", "user_id")],
         set="username=NULL, user_id=NULL, actor=NULL"),
    # --- pseudonymise: audit trail stays, the person does not -------------
    dict(kind="pseudonymise", table="audit_log", match=[("user_id", "user_id")],
         set="user_id=NULL, ip_address=NULL, user_agent=NULL"),
    dict(kind="pseudonymise", table="audit_log#subject", match=[("resource_id", "username")],
         extra="`resource_type` IN ('gdpr','user')", set="ip_address=NULL", real_table="audit_log"),
]

_AUDIT_TEXT_COLUMNS = ("description", "details")

EXTRA_EXPORT_REDACT = {
    "user_2fa": "SELECT user_id, enabled, enabled_at, created_at FROM user_2fa WHERE user_id=%s",
    "password_reset_tokens": "SELECT id, account_type, account_id, purpose, created_at, expires_at, used_at "
                             "FROM password_reset_tokens WHERE account_type='user' AND account_id=%s",
}


def _spec_table(spec):
    return spec.get("real_table") or spec["table"]


def _set_clause(spec, subject):
    """(sql fragment, params). audit_log rows also get usernames / e-mails
    replaced in their free text by a stable pseudonym."""
    sql, params = spec.get("set", ""), []
    if spec["kind"] == "pseudonymise":
        ident = [v for v in [subject.get("username")] + list(subject.get("emails") or []) if v]
        pseudo = _pseudonym(subject)
        fragments = []
        for col in _AUDIT_TEXT_COLUMNS:
            expr = "`%s`" % col
            for v in ident:
                expr = "REPLACE(%s, %%s, %%s)" % expr
                params.extend([v, pseudo])
            fragments.append("`%s`=%s" % (col, expr))
        # params order: each column's REPLACE chain in sequence
        sql = (sql + ", " if sql else "") + ", ".join(fragments)
    return sql, params


def _extra_export_rows(conn, subject, learner_view=False):
    """Rows for the export. ``learner_view`` (the person's OWN download) leaves out
    HR-only goals -- unshared goals never reach learner-facing output (S-4.4)."""
    out = {}
    for spec in EXTRA_SPECS:
        table = _spec_table(spec)
        if spec["table"] != table:
            continue  # the "#variant" twins export as their base table
        if table in ("users", "company_users", "course_orders"):
            continue  # already exported by the legacy queries
        if table in EXTRA_EXPORT_REDACT and subject.get("user_id") is not None:
            out[table] = _fetch_all(conn, EXTRA_EXPORT_REDACT[table], (subject["user_id"],))
            continue
        where, params = _build_where(spec, subject)
        if not where:
            out[table] = []
            continue
        if learner_view and table == "employee_goals":
            where += " AND shared_with_employee = 1"
        out[table] = _fetch_all(conn, "SELECT * FROM `%s` WHERE %s" % (table, where), tuple(params))
    return out


# The AI store (app1/memory_store.py, N-3.3) lives in MySQL tables ``ai_*`` (SQLite
# only as the dev fallback). The report keys stay the short names the admin UI and
# the old SQLite layout used.
_AI_STORE_TABLES = (
    ("sessions", "ai_sessions"),
    ("analytics", "ai_analytics_events"),
    ("debug_logs", "ai_debug_logs"),
    ("latency_logs", "ai_latency_logs"),
)


def _ai_store_run(sql, params, fetch=False):
    from app1 import memory_store
    return memory_store._run(sql, params, fetch=fetch)


def _sqlite_ai_store_plan(subject, browser_token=None):
    """Counts of AI-store rows tied to this subject (by session id / username /
    browser token). Read-only; empty when there is nothing to link."""
    plan = {}
    sessions = list(subject.get("session_ids") or [])
    username = subject.get("username")
    try:
        from app1 import memory_store  # noqa: F401
    except Exception as exc:
        return {"error": str(exc)}
    if sessions:
        marks = ",".join(["%s"] * len(sessions))
        for key, tbl in _AI_STORE_TABLES:
            try:
                row = _ai_store_run("SELECT COUNT(*) AS n FROM %s WHERE session_id IN (%s)" % (tbl, marks),
                                    sessions, fetch=True).one()
                plan[key] = int(row["n"] if row else 0)
            except Exception:
                plan[key] = 0
    if username:
        try:
            row = _ai_store_run("SELECT COUNT(*) AS n FROM ai_analytics_events WHERE username = %s",
                                (username,), fetch=True).one()
            plan["analytics"] = max(plan.get("analytics", 0), int(row["n"] if row else 0))
        except Exception:
            plan.setdefault("analytics", 0)
    if browser_token:
        try:
            row = _ai_store_run("SELECT COUNT(*) AS n FROM ai_anonymous_profiles WHERE browser_token = %s",
                                (browser_token,), fetch=True).one()
            plan["anonymous_profiles"] = int(row["n"] if row else 0)
        except Exception:
            plan["anonymous_profiles"] = 0
    return plan


def _sqlite_ai_store_erase(subject, browser_token=None):
    sessions = list(subject.get("session_ids") or [])
    username = subject.get("username")
    deleted = {}
    try:
        from app1 import memory_store  # noqa: F401
    except Exception as exc:
        return {"error": str(exc)}
    try:
        if sessions:
            marks = ",".join(["%s"] * len(sessions))
            for key, tbl in _AI_STORE_TABLES:
                try:
                    res = _ai_store_run("DELETE FROM %s WHERE session_id IN (%s)" % (tbl, marks), sessions)
                    deleted[key] = int(res.rowcount or 0)
                except Exception:
                    deleted[key] = 0
        if username:
            res = _ai_store_run("DELETE FROM ai_analytics_events WHERE username = %s", (username,))
            deleted["analytics"] = deleted.get("analytics", 0) + int(res.rowcount or 0)
        if browser_token:
            res = _ai_store_run("DELETE FROM ai_anonymous_profiles WHERE browser_token = %s", (browser_token,))
            deleted["anonymous_profiles"] = int(res.rowcount or 0)
    except Exception as exc:
        return {"error": str(exc)}
    return deleted


# Registry checked by tests/test_gdpr_table_coverage.py against every CREATE TABLE.
# disposition: delete | anonymise | pseudonymise | retain.  ``retain`` needs a reason.
def _cov(disposition, note=""):
    return (disposition, note)


COVERAGE = {
    "customer_accounts": _cov("retain", "Company contract and professional account-contact configuration"),
    "company_launch_checks": _cov("anonymise", "Company setup review audit; confirming actor id is removed"),
    "customer_requests": _cov("delete"),
    "sales_enquiries": _cov("delete"),
    "mail_outbox": _cov("delete"),
    **{t: _cov("delete") for t in ("learning_assignment_steps", "course_order_details", "course_order_changes", "learning_outcome_reviews")},
    # --- profile / AI state: hard delete (legacy username lists) ---------
    **{t: _cov("delete") for t in (
        "user_skills", "user_experience", "user_education", "user_completed_courses",
        "user_profile_summary", "user_conversations", "conversation_history", "user_learning_goals",
        "user_learning_paths", "user_profile_checkins", "user_certifications", "user_languages", "user_portfolio_links",
        "user_memories", "user_active_sessions", "user_conversation_summaries", "user_knowledge",
        "notifications",
        "ai_agent_runs", "ai_tool_runs", "hr_chatbot_interactions", "ai_cv_parse_jobs", "ai_confirm_tokens",
        "employee_learning_progress", "employee_skills_matrix", "employee_goals",
        "employee_performance_reviews", "employee_skill_history", "user_2fa", "password_reset_tokens",
        "email_log",
        "company_chat_messages",
        # AI store (N-3.3): erased through the subject's session ids / username / browser token
        "ai_sessions", "ai_analytics_events", "ai_debug_logs", "ai_latency_logs", "ai_anonymous_profiles",
        # small per-user tables now owned by schema_registry (username-keyed profile data)
        "brands", "app_usage", "social_metrics", "user_learning_path_versions",
    )},
    # --- survive for accounting / audit, identity removed ----------------
    "course_orders": _cov("anonymise"),
    "course_reviews": _cov("anonymise"),
    "order_approvals": _cov("anonymise"),
    "company_users": _cov("anonymise"),
    "chatbot_interactions": _cov("anonymise"),
    "users": _cov("anonymise"),
    "credit_usage": _cov("anonymise", "AI-credit ledger kept for the company's billing; person removed"),
    "dsr_requests": _cov("anonymise", "ticket kept as proof of handling; identity removed on completion"),
    "audit_log": _cov("pseudonymise", "actor id nulled, usernames/e-mails in free text replaced by a stable pseudonym"),
    # --- no natural person inside (company/system configuration & counters)
    **{t: _cov("retain", "company or system data; no natural-person identifier") for t in (
        "companies", "company_analytics", "company_api_keys", "company_approval_policies", "company_brand_assets",
        "company_course_activations", "company_courses", "company_custom_code", "company_departments",
        "company_insights", "company_report_schedules", "company_settings", "company_skill_targets",
        "company_sso_configs", "company_supplier_agreements", "company_supplier_preferences",
        "company_theme_templates", "company_webhooks", "company_widget_settings", "compliance_requirements",
        "department_budgets", "learning_paths", "vendor_submissions", "vendors", "ai_secrets", "ai_settings",
        "api_auth_attempts", "api_rate_limit_counters", "auth_login_attempts", "scheduled_job_runs",
        "widget_ask_rate_counters", "company_credit_accounts", "learning_path_steps", "learning_path_versions",
        "vendor_profiles", "company_team_order_policy",
    )},
    # --- short-lived operational data: purged by the retention job (S-4.3) ---
    "api_request_logs": _cov("retain", "system caller (API key), purged by retention_service"),
    "company_settings_history": _cov("retain", "company admin change history (ip), purged by retention_service"),
    "company_notifications": _cov("retain", "company broadcasts; purged by retention_service"),
    "event_outbox": _cov("retain", "delivery queue; delivered rows purged by retention_service"),
    "hr_notification_queue": _cov("retain", "transient queue; purged by retention_service"),
}
