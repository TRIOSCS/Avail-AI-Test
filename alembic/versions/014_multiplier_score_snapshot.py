"""Add multiplier_score_snapshot table for competitive bonus determination.

Non-stacking offer pipeline points (each offer earns only its highest tier)
plus bonus points from RFQs/stock lists (buyer) or accounts (sales).
1st place $500, 2nd place $250 — requires minimum Avail Score.

Revision ID: 014_multiplier_score_snapshot
Revises: 013_avail_score_snapshot
Create Date: 2026-02-26
"""

from alembic import op
import sqlalchemy as sa

revision = "014_multiplier_score_snapshot"
down_revision = "013_avail_score_snapshot"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "multiplier_score_snapshot",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column(
            "user_id",
            sa.Integer,
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("month", sa.Date, nullable=False),
        sa.Column("role_type", sa.String(20), nullable=False),
        # Totals
        sa.Column("offer_points", sa.Float, default=0),
        sa.Column("bonus_points", sa.Float, default=0),
        sa.Column("total_points", sa.Float, default=0),
        # Buyer breakdown: offer pipeline tiers
        sa.Column("offers_total", sa.Integer, default=0),
        sa.Column("offers_base_count", sa.Integer, default=0),
        sa.Column("offers_base_pts", sa.Float, default=0),
        sa.Column("offers_quoted_count", sa.Integer, default=0),
        sa.Column("offers_quoted_pts", sa.Float, default=0),
        sa.Column("offers_bp_count", sa.Integer, default=0),
        sa.Column("offers_bp_pts", sa.Float, default=0),
        sa.Column("offers_po_count", sa.Integer, default=0),
        sa.Column("offers_po_pts", sa.Float, default=0),
        sa.Column("rfqs_sent_count", sa.Integer, default=0),
        sa.Column("rfqs_sent_pts", sa.Float, default=0),
        sa.Column("stock_lists_count", sa.Integer, default=0),
        sa.Column("stock_lists_pts", sa.Float, default=0),
        # Sales breakdown: quotes + proactive + accounts
        sa.Column("quotes_sent_count", sa.Integer, default=0),
        sa.Column("quotes_sent_pts", sa.Float, default=0),
        sa.Column("quotes_won_count", sa.Integer, default=0),
        sa.Column("quotes_won_pts", sa.Float, default=0),
        sa.Column("proactive_sent_count", sa.Integer, default=0),
        sa.Column("proactive_sent_pts", sa.Float, default=0),
        sa.Column("proactive_converted_count", sa.Integer, default=0),
        sa.Column("proactive_converted_pts", sa.Float, default=0),
        sa.Column("new_accounts_count", sa.Integer, default=0),
        sa.Column("new_accounts_pts", sa.Float, default=0),
        # Ranking
        sa.Column("rank", sa.Integer),
        sa.Column("avail_score", sa.Float, default=0),
        sa.Column("qualified", sa.Boolean, default=False),
        sa.Column("bonus_amount", sa.Float, default=0),
        sa.Column("created_at", sa.DateTime),
        sa.Column("updated_at", sa.DateTime),
    )
    op.create_index(
        "ix_mss_user_month",
        "multiplier_score_snapshot",
        ["user_id", "month", "role_type"],
        unique=True,
    )
    op.create_index(
        "ix_mss_month_role_rank",
        "multiplier_score_snapshot",
        ["month", "role_type", "rank"],
    )
    op.create_index(
        "ix_mss_month_role_points",
        "multiplier_score_snapshot",
        ["month", "role_type", "total_points"],
    )


def downgrade() -> None:
    op.drop_index("ix_mss_month_role_points", table_name="multiplier_score_snapshot")
    op.drop_index("ix_mss_month_role_rank", table_name="multiplier_score_snapshot")
    op.drop_index("ix_mss_user_month", table_name="multiplier_score_snapshot")
    op.drop_table("multiplier_score_snapshot")
