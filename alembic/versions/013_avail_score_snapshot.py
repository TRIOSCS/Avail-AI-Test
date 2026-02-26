"""Add avail_score_snapshot table for Avail Score system.

0-100 scoring (50 behaviors + 50 outcomes) ranking buyers and salespeople
with $500/$250 bonus for 1st/2nd place finishers.

Revision ID: 013_avail_score_snapshot
Revises: 012_vendor_name_normalized
Create Date: 2026-02-26
"""

from alembic import op
import sqlalchemy as sa

revision = "013_avail_score_snapshot"
down_revision = "012_vendor_name_normalized"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "avail_score_snapshot",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column(
            "user_id",
            sa.Integer,
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("month", sa.Date, nullable=False),
        sa.Column("role_type", sa.String(20), nullable=False),
        # Behavior metrics (b1–b5)
        sa.Column("b1_score", sa.Float, default=0),
        sa.Column("b1_label", sa.String(50)),
        sa.Column("b1_raw", sa.String(100)),
        sa.Column("b2_score", sa.Float, default=0),
        sa.Column("b2_label", sa.String(50)),
        sa.Column("b2_raw", sa.String(100)),
        sa.Column("b3_score", sa.Float, default=0),
        sa.Column("b3_label", sa.String(50)),
        sa.Column("b3_raw", sa.String(100)),
        sa.Column("b4_score", sa.Float, default=0),
        sa.Column("b4_label", sa.String(50)),
        sa.Column("b4_raw", sa.String(100)),
        sa.Column("b5_score", sa.Float, default=0),
        sa.Column("b5_label", sa.String(50)),
        sa.Column("b5_raw", sa.String(100)),
        sa.Column("behavior_total", sa.Float, default=0),
        # Outcome metrics (o1–o5)
        sa.Column("o1_score", sa.Float, default=0),
        sa.Column("o1_label", sa.String(50)),
        sa.Column("o1_raw", sa.String(100)),
        sa.Column("o2_score", sa.Float, default=0),
        sa.Column("o2_label", sa.String(50)),
        sa.Column("o2_raw", sa.String(100)),
        sa.Column("o3_score", sa.Float, default=0),
        sa.Column("o3_label", sa.String(50)),
        sa.Column("o3_raw", sa.String(100)),
        sa.Column("o4_score", sa.Float, default=0),
        sa.Column("o4_label", sa.String(50)),
        sa.Column("o4_raw", sa.String(100)),
        sa.Column("o5_score", sa.Float, default=0),
        sa.Column("o5_label", sa.String(50)),
        sa.Column("o5_raw", sa.String(100)),
        sa.Column("outcome_total", sa.Float, default=0),
        # Composite
        sa.Column("total_score", sa.Float, default=0),
        sa.Column("rank", sa.Integer),
        sa.Column("qualified", sa.Boolean, default=False),
        sa.Column("bonus_amount", sa.Float, default=0),
        sa.Column("created_at", sa.DateTime),
        sa.Column("updated_at", sa.DateTime),
    )
    op.create_index(
        "ix_ass_user_month",
        "avail_score_snapshot",
        ["user_id", "month", "role_type"],
        unique=True,
    )
    op.create_index(
        "ix_ass_month_role_rank",
        "avail_score_snapshot",
        ["month", "role_type", "rank"],
    )
    op.create_index(
        "ix_ass_month_role_score",
        "avail_score_snapshot",
        ["month", "role_type", "total_score"],
    )


def downgrade() -> None:
    op.drop_index("ix_ass_month_role_score", table_name="avail_score_snapshot")
    op.drop_index("ix_ass_month_role_rank", table_name="avail_score_snapshot")
    op.drop_index("ix_ass_user_month", table_name="avail_score_snapshot")
    op.drop_table("avail_score_snapshot")
