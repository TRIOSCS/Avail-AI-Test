"""Merge three orphan migration heads into one.

Revision ID: ba090b14bf74
Revises: 049_pg_trgm, fb863358a701, d1a2b3c4e5f6 (merged)
Create Date: 2026-03-20 20:31:19.730652
"""

from collections.abc import Sequence

# revision identifiers, used by Alembic.
revision: str = "ba090b14bf74"
down_revision: str | None = (
    "049_pg_trgm",
    "fb863358a701",
    "d1a2b3c4e5f6",
)
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
