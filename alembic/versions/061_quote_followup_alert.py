"""Add followup_alert_sent_at to quotes for stale quote tracking.

Revision ID: 061
Revises: 060
Create Date: 2026-03-07
"""

from alembic import op

revision = "061"
down_revision = "060"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("ALTER TABLE quotes ADD COLUMN IF NOT EXISTS followup_alert_sent_at TIMESTAMP WITH TIME ZONE")


def downgrade():
    op.execute("ALTER TABLE quotes DROP COLUMN IF EXISTS followup_alert_sent_at")
