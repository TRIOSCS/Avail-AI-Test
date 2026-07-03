"""QP lightweight fold (decision C): rename section approved-at → reviewed-at, add
reviewed-by FKs, drop dead top-level approved_by/approved_at.

What (DDL, reversible):
  - RENAME quality_plans.sales_section_approved_at → sales_section_reviewed_at
    and purchasing_section_approved_at → purchasing_section_reviewed_at (hand-written
    ALTER … RENAME COLUMN so existing timestamps survive — autogenerate would emit a
    drop+add and lose data).
  - ADD quality_plans.sales_section_reviewed_by_id / purchasing_section_reviewed_by_id
    (Integer FK users.id ON DELETE SET NULL, nullable) — who marked each section reviewed.
  - DROP quality_plans.approved_by_id / approved_at (truly dead: never written or
    rendered anywhere in the app; the top-level QP approve workflow is retired by
    decision C). Downgrade recreates both as nullable (always-NULL in every row today,
    so no data restore is needed).

Downgrade: fully reversible — drops the reviewed-by FKs/columns, renames the two
reviewed-at columns back to approved-at, and recreates approved_by_id (FK users.id
SET NULL) + approved_at exactly as migration 157 created them.

Called by: alembic (upgrade/downgrade).
Depends on: quality_plans (section timestamp columns from 161; approved_by_id/approved_at
            from 157), users (FK target).

Revision ID: 177_qp_section_reviewed_cols
Revises: 176_retire_deal_po_gate
Create Date: 2026-07-03
"""

import sqlalchemy as sa

from alembic import op

revision = "177_qp_section_reviewed_cols"
down_revision = "176_retire_deal_po_gate"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Rename the two section timestamp columns in place (preserve existing data).
    op.alter_column("quality_plans", "sales_section_approved_at", new_column_name="sales_section_reviewed_at")
    op.alter_column("quality_plans", "purchasing_section_approved_at", new_column_name="purchasing_section_reviewed_at")

    # Add the per-section reviewed-by FK columns.
    op.add_column("quality_plans", sa.Column("sales_section_reviewed_by_id", sa.Integer(), nullable=True))
    op.add_column("quality_plans", sa.Column("purchasing_section_reviewed_by_id", sa.Integer(), nullable=True))
    op.create_foreign_key(
        "fk_qp_sales_reviewed_by",
        "quality_plans",
        "users",
        ["sales_section_reviewed_by_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_foreign_key(
        "fk_qp_purchasing_reviewed_by",
        "quality_plans",
        "users",
        ["purchasing_section_reviewed_by_id"],
        ["id"],
        ondelete="SET NULL",
    )

    # Drop the dead top-level approver columns (PG drops their FK automatically).
    op.drop_column("quality_plans", "approved_by_id")
    op.drop_column("quality_plans", "approved_at")


def downgrade() -> None:
    # Recreate the dead top-level approver columns exactly as 157 created them.
    op.add_column("quality_plans", sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("quality_plans", sa.Column("approved_by_id", sa.Integer(), nullable=True))
    op.create_foreign_key(
        "fk_qp_approved_by",
        "quality_plans",
        "users",
        ["approved_by_id"],
        ["id"],
        ondelete="SET NULL",
    )

    # Drop the reviewed-by FK columns.
    op.drop_constraint("fk_qp_sales_reviewed_by", "quality_plans", type_="foreignkey")
    op.drop_constraint("fk_qp_purchasing_reviewed_by", "quality_plans", type_="foreignkey")
    op.drop_column("quality_plans", "sales_section_reviewed_by_id")
    op.drop_column("quality_plans", "purchasing_section_reviewed_by_id")

    # Rename the section timestamp columns back to their approved-at names.
    op.alter_column("quality_plans", "sales_section_reviewed_at", new_column_name="sales_section_approved_at")
    op.alter_column("quality_plans", "purchasing_section_reviewed_at", new_column_name="purchasing_section_approved_at")
