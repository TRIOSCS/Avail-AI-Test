"""Add promoted_to_type and promoted_to_id to prospect_contacts.

Tracks which VendorContact or SiteContact a prospect was promoted to.

Revision ID: 026_prospect_promote
Revises: 025_connector_health
Create Date: 2026-02-27
"""

from alembic import op

revision = "026_prospect_promote"
down_revision = "025_connector_health"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("ALTER TABLE prospect_contacts ADD COLUMN IF NOT EXISTS promoted_to_type VARCHAR(20)")
    op.execute("ALTER TABLE prospect_contacts ADD COLUMN IF NOT EXISTS promoted_to_id INTEGER")


def downgrade():
    op.execute("ALTER TABLE IF EXISTS prospect_contacts DROP COLUMN IF EXISTS promoted_to_id")
    op.execute("ALTER TABLE IF EXISTS prospect_contacts DROP COLUMN IF EXISTS promoted_to_type")
