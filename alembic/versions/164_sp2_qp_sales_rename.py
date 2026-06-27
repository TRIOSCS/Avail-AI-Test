"""SP-2 remaining schema: qp_sales column/gate rename + sales_so_number retirement.

Second SP-2 migration, chained after 163 (which shipped quote_id-nullable on its own).
This revision accumulates the de-collision/SO# schema changes that deploy together when
the user-facing SP-2 work lands:
  - Op A (this task): rename users.can_approve_sales_orders -> can_approve_qp_sales.
  - Op B (Task 5): data update approval_requests.gate_type 'sales_order' -> 'qp_sales'.
  - Op C (Task 7): backfill quality_plans.sales_so_number onto buy_plans_v3.sales_order_number,
    then drop the column.
NOT yet deployed — later tasks append their ops here, then it is round-tripped (Task 11).

Revision ID: 164_sp2_qp_sales_rename
Revises: 163_sp2_sales_order_gate
"""

from alembic import op

revision = "164_sp2_qp_sales_rename"
down_revision = "163_sp2_sales_order_gate"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Op A — rename the QP Sales-section approver toggle column.
    op.alter_column("users", "can_approve_sales_orders", new_column_name="can_approve_qp_sales")


def downgrade() -> None:
    op.alter_column("users", "can_approve_qp_sales", new_column_name="can_approve_sales_orders")
