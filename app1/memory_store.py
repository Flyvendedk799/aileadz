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
    """)
    conn.commit()


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


def update_session_field(session_id, field, value):
    """Update a single field on an existing session."""
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


# Initialize on import
init_db()
