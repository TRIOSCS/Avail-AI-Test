"""Drop dead AI-scoring columns from requisition_tasks.

What (DDL, reversible):
  - DROP requisition_tasks.ai_priority_score (Float, was 0.0-1.0 urgency score)
  - DROP requisition_tasks.ai_risk_flag (String(255), was a short risk-alert string)

Why: both columns were written by nothing. Their only writers were the AI/heuristic
task-scoring helpers (score_tasks_with_ai / apply_simple_scoring), which were never
wired into any request path and have been removed as dead code. The only readers were
the requisition Task-board row template and a JSON list endpoint, both of which always
saw NULL. No feature depended on them, so they are dropped rather than left inert.

Downgrade: fully reversible — recreates both columns as nullable (no data to restore,
since nothing ever populated them).

Called by: alembic (upgrade/downgrade).
Depends on: requisition_tasks (table exists since the initial task-board schema).

Revision ID: 182_drop_task_ai_columns
Revises: 181_add_user_display_timezone
Create Date: 2026-07-04
"""

import sqlalchemy as sa

from alembic import op

revision = "182_drop_task_ai_columns"
down_revision = "181_add_user_display_timezone"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_column("requisition_tasks", "ai_risk_flag")
    op.drop_column("requisition_tasks", "ai_priority_score")


def downgrade() -> None:
    op.add_column(
        "requisition_tasks",
        sa.Column("ai_priority_score", sa.Float(), nullable=True),
    )
    op.add_column(
        "requisition_tasks",
        sa.Column("ai_risk_flag", sa.String(length=255), nullable=True),
    )
