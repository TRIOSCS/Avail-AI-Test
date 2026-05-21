"""Add selected_for_quote and selected_at columns to offers table.

Supports quote candidate selection workflow where sales users pick
offers for quoting at the part level.

Revision ID: 067
Revises: 066
"""

import sqlalchemy as sa

from alembic import op

revision = "067"
down_revision = "066"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("offers", sa.Column("selected_for_quote", sa.Boolean(), nullable=False, server_default="false"))
    op.add_column("offers", sa.Column("selected_at", sa.DateTime(), nullable=True))


def downgrade():
    op.execute("ALTER TABLE IF EXISTS offers DROP COLUMN IF EXISTS selected_at")
    op.execute("ALTER TABLE IF EXISTS offers DROP COLUMN IF EXISTS selected_for_quote")
