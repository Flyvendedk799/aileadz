"""Add launch workflow storage without discarding existing orders or history.

Revision ID: c001_launch_workflows
Revises: b001_part_b_lifecycle

Runtime enterprise bootstrap uses the same canonical definitions and can safely
run before this revision. Legacy assignment snapshots are populated by the
runtime data migration after all enterprise tables exist. Downgrade preserves
customer data; roll application code back without dropping these additive tables.
"""

import re
from alembic import op
import sqlalchemy as sa

revision = "c001_launch_workflows"
down_revision = "b001_part_b_lifecycle"
branch_labels = None
depends_on = None

TABLES = {
    "customer_accounts",
    "company_launch_checks",
    "customer_requests",
    "sales_enquiries",
    "mail_outbox",
    "course_order_changes",
    "learning_outcome_reviews",
    "learning_assignment_steps",
    "course_order_details",
}


def upgrade():
    from schema_registry import REGISTRY_DDL

    for ddl in REGISTRY_DDL:
        match = re.search(r"CREATE TABLE IF NOT EXISTS\s+(\w+)", ddl)
        if match and match.group(1) in TABLES:
            op.execute(ddl)
    bind = op.get_bind()
    exists = bind.execute(
        sa.text(
            "SELECT COUNT(*) FROM INFORMATION_SCHEMA.COLUMNS WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='course_orders' AND COLUMN_NAME='cancellation_fee'"
        )
    ).scalar()
    if not exists:
        op.execute("ALTER TABLE course_orders ADD COLUMN cancellation_fee DECIMAL(10,2) NOT NULL DEFAULT 0")


def downgrade():
    pass
