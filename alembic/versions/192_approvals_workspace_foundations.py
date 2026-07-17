"""Approvals Workspace foundations — order type, payment method, audit FKs, attachments.

What (DDL, additive, reversible):
  - buy_plans_v3.order_type (String(20) NOT NULL server_default 'new') + ix_bpv3_order_type;
    data backfill sets order_type='stock_sale' where is_stock_sale is true (SalesOrderType
    vocabulary — see app/constants.py).
  - buy_plan_lines.payment_method (String(20) NULL — PaymentMethod vocabulary incl. the
    new ACH/COD members), received_at (timestamptz NULL) and received_by_id (Integer FK
    users.id ondelete SET NULL, NULL) for the manual mark-received event.
  - activity_log.buy_plan_line_id (FK buy_plan_lines.id ondelete SET NULL) +
    ix_activity_buy_plan_line, activity_log.prepayment_id (FK prepayments.id ondelete
    SET NULL) + ix_activity_prepayment — per-line / per-prepayment notes threads and
    field-diff audit rows key on these; SET NULL so the timeline outlives its subject.
  - quality_plans: seven nullable PURCHASING AS9120B columns
    (purchasing_traceability_verified Bool, purchasing_counterfeit_risk String(50),
    purchasing_risk_level String(50), purchasing_coc_available Bool,
    purchasing_vendor_rating String(255), purchasing_sn_previously_received Bool,
    purchasing_serial_numbers Text).
  - NEW buy_plan_attachments table (mirrors company_attachments): three nullable subject
    FKs (buy_plan_id / buy_plan_line_id / prepayment_id, all ondelete CASCADE —
    exactly-one-set is app-validated, no DB CHECK), OneDrive/SharePoint library fields,
    uploaded_by_id (FK users.id ondelete SET NULL), created_at; indexed on each subject FK.

Downgrade: fully reversible — drops the table, then every added index/FK/column in
reverse order. NOTE: dropping order_type / payment_method / received_* / the AS9120B
columns loses their data (additive columns, no pre-existing consumers).

Called by: alembic (upgrade/downgrade).
Depends on: buy_plans_v3, buy_plan_lines, activity_log, quality_plans, prepayments, users.

Revision ID: 192_approvals_workspace_foundations
Revises: 191_companies_account_type_index
Create Date: 2026-07-17
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy import text

from alembic import op

revision: str = "192_approvals_workspace_foundations"
down_revision: str | None = "191_companies_account_type_index"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # ── buy_plans_v3.order_type ──────────────────────────────────────────
    op.add_column(
        "buy_plans_v3",
        sa.Column("order_type", sa.String(length=20), nullable=False, server_default="new"),
    )
    op.create_index("ix_bpv3_order_type", "buy_plans_v3", ["order_type"], if_not_exists=True)
    # Data backfill: pre-existing stock sales (vendor-name detected boolean) become the
    # explicit 'stock_sale' order type; everything else stays at the 'new' default.
    op.get_bind().execute(text("UPDATE buy_plans_v3 SET order_type = 'stock_sale' WHERE is_stock_sale IS true"))

    # ── buy_plan_lines: payment_method + receiving stamps ────────────────
    op.add_column("buy_plan_lines", sa.Column("payment_method", sa.String(length=20), nullable=True))
    op.add_column("buy_plan_lines", sa.Column("received_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("buy_plan_lines", sa.Column("received_by_id", sa.Integer(), nullable=True))
    op.create_foreign_key(
        "fk_bpl_received_by",
        "buy_plan_lines",
        "users",
        ["received_by_id"],
        ["id"],
        ondelete="SET NULL",
    )

    # ── activity_log: per-line / per-prepayment audit FKs ────────────────
    op.add_column("activity_log", sa.Column("buy_plan_line_id", sa.Integer(), nullable=True))
    op.create_foreign_key(
        "fk_activity_buy_plan_line",
        "activity_log",
        "buy_plan_lines",
        ["buy_plan_line_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index(
        "ix_activity_buy_plan_line",
        "activity_log",
        ["buy_plan_line_id", "created_at"],
        postgresql_where=sa.text("buy_plan_line_id IS NOT NULL"),
        if_not_exists=True,
    )
    op.add_column("activity_log", sa.Column("prepayment_id", sa.Integer(), nullable=True))
    op.create_foreign_key(
        "fk_activity_prepayment",
        "activity_log",
        "prepayments",
        ["prepayment_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index(
        "ix_activity_prepayment",
        "activity_log",
        ["prepayment_id", "created_at"],
        postgresql_where=sa.text("prepayment_id IS NOT NULL"),
        if_not_exists=True,
    )

    # ── quality_plans: PURCHASING AS9120B columns ────────────────────────
    op.add_column("quality_plans", sa.Column("purchasing_traceability_verified", sa.Boolean(), nullable=True))
    op.add_column("quality_plans", sa.Column("purchasing_counterfeit_risk", sa.String(length=50), nullable=True))
    op.add_column("quality_plans", sa.Column("purchasing_risk_level", sa.String(length=50), nullable=True))
    op.add_column("quality_plans", sa.Column("purchasing_coc_available", sa.Boolean(), nullable=True))
    op.add_column("quality_plans", sa.Column("purchasing_vendor_rating", sa.String(length=255), nullable=True))
    op.add_column("quality_plans", sa.Column("purchasing_sn_previously_received", sa.Boolean(), nullable=True))
    op.add_column("quality_plans", sa.Column("purchasing_serial_numbers", sa.Text(), nullable=True))

    # ── buy_plan_attachments ─────────────────────────────────────────────
    op.create_table(
        "buy_plan_attachments",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("buy_plan_id", sa.Integer(), nullable=True),
        sa.Column("buy_plan_line_id", sa.Integer(), nullable=True),
        sa.Column("prepayment_id", sa.Integer(), nullable=True),
        sa.Column("file_name", sa.String(length=500), nullable=False),
        sa.Column("library_item_id", sa.String(length=500), nullable=True),
        sa.Column("library_drive_id", sa.String(length=200), nullable=True),
        sa.Column("library_web_url", sa.Text(), nullable=True),
        sa.Column("thumbnail_url", sa.Text(), nullable=True),
        sa.Column("content_type", sa.String(length=100), nullable=True),
        sa.Column("size_bytes", sa.Integer(), nullable=True),
        sa.Column("uploaded_by_id", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["buy_plan_id"], ["buy_plans_v3.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["buy_plan_line_id"], ["buy_plan_lines.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["prepayment_id"], ["prepayments.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["uploaded_by_id"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        if_not_exists=True,
    )
    op.create_index("ix_bp_attachments_plan", "buy_plan_attachments", ["buy_plan_id"], if_not_exists=True)
    op.create_index("ix_bp_attachments_line", "buy_plan_attachments", ["buy_plan_line_id"], if_not_exists=True)
    op.create_index("ix_bp_attachments_prepayment", "buy_plan_attachments", ["prepayment_id"], if_not_exists=True)


def downgrade() -> None:
    # buy_plan_attachments (indexes then table)
    op.drop_index("ix_bp_attachments_prepayment", table_name="buy_plan_attachments", if_exists=True)
    op.drop_index("ix_bp_attachments_line", table_name="buy_plan_attachments", if_exists=True)
    op.drop_index("ix_bp_attachments_plan", table_name="buy_plan_attachments", if_exists=True)
    op.drop_table("buy_plan_attachments", if_exists=True)

    # quality_plans AS9120B columns (data loss: additive audit answers)
    op.drop_column("quality_plans", "purchasing_serial_numbers")
    op.drop_column("quality_plans", "purchasing_sn_previously_received")
    op.drop_column("quality_plans", "purchasing_vendor_rating")
    op.drop_column("quality_plans", "purchasing_coc_available")
    op.drop_column("quality_plans", "purchasing_risk_level")
    op.drop_column("quality_plans", "purchasing_counterfeit_risk")
    op.drop_column("quality_plans", "purchasing_traceability_verified")

    # activity_log audit FKs
    op.drop_index("ix_activity_prepayment", table_name="activity_log", if_exists=True)
    op.drop_constraint("fk_activity_prepayment", "activity_log", type_="foreignkey")
    op.drop_column("activity_log", "prepayment_id")
    op.drop_index("ix_activity_buy_plan_line", table_name="activity_log", if_exists=True)
    op.drop_constraint("fk_activity_buy_plan_line", "activity_log", type_="foreignkey")
    op.drop_column("activity_log", "buy_plan_line_id")

    # buy_plan_lines receiving + payment method (data loss: additive stamps)
    op.drop_constraint("fk_bpl_received_by", "buy_plan_lines", type_="foreignkey")
    op.drop_column("buy_plan_lines", "received_by_id")
    op.drop_column("buy_plan_lines", "received_at")
    op.drop_column("buy_plan_lines", "payment_method")

    # buy_plans_v3.order_type (data loss: backfilled/user-chosen order types)
    op.drop_index("ix_bpv3_order_type", table_name="buy_plans_v3", if_exists=True)
    op.drop_column("buy_plans_v3", "order_type")
