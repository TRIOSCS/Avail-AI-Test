"""Workflow state clarity — contact error_message.

Add error_message column to contacts table for persisting RFQ send failures.

Revision ID: 070
Revises: 069
Create Date: 2026-03-10
"""

import sqlalchemy as sa

from alembic import op

revision = "070"
down_revision = "069"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("contacts", sa.Column("error_message", sa.String(500), nullable=True))


def downgrade() -> None:
    op.drop_column("contacts", "error_message")
