"""EmailIntelligence table for AI-powered inbox mining.

Revision ID: 032_email_intelligence
Revises: 031_ics_search_tables
Create Date: 2026-02-28

Stores AI classification results, pricing intelligence, and extracted data
from the email mining pipeline.
"""

revision = "032_email_intelligence"
down_revision = "031_ics_search_tables"

from alembic import op
import sqlalchemy as sa


def upgrade() -> None:
    op.create_table(
        "email_intelligence",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("message_id", sa.String(255), nullable=False, index=True),
        sa.Column("user_id", sa.Integer, sa.ForeignKey("users.id"), nullable=False),
        sa.Column("sender_email", sa.String(255), nullable=False),
        sa.Column("sender_domain", sa.String(255), nullable=False, index=True),
        sa.Column("classification", sa.String(20), nullable=False),
        sa.Column("confidence", sa.Float, nullable=False, default=0.0),
        sa.Column("has_pricing", sa.Boolean, default=False),
        sa.Column("parts_detected", sa.JSON, default=list),
        sa.Column("brands_detected", sa.JSON, default=list),
        sa.Column("commodities_detected", sa.JSON, default=list),
        sa.Column("parsed_quotes", sa.JSON),
        sa.Column("subject", sa.String(500)),
        sa.Column("received_at", sa.DateTime),
        sa.Column("conversation_id", sa.String(255), index=True),
        sa.Column("auto_applied", sa.Boolean, default=False),
        sa.Column("needs_review", sa.Boolean, default=False),
        sa.Column("thread_summary", sa.JSON),
        sa.Column("created_at", sa.DateTime, default=sa.func.now()),
    )
    op.create_index(
        "ix_email_intel_user_received",
        "email_intelligence",
        ["user_id", "received_at"],
    )
    op.create_index(
        "ix_email_intel_classification",
        "email_intelligence",
        ["classification"],
    )
    op.create_index(
        "ix_email_intel_needs_review",
        "email_intelligence",
        ["needs_review"],
    )


def downgrade() -> None:
    op.drop_index("ix_email_intel_needs_review")
    op.drop_index("ix_email_intel_classification")
    op.drop_index("ix_email_intel_user_received")
    op.drop_table("email_intelligence")
