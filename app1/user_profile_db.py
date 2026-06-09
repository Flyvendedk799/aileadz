"""
User Profile Database Layer — connects app1 chatbot to the main MySQL user system.
Manages: skills, work experience, education, completed courses, and profile summary.
"""
import time
from flask import current_app
import MySQLdb.cursors
from db_compat import refresh_flask_mysql_connection


# ── Table Creation (run once) ──

# Extracted so the wrong-schema migration path below can re-create exactly this
# table by name. (It used to reference `_TABLES_SQL[-1]`, which silently pointed
# at whatever happened to be last in the list — a latent bug that grows every
# time a new table is appended.)
_USER_CONVERSATIONS_SQL = """CREATE TABLE IF NOT EXISTS user_conversations (
        username VARCHAR(255) PRIMARY KEY,
        session_id VARCHAR(100) NOT NULL,
        messages LONGTEXT,
        summary TEXT DEFAULT NULL,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
    )"""

_TABLES_SQL = [
    """CREATE TABLE IF NOT EXISTS user_skills (
        id INT AUTO_INCREMENT PRIMARY KEY,
        username VARCHAR(255) NOT NULL,
        skill_name VARCHAR(255) NOT NULL,
        skill_level ENUM('begynder','mellem','avanceret','ekspert') DEFAULT 'mellem',
        source VARCHAR(50) DEFAULT 'manual',
        added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        UNIQUE KEY unique_skill (username, skill_name),
        INDEX idx_username (username)
    )""",
    """CREATE TABLE IF NOT EXISTS user_experience (
        id INT AUTO_INCREMENT PRIMARY KEY,
        username VARCHAR(255) NOT NULL,
        title VARCHAR(255) NOT NULL,
        company VARCHAR(255) DEFAULT '',
        start_year INT DEFAULT NULL,
        end_year INT DEFAULT NULL,
        is_current TINYINT(1) DEFAULT 0,
        description TEXT DEFAULT NULL,
        added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        INDEX idx_username (username)
    )""",
    """CREATE TABLE IF NOT EXISTS user_education (
        id INT AUTO_INCREMENT PRIMARY KEY,
        username VARCHAR(255) NOT NULL,
        degree VARCHAR(255) NOT NULL,
        institution VARCHAR(255) DEFAULT '',
        year_completed INT DEFAULT NULL,
        description TEXT DEFAULT NULL,
        added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        INDEX idx_username (username)
    )""",
    """CREATE TABLE IF NOT EXISTS user_completed_courses (
        id INT AUTO_INCREMENT PRIMARY KEY,
        username VARCHAR(255) NOT NULL,
        course_title VARCHAR(255) NOT NULL,
        course_handle VARCHAR(255) DEFAULT NULL,
        vendor VARCHAR(255) DEFAULT '',
        completed_date VARCHAR(50) DEFAULT NULL,
        certificate_note TEXT DEFAULT NULL,
        added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        UNIQUE KEY unique_course (username, course_title),
        INDEX idx_username (username)
    )""",
    """CREATE TABLE IF NOT EXISTS user_profile_summary (
        username VARCHAR(255) PRIMARY KEY,
        headline VARCHAR(500) DEFAULT '',
        bio TEXT DEFAULT NULL,
        goals TEXT DEFAULT NULL,
        preferred_location VARCHAR(255) DEFAULT '',
        preferred_format VARCHAR(100) DEFAULT '',
        budget_range VARCHAR(100) DEFAULT '',
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
    )""",
    _USER_CONVERSATIONS_SQL,
    """CREATE TABLE IF NOT EXISTS conversation_history (
        id INT AUTO_INCREMENT PRIMARY KEY,
        username VARCHAR(255) NOT NULL,
        session_id VARCHAR(100) NOT NULL,
        title VARCHAR(255) DEFAULT 'Ny samtale',
        messages LONGTEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
        INDEX idx_user_updated (username, updated_at DESC)
    )""",
    """CREATE TABLE IF NOT EXISTS user_learning_goals (
        id INT AUTO_INCREMENT PRIMARY KEY,
        username VARCHAR(255) NOT NULL,
        title VARCHAR(255) NOT NULL,
        description TEXT DEFAULT NULL,
        target_date VARCHAR(50) DEFAULT NULL,
        status ENUM('aktiv','fuldfoert','paa_pause') DEFAULT 'aktiv',
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
        INDEX idx_username (username)
    )""",
    """CREATE TABLE IF NOT EXISTS user_certifications (
        id INT AUTO_INCREMENT PRIMARY KEY,
        username VARCHAR(255) NOT NULL,
        name VARCHAR(255) NOT NULL,
        issuer VARCHAR(255) DEFAULT '',
        issue_date VARCHAR(20) DEFAULT NULL,
        expiry_date VARCHAR(20) DEFAULT NULL,
        credential_id VARCHAR(255) DEFAULT NULL,
        credential_url VARCHAR(500) DEFAULT NULL,
        source VARCHAR(50) DEFAULT 'manual',
        expiry_reminded_for VARCHAR(20) DEFAULT NULL,
        added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        INDEX idx_username (username)
    )""",
    """CREATE TABLE IF NOT EXISTS user_languages (
        id INT AUTO_INCREMENT PRIMARY KEY,
        username VARCHAR(255) NOT NULL,
        language VARCHAR(100) NOT NULL,
        proficiency ENUM('begynder','mellem','flydende','modersmaal') DEFAULT 'mellem',
        source VARCHAR(50) DEFAULT 'manual',
        added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        UNIQUE KEY unique_language (username, language),
        INDEX idx_username (username)
    )""",
    """CREATE TABLE IF NOT EXISTS user_portfolio_links (
        id INT AUTO_INCREMENT PRIMARY KEY,
        username VARCHAR(255) NOT NULL,
        label VARCHAR(150) NOT NULL,
        url VARCHAR(500) NOT NULL,
        kind VARCHAR(40) DEFAULT 'link',
        source VARCHAR(50) DEFAULT 'manual',
        added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        INDEX idx_username (username)
    )""",
    # Atomic, free-form facts the AI learns about the user (preferences, life
    # context, personality, interests) — distinct from the structured profile
    # sections above. Powers the Mind-Map and the "what I know about you" context
    # injected into every chat turn. `used_count`/`last_used_at` track when a
    # memory actually informs a conversation so the UI can surface it in realtime.
    """CREATE TABLE IF NOT EXISTS user_memories (
        id INT AUTO_INCREMENT PRIMARY KEY,
        username VARCHAR(255) NOT NULL,
        category VARCHAR(60) NOT NULL DEFAULT 'andet',
        label VARCHAR(200) NOT NULL,
        detail TEXT DEFAULT NULL,
        source VARCHAR(40) DEFAULT 'ai',
        confidence FLOAT DEFAULT 0.8,
        used_count INT DEFAULT 0,
        last_used_at DATETIME DEFAULT NULL,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
        UNIQUE KEY unique_memory (username, label),
        INDEX idx_username (username),
        INDEX idx_cat (username, category)
    )""",
]


_tables_ensured = False


def ensure_tables():
    """Create tables if they don't exist. Cached after first success (Phase 2C)."""
    global _tables_ensured
    # Always verify/heal the request connection FIRST, even when the table
    # creation is cached. flask_mysqldb hands back the same connection object for
    # the whole request even if its socket died mid-request, so callers (profile
    # load + every profile tool executor) rely on this to recover a dead
    # connection rather than failing with InterfaceError(0, '').
    try:
        refresh_flask_mysql_connection(current_app.mysql)
    except Exception:
        pass
    if _tables_ensured:
        return
    try:
        cur = current_app.mysql.connection.cursor()
        for i, sql in enumerate(_TABLES_SQL):
            try:
                cur.execute(sql)
            except Exception as table_err:
                print(f"[UserProfileDB] Table {i} creation error: {table_err}")
                current_app.mysql.connection.rollback()
        current_app.mysql.connection.commit()

        # Idempotent migration: add the cross-session rolling `summary` column to
        # an existing user_conversations table (mirrors the anonymous-profile
        # conversation_summary). Safe to run every boot — a duplicate-column error
        # just rolls back and is ignored.
        try:
            cur.execute("SELECT summary FROM user_conversations LIMIT 0")
        except Exception:
            current_app.mysql.connection.rollback()
            try:
                cur.execute("ALTER TABLE user_conversations ADD COLUMN summary TEXT DEFAULT NULL")
                current_app.mysql.connection.commit()
            except Exception as alter_err:
                print(f"[UserProfileDB] summary column migration skipped: {alter_err}")
                try:
                    current_app.mysql.connection.rollback()
                except Exception:
                    pass

        # Migration: if user_conversations exists with wrong schema, recreate it
        try:
            cur.execute("SELECT username, session_id FROM user_conversations LIMIT 0")
        except Exception:
            current_app.mysql.connection.rollback()
            try:
                cur.execute("DROP TABLE IF EXISTS user_conversations")
                cur.execute(_USER_CONVERSATIONS_SQL)  # Re-create with correct schema
                current_app.mysql.connection.commit()
                print("[UserProfileDB] Recreated user_conversations table with correct schema")
            except Exception as e2:
                print(f"[UserProfileDB] Migration error for user_conversations: {e2}")

        # Idempotent migration: add the cert-expiry reminder marker to an existing
        # user_certifications table (so the daily cert-expiry job can dedupe). Safe
        # to run every boot — a duplicate-column error just rolls back and is ignored.
        try:
            cur.execute("SELECT expiry_reminded_for FROM user_certifications LIMIT 0")
        except Exception:
            current_app.mysql.connection.rollback()
            try:
                cur.execute("ALTER TABLE user_certifications ADD COLUMN expiry_reminded_for VARCHAR(20) DEFAULT NULL")
                current_app.mysql.connection.commit()
            except Exception as alter_err:
                print(f"[UserProfileDB] expiry_reminded_for column migration skipped: {alter_err}")
                try:
                    current_app.mysql.connection.rollback()
                except Exception:
                    pass

        cur.close()
        _tables_ensured = True
    except Exception as e:
        print(f"[UserProfileDB] Table creation error: {e}")


# ── Skills CRUD ──

def get_skills(username):
    cur = current_app.mysql.connection.cursor(MySQLdb.cursors.DictCursor)
    cur.execute("SELECT id, skill_name, skill_level, source FROM user_skills WHERE username = %s ORDER BY skill_name", (username,))
    rows = cur.fetchall()
    cur.close()
    return list(rows)


def add_skill(username, skill_name, skill_level="mellem", source="chatbot"):
    cur = current_app.mysql.connection.cursor()
    cur.execute(
        "INSERT INTO user_skills (username, skill_name, skill_level, source) VALUES (%s, %s, %s, %s) "
        "ON DUPLICATE KEY UPDATE skill_level = VALUES(skill_level), source = VALUES(source)",
        (username, skill_name.strip(), skill_level, source)
    )
    current_app.mysql.connection.commit()
    cur.close()
    return True


def remove_skill(username, skill_name):
    cur = current_app.mysql.connection.cursor()
    cur.execute("DELETE FROM user_skills WHERE username = %s AND skill_name = %s", (username, skill_name.strip()))
    affected = cur.rowcount
    current_app.mysql.connection.commit()
    cur.close()
    return affected > 0


def update_skill_level(username, skill_name, new_level):
    cur = current_app.mysql.connection.cursor()
    cur.execute("UPDATE user_skills SET skill_level = %s WHERE username = %s AND skill_name = %s",
                (new_level, username, skill_name.strip()))
    affected = cur.rowcount
    current_app.mysql.connection.commit()
    cur.close()
    return affected > 0


# ── Experience CRUD ──

def get_experience(username):
    cur = current_app.mysql.connection.cursor(MySQLdb.cursors.DictCursor)
    cur.execute("SELECT * FROM user_experience WHERE username = %s ORDER BY COALESCE(end_year, 9999) DESC, start_year DESC", (username,))
    rows = cur.fetchall()
    cur.close()
    return list(rows)


def add_experience(username, title, company="", start_year=None, end_year=None, is_current=False, description=""):
    cur = current_app.mysql.connection.cursor()
    cur.execute(
        "INSERT INTO user_experience (username, title, company, start_year, end_year, is_current, description) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s)",
        (username, title.strip(), company.strip(), start_year, end_year, int(is_current), description.strip() if description else None)
    )
    current_app.mysql.connection.commit()
    new_id = cur.lastrowid
    cur.close()
    return new_id


def remove_experience(username, experience_id):
    cur = current_app.mysql.connection.cursor()
    cur.execute("DELETE FROM user_experience WHERE id = %s AND username = %s", (experience_id, username))
    affected = cur.rowcount
    current_app.mysql.connection.commit()
    cur.close()
    return affected > 0


def update_experience(username, experience_id, **fields):
    if not fields:
        return False
    allowed = {"title", "company", "start_year", "end_year", "is_current", "description"}
    updates = {k: v for k, v in fields.items() if k in allowed}
    if not updates:
        return False
    set_clause = ", ".join(f"{k} = %s" for k in updates)
    values = list(updates.values()) + [experience_id, username]
    cur = current_app.mysql.connection.cursor()
    cur.execute(f"UPDATE user_experience SET {set_clause} WHERE id = %s AND username = %s", tuple(values))
    affected = cur.rowcount
    current_app.mysql.connection.commit()
    cur.close()
    return affected > 0


# ── Education CRUD ──

def get_education(username):
    cur = current_app.mysql.connection.cursor(MySQLdb.cursors.DictCursor)
    cur.execute("SELECT * FROM user_education WHERE username = %s ORDER BY COALESCE(year_completed, 9999) DESC", (username,))
    rows = cur.fetchall()
    cur.close()
    return list(rows)


def add_education(username, degree, institution="", year_completed=None, description=""):
    cur = current_app.mysql.connection.cursor()
    cur.execute(
        "INSERT INTO user_education (username, degree, institution, year_completed, description) "
        "VALUES (%s, %s, %s, %s, %s)",
        (username, degree.strip(), institution.strip(), year_completed, description.strip() if description else None)
    )
    current_app.mysql.connection.commit()
    new_id = cur.lastrowid
    cur.close()
    return new_id


def remove_education(username, education_id):
    cur = current_app.mysql.connection.cursor()
    cur.execute("DELETE FROM user_education WHERE id = %s AND username = %s", (education_id, username))
    affected = cur.rowcount
    current_app.mysql.connection.commit()
    cur.close()
    return affected > 0


def update_education(username, education_id, **fields):
    """Update an existing education entry."""
    if not fields:
        return False
    allowed = {"degree", "institution", "year_completed", "description"}
    updates = {k: v for k, v in fields.items() if k in allowed}
    if not updates:
        return False
    set_clause = ", ".join(f"{k} = %s" for k in updates)
    values = list(updates.values()) + [education_id, username]
    cur = current_app.mysql.connection.cursor()
    cur.execute(f"UPDATE user_education SET {set_clause} WHERE id = %s AND username = %s", tuple(values))
    affected = cur.rowcount
    current_app.mysql.connection.commit()
    cur.close()
    return affected > 0


# ── Completed Courses CRUD ──

def get_completed_courses(username):
    cur = current_app.mysql.connection.cursor(MySQLdb.cursors.DictCursor)
    cur.execute("SELECT * FROM user_completed_courses WHERE username = %s ORDER BY added_at DESC", (username,))
    rows = cur.fetchall()
    cur.close()
    return list(rows)


def add_completed_course(username, course_title, course_handle=None, vendor="", completed_date=None, certificate_note=None):
    cur = current_app.mysql.connection.cursor()
    cur.execute(
        "INSERT INTO user_completed_courses (username, course_title, course_handle, vendor, completed_date, certificate_note) "
        "VALUES (%s, %s, %s, %s, %s, %s) "
        "ON DUPLICATE KEY UPDATE vendor = VALUES(vendor), completed_date = VALUES(completed_date), certificate_note = VALUES(certificate_note)",
        (username, course_title.strip(), course_handle, vendor.strip(), completed_date, certificate_note)
    )
    current_app.mysql.connection.commit()
    cur.close()
    return True


def remove_completed_course(username, course_title):
    cur = current_app.mysql.connection.cursor()
    cur.execute("DELETE FROM user_completed_courses WHERE username = %s AND course_title = %s", (username, course_title.strip()))
    affected = cur.rowcount
    current_app.mysql.connection.commit()
    cur.close()
    return affected > 0


# ── Certifications CRUD ──
# First-class certifications (distinct from completed courses): a real credential
# with an issuer, optional issue/expiry dates, credential id and verify URL. The
# profile UI derives a validity state (gyldig / udløber snart / udløbet) from
# expiry_date. Dates are stored as free-form strings ('YYYY', 'YYYY-MM' or
# 'YYYY-MM-DD') so partial dates from a CV survive round-trips.

def get_certifications(username):
    cur = current_app.mysql.connection.cursor(MySQLdb.cursors.DictCursor)
    cur.execute(
        "SELECT id, name, issuer, issue_date, expiry_date, credential_id, credential_url "
        "FROM user_certifications WHERE username = %s ORDER BY added_at DESC",
        (username,)
    )
    rows = cur.fetchall()
    cur.close()
    return list(rows)


def add_certification(username, name, issuer="", issue_date=None, expiry_date=None,
                      credential_id=None, credential_url=None, source="manual"):
    cur = current_app.mysql.connection.cursor()
    cur.execute(
        "INSERT INTO user_certifications "
        "(username, name, issuer, issue_date, expiry_date, credential_id, credential_url, source) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
        (username, name.strip()[:255], (issuer or "").strip()[:255],
         (issue_date or None), (expiry_date or None),
         (credential_id or None), (credential_url or None), source)
    )
    current_app.mysql.connection.commit()
    new_id = cur.lastrowid
    cur.close()
    return new_id


def remove_certification(username, certification_id):
    cur = current_app.mysql.connection.cursor()
    cur.execute("DELETE FROM user_certifications WHERE id = %s AND username = %s", (certification_id, username))
    affected = cur.rowcount
    current_app.mysql.connection.commit()
    cur.close()
    return affected > 0


def update_certification(username, certification_id, **fields):
    allowed = {"name", "issuer", "issue_date", "expiry_date", "credential_id", "credential_url"}
    updates = {k: v for k, v in fields.items() if k in allowed and v is not None}
    if not updates:
        return False
    set_clause = ", ".join(f"{k} = %s" for k in updates)
    values = list(updates.values()) + [certification_id, username]
    cur = current_app.mysql.connection.cursor()
    cur.execute(f"UPDATE user_certifications SET {set_clause} WHERE id = %s AND username = %s", tuple(values))
    affected = cur.rowcount
    current_app.mysql.connection.commit()
    cur.close()
    return affected > 0


# ── Languages CRUD ──

_LANGUAGE_LEVELS = ("begynder", "mellem", "flydende", "modersmaal")


def get_languages(username):
    cur = current_app.mysql.connection.cursor(MySQLdb.cursors.DictCursor)
    cur.execute(
        "SELECT id, language, proficiency FROM user_languages WHERE username = %s "
        "ORDER BY FIELD(proficiency,'modersmaal','flydende','mellem','begynder'), language",
        (username,)
    )
    rows = cur.fetchall()
    cur.close()
    return list(rows)


def add_language(username, language, proficiency="mellem", source="manual"):
    if proficiency not in _LANGUAGE_LEVELS:
        proficiency = "mellem"
    cur = current_app.mysql.connection.cursor()
    cur.execute(
        "INSERT INTO user_languages (username, language, proficiency, source) VALUES (%s, %s, %s, %s) "
        "ON DUPLICATE KEY UPDATE proficiency = VALUES(proficiency), source = VALUES(source)",
        (username, language.strip()[:100], proficiency, source)
    )
    current_app.mysql.connection.commit()
    cur.close()
    return True


def remove_language(username, language):
    cur = current_app.mysql.connection.cursor()
    cur.execute("DELETE FROM user_languages WHERE username = %s AND language = %s", (username, language.strip()))
    affected = cur.rowcount
    current_app.mysql.connection.commit()
    cur.close()
    return affected > 0


def update_language_level(username, language, new_level):
    if new_level not in _LANGUAGE_LEVELS:
        return False
    cur = current_app.mysql.connection.cursor()
    cur.execute("UPDATE user_languages SET proficiency = %s WHERE username = %s AND language = %s",
                (new_level, username, language.strip()))
    affected = cur.rowcount
    current_app.mysql.connection.commit()
    cur.close()
    return affected > 0


# ── Portfolio Links CRUD ──
# External links the learner wants on their profile: LinkedIn, GitHub, a
# portfolio site, a published cert, etc. `kind` drives the icon in the UI.

_PORTFOLIO_KINDS = ("linkedin", "github", "portfolio", "website", "certificate", "other", "link")


def _infer_link_kind(url):
    u = (url or "").lower()
    if "linkedin." in u:
        return "linkedin"
    if "github." in u or "gitlab." in u:
        return "github"
    if "behance." in u or "dribbble." in u or "portfolio" in u:
        return "portfolio"
    return "website"


def get_portfolio_links(username):
    cur = current_app.mysql.connection.cursor(MySQLdb.cursors.DictCursor)
    cur.execute("SELECT id, label, url, kind FROM user_portfolio_links WHERE username = %s ORDER BY added_at DESC", (username,))
    rows = cur.fetchall()
    cur.close()
    return list(rows)


def add_portfolio_link(username, label, url, kind=None, source="manual"):
    url = (url or "").strip()
    if not url:
        return None
    if kind not in _PORTFOLIO_KINDS:
        kind = _infer_link_kind(url)
    cur = current_app.mysql.connection.cursor()
    cur.execute(
        "INSERT INTO user_portfolio_links (username, label, url, kind, source) VALUES (%s, %s, %s, %s, %s)",
        (username, (label or "").strip()[:150] or url[:150], url[:500], kind, source)
    )
    current_app.mysql.connection.commit()
    new_id = cur.lastrowid
    cur.close()
    return new_id


def remove_portfolio_link(username, link_id):
    cur = current_app.mysql.connection.cursor()
    cur.execute("DELETE FROM user_portfolio_links WHERE id = %s AND username = %s", (link_id, username))
    affected = cur.rowcount
    current_app.mysql.connection.commit()
    cur.close()
    return affected > 0


# ── User Memories (atomic free-form facts) ──
# The AI's working memory ABOUT the user — preferences, life context, traits,
# interests that don't fit a structured profile section. Surfaced on the
# Mind-Map and injected (relevance-filtered) into every chat turn.

_MEMORY_CATEGORIES = ("praeference", "maal", "kontekst", "personlighed", "interesse", "andet")

# Danish + English stopwords for the lightweight relevance scorer. Small on
# purpose — this is keyword overlap, not NLP.
_MEMORY_STOPWORDS = {
    "og", "i", "jeg", "det", "at", "en", "den", "til", "er", "som", "på", "de",
    "med", "han", "af", "for", "ikke", "der", "var", "mig", "men", "et", "har",
    "om", "vi", "min", "havde", "ham", "hun", "nu", "over", "da", "fra", "du",
    "ud", "sin", "dem", "os", "op", "man", "hans", "hvor", "eller", "hvad", "skal",
    "kan", "vil", "være", "blev", "her", "dig", "din", "the", "a", "to", "of",
    "and", "is", "in", "it", "you", "my", "me", "we", "for", "on", "with",
}


def _memory_tokens(text):
    """Lowercase alnum tokens minus stopwords (for the relevance scorer)."""
    import re
    toks = re.findall(r"[a-zA-ZæøåÆØÅ0-9]+", (text or "").lower())
    return {t for t in toks if len(t) > 2 and t not in _MEMORY_STOPWORDS}


def get_memories(username):
    cur = current_app.mysql.connection.cursor(MySQLdb.cursors.DictCursor)
    cur.execute(
        "SELECT id, category, label, detail, source, confidence, used_count, "
        "last_used_at, created_at, updated_at "
        "FROM user_memories WHERE username = %s ORDER BY updated_at DESC",
        (username,)
    )
    rows = cur.fetchall()
    cur.close()
    return list(rows)


def add_memory(username, label, category="andet", detail=None, source="ai", confidence=0.8):
    label = (label or "").strip()
    if not label:
        return None
    if category not in _MEMORY_CATEGORIES:
        category = "andet"
    cur = current_app.mysql.connection.cursor()
    cur.execute(
        "INSERT INTO user_memories (username, category, label, detail, source, confidence) "
        "VALUES (%s, %s, %s, %s, %s, %s) "
        "ON DUPLICATE KEY UPDATE category = VALUES(category), detail = VALUES(detail), "
        "source = VALUES(source), confidence = VALUES(confidence)",
        (username, category, label[:200], (detail or None), source, float(confidence or 0.8))
    )
    current_app.mysql.connection.commit()
    new_id = cur.lastrowid
    cur.close()
    return new_id


def update_memory(username, memory_id, **fields):
    allowed = {"category", "label", "detail", "confidence"}
    updates = {k: v for k, v in fields.items() if k in allowed and v is not None}
    if "category" in updates and updates["category"] not in _MEMORY_CATEGORIES:
        updates.pop("category")
    if not updates:
        return False
    set_clause = ", ".join(f"{k} = %s" for k in updates)
    values = list(updates.values()) + [memory_id, username]
    cur = current_app.mysql.connection.cursor()
    cur.execute(f"UPDATE user_memories SET {set_clause} WHERE id = %s AND username = %s", tuple(values))
    affected = cur.rowcount
    current_app.mysql.connection.commit()
    cur.close()
    return affected > 0


def remove_memory(username, memory_id):
    cur = current_app.mysql.connection.cursor()
    cur.execute("DELETE FROM user_memories WHERE id = %s AND username = %s", (memory_id, username))
    affected = cur.rowcount
    current_app.mysql.connection.commit()
    cur.close()
    return affected > 0


def touch_memories(username, memory_ids):
    """Mark memories as used this turn: bump used_count + last_used_at. Guarded."""
    ids = [int(i) for i in (memory_ids or []) if str(i).strip().isdigit()]
    if not ids:
        return 0
    placeholders = ", ".join(["%s"] * len(ids))
    cur = current_app.mysql.connection.cursor()
    cur.execute(
        f"UPDATE user_memories SET used_count = used_count + 1, last_used_at = NOW() "
        f"WHERE username = %s AND id IN ({placeholders})",
        tuple([username] + ids)
    )
    affected = cur.rowcount
    current_app.mysql.connection.commit()
    cur.close()
    return affected


def select_relevant_memories(username, query_text, limit=6):
    """Return the memories most relevant to `query_text` (token overlap), falling
    back to the most-recently-updated when nothing overlaps. Used to inject a
    focused 'what I know about you' block per turn rather than dumping everything.
    """
    try:
        memories = get_memories(username)
    except Exception:
        return []
    if not memories:
        return []
    q_tokens = _memory_tokens(query_text)
    scored = []
    for m in memories:
        m_tokens = _memory_tokens((m.get("label") or "") + " " + (m.get("detail") or ""))
        overlap = len(q_tokens & m_tokens)
        scored.append((overlap, m))
    # Anything with overlap is relevant; sort by overlap desc, then recency (input
    # order is already updated_at DESC). Stable sort preserves recency tie-break.
    relevant = [m for ov, m in sorted(scored, key=lambda x: x[0], reverse=True) if ov > 0]
    if relevant:
        return relevant[:limit]
    # No keyword hit — fall back to the most recent few so personalization still
    # happens (e.g. "what should I take next?" with no shared tokens).
    return memories[: min(limit, 4)]


_MEMORY_CATEGORY_LABELS = {
    "praeference": "Præference", "maal": "Mål", "kontekst": "Kontekst",
    "personlighed": "Personlighed", "interesse": "Interesse", "andet": "Andet",
}


def format_memories_for_ai(memories):
    """Compact text block of memories for the system context. Empty -> ''."""
    if not memories:
        return ""
    lines = []
    for m in memories[:12]:
        cat = _MEMORY_CATEGORY_LABELS.get(m.get("category"), "Andet")
        line = f"- [{cat}] {m.get('label')}"
        if m.get("detail"):
            line += f": {m['detail']}"
        lines.append(line)
    return "\n".join(lines)


# ── Profile completeness (drives the AI Profiler) ──

# (key, human label, predicate over the full-profile dict)
_COMPLETENESS_SECTIONS = [
    ("headline", "Overskrift", lambda p: bool((p.get("headline") or "").strip())),
    ("skills", "Kompetencer", lambda p: len(p.get("skills") or []) > 0),
    ("experience", "Erfaring", lambda p: len(p.get("experience") or []) > 0),
    ("education", "Uddannelse", lambda p: len(p.get("education") or []) > 0),
    ("certifications", "Certificeringer", lambda p: len(p.get("certifications") or []) > 0),
    ("languages", "Sprog", lambda p: len(p.get("languages") or []) > 0),
    ("goals", "Karrieremål", lambda p: bool((p.get("goals") or "").strip()) or len(p.get("learning_goals") or []) > 0),
    ("preferred_format", "Læringspræferencer", lambda p: bool((p.get("preferred_format") or "").strip()) or bool((p.get("preferred_location") or "").strip())),
]


def profile_completeness(username, profile=None):
    """Return {'pct', 'done', 'total', 'sections':[{key,label,done}], 'missing':[label]}.

    Drives the AI Profiler's "complete me to 100%" loop. Accepts a pre-fetched
    full-profile dict (to avoid a second DB round-trip) or fetches it.
    """
    try:
        p = profile if profile is not None else get_full_profile(username)
    except Exception:
        p = {}
    sections = []
    for key, label, pred in _COMPLETENESS_SECTIONS:
        try:
            done = bool(pred(p))
        except Exception:
            done = False
        sections.append({"key": key, "label": label, "done": done})
    done_count = sum(1 for s in sections if s["done"])
    total = len(sections)
    return {
        "pct": round(done_count / total * 100) if total else 0,
        "done": done_count,
        "total": total,
        "sections": sections,
        "missing": [s["label"] for s in sections if not s["done"]],
    }


# ── Profile Summary CRUD ──

def get_profile_summary(username):
    cur = current_app.mysql.connection.cursor(MySQLdb.cursors.DictCursor)
    cur.execute("SELECT * FROM user_profile_summary WHERE username = %s", (username,))
    row = cur.fetchone()
    cur.close()
    return dict(row) if row else None


def update_profile_summary(username, **fields):
    allowed = {"headline", "bio", "goals", "preferred_location", "preferred_format", "budget_range"}
    updates = {k: v for k, v in fields.items() if k in allowed and v is not None}
    if not updates:
        return False
    # Upsert
    columns = ["username"] + list(updates.keys())
    placeholders = ", ".join(["%s"] * len(columns))
    on_dup = ", ".join(f"{k} = VALUES({k})" for k in updates)
    values = [username] + list(updates.values())
    cur = current_app.mysql.connection.cursor()
    cur.execute(
        f"INSERT INTO user_profile_summary ({', '.join(columns)}) VALUES ({placeholders}) "
        f"ON DUPLICATE KEY UPDATE {on_dup}",
        tuple(values)
    )
    current_app.mysql.connection.commit()
    cur.close()
    return True


# ── Learning Goals CRUD ──

_GOAL_STATUSES = ("aktiv", "fuldfoert", "paa_pause")


def get_learning_goals(username):
    cur = current_app.mysql.connection.cursor(MySQLdb.cursors.DictCursor)
    cur.execute(
        "SELECT id, title, description, target_date, status, created_at, updated_at "
        "FROM user_learning_goals WHERE username = %s "
        "ORDER BY (status = 'aktiv') DESC, updated_at DESC",
        (username,)
    )
    rows = cur.fetchall()
    cur.close()
    return list(rows)


def add_learning_goal(username, title, description="", target_date=None):
    cur = current_app.mysql.connection.cursor()
    cur.execute(
        "INSERT INTO user_learning_goals (username, title, description, target_date, status) "
        "VALUES (%s, %s, %s, %s, 'aktiv')",
        (username, title.strip()[:255], (description or "").strip() or None,
         (target_date or "").strip()[:50] or None)
    )
    goal_id = cur.lastrowid
    current_app.mysql.connection.commit()
    cur.close()
    return goal_id


def update_learning_goal(username, goal_id, **fields):
    allowed = {"title", "description", "target_date", "status"}
    updates = {k: v for k, v in fields.items() if k in allowed and v is not None}
    if "status" in updates and updates["status"] not in _GOAL_STATUSES:
        updates.pop("status")
    if not updates:
        return False
    sets = ", ".join(f"{k} = %s" for k in updates)
    vals = list(updates.values()) + [username, goal_id]
    cur = current_app.mysql.connection.cursor()
    cur.execute(f"UPDATE user_learning_goals SET {sets} WHERE username = %s AND id = %s", tuple(vals))
    affected = cur.rowcount
    current_app.mysql.connection.commit()
    cur.close()
    return affected > 0


def delete_learning_goal(username, goal_id):
    cur = current_app.mysql.connection.cursor()
    cur.execute("DELETE FROM user_learning_goals WHERE username = %s AND id = %s", (username, goal_id))
    affected = cur.rowcount
    current_app.mysql.connection.commit()
    cur.close()
    return affected > 0


def format_goals_for_ai(goals):
    """Compact text of active goals for the AI system context."""
    active = [g for g in (goals or []) if g.get("status") == "aktiv"]
    if not active:
        return ""
    bits = []
    for g in active[:6]:
        s = g["title"]
        if g.get("target_date"):
            s += f" (inden {g['target_date']})"
        bits.append(s)
    return "Aktive udviklingsmål: " + "; ".join(bits)


# ── Full Profile Snapshot (for AI context) ──

def get_full_profile(username):
    """Get a complete profile snapshot for use by the AI chatbot."""
    profile = get_profile_summary(username) or {}
    skills = get_skills(username)
    experience = get_experience(username)
    education = get_education(username)
    courses = get_completed_courses(username)
    try:
        goals = get_learning_goals(username)
    except Exception:
        goals = []
    try:
        certifications = get_certifications(username)
    except Exception:
        certifications = []
    try:
        languages = get_languages(username)
    except Exception:
        languages = []
    try:
        portfolio_links = get_portfolio_links(username)
    except Exception:
        portfolio_links = []

    return {
        "username": username,
        "learning_goals": [
            {"id": g["id"], "title": g["title"], "description": g.get("description") or "",
             "target_date": g.get("target_date") or "", "status": g["status"]}
            for g in goals
        ],
        "headline": profile.get("headline", ""),
        "bio": profile.get("bio", ""),
        "goals": profile.get("goals", ""),
        "preferred_location": profile.get("preferred_location", ""),
        "preferred_format": profile.get("preferred_format", ""),
        "budget_range": profile.get("budget_range", ""),
        "skills": [{"name": s["skill_name"], "level": s["skill_level"]} for s in skills],
        "experience": [
            {"id": e["id"], "title": e["title"], "company": e["company"],
             "start_year": e["start_year"], "end_year": e["end_year"],
             "is_current": bool(e["is_current"]), "description": e.get("description", "")}
            for e in experience
        ],
        "education": [
            {"id": e["id"], "degree": e["degree"], "institution": e["institution"],
             "year_completed": e["year_completed"], "description": e.get("description", "")}
            for e in education
        ],
        "completed_courses": [
            {"title": c["course_title"], "vendor": c["vendor"],
             "completed_date": c.get("completed_date", ""), "handle": c.get("course_handle", ""),
             "certificate_note": c.get("certificate_note", "")}
            for c in courses
        ],
        "certifications": [
            {"id": c["id"], "name": c["name"], "issuer": c.get("issuer") or "",
             "issue_date": c.get("issue_date") or "", "expiry_date": c.get("expiry_date") or "",
             "credential_id": c.get("credential_id") or "", "credential_url": c.get("credential_url") or ""}
            for c in certifications
        ],
        "languages": [
            {"id": l["id"], "language": l["language"], "proficiency": l["proficiency"]}
            for l in languages
        ],
        "portfolio_links": [
            {"id": p["id"], "label": p["label"], "url": p["url"], "kind": p.get("kind") or "link"}
            for p in portfolio_links
        ],
    }


def format_profile_for_ai(profile_data):
    """Format the full profile into a concise text block for the AI system message."""
    if not profile_data:
        return ""

    parts = []
    if profile_data.get("headline"):
        parts.append(f"Overskrift: {profile_data['headline']}")
    if profile_data.get("bio"):
        parts.append(f"Bio: {profile_data['bio'][:200]}")
    if profile_data.get("goals"):
        parts.append(f"Mål: {profile_data['goals'][:200]}")
    active_goals = [g for g in profile_data.get("learning_goals", []) if g.get("status") == "aktiv"]
    if active_goals:
        gstrs = [g["title"] + (f" (inden {g['target_date']})" if g.get("target_date") else "") for g in active_goals[:6]]
        parts.append("Aktive udviklingsmål: " + "; ".join(gstrs))
    if profile_data.get("preferred_location"):
        parts.append(f"Foretrukken lokation: {profile_data['preferred_location']}")
    if profile_data.get("preferred_format"):
        parts.append(f"Foretrukken format: {profile_data['preferred_format']}")
    if profile_data.get("budget_range"):
        parts.append(f"Budget: {profile_data['budget_range']}")

    skills = profile_data.get("skills", [])
    if skills:
        skill_strs = [f"{s['name']} ({s['level']})" for s in skills[:15]]
        parts.append(f"Kompetencer: {', '.join(skill_strs)}")

    exp = profile_data.get("experience", [])
    if exp:
        exp_strs = []
        for e in exp[:5]:
            period = f"{e['start_year'] or '?'}-{'nu' if e['is_current'] else (e['end_year'] or '?')}"
            exp_strs.append(f"{e['title']} @ {e['company']} ({period})")
        parts.append(f"Erfaring: {'; '.join(exp_strs)}")

    edu = profile_data.get("education", [])
    if edu:
        edu_strs = [f"{e['degree']} — {e['institution']} ({e.get('year_completed', '?')})" for e in edu[:5]]
        parts.append(f"Uddannelse: {'; '.join(edu_strs)}")

    courses = profile_data.get("completed_courses", [])
    if courses:
        course_strs = [f"{c['title']} ({c['vendor']})" for c in courses[:10]]
        parts.append(f"Gennemførte kurser: {', '.join(course_strs)}")

    certs = profile_data.get("certifications", [])
    if certs:
        cert_strs = []
        for c in certs[:10]:
            s = c["name"]
            if c.get("issuer"):
                s += f" ({c['issuer']})"
            if c.get("expiry_date"):
                s += f", udløber {c['expiry_date']}"
            cert_strs.append(s)
        parts.append(f"Certificeringer: {'; '.join(cert_strs)}")

    languages = profile_data.get("languages", [])
    if languages:
        lang_strs = [f"{l['language']} ({l['proficiency']})" for l in languages[:10]]
        parts.append(f"Sprog: {', '.join(lang_strs)}")

    return "\n".join(parts) if parts else ""


# ── Conversation Persistence (logged-in users) ──

def save_conversation(username, session_id, messages):
    """Save conversation messages to MySQL for session persistence."""
    import json as _json
    # Only save user + assistant messages (skip system/tool for size)
    saved = [m for m in messages if m.get("role") in ("user", "assistant") and m.get("content")]
    refresh_flask_mysql_connection(current_app.mysql)
    cur = current_app.mysql.connection.cursor()
    cur.execute(
        "INSERT INTO user_conversations (username, session_id, messages) VALUES (%s, %s, %s) "
        "ON DUPLICATE KEY UPDATE session_id = VALUES(session_id), messages = VALUES(messages)",
        (username, session_id, _json.dumps(saved, ensure_ascii=False))
    )
    current_app.mysql.connection.commit()
    cur.close()


def load_conversation(username):
    """Load saved conversation for a logged-in user. Returns dict or None."""
    import json as _json
    cur = current_app.mysql.connection.cursor(MySQLdb.cursors.DictCursor)
    cur.execute("SELECT session_id, messages, updated_at FROM user_conversations WHERE username = %s", (username,))
    row = cur.fetchone()
    cur.close()
    if not row or not row.get("messages"):
        return None
    try:
        msgs = _json.loads(row["messages"])
    except (_json.JSONDecodeError, TypeError):
        return None
    return {"session_id": row["session_id"], "messages": msgs, "updated_at": row["updated_at"]}


def clear_conversation(username):
    """Clear saved conversation for a logged-in user (new conversation)."""
    cur = current_app.mysql.connection.cursor()
    cur.execute("DELETE FROM user_conversations WHERE username = %s", (username,))
    current_app.mysql.connection.commit()
    cur.close()


# ── Cross-session rolling summary (logged-in users) ──
# Mirrors the anonymous-profile `conversation_summary` so memory survives session
# boundaries for logged-in users too — not just the verbatim last messages.

def save_conversation_summary(username, session_id, summary_text):
    """Persist a rolling conversation summary for a logged-in user.

    Upserts on the user_conversations row (PK = username). session_id keeps the
    row's NOT NULL constraint satisfied when no conversation row exists yet; if a
    row already exists, the session_id is refreshed and the existing messages are
    left untouched. The summary is capped to keep the column small. Returns True
    on success, False if there was nothing to store.
    """
    text = (summary_text or "").strip()
    if not text:
        return False
    text = text[:4000]  # cap — the summary is a compact recap, not a transcript
    # Best-effort, like save_conversation_history: a rolling-summary persistence
    # failure must never raise into the SSE turn that triggered it.
    try:
        refresh_flask_mysql_connection(current_app.mysql)
        cur = current_app.mysql.connection.cursor()
        cur.execute(
            "INSERT INTO user_conversations (username, session_id, summary) VALUES (%s, %s, %s) "
            "ON DUPLICATE KEY UPDATE session_id = VALUES(session_id), summary = VALUES(summary)",
            (username, session_id or "", text)
        )
        current_app.mysql.connection.commit()
        cur.close()
        return True
    except Exception:
        return False


def load_conversation_summary(username):
    """Load the rolling cross-session summary for a logged-in user (or "")."""
    cur = current_app.mysql.connection.cursor(MySQLdb.cursors.DictCursor)
    try:
        cur.execute("SELECT summary FROM user_conversations WHERE username = %s", (username,))
        row = cur.fetchone()
    except Exception:
        # Column may not exist yet on a not-yet-migrated table — degrade to "".
        cur.close()
        return ""
    cur.close()
    if not row:
        return ""
    return (row.get("summary") or "").strip()


# ── Conversation History (multi-session) ──

def _extract_title(messages, max_len=60):
    """Extract a title from the first user message."""
    for m in messages:
        if m.get("role") == "user" and m.get("content"):
            text = m["content"].strip()
            if len(text) > max_len:
                return text[:max_len].rsplit(" ", 1)[0] + "..."
            return text
    return "Ny samtale"


def save_conversation_history(username, session_id, messages):
    """Save or update a conversation in the history table."""
    import json as _json
    saved = [m for m in messages if m.get("role") in ("user", "assistant") and m.get("content")]
    if not saved:
        return
    title = _extract_title(saved)
    try:
        current_app.mysql.connection.ping(True)
    except Exception:
        pass
    cur = current_app.mysql.connection.cursor()
    # Check if this session already exists
    cur.execute("SELECT id FROM conversation_history WHERE username = %s AND session_id = %s", (username, session_id))
    row = cur.fetchone()
    if row:
        cur.execute(
            "UPDATE conversation_history SET messages = %s, title = %s WHERE id = %s",
            (_json.dumps(saved, ensure_ascii=False), title, row["id"])
        )
    else:
        cur.execute(
            "INSERT INTO conversation_history (username, session_id, title, messages) VALUES (%s, %s, %s, %s)",
            (username, session_id, title, _json.dumps(saved, ensure_ascii=False))
        )
    current_app.mysql.connection.commit()
    cur.close()


def list_conversations(username, limit=30):
    """List conversation history for a user, newest first."""
    try:
        current_app.mysql.connection.ping(True)
    except Exception:
        pass
    cur = current_app.mysql.connection.cursor(MySQLdb.cursors.DictCursor)
    cur.execute(
        "SELECT id, session_id, title, created_at, updated_at FROM conversation_history "
        "WHERE username = %s ORDER BY updated_at DESC LIMIT %s",
        (username, limit)
    )
    rows = cur.fetchall()
    cur.close()
    return rows


def load_conversation_by_id(username, conv_id):
    """Load a specific conversation by its ID."""
    import json as _json
    # Ensure connection is alive
    try:
        current_app.mysql.connection.ping(True)
    except Exception:
        pass
    cur = current_app.mysql.connection.cursor(MySQLdb.cursors.DictCursor)
    cur.execute(
        "SELECT id, session_id, title, messages, updated_at FROM conversation_history "
        "WHERE id = %s AND username = %s",
        (conv_id, username)
    )
    row = cur.fetchone()
    cur.close()
    if not row:
        print(f"[load_conversation_by_id] No row found for id={conv_id}, user={username}")
        return None
    if not row.get("messages"):
        print(f"[load_conversation_by_id] Empty messages for id={conv_id}")
        # Return with empty messages instead of None so it doesn't 404
        row["messages"] = []
        return row
    try:
        row["messages"] = _json.loads(row["messages"])
    except Exception as e:
        print(f"[load_conversation_by_id] JSON parse error for id={conv_id}: {e}")
        row["messages"] = []
    return row


def delete_conversation(username, conv_id):
    """Delete a conversation from history."""
    cur = current_app.mysql.connection.cursor()
    cur.execute("DELETE FROM conversation_history WHERE id = %s AND username = %s", (conv_id, username))
    current_app.mysql.connection.commit()
    affected = cur.rowcount
    cur.close()
    return affected > 0
