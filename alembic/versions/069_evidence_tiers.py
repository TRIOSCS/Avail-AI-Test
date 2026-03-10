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
    op.create_index("ix_sightings_evidence_tier", "sightings", ["evidence_tier"])
    op.create_index("ix_offers_evidence_tier", "offers", ["evidence_tier"])


def downgrade() -> None:
    op.drop_index("ix_offers_evidence_tier", table_name="offers")
    op.drop_index("ix_sightings_evidence_tier", table_name="sightings")

    op.drop_column("offers", "promoted_at")
    op.drop_column("offers", "promoted_by_id")
    op.drop_column("offers", "parse_confidence")
    op.drop_column("offers", "evidence_tier")

    op.drop_column("sightings", "score_components")
    op.drop_column("sightings", "evidence_tier")
