"""Prepayment: link to the specific PO line (buy_plan_line_id).

What (DDL, reversible):
  - ADD prepayments.buy_plan_line_id (Integer FK buy_plan_lines.id ondelete SET NULL,
    nullable) — the specific PO line this prepayment is against. Nullable + SET NULL so a
    prepayment record outlives the line it prepaid (audit trail survives a line delete).
  - ADD fk_prepayment_buy_plan_line FK constraint + ix_prepayment_buy_plan_line index.

Downgrade: fully reversible — drops the index, the FK constraint, then the column.

Called by: alembic (upgrade/downgrade).
Depends on: prepayments (from 157_qp_approvals), buy_plan_lines (FK target).

Revision ID: 178_prepayment_line_link
Revises: 177_qp_section_reviewed_cols
Create Date: 2026-07-03
"""

import sqlalchemy as sa

from alembic import op

revision = "178_prepayment_line_link"
down_revision = "177_qp_section_reviewed_cols"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "prepayments",
        sa.Column("buy_plan_line_id", sa.Integer(), nullable=True),
    )
    op.create_foreign_key(
        "fk_prepayment_buy_plan_line",
        "prepayments",
        "buy_plan_lines",
        ["buy_plan_line_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index("ix_prepayment_buy_plan_line", "prepayments", ["buy_plan_line_id"])


def downgrade() -> None:
    op.drop_index("ix_prepayment_buy_plan_line", table_name="prepayments")
    op.drop_constraint("fk_prepayment_buy_plan_line", "prepayments", type_="foreignkey")
    op.drop_column("prepayments", "buy_plan_line_id")
