"""Add is_internal_part flag to material_cards.

Revision ID: 047
Revises: 046_fix_threshold_entity_types
"""

import sqlalchemy as sa

from alembic import op

revision = "047"
down_revision = "046_fix_threshold_entity_types"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "material_cards",
        sa.Column("is_internal_part", sa.Boolean(), server_default="false", nullable=True),
    )
    op.create_index("ix_mc_internal_part", "material_cards", ["is_internal_part"], if_not_exists=True)


def downgrade() -> None:
    op.drop_index("ix_mc_internal_part", table_name="material_cards", if_exists=True)
    op.execute("ALTER TABLE material_cards DROP COLUMN IF EXISTS is_internal_part")
