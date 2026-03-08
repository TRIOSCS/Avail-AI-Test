"""Add self_heal_log table for pattern tracking.

Revision ID: 040_add_self_heal_log
Revises: 039_add_trouble_tickets
Create Date: 2026-03-02
"""

import sqlalchemy as sa

from alembic import op

revision = "040_add_self_heal_log"
down_revision = "039_add_trouble_tickets"


def upgrade() -> None:
    op.create_table(
        "self_heal_log",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("ticket_id", sa.Integer(), sa.ForeignKey("trouble_tickets.id", ondelete="CASCADE"), nullable=False),
        sa.Column("category", sa.String(20)),
        sa.Column("risk_tier", sa.String(10)),
        sa.Column("files_modified", sa.JSON()),
        sa.Column("fix_succeeded", sa.Boolean()),
        sa.Column("iterations_used", sa.Integer()),
        sa.Column("cost_usd", sa.Float()),
        sa.Column("user_verified", sa.Boolean()),
        sa.Column("created_at", sa.DateTime()),
    )
    op.create_index("ix_self_heal_log_ticket_id", "self_heal_log", ["ticket_id"])
    op.create_index("ix_self_heal_log_created_at", "self_heal_log", ["created_at"])


def downgrade() -> None:
    op.drop_index("ix_self_heal_log_created_at")
    op.drop_index("ix_self_heal_log_ticket_id")
    op.drop_table("self_heal_log")
