"""Create sourcing lead, evidence, and feedback tables.

Adds canonical lead storage for sourcing with one lead per vendor+part in a
requirement, plus append-only evidence and buyer feedback history tables.

Revision ID: 077
Revises: 076
Create Date: 2026-03-14
"""

import sqlalchemy as sa

from alembic import op

revision = "077"
down_revision = "076"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "sourcing_leads",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("lead_id", sa.String(length=64), nullable=False),
        sa.Column("requirement_id", sa.Integer(), sa.ForeignKey("requirements.id", ondelete="CASCADE"), nullable=False),
        sa.Column("requisition_id", sa.Integer(), sa.ForeignKey("requisitions.id", ondelete="CASCADE"), nullable=False),
        sa.Column("part_number_requested", sa.String(length=255), nullable=False),
        sa.Column("part_number_matched", sa.String(length=255), nullable=False),
        sa.Column("match_type", sa.String(length=32), nullable=False, server_default="exact"),
        sa.Column("vendor_name", sa.String(length=255), nullable=False),
        sa.Column("vendor_name_normalized", sa.String(length=255), nullable=False),
        sa.Column("canonical_vendor_id", sa.String(length=128), nullable=True),
        sa.Column("vendor_card_id", sa.Integer(), sa.ForeignKey("vendor_cards.id", ondelete="SET NULL"), nullable=True),
        sa.Column("primary_source_type", sa.String(length=64), nullable=False),
        sa.Column("primary_source_name", sa.String(length=128), nullable=False),
        sa.Column("source_reference", sa.String(length=1000), nullable=True),
        sa.Column("source_first_seen_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("source_last_seen_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("contact_name", sa.String(length=255), nullable=True),
        sa.Column("contact_email", sa.String(length=255), nullable=True),
        sa.Column("contact_phone", sa.String(length=100), nullable=True),
        sa.Column("contact_url", sa.String(length=1000), nullable=True),
        sa.Column("location", sa.String(length=255), nullable=True),
        sa.Column("notes_for_buyer", sa.Text(), nullable=True),
        sa.Column("suggested_next_action", sa.String(length=500), nullable=True),
        sa.Column("confidence_score", sa.Float(), nullable=False, server_default="0"),
        sa.Column("confidence_band", sa.String(length=16), nullable=False, server_default="low"),
        sa.Column("freshness_score", sa.Float(), nullable=True),
        sa.Column("source_reliability_score", sa.Float(), nullable=True),
        sa.Column("contactability_score", sa.Float(), nullable=True),
        sa.Column("historical_success_score", sa.Float(), nullable=True),
        sa.Column("reason_summary", sa.Text(), nullable=False, server_default=""),
        sa.Column("risk_flags", sa.JSON(), nullable=False, server_default=sa.text("'[]'")),
        sa.Column("evidence_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("corroborated", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("vendor_safety_score", sa.Float(), nullable=True),
        sa.Column("vendor_safety_band", sa.String(length=24), nullable=True),
        sa.Column("vendor_safety_summary", sa.Text(), nullable=True),
        sa.Column("vendor_safety_flags", sa.JSON(), nullable=False, server_default=sa.text("'[]'")),
        sa.Column("vendor_safety_last_checked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("buyer_status", sa.String(length=32), nullable=False, server_default="new"),
        sa.Column("buyer_owner_user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("last_buyer_action_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("buyer_feedback_summary", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.UniqueConstraint(
            "requirement_id",
            "vendor_name_normalized",
            "part_number_matched",
            name="uq_sourcing_lead_requirement_vendor_part",
        ),
    )
    op.create_index("ix_sourcing_leads_lead_id", "sourcing_leads", ["lead_id"], unique=True)
    op.create_index("ix_sourcing_leads_requirement_id", "sourcing_leads", ["requirement_id"])
    op.create_index("ix_sourcing_leads_requisition_id", "sourcing_leads", ["requisition_id"])
    op.create_index("ix_sourcing_leads_vendor_name_normalized", "sourcing_leads", ["vendor_name_normalized"])
    op.create_index("ix_sourcing_leads_vendor_card_id", "sourcing_leads", ["vendor_card_id"])
    op.create_index("ix_sourcing_leads_buyer_owner_user_id", "sourcing_leads", ["buyer_owner_user_id"])
    op.create_index("ix_sourcing_leads_status", "sourcing_leads", ["buyer_status"])
    op.create_index("ix_sourcing_leads_confidence", "sourcing_leads", ["confidence_score"])
    op.create_index("ix_sourcing_leads_safety", "sourcing_leads", ["vendor_safety_score"])
    op.create_index("ix_sourcing_leads_req_status", "sourcing_leads", ["requisition_id", "buyer_status"])

    op.create_table(
        "lead_evidence",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("evidence_id", sa.String(length=64), nullable=False),
        sa.Column("lead_id", sa.Integer(), sa.ForeignKey("sourcing_leads.id", ondelete="CASCADE"), nullable=False),
        sa.Column("signal_type", sa.String(length=64), nullable=False),
        sa.Column("source_type", sa.String(length=64), nullable=False),
        sa.Column("source_name", sa.String(length=128), nullable=False),
        sa.Column("source_reference", sa.String(length=1000), nullable=True),
        sa.Column("part_number_observed", sa.String(length=255), nullable=True),
        sa.Column("vendor_name_observed", sa.String(length=255), nullable=True),
        sa.Column("observed_text", sa.Text(), nullable=True),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("freshness_age_days", sa.Float(), nullable=True),
        sa.Column("weight", sa.Float(), nullable=True),
        sa.Column("confidence_impact", sa.Float(), nullable=True),
        sa.Column("explanation", sa.Text(), nullable=True),
        sa.Column("source_reliability_band", sa.String(length=16), nullable=True),
        sa.Column("verification_state", sa.String(length=32), nullable=True, server_default="raw"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("ix_lead_evidence_evidence_id", "lead_evidence", ["evidence_id"], unique=True)
    op.create_index("ix_lead_evidence_lead_id", "lead_evidence", ["lead_id"])
    op.create_index("ix_lead_evidence_source_type", "lead_evidence", ["source_type"])
    op.create_index("ix_lead_evidence_verification", "lead_evidence", ["verification_state"])

    op.create_table(
        "lead_feedback_events",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("lead_id", sa.Integer(), sa.ForeignKey("sourcing_leads.id", ondelete="CASCADE"), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("reason_code", sa.String(length=64), nullable=True),
        sa.Column("contact_method", sa.String(length=32), nullable=True),
        sa.Column("contact_attempt_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_by_user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("ix_lead_feedback_events_lead_id", "lead_feedback_events", ["lead_id"])
    op.create_index("ix_lead_feedback_events_created_by_user_id", "lead_feedback_events", ["created_by_user_id"])
    op.create_index("ix_lead_feedback_lead_created", "lead_feedback_events", ["lead_id", "created_at"])
    op.create_index("ix_lead_feedback_status", "lead_feedback_events", ["status"])


def downgrade() -> None:
    op.drop_index("ix_lead_feedback_status", table_name="lead_feedback_events")
    op.drop_index("ix_lead_feedback_lead_created", table_name="lead_feedback_events")
    op.drop_index("ix_lead_feedback_events_created_by_user_id", table_name="lead_feedback_events")
    op.drop_index("ix_lead_feedback_events_lead_id", table_name="lead_feedback_events")
    op.drop_table("lead_feedback_events")

    op.drop_index("ix_lead_evidence_verification", table_name="lead_evidence")
    op.drop_index("ix_lead_evidence_source_type", table_name="lead_evidence")
    op.drop_index("ix_lead_evidence_lead_id", table_name="lead_evidence")
    op.drop_index("ix_lead_evidence_evidence_id", table_name="lead_evidence")
    op.drop_table("lead_evidence")

    op.drop_index("ix_sourcing_leads_req_status", table_name="sourcing_leads")
    op.drop_index("ix_sourcing_leads_safety", table_name="sourcing_leads")
    op.drop_index("ix_sourcing_leads_confidence", table_name="sourcing_leads")
    op.drop_index("ix_sourcing_leads_status", table_name="sourcing_leads")
    op.drop_index("ix_sourcing_leads_buyer_owner_user_id", table_name="sourcing_leads")
    op.drop_index("ix_sourcing_leads_vendor_card_id", table_name="sourcing_leads")
    op.drop_index("ix_sourcing_leads_vendor_name_normalized", table_name="sourcing_leads")
    op.drop_index("ix_sourcing_leads_requisition_id", table_name="sourcing_leads")
    op.drop_index("ix_sourcing_leads_requirement_id", table_name="sourcing_leads")
    op.drop_index("ix_sourcing_leads_lead_id", table_name="sourcing_leads")
    op.drop_table("sourcing_leads")
