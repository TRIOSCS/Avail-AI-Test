"""Add teams_alert_sent_at to vendor_responses for dedup.

Revision ID: 060
Revises: 059
Create Date: 2026-03-07
"""

from alembic import op

revision = "060"
down_revision = "059"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("ALTER TABLE vendor_responses ADD COLUMN IF NOT EXISTS teams_alert_sent_at TIMESTAMP WITH TIME ZONE")


def downgrade():
    op.execute("ALTER TABLE vendor_responses DROP COLUMN IF EXISTS teams_alert_sent_at")
