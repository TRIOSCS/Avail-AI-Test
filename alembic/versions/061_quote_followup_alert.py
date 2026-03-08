"""Add followup_alert_sent_at to quotes for stale quote tracking.

Revision ID: 061
Revises: 060
Create Date: 2026-03-07
"""

import sqlalchemy as sa

from alembic import op

revision = "061"
down_revision = "060"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("quotes", sa.Column("followup_alert_sent_at", sa.DateTime(timezone=True), nullable=True))


def downgrade():
    op.drop_column("quotes", "followup_alert_sent_at")
