"""Restore the pg_trgm GIN search indexes lost from the live DB.

Migration a513288799de created 15 pg_trgm GIN indexes powering fuzzy / substring
search across requisitions, companies, vendor cards, vendor/site contacts,
requirements, and offers. All 15 are absent from the live database (a fresh-DB
restore/rebuild dropped them while later migrations' indexes survived), even though
a513 is stamped applied and the ORM models still declare every one — so every
CRM/vendor/search name/email/MPN ILIKE currently runs a sequential scan.

This re-creates the identical index set (same names, columns, and gin_trgm_ops) so
the names match both a513 and the model __table_args__ (fresh-DB drift gate stays
green; a fresh migration-built DB already has them, so every CREATE IF NOT EXISTS is
a no-op there). pg_trgm is already installed live. Building now — while the tables
are near-empty (SFDC import pending) — is effectively instant.

Revision ID: 202_restore_trgm_indexes
Revises: 201_drop_offer_valid_until
Create Date: 2026-07-21
"""

from collections.abc import Sequence

from alembic import op

revision: str = "202_restore_trgm_indexes"
down_revision: str | None = "201_drop_offer_valid_until"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# (table, column) pairs — the exact set migration a513288799de defines, matching the
# ix_<table>_<column>_trgm indexes declared in the ORM models' __table_args__.
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
    # pg_trgm is already enabled live; guard anyway so a fresh/dev DB self-heals.
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm;")
    for table, column in _TRIGRAM_INDEXES:
        idx = _index_name(table, column)
        op.execute(f"CREATE INDEX IF NOT EXISTS {idx} ON {table} USING gin ({column} gin_trgm_ops);")


def downgrade() -> None:
    for table, column in reversed(_TRIGRAM_INDEXES):
        idx = _index_name(table, column)
        op.execute(f"DROP INDEX IF EXISTS {idx};")
