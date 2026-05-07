"""Add 8x8 Work Analytics fields to users table.

Columns: eight_by_eight_extension (nullable), eight_by_eight_enabled (default False).

Revision ID: 052
Revises: 051
"""

import sqlalchemy as sa

from alembic import op

revision = "052"
down_revision = "051"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS eight_by_eight_extension VARCHAR(20)")
    op.add_column(
        "users", sa.Column("eight_by_eight_enabled", sa.Boolean(), nullable=False, server_default=sa.text("false"))
    )


def downgrade():
    op.execute("ALTER TABLE IF EXISTS users DROP COLUMN IF EXISTS eight_by_eight_enabled")
    op.execute("ALTER TABLE IF EXISTS users DROP COLUMN IF EXISTS eight_by_eight_extension")
