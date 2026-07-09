"""Cutover (Chunk D): drop the retired ``bids`` + ``bid_solicitations`` tables.

The Trading module replaced the old per-line money-required ``Bid`` and the
email-RFQ ``BidSolicitation`` with ``ExcessOffer`` / ``ExcessOfferLine`` (migration
126) and the clean bid-back ``CustomerBid`` / ``CustomerBidLine`` (migration 127).
With the old models, service methods, router and templates removed, the two backing
tables are dead. This drops them.

``downgrade`` recreates both tables with their exact pre-cutover schema (the squashed
001_initial baseline) so the migration is schema-reversible — it does NOT restore any
rows (the data is intentionally retired). ``bids`` is recreated first because
``bid_solicitations.parsed_bid_id`` carries a FK onto it.

Revision ID: 128_drop_bid_tables
Revises: 127_bid_back_schema
Create Date: 2026-06-23
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "129_drop_bid_tables"
down_revision: str | None = "128_bid_back_schema"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Drop bid_solicitations first — it FK-references bids via parsed_bid_id.
    op.drop_index("ix_bidsol_status", table_name="bid_solicitations", if_exists=True)
    op.drop_index("ix_bid_solicitations_line_item", table_name="bid_solicitations", if_exists=True)
    op.drop_index("ix_bid_solicitations_graph_msg", table_name="bid_solicitations", if_exists=True)
    op.drop_index("ix_bid_solicitations_contact", table_name="bid_solicitations", if_exists=True)
    op.drop_table("bid_solicitations", if_exists=True)

    op.drop_index("ix_bids_vendor_card", table_name="bids", if_exists=True)
    op.drop_index("ix_bids_status", table_name="bids", if_exists=True)
    op.drop_index("ix_bids_line_item", table_name="bids", if_exists=True)
    op.drop_index("ix_bids_company", table_name="bids", if_exists=True)
    op.drop_table("bids", if_exists=True)


def downgrade() -> None:
    # Recreate the pre-cutover schema (001_initial baseline). bids first so the
    # bid_solicitations.parsed_bid_id FK target exists. Structure-only — no data.
    op.create_table(
        "bids",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("excess_line_item_id", sa.Integer(), nullable=False),
        sa.Column("bidder_company_id", sa.Integer(), nullable=True),
        sa.Column("bidder_vendor_card_id", sa.Integer(), nullable=True),
        sa.Column("unit_price", sa.Numeric(precision=12, scale=4), nullable=False),
        sa.Column("quantity_wanted", sa.Integer(), nullable=False),
        sa.Column("lead_time_days", sa.Integer(), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=True),
        sa.Column("source", sa.String(length=20), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("created_by", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=True, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(), nullable=True, server_default=sa.text("now()")),
        sa.ForeignKeyConstraint(
            ["bidder_company_id"], ["companies.id"], name="bids_bidder_company_id_fkey", ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(
            ["bidder_vendor_card_id"], ["vendor_cards.id"], name="bids_bidder_vendor_card_id_fkey", ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"], name="bids_created_by_fkey", ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["excess_line_item_id"], ["excess_line_items.id"], name="bids_excess_line_item_id_fkey", ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_bids_company", "bids", ["bidder_company_id"], unique=False)
    op.create_index("ix_bids_line_item", "bids", ["excess_line_item_id"], unique=False)
    op.create_index("ix_bids_status", "bids", ["status"], unique=False)
    op.create_index("ix_bids_vendor_card", "bids", ["bidder_vendor_card_id"], unique=False)

    op.create_table(
        "bid_solicitations",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("excess_line_item_id", sa.Integer(), nullable=False),
        sa.Column("contact_id", sa.Integer(), nullable=False),
        sa.Column("sent_by", sa.Integer(), nullable=False),
        sa.Column("recipient_email", sa.String(length=255), nullable=True),
        sa.Column("recipient_name", sa.String(length=255), nullable=True),
        sa.Column("graph_message_id", sa.String(length=500), nullable=True),
        sa.Column("subject", sa.String(length=500), nullable=True),
        sa.Column("body_preview", sa.String(length=500), nullable=True),
        sa.Column("response_received_at", sa.DateTime(), nullable=True),
        sa.Column("parsed_bid_id", sa.Integer(), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=True),
        sa.Column("sent_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True, server_default=sa.text("now()")),
        sa.ForeignKeyConstraint(
            ["excess_line_item_id"],
            ["excess_line_items.id"],
            name="bid_solicitations_excess_line_item_id_fkey",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["parsed_bid_id"], ["bids.id"], name="bid_solicitations_parsed_bid_id_fkey", ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(["sent_by"], ["users.id"], name="bid_solicitations_sent_by_fkey", ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_bid_solicitations_contact", "bid_solicitations", ["contact_id"], unique=False)
    op.create_index("ix_bid_solicitations_graph_msg", "bid_solicitations", ["graph_message_id"], unique=False)
    op.create_index("ix_bid_solicitations_line_item", "bid_solicitations", ["excess_line_item_id"], unique=False)
    op.create_index("ix_bidsol_status", "bid_solicitations", ["status"], unique=False)
