"""Merge p2.7 partial indexes and p3.1 assigned_buyer_id index.

Revision ID: 1223a56cbbbb
Revises: 187_startup_backfill_partial_idx, 71d3fef96529
Create Date: 2026-07-09 06:39:49.893060
"""

from typing import Sequence, Union

# revision identifiers, used by Alembic.
revision: str = "1223a56cbbbb"
down_revision: Union[str, None] = ("187_startup_backfill_partial_idx", "71d3fef96529")
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
