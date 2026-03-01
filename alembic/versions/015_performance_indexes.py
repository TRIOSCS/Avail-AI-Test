"""Add performance indexes and is_stale column on offers.

ix_offers_status_created: composite index on offers(status, created_at DESC)
for the /api/dashboard/hot-offers endpoint which filters by status='active'
and sorts by created_at DESC.

ix_poff_status_sent: composite index on proactive_offers(status, sent_at)
for the proactive offer expiry job which filters by status='sent' and sent_at.

is_stale: Boolean flag on offers — display-only metadata for offers older
than 14 days. Does NOT hide or filter offers. "Leave no stone unturned."
Idempotent: column/indexes may already exist from startup.

Revision ID: 015_performance_indexes
Revises: 014_multiplier_score_snapshot
Create Date: 2026-02-26
"""

from sqlalchemy import text

from alembic import op

revision = "015_performance_indexes"
down_revision = "014_multiplier_score_snapshot"
branch_labels = None
depends_on = None


def upgrade() -> None:
    conn = op.get_bind()
    conn.execute(text("ALTER TABLE offers ADD COLUMN IF NOT EXISTS is_stale BOOLEAN NOT NULL DEFAULT FALSE"))
    conn.execute(text("CREATE INDEX IF NOT EXISTS ix_offers_status_created ON offers (status, created_at)"))
    conn.execute(text("CREATE INDEX IF NOT EXISTS ix_poff_status_sent ON proactive_offers (status, sent_at)"))


def downgrade() -> None:
    op.drop_column("offers", "is_stale")
    op.drop_index("ix_poff_status_sent", table_name="proactive_offers")
    op.drop_index("ix_offers_status_created", table_name="offers")
