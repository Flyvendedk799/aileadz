"""
User Profile Database Layer — connects app1 chatbot to the main MySQL user system.
Manages: skills, work experience, education, completed courses, and profile summary.
"""
import time
from flask import current_app
import MySQLdb.cursors


# ── Table Creation (run once) ──

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
    """CREATE TABLE IF NOT EXISTS user_conversations (
        username VARCHAR(255) PRIMARY KEY,
        session_id VARCHAR(100) NOT NULL,
        messages LONGTEXT,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
    )""",
]


_tables_ensured = False


def ensure_tables():
    """Create tables if they don't exist. Cached after first success (Phase 2C)."""
    global _tables_ensured
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

        # Migration: if user_conversations exists with wrong schema, recreate it
        try:
            cur.execute("SELECT username, session_id FROM user_conversations LIMIT 0")
        except Exception:
            current_app.mysql.connection.rollback()
            try:
                cur.execute("DROP TABLE IF EXISTS user_conversations")
                cur.execute(_TABLES_SQL[-1])  # Re-create with correct schema
                current_app.mysql.connection.commit()
                print("[UserProfileDB] Recreated user_conversations table with correct schema")
            except Exception as e2:
                print(f"[UserProfileDB] Migration error for user_conversations: {e2}")

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


# ── Full Profile Snapshot (for AI context) ──

def get_full_profile(username):
    """Get a complete profile snapshot for use by the AI chatbot."""
    profile = get_profile_summary(username) or {}
    skills = get_skills(username)
    experience = get_experience(username)
    education = get_education(username)
    courses = get_completed_courses(username)

    return {
        "username": username,
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

    return "\n".join(parts) if parts else ""


# ── Conversation Persistence (logged-in users) ──

def save_conversation(username, session_id, messages):
    """Save conversation messages to MySQL for session persistence."""
    import json as _json
    # Only save user + assistant messages (skip system/tool for size)
    saved = [m for m in messages if m.get("role") in ("user", "assistant") and m.get("content")]
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
