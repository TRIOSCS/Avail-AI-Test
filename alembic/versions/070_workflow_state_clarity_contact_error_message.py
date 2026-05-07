"""Workflow state clarity — contact error_message.

Add error_message column to contacts table for persisting RFQ send failures.

Revision ID: 070
Revises: 069
Create Date: 2026-03-10
"""

from alembic import op

revision = "070"
down_revision = "069"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE contacts ADD COLUMN IF NOT EXISTS error_message VARCHAR(500)")


def downgrade() -> None:
    op.execute("ALTER TABLE IF EXISTS contacts DROP COLUMN IF EXISTS error_message")
