"""CustomerBid send/accept/reject lifecycle stamps.

What (DDL, reversible):
  - ADD customer_bids.sent_at (UTCDateTime, NULLABLE) — stamped when the clean
    bid-back PDF is emailed to the seller (draft -> sent).
  - ADD customer_bids.responded_at (UTCDateTime, NULLABLE) — when the trader logged
    the seller's answer (sent -> accepted/rejected).
  - ADD customer_bids.responded_by_id (Integer FK users.id ondelete SET NULL,
    NULLABLE) — WHO logged the seller's answer (the trader; the seller is not a User).

Powers the M4 bid-back lifecycle: draft -> sent -> accepted/rejected with a who/when
audit trail on the same CustomerBid row (re-assembling bumps ``revision`` and clears
these stamps — a new revision is a fresh draft). The ``status`` column + ``revision``
already exist (initial resell schema); this migration only adds the three stamps.

Downgrade: fully reversible — drops the FK then the three columns.

Called by: alembic (upgrade/downgrade).
Depends on: customer_bids + users (both exist since the resell schema).

Revision ID: 183_customer_bid_lifecycle
Revises: 182_drop_task_ai_columns
Create Date: 2026-07-04
"""

import sqlalchemy as sa

import app.database
from alembic import op

revision = "183_customer_bid_lifecycle"
down_revision = "182_drop_task_ai_columns"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("customer_bids", sa.Column("sent_at", app.database.UTCDateTime(), nullable=True))
    op.add_column("customer_bids", sa.Column("responded_at", app.database.UTCDateTime(), nullable=True))
    op.add_column("customer_bids", sa.Column("responded_by_id", sa.Integer(), nullable=True))
    op.create_foreign_key(
        "fk_customer_bids_responded_by",
        "customer_bids",
        "users",
        ["responded_by_id"],
        ["id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    op.drop_constraint("fk_customer_bids_responded_by", "customer_bids", type_="foreignkey")
    op.drop_column("customer_bids", "responded_by_id")
    op.drop_column("customer_bids", "responded_at")
    op.drop_column("customer_bids", "sent_at")
