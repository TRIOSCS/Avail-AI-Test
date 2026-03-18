"""Add pg_trgm extension and GIN trigram indexes for global search.

Enables the pg_trgm PostgreSQL extension and adds GIN trigram indexes
on key text columns to power fast fuzzy / substring search across
requisitions, companies, vendors, contacts, requirements, and offers.

Revision ID: a513288799de
Revises: 1e277d3b08a6
Create Date: 2026-03-18 20:55:45.183158
"""

from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a513288799de"
down_revision: Union[str, None] = "1e277d3b08a6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# (table, column) pairs that need trigram indexes
_TRIGRAM_INDEXES: list[tuple[str, str]] = [
    ("requisitions", "name"),
    ("requisitions", "customer_name"),
    ("companies", "name"),
    ("companies", "domain"),
    ("vendor_cards", "display_name"),
    ("vendor_cards", "normalized_name"),
    ("vendor_cards", "domain"),
    ("vendor_contacts", "full_name"),
    ("vendor_contacts", "email"),
    ("site_contacts", "full_name"),
    ("site_contacts", "email"),
    ("requirements", "primary_mpn"),
    ("requirements", "normalized_mpn"),
    ("offers", "mpn"),
    ("offers", "vendor_name"),
]


def _index_name(table: str, column: str) -> str:
    return f"ix_{table}_{column}_trgm"


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm;")
    for table, column in _TRIGRAM_INDEXES:
        idx = _index_name(table, column)
        op.execute(f"CREATE INDEX IF NOT EXISTS {idx} ON {table} USING gin ({column} gin_trgm_ops);")


def downgrade() -> None:
    for table, column in reversed(_TRIGRAM_INDEXES):
        idx = _index_name(table, column)
        op.execute(f"DROP INDEX IF EXISTS {idx};")
    op.execute("DROP EXTENSION IF EXISTS pg_trgm;")
