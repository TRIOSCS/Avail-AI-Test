"""Add last_error_at and error_count_24h columns to api_sources.

Supports connector health dashboard with auto-degraded status detection.

Revision ID: 025_connector_health
Revises: 024_vendor_trgm
Create Date: 2026-02-27
"""

import sqlalchemy as sa
from alembic import op

revision = "025_connector_health"
down_revision = "024_vendor_trgm"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("api_sources", sa.Column("last_error_at", sa.DateTime(), nullable=True))
    op.add_column(
        "api_sources",
        sa.Column("error_count_24h", sa.Integer(), nullable=False, server_default="0"),
    )


def downgrade():
    op.drop_column("api_sources", "error_count_24h")
    op.drop_column("api_sources", "last_error_at")
