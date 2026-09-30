"""A tiny MySQL-flavoured SQLite harness for service-level tests.

The app talks MySQL (PyMySQL, ``%s`` placeholders, ``NOW()``, ``ON DUPLICATE KEY
UPDATE``...).  CI has a real MySQL, but local runs and most unit tests do not, so
the order lifecycle, notifications and credit ledger are exercised against an
in-memory SQLite database through this translator.  Only the MySQL constructs the
code under test actually uses are translated; anything else raises loudly so a
test never silently passes on a statement it did not understand.

Usage::

    db = SqliteMysql()           # schema for orders/notifications/credits created
    app.mysql = db.mysql         # object with .connection
    db.execute("INSERT ...")     # seed helpers
"""

from __future__ import annotations

import datetime
import re
import sqlite3

# MySQL hands back datetime/date objects for these columns; SQLite stores text.
# Templates call .strftime() on them, so convert on the way out.
_DATETIME_COLUMNS = {
    "created_at", "updated_at", "timestamp", "requested_at", "decided_at", "booked_at",
    "completion_date", "completion_deadline", "payment_date", "invoice_date", "invoice_due_date",
    "started_at", "completed_at", "read_at", "captured_at",
}


def _convert(row):
    if row is None:
        return None
    d = dict(row)
    for k in _DATETIME_COLUMNS & set(d):
        v = d[k]
        if isinstance(v, str) and v:
            try:
                d[k] = datetime.datetime.fromisoformat(v)
            except ValueError:
                pass
    return d

SCHEMA = """
CREATE TABLE users (id INTEGER PRIMARY KEY AUTOINCREMENT, username TEXT UNIQUE, password TEXT,
  email TEXT, credits INTEGER DEFAULT 0, role TEXT DEFAULT 'user', email_notifications INTEGER DEFAULT 1,
  first_login_completed INTEGER DEFAULT 0, created_at TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE companies (id INTEGER PRIMARY KEY AUTOINCREMENT, company_name TEXT, company_slug TEXT,
  status TEXT DEFAULT 'active', settings TEXT);
CREATE TABLE company_users (id INTEGER PRIMARY KEY AUTOINCREMENT, company_id INTEGER, user_id INTEGER,
  username TEXT, full_name TEXT, email TEXT, role TEXT DEFAULT 'employee', department TEXT,
  status TEXT DEFAULT 'active', manager_user_id INTEGER, total_courses_completed INTEGER DEFAULT 0,
  courses_completed INTEGER DEFAULT 0);
CREATE TABLE vendors (id INTEGER PRIMARY KEY AUTOINCREMENT, vendor_name TEXT, slug TEXT, contact_email TEXT,
  status TEXT DEFAULT 'active');
CREATE TABLE course_orders (id INTEGER PRIMARY KEY AUTOINCREMENT, order_id TEXT UNIQUE, company_id INTEGER,
  user_id INTEGER, username TEXT, product_handle TEXT, product_title TEXT, price REAL, variant_date TEXT,
  variant_location TEXT, status TEXT DEFAULT 'approved', budget_charged INTEGER DEFAULT 0,
  completion_status TEXT, completion_date TEXT, completion_deadline TEXT, started_at TEXT,
  department TEXT, approved_by INTEGER, payment_status TEXT DEFAULT 'not_paid', payment_date TEXT,
  invoice_number TEXT, billing_notes TEXT, billing_note TEXT, vendor_id INTEGER,
  billing_status TEXT DEFAULT 'not_invoiced', invoice_date TEXT, invoice_due_date TEXT,
  payment_method TEXT, payment_reference TEXT, booked_at TEXT, booked_by TEXT, cancel_reason TEXT,
  request_notes TEXT, group_order_id TEXT, course_source TEXT DEFAULT 'external', user_email TEXT,
  user_name TEXT, user_phone TEXT, chatbot_session_id TEXT, chatbot_queries_before_order INTEGER DEFAULT 0,
  recommended_by_tool TEXT, created_at TEXT DEFAULT CURRENT_TIMESTAMP, updated_at TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE order_approvals (id INTEGER PRIMARY KEY AUTOINCREMENT, order_id TEXT, company_id INTEGER,
  requester_user_id INTEGER, approver_user_id INTEGER, status TEXT DEFAULT 'pending', notes TEXT,
  requested_at TEXT DEFAULT CURRENT_TIMESTAMP, decided_at TEXT);
CREATE TABLE order_status_history (id INTEGER PRIMARY KEY AUTOINCREMENT, order_id TEXT, company_id INTEGER,
  kind TEXT DEFAULT 'status', from_value TEXT, to_value TEXT, actor_user_id INTEGER, actor_kind TEXT,
  actor_label TEXT, note TEXT, created_at TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE department_budgets (id INTEGER PRIMARY KEY AUTOINCREMENT, company_id INTEGER, department TEXT,
  annual_budget REAL DEFAULT 0, spent REAL DEFAULT 0, fiscal_year INTEGER);
CREATE TABLE company_approval_policies (id INTEGER PRIMARY KEY AUTOINCREMENT, company_id INTEGER,
  department TEXT, auto_approve_under REAL, require_approval_over REAL, is_active INTEGER DEFAULT 1);
CREATE TABLE audit_log (id INTEGER PRIMARY KEY AUTOINCREMENT, company_id INTEGER, user_id INTEGER,
  action TEXT, action_type TEXT, resource_type TEXT, resource_id TEXT, description TEXT, details TEXT,
  created_at TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE notifications (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id TEXT, company_id INTEGER,
  recipient_user_id INTEGER, sender_user_id INTEGER, kind TEXT DEFAULT 'info', title TEXT, message TEXT,
  image_url TEXT, action_url TEXT, is_urgent INTEGER DEFAULT 0, dedupe_key TEXT, read INTEGER DEFAULT 0,
  read_at TEXT, timestamp TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE user_completed_courses (id INTEGER PRIMARY KEY AUTOINCREMENT, username TEXT, course_title TEXT,
  course_handle TEXT, vendor TEXT, completed_date TEXT, certificate_note TEXT,
  added_at TEXT DEFAULT CURRENT_TIMESTAMP, UNIQUE(username, course_title));
CREATE TABLE employee_learning_progress (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER,
  company_id INTEGER, learning_path_id INTEGER, course_handle TEXT, content_type TEXT, content_id INTEGER,
  content_name TEXT, status TEXT DEFAULT 'not_started', progress_percentage REAL DEFAULT 0,
  started_at TEXT, completed_at TEXT, due_date TEXT, created_at TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE employee_skills_matrix (id INTEGER PRIMARY KEY AUTOINCREMENT, employee_id INTEGER,
  company_id INTEGER, skill_name TEXT, current_level INTEGER, target_level INTEGER,
  UNIQUE(employee_id, company_id, skill_name));
CREATE TABLE employee_skill_history (id INTEGER PRIMARY KEY AUTOINCREMENT, employee_id INTEGER,
  company_id INTEGER, skill_name TEXT, level INTEGER, previous_level INTEGER, source TEXT, order_id INTEGER,
  captured_at TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE credit_usage (id INTEGER PRIMARY KEY AUTOINCREMENT, username TEXT, user_id INTEGER,
  company_id INTEGER, credits_used INTEGER DEFAULT 0, description TEXT, kind TEXT DEFAULT 'usage',
  assistant TEXT, model TEXT, tokens_in INTEGER, tokens_out INTEGER, actor TEXT,
  timestamp TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE schema_meta (meta_key TEXT PRIMARY KEY, meta_value TEXT, updated_at TEXT DEFAULT CURRENT_TIMESTAMP);
"""


def translate(sql: str) -> str:
    s = sql
    s = s.replace("%%", "%")
    s = re.sub(r"%s", "?", s)
    s = re.sub(r"\s+FOR UPDATE\b", "", s)
    s = re.sub(r"INSERT IGNORE", "INSERT OR IGNORE", s)
    s = re.sub(r"\bNOW\(\)", "CURRENT_TIMESTAMP", s)
    s = re.sub(r"\bCURDATE\(\)", "DATE('now')", s)
    s = re.sub(r"\bGREATEST\(", "MAX(", s)
    s = re.sub(r"DATE_SUB\(CURRENT_TIMESTAMP,\s*INTERVAL\s*\?\s*(MINUTE|HOUR|DAY)\)",
               lambda m: "datetime('now', '-' || ? || ' %ss')" % m.group(1).lower(), s)

    def dup(m):
        body = m.group(1)
        body = re.sub(r"VALUES\((\w+)\)", r"excluded.\1", body)
        return " ON CONFLICT DO UPDATE SET " + body

    s = re.sub(r"\s+ON DUPLICATE KEY UPDATE\s+(.*)$", dup, s, flags=re.S)
    if re.search(r"\b(INTERVAL|UNIX_TIMESTAMP|JSON_CONTAINS|DATE_FORMAT)\b", s):
        raise NotImplementedError("sqlite_mysql cannot translate: " + sql[:120])
    return s


class _Cursor:
    def __init__(self, conn):
        self._c = conn.cursor()
        self._conn = conn
        self.rowcount = 0
        self.lastrowid = None

    def execute(self, sql, params=()):
        self._c.execute(translate(sql), tuple(params or ()))
        self.rowcount = self._c.rowcount
        self.lastrowid = self._c.lastrowid
        return self

    def fetchone(self):
        return _convert(self._c.fetchone())

    def fetchall(self):
        return [_convert(r) for r in self._c.fetchall()]

    def close(self):
        self._c.close()


class _Conn:
    def __init__(self, raw):
        self._raw = raw

    def cursor(self, *a, **k):
        return _Cursor(self._raw)

    def commit(self):
        self._raw.commit()

    def rollback(self):
        self._raw.rollback()

    def ping(self, *a, **k):
        return True

    @property
    def _connection(self):  # flask-mysql wrapper compat (``conn._connection.open``)
        return type("Raw", (), {"open": True})()


class SqliteMysql:
    """Holds the in-memory DB; ``.mysql`` mimics ``app.mysql`` (``.connection``)."""

    def __init__(self):
        raw = sqlite3.connect(":memory:", check_same_thread=False)
        raw.row_factory = sqlite3.Row
        raw.executescript(SCHEMA)
        self.raw = raw
        self.connection = _Conn(raw)
        self.mysql = self

    def execute(self, sql, params=()):
        cur = self.connection.cursor().execute(sql, params)
        self.raw.commit()
        return cur

    def query(self, sql, params=()):
        return self.connection.cursor().execute(sql, params).fetchall()

    def one(self, sql, params=()):
        return self.connection.cursor().execute(sql, params).fetchone()


# ── extra tables used by the HR-workspace tests (appended; safe to merge) ──
SCHEMA_HR = """
CREATE TABLE employee_goals (id INTEGER PRIMARY KEY AUTOINCREMENT, employee_id INTEGER, company_id INTEGER,
  goal_title TEXT, goal_description TEXT, target_date TEXT, status TEXT DEFAULT 'active', progress REAL DEFAULT 0,
  shared_with_employee INTEGER NOT NULL DEFAULT 0, shared_at TEXT, shared_by INTEGER, share_note TEXT,
  created_at TEXT DEFAULT CURRENT_TIMESTAMP);
"""

_orig_init = SqliteMysql.__init__


def _init_with_hr(self):
    _orig_init(self)
    self.raw.executescript(SCHEMA_HR)


SqliteMysql.__init__ = _init_with_hr
