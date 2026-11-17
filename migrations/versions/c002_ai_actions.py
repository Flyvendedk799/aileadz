"""Durable actor-scoped assistant operations, receipts and transcripts."""
from alembic import op

revision = 'c002_ai_actions'
down_revision = 'c001_launch_workflows'
branch_labels = None
depends_on = None


def upgrade():
    from schema_registry import AI_ACTIONS_DDL
    op.execute(AI_ACTIONS_DDL)


def downgrade():
    # Preserve customer history and receipts on an application rollback.
    pass
