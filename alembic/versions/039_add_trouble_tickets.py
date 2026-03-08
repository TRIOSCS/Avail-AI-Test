"""Add trouble_tickets table for self-heal pipeline.

Revision ID: 039_add_trouble_tickets
Revises: 038_api_health_monitoring
Create Date: 2026-03-02
"""

import sqlalchemy as sa

from alembic import op

revision = "039_add_trouble_tickets"
down_revision = "038_api_health_monitoring"


def upgrade() -> None:
    op.create_table(
        "trouble_tickets",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("ticket_number", sa.String(20), unique=True, nullable=False),
        sa.Column("submitted_by", sa.Integer(), sa.ForeignKey("users.id", ondelete="SET NULL")),
        sa.Column("status", sa.String(30), nullable=False, server_default="submitted"),
        sa.Column("risk_tier", sa.String(10)),
        sa.Column("category", sa.String(20)),
        sa.Column("title", sa.String(200), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("current_page", sa.String(500)),
        sa.Column("user_agent", sa.String(500)),
        sa.Column("auto_captured_context", sa.JSON()),
        sa.Column("sanitized_context", sa.JSON()),
        sa.Column("diagnosis", sa.JSON()),
        sa.Column("generated_prompt", sa.Text()),
        sa.Column("file_mapping", sa.JSON()),
        sa.Column("fix_branch", sa.String(200)),
        sa.Column("fix_pr_url", sa.String(500)),
        sa.Column("iterations_used", sa.Integer()),
        sa.Column("cost_tokens", sa.Integer()),
        sa.Column("cost_usd", sa.Float()),
        sa.Column("resolution_notes", sa.Text()),
        sa.Column("parent_ticket_id", sa.Integer(), sa.ForeignKey("trouble_tickets.id", ondelete="SET NULL")),
        sa.Column("created_at", sa.DateTime()),
        sa.Column("updated_at", sa.DateTime()),
        sa.Column("diagnosed_at", sa.DateTime()),
        sa.Column("resolved_at", sa.DateTime()),
    )
    op.create_index("ix_trouble_tickets_status", "trouble_tickets", ["status"])
    op.create_index("ix_trouble_tickets_risk_tier", "trouble_tickets", ["risk_tier"])
    op.create_index("ix_trouble_tickets_submitted_by", "trouble_tickets", ["submitted_by"])
    op.create_index("ix_trouble_tickets_created_at", "trouble_tickets", ["created_at"])


def downgrade() -> None:
    op.drop_index("ix_trouble_tickets_created_at")
    op.drop_index("ix_trouble_tickets_submitted_by")
    op.drop_index("ix_trouble_tickets_risk_tier")
    op.drop_index("ix_trouble_tickets_status")
    op.drop_table("trouble_tickets")
