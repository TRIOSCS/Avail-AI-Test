"""Create Buy Plan V3 tables: buy_plans_v3, buy_plan_lines, verification_group_members.

New structured buy plan system with separate line items, dual approval tracks
(manager + ops SO verification), per-line buyer assignment and PO tracking.

Non-destructive: adds new tables only, does not modify existing buy_plans table.

Revision ID: 020_buy_plan_v3
Revises: 019_activity_req_channel
Create Date: 2026-02-27
"""

import sqlalchemy as sa
from alembic import op

revision = "020_buy_plan_v3"
down_revision = "019_activity_req_channel"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ── buy_plans_v3 ─────────────────────────────────────────────────
    op.create_table(
        "buy_plans_v3",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("quote_id", sa.Integer, sa.ForeignKey("quotes.id", ondelete="CASCADE"), nullable=False),
        sa.Column("requisition_id", sa.Integer, sa.ForeignKey("requisitions.id", ondelete="CASCADE"), nullable=False),
        sa.Column("sales_order_number", sa.String(100)),
        sa.Column("customer_po_number", sa.String(100)),
        sa.Column("status", sa.String(30), nullable=False, server_default="draft"),
        sa.Column("so_status", sa.String(30), nullable=False, server_default="pending"),
        sa.Column("total_cost", sa.Numeric(12, 2)),
        sa.Column("total_revenue", sa.Numeric(12, 2)),
        sa.Column("total_margin_pct", sa.Numeric(5, 2)),
        sa.Column("ai_summary", sa.Text),
        sa.Column("ai_flags", sa.JSON, server_default="[]"),
        sa.Column("auto_approved", sa.Boolean, server_default="false"),
        sa.Column("approved_by_id", sa.Integer, sa.ForeignKey("users.id")),
        sa.Column("approved_at", sa.DateTime),
        sa.Column("approval_notes", sa.Text),
        sa.Column("so_verified_by_id", sa.Integer, sa.ForeignKey("users.id")),
        sa.Column("so_verified_at", sa.DateTime),
        sa.Column("so_rejection_note", sa.Text),
        sa.Column("submitted_by_id", sa.Integer, sa.ForeignKey("users.id")),
        sa.Column("submitted_at", sa.DateTime),
        sa.Column("salesperson_notes", sa.Text),
        sa.Column("completed_at", sa.DateTime),
        sa.Column("case_report", sa.Text),
        sa.Column("cancelled_at", sa.DateTime),
        sa.Column("cancelled_by_id", sa.Integer, sa.ForeignKey("users.id")),
        sa.Column("cancellation_reason", sa.Text),
        sa.Column("halted_by_id", sa.Integer, sa.ForeignKey("users.id")),
        sa.Column("halted_at", sa.DateTime),
        sa.Column("is_stock_sale", sa.Boolean, server_default="false"),
        sa.Column("created_at", sa.DateTime, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime, server_default=sa.text("now()")),
    )
    op.create_index("ix_bpv3_status", "buy_plans_v3", ["status"])
    op.create_index("ix_bpv3_so_status", "buy_plans_v3", ["so_status"])
    op.create_index("ix_bpv3_quote", "buy_plans_v3", ["quote_id"])
    op.create_index("ix_bpv3_requisition", "buy_plans_v3", ["requisition_id"])
    op.create_index("ix_bpv3_submitted_by", "buy_plans_v3", ["submitted_by_id"])
    op.create_index("ix_bpv3_status_created", "buy_plans_v3", ["status", "created_at"])

    # ── buy_plan_lines ───────────────────────────────────────────────
    op.create_table(
        "buy_plan_lines",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("buy_plan_id", sa.Integer, sa.ForeignKey("buy_plans_v3.id", ondelete="CASCADE"), nullable=False),
        sa.Column("requirement_id", sa.Integer, sa.ForeignKey("requirements.id", ondelete="SET NULL")),
        sa.Column("offer_id", sa.Integer, sa.ForeignKey("offers.id", ondelete="SET NULL")),
        sa.Column("quantity", sa.Integer, nullable=False),
        sa.Column("unit_cost", sa.Numeric(12, 4)),
        sa.Column("unit_sell", sa.Numeric(12, 4)),
        sa.Column("margin_pct", sa.Numeric(5, 2)),
        sa.Column("ai_score", sa.Float),
        sa.Column("buyer_id", sa.Integer, sa.ForeignKey("users.id")),
        sa.Column("assignment_reason", sa.String(100)),
        sa.Column("status", sa.String(30), nullable=False, server_default="awaiting_po"),
        sa.Column("po_number", sa.String(100)),
        sa.Column("estimated_ship_date", sa.DateTime),
        sa.Column("po_confirmed_at", sa.DateTime),
        sa.Column("po_verified_by_id", sa.Integer, sa.ForeignKey("users.id")),
        sa.Column("po_verified_at", sa.DateTime),
        sa.Column("po_rejection_note", sa.Text),
        sa.Column("issue_type", sa.String(30)),
        sa.Column("issue_note", sa.Text),
        sa.Column("sales_note", sa.Text),
        sa.Column("manager_note", sa.Text),
        sa.Column("created_at", sa.DateTime, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime, server_default=sa.text("now()")),
    )
    op.create_index("ix_bpl_buy_plan", "buy_plan_lines", ["buy_plan_id"])
    op.create_index("ix_bpl_requirement", "buy_plan_lines", ["requirement_id"])
    op.create_index("ix_bpl_status", "buy_plan_lines", ["status"])
    op.create_index("ix_bpl_buyer", "buy_plan_lines", ["buyer_id"])
    op.create_index("ix_bpl_offer", "buy_plan_lines", ["offer_id"])
    op.create_index("ix_bpl_plan_requirement", "buy_plan_lines", ["buy_plan_id", "requirement_id"])

    # ── verification_group_members ───────────────────────────────────
    op.create_table(
        "verification_group_members",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("user_id", sa.Integer, sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False, unique=True),
        sa.Column("is_active", sa.Boolean, nullable=False, server_default="true"),
        sa.Column("added_at", sa.DateTime, server_default=sa.text("now()")),
    )
    op.create_index("ix_vgm_active", "verification_group_members", ["is_active"])


def downgrade() -> None:
    op.drop_table("buy_plan_lines")
    op.drop_table("buy_plans_v3")
    op.drop_table("verification_group_members")
