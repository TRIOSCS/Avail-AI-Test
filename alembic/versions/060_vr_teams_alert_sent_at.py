"""Add teams_alert_sent_at to vendor_responses for dedup.

Revision ID: 060
Revises: 059
Create Date: 2026-03-07
"""

from alembic import op
import sqlalchemy as sa

revision = "060"
down_revision = "059"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("vendor_responses", sa.Column("teams_alert_sent_at", sa.DateTime(timezone=True), nullable=True))


def downgrade():
    op.drop_column("vendor_responses", "teams_alert_sent_at")
