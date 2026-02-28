"""Graph Intelligence — OOO detection + mailbox timezone columns.

Revision ID: 033_graph_intelligence
Revises: 032_email_intelligence
Create Date: 2026-02-28

Adds:
  - VendorContact.is_ooo, ooo_return_date for OOO suppression
  - User.timezone, working_hours_start/end from mailbox settings
"""

revision = "033_graph_intelligence"
down_revision = "032_email_intelligence"

from alembic import op
import sqlalchemy as sa


def upgrade() -> None:
    # VendorContact OOO columns
    op.add_column("vendor_contacts", sa.Column("is_ooo", sa.Boolean, default=False))
    op.add_column("vendor_contacts", sa.Column("ooo_return_date", sa.DateTime))

    # User mailbox settings
    op.add_column("users", sa.Column("timezone", sa.String(100)))
    op.add_column("users", sa.Column("working_hours_start", sa.String(10)))
    op.add_column("users", sa.Column("working_hours_end", sa.String(10)))


def downgrade() -> None:
    op.drop_column("users", "working_hours_end")
    op.drop_column("users", "working_hours_start")
    op.drop_column("users", "timezone")
    op.drop_column("vendor_contacts", "ooo_return_date")
    op.drop_column("vendor_contacts", "is_ooo")
