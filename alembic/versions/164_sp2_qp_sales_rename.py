"""SP-2 remaining schema: qp_sales column/gate rename + sales_so_number retirement.

Second SP-2 migration, chained after 163 (which shipped quote_id-nullable on its own).
This revision accumulates the de-collision/SO# schema changes that deploy together when
the user-facing SP-2 work lands:
  - Op A (this task): rename users.can_approve_sales_orders -> can_approve_qp_sales.
  - Op B (Task 5): data update approval_requests.gate_type 'sales_order' -> 'qp_sales'.
  - Op C (Task 7): backfill quality_plans.sales_so_number onto buy_plans_v3.sales_order_number,
    then drop the column.

Downgrade is partially lossy (documented below). Never run a bare downgrade on a DB
that has SO-origin rows; roll back code and schema together.
NOT yet deployed — round-tripped in Task 11.

Revision ID: 164_sp2_qp_sales_rename
Revises: 163_sp2_sales_order_gate
"""

import sqlalchemy as sa

from alembic import op

revision = "164_sp2_qp_sales_rename"
down_revision = "163_sp2_sales_order_gate"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Op A — rename the QP Sales-section approver toggle column.
    op.alter_column("users", "can_approve_sales_orders", new_column_name="can_approve_qp_sales")
    # Op B — rewrite persisted QP-sales gate values (free String(50) column, no CHECK).
    op.execute("UPDATE approval_requests SET gate_type = 'qp_sales' WHERE gate_type = 'sales_order'")
    # Op C — retire quality_plans.sales_so_number (canonical SO# = buy_plans_v3.sales_order_number).
    # Step C1: length pre-check — BuyPlan.sales_order_number is String(100); abort if any QP
    # value would be silently truncated rather than copy corrupt data.
    bind = op.get_bind()
    over = bind.execute(sa.text("SELECT count(*) FROM quality_plans WHERE length(sales_so_number) > 100")).scalar()
    if over:
        raise RuntimeError(
            f"{over} quality_plans.sales_so_number values exceed 100 chars; "
            "widen BuyPlan.sales_order_number or clean the data first"
        )
    # Step C2: backfill — copy the QP SO# onto the buy plan where the buy plan's is blank.
    op.execute(
        "UPDATE buy_plans_v3 SET sales_order_number = q.sales_so_number "
        "FROM quality_plans q "
        "WHERE q.buy_plan_id = buy_plans_v3.id "
        "AND (buy_plans_v3.sales_order_number IS NULL OR buy_plans_v3.sales_order_number = '') "
        "AND q.sales_so_number IS NOT NULL"
    )
    # Step C3: drop the now-redundant column.
    op.drop_column("quality_plans", "sales_so_number")


def downgrade() -> None:
    # Op C reverse — re-add the dropped column (empty; per-QP values moved to buy plan,
    # not restored here — accepted data loss on downgrade).
    op.add_column("quality_plans", sa.Column("sales_so_number", sa.String(length=255), nullable=True))
    # Op B reverse — restore persisted gate values to the old string.
    op.execute("UPDATE approval_requests SET gate_type = 'sales_order' WHERE gate_type = 'qp_sales'")
    op.alter_column("users", "can_approve_qp_sales", new_column_name="can_approve_sales_orders")
