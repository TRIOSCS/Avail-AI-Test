"""Add selected_for_quote and selected_at columns to offers table.

Supports quote candidate selection workflow where sales users pick
offers for quoting at the part level.

Revision ID: 067
Revises: 066
"""

from alembic import op
import sqlalchemy as sa

revision = "067"
down_revision = "066"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("offers", sa.Column("selected_for_quote", sa.Boolean(), nullable=False, server_default="false"))
    op.add_column("offers", sa.Column("selected_at", sa.DateTime(), nullable=True))


def downgrade():
    op.drop_column("offers", "selected_at")
    op.drop_column("offers", "selected_for_quote")
