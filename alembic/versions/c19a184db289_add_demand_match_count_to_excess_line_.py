"""Add demand_match_count to excess_line_items.

Revision ID: c19a184db289
Revises: c68ec71457b6
Create Date: 2026-03-20 04:16:22.630884
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "c19a184db289"
down_revision: str | None = "c68ec71457b6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("excess_line_items", sa.Column("demand_match_count", sa.Integer(), nullable=True, server_default="0"))


def downgrade() -> None:
    op.execute("ALTER TABLE IF EXISTS excess_line_items DROP COLUMN IF EXISTS demand_match_count")
