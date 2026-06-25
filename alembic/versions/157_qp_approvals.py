"""157_qp_approvals — QP + Approvals Engine schema (migration 157).

ADDITIVE ONLY: CREATE tables + ADD columns. No drops in upgrade().
New tables: approval_gate_configs, prepayments, quality_plans,
            approval_requests, approval_events, approval_outbox,
            approval_steps, approval_step_recipients.
New columns on offers: is_primary, sourcing_type, vendor_rating,
                       terms, location, specifics.

Chains onto: 156_user_avatar
Claimed in:  MIGRATION_NUMBERS_IN_FLIGHT.txt (157 feat/qp-approvals-phase1)

Revision ID: 157_qp_approvals
Revises: 156_user_avatar
Create Date: 2026-06-25
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers
revision: str = "157_qp_approvals"
down_revision: Union[str, None] = "156_user_avatar"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Create 8 new tables and 6 new columns on offers.

    Additive only.
    """

    # ── approval_gate_configs ──────────────────────────────────────────────
    op.create_table(
        "approval_gate_configs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("gate_type", sa.String(50), nullable=False),
        sa.Column(
            "approver_user_id",
            sa.Integer(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("max_amount", sa.Numeric(12, 2), nullable=True),
        sa.Column("active", sa.Boolean(), nullable=False, server_default="true"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_approval_gate_cfg_type", "approval_gate_configs", ["gate_type"])
    op.create_index("ix_approval_gate_cfg_approver", "approval_gate_configs", ["approver_user_id"])
    op.create_index("ix_approval_gate_cfg_active", "approval_gate_configs", ["active"])

    # ── prepayments ────────────────────────────────────────────────────────
    op.create_table(
        "prepayments",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "vendor_card_id",
            sa.Integer(),
            sa.ForeignKey("vendor_cards.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "buy_plan_id",
            sa.Integer(),
            sa.ForeignKey("buy_plans_v3.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("total_incl_fees", sa.Numeric(12, 2), nullable=False),
        sa.Column("currency", sa.String(10), nullable=False, server_default="USD"),
        sa.Column("payment_method", sa.String(20), nullable=True),
        sa.Column("test_report_sent", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("buyer_remarks", sa.Text(), nullable=True),
        sa.Column(
            "created_by_id",
            sa.Integer(),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_prepayment_vendor_card", "prepayments", ["vendor_card_id"])
    op.create_index("ix_prepayment_buy_plan", "prepayments", ["buy_plan_id"])
    op.create_index("ix_prepayment_created_by", "prepayments", ["created_by_id"])

    # ── quality_plans ──────────────────────────────────────────────────────
    op.create_table(
        "quality_plans",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "buy_plan_id",
            sa.Integer(),
            sa.ForeignKey("buy_plans_v3.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "vendor_card_id",
            sa.Integer(),
            sa.ForeignKey("vendor_cards.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("status", sa.String(50), nullable=False, server_default="draft"),
        sa.Column("order_type", sa.String(20), nullable=False, server_default="new"),
        sa.Column("inspection_level", sa.String(50), nullable=True),
        sa.Column("sampling_rate", sa.String(50), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column(
            "created_by_id",
            sa.Integer(),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "approved_by_id",
            sa.Integer(),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_qp_buy_plan", "quality_plans", ["buy_plan_id"])
    op.create_index("ix_qp_vendor_card", "quality_plans", ["vendor_card_id"])
    op.create_index("ix_qp_status", "quality_plans", ["status"])
    op.create_index("ix_qp_created_by", "quality_plans", ["created_by_id"])

    # ── approval_requests ──────────────────────────────────────────────────
    op.create_table(
        "approval_requests",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("gate_type", sa.String(50), nullable=False),
        sa.Column("status", sa.String(50), nullable=False, server_default="requested"),
        sa.Column("amount", sa.Numeric(12, 2), nullable=True),
        sa.Column("currency", sa.String(10), nullable=True, server_default="USD"),
        sa.Column(
            "requested_by_id",
            sa.Integer(),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "owner_id",
            sa.Integer(),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "subject_quality_plan_id",
            sa.Integer(),
            sa.ForeignKey("quality_plans.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "subject_prepayment_id",
            sa.Integer(),
            sa.ForeignKey("prepayments.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("resolution_note", sa.Text(), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_approval_req_owner", "approval_requests", ["owner_id"])
    op.create_index("ix_approval_req_status", "approval_requests", ["status"])
    op.create_index("ix_approval_req_gate_type", "approval_requests", ["gate_type"])
    op.create_index("ix_approval_req_subject_qp", "approval_requests", ["subject_quality_plan_id"])
    op.create_index("ix_approval_req_subject_pp", "approval_requests", ["subject_prepayment_id"])

    # ── approval_events ────────────────────────────────────────────────────
    op.create_table(
        "approval_events",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "request_id",
            sa.Integer(),
            sa.ForeignKey("approval_requests.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "actor_id",
            sa.Integer(),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("event_type", sa.String(50), nullable=False),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("payload", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_approval_event_request", "approval_events", ["request_id"])
    op.create_index("ix_approval_event_actor", "approval_events", ["actor_id"])
    op.create_index("ix_approval_event_type", "approval_events", ["event_type"])

    # ── approval_outbox ────────────────────────────────────────────────────
    op.create_table(
        "approval_outbox",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "request_id",
            sa.Integer(),
            sa.ForeignKey("approval_requests.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "recipient_user_id",
            sa.Integer(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("channel", sa.String(50), nullable=False, server_default="email"),
        sa.Column("payload", sa.JSON(), nullable=True),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("fail_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_approval_outbox_request", "approval_outbox", ["request_id"])
    op.create_index("ix_approval_outbox_recipient", "approval_outbox", ["recipient_user_id"])
    op.create_index("ix_approval_outbox_sent", "approval_outbox", ["sent_at"])

    # ── approval_steps ─────────────────────────────────────────────────────
    op.create_table(
        "approval_steps",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "request_id",
            sa.Integer(),
            sa.ForeignKey("approval_requests.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("seq", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("rule", sa.String(20), nullable=False, server_default="any"),
        sa.Column("status", sa.String(50), nullable=False, server_default="pending"),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_approval_step_request", "approval_steps", ["request_id"])
    op.create_index("ix_approval_step_status", "approval_steps", ["status"])

    # ── approval_step_recipients ───────────────────────────────────────────
    op.create_table(
        "approval_step_recipients",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "step_id",
            sa.Integer(),
            sa.ForeignKey("approval_steps.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "user_id",
            sa.Integer(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("status", sa.String(50), nullable=False, server_default="pending"),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("decision_note", sa.Text(), nullable=True),
        sa.Column(
            "reassigned_to_id",
            sa.Integer(),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("step_id", "user_id", name="uq_approval_step_recipient"),
    )
    op.create_index("ix_approval_recip_step", "approval_step_recipients", ["step_id"])
    op.create_index("ix_approval_recip_user", "approval_step_recipients", ["user_id"])
    op.create_index("ix_approval_recip_status", "approval_step_recipients", ["status"])

    # ── offers: 6 new nullable columns ────────────────────────────────────
    op.add_column(
        "offers",
        sa.Column(
            "is_primary",
            sa.Boolean(),
            nullable=True,
            server_default="false",
        ),
    )
    op.add_column("offers", sa.Column("sourcing_type", sa.String(50), nullable=True))
    op.add_column("offers", sa.Column("vendor_rating", sa.Numeric(3, 1), nullable=True))
    op.add_column("offers", sa.Column("terms", sa.JSON(), nullable=True))
    op.add_column("offers", sa.Column("location", sa.String(255), nullable=True))
    op.add_column("offers", sa.Column("specifics", sa.Text(), nullable=True))


def downgrade() -> None:
    """Symmetric reversal: drop 6 offer columns then drop all 8 new tables."""

    # Offer columns (reverse add order)
    op.drop_column("offers", "specifics")
    op.drop_column("offers", "location")
    op.drop_column("offers", "terms")
    op.drop_column("offers", "vendor_rating")
    op.drop_column("offers", "sourcing_type")
    op.drop_column("offers", "is_primary")

    # Drop tables in reverse dependency order
    op.drop_index("ix_approval_recip_status", table_name="approval_step_recipients")
    op.drop_index("ix_approval_recip_user", table_name="approval_step_recipients")
    op.drop_index("ix_approval_recip_step", table_name="approval_step_recipients")
    op.drop_table("approval_step_recipients")

    op.drop_index("ix_approval_step_status", table_name="approval_steps")
    op.drop_index("ix_approval_step_request", table_name="approval_steps")
    op.drop_table("approval_steps")

    op.drop_index("ix_approval_outbox_sent", table_name="approval_outbox")
    op.drop_index("ix_approval_outbox_recipient", table_name="approval_outbox")
    op.drop_index("ix_approval_outbox_request", table_name="approval_outbox")
    op.drop_table("approval_outbox")

    op.drop_index("ix_approval_event_type", table_name="approval_events")
    op.drop_index("ix_approval_event_actor", table_name="approval_events")
    op.drop_index("ix_approval_event_request", table_name="approval_events")
    op.drop_table("approval_events")

    op.drop_index("ix_approval_req_subject_pp", table_name="approval_requests")
    op.drop_index("ix_approval_req_subject_qp", table_name="approval_requests")
    op.drop_index("ix_approval_req_gate_type", table_name="approval_requests")
    op.drop_index("ix_approval_req_status", table_name="approval_requests")
    op.drop_index("ix_approval_req_owner", table_name="approval_requests")
    op.drop_table("approval_requests")

    op.drop_index("ix_qp_created_by", table_name="quality_plans")
    op.drop_index("ix_qp_status", table_name="quality_plans")
    op.drop_index("ix_qp_vendor_card", table_name="quality_plans")
    op.drop_index("ix_qp_buy_plan", table_name="quality_plans")
    op.drop_table("quality_plans")

    op.drop_index("ix_prepayment_created_by", table_name="prepayments")
    op.drop_index("ix_prepayment_buy_plan", table_name="prepayments")
    op.drop_index("ix_prepayment_vendor_card", table_name="prepayments")
    op.drop_table("prepayments")

    op.drop_index("ix_approval_gate_cfg_active", table_name="approval_gate_configs")
    op.drop_index("ix_approval_gate_cfg_approver", table_name="approval_gate_configs")
    op.drop_index("ix_approval_gate_cfg_type", table_name="approval_gate_configs")
    op.drop_table("approval_gate_configs")
