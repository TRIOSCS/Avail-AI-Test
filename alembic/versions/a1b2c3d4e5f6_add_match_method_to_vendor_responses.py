"""Add match_method column to vendor_responses.

Revision ID: a1b2c3d4e5f6
Revises: ed9318daa67c, 049_pg_trgm, d1a2b3c4e5f6
Create Date: 2026-03-30 02:50:00.000000
"""

from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a1b2c3d4e5f6"
down_revision: Union[str, None] = ("ed9318daa67c", "049_pg_trgm", "d1a2b3c4e5f6")
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("ALTER TABLE vendor_responses ADD COLUMN IF NOT EXISTS match_method VARCHAR(50)")


def downgrade() -> None:
    op.execute("ALTER TABLE vendor_responses DROP COLUMN IF EXISTS match_method")
