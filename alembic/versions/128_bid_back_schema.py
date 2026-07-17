"""Additive bid-back (Customer Bid) schema + excess-list posting window (Chunk E).

Purely ADDITIVE — no drops:
- create ``customer_bids`` (the outbound bid-back header) + ``customer_bid_lines``
  (one priced line each, with internal offer provenance that is never exported);
- add ``open_at`` + ``close_at`` (nullable) to ``excess_lists`` — the posting window
  (open_at stamped on publish, close_at on close; close_at drives the "closes in Xd" chip).

Bid / BidSolicitation are intentionally left untouched; a later cutover revision removes them.

Revision ID: 127_bid_back_schema
Revises: 126_trading_additive_schema
Create Date: 2026-06-23
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "128_bid_back_schema"
down_revision: str | None = "127_trading_additive_schema"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # --- excess_lists: posting window ---
    op.add_column("excess_lists", sa.Column("open_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("excess_lists", sa.Column("close_at", sa.DateTime(timezone=True), nullable=True))

    # --- customer_bids: outbound bid-back header ---
    op.create_table(
        "customer_bids",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("excess_list_id", sa.Integer(), nullable=False),
        sa.Column("owner_id", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=True),
        # ``revision`` counts assembly passes. Under D3 (2026-07) a re-assemble off a
        # TERMINAL (accepted/rejected) prior bid INSERTs a NEW customer_bids row
        # (revision + 1) so the answered revision is frozen history; a non-terminal
        # (draft/sent) prior still bumps in place on the same row.
        sa.Column("revision", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=True),
        sa.ForeignKeyConstraint(["excess_list_id"], ["excess_lists.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["owner_id"], ["users.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_customer_bids_list", "customer_bids", ["excess_list_id"])
    op.create_index("ix_customer_bids_owner", "customer_bids", ["owner_id"])
    op.create_index("ix_customer_bids_status", "customer_bids", ["status"])

    # --- customer_bid_lines: per-line priced rows (internal provenance never exported) ---
    op.create_table(
        "customer_bid_lines",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("customer_bid_id", sa.Integer(), nullable=False),
        sa.Column("excess_line_item_id", sa.Integer(), nullable=True),
        sa.Column("selected_offer_id", sa.Integer(), nullable=True),
        sa.Column("selected_offer_line_id", sa.Integer(), nullable=True),
        sa.Column("customer_unit_price", sa.Numeric(precision=12, scale=4), nullable=True),
        sa.Column("quantity", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(["customer_bid_id"], ["customer_bids.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["excess_line_item_id"], ["excess_line_items.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["selected_offer_id"], ["excess_offers.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["selected_offer_line_id"], ["excess_offer_lines.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_customer_bid_lines_bid", "customer_bid_lines", ["customer_bid_id"])
    op.create_index("ix_customer_bid_lines_line_item", "customer_bid_lines", ["excess_line_item_id"])


def downgrade() -> None:
    op.drop_index("ix_customer_bid_lines_line_item", table_name="customer_bid_lines")
    op.drop_index("ix_customer_bid_lines_bid", table_name="customer_bid_lines")
    op.drop_table("customer_bid_lines")

    op.drop_index("ix_customer_bids_status", table_name="customer_bids")
    op.drop_index("ix_customer_bids_owner", table_name="customer_bids")
    op.drop_index("ix_customer_bids_list", table_name="customer_bids")
    op.drop_table("customer_bids")

    op.drop_column("excess_lists", "close_at")
    op.drop_column("excess_lists", "open_at")
