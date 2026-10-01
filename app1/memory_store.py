"""
One AI analytics / memory store (N-3.3).

Sessions, analytics events (incl. feedback), debug logs, latency logs and
anonymous profiles live in MySQL (tables ``ai_*``) so they survive redeploys and
are shared by every server. The old per-server SQLite ``ai_memory.db`` is only a
fallback for local development without a database (``AI_MEMORY_BACKEND=sqlite``
or no MySQL bound); ``import_legacy_sqlite`` copies its rows across once.

Every public function keeps its old signature. Telemetry must never break a chat
turn, so the public helpers swallow database errors.

Feedback scale (one scale everywhere): ``feedback_rating`` is +1 (thumbs up) or
-1 (thumbs down), 0 = none. ``message_index`` ties a rating to one answer.
"""
import sqlite3
import json
import os
import re
import time
import threading

DB_PATH = os.path.join(os.path.dirname(__file__), "ai_memory.db")

# Thread-local connections for the SQLite dev fallback
_local = threading.local()

_bound_mysql = None          # set by bind_mysql(app.mysql) so threads outside a request can write
_ready_ids = set()           # id(mysql) whose tables were verified

AI_TABLES = ("ai_sessions", "ai_analytics_events", "ai_debug_logs",
             "ai_anonymous_profiles", "ai_latency_logs")


def bind_mysql(mysql):
    """Remember the app's MySQL handle for code running outside a request."""
    global _bound_mysql
    _bound_mysql = mysql


def _mysql():
    try:
        from flask import current_app
        m = getattr(current_app, "mysql", None)
        if m is not None:
            return m
    except Exception:
        pass
    return _bound_mysql


def _backend():
    """'mysql' or 'sqlite'. AI_MEMORY_BACKEND=sqlite forces the dev fallback;
    otherwise MySQL whenever one is reachable (request or bound app)."""
    if (os.environ.get("AI_MEMORY_BACKEND") or "auto").strip().lower() == "sqlite":
        return "sqlite"
    return "mysql" if _mysql() is not None else "sqlite"


_SQLITE_DDL = """
CREATE TABLE ai_sessions (
    session_id TEXT PRIMARY KEY, user_profile TEXT DEFAULT '', conversation_summary TEXT DEFAULT '',
    shown_products TEXT DEFAULT '[]', last_active REAL DEFAULT 0);
CREATE TABLE ai_analytics_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL, timestamp REAL NOT NULL,
    event_type TEXT NOT NULL, query_text TEXT DEFAULT '', tool_used TEXT DEFAULT '',
    results_count INTEGER DEFAULT 0, feedback_rating INTEGER DEFAULT 0, message_index INTEGER DEFAULT 0,
    company_id INTEGER, username TEXT, extra TEXT DEFAULT '{}', reviewed INTEGER DEFAULT 0);
CREATE TABLE ai_debug_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL, timestamp REAL NOT NULL,
    step TEXT NOT NULL, data TEXT DEFAULT '{}');
CREATE TABLE ai_anonymous_profiles (
    browser_token TEXT PRIMARY KEY, interests TEXT DEFAULT '[]', budget_range TEXT DEFAULT '',
    preferred_location TEXT DEFAULT '', preferred_format TEXT DEFAULT '', last_viewed TEXT DEFAULT '[]',
    last_searches TEXT DEFAULT '[]', conversation_summary TEXT DEFAULT '', created_at REAL DEFAULT 0,
    last_active REAL DEFAULT 0);
CREATE TABLE ai_latency_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL, timestamp REAL NOT NULL,
    operation TEXT NOT NULL, latency_ms REAL NOT NULL, prompt_version TEXT DEFAULT '', extra TEXT DEFAULT '{}');
CREATE INDEX idx_ai_analytics_session ON ai_analytics_events(session_id);
CREATE INDEX idx_ai_debug_session ON ai_debug_logs(session_id);
"""


def _get_conn():
    """Thread-local SQLite connection (dev fallback only)."""
    if not hasattr(_local, "conn") or _local.conn is None:
        _local.conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        _local.conn.row_factory = sqlite3.Row
        _local.conn.execute("PRAGMA journal_mode=WAL")
        _local.conn.execute("PRAGMA synchronous=NORMAL")
        _local.ddl = False
    return _local.conn


def _create_sqlite_tables(conn):
    for stmt in _SQLITE_DDL.split(";"):
        if stmt.strip():
            try:
                conn.execute(stmt)
            except sqlite3.OperationalError:
                pass                      # already exists
    conn.commit()
    _local.ddl = True


def init_db():
    """Create the SQLite fallback tables (MySQL tables come from schema_registry)."""
    _create_sqlite_tables(_get_conn())


def _ensure_mysql(mysql):
    if id(mysql) in _ready_ids:
        return
    _ready_ids.add(id(mysql))
    try:
        from schema_registry import REGISTRY_DDL
        cur = mysql.connection.cursor()
        for ddl in REGISTRY_DDL:
            if any(("EXISTS " + t + " ") in ddl or ("EXISTS " + t + "(") in ddl for t in AI_TABLES):
                try:
                    cur.execute(ddl)
                except Exception:
                    pass
        mysql.connection.commit()
        cur.close()
    except Exception:
        pass

    # S-1.7: admin-review flag for feedback rows (tables created before it existed).
    try:
        cur = mysql.connection.cursor()
        try:
            cur.execute("ALTER TABLE ai_analytics_events ADD COLUMN reviewed TINYINT NOT NULL DEFAULT 0")
            mysql.connection.commit()
        except Exception:
            try:
                mysql.connection.rollback()
            except Exception:
                pass                      # column already there
        finally:
            cur.close()
    except Exception:
        pass


class _Result:
    def __init__(self, rows, rowcount):
        self.rows = rows
        self.rowcount = rowcount

    def one(self):
        return self.rows[0] if self.rows else None


def _run(sql, params=(), fetch=False):
    """Run one statement on the active backend (``%s`` placeholders, dict rows).
    Raises on database errors; public helpers decide whether to swallow."""
    params = tuple(params or ())
    if _backend() == "mysql":
        mysql = _mysql()
        _ensure_mysql(mysql)
        conn = mysql.connection
        cur = conn.cursor()
        try:
            cur.execute(sql, params)
            rows = [dict(r) for r in (cur.fetchall() or [])] if fetch else []
            count = cur.rowcount
            conn.commit()
            return _Result(rows, count)
        except Exception:
            try:
                conn.rollback()
            except Exception:
                pass
            raise
        finally:
            try:
                cur.close()
            except Exception:
                pass
    conn = _get_conn()
    if not getattr(_local, "ddl", False):
        _create_sqlite_tables(conn)
    cur = conn.execute(re.sub(r"%s", "?", sql), params)
    rows = [dict(r) for r in cur.fetchall()] if fetch else []
    conn.commit()
    return _Result(rows, cur.rowcount)


def _upsert(table, key_col, key, values):
    """Portable upsert: UPDATE when the key exists, INSERT otherwise."""
    cols = list(values)
    sets = ", ".join(f"{c} = %s" for c in cols)
    exists = _run(f"SELECT 1 AS x FROM {table} WHERE {key_col} = %s", (key,), fetch=True).one()
    if exists:
        _run(f"UPDATE {table} SET {sets} WHERE {key_col} = %s", [values[c] for c in cols] + [key])
        return
    allc = [key_col] + cols
    try:
        _run(f"INSERT INTO {table} ({', '.join(allc)}) VALUES ({', '.join(['%s'] * len(allc))})",
             [key] + [values[c] for c in cols])
    except Exception:  # lost a race with a concurrent insert
        _run(f"UPDATE {table} SET {sets} WHERE {key_col} = %s", [values[c] for c in cols] + [key])


def _loads(raw, default):
    try:
        return json.loads(raw) if raw else default
    except (TypeError, ValueError):
        return default


def _quiet(fn):
    """Decorator: telemetry writes never raise into the chat turn."""
    def wrapper(*a, **k):
        try:
            return fn(*a, **k)
        except Exception as exc:
            print(f"[memory_store] {fn.__name__}: {exc}")
            return None
    wrapper.__name__ = fn.__name__
    wrapper.__doc__ = fn.__doc__
    return wrapper


# ── Session CRUD ──

def load_session(session_id):
    """Load a session from the database. Returns dict or None."""
    try:
        row = _run("SELECT * FROM ai_sessions WHERE session_id = %s", (session_id,), fetch=True).one()
    except Exception as exc:
        print(f"[memory_store] load_session: {exc}")
        return None
    if not row:
        return None
    return {
        "session_id": row["session_id"],
        "user_profile": row["user_profile"],
        "conversation_summary": row["conversation_summary"],
        "shown_products": _loads(row["shown_products"], []),
        "last_active": row["last_active"],
    }


@_quiet
def save_session(session_id, user_profile="", conversation_summary="", shown_products=None):
    """Upsert a session into the database."""
    _upsert("ai_sessions", "session_id", session_id, {
        "user_profile": user_profile, "conversation_summary": conversation_summary,
        "shown_products": json.dumps(shown_products or []), "last_active": time.time()})


_ALLOWED_SESSION_FIELDS = {"user_profile", "conversation_summary", "shown_products"}


def update_session_field(session_id, field, value):
    """Update a single field on an existing session."""
    if field not in _ALLOWED_SESSION_FIELDS:
        raise ValueError(f"Field '{field}' is not allowed. Must be one of: {_ALLOWED_SESSION_FIELDS}")
    if field == "shown_products":
        value = json.dumps(value)
    try:
        _run(f"UPDATE ai_sessions SET {field} = %s, last_active = %s WHERE session_id = %s",
             (value, time.time(), session_id))
    except Exception as exc:
        print(f"[memory_store] update_session_field: {exc}")


@_quiet
def cleanup_old_sessions(ttl=3600):
    """Remove sessions older than TTL seconds."""
    _run("DELETE FROM ai_sessions WHERE last_active < %s", (time.time() - ttl,))


# ── Automatic Periodic Cleanup ──
_last_cleanup_time = 0
_CLEANUP_INTERVAL = 3600  # Run cleanup at most once per hour


def _auto_cleanup():
    """Periodic retention: debug/latency 7 days, analytics 90 days (feedback is
    kept for quality review), sessions 24h, anonymous profiles 30 days.
    Skips if the last cleanup was < 1 hour ago."""
    global _last_cleanup_time
    now = time.time()
    if now - _last_cleanup_time < _CLEANUP_INTERVAL:
        return
    _last_cleanup_time = now
    try:
        _run("DELETE FROM ai_debug_logs WHERE timestamp < %s", (now - 7 * 86400,))
        _run("DELETE FROM ai_analytics_events WHERE timestamp < %s AND event_type <> 'feedback'",
             (now - 90 * 86400,))
        _run("DELETE FROM ai_sessions WHERE last_active < %s", (now - 86400,))
        _run("DELETE FROM ai_anonymous_profiles WHERE last_active < %s", (now - 30 * 86400,))
        _run("DELETE FROM ai_latency_logs WHERE timestamp < %s", (now - 7 * 86400,))
    except Exception as e:
        print(f"[DB Cleanup Error] {e}")


# ── Analytics ──

def _scope():
    """(company_id, username) of the current request, for tenant-scoped reuse."""
    try:
        from flask import session as _s, has_request_context
        if has_request_context():
            return _s.get("company_id"), _s.get("user")
    except Exception:
        pass
    return None, None


@_quiet
def log_event(session_id, event_type, query_text="", tool_used="", results_count=0,
              feedback_rating=0, message_index=0, extra=None, company_id=None):
    """Log an analytics event. ``company_id`` tags the tenant (S-1.7); when not
    given it is taken from the current request."""
    scope_company, username = _scope()
    if company_id is None:
        company_id = scope_company
    try:
        feedback_rating = int(feedback_rating or 0)
    except (TypeError, ValueError):
        feedback_rating = 0
    feedback_rating = max(-1, min(1, feedback_rating))      # one scale: -1 / 0 / +1
    try:
        message_index = int(message_index or 0)
    except (TypeError, ValueError):
        message_index = 0
    _run("""INSERT INTO ai_analytics_events (session_id, timestamp, event_type, query_text, tool_used,
                results_count, feedback_rating, message_index, company_id, username, extra)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
         (session_id, time.time(), event_type, query_text, tool_used, results_count,
          feedback_rating, message_index, company_id, username, json.dumps(extra or {}, ensure_ascii=False)))


def get_top_rated_interactions(limit=5, min_rating=1, company_id=None):
    """Highest-rated interactions for few-shot examples (S-1.7).

    Tenant-safe: only rows an admin has REVIEWED and that belong to exactly the
    caller's ``company_id`` (None matches only tenant-less rows) are returned, so
    one company's thumbs-up'd text can never reach another company's prompt.
    """
    sql = ("SELECT query_text, extra FROM ai_analytics_events "
           "WHERE event_type = 'feedback' AND feedback_rating >= %s AND reviewed = 1")
    params = [min_rating]
    if company_id is None:
        sql += " AND company_id IS NULL"
    else:
        sql += " AND company_id = %s"
        params.append(company_id)
    sql += " ORDER BY feedback_rating DESC, timestamp DESC LIMIT %s"
    params.append(limit)
    try:
        rows = _run(sql, params, fetch=True).rows
    except Exception:
        return []
    return [{"query": r["query_text"], "extra": _loads(r["extra"], {})} for r in rows]


def list_feedback_for_review(limit=50, only_pending=True):
    """Positive feedback rows awaiting (or past) admin review (S-1.7)."""
    sql = ("SELECT id, timestamp, company_id, query_text, extra, reviewed FROM ai_analytics_events "
           "WHERE event_type = 'feedback' AND feedback_rating > 0")
    if only_pending:
        sql += " AND reviewed = 0"
    sql += " ORDER BY timestamp DESC LIMIT %s"
    try:
        rows = _run(sql, (limit,), fetch=True).rows
    except Exception:
        return []
    out = []
    for r in rows:
        extra = _loads(r["extra"], {})
        out.append({"id": r["id"], "timestamp": r["timestamp"], "company_id": r["company_id"],
                    "query": r["query_text"], "response": extra.get("assistant_response", ""),
                    "reviewed": bool(r["reviewed"])})
    return out


def set_feedback_reviewed(feedback_id, approved=True):
    """Admin gate: mark a feedback row reusable (or not) as a prompt example."""
    try:
        res = _run("UPDATE ai_analytics_events SET reviewed = %s WHERE id = %s AND event_type = 'feedback'",
                   (1 if approved else 0, int(feedback_id)))
    except Exception:
        return False
    return (res.rowcount or 0) > 0


# ── Debug Logging ──

@_quiet
def log_debug(session_id, step, data=None):
    """Log a debug entry for the admin log page."""
    _auto_cleanup()  # Periodic cleanup (skips if < 1 hour since last)
    _run("INSERT INTO ai_debug_logs (session_id, timestamp, step, data) VALUES (%s, %s, %s, %s)",
         (session_id, time.time(), step, json.dumps(data or {}, ensure_ascii=False, default=str)))


def get_debug_sessions(limit=50):
    """Get recent sessions that have debug logs, ordered by last activity."""
    try:
        return _run("""SELECT session_id, MIN(timestamp) AS started, MAX(timestamp) AS last_active,
                              COUNT(*) AS entry_count
                       FROM ai_debug_logs GROUP BY session_id
                       ORDER BY last_active DESC LIMIT %s""", (limit,), fetch=True).rows
    except Exception:
        return []


def get_debug_logs_for_session(session_id):
    """Get all debug log entries for a specific session, ordered chronologically."""
    try:
        rows = _run("SELECT step, timestamp, data FROM ai_debug_logs WHERE session_id = %s "
                    "ORDER BY timestamp ASC, id ASC", (session_id,), fetch=True).rows
    except Exception:
        return []
    return [{"step": r["step"], "timestamp": r["timestamp"], "data": _loads(r["data"], r["data"])}
            for r in rows]


def clear_debug_logs(before_timestamp=None):
    """Clear debug logs, optionally only those before a timestamp."""
    try:
        if before_timestamp:
            _run("DELETE FROM ai_debug_logs WHERE timestamp < %s", (before_timestamp,))
        else:
            _run("DELETE FROM ai_debug_logs")
    except Exception as exc:
        print(f"[memory_store] clear_debug_logs: {exc}")


# ── 5.4: Latency & Observability ──

@_quiet
def log_latency(session_id, operation, latency_ms, prompt_version="", extra=None):
    """Log latency for a specific operation (API call, tool execution, etc.)."""
    _run("INSERT INTO ai_latency_logs (session_id, timestamp, operation, latency_ms, prompt_version, extra) "
         "VALUES (%s, %s, %s, %s, %s, %s)",
         (session_id, time.time(), operation, latency_ms, prompt_version, json.dumps(extra or {})))


def get_latency_stats(hours=24, operation=None):
    """Get latency statistics for the given time window."""
    cutoff = time.time() - (hours * 3600)
    cols = ("SELECT operation, AVG(latency_ms) AS avg_ms, MIN(latency_ms) AS min_ms, "
            "MAX(latency_ms) AS max_ms, COUNT(*) AS count FROM ai_latency_logs WHERE timestamp > %s")
    try:
        if operation:
            return _run(cols + " AND operation = %s GROUP BY operation", (cutoff, operation), fetch=True).rows
        return _run(cols + " GROUP BY operation ORDER BY avg_ms DESC", (cutoff,), fetch=True).rows
    except Exception:
        return []


def get_observability_dashboard(hours=24):
    """Get key metrics for the observability dashboard."""
    cutoff = time.time() - (hours * 3600)
    latency = get_latency_stats(hours)
    try:
        s = _run("SELECT COUNT(*) AS total, SUM(CASE WHEN results_count > 0 THEN 1 ELSE 0 END) AS hits "
                 "FROM ai_analytics_events WHERE event_type = 'tool_call' AND timestamp > %s",
                 (cutoff,), fetch=True).one() or {}
        f = _run("SELECT COUNT(*) AS total, SUM(CASE WHEN feedback_rating > 0 THEN 1 ELSE 0 END) AS positive, "
                 "SUM(CASE WHEN feedback_rating < 0 THEN 1 ELSE 0 END) AS negative "
                 "FROM ai_analytics_events WHERE event_type = 'feedback' AND timestamp > %s",
                 (cutoff,), fetch=True).one() or {}
        depth = _run("SELECT session_id, COUNT(*) AS msgs FROM ai_analytics_events "
                     "WHERE event_type = 'user_query' AND timestamp > %s GROUP BY session_id",
                     (cutoff,), fetch=True).rows
        versions = _run("SELECT prompt_version, COUNT(*) AS count, AVG(latency_ms) AS avg_latency "
                        "FROM ai_latency_logs WHERE timestamp > %s AND prompt_version <> '' "
                        "GROUP BY prompt_version", (cutoff,), fetch=True).rows
    except Exception:
        s, f, depth, versions = {}, {}, [], []
    total_searches = int(s.get("total") or 0)
    hits = int(s.get("hits") or 0)
    hit_rate = (hits / total_searches * 100) if total_searches else 0
    avg_depth = sum(int(r["msgs"]) for r in depth) / max(len(depth), 1) if depth else 0
    return {
        "latency_by_operation": latency,
        "search_hit_rate": round(hit_rate, 1),
        "total_searches": total_searches,
        "feedback_total": int(f.get("total") or 0),
        "feedback_positive": int(f.get("positive") or 0),
        "feedback_negative": int(f.get("negative") or 0),
        "avg_conversation_depth": round(avg_depth, 1),
        "unique_sessions": len(depth),
        "ab_versions": versions,
    }


# ── 6.3: Anonymous User Persistence ──

@_quiet
def save_anonymous_profile(browser_token, interests=None, budget_range="",
                           preferred_location="", preferred_format="",
                           last_viewed=None, last_searches=None, conversation_summary=""):
    """Upsert an anonymous user profile by browser token."""
    now = time.time()
    values = {
        "interests": json.dumps(interests or []), "budget_range": budget_range,
        "preferred_location": preferred_location, "preferred_format": preferred_format,
        "last_viewed": json.dumps(last_viewed or []), "last_searches": json.dumps(last_searches or []),
        "conversation_summary": conversation_summary, "last_active": now,
    }
    if not _run("SELECT 1 AS x FROM ai_anonymous_profiles WHERE browser_token = %s",
                (browser_token,), fetch=True).one():
        values["created_at"] = now
    _upsert("ai_anonymous_profiles", "browser_token", browser_token, values)


def load_anonymous_profile(browser_token):
    """Load an anonymous user profile. Returns dict or None."""
    try:
        row = _run("SELECT * FROM ai_anonymous_profiles WHERE browser_token = %s",
                   (browser_token,), fetch=True).one()
    except Exception:
        return None
    if not row:
        return None
    return {
        "browser_token": row["browser_token"],
        "interests": _loads(row["interests"], []),
        "budget_range": row["budget_range"],
        "preferred_location": row["preferred_location"],
        "preferred_format": row["preferred_format"],
        "last_viewed": _loads(row["last_viewed"], []),
        "last_searches": _loads(row["last_searches"], []),
        "conversation_summary": row.get("conversation_summary") or "",
        "created_at": row["created_at"],
        "last_active": row["last_active"],
    }


def update_anonymous_interests(browser_token, new_interests=None, new_search=None, new_viewed=None):
    """Incrementally update an anonymous profile with new activity."""
    profile = load_anonymous_profile(browser_token)
    if not profile:
        profile = {"interests": [], "last_viewed": [], "last_searches": [],
                   "budget_range": "", "preferred_location": "", "preferred_format": ""}

    if new_interests:
        existing = list(profile["interests"])
        for interest in new_interests:
            if interest not in existing:
                existing.append(interest)
        profile["interests"] = existing[-10:]  # Keep last 10

    if new_search:
        searches = profile["last_searches"]
        searches.append(new_search)
        profile["last_searches"] = searches[-10:]  # Keep last 10

    if new_viewed:
        viewed = profile["last_viewed"]
        existing_handles = {v.get("handle") for v in viewed}
        for item in new_viewed:
            if item.get("handle") not in existing_handles:
                viewed.append(item)
                existing_handles.add(item.get("handle"))
        profile["last_viewed"] = viewed[-5:]  # Keep last 5

    save_anonymous_profile(
        browser_token,
        interests=profile["interests"],
        budget_range=profile.get("budget_range", ""),
        preferred_location=profile.get("preferred_location", ""),
        preferred_format=profile.get("preferred_format", ""),
        last_viewed=profile["last_viewed"],
        last_searches=profile["last_searches"],
        conversation_summary=profile.get("conversation_summary", ""),
    )
    return profile


def update_anonymous_summary(browser_token, summary_text):
    """Store conversation summary for an anonymous user (persists across sessions)."""
    profile = load_anonymous_profile(browser_token)
    if not profile:
        profile = {"interests": [], "last_viewed": [], "last_searches": [],
                   "budget_range": "", "preferred_location": "", "preferred_format": ""}
    profile["conversation_summary"] = summary_text[:2000]  # Cap at 2000 chars
    save_anonymous_profile(
        browser_token,
        interests=profile["interests"],
        budget_range=profile.get("budget_range", ""),
        preferred_location=profile.get("preferred_location", ""),
        preferred_format=profile.get("preferred_format", ""),
        last_viewed=profile.get("last_viewed", []),
        last_searches=profile.get("last_searches", []),
        conversation_summary=profile["conversation_summary"],
    )


@_quiet
def cleanup_anonymous_profiles(ttl=604800):
    """Remove anonymous profiles older than TTL seconds (default 7 days)."""
    _run("DELETE FROM ai_anonymous_profiles WHERE last_active < %s", (time.time() - ttl,))


# ── GDPR ──

def erase_subject(browser_token=None, session_id=None, username=None):
    """Delete every AI-store row for one browser token / session id / username.
    Returns the number of rows removed. Scoped strictly to the supplied keys."""
    deleted = 0
    if username:
        deleted += _run("DELETE FROM ai_analytics_events WHERE username = %s", (username,)).rowcount or 0
    if browser_token:
        deleted += _run("DELETE FROM ai_anonymous_profiles WHERE browser_token = %s",
                        (browser_token,)).rowcount or 0
    if session_id:
        for tbl in ("ai_sessions", "ai_analytics_events", "ai_debug_logs", "ai_latency_logs"):
            deleted += _run(f"DELETE FROM {tbl} WHERE session_id = %s", (session_id,)).rowcount or 0
    return deleted


# ── One-time import of the old per-server SQLite file ──

def import_legacy_sqlite(path=None):
    """Copy anonymous profiles, analytics (incl. feedback) and latency rows from the
    legacy ``ai_memory.db`` into MySQL. Returns rows copied; 0 when there is no file
    or no MySQL. Safe to re-run (profiles upsert; events only copied once per file)."""
    path = path or DB_PATH
    if _backend() != "mysql" or not os.path.exists(path):
        return 0
    copied = 0
    src = sqlite3.connect(path)
    src.row_factory = sqlite3.Row
    try:
        for r in src.execute("SELECT * FROM anonymous_profiles").fetchall():
            d = dict(r)
            token = d.pop("browser_token")
            _upsert("ai_anonymous_profiles", "browser_token", token, d)
            copied += 1
        for r in src.execute("SELECT * FROM analytics ORDER BY id").fetchall():
            d = dict(r)
            rating = int(d.get("feedback_rating") or 0)
            _run("""INSERT INTO ai_analytics_events (session_id, timestamp, event_type, query_text, tool_used,
                        results_count, feedback_rating, message_index, company_id, extra)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                 (d["session_id"], d["timestamp"], d["event_type"], d.get("query_text") or "",
                  d.get("tool_used") or "", d.get("results_count") or 0, max(-1, min(1, rating)),
                  d.get("message_index") or 0, d.get("company_id"), d.get("extra") or "{}"))
            copied += 1
    except sqlite3.Error:
        pass
    finally:
        src.close()
    return copied


# Initialize the SQLite fallback on import (cheap; only used without MySQL).
if (os.environ.get("AI_MEMORY_BACKEND") or "").strip().lower() == "sqlite":
    init_db()
