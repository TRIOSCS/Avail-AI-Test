"""Add purchase_history_recorded_at idempotency column to buy_plans_v3.

What: adds ``buy_plans_v3.purchase_history_recorded_at`` nullable timestamptz column.
      Set once CPH has been recorded from this plan's lines (idempotency guard for
      the buy-plan→customer_part_history feed and its backfill).

Downgrade: drops the column (reversible).

Called by: alembic (upgrade/downgrade).
Depends on: buy_plans_v3 table.

Revision ID: bp_cph_recorded_at
Revises: 107_is_scratch_requisitions
Create Date: 2026-06-17
"""

import sqlalchemy as sa

from alembic import op

revision = "bp_cph_recorded_at"
down_revision = "107_is_scratch_requisitions"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("buy_plans_v3", sa.Column("purchase_history_recorded_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column("buy_plans_v3", "purchase_history_recorded_at")
