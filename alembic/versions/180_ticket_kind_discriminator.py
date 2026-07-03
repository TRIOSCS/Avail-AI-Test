"""Trouble tickets: add the bug/feature kind discriminator.

What (DDL, reversible):
  - ADD trouble_tickets.ticket_type (String(20), NOT NULL, server_default 'bug').
    The server_default backfills every pre-existing row as a bug, so the existing
    Report-a-Problem path is unchanged; feature requests store 'feature'.
  - CREATE INDEX ix_trouble_tickets_ticket_type on (ticket_type) — the unified inbox
    filters tickets by kind.

Downgrade: fully reversible — drops the index then the column.

Called by: alembic (upgrade/downgrade).
Depends on: trouble_tickets (table exists since migration 043).

Revision ID: 180_ticket_kind_discriminator
Revises: 179_prepayment_lifecycle
Create Date: 2026-07-03
"""

import sqlalchemy as sa

from alembic import op

revision = "180_ticket_kind_discriminator"
down_revision = "179_prepayment_lifecycle"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "trouble_tickets",
        sa.Column("ticket_type", sa.String(length=20), nullable=False, server_default="bug"),
    )
    op.create_index("ix_trouble_tickets_ticket_type", "trouble_tickets", ["ticket_type"])


def downgrade() -> None:
    op.drop_index("ix_trouble_tickets_ticket_type", table_name="trouble_tickets")
    op.drop_column("trouble_tickets", "ticket_type")
