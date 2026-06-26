"""Re-source + PO-cancellation performance schema (migration 162).

Adds the immutable po_cancellations table (one append-only row per cancelled vendor
PO, powering vendor cancellation-frequency + days-to-cancel metrics) plus the metric
columns it feeds: vendor_cards.avg_days_to_cancel / slow_cancel_count and
vendor_metrics_snapshot.avg_days_to_cancel, and the users.notify_resource_alert_enabled
preference gating the urgent re-source broadcast's personal pushes.

All additive/reversible. Round-tripped (upgrade -> downgrade -> upgrade) on a throwaway
PG; staging untouched. Chains onto 161_qp_native_sections; single head verified via
`alembic heads`.

Revision ID: 162_resource_and_cancellations
Revises: 161_qp_native_sections
Create Date: 2026-06-26
"""

from __future__ import annotations

import sqlalchemy as sa

import app.database
from alembic import op

revision = "162_resource_and_cancellations"
down_revision = "161_qp_native_sections"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "po_cancellations",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("buy_plan_id", sa.Integer(), nullable=True),
        sa.Column("buy_plan_line_id", sa.Integer(), nullable=True),
        sa.Column("requirement_id", sa.Integer(), nullable=True),
        sa.Column("offer_id", sa.Integer(), nullable=True),
        sa.Column("vendor_card_id", sa.Integer(), nullable=True),
        sa.Column("vendor_name_normalized", sa.String(length=255), nullable=False),
        sa.Column("normalized_mpn", sa.String(length=255), nullable=False),
        sa.Column("po_number", sa.String(length=100), nullable=False),
        sa.Column("po_cut_at", app.database.UTCDateTime(), nullable=True),
        sa.Column("cancelled_at", app.database.UTCDateTime(), nullable=False),
        sa.Column("days_to_cancel", sa.Integer(), nullable=True),
        sa.Column("reason_code", sa.String(length=32), nullable=False),
        sa.Column("reason_text", sa.Text(), nullable=True),
        sa.Column("cancelled_by_id", sa.Integer(), nullable=True),
        sa.Column("created_at", app.database.UTCDateTime(), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["buy_plan_id"], ["buy_plans_v3.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["buy_plan_line_id"], ["buy_plan_lines.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["requirement_id"], ["requirements.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["offer_id"], ["offers.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["vendor_card_id"], ["vendor_cards.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["cancelled_by_id"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_po_cancel_vendor_card", "po_cancellations", ["vendor_card_id"], unique=False)
    op.create_index("ix_po_cancel_vendor_norm", "po_cancellations", ["vendor_name_normalized"], unique=False)
    op.create_index("ix_po_cancel_vendor_cut", "po_cancellations", ["vendor_card_id", "cancelled_at"], unique=False)
    op.create_index("ix_po_cancel_line", "po_cancellations", ["buy_plan_line_id"], unique=False)
    op.create_index("ix_po_cancel_requirement", "po_cancellations", ["requirement_id"], unique=False)
    op.create_index("ix_po_cancel_mpn", "po_cancellations", ["normalized_mpn"], unique=False)

    op.add_column("vendor_cards", sa.Column("avg_days_to_cancel", sa.Float(), nullable=True))
    op.add_column("vendor_cards", sa.Column("slow_cancel_count", sa.Integer(), nullable=True))
    op.add_column("vendor_metrics_snapshot", sa.Column("avg_days_to_cancel", sa.Float(), nullable=True))
    op.add_column(
        "users",
        sa.Column(
            "notify_resource_alert_enabled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("true"),
        ),
    )


def downgrade() -> None:
    op.drop_column("users", "notify_resource_alert_enabled")
    op.drop_column("vendor_metrics_snapshot", "avg_days_to_cancel")
    op.drop_column("vendor_cards", "slow_cancel_count")
    op.drop_column("vendor_cards", "avg_days_to_cancel")

    op.drop_index("ix_po_cancel_mpn", table_name="po_cancellations")
    op.drop_index("ix_po_cancel_requirement", table_name="po_cancellations")
    op.drop_index("ix_po_cancel_line", table_name="po_cancellations")
    op.drop_index("ix_po_cancel_vendor_cut", table_name="po_cancellations")
    op.drop_index("ix_po_cancel_vendor_norm", table_name="po_cancellations")
    op.drop_index("ix_po_cancel_vendor_card", table_name="po_cancellations")
    op.drop_table("po_cancellations")
