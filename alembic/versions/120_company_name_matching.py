"""Company name-matching foundation — normalized_name + alternate_names (Increment 3).

Mirrors VendorCard's dedup foundation onto Company so the duplicate scanner has a durable
match key and merges can record absorbed (loser) names:

- companies.normalized_name (String(255), nullable, btree-indexed): the suffix-stripped /
  lowercased match key. NULLABLE and NOT unique on purpose — companies legitimately share
  a normalized form across the dedup window (the policy keeps different-owner accounts
  separate), so this is a similarity key, not a constraint (contrast VendorCard, where it
  IS unique). Kept in lockstep with `name` by Company._sync_normalized_name (@validates).
- companies.alternate_names (JSON): names this company has been known by; merge_companies
  appends the loser's name (+ its alternates) here so a re-import of the old name
  fuzzy-matches an existing card instead of recreating the duplicate.

Backfill: normalized_name is populated from the existing `name` using the SAME normalizer
the scanner uses (app.vendor_utils.normalize_vendor_name) — Inc/LLC/Ltd/Corp/GmbH +
leading-"the" stripped, lowercased, whitespace collapsed. Blank/whitespace names stay NULL.

pg_trgm: on PostgreSQL we additionally create a GIN(normalized_name gin_trgm_ops) index so
find_company_dedup_candidates can drop the 500-row O(n^2) rapidfuzz cap and scan with
func.similarity(). pg_trgm is Postgres-only; the extension + GIN index are guarded behind
`dialect.name == "postgresql"` (the SQLite test DB keeps the rapidfuzz path —
feedback_sqlite_masks_postgres). The plain btree index is created on all dialects.

Revision ID: 120_company_name_matching
Revises: 119_alert_seen
Create Date: 2026-06-19
"""

import sqlalchemy as sa

from alembic import op

revision = "120_company_name_matching"
down_revision = "119_alert_seen"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()

    op.add_column("companies", sa.Column("normalized_name", sa.String(length=255), nullable=True))
    op.add_column("companies", sa.Column("alternate_names", sa.JSON(), nullable=True))

    # Plain btree index (all dialects) — exact-normalized lookups + the column scan.
    op.create_index("ix_companies_normalized_name", "companies", ["normalized_name"], unique=False)

    # Backfill normalized_name from name with the project normalizer (identical to
    # scan-time scoring). Done in Python so the regex/suffix logic stays single-sourced.
    from app.vendor_utils import normalize_vendor_name

    companies = sa.table(
        "companies",
        sa.column("id", sa.Integer),
        sa.column("name", sa.String),
        sa.column("normalized_name", sa.String),
    )
    rows = bind.execute(sa.select(companies.c.id, companies.c.name)).fetchall()
    for row in rows:
        norm = normalize_vendor_name(row.name or "") or None
        if norm:
            bind.execute(sa.update(companies).where(companies.c.id == row.id).values(normalized_name=norm))

    if bind.dialect.name == "postgresql":
        # pg_trgm GIN index for similarity() scanning — Postgres-only (SQLite has no
        # pg_trgm; the scanner falls back to rapidfuzz there).
        op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
        op.create_index(
            "ix_companies_normalized_name_trgm",
            "companies",
            [sa.text("normalized_name gin_trgm_ops")],
            unique=False,
            postgresql_using="gin",
        )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.drop_index("ix_companies_normalized_name_trgm", table_name="companies", if_exists=True)
    op.drop_index("ix_companies_normalized_name", table_name="companies")
    op.drop_column("companies", "alternate_names")
    op.drop_column("companies", "normalized_name")
