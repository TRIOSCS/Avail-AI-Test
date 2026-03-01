"""Add promoted_to_type and promoted_to_id to prospect_contacts.

Tracks which VendorContact or SiteContact a prospect was promoted to.

Revision ID: 026_prospect_promote
Revises: 025_connector_health
Create Date: 2026-02-27
"""

import sqlalchemy as sa

from alembic import op

revision = "026_prospect_promote"
down_revision = "025_connector_health"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("prospect_contacts", sa.Column("promoted_to_type", sa.String(20), nullable=True))
    op.add_column("prospect_contacts", sa.Column("promoted_to_id", sa.Integer(), nullable=True))


def downgrade():
    op.drop_column("prospect_contacts", "promoted_to_id")
    op.drop_column("prospect_contacts", "promoted_to_type")
