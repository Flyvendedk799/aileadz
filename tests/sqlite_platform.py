"""Extra SQLite schema + app factory for vendor/enterprise/settings tests.

Builds on ``tests.sqlite_mysql.SqliteMysql`` without editing it: adds the vendor,
webhook, token and request tables, teaches the translator ``DATE_ADD(NOW(),
INTERVAL n MINUTE)``, and offers ``make_app(db)`` which boots the real Flask app
with the in-memory database as ``app.mysql`` (boot-time MySQL DDL hooks disabled).
"""

import os
import re

os.environ.setdefault("SANDBOX", "1")
os.environ.setdefault("AI_WARMUP_ON_IMPORT", "0")
os.environ.setdefault("SCHEDULER_OPPORTUNISTIC", "0")

from unittest import mock  # noqa: E402

from tests import sqlite_mysql  # noqa: E402

EXTRA_SCHEMA = """
ALTER TABLE users ADD COLUMN status TEXT DEFAULT 'active';
ALTER TABLE company_users ADD COLUMN employee_id TEXT;
ALTER TABLE companies ADD COLUMN industry TEXT;
ALTER TABLE companies ADD COLUMN updated_at TEXT;
ALTER TABLE companies ADD COLUMN subscription_plan TEXT DEFAULT 'trial';
ALTER TABLE companies ADD COLUMN features TEXT;
ALTER TABLE companies ADD COLUMN created_at TEXT DEFAULT CURRENT_TIMESTAMP;
ALTER TABLE vendors ADD COLUMN password_hash TEXT;
ALTER TABLE vendors ADD COLUMN description TEXT;
ALTER TABLE vendors ADD COLUMN website TEXT;
ALTER TABLE vendors ADD COLUMN logo_url TEXT;
ALTER TABLE vendors ADD COLUMN invite_token TEXT;
ALTER TABLE vendors ADD COLUMN invite_expires_at TEXT;
ALTER TABLE vendors ADD COLUMN created_at TEXT DEFAULT CURRENT_TIMESTAMP;
ALTER TABLE vendors ADD COLUMN updated_at TEXT DEFAULT CURRENT_TIMESTAMP;
CREATE TABLE vendor_submissions (id INTEGER PRIMARY KEY AUTOINCREMENT, vendor_id INTEGER, job_id TEXT,
  filename TEXT, row_count INTEGER, status TEXT DEFAULT 'pending', reviewed_by INTEGER, reviewed_at TEXT,
  created_at TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE account_requests (id INTEGER PRIMARY KEY AUTOINCREMENT, company_id INTEGER, kind TEXT,
  requested_by INTEGER, requested_by_name TEXT, note TEXT, status TEXT DEFAULT 'open', handled_by INTEGER,
  created_at TEXT DEFAULT CURRENT_TIMESTAMP, handled_at TEXT);
CREATE TABLE company_webhooks (id INTEGER PRIMARY KEY AUTOINCREMENT, company_id INTEGER, name TEXT, url TEXT,
  secret TEXT, events TEXT, is_active INTEGER DEFAULT 1, total_deliveries INTEGER DEFAULT 0,
  successful_deliveries INTEGER DEFAULT 0, failed_deliveries INTEGER DEFAULT 0, last_delivery_at TEXT,
  created_by INTEGER, created_at TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE event_outbox (id INTEGER PRIMARY KEY AUTOINCREMENT, company_id INTEGER, event_type TEXT,
  payload TEXT, status TEXT DEFAULT 'pending', attempts INTEGER DEFAULT 0, last_error TEXT,
  created_at TEXT DEFAULT CURRENT_TIMESTAMP, delivered_at TEXT);
CREATE TABLE webhook_deliveries (id INTEGER PRIMARY KEY AUTOINCREMENT, outbox_id INTEGER, webhook_id INTEGER,
  company_id INTEGER, event_type TEXT, status TEXT DEFAULT 'pending', attempts INTEGER DEFAULT 0,
  http_status INTEGER, last_error TEXT, created_at TEXT DEFAULT CURRENT_TIMESTAMP, delivered_at TEXT);
CREATE TABLE company_api_keys (id INTEGER PRIMARY KEY AUTOINCREMENT, company_id INTEGER, key_name TEXT,
  api_key TEXT, api_key_hash TEXT, key_prefix TEXT, permissions TEXT, is_active INTEGER DEFAULT 1,
  last_used_at TEXT, created_by INTEGER, created_at TEXT DEFAULT CURRENT_TIMESTAMP, expires_at TEXT);
CREATE TABLE company_departments (id INTEGER PRIMARY KEY AUTOINCREMENT, company_id INTEGER,
  department_name TEXT, department_code TEXT, description TEXT);
CREATE TABLE company_settings (id INTEGER PRIMARY KEY AUTOINCREMENT, company_id INTEGER UNIQUE,
  enable_white_label INTEGER DEFAULT 0, language TEXT, timezone TEXT, support_email TEXT, support_phone TEXT, company_website TEXT);
"""

_orig_translate = sqlite_mysql.translate


def _translate(sql):
    s = re.sub(r"DATE_SUB\((?:NOW\(\)|CURRENT_TIMESTAMP),\s*INTERVAL\s*(\d+)\s*(MINUTE|HOUR|DAY)\)",
               lambda m: "datetime('now', '-%s %ss')" % (m.group(1), m.group(2).lower()), sql)
    s = re.sub(r"DATE_ADD\(NOW\(\),\s*INTERVAL\s*%s\s*(MINUTE|HOUR|DAY)\)",
               lambda m: "datetime('now', '+' || %s || ' " + m.group(1).lower() + "s')", s)
    return _orig_translate(s)


sqlite_mysql.translate = _translate


class PlatformDB(sqlite_mysql.SqliteMysql):
    def __init__(self):
        super().__init__()
        self.raw.executescript(EXTRA_SCHEMA)
        self.raw.commit()


def make_app(db):
    """The real app with ``db`` as MySQL and the boot-time MySQL DDL hooks off."""
    import run
    app = run.create_app()
    app.config["TESTING"] = True
    app.mysql = db
    for flag in ("_branding_schema_ensured", "_enterprise_tables_created", "_perf_indexes_ensured",
                 "_ai_subsystems_warmed"):
        setattr(app, flag, True)
    return app


def client_as(app, **session_values):
    c = app.test_client()
    with c.session_transaction() as s:
        s.update(session_values)
    return c


def render_patches():
    """Patches needed to render fm_base pages against SQLite (branding lookup)."""
    return mock.patch("white_label_global_integration.get_template_context", return_value={})
