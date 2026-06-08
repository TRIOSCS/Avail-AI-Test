"""Add material_cards.condition (broker stock-condition facet).

New | Recertified | Refurbished | Used | Pulled | Unknown. Nullable, application-validated
(like lifecycle_status), indexed for faceting. Populated by a later source (offer/sighting
provenance); the Condition facet shows only values that have data.

(Commodity-spec-schema seed changes are reconciled at startup via reseed_changed_schemas,
not via a migration — see app/startup.py — so this is the only schema change in the rework.)

Revision ID: 090_add_condition_mc
Revises: 089_oem_enrichment_columns
Create Date: 2026-06-08
"""

import sqlalchemy as sa

from alembic import op

revision = "090_add_condition_mc"
down_revision = "089_oem_enrichment_columns"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("material_cards", sa.Column("condition", sa.String(20), nullable=True))
    op.create_index("ix_material_cards_condition", "material_cards", ["condition"])


def downgrade() -> None:
    op.drop_index("ix_material_cards_condition", table_name="material_cards")
    op.drop_column("material_cards", "condition")
