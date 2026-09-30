"""order lifecycle, billing dimension, unified notifications (Part B, N-1.1/N-3.2/N-3.4).

Revision ID: b001_part_b_lifecycle
Revises: 0002_performance_indexes
Create Date: 2026-09-30

What it does (all idempotent; safe on a database the runtime bootstrap already
touched):

* ``course_orders``: vendor_id, billing_status, invoice_due_date, payment_method,
  payment_reference, billing_note, booked_at, booked_by, cancel_reason,
  request_notes, group_order_id, invoice_date.
* ``order_status_history``, ``company_team_order_policy``, ``schema_meta``.
* ``notifications``: the per-recipient columns of the unified notification table.
* Data: the legacy status values are mapped onto the lifecycle
  (``pending`` -> ``approved``, ``confirmed``/``processing`` -> ``booked``,
  ``invoiced``/``paid`` moved to ``billing_status``).

The runtime ``enterprise_tables.ensure_enterprise_tables`` adds the same columns
from the single table definitions, and ``schema_registry.run_data_migrations``
applies the same data fix once (flagged in ``schema_meta``) - so it does not matter
which one runs first.

Merge note: if Part A adds its own revision off ``0002_performance_indexes``,
run ``alembic merge heads`` (no conflicting DDL).
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "b001_part_b_lifecycle"
down_revision: Union[str, Sequence[str], None] = "0002_performance_indexes"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

ORDER_COLUMNS = [
    ("vendor_id", "INT NULL"),
    ("billing_status", "VARCHAR(20) NOT NULL DEFAULT 'not_invoiced'"),
    ("invoice_date", "DATE NULL"),
    ("invoice_due_date", "DATE NULL"),
    ("payment_method", "VARCHAR(50) NULL"),
    ("payment_reference", "VARCHAR(255) NULL"),
    ("billing_note", "TEXT NULL"),
    ("booked_at", "DATETIME NULL"),
    ("booked_by", "VARCHAR(255) NULL"),
    ("cancel_reason", "VARCHAR(255) NULL"),
    ("request_notes", "TEXT NULL"),
    ("group_order_id", "VARCHAR(50) NULL"),
]

NOTIFICATION_COLUMNS = [
    ("company_id", "INT NULL"),
    ("recipient_user_id", "INT NULL"),
    ("sender_user_id", "INT NULL"),
    ("kind", "VARCHAR(40) DEFAULT 'info'"),
    ("action_url", "VARCHAR(500) NULL"),
    ("is_urgent", "TINYINT DEFAULT 0"),
    ("dedupe_key", "VARCHAR(191) NULL"),
    ("read_at", "DATETIME NULL"),
]

NEW_TABLES = [
    """CREATE TABLE IF NOT EXISTS order_status_history (
        id INT AUTO_INCREMENT PRIMARY KEY,
        order_id VARCHAR(50) NOT NULL,
        company_id INT NULL,
        kind VARCHAR(10) NOT NULL DEFAULT 'status',
        from_value VARCHAR(30) NULL,
        to_value VARCHAR(30) NOT NULL,
        actor_user_id INT NULL,
        actor_kind VARCHAR(20) NOT NULL DEFAULT 'user',
        actor_label VARCHAR(255) NULL,
        note VARCHAR(500) NULL,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        INDEX idx_osh_order (order_id, created_at),
        INDEX idx_osh_company (company_id, created_at)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",
    """CREATE TABLE IF NOT EXISTS company_team_order_policy (
        id INT AUTO_INCREMENT PRIMARY KEY,
        company_id INT NOT NULL,
        vendor_id INT NULL,
        mode VARCHAR(30) NOT NULL DEFAULT 'linked_orders',
        updated_by INT NULL,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
        INDEX idx_ctop_company (company_id, vendor_id)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",
    """CREATE TABLE IF NOT EXISTS schema_meta (
        meta_key VARCHAR(100) PRIMARY KEY,
        meta_value VARCHAR(255),
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",
]

STATUS_FIXES = [
    "UPDATE course_orders SET billing_note = billing_notes WHERE (billing_note IS NULL OR billing_note = '') "
    "AND billing_notes IS NOT NULL AND billing_notes <> ''",
    "UPDATE course_orders SET billing_status = 'paid' WHERE status = 'paid' OR payment_status = 'paid'",
    "UPDATE course_orders SET billing_status = 'invoiced' WHERE billing_status = 'not_invoiced' "
    "AND (status = 'invoiced' OR payment_status = 'invoiced')",
    "UPDATE course_orders SET status = CASE WHEN completion_status = 'completed' THEN 'completed' ELSE 'booked' END "
    "WHERE status IN ('invoiced', 'paid')",
    "UPDATE course_orders SET status = 'booked' WHERE status IN ('processing', 'confirmed')",
    "UPDATE course_orders SET status = 'approved' WHERE status = 'pending'",
]


def _has_table(bind, table: str) -> bool:
    return bool(bind.execute(sa.text(
        "SELECT COUNT(*) FROM INFORMATION_SCHEMA.TABLES WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = :t"),
        {"t": table}).scalar())


def _has_column(bind, table: str, column: str) -> bool:
    return bool(bind.execute(sa.text(
        "SELECT COUNT(*) FROM INFORMATION_SCHEMA.COLUMNS WHERE TABLE_SCHEMA = DATABASE() "
        "AND TABLE_NAME = :t AND COLUMN_NAME = :c"), {"t": table, "c": column}).scalar())


def _add_columns(bind, table: str, columns) -> None:
    if not _has_table(bind, table):
        return
    for name, ddl in columns:
        if not _has_column(bind, table, name):
            op.execute(f"ALTER TABLE `{table}` ADD COLUMN `{name}` {ddl}")


def upgrade() -> None:
    bind = op.get_bind()
    for ddl in NEW_TABLES:
        op.execute(ddl)
    _add_columns(bind, "course_orders", ORDER_COLUMNS)
    _add_columns(bind, "notifications", NOTIFICATION_COLUMNS)
    if _has_table(bind, "course_orders") and _has_column(bind, "course_orders", "billing_status"):
        already = bind.execute(sa.text(
            "SELECT COUNT(*) FROM schema_meta WHERE meta_key = 'order_lifecycle_v1'")).scalar()
        if not already:
            for sql in STATUS_FIXES:
                op.execute(sql)
            op.execute("INSERT INTO schema_meta (meta_key, meta_value) VALUES ('order_lifecycle_v1', 'done')")


def downgrade() -> None:
    # Additive migration: columns/tables are kept on downgrade (dropping order
    # history would lose audit data). Status values are not reverted.
    pass
