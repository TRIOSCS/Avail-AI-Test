"""Add pg_trgm extension and GIN trigram index on vendor_cards.normalized_name.

Enables PostgreSQL similarity() for fast fuzzy vendor duplicate checking
instead of loading all rows into Python.

Revision ID: 049_pg_trgm
Revises: f3fbddb04947
Create Date: 2026-03-20
"""

from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "049_pg_trgm"
down_revision: Union[str, None] = "f3fbddb04947"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Enable pg_trgm extension (safe to call if already enabled)
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")

    # GIN trigram index for fast similarity() queries on vendor names
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_vendor_cards_normalized_name_trgm "
        "ON vendor_cards USING gin (normalized_name gin_trgm_ops)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_vendor_cards_normalized_name_trgm")
    # Don't drop the extension — other tables may depend on it
