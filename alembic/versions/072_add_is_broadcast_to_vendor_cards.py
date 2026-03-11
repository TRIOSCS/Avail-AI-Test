"""Add is_broadcast flag to vendor_cards.

Broadcast vendors are always included in stock inquiry results
regardless of whether they have sightings for the queried MPN.

Revision ID: 072
Revises: 071
Create Date: 2026-03-11
"""

from alembic import op
import sqlalchemy as sa

revision = "072"
down_revision = "071"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "vendor_cards",
        sa.Column("is_broadcast", sa.Boolean(), server_default=sa.text("false"), nullable=True),
    )
    op.create_index("ix_vendor_cards_broadcast", "vendor_cards", ["is_broadcast"], postgresql_where=sa.text("is_broadcast = true"))


def downgrade():
    op.drop_index("ix_vendor_cards_broadcast", table_name="vendor_cards")
    op.drop_column("vendor_cards", "is_broadcast")
