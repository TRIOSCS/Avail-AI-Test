"""Add demand_match_count to excess_line_items.

Revision ID: c19a184db289
Revises: c68ec71457b6
Create Date: 2026-03-20 04:16:22.630884
"""

from typing import Sequence, Union

from alembic import op

revision: str = "c19a184db289"
down_revision: Union[str, None] = "c68ec71457b6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("ALTER TABLE excess_line_items ADD COLUMN IF NOT EXISTS demand_match_count INTEGER DEFAULT '0'")


def downgrade() -> None:
    op.execute("ALTER TABLE excess_line_items DROP COLUMN IF EXISTS demand_match_count")
