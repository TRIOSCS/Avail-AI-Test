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
    op.add_column("users", sa.Column("eight_by_eight_extension", sa.String(20), nullable=True))
    op.add_column(
        "users", sa.Column("eight_by_eight_enabled", sa.Boolean(), nullable=False, server_default=sa.text("false"))
    )


def downgrade():
    op.drop_column("users", "eight_by_eight_enabled")
    op.drop_column("users", "eight_by_eight_extension")
