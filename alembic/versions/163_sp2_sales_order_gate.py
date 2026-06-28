"""SP-2: make BuyPlan.quote_id nullable (Sales Order origination from RFQ offers).

First, independently-shippable increment of Approvals SP-2: relax buy_plans_v3.quote_id
so a buy plan can be originated directly from RFQ offers with no customer quote. The
remaining SP-2 schema changes (qp_sales gate/column rename, sales_so_number drop) ship in
their own later migrations — this revision is self-contained.

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


def downgrade() -> None:
    # WARNING: re-asserting NOT NULL fails if any SO-origin (quote_id IS NULL) rows
    # exist; delete/backfill them before downgrading. Roll back code + schema together.
    op.alter_column("buy_plans_v3", "quote_id", existing_type=sa.Integer(), nullable=False)
