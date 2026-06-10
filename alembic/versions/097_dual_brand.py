"""Dual-brand filtering — add brand column + brand/manufacturer provenance.

What: Adds the dual-brand columns so a card can carry BOTH the OEM label (brand —
      IBM, Dell, Hewlett Packard Enterprise, Lenovo) and the actual maker
      (manufacturer — Seagate Technology, Kingston Technology, Hitachi/IBM):
        - material_cards.brand (String(255), nullable, indexed via ix_material_cards_brand)
        - brand_source / brand_confidence / brand_tier / brand_updated_at
        - manufacturer_source / manufacturer_confidence / manufacturer_tier /
          manufacturer_updated_at
      All nine columns are nullable. Purely additive DDL, NO data writes: pattern is
      migration 096 (category_*) verbatim — valued-but-unprovenanced manufacturer values
      rank at the legacy_backfill floor (tier 50, conf 0.5) at runtime inside
      spec_tiers.set_manufacturer/set_brand, so no in-migration backfill is needed.
      The data backfill is a separate, dry-run-first operator command
      (python -m app.management.backfill_dual_brand) run post-deploy.
Called by: alembic (upgrade/downgrade).
Depends on: 096_spec_provenance (current single head); material_cards table.

Revision ID: 097_dual_brand
Revises: 096_spec_provenance
Create Date: 2026-06-10
"""

import sqlalchemy as sa

from alembic import op

revision = "097_dual_brand"
down_revision = "096_spec_provenance"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("material_cards", sa.Column("brand", sa.String(length=255), nullable=True))
    op.add_column("material_cards", sa.Column("brand_source", sa.String(length=50), nullable=True))
    op.add_column("material_cards", sa.Column("brand_confidence", sa.Float(), nullable=True))
    op.add_column("material_cards", sa.Column("brand_tier", sa.Integer(), nullable=True))
    op.add_column("material_cards", sa.Column("brand_updated_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("material_cards", sa.Column("manufacturer_source", sa.String(length=50), nullable=True))
    op.add_column("material_cards", sa.Column("manufacturer_confidence", sa.Float(), nullable=True))
    op.add_column("material_cards", sa.Column("manufacturer_tier", sa.Integer(), nullable=True))
    op.add_column("material_cards", sa.Column("manufacturer_updated_at", sa.DateTime(timezone=True), nullable=True))
    op.create_index("ix_material_cards_brand", "material_cards", ["brand"])


def downgrade() -> None:
    # Additive columns — no data restore needed. Drop the index, then columns in reverse.
    op.drop_index("ix_material_cards_brand", table_name="material_cards")
    op.drop_column("material_cards", "manufacturer_updated_at")
    op.drop_column("material_cards", "manufacturer_tier")
    op.drop_column("material_cards", "manufacturer_confidence")
    op.drop_column("material_cards", "manufacturer_source")
    op.drop_column("material_cards", "brand_updated_at")
    op.drop_column("material_cards", "brand_tier")
    op.drop_column("material_cards", "brand_confidence")
    op.drop_column("material_cards", "brand_source")
    op.drop_column("material_cards", "brand")
