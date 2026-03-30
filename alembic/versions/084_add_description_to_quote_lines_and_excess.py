"""Add description column to quote_lines and excess_line_items.

Revision ID: 084_description
Revises: eabe89205d07
Create Date: 2026-03-30
"""

import sqlalchemy as sa

from alembic import op

revision = "084_description"
down_revision = "eabe89205d07"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("quote_lines", sa.Column("description", sa.String(500), nullable=True))
    op.add_column("excess_line_items", sa.Column("description", sa.String(500), nullable=True))


def downgrade() -> None:
    op.drop_column("excess_line_items", "description")
    op.drop_column("quote_lines", "description")
