"""Add performance indexes and is_stale column on offers.

ix_offers_status_created: composite index on offers(status, created_at DESC)
for the /api/dashboard/hot-offers endpoint which filters by status='active'
and sorts by created_at DESC.

ix_poff_status_sent: composite index on proactive_offers(status, sent_at)
for the proactive offer expiry job which filters by status='sent' and sent_at.

is_stale: Boolean flag on offers — display-only metadata for offers older
than 14 days. Does NOT hide or filter offers. "Leave no stone unturned."

Revision ID: 015_performance_indexes
Revises: 014_multiplier_score_snapshot
Create Date: 2026-02-26
"""

from alembic import op
import sqlalchemy as sa

revision = "015_performance_indexes"
down_revision = "014_multiplier_score_snapshot"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index(
        "ix_offers_status_created",
        "offers",
        ["status", "created_at"],
    )
    op.create_index(
        "ix_poff_status_sent",
        "proactive_offers",
        ["status", "sent_at"],
    )
    op.add_column(
        "offers",
        sa.Column("is_stale", sa.Boolean, server_default=sa.text("false"), nullable=False),
    )


def downgrade() -> None:
    op.drop_column("offers", "is_stale")
    op.drop_index("ix_poff_status_sent", table_name="proactive_offers")
    op.drop_index("ix_offers_status_created", table_name="offers")
