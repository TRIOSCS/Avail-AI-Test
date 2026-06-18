"""Add contacts.sent_at for durable true send-time tracking.

What: adds contacts.sent_at (UTCDateTime, nullable) so the outbound clock
      advances at the moment sendMail succeeds rather than waiting up to 30 min
      for the scan_sent_folder job to find the message.
Downgrade: drops the column.

Revision ID: 114_contact_sent_at
Revises: 113_quote_source
Create Date: 2026-06-18
"""

import sqlalchemy as sa

from alembic import op

revision = "114_contact_sent_at"
down_revision = "113_quote_source"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("contacts", sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column("contacts", "sent_at")
