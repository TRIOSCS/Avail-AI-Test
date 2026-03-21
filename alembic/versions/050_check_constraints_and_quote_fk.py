"""Add CHECK constraints for prices/quantities and fix quotes.customer_site_id FK
ondelete.

Adds non-negative CHECK constraints on price columns and positive-quantity
constraints on quantity columns across offers, excess_line_items, bids, and
sightings tables.  Also changes quotes.customer_site_id FK to ondelete=SET NULL
and makes the column nullable.

Constraints are added NOT VALID first (non-blocking on large tables), then
validated in a separate step.

Revision ID: 050_check_constraints
Revises: 8aad37e73b45
Create Date: 2026-03-21
"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

revision: str = "050_check_constraints"
down_revision: Union[str, None] = "8aad37e73b45"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _constraint_exists(name: str) -> bool:
    """Check whether a constraint already exists (PostgreSQL)."""
    conn = op.get_bind()
    result = conn.execute(
        sa.text("SELECT 1 FROM pg_constraint WHERE conname = :name"),
        {"name": name},
    )
    return result.fetchone() is not None


# (constraint_name, table, expression)
CHECK_CONSTRAINTS = [
    ("ck_offers_unit_price_nonneg", "offers", "unit_price >= 0"),
    ("ck_offers_qty_available_nonneg", "offers", "qty_available >= 0"),
    ("ck_excess_line_items_quantity_pos", "excess_line_items", "quantity > 0"),
    ("ck_excess_line_items_asking_price_nonneg", "excess_line_items", "asking_price >= 0"),
    ("ck_bids_unit_price_nonneg", "bids", "unit_price >= 0"),
    ("ck_bids_quantity_wanted_pos", "bids", "quantity_wanted > 0"),
    ("ck_sightings_unit_price_nonneg", "sightings", "unit_price >= 0"),
    ("ck_sightings_qty_available_nonneg", "sightings", "qty_available >= 0"),
]


def upgrade() -> None:
    conn = op.get_bind()
    is_pg = conn.dialect.name == "postgresql"

    # --- CHECK constraints ---
    for name, table, expr in CHECK_CONSTRAINTS:
        if is_pg:
            if not _constraint_exists(name):
                # Add NOT VALID first (non-blocking for large tables)
                conn.execute(sa.text(f"ALTER TABLE {table} ADD CONSTRAINT {name} CHECK ({expr}) NOT VALID"))
                # Then validate (scans rows but doesn't hold ACCESS EXCLUSIVE)
                conn.execute(sa.text(f"ALTER TABLE {table} VALIDATE CONSTRAINT {name}"))
        else:
            # SQLite: use Alembic op (limited CHECK support but fine for tests)
            op.create_check_constraint(name, table, expr)

    # --- quotes.customer_site_id: nullable + ondelete SET NULL ---
    if is_pg:
        # Make column nullable
        op.alter_column("quotes", "customer_site_id", nullable=True)

        # Drop old FK (SQLAlchemy default naming: quotes_customer_site_id_fkey)
        fk_name = "quotes_customer_site_id_fkey"
        if _constraint_exists(fk_name):
            op.drop_constraint(fk_name, "quotes", type_="foreignkey")

        # Recreate with ondelete SET NULL
        op.create_foreign_key(
            fk_name,
            "quotes",
            "customer_sites",
            ["customer_site_id"],
            ["id"],
            ondelete="SET NULL",
        )


def downgrade() -> None:
    conn = op.get_bind()
    is_pg = conn.dialect.name == "postgresql"

    # --- Revert quotes.customer_site_id FK ---
    if is_pg:
        fk_name = "quotes_customer_site_id_fkey"
        if _constraint_exists(fk_name):
            op.drop_constraint(fk_name, "quotes", type_="foreignkey")

        op.create_foreign_key(
            fk_name,
            "quotes",
            "customer_sites",
            ["customer_site_id"],
            ["id"],
        )

        # Restore NOT NULL (will fail if NULLs exist — acceptable for rollback)
        op.alter_column("quotes", "customer_site_id", nullable=False)

    # --- Drop CHECK constraints ---
    for name, table, _ in CHECK_CONSTRAINTS:
        if is_pg:
            if _constraint_exists(name):
                op.drop_constraint(name, table, type_="check")
        else:
            op.drop_constraint(name, table, type_="check")
