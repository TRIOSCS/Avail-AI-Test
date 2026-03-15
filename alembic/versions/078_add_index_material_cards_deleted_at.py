"""Add index on material_cards.deleted_at for soft-delete query performance.

Soft-delete queries (WHERE deleted_at IS NULL) run on every MaterialCard
lookup. Without an index this causes full table scans.

Revision ID: 078
Revises: 077
Create Date: 2026-03-15
"""

from alembic import op

revision = "078"
down_revision = "077"
branch_labels = None
depends_on = None


def upgrade():
    op.create_index("ix_material_cards_deleted_at", "material_cards", ["deleted_at"])


def downgrade():
    op.drop_index("ix_material_cards_deleted_at", table_name="material_cards")
