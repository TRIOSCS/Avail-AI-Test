"""Merge heads.

Revision ID: 885342959628
Revises: a3f9c1d82e47, b7e2a4c91f35
Create Date: 2026-03-29 19:55:28.858332
"""

from typing import Sequence, Union

# revision identifiers, used by Alembic.
revision: str = "885342959628"
down_revision: Union[str, None] = ("a3f9c1d82e47", "b7e2a4c91f35")
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
