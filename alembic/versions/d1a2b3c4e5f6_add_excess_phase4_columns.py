"""Add excess phase 4 columns: normalized_part_number, excess_line_item_id.

Adds ExcessLineItem.normalized_part_number (VARCHAR 100, nullable, indexed)
and Offer.excess_line_item_id (INTEGER, nullable, FK to excess_line_items.id,
ondelete SET NULL) with index.

Idempotent: uses IF NOT EXISTS / column existence checks since these columns
may already exist from a prior run of the original d1a2b3c4e5f6 migration.

Revision ID: d1a2b3c4e5f6
Revises: c19a184db289
Create Date: 2026-03-20 12:00:00.000000
"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

revision: str = "d1a2b3c4e5f6"
down_revision: Union[str, None] = "c19a184db289"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


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
    # FK — check via pg_constraint
    conn = op.get_bind()
    fk_exists = conn.execute(
        sa.text("SELECT 1 FROM pg_constraint WHERE conname = 'fk_offers_excess_line_item_id'")
    ).scalar()
    if not fk_exists:
        op.create_foreign_key(
            "fk_offers_excess_line_item_id",
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
    op.drop_index("ix_offers_excess_line_item", table_name="offers")
    op.drop_constraint("fk_offers_excess_line_item_id", "offers", type_="foreignkey")
    op.drop_column("offers", "excess_line_item_id")

    op.drop_index(
        "ix_excess_line_items_normalized_part_number",
        table_name="excess_line_items",
    )
    op.drop_column("excess_line_items", "normalized_part_number")
