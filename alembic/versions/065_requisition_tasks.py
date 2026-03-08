"""Requisition Tasks — pipeline-style task board per requisition.

Creates requisition_tasks table for tracking sourcing, sales,
and general tasks with AI priority scoring and risk alerts.

Revision ID: 065
Revises: 064
Create Date: 2026-03-08
"""

from alembic import op
import sqlalchemy as sa

revision = "065"
down_revision = "064"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "requisition_tasks",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("requisition_id", sa.Integer(), sa.ForeignKey("requisitions.id", ondelete="CASCADE"), nullable=False),
        sa.Column("title", sa.String(255), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("task_type", sa.String(20), nullable=False, server_default="general"),
        sa.Column("status", sa.String(20), nullable=False, server_default="todo"),
        sa.Column("priority", sa.Integer(), nullable=False, server_default="2"),
        sa.Column("ai_priority_score", sa.Float(), nullable=True),
        sa.Column("ai_risk_flag", sa.String(255), nullable=True),
        sa.Column("assigned_to_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("created_by", sa.Integer(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("source", sa.String(20), nullable=False, server_default="manual"),
        sa.Column("source_ref", sa.String(100), nullable=True),
        sa.Column("due_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_rt_req_status", "requisition_tasks", ["requisition_id", "status"])
    op.create_index("ix_rt_assignee_status", "requisition_tasks", ["assigned_to_id", "status"])
    op.create_index("ix_rt_status_due", "requisition_tasks", ["status", "due_at"])


def downgrade() -> None:
    op.drop_index("ix_rt_status_due", table_name="requisition_tasks")
    op.drop_index("ix_rt_assignee_status", table_name="requisition_tasks")
    op.drop_index("ix_rt_req_status", table_name="requisition_tasks")
    op.drop_table("requisition_tasks")
