"""Resell-outreach tracking schema (ADDITIVE).

Revision ID: 132_resell_outreach_schema
Revises: 131_tbf_search_tables
Create Date: 2026-06-23

The outbound trader->buyer half of Resell: who the trader offered excess to and how
each buyer responded. ADDITIVE only — no drops, no existing-column changes:
- create excess_outreach (one row per buyer x line; the tracking spine)
- create buyer_scores (1:1 per vendor_card buyer-engagement rollup)
- add activity_log.excess_list_id (nullable SET NULL scope + partial index)

Downgrade reverses each (drop the column + index, drop both tables).
"""

revision = "132_resell_outreach_schema"
down_revision = "131_tbf_search_tables"

import sqlalchemy as sa

from alembic import op


def upgrade():
    # ── excess_outreach ───────────────────────────────────────────────
    op.create_table(
        "excess_outreach",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column(
            "excess_list_id",
            sa.Integer,
            sa.ForeignKey("excess_lists.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "excess_line_item_id",
            sa.Integer,
            sa.ForeignKey("excess_line_items.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "target_vendor_card_id",
            sa.Integer,
            sa.ForeignKey("vendor_cards.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "submitted_by",
            sa.Integer,
            sa.ForeignKey("users.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("channel", sa.String(20), server_default="email"),
        sa.Column("status", sa.String(20), server_default="sent"),
        sa.Column("graph_message_id", sa.String(255), nullable=True),
        sa.Column("graph_conversation_id", sa.String(255), nullable=True),
        sa.Column("parts_included", sa.JSON, nullable=True),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        if_not_exists=True,
    )
    op.create_index("ix_excess_outreach_list", "excess_outreach", ["excess_list_id"], if_not_exists=True)
    op.create_index("ix_excess_outreach_vendor_card", "excess_outreach", ["target_vendor_card_id"], if_not_exists=True)
    op.create_index("ix_excess_outreach_status", "excess_outreach", ["status"], if_not_exists=True)
    op.create_index("ix_excess_outreach_conversation", "excess_outreach", ["graph_conversation_id"], if_not_exists=True)

    # ── buyer_scores ──────────────────────────────────────────────────
    op.create_table(
        "buyer_scores",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column(
            "vendor_card_id",
            sa.Integer,
            sa.ForeignKey("vendor_cards.id", ondelete="CASCADE"),
            nullable=False,
            unique=True,
        ),
        sa.Column("offers_received", sa.Integer, nullable=False, server_default="0"),
        sa.Column("wins", sa.Integer, nullable=False, server_default="0"),
        sa.Column("avg_bid_pct_of_ask", sa.Numeric(6, 2), nullable=True),
        sa.Column("response_rate", sa.Numeric(5, 2), nullable=True),
        sa.Column("median_response_hours", sa.Numeric(8, 2), nullable=True),
        sa.Column("last_offered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("commodity_affinity", sa.JSON, nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        if_not_exists=True,
    )
    op.create_index("ix_buyer_scores_vendor_card", "buyer_scores", ["vendor_card_id"], unique=True, if_not_exists=True)

    # ── activity_log.excess_list_id scope ─────────────────────────────
    op.add_column(
        "activity_log",
        sa.Column(
            "excess_list_id",
            sa.Integer,
            sa.ForeignKey("excess_lists.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    op.create_index(
        "ix_activity_excess_list",
        "activity_log",
        ["excess_list_id", "created_at"],
        postgresql_where=sa.text("excess_list_id IS NOT NULL"),
        if_not_exists=True,
    )


def downgrade():
    op.drop_index("ix_activity_excess_list", table_name="activity_log", if_exists=True)
    op.drop_column("activity_log", "excess_list_id")

    op.drop_index("ix_buyer_scores_vendor_card", table_name="buyer_scores", if_exists=True)
    op.drop_table("buyer_scores", if_exists=True)

    op.drop_index("ix_excess_outreach_conversation", table_name="excess_outreach", if_exists=True)
    op.drop_index("ix_excess_outreach_status", table_name="excess_outreach", if_exists=True)
    op.drop_index("ix_excess_outreach_vendor_card", table_name="excess_outreach", if_exists=True)
    op.drop_index("ix_excess_outreach_list", table_name="excess_outreach", if_exists=True)
    op.drop_table("excess_outreach", if_exists=True)
