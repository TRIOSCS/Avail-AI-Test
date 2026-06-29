"""Unavailability v2: condition column + replace single UniqueConstraint with two partial unique indexes.

What: Extends vendor_part_unavailability for condition-aware unavailability tracking:

  1. ``vendor_part_unavailability.condition`` (String(16), nullable) — the optional
     condition qualifier for the unavailability record (e.g. "new", "used", "refurb").
     NULL means the record applies to ALL conditions (the existing catch-all semantics;
     every existing row becomes a NULL/all-conditions record with no data change needed).

  2. Drops ``uq_vendor_part_unavail_vendor_mpn`` (the old UNIQUE vendor, mpn pair) and
     replaces it with two PARTIAL unique indexes:

     - ``uq_vpu_vendor_mpn_condition`` — UNIQUE (vendor_name_normalized, normalized_mpn,
       condition) WHERE condition IS NOT NULL.  Prevents duplicate condition-specific rows.
     - ``uq_vpu_vendor_mpn_allcond`` — UNIQUE (vendor_name_normalized, normalized_mpn)
       WHERE condition IS NULL.  Preserves the one-catch-all-per-vendor-part invariant.

Rollback (downgrade): drops both partial indexes, drops the condition column, then
recreates the original ``uq_vendor_part_unavail_vendor_mpn`` UNIQUE constraint.  Safe
because after downgrade every row has condition=NULL (i.e. one entry per vendor+mpn
pair) so the re-added unique constraint will not conflict.

Additive/reversible; round-tripped upgrade->downgrade->upgrade on a throwaway Postgres
(never against staging). Chains onto 170_prospecting_persistence; single head verified
via `alembic heads`.

Revision ID: 171_unavail_condition
Revises: 170_prospecting_persistence
Create Date: 2026-06-29
"""

import sqlalchemy as sa

from alembic import op

revision = "171_unavail_condition"
down_revision = "170_prospecting_persistence"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 1. Add condition column (nullable; existing rows become NULL = all-conditions catch-all).
    op.add_column(
        "vendor_part_unavailability",
        sa.Column("condition", sa.String(16), nullable=True),
    )

    # 2. Drop the old single-pair unique constraint.
    op.drop_constraint(
        "uq_vendor_part_unavail_vendor_mpn",
        "vendor_part_unavailability",
        type_="unique",
    )

    # 3. Partial unique index for condition-specific rows (condition IS NOT NULL).
    op.create_index(
        "uq_vpu_vendor_mpn_condition",
        "vendor_part_unavailability",
        ["vendor_name_normalized", "normalized_mpn", "condition"],
        unique=True,
        postgresql_where=sa.text("condition IS NOT NULL"),
        sqlite_where=sa.text("condition IS NOT NULL"),
    )

    # 4. Partial unique index for the all-conditions catch-all rows (condition IS NULL).
    op.create_index(
        "uq_vpu_vendor_mpn_allcond",
        "vendor_part_unavailability",
        ["vendor_name_normalized", "normalized_mpn"],
        unique=True,
        postgresql_where=sa.text("condition IS NULL"),
        sqlite_where=sa.text("condition IS NULL"),
    )


def downgrade() -> None:
    # Reverse in LIFO order: indexes first, then column, then rebuild constraint.
    op.drop_index("uq_vpu_vendor_mpn_allcond", table_name="vendor_part_unavailability")
    op.drop_index("uq_vpu_vendor_mpn_condition", table_name="vendor_part_unavailability")

    op.drop_column("vendor_part_unavailability", "condition")

    # Restore original unique constraint (all post-downgrade rows are NULL = one per pair).
    op.create_unique_constraint(
        "uq_vendor_part_unavail_vendor_mpn",
        "vendor_part_unavailability",
        ["vendor_name_normalized", "normalized_mpn"],
    )
