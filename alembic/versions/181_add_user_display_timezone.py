"""Per-user display timezone: add users.display_timezone.

What (DDL, reversible):
  - ADD users.display_timezone (String(64), NULLABLE). Holds the IANA zone name
    (e.g. 'America/New_York', 'Asia/Tokyo') used to render UTC timestamps in the
    viewer's own timezone. Auto-detected from the browser
    (Intl.DateTimeFormat().resolvedOptions().timeZone) and overridable in the
    profile page. NULL means "not yet known" -> the app falls back to the business
    default (America/New_York).

Distinct from the pre-existing users.timezone column, which stores the Microsoft
Graph mailbox timezone (Windows format, e.g. 'Pacific Standard Time') used for RFQ
send-window scheduling and is NOT a valid IANA name. Overloading that column would
corrupt it on the next Graph mailbox sync, so this is a separate column.

Downgrade: fully reversible — drops the column.

Called by: alembic (upgrade/downgrade).
Depends on: users (table exists since the initial schema).

Revision ID: 181_add_user_display_timezone
Revises: 180_ticket_kind_discriminator
Create Date: 2026-07-04
"""

import sqlalchemy as sa

from alembic import op

revision = "181_add_user_display_timezone"
down_revision = "180_ticket_kind_discriminator"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column("display_timezone", sa.String(length=64), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("users", "display_timezone")
