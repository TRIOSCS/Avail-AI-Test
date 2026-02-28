"""Unified score snapshot table for cross-role leaderboard.

Revision ID: 036_unified_score_snapshot
Revises: 035_vendor_email_health
Create Date: 2026-02-28

Adds unified_score_snapshot table with 5 category percentages,
weighted unified score, cached source scores, and AI blurb columns.
"""

import sqlalchemy as sa
from alembic import op

revision = "036_unified_score_snapshot"
down_revision = "035_vendor_email_health"


def upgrade() -> None:
    op.create_table(
        "unified_score_snapshot",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("month", sa.Date(), nullable=False),
        # 5 category percentages
        sa.Column("prospecting_pct", sa.Float(), server_default="0"),
        sa.Column("execution_pct", sa.Float(), server_default="0"),
        sa.Column("followthrough_pct", sa.Float(), server_default="0"),
        sa.Column("closing_pct", sa.Float(), server_default="0"),
        sa.Column("depth_pct", sa.Float(), server_default="0"),
        # Weighted total
        sa.Column("unified_score", sa.Float(), server_default="0"),
        sa.Column("rank", sa.Integer()),
        # Source scores
        sa.Column("primary_role", sa.String(20)),
        sa.Column("avail_score_buyer", sa.Float()),
        sa.Column("avail_score_sales", sa.Float()),
        sa.Column("multiplier_points_buyer", sa.Float()),
        sa.Column("multiplier_points_sales", sa.Float()),
        # AI blurb
        sa.Column("ai_blurb_strength", sa.Text()),
        sa.Column("ai_blurb_improvement", sa.Text()),
        sa.Column("ai_blurb_generated_at", sa.DateTime()),
        # Timestamps
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now()),
    )
    op.create_index("ix_uss_user_month", "unified_score_snapshot", ["user_id", "month"], unique=True)
    op.create_index("ix_uss_month_rank", "unified_score_snapshot", ["month", "rank"])


def downgrade() -> None:
    op.drop_index("ix_uss_month_rank", table_name="unified_score_snapshot")
    op.drop_index("ix_uss_user_month", table_name="unified_score_snapshot")
    op.drop_table("unified_score_snapshot")
