"""Add material_card_id FK to offers, sightings, requirements.

Revision ID: 010_add_material_card_fk
Revises: 009_prospect_accounts_discovery_batches
Create Date: 2026-02-25
"""

from alembic import op

revision = "010_add_material_card_fk"
down_revision = "009_prospect_accounts_discovery_batches"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Add nullable material_card_id to all three tables (backfill later, then set NOT NULL)
    op.execute("ALTER TABLE requirements ADD COLUMN IF NOT EXISTS material_card_id INTEGER")
    op.execute("ALTER TABLE sightings ADD COLUMN IF NOT EXISTS material_card_id INTEGER")
    op.execute("ALTER TABLE offers ADD COLUMN IF NOT EXISTS material_card_id INTEGER")

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
    op.create_index("ix_requirements_material_card", "requirements", ["material_card_id"], if_not_exists=True)
    op.create_index("ix_sightings_material_card", "sightings", ["material_card_id"], if_not_exists=True)
    op.create_index("ix_offers_material_card", "offers", ["material_card_id"], if_not_exists=True)


def downgrade() -> None:
    op.drop_constraint("fk_offers_material_card", "offers", type_="foreignkey")
    op.drop_constraint("fk_sightings_material_card", "sightings", type_="foreignkey")
    op.drop_constraint("fk_requirements_material_card", "requirements", type_="foreignkey")
    op.drop_index("ix_offers_material_card", "offers", if_exists=True)
    op.drop_index("ix_sightings_material_card", "sightings", if_exists=True)
    op.drop_index("ix_requirements_material_card", "requirements", if_exists=True)
    op.execute("ALTER TABLE IF EXISTS offers DROP COLUMN IF EXISTS material_card_id")
    op.execute("ALTER TABLE IF EXISTS sightings DROP COLUMN IF EXISTS material_card_id")
    op.execute("ALTER TABLE IF EXISTS requirements DROP COLUMN IF EXISTS material_card_id")
