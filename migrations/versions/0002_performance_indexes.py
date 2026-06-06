"""performance indexes — hot-path indexes for dashboards/reports.

Revision ID: 0002_performance_indexes
Revises: 0001_baseline
Create Date: 2026-06-06

PURPOSE
-------
Add indexes on the columns the admin / HR / chatbot-AI dashboards filter and
group on (created_at, company_id, status, username, query_text). Without them
those queries full-scan the largest tables (chatbot_interactions, course_orders,
company_users), which is the dominant cost of the slow report pages.

RELATIONSHIP TO THE RUNTIME BOOTSTRAP
-------------------------------------
On live PythonAnywhere the authoritative applier is the runtime ensurer
``performance_indexes.ensure_performance_indexes(app)`` (wired into a
before_request hook in run.py), so the indexes land on a plain ``git pull`` +
web-app reload. This Alembic revision records the SAME index set as part of the
migration history for DBs managed via ``alembic upgrade``. Per migrations/env.py
this module must stay STANDALONE (no app imports), so the list is duplicated
here — keep it in sync with ``performance_indexes.PERFORMANCE_INDEXES``.

SAFETY
------
upgrade() is idempotent: every index is checked against INFORMATION_SCHEMA and
skipped if it (or a required column / the table) is absent, so running it against
the live DB cannot fail or double-create. downgrade() drops only indexes that
exist.
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = "0002_performance_indexes"
down_revision: Union[str, Sequence[str], None] = "0001_baseline"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# (table, index_name, columns_sql, required_columns)
# Mirror of performance_indexes.PERFORMANCE_INDEXES — keep in sync.
INDEXES = [
    ("chatbot_interactions", "idx_ci_created", "created_at", ["created_at"]),
    ("chatbot_interactions", "idx_ci_user_created", "username, created_at", ["username", "created_at"]),
    ("chatbot_interactions", "idx_ci_query", "query_text(191)", ["query_text"]),
    ("chatbot_interactions", "idx_ci_company_created", "company_id, created_at", ["company_id", "created_at"]),
    ("chatbot_interactions", "idx_ci_session", "session_id", ["session_id"]),
    ("course_orders", "idx_co_company_created", "company_id, created_at", ["company_id", "created_at"]),
    ("course_orders", "idx_co_company_status", "company_id, status", ["company_id", "status"]),
    ("course_orders", "idx_co_status", "status", ["status"]),
    ("course_orders", "idx_co_user_company", "user_id, company_id", ["user_id", "company_id"]),
    ("company_users", "idx_cu_company_status", "company_id, status", ["company_id", "status"]),
    ("company_users", "idx_cu_company_status_login", "company_id, status, last_login", ["company_id", "status", "last_login"]),
    ("company_users", "idx_cu_user_company", "user_id, company_id", ["user_id", "company_id"]),
    ("employee_learning_progress", "idx_elp_user_company", "user_id, company_id", ["user_id", "company_id"]),
    ("ai_agent_runs", "idx_aar_created", "created_at", ["created_at"]),
    ("users", "idx_users_role", "role", ["role"]),
    ("users", "idx_users_created", "created_at", ["created_at"]),
    ("notifications", "idx_notif_user_read", "user_id, `read`", ["user_id", "read"]),
    ("credit_usage", "idx_credit_usage_user", "username, `timestamp`", ["username", "timestamp"]),
    ("app_usage", "idx_app_usage_user", "username", ["username"]),
    ("social_metrics", "idx_social_metrics_user", "username", ["username"]),
]


def _table_exists(bind, table) -> bool:
    return bool(bind.execute(
        sa.text("SELECT 1 FROM INFORMATION_SCHEMA.TABLES "
                "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = :t LIMIT 1"),
        {"t": table},
    ).first())


def _columns(bind, table):
    rows = bind.execute(
        sa.text("SELECT COLUMN_NAME FROM INFORMATION_SCHEMA.COLUMNS "
                "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = :t"),
        {"t": table},
    ).fetchall()
    return {str(r[0]).lower() for r in rows}


def _index_exists(bind, table, name) -> bool:
    return bool(bind.execute(
        sa.text("SELECT 1 FROM INFORMATION_SCHEMA.STATISTICS "
                "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = :t AND INDEX_NAME = :n LIMIT 1"),
        {"t": table, "n": name},
    ).first())


def upgrade() -> None:
    bind = op.get_bind()
    table_cols = {}
    for table, name, cols_sql, required in INDEXES:
        if table not in table_cols:
            table_cols[table] = _columns(bind, table) if _table_exists(bind, table) else None
        cols = table_cols[table]
        if not cols:
            continue
        if not all(c.lower() in cols for c in required):
            continue
        if _index_exists(bind, table, name):
            continue
        op.execute(f"ALTER TABLE `{table}` ADD INDEX `{name}` ({cols_sql})")


def downgrade() -> None:
    bind = op.get_bind()
    for table, name, _cols_sql, _required in INDEXES:
        if _table_exists(bind, table) and _index_exists(bind, table, name):
            op.execute(f"ALTER TABLE `{table}` DROP INDEX `{name}`")
