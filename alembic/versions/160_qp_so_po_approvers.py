"""QP Phase C2a: per-user Sales-Order and Purchase-Order approval toggles.

Adds two boolean approval-right columns to ``users`` so the QP Sales section
(SALES_ORDER gate) and Purchasing section (PURCHASE_ORDER gate) can route to
eligible approvers, exactly like the existing buy-plan / prepayment toggles:

  - can_approve_sales_orders  → User.can_approve_sales_orders (no amount limit)
  - can_approve_pos           → User.can_approve_pos          (no amount limit)

Both NOT NULL with a ``false`` server_default (existing rows default to "cannot
approve"), so the change is additive and fully reversible. Approvers are per-user
toggles, never hardwired names (the locked rule).

Called by: alembic. Depends on: 159_approval_subject_poly.

Revision ID: 160_qp_so_po_approvers
Revises: 159_approval_subject_poly
Create Date: 2026-06-26
"""

import sqlalchemy as sa

from alembic import op

revision = "160_qp_so_po_approvers"
down_revision = "159_approval_subject_poly"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column(
            "can_approve_sales_orders",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )
    op.add_column(
        "users",
        sa.Column(
            "can_approve_pos",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )


def downgrade() -> None:
    op.drop_column("users", "can_approve_pos")
    op.drop_column("users", "can_approve_sales_orders")
