"""Merge heads.

Revision ID: a4df7e282b71
Revises: 050_check_constraints, 8e06fcdc5740
Create Date: 2026-03-21 02:56:33.703789
"""

from collections.abc import Sequence

# revision identifiers, used by Alembic.
revision: str = "a4df7e282b71"
down_revision: str | None = ("050_check_constraints", "8e06fcdc5740")
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
