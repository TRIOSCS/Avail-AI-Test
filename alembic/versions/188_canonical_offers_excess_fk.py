"""Canonicalize the FK on offers.excess_line_item_id (single, canonically named).

Root-cause follow-up to the latent FK-name mismatch documented in PR #714:
migration 001 (the explicit-DDL baseline) creates the FK on
offers.excess_line_item_id under PostgreSQL's default name
``offers_excess_line_item_id_fkey`` (the name an unnamed model
``ForeignKey`` produces — there is no metadata naming_convention), while
d1a2b3c4e5f6's replay guard checked pg_constraint for its OWN name
``fk_offers_excess_line_item_id`` — a name the baseline never creates — and
so created a SECOND, identical FK on the column. Reproduced on a throwaway
PostgreSQL 16: a fresh ``alembic upgrade head`` left BOTH constraints on the
column (same definition, two names). alembic autogenerate compares FKs by
signature, not name, so the schema-drift gate never flagged the duplicate.

This migration converges every reachable database state onto exactly one FK
named ``offers_excess_line_item_id_fkey``:

- both names present (fresh-chain replay before this fix) -> drop the stray
  ``fk_offers_excess_line_item_id``;
- only ``fk_offers_excess_line_item_id`` present (pure-incremental history
  that ran the original d1a2b3c4e5f6) -> RENAME it to the canonical name
  (rename keeps the constraint validated — no re-check of existing rows);
- only the canonical name present (fresh replay after d1a2b3c4e5f6's guard
  fix landed alongside this migration) -> no-op.

d1a2b3c4e5f6 and 5c6736d6381f now carry column-scoped FK guards (any FK on
offers.excess_line_item_id -> excess_line_items counts, regardless of name),
so no future replay can recreate the duplicate.

Downgrade is a documented no-op (mirrors 100/173/176): re-creating a
redundant duplicate FK would re-introduce the defect, and every earlier
revision is valid with the single canonical constraint.

PostgreSQL-only (pg_constraint / RENAME CONSTRAINT) — no-op on the SQLite
test dialect, guarded like sibling migrations (e.g. 187).

Revision ID: 188_canonical_offers_excess_fk
Revises: 187_startup_backfill_partial_idx
Create Date: 2026-07-15
"""

import sqlalchemy as sa

from alembic import op

revision = "188_canonical_offers_excess_fk"
down_revision = "187_startup_backfill_partial_idx"
branch_labels = None
depends_on = None

_CANONICAL = "offers_excess_line_item_id_fkey"
_STRAY = "fk_offers_excess_line_item_id"


def _excess_fk_names(conn) -> set[str]:
    """Names of every FK constraint covering offers.excess_line_item_id ->
    excess_line_items, regardless of constraint name."""
    rows = conn.execute(
        sa.text(
            "SELECT con.conname FROM pg_constraint con "
            "WHERE con.contype = 'f' "
            "AND con.conrelid = to_regclass('offers') "
            "AND con.confrelid = to_regclass('excess_line_items') "
            "AND con.conkey = ARRAY[(SELECT attnum FROM pg_attribute "
            "WHERE attrelid = con.conrelid AND attname = 'excess_line_item_id')]::smallint[]"
        )
    ).scalars()
    return set(rows)


def upgrade() -> None:
    conn = op.get_bind()
    if conn.dialect.name != "postgresql":
        return

    names = _excess_fk_names(conn)
    if _CANONICAL in names and _STRAY in names:
        op.drop_constraint(_STRAY, "offers", type_="foreignkey")
    elif _STRAY in names:
        op.execute(f"ALTER TABLE offers RENAME CONSTRAINT {_STRAY} TO {_CANONICAL}")
    # Only the canonical FK (or, degenerately, none) — nothing to converge.


def downgrade() -> None:
    # Documented no-op: the upgrade removes/renames a redundant duplicate of a
    # constraint that every earlier revision is already valid against.
    # Re-creating the stray fk_offers_excess_line_item_id would re-introduce
    # the duplicate-FK defect this migration exists to remove.
    pass
