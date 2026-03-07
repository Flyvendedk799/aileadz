"""
Phase 4: Persistent memory store using SQLite.
Stores user profiles, conversation summaries, shown products, and analytics.
Zero external dependencies — uses Python's built-in sqlite3.
"""
import sqlite3
import json
import os
import time
import threading

DB_PATH = os.path.join(os.path.dirname(__file__), "ai_memory.db")

# Thread-local connections for safe concurrent access
_local = threading.local()


def _get_conn():
    """Get a thread-local SQLite connection."""
    if not hasattr(_local, "conn") or _local.conn is None:
        _local.conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        _local.conn.row_factory = sqlite3.Row
        _local.conn.execute("PRAGMA journal_mode=WAL")
        _local.conn.execute("PRAGMA synchronous=NORMAL")
    return _local.conn


def init_db():
    """Create tables if they don't exist."""
    conn = _get_conn()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS sessions (
            session_id TEXT PRIMARY KEY,
            user_profile TEXT DEFAULT '',
            conversation_summary TEXT DEFAULT '',
            shown_products TEXT DEFAULT '[]',
            last_active REAL DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS analytics (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL,
            timestamp REAL NOT NULL,
            event_type TEXT NOT NULL,
            query_text TEXT DEFAULT '',
            tool_used TEXT DEFAULT '',
            results_count INTEGER DEFAULT 0,
            feedback_rating INTEGER DEFAULT 0,
            message_index INTEGER DEFAULT 0,
            extra TEXT DEFAULT '{}'
        );

        CREATE INDEX IF NOT EXISTS idx_analytics_session ON analytics(session_id);
        CREATE INDEX IF NOT EXISTS idx_analytics_event ON analytics(event_type);
        CREATE INDEX IF NOT EXISTS idx_analytics_rating ON analytics(feedback_rating);

        CREATE TABLE IF NOT EXISTS debug_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL,
            timestamp REAL NOT NULL,
            step TEXT NOT NULL,
            data TEXT DEFAULT '{}'
        );

        CREATE INDEX IF NOT EXISTS idx_debug_session ON debug_logs(session_id);
        CREATE INDEX IF NOT EXISTS idx_debug_timestamp ON debug_logs(timestamp);

        CREATE TABLE IF NOT EXISTS anonymous_profiles (
            browser_token TEXT PRIMARY KEY,
            interests TEXT DEFAULT '[]',
            budget_range TEXT DEFAULT '',
            preferred_location TEXT DEFAULT '',
            preferred_format TEXT DEFAULT '',
            last_viewed TEXT DEFAULT '[]',
            last_searches TEXT DEFAULT '[]',
            conversation_summary TEXT DEFAULT '',
            created_at REAL DEFAULT 0,
            last_active REAL DEFAULT 0
        );

        CREATE INDEX IF NOT EXISTS idx_anon_last_active ON anonymous_profiles(last_active);

        CREATE TABLE IF NOT EXISTS latency_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL,
            timestamp REAL NOT NULL,
            operation TEXT NOT NULL,
            latency_ms REAL NOT NULL,
            prompt_version TEXT DEFAULT '',
            extra TEXT DEFAULT '{}'
        );

        CREATE INDEX IF NOT EXISTS idx_latency_session ON latency_logs(session_id);
        CREATE INDEX IF NOT EXISTS idx_latency_op ON latency_logs(operation);
    """)
    conn.commit()

    # Schema migration: add conversation_summary to anonymous_profiles if missing
    try:
        cursor = conn.execute("PRAGMA table_info(anonymous_profiles)")
        columns = {row["name"] for row in cursor.fetchall()}
        if "conversation_summary" not in columns:
            conn.execute("ALTER TABLE anonymous_profiles ADD COLUMN conversation_summary TEXT DEFAULT ''")
            conn.commit()
    except Exception:
        pass


# ── Session CRUD ──

def load_session(session_id):
    """Load a session from the database. Returns dict or None."""
    conn = _get_conn()
    row = conn.execute("SELECT * FROM sessions WHERE session_id = ?", (session_id,)).fetchone()
    if not row:
        return None
    return {
        "session_id": row["session_id"],
        "user_profile": row["user_profile"],
        "conversation_summary": row["conversation_summary"],
        "shown_products": json.loads(row["shown_products"]) if row["shown_products"] else [],
        "last_active": row["last_active"],
    }


def save_session(session_id, user_profile="", conversation_summary="", shown_products=None):
    """Upsert a session into the database."""
    conn = _get_conn()
    shown_json = json.dumps(shown_products or [])
    conn.execute("""
        INSERT INTO sessions (session_id, user_profile, conversation_summary, shown_products, last_active)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(session_id) DO UPDATE SET
            user_profile = excluded.user_profile,
            conversation_summary = excluded.conversation_summary,
            shown_products = excluded.shown_products,
            last_active = excluded.last_active
    """, (session_id, user_profile, conversation_summary, shown_json, time.time()))
    conn.commit()


_ALLOWED_SESSION_FIELDS = {"user_profile", "conversation_summary", "shown_products"}


def update_session_field(session_id, field, value):
    """Update a single field on an existing session."""
    if field not in _ALLOWED_SESSION_FIELDS:
        raise ValueError(f"Field '{field}' is not allowed. Must be one of: {_ALLOWED_SESSION_FIELDS}")
    conn = _get_conn()
    if field == "shown_products":
        value = json.dumps(value)
    conn.execute(f"UPDATE sessions SET {field} = ?, last_active = ? WHERE session_id = ?",
                 (value, time.time(), session_id))
    conn.commit()


def cleanup_old_sessions(ttl=3600):
    """Remove sessions older than TTL seconds."""
    conn = _get_conn()
    cutoff = time.time() - ttl
    conn.execute("DELETE FROM sessions WHERE last_active < ?", (cutoff,))
    conn.commit()


# ── Analytics ──

def log_event(session_id, event_type, query_text="", tool_used="", results_count=0,
              feedback_rating=0, message_index=0, extra=None):
    """Log an analytics event."""
    conn = _get_conn()
    conn.execute("""
        INSERT INTO analytics (session_id, timestamp, event_type, query_text, tool_used,
                              results_count, feedback_rating, message_index, extra)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (session_id, time.time(), event_type, query_text, tool_used,
          results_count, feedback_rating, message_index, json.dumps(extra or {})))
    conn.commit()


def get_top_rated_interactions(limit=5, min_rating=1):
    """Get the highest-rated interactions for few-shot examples."""
    conn = _get_conn()
    rows = conn.execute("""
        SELECT query_text, extra FROM analytics
        WHERE event_type = 'feedback' AND feedback_rating >= ?
        ORDER BY feedback_rating DESC, timestamp DESC
        LIMIT ?
    """, (min_rating, limit)).fetchall()
    return [{"query": r["query_text"], "extra": json.loads(r["extra"])} for r in rows]


def get_search_analytics(limit=20):
    """Get recent search analytics for debugging/optimization."""
    conn = _get_conn()
    rows = conn.execute("""
        SELECT query_text, tool_used, results_count, timestamp FROM analytics
        WHERE event_type = 'tool_call'
        ORDER BY timestamp DESC LIMIT ?
    """, (limit,)).fetchall()
    return [dict(r) for r in rows]


# ── Debug Logging ──

def log_debug(session_id, step, data=None):
    """Log a debug entry for the admin log page."""
    conn = _get_conn()
    conn.execute(
        "INSERT INTO debug_logs (session_id, timestamp, step, data) VALUES (?, ?, ?, ?)",
        (session_id, time.time(), step, json.dumps(data or {}, ensure_ascii=False))
    )
    conn.commit()


def get_debug_sessions(limit=50):
    """Get recent sessions that have debug logs, ordered by last activity."""
    conn = _get_conn()
    rows = conn.execute("""
        SELECT session_id, MIN(timestamp) as started, MAX(timestamp) as last_active, COUNT(*) as entry_count
        FROM debug_logs
        GROUP BY session_id
        ORDER BY last_active DESC
        LIMIT ?
    """, (limit,)).fetchall()
    return [dict(r) for r in rows]


def get_debug_logs_for_session(session_id):
    """Get all debug log entries for a specific session, ordered chronologically."""
    conn = _get_conn()
    rows = conn.execute(
        "SELECT step, timestamp, data FROM debug_logs WHERE session_id = ? ORDER BY timestamp ASC",
        (session_id,)
    ).fetchall()
    result = []
    for r in rows:
        entry = {"step": r["step"], "timestamp": r["timestamp"]}
        try:
            entry["data"] = json.loads(r["data"])
        except (json.JSONDecodeError, TypeError):
            entry["data"] = r["data"]
        result.append(entry)
    return result


def clear_debug_logs(before_timestamp=None):
    """Clear debug logs, optionally only those before a timestamp."""
    conn = _get_conn()
    if before_timestamp:
        conn.execute("DELETE FROM debug_logs WHERE timestamp < ?", (before_timestamp,))
    else:
        conn.execute("DELETE FROM debug_logs")
    conn.commit()


# ── 5.4: Latency & Observability ──

def log_latency(session_id, operation, latency_ms, prompt_version="", extra=None):
    """Log latency for a specific operation (API call, tool execution, etc.)."""
    conn = _get_conn()
    conn.execute(
        "INSERT INTO latency_logs (session_id, timestamp, operation, latency_ms, prompt_version, extra) VALUES (?, ?, ?, ?, ?, ?)",
        (session_id, time.time(), operation, latency_ms, prompt_version, json.dumps(extra or {}))
    )
    conn.commit()


def get_latency_stats(hours=24, operation=None):
    """Get latency statistics for the given time window."""
    conn = _get_conn()
    cutoff = time.time() - (hours * 3600)
    if operation:
        rows = conn.execute(
            "SELECT operation, AVG(latency_ms) as avg_ms, MIN(latency_ms) as min_ms, MAX(latency_ms) as max_ms, COUNT(*) as count "
            "FROM latency_logs WHERE timestamp > ? AND operation = ? GROUP BY operation",
            (cutoff, operation)
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT operation, AVG(latency_ms) as avg_ms, MIN(latency_ms) as min_ms, MAX(latency_ms) as max_ms, COUNT(*) as count "
            "FROM latency_logs WHERE timestamp > ? GROUP BY operation ORDER BY avg_ms DESC",
            (cutoff,)
        ).fetchall()
    return [dict(r) for r in rows]


def get_observability_dashboard(hours=24):
    """Get key metrics for the observability dashboard."""
    conn = _get_conn()
    cutoff = time.time() - (hours * 3600)

    # Response times
    latency = get_latency_stats(hours)

    # Search hit rate
    searches = conn.execute(
        "SELECT COUNT(*) as total, SUM(CASE WHEN results_count > 0 THEN 1 ELSE 0 END) as hits "
        "FROM analytics WHERE event_type = 'tool_call' AND timestamp > ?", (cutoff,)
    ).fetchone()
    total_searches = searches["total"] if searches else 0
    hit_rate = (searches["hits"] / total_searches * 100) if total_searches > 0 else 0

    # Feedback stats
    feedback = conn.execute(
        "SELECT COUNT(*) as total, SUM(CASE WHEN feedback_rating > 0 THEN 1 ELSE 0 END) as positive, "
        "SUM(CASE WHEN feedback_rating < 0 THEN 1 ELSE 0 END) as negative "
        "FROM analytics WHERE event_type = 'feedback' AND timestamp > ?", (cutoff,)
    ).fetchone()

    # Conversation depth
    depth = conn.execute(
        "SELECT session_id, COUNT(*) as msgs FROM analytics WHERE event_type = 'user_query' AND timestamp > ? "
        "GROUP BY session_id", (cutoff,)
    ).fetchall()
    avg_depth = sum(r["msgs"] for r in depth) / max(len(depth), 1) if depth else 0

    # A/B version breakdown
    versions = conn.execute(
        "SELECT prompt_version, COUNT(*) as count, AVG(latency_ms) as avg_latency "
        "FROM latency_logs WHERE timestamp > ? AND prompt_version != '' GROUP BY prompt_version",
        (cutoff,)
    ).fetchall()

    return {
        "latency_by_operation": latency,
        "search_hit_rate": round(hit_rate, 1),
        "total_searches": total_searches,
        "feedback_total": feedback["total"] if feedback else 0,
        "feedback_positive": feedback["positive"] if feedback else 0,
        "feedback_negative": feedback["negative"] if feedback else 0,
        "avg_conversation_depth": round(avg_depth, 1),
        "unique_sessions": len(depth),
        "ab_versions": [dict(r) for r in versions] if versions else [],
    }


# ── 6.3: Anonymous User Persistence ──

def save_anonymous_profile(browser_token, interests=None, budget_range="",
                           preferred_location="", preferred_format="",
                           last_viewed=None, last_searches=None, conversation_summary=""):
    """Upsert an anonymous user profile by browser token."""
    conn = _get_conn()
    now = time.time()
    conn.execute("""
        INSERT INTO anonymous_profiles (browser_token, interests, budget_range, preferred_location,
                                         preferred_format, last_viewed, last_searches, conversation_summary,
                                         created_at, last_active)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(browser_token) DO UPDATE SET
            interests = excluded.interests,
            budget_range = excluded.budget_range,
            preferred_location = excluded.preferred_location,
            preferred_format = excluded.preferred_format,
            last_viewed = excluded.last_viewed,
            last_searches = excluded.last_searches,
            conversation_summary = excluded.conversation_summary,
            last_active = excluded.last_active
    """, (browser_token, json.dumps(interests or []), budget_range,
          preferred_location, preferred_format,
          json.dumps(last_viewed or []), json.dumps(last_searches or []),
          conversation_summary, now, now))
    conn.commit()


def load_anonymous_profile(browser_token):
    """Load an anonymous user profile. Returns dict or None."""
    conn = _get_conn()
    row = conn.execute("SELECT * FROM anonymous_profiles WHERE browser_token = ?",
                       (browser_token,)).fetchone()
    if not row:
        return None
    return {
        "browser_token": row["browser_token"],
        "interests": json.loads(row["interests"]) if row["interests"] else [],
        "budget_range": row["budget_range"],
        "preferred_location": row["preferred_location"],
        "preferred_format": row["preferred_format"],
        "last_viewed": json.loads(row["last_viewed"]) if row["last_viewed"] else [],
        "last_searches": json.loads(row["last_searches"]) if row["last_searches"] else [],
        "conversation_summary": row["conversation_summary"] if "conversation_summary" in row.keys() else "",
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
        existing = set(profile["interests"])
        for interest in new_interests:
            existing.add(interest)
        profile["interests"] = list(existing)[-10:]  # Keep last 10

    if new_search:
        searches = profile["last_searches"]
        searches.append(new_search)
        profile["last_searches"] = searches[-10:]  # Keep last 10

    if new_viewed:
        viewed = profile["last_viewed"]
        # Add as {handle, title} dicts, dedup by handle
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


def cleanup_anonymous_profiles(ttl=604800):
    """Remove anonymous profiles older than TTL seconds (default 7 days)."""
    conn = _get_conn()
    cutoff = time.time() - ttl
    conn.execute("DELETE FROM anonymous_profiles WHERE last_active < ?", (cutoff,))
    conn.commit()


# Initialize on import
init_db()
