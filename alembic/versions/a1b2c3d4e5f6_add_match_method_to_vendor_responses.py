"""Add match_method column to vendor_responses.

Revision ID: a1b2c3d4e5f6
Revises: ed9318daa67c, 049_pg_trgm, d1a2b3c4e5f6
Create Date: 2026-03-30 02:50:00.000000
"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a1b2c3d4e5f6"
down_revision: Union[str, None] = ("ed9318daa67c", "049_pg_trgm", "d1a2b3c4e5f6")
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("vendor_responses", sa.Column("match_method", sa.String(50), nullable=True))


def downgrade() -> None:
    op.drop_column("vendor_responses", "match_method")
