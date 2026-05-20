"""Add evidence tier columns to sightings and offers.

Sightings get evidence_tier (T1–T7) and score_components (JSON).
Offers get evidence_tier, parse_confidence, promoted_by_id, promoted_at.

Revision ID: 069
Revises: 068
Create Date: 2026-03-10
"""

import sqlalchemy as sa

from alembic import op

revision = "069"
down_revision = "068"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Sighting columns
    op.add_column("sightings", sa.Column("evidence_tier", sa.String(4), nullable=True))
    op.add_column("sightings", sa.Column("score_components", sa.JSON(), nullable=True))

    # Offer columns
    op.add_column("offers", sa.Column("evidence_tier", sa.String(4), nullable=True))
    op.add_column("offers", sa.Column("parse_confidence", sa.Float(), nullable=True))
    op.add_column(
        "offers",
        sa.Column("promoted_by_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
    )
    op.add_column("offers", sa.Column("promoted_at", sa.DateTime(), nullable=True))

    # Index for filtering by evidence tier
    op.create_index("ix_sightings_evidence_tier", "sightings", ["evidence_tier"], if_not_exists=True)
    op.create_index("ix_offers_evidence_tier", "offers", ["evidence_tier"], if_not_exists=True)


def downgrade() -> None:
    op.drop_index("ix_offers_evidence_tier", table_name="offers", if_exists=True)
    op.drop_index("ix_sightings_evidence_tier", table_name="sightings", if_exists=True)

    op.execute("ALTER TABLE IF EXISTS offers DROP COLUMN IF EXISTS promoted_at")
    op.execute("ALTER TABLE IF EXISTS offers DROP COLUMN IF EXISTS promoted_by_id")
    op.execute("ALTER TABLE IF EXISTS offers DROP COLUMN IF EXISTS parse_confidence")
    op.execute("ALTER TABLE IF EXISTS offers DROP COLUMN IF EXISTS evidence_tier")

    op.execute("ALTER TABLE IF EXISTS sightings DROP COLUMN IF EXISTS score_components")
    op.execute("ALTER TABLE IF EXISTS sightings DROP COLUMN IF EXISTS evidence_tier")
