"""Add description column to quote_lines and excess_line_items.

Revision ID: 084_description
Revises: eabe89205d07
Create Date: 2026-03-30
"""

from alembic import op

revision = "084_description"
down_revision = "eabe89205d07"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE quote_lines ADD COLUMN IF NOT EXISTS description VARCHAR(500)")
    op.execute("ALTER TABLE excess_line_items ADD COLUMN IF NOT EXISTS description VARCHAR(500)")


def downgrade() -> None:
    op.execute("ALTER TABLE excess_line_items DROP COLUMN IF EXISTS description")
    op.execute("ALTER TABLE quote_lines DROP COLUMN IF EXISTS description")
