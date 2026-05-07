"""Add commodity_tags JSON column to users table.

Enables per-buyer commodity routing in Buy Plan V3 assignment.

Revision ID: 029_user_commodity_tags
Revises: 028_reactivation_signals
Create Date: 2026-02-27
"""

from alembic import op

revision = "029_user_commodity_tags"
down_revision = "028_reactivation_signals"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS commodity_tags JSON")


def downgrade():
    op.execute("ALTER TABLE users DROP COLUMN IF EXISTS commodity_tags")
