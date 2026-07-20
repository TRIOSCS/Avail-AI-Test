"""Resell D6: drop the dead ``excess_offers.valid_until`` column.

What (DDL, reversible):
  - DROP COLUMN excess_offers.valid_until — a leftover from the pre-Resell ``Bid`` model
    that the ExcessOffer path never reads or writes (offers are collected, rolled up to a
    best-per-unit price, and awarded; there is no offer-expiry clock — that lives on the
    outreach posting window, not the offer). The model column, the unused
    submit_offer ``valid_until`` param, and the unused ExcessOfferCreate/ExcessOfferResponse
    schema fields are removed in the SAME change so the fresh-DB schema-drift gate stays
    green (a lingering model column with no table column emits a remove_column diff).

Why: dead-column disposition (D6). Staging has 0 non-null ``valid_until`` rows, so the drop
is a no-data-loss cleanup.

Downgrade: re-adds the nullable column (``DateTime(timezone=True)``, matching the original
127 definition). Additive-reverse — there is no data to restore (the column was always dead).

Called by: alembic (upgrade/downgrade).
Depends on: excess_offers (127 trading additive schema / 128 cutover).

Revision ID: 201_drop_offer_valid_until
Revises: 200_resell_hot_indexes
Create Date: 2026-07-20
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "201_drop_offer_valid_until"
down_revision = "200_resell_hot_indexes"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_column("excess_offers", "valid_until")


def downgrade() -> None:
    op.add_column(
        "excess_offers",
        sa.Column("valid_until", sa.DateTime(timezone=True), nullable=True),
    )
