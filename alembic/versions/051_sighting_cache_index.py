"""Add composite index on sightings for per-source search cache lookups.

Index: (normalized_mpn, source_type, created_at) — allows efficient queries
to find which connector+MPN pairs already have recent results.

Revision ID: 051
Revises: 050
"""

from alembic import op

revision = "051"
down_revision = "050"
branch_labels = None
depends_on = None


def upgrade():
    op.create_index(
        "ix_sighting_cache_lookup",
        "sightings",
        ["normalized_mpn", "source_type", "created_at"],
        if_not_exists=True,
    )


def downgrade():
    op.drop_index("ix_sighting_cache_lookup", table_name="sightings")
