"""Add evidence tier columns to sightings and offers.

Sightings get evidence_tier (T1–T7) and score_components (JSON).
Offers get evidence_tier, parse_confidence, promoted_by_id, promoted_at.

Revision ID: 069
Revises: 068
Create Date: 2026-03-10
"""

from alembic import op

revision = "069"
down_revision = "068"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Sighting columns
    op.execute("ALTER TABLE sightings ADD COLUMN IF NOT EXISTS evidence_tier VARCHAR(4)")
    op.execute("ALTER TABLE sightings ADD COLUMN IF NOT EXISTS score_components JSON")

    # Offer columns
    op.execute("ALTER TABLE offers ADD COLUMN IF NOT EXISTS evidence_tier VARCHAR(4)")
    op.execute("ALTER TABLE offers ADD COLUMN IF NOT EXISTS parse_confidence DOUBLE PRECISION")
    op.execute("ALTER TABLE offers ADD COLUMN IF NOT EXISTS promoted_by_id INTEGER")

    # Index for filtering by evidence tier
    op.create_index("ix_sightings_evidence_tier", "sightings", ["evidence_tier"], if_not_exists=True)
    op.create_index("ix_offers_evidence_tier", "offers", ["evidence_tier"], if_not_exists=True)


def downgrade() -> None:
    op.drop_index("ix_offers_evidence_tier", table_name="offers", if_exists=True)
    op.drop_index("ix_sightings_evidence_tier", table_name="sightings", if_exists=True)

    op.execute("ALTER TABLE offers DROP COLUMN IF EXISTS promoted_at")
    op.execute("ALTER TABLE offers DROP COLUMN IF EXISTS promoted_by_id")
    op.execute("ALTER TABLE offers DROP COLUMN IF EXISTS parse_confidence")
    op.execute("ALTER TABLE offers DROP COLUMN IF EXISTS evidence_tier")

    op.execute("ALTER TABLE sightings DROP COLUMN IF EXISTS score_components")
    op.execute("ALTER TABLE sightings DROP COLUMN IF EXISTS evidence_tier")
