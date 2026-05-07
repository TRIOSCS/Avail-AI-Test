"""Add selected_for_quote and selected_at columns to offers table.

Supports quote candidate selection workflow where sales users pick
offers for quoting at the part level.

Revision ID: 067
Revises: 066
"""

from alembic import op

revision = "067"
down_revision = "066"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("ALTER TABLE offers ADD COLUMN IF NOT EXISTS selected_for_quote BOOLEAN NOT NULL DEFAULT 'false'")
    op.execute("ALTER TABLE offers ADD COLUMN IF NOT EXISTS selected_at TIMESTAMP WITHOUT TIME ZONE")


def downgrade():
    op.execute("ALTER TABLE offers DROP COLUMN IF EXISTS selected_at")
    op.execute("ALTER TABLE offers DROP COLUMN IF EXISTS selected_for_quote")
