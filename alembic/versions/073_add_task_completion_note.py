"""Add completion_note column and creator_status index to requisition_tasks.

Supports the simplified task workflow where assignees complete tasks
with a resolution note, and the "Waiting On" query uses created_by + status.

Revision ID: 073
Revises: 072
Create Date: 2026-03-11
"""

from alembic import op

revision = "073"
down_revision = "072"


def upgrade():
    op.execute("ALTER TABLE requisition_tasks ADD COLUMN IF NOT EXISTS completion_note TEXT")
    op.create_index("ix_rt_creator_status", "requisition_tasks", ["created_by", "status"], if_not_exists=True)


def downgrade():
    op.drop_index("ix_rt_creator_status", table_name="requisition_tasks", if_exists=True)
    op.execute("ALTER TABLE IF EXISTS requisition_tasks DROP COLUMN IF EXISTS completion_note")
