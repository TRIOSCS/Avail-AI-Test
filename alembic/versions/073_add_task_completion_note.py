"""Add completion_note column and creator_status index to requisition_tasks.

Supports the simplified task workflow where assignees complete tasks
with a resolution note, and the "Waiting On" query uses created_by + status.

Revision ID: 073
Revises: 072
Create Date: 2026-03-11
"""

from alembic import op
import sqlalchemy as sa

revision = "073"
down_revision = "072"


def upgrade():
    op.add_column("requisition_tasks", sa.Column("completion_note", sa.Text(), nullable=True))
    op.create_index("ix_rt_creator_status", "requisition_tasks", ["created_by", "status"])


def downgrade():
    op.drop_index("ix_rt_creator_status", table_name="requisition_tasks")
    op.drop_column("requisition_tasks", "completion_note")
