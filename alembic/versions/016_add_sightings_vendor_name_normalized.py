"""Add vendor_name_normalized to sightings table.

App code (e.g. requisitions list_requirements) uses this column; with TESTING=1
startup _add_missing_columns is skipped, so Alembic must add it. Idempotent.

Revision ID: 016_add_sightings_vendor_name_normalized
Revises: 015_performance_indexes
Create Date: 2026-02-26
"""

from alembic import op
from sqlalchemy import text

revision = "016_add_sightings_vendor_name_normalized"
down_revision = "015_performance_indexes"
branch_labels = None
depends_on = None


def upgrade() -> None:
    conn = op.get_bind()
    # Widen version_num so long revision IDs (e.g. 016_add_sightings_vendor_name_normalized) fit
    conn.execute(text("ALTER TABLE alembic_version ALTER COLUMN version_num TYPE VARCHAR(64)"))
    conn.execute(text("ALTER TABLE sightings ADD COLUMN IF NOT EXISTS vendor_name_normalized VARCHAR(255)"))
    conn.execute(text("UPDATE sightings SET vendor_name_normalized = LOWER(TRIM(vendor_name)) WHERE vendor_name IS NOT NULL AND vendor_name_normalized IS NULL"))
    conn.execute(text("CREATE INDEX IF NOT EXISTS ix_sightings_vendor_norm ON sightings (vendor_name_normalized)"))


def downgrade() -> None:
    op.drop_index("ix_sightings_vendor_norm", table_name="sightings")
    op.drop_column("sightings", "vendor_name_normalized")
