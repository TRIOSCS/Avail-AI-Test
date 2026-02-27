"""Add composite index on activity_log(requisition_id, channel, created_at).

Speeds up activity queries filtered by requisition + channel (e.g. RFQ timeline).

Revision ID: 019_activity_req_channel
Revises: 018_missing_orm_cols
Create Date: 2026-02-27
"""

from alembic import op

revision = "019_activity_req_channel"
down_revision = "018_missing_orm_cols"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_activity_req_channel
        ON activity_log (requisition_id, channel, created_at)
        WHERE requisition_id IS NOT NULL
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_activity_req_channel")
