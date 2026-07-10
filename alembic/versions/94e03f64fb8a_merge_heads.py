"""Merge heads.

Revision ID: 94e03f64fb8a
Revises: 0e6840111cfd, 4724fcfde85e
Create Date: 2026-03-24 15:55:00.000000
"""

from collections.abc import Sequence

# revision identifiers, used by Alembic.
revision: str = "94e03f64fb8a"
down_revision: str | None = ("0e6840111cfd", "4724fcfde85e")
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
