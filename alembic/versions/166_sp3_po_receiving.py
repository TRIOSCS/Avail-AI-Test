"""SP-3: QP-purchasing de-collision + deal-level PO gate approver columns.

Three schema/data operations that deploy together with the SP-3 code (PO execution +
receiving):
  - Op A (data): rewrite persisted QP-purchasing gate values
    approval_requests.gate_type 'purchase_order' -> 'qp_purchasing' for QualityPlan
    subjects only (the deal-level PURCHASE_ORDER gate, subject_type='buy_plan', keeps
    its value). gate_type is a free String(50) with no CHECK, so a bare value rewrite is
    safe and reversible.
  - Op B (rename): users.can_approve_pos -> can_approve_qp_purchasing (the QP Purchasing
    section approver right).
  - Op C (add): users.can_approve_purchase_orders (Bool, server_default false) +
    users.purchase_order_approval_limit (Numeric(12,2), nullable=unlimited) — the new
    deal-level PO gate approver right + optional dollar limit (mirrors the prepayment gate).

The BuyPlanStatus.INBOUND value is a free String(30) column with no DB CHECK, so the new
receiving state needs no DDL. Additive/reversible; round-tripped on a throwaway PG.

Called by: alembic. Depends on: 165_vendor_is_active.

Revision ID: 166_sp3_po_receiving
Revises: 165_vendor_is_active
"""

import sqlalchemy as sa

from alembic import op

revision = "166_sp3_po_receiving"
down_revision = "165_vendor_is_active"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Op A — de-collide the QP-purchasing gate value from the deal-level PO gate. Only
    # QualityPlan-subject rows move; deal-level PO rows (subject_type='buy_plan') stay.
    op.execute(
        "UPDATE approval_requests SET gate_type = 'qp_purchasing' "
        "WHERE gate_type = 'purchase_order' AND subject_type = 'quality_plan'"
    )
    # Op B — rename the QP Purchasing-section approver toggle column.
    op.alter_column("users", "can_approve_pos", new_column_name="can_approve_qp_purchasing")
    # Op C — add the deal-level PO gate approver right + optional dollar limit.
    op.add_column(
        "users",
        sa.Column(
            "can_approve_purchase_orders",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )
    op.add_column(
        "users",
        sa.Column("purchase_order_approval_limit", sa.Numeric(12, 2), nullable=True),
    )


def downgrade() -> None:
    # Op C reverse — drop the deal-level PO approver columns.
    op.drop_column("users", "purchase_order_approval_limit")
    op.drop_column("users", "can_approve_purchase_orders")
    # Op B reverse — restore the QP Purchasing-section column name.
    op.alter_column("users", "can_approve_qp_purchasing", new_column_name="can_approve_pos")
    # Op A reverse — restore the QP-purchasing gate value to the old 'purchase_order' string.
    op.execute(
        "UPDATE approval_requests SET gate_type = 'purchase_order' "
        "WHERE gate_type = 'qp_purchasing' AND subject_type = 'quality_plan'"
    )
