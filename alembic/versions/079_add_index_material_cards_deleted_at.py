"""Add index on material_cards.deleted_at for soft-delete query performance.

Soft-delete queries (WHERE deleted_at IS NULL) run on every MaterialCard
lookup. Without an index this causes full table scans.

Revision ID: 079
Revises: 078_add_company_id
Create Date: 2026-03-15
"""

from alembic import op

revision = "079"
down_revision = "078_add_company_id"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
        CREATE INDEX IF NOT EXISTS ix_material_cards_deleted_at
        ON material_cards (deleted_at)
    """)


def downgrade():
    op.drop_index("ix_material_cards_deleted_at", table_name="material_cards")
