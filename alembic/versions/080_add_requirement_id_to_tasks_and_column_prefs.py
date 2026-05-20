"""Add requirement_id to requisition_tasks and parts_column_prefs to users.

Supports the new split-panel parts workspace:
- requirement_id lets tasks link to a specific part number
- parts_column_prefs stores per-user visible column choices

Revision ID: 080
Revises: 079
"""

import sqlalchemy as sa

from alembic import op

revision = "080"
down_revision = "079"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "requisition_tasks",
        sa.Column("requirement_id", sa.Integer(), sa.ForeignKey("requirements.id", ondelete="SET NULL"), nullable=True),
    )
    op.create_index("ix_rt_requirement", "requisition_tasks", ["requirement_id"], if_not_exists=True)

    op.add_column(
        "users",
        sa.Column("parts_column_prefs", sa.JSON(), nullable=True),
    )


def downgrade():
    op.execute("ALTER TABLE IF EXISTS users DROP COLUMN IF EXISTS parts_column_prefs")
    op.drop_index("ix_rt_requirement", table_name="requisition_tasks", if_exists=True)
    op.execute("ALTER TABLE IF EXISTS requisition_tasks DROP COLUMN IF EXISTS requirement_id")
