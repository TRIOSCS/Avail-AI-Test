"""Add excess phase 4 columns: normalized_part_number, excess_line_item_id.

Adds ExcessLineItem.normalized_part_number (VARCHAR 100, nullable, indexed)
and Offer.excess_line_item_id (INTEGER, nullable, FK to excess_line_items.id,
ondelete SET NULL) with index.

Idempotent: uses IF NOT EXISTS / column existence checks since these columns
may already exist from a prior run of the original d1a2b3c4e5f6 migration.
The offers FK guard is COLUMN-scoped (any FK covering excess_line_item_id ->
excess_line_items counts, regardless of name): the original name-scoped guard
missed the baseline's PostgreSQL-default 'offers_excess_line_item_id_fkey'
and duplicated the FK on fresh-DB replays — see
188_canonical_offers_excess_fk for the full history and the cleanup.

Revision ID: d1a2b3c4e5f6
Revises: c19a184db289
Create Date: 2026-03-20 12:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "d1a2b3c4e5f6"
down_revision: str | None = "c19a184db289"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _column_exists(table: str, column: str) -> bool:
    """Check whether a column already exists (PostgreSQL)."""
    conn = op.get_bind()
    result = conn.execute(
        sa.text("SELECT 1 FROM information_schema.columns WHERE table_name = :table AND column_name = :column"),
        {"table": table, "column": column},
    )
    return result.scalar() is not None


def _index_exists(index_name: str) -> bool:
    """Check whether an index already exists (PostgreSQL)."""
    conn = op.get_bind()
    result = conn.execute(
        sa.text("SELECT 1 FROM pg_indexes WHERE indexname = :name"),
        {"name": index_name},
    )
    return result.scalar() is not None


def upgrade() -> None:
    # ExcessLineItem.normalized_part_number
    if not _column_exists("excess_line_items", "normalized_part_number"):
        op.add_column(
            "excess_line_items",
            sa.Column("normalized_part_number", sa.String(100), nullable=True),
        )
    if not _index_exists("ix_excess_line_items_normalized_part_number"):
        op.create_index(
            "ix_excess_line_items_normalized_part_number",
            "excess_line_items",
            ["normalized_part_number"],
        )

    # Offer.excess_line_item_id
    if not _column_exists("offers", "excess_line_item_id"):
        op.add_column(
            "offers",
            sa.Column("excess_line_item_id", sa.Integer(), nullable=True),
        )
    # FK — COLUMN-scoped pg_constraint check: skip when ANY foreign key already
    # covers offers.excess_line_item_id -> excess_line_items, whatever its name.
    # The original name-scoped guard (conname = 'fk_offers_excess_line_item_id')
    # missed the baseline's 'offers_excess_line_item_id_fkey' (001 creates this FK
    # under PostgreSQL's default name), so a fresh-DB chain replay created a
    # duplicate second FK on the column; 188_canonical_offers_excess_fk removes
    # the stray, and this guard stops replays from ever recreating it.
    conn = op.get_bind()
    fk_exists = conn.execute(
        sa.text(
            "SELECT 1 FROM pg_constraint con "
            "WHERE con.contype = 'f' "
            "AND con.conrelid = to_regclass('offers') "
            "AND con.confrelid = to_regclass('excess_line_items') "
            "AND con.conkey = ARRAY[(SELECT attnum FROM pg_attribute "
            "WHERE attrelid = con.conrelid AND attname = 'excess_line_item_id')]::smallint[]"
        )
    ).scalar()
    if not fk_exists:
        op.create_foreign_key(
            "offers_excess_line_item_id_fkey",
            "offers",
            "excess_line_items",
            ["excess_line_item_id"],
            ["id"],
            ondelete="SET NULL",
        )
    if not _index_exists("ix_offers_excess_line_item"):
        op.create_index(
            "ix_offers_excess_line_item",
            "offers",
            ["excess_line_item_id"],
        )


def downgrade() -> None:
    op.drop_index("ix_offers_excess_line_item", table_name="offers", if_exists=True)
    # env.py's drop_constraint wrapper skips a missing name, and the raw
    # DROP COLUMN below cascades any FK still covering the column — so this
    # drop is safe whichever of the historical FK names a database carries.
    op.drop_constraint("offers_excess_line_item_id_fkey", "offers", type_="foreignkey")
    op.execute("ALTER TABLE IF EXISTS offers DROP COLUMN IF EXISTS excess_line_item_id")

    op.drop_index(
        "ix_excess_line_items_normalized_part_number",
        table_name="excess_line_items",
        if_exists=True,
    )
    op.execute("ALTER TABLE IF EXISTS excess_line_items DROP COLUMN IF EXISTS normalized_part_number")
