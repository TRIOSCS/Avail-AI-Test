"""Migrate vendor tag columns to JSONB and add sighting compound index.

Revision ID: 082
Revises: 8c22bd2f6837
Create Date: 2026-03-29
"""

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

from alembic import op

# revision identifiers, used by Alembic.
revision = "082"
down_revision = "8c22bd2f6837"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Convert brand_tags and commodity_tags from JSON to JSONB
    op.alter_column(
        "vendor_cards",
        "brand_tags",
        type_=JSONB,
        existing_type=sa.JSON(),
        existing_nullable=True,
        postgresql_using="brand_tags::jsonb",
    )
    op.alter_column(
        "vendor_cards",
        "commodity_tags",
        type_=JSONB,
        existing_type=sa.JSON(),
        existing_nullable=True,
        postgresql_using="commodity_tags::jsonb",
    )

    # Add GIN indexes on the JSONB tag columns for fast containment queries
    op.execute("CREATE INDEX IF NOT EXISTS ix_vendor_cards_brand_tags_gin ON vendor_cards USING gin (brand_tags)")
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_vendor_cards_commodity_tags_gin ON vendor_cards USING gin (commodity_tags)"
    )

    # Add compound index on sightings for MPN + vendor lookups
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_sightings_mpn_vendor_norm ON sightings (normalized_mpn, vendor_name_normalized)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_sightings_mpn_vendor_norm")
    op.execute("DROP INDEX IF EXISTS ix_vendor_cards_commodity_tags_gin")
    op.execute("DROP INDEX IF EXISTS ix_vendor_cards_brand_tags_gin")

    op.alter_column(
        "vendor_cards",
        "commodity_tags",
        type_=sa.JSON(),
        existing_type=JSONB,
        existing_nullable=True,
    )
    op.alter_column(
        "vendor_cards",
        "brand_tags",
        type_=sa.JSON(),
        existing_type=JSONB,
        existing_nullable=True,
    )
