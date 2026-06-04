"""Add specs_enriched_at to material_cards.

Marks that the spec-enrichment pass has run for a card so the pass is
idempotent: NULL means the spec pass has not yet run for that card.

Revision ID: 087_add_specs_enriched_at
Revises: 086_add_activity_digest
Create Date: 2026-06-04
"""

import sqlalchemy as sa

from alembic import op

revision = "087_add_specs_enriched_at"
down_revision = "086_add_activity_digest"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("material_cards", sa.Column("specs_enriched_at", sa.DateTime(timezone=True), nullable=True))
    op.create_index("ix_material_cards_specs_enriched_at", "material_cards", ["specs_enriched_at"])


def downgrade() -> None:
    op.drop_index("ix_material_cards_specs_enriched_at", table_name="material_cards")
    op.drop_column("material_cards", "specs_enriched_at")
