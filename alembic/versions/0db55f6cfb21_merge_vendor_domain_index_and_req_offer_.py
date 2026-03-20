"""Merge vendor domain index and req offer fields heads.

Revision ID: 0db55f6cfb21
Revises: 8e06fcdc5740, a7b8c9d0e1f2
Create Date: 2026-03-20 21:08:28.397084
"""

from typing import Sequence, Union

# revision identifiers, used by Alembic.
revision: str = "0db55f6cfb21"
down_revision: Union[str, None] = ("8e06fcdc5740", "a7b8c9d0e1f2")
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
