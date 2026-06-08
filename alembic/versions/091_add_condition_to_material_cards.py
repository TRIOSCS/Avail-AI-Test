"""Add material_cards.condition (broker stock-condition facet).

New | Recertified | Refurbished | Used | Pulled | Unknown. Nullable, application-validated
(like lifecycle_status), indexed for faceting. Populated by a later source (offer/sighting
provenance); the filter shows only values that have data.

Revision ID: 091_add_condition_mc
Revises: 090_reseed_commodity_schemas
Create Date: 2026-06-08
"""

import sqlalchemy as sa

from alembic import op

revision = "091_add_condition_mc"
down_revision = "090_reseed_commodity_schemas"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("material_cards", sa.Column("condition", sa.String(20), nullable=True))
    op.create_index("ix_material_cards_condition", "material_cards", ["condition"])


def downgrade() -> None:
    op.drop_index("ix_material_cards_condition", table_name="material_cards")
    op.drop_column("material_cards", "condition")
