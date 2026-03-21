"""Merge heads.

Revision ID: a4df7e282b71
Revises: 050_check_constraints, 8e06fcdc5740
Create Date: 2026-03-21 02:56:33.703789
"""

from typing import Sequence, Union

# revision identifiers, used by Alembic.
revision: str = "a4df7e282b71"
down_revision: Union[str, None] = ("050_check_constraints", "8e06fcdc5740")
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
