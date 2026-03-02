"""Add is_internal_part flag to material_cards.

Revision ID: 047
Revises: 046
"""

from alembic import op
import sqlalchemy as sa

revision = "047"
down_revision = "046"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "material_cards",
        sa.Column("is_internal_part", sa.Boolean(), server_default="false", nullable=True),
    )
    op.create_index("ix_mc_internal_part", "material_cards", ["is_internal_part"])


def downgrade() -> None:
    op.drop_index("ix_mc_internal_part", table_name="material_cards")
    op.drop_column("material_cards", "is_internal_part")
