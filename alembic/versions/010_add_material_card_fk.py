"""Add material_card_id FK to offers, sightings, requirements.

Revision ID: 010_add_material_card_fk
Revises: 009_prospect_accounts_discovery_batches
Create Date: 2026-02-25
"""

import sqlalchemy as sa

from alembic import op

revision = "010_add_material_card_fk"
down_revision = "009_prospect_accounts_discovery_batches"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Add nullable material_card_id to all three tables (backfill later, then set NOT NULL)
    op.add_column("requirements", sa.Column("material_card_id", sa.Integer(), nullable=True))
    op.add_column("sightings", sa.Column("material_card_id", sa.Integer(), nullable=True))
    op.add_column("offers", sa.Column("material_card_id", sa.Integer(), nullable=True))

    # Foreign keys
    op.create_foreign_key(
        "fk_requirements_material_card",
        "requirements",
        "material_cards",
        ["material_card_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_foreign_key(
        "fk_sightings_material_card",
        "sightings",
        "material_cards",
        ["material_card_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_foreign_key(
        "fk_offers_material_card",
        "offers",
        "material_cards",
        ["material_card_id"],
        ["id"],
        ondelete="SET NULL",
    )

    # Indexes for FK lookups
    op.create_index("ix_requirements_material_card", "requirements", ["material_card_id"])
    op.create_index("ix_sightings_material_card", "sightings", ["material_card_id"])
    op.create_index("ix_offers_material_card", "offers", ["material_card_id"])


def downgrade() -> None:
    op.drop_constraint("fk_offers_material_card", "offers", type_="foreignkey")
    op.drop_constraint("fk_sightings_material_card", "sightings", type_="foreignkey")
    op.drop_constraint("fk_requirements_material_card", "requirements", type_="foreignkey")
    op.drop_index("ix_offers_material_card", "offers")
    op.drop_index("ix_sightings_material_card", "sightings")
    op.drop_index("ix_requirements_material_card", "requirements")
    op.drop_column("offers", "material_card_id")
    op.drop_column("sightings", "material_card_id")
    op.drop_column("requirements", "material_card_id")
