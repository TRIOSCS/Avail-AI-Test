"""Add GIN trigram index on vendor_cards.normalized_name.

Uses pg_trgm extension (already enabled in startup.py) for fast fuzzy matching.
Replaces Python-side thefuzz loops with single SQL query.

Revision ID: 024_vendor_trgm
Revises: 023_nc_cache_status
Create Date: 2026-02-27
"""

from alembic import op

revision = "024_vendor_trgm"
down_revision = "023_nc_cache_status"
branch_labels = None
depends_on = None


def upgrade():
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_vendor_cards_name_trgm ON vendor_cards USING gin (normalized_name gin_trgm_ops)"
    )


def downgrade():
    op.execute("DROP INDEX IF EXISTS ix_vendor_cards_name_trgm")
