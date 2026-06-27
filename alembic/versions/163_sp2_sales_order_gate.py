"""SP-2 sales-order gate: quote_id nullable, qp_sales rename, drop sales_so_number.

Revision ID: 163_sp2_sales_order_gate
Revises: 162_resource_and_cancellations
"""

import sqlalchemy as sa

from alembic import op

revision = "163_sp2_sales_order_gate"
down_revision = "162_resource_and_cancellations"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Op 1 — BuyPlan.quote_id nullable (SO origination from offers, no quote).
    op.alter_column("buy_plans_v3", "quote_id", existing_type=sa.Integer(), nullable=True)
    # (ops 2–5 appended in Tasks 4, 5, 8)


def downgrade() -> None:
    # WARNING: re-asserting NOT NULL fails if any SO-origin (quote_id IS NULL) rows
    # exist; delete/backfill them before downgrading. Roll back code + schema together.
    op.alter_column("buy_plans_v3", "quote_id", existing_type=sa.Integer(), nullable=False)
