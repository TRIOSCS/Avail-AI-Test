"""Additive Trading (resell-brokerage) schema foundation.

Purely ADDITIVE — no drops:
- create ``excess_offers`` (inbound broker offer header) + ``excess_offer_lines``;
- add rollup + material-card columns to ``excess_line_items``
  (material_card_id, best_offer_unit_price, best_offer_id, offer_count);
- add ``version`` to ``excess_lists``.

Bid / BidSolicitation are intentionally left untouched this chunk; a later cutover
revision removes them.

Revision ID: 126_trading_additive_schema
Revises: 125_enrichment_provenance
Create Date: 2026-06-23
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "127_trading_additive_schema"
down_revision: str | None = "126_unified_attachments"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # --- excess_line_items: material card + best-offer rollup columns ---
    op.add_column(
        "excess_line_items",
        sa.Column("material_card_id", sa.Integer(), nullable=True),
    )
    op.create_foreign_key(
        "fk_excess_line_items_material_card",
        "excess_line_items",
        "material_cards",
        ["material_card_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.add_column(
        "excess_line_items",
        sa.Column("best_offer_unit_price", sa.Numeric(precision=12, scale=4), nullable=True),
    )
    op.add_column(
        "excess_line_items",
        sa.Column("best_offer_id", sa.Integer(), nullable=True),
    )
    op.add_column(
        "excess_line_items",
        sa.Column("offer_count", sa.Integer(), nullable=False, server_default="0"),
    )

    # --- excess_lists: lock-on-post version ---
    op.add_column(
        "excess_lists",
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
    )

    # --- excess_offers: inbound broker offer header ---
    op.create_table(
        "excess_offers",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("excess_list_id", sa.Integer(), nullable=False),
        sa.Column("submitted_by", sa.Integer(), nullable=False),
        sa.Column("offerer_company_id", sa.Integer(), nullable=True),
        sa.Column("offerer_vendor_card_id", sa.Integer(), nullable=True),
        sa.Column("scope", sa.String(length=20), nullable=True),
        sa.Column("take_all_total_price", sa.Numeric(precision=12, scale=4), nullable=True),
        sa.Column("valid_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=True),
        sa.ForeignKeyConstraint(["excess_list_id"], ["excess_lists.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["submitted_by"], ["users.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["offerer_company_id"], ["companies.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["offerer_vendor_card_id"], ["vendor_cards.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_excess_offers_list", "excess_offers", ["excess_list_id"])
    op.create_index("ix_excess_offers_status", "excess_offers", ["status"])

    # --- excess_offer_lines: per-line offer rows (incl. unmatched queue) ---
    op.create_table(
        "excess_offer_lines",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("offer_id", sa.Integer(), nullable=False),
        sa.Column("excess_line_item_id", sa.Integer(), nullable=True),
        sa.Column("mpn_raw", sa.String(length=100), nullable=False),
        sa.Column("quantity", sa.Integer(), nullable=False),
        sa.Column("unit_price", sa.Numeric(precision=12, scale=4), nullable=True),
        sa.Column("lead_time_days", sa.Integer(), nullable=True),
        sa.Column("terms_text", sa.Text(), nullable=True),
        sa.Column("match_status", sa.String(length=20), nullable=True),
        sa.ForeignKeyConstraint(["offer_id"], ["excess_offers.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["excess_line_item_id"], ["excess_line_items.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_excess_offer_lines_offer", "excess_offer_lines", ["offer_id"])
    op.create_index("ix_excess_offer_lines_line_item", "excess_offer_lines", ["excess_line_item_id"])


def downgrade() -> None:
    op.drop_index("ix_excess_offer_lines_line_item", table_name="excess_offer_lines")
    op.drop_index("ix_excess_offer_lines_offer", table_name="excess_offer_lines")
    op.drop_table("excess_offer_lines")

    op.drop_index("ix_excess_offers_status", table_name="excess_offers")
    op.drop_index("ix_excess_offers_list", table_name="excess_offers")
    op.drop_table("excess_offers")

    op.drop_column("excess_lists", "version")

    op.drop_constraint("fk_excess_line_items_material_card", "excess_line_items", type_="foreignkey")
    op.drop_column("excess_line_items", "offer_count")
    op.drop_column("excess_line_items", "best_offer_id")
    op.drop_column("excess_line_items", "best_offer_unit_price")
    op.drop_column("excess_line_items", "material_card_id")
